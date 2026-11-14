"""Invalid review input cannot leave a newly enrolled caller behind."""

from __future__ import annotations

import copy
import subprocess
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import test_review_transaction as transactions
from review_sensei_github import cli, review_run
from test_review_run import EntryFixture

from review_sensei.errors import ReviewInputError
from review_sensei.validation import DEFAULT_REVIEW_LIMITS


class ReviewStartupInputTests(unittest.TestCase):
    @staticmethod
    def invoke_generated(fixture):
        original = cli.main

        def main(argv):
            argv = list(argv)
            index = argv.index("--diff")
            del argv[index : index + 2]
            return original(argv)

        with patch.object(cli, "main", side_effect=main):
            return fixture.invoke()

    def assert_no_enrollment(self, fixture, previous, *, provider_calls=0):
        self.assertEqual(fixture.comment, previous)
        self.assertEqual(fixture.provider_calls, provider_calls)
        self.assertIsNone(fixture.selected)
        self.assertFalse(
            [(method, path) for _, method, path in fixture.trace if method != "GET"]
        )
        self.assertFalse(any("session" in path for _, _, path in fixture.trace))

    def test_supplied_invalid_input_refuses_before_enrollment(self):
        for content, message in (
            (b"\xff", "UTF-8"),
            (b"not a patch\n", "diff"),
            (b"x" * (DEFAULT_REVIEW_LIMITS.max_diff_bytes + 1), "size limit"),
        ):
            with (
                self.subTest(message=message),
                tempfile.TemporaryDirectory() as directory,
            ):
                fixture = EntryFixture(directory)
                previous = copy.deepcopy(fixture.comment)
                (fixture.root / "review.patch").write_bytes(content)
                self.assertEqual(fixture.invoke(), 1)
                self.assertIn(message.lower(), fixture.error.lower())
                self.assert_no_enrollment(fixture, previous)

    def test_invalid_input_preserves_existing_unclosed_predecessor(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = EntryFixture(directory)
            fixture.before_provider = lambda: (_ for _ in ()).throw(
                ReviewInputError("fixture entered provider refusal")
            )
            self.assertEqual(fixture.invoke(), 1)
            previous = copy.deepcopy(fixture.comment)
            fixture.next_run()
            fixture.selected = None
            (fixture.root / "review.patch").write_bytes(b"\xff")
            self.assertEqual(fixture.invoke(), 1)
            self.assert_no_enrollment(fixture, previous, provider_calls=1)

    def test_preparation_failure_and_cancellation_do_not_enroll(self):
        for error, code in (
            (ReviewInputError("git diff failed"), 1),
            (KeyboardInterrupt(), 130),
        ):
            with (
                self.subTest(error=type(error).__name__),
                tempfile.TemporaryDirectory() as directory,
            ):
                fixture = EntryFixture(directory)
                previous = copy.deepcopy(fixture.comment)
                with patch.object(review_run, "prepare_diff", side_effect=error):
                    self.assertEqual(self.invoke_generated(fixture), code)
                self.assert_no_enrollment(fixture, previous)

    def test_real_oversized_git_diff_does_not_enroll(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = EntryFixture(directory)
            previous = copy.deepcopy(fixture.comment)

            def git(*args):
                return subprocess.check_output(
                    [
                        "git",
                        "-c",
                        "user.name=Fixture",
                        "-c",
                        "user.email=fixture@example.invalid",
                        *args,
                    ],
                    cwd=fixture.root,
                    stderr=subprocess.PIPE,
                    text=True,
                ).strip()

            base = git("rev-parse", "HEAD")
            (fixture.root / "large.txt").write_text(
                "x" * (DEFAULT_REVIEW_LIMITS.max_diff_bytes + 1)
            )
            git("add", "large.txt")
            git("commit", "-m", "oversized diff fixture")
            head = git("rev-parse", "HEAD")
            git("checkout", "--detach", fixture.policy)
            fixture.live_base = fixture.requested_base = base
            fixture.live_head = fixture.requested_head = head
            (fixture.root / "review.patch").unlink()
            self.assertEqual(self.invoke_generated(fixture), 1)
            self.assertIn("diff exceeds the configured byte limit", fixture.error)
            self.assert_no_enrollment(fixture, previous)
            self.assertFalse((fixture.root / "review.patch").exists())
            self.assertFalse(list(fixture.root.glob("review-sensei-diff-*")))

    def test_valid_fresh_run_after_input_refusal_has_no_stranded_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = EntryFixture(directory)
            previous = copy.deepcopy(fixture.comment)
            (fixture.root / "review.patch").write_bytes(b"\xff")
            self.assertEqual(fixture.invoke(), 1)
            self.assert_no_enrollment(fixture, previous)
            claims = {p.name: p.read_bytes() for p in fixture.root.glob("*.claim")}
            self.assertEqual(len(claims), 1)
            refused_run = fixture.run_id
            fixture.next_run()
            self.assertNotEqual(fixture.run_id, refused_run)
            (fixture.root / "review.patch").write_text(
                transactions.HostedAnalysisCliTests.ANALYSIS_DIFF, encoding="utf-8"
            )
            self.assertEqual(fixture.invoke(), 0, fixture.error)
            self.assertEqual(fixture.provider_calls, 1)
            self.assertEqual(
                {name: (fixture.root / name).read_bytes() for name in claims}, claims
            )
            self.assertEqual(len(list(fixture.root.glob("*.claim"))), 2)
            self.assertIsNotNone(fixture.accounted.accounting.terminal_transaction)
            self.assertEqual(fixture.accounted.accounting.configuration.limit, 512)

    def test_supplied_large_change_modes_keep_existing_input_bounds(self):
        # Two individually bounded files exceed the ordinary total bound.
        diff = "".join(
            f"diff --git a/{name} b/{name}\nnew file mode 100644\n"
            f"--- /dev/null\n+++ b/{name}\n@@ -0,0 +1,6000 @@\n"
            + ("+" + "x" * 99 + "\n")
            * 6000
            for name in ("one.txt", "two.txt")
        )
        self.assertGreater(len(diff.encode()), DEFAULT_REVIEW_LIMITS.max_diff_bytes)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "review.patch"
            source.write_text(diff, encoding="utf-8")
            for mode, orchestrate in (
                ("legacy", False),
                ("legacy", True),
                ("unified", False),
            ):
                with self.subTest(mode=mode, orchestrate=orchestrate):
                    config = root / "config.yml"
                    config.write_text(
                        "schema: 1\nadvanced:\n  review_work:\n    mode: " + mode + "\n"
                    )
                    args = Namespace(diff=source, orchestrate_large_changes=orchestrate)
                    if mode == "legacy" and not orchestrate:
                        with self.assertRaisesRegex(ReviewInputError, "size limit"):
                            review_run._prepare_review_input(
                                args,
                                directory=root,
                                configuration_path=config,
                                head_repository="owner/repo",
                            )
                    else:
                        review_run._prepare_review_input(
                            args,
                            directory=root,
                            configuration_path=config,
                            head_repository="owner/repo",
                        )

    def test_restart_preparation_cancellation_retains_claim_without_activation(
        self,
    ):
        from test_controlled_restart_journeys import RestartJourney

        with tempfile.TemporaryDirectory() as directory:
            journey = RestartJourney(directory)
            journey.actual_diff = True
            original = copy.deepcopy(journey.old)
            paths = []
            claims = {}

            def cancel(**kwargs):
                paths.append(kwargs["output"])
                self.assertFalse(
                    (journey.fixture.root / "abandoned-history.json").exists()
                )
                claims.update(
                    {
                        p.name: p.read_bytes()
                        for p in journey.fixture.root.glob("*.claim")
                    }
                )
                self.assertEqual(len(claims), 1)
                raise KeyboardInterrupt()

            with patch.object(review_run, "prepare_diff", side_effect=cancel):
                self.assertEqual(journey.invoke(), 130)
            self.assertEqual(journey.old, original)
            self.assert_no_enrollment(journey.fixture, None)
            self.assertEqual(len(paths), 1)
            self.assertFalse(paths[0].parent.exists())
            self.assertFalse((journey.fixture.root / "abandoned-history.json").exists())
            self.assertEqual(
                {p.name: p.read_bytes() for p in journey.fixture.root.glob("*.claim")},
                claims,
            )

    def test_real_unrelated_git_history_refuses_before_enrollment(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = EntryFixture(directory)
            previous = copy.deepcopy(fixture.comment)

            def git(*args):
                return subprocess.check_output(
                    [
                        "git",
                        "-c",
                        "user.name=Fixture",
                        "-c",
                        "user.email=fixture@example.invalid",
                        *args,
                    ],
                    cwd=fixture.root,
                    stderr=subprocess.PIPE,
                    text=True,
                ).strip()

            git("checkout", "--orphan", "unrelated-input")
            git("commit", "--allow-empty", "-m", "unrelated local fixture")
            head = git("rev-parse", "HEAD")
            git("checkout", "--detach", fixture.policy)
            fixture.live_base = fixture.requested_base = fixture.policy
            fixture.live_head = fixture.requested_head = head
            (fixture.root / "review.patch").unlink()
            self.assertEqual(self.invoke_generated(fixture), 1)
            self.assertIn("do not share a merge base", fixture.error)
            self.assert_no_enrollment(fixture, previous)
            self.assertFalse((fixture.root / "review.patch").exists())
