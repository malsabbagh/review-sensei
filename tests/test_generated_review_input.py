"""Generated review input keeps the configured mode's existing bounded contract."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from argparse import Namespace
from contextlib import chdir
from pathlib import Path

from review_sensei_github.review_run import _prepare_review_input

from review_sensei.errors import ReviewInputError
from review_sensei.validation import DEFAULT_REVIEW_LIMITS, DEFAULT_TOTAL_WORK_BUDGET
from review_sensei.workflow import prepare_diff


def git(root, *args):
    return subprocess.check_output(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            *args,
        ],
        cwd=root,
        stderr=subprocess.PIPE,
    )


def history(root, files):
    git(root, "init", "-b", "main")
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "base")
    base = git(root, "rev-parse", "HEAD").decode().strip()
    for name, content in files.items():
        (root / name).write_text(content, encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "change")
    return base, git(root, "rev-parse", "HEAD").decode().strip()


class GeneratedReviewInputTests(unittest.TestCase):
    def test_generated_and_supplied_large_inputs_use_the_same_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base, head = history(
                root, {name: ("x" * 99 + "\n") * 6000 for name in ("one", "two")}
            )
            exact = git(root, "diff", f"{base}...{head}")
            self.assertGreater(len(exact), DEFAULT_REVIEW_LIMITS.max_diff_bytes)
            for mode, orchestrate in (
                ("legacy", False),
                ("legacy", True),
                ("unified", False),
            ):
                with self.subTest(mode=mode, orchestrate=orchestrate):
                    config = root / "config.yml"
                    config.write_text(
                        "schema: 1\nadvanced:\n  review_work:\n    mode: "
                        + mode
                        + "\n",
                        encoding="utf-8",
                    )
                    output = root / (mode + str(orchestrate))
                    output.mkdir()
                    args = Namespace(
                        diff=None,
                        repository="owner/repo",
                        base_sha=base,
                        head_sha=head,
                        orchestrate_large_changes=orchestrate,
                    )
                    with chdir(root):
                        if mode == "legacy" and not orchestrate:
                            with self.assertRaisesRegex(ReviewInputError, "byte limit"):
                                _prepare_review_input(
                                    args,
                                    directory=output,
                                    configuration_path=config,
                                    head_repository="owner/repo",
                                )
                            self.assertFalse(args.diff.exists())
                        else:
                            _prepare_review_input(
                                args,
                                directory=output,
                                configuration_path=config,
                                head_repository="owner/repo",
                            )
                            self.assertEqual(args.diff.read_bytes(), exact)
                            # The exact same bytes still qualify when supplied.
                            _prepare_review_input(
                                args,
                                directory=output,
                                configuration_path=config,
                                head_repository="owner/repo",
                            )

    def test_aggregate_overflow_preserves_prior_output_and_removes_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base, head = history(
                root,
                {"large": "x" * (DEFAULT_TOTAL_WORK_BUDGET.max_total_diff_bytes + 1)},
            )
            output = root / "review.patch"
            output.write_bytes(b"previous owned input")
            with self.assertRaisesRegex(ReviewInputError, "byte limit"):
                prepare_diff(
                    repository=root,
                    base_ref=base,
                    head_ref=head,
                    output=output,
                    aggregate_work=True,
                )
            self.assertEqual(output.read_bytes(), b"previous owned input")
            self.assertFalse(list(root.glob("review-sensei-diff-*")))

    def test_aggregate_line_inventory_is_not_truncated_at_single_request_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            count = DEFAULT_REVIEW_LIMITS.max_diff_lines + 1
            base, head = history(root, {"lines": "x\n" * count})
            output = root / "review.patch"
            with self.assertRaisesRegex(ReviewInputError, "line limit"):
                prepare_diff(
                    repository=root, base_ref=base, head_ref=head, output=output
                )
            self.assertFalse(output.exists())
            prepare_diff(
                repository=root,
                base_ref=base,
                head_ref=head,
                output=output,
                aggregate_work=True,
            )
            self.assertEqual(output.read_bytes(), git(root, "diff", f"{base}...{head}"))
            self.assertEqual(output.read_bytes().count(b"+x\n"), count)

    def test_aggregate_mode_cannot_supply_new_limit_values(self):
        for options in (
            {"aggregate_work": 1},
            {"aggregate_work": True, "max_diff_bytes": 2**30},
            {"aggregate_work": True, "max_diff_lines": 1},
            {"aggregate_work": True, "max_diff_files": 1},
            {"aggregate_work": True, "max_diff_hunks": 1},
        ):
            with self.subTest(options=options), self.assertRaises(ReviewInputError):
                prepare_diff(base_ref="main", head_ref="main", **options)


if __name__ == "__main__":
    unittest.main()
