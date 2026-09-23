import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from review_sensei.errors import ReviewInputError
from review_sensei.workflow import (
    ReviewExecutionPlan,
    normalize_review_sensei_version,
    parse_prepare_diff_args,
    plan_review_execution,
    prepare_diff,
)


def _git(repository: Path, *argv: str) -> None:
    subprocess.run(
        ["git", *argv],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )


def _write(repository: Path, name: str, content: str) -> None:
    path = repository / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _init_repository(root: Path, name: str) -> Path:
    repository = root / name
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.email", "test@example.com")
    _git(repository, "config", "user.name", "Test")
    _write(repository, "base.txt", "base\n")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "base")
    return repository


def _reusable_workflow() -> str:
    return (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "review-sensei-run.yml"
    ).read_text(encoding="utf-8")


class WorkflowValidationTests(unittest.TestCase):
    def test_reusable_workflow_defaults_and_rejects_retired_legacy_mode(self):
        workflow = _reusable_workflow()
        self.assertIn(
            "review_mode:\n        required: false\n        default: merge-focused",
            workflow,
        )
        self.assertIn("REVIEW_MODE: ${{ inputs.review_mode }}", workflow)
        # The positive form above would still pass if a second, later default
        # re-enabled the retired mode, so pin the retired value out entirely.
        self.assertNotIn("default: legacy", workflow)
        self.assertIn(
            "legacy review mode is retired; migrate configuration to merge-focused",
            workflow,
        )
        self.assertIn("merge the pending setup-v5 pull request", workflow)
        legacy_line = next(
            line for line in workflow.splitlines() if "legacy) echo" in line
        )
        self.assertIn("REVIEWSENSEI_REVIEW_MODE", legacy_line)

    def test_reusable_workflow_review_mode_case_rejects_legacy_at_runtime(self):
        # Git Bash is present on the Windows compatibility runners, so the
        # guard is executed there too; only a host without any POSIX bash is
        # skipped, with the reason reported.
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("no POSIX bash is available to execute the workflow guard")
        workflow = _reusable_workflow()
        # Execute the guard the workflow itself runs instead of trusting that
        # the literal is still wired the way the assertions above describe.
        # The marker must occur exactly once and inside the named step, so a
        # second copy elsewhere can never be sliced instead of the real guard.
        marker = 'case "$REVIEW_MODE" in'
        self.assertEqual(workflow.count(marker), 1)
        step_start = workflow.index("      - name: Reject unsupported provider mode")
        step_end = workflow.find("\n      - name:", step_start + 1)
        self.assertNotEqual(step_end, -1)
        guard_step = workflow[step_start:step_end]
        self.assertIn(marker, guard_step)
        # Execute the guard's own normalization together with the review-mode
        # case, both verbatim, so the executed value is the one the case reads
        # rather than the raw input. The intervening provider-mode cases are
        # unrelated to this behavior and are not part of the slice.
        normalize = 'REVIEW_MODE="$(printf'
        self.assertIn(normalize, guard_step)
        normalize_start = step_start + guard_step.index(normalize)
        normalize_end = workflow.index('case "$PROVIDER_MODE" in', normalize_start)
        case_start = step_start + guard_step.index(marker)
        case_end = workflow.index("esac", case_start) + len("esac")
        guard = (
            workflow[normalize_start:normalize_end]
            + "\n"
            + workflow[case_start:case_end]
        )

        def run(mode: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                [bash, "-c", guard],
                env={**os.environ, "REVIEW_MODE": mode},
                capture_output=True,
                text=True,
            )

        for mode in ("advisory", "merge-focused", "strict"):
            with self.subTest(mode=mode):
                completed = run(mode)
                self.assertEqual(completed.returncode, 0, completed.stderr)
        legacy = run("legacy")
        self.assertEqual(legacy.returncode, 1)
        self.assertIn("legacy review mode is retired", legacy.stderr)
        self.assertIn("REVIEWSENSEI_REVIEW_MODE", legacy.stderr)
        # The guard normalizes exactly like the CLI resolves the mode, so a
        # padded or upper-cased retired value takes the migration arm instead
        # of the generic rejection.
        for mode in (" LEGACY ", "Legacy"):
            with self.subTest(mode=mode):
                normalized_legacy = run(mode)
                self.assertEqual(normalized_legacy.returncode, 1)
                self.assertIn("legacy review mode is retired", normalized_legacy.stderr)
        for mode in (" MERGE-FOCUSED ", "Merge-Focused"):
            with self.subTest(mode=mode):
                normalized_supported = run(mode)
                self.assertEqual(
                    normalized_supported.returncode, 0, normalized_supported.stderr
                )
        bogus = run("bogus")
        self.assertEqual(bogus.returncode, 1)
        self.assertIn(
            "review mode must be advisory, merge-focused, or strict", bogus.stderr
        )
        # The input is optional with a `merge-focused` default, so an absent or
        # empty value resolves to the default here as it does in the CLI;
        # callers that pin the tag without passing the input must not fail.
        empty = run("")
        self.assertEqual(empty.returncode, 0, empty.stderr)
        self.assertNotIn("::error::", empty.stderr)

    def test_authoritative_execution_plan_binds_identity_and_eligibility(self):
        plan = plan_review_execution(
            repository="owner/repo",
            repository_id=42,
            pull_request_number=7,
            base_ref="main",
            base_sha="a" * 40,
            head_ref="feature",
            head_repository="owner/repo",
            head_sha="b" * 40,
            title="Improve review",
        )
        self.assertIsInstance(plan, ReviewExecutionPlan)
        self.assertTrue(plan.eligible)
        self.assertIsNone(plan.skip_reason)
        self.assertEqual(plan.to_dict()["head_sha"], "b" * 40)

    def test_authoritative_execution_plan_skips_stale_or_ineligible_prs(self):
        common = dict(
            repository="owner/repo",
            repository_id=42,
            pull_request_number=7,
            base_ref="main",
            base_sha="a" * 40,
            head_ref="feature",
            head_repository="owner/repo",
            head_sha="b" * 40,
        )
        self.assertEqual(
            plan_review_execution(**common, state="closed").skip_reason,
            "pr_not_open",
        )
        self.assertEqual(
            plan_review_execution(**common, draft=True).skip_reason,
            "draft_pr",
        )
        self.assertEqual(
            plan_review_execution(
                **{**common, "head_repository": "fork/repo"}
            ).skip_reason,
            "fork_not_allowed",
        )

    def test_authoritative_execution_plan_rejects_malformed_identity(self):
        with self.assertRaises(ReviewInputError):
            plan_review_execution(
                repository="owner/repo",
                repository_id=1,
                pull_request_number=1,
                base_ref="main",
                base_sha="not-a-sha",
                head_ref="feature",
                head_repository="owner/repo",
                head_sha="b" * 40,
            )

    def test_malicious_refs_are_rejected_without_git(self):
        for value in (
            "-o",
            "main\n",
            "main; touch /tmp/pwned",
            "main space",
            "main$(id)",
            "main`id`",
            "main|cat",
            "main&cat",
            "main>out",
            "main<in",
            "main*",
            "main?",
            "main[",
            "main]",
            "main{",
            "main}",
            "main(",
            "main)",
            "main\\",
            "main'",
            'main"',
            "main!",
            "main$",
            "main\x00",
            "main//feature",
            "main/../feature",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ReviewInputError):
                    parse_prepare_diff_args(base_ref=value, head_ref="main")

    def test_ref_validation_rejects_missing_or_non_string_values(self):
        for value in (None, "", 1, [], b"main"):
            with self.subTest(value=value):
                with self.assertRaises(ReviewInputError):
                    parse_prepare_diff_args(base_ref=value, head_ref="main")
                with self.assertRaises(ReviewInputError):
                    parse_prepare_diff_args(base_ref="main", head_ref=value)

    def test_fork_repository_rejects_non_string_slugs(self):
        for value in ("", 1, None):
            with self.subTest(value=value):
                if value is None:
                    continue
                with self.assertRaises(ReviewInputError):
                    parse_prepare_diff_args(
                        base_ref="main",
                        head_ref="feature",
                        head_repository=value,
                    )

    def test_positive_int_bounds_are_validated(self):
        with self.assertRaises(ReviewInputError):
            parse_prepare_diff_args(
                base_ref="main",
                head_ref="feature",
                max_diff_bytes=0,
            )
        with self.assertRaises(ReviewInputError):
            parse_prepare_diff_args(
                base_ref="main",
                head_ref="feature",
                max_diff_bytes=True,
            )
        with self.assertRaises(ReviewInputError):
            parse_prepare_diff_args(
                base_ref="main",
                head_ref="feature",
                max_diff_bytes=1_048_577,
            )

    def test_repository_and_output_must_be_paths(self):
        with self.assertRaises(ReviewInputError):
            parse_prepare_diff_args(
                base_ref="main",
                head_ref="feature",
                repository="not-a-path",
            )
        with self.assertRaises(ReviewInputError):
            parse_prepare_diff_args(
                base_ref="main",
                head_ref="feature",
                output="not-a-path",
            )

    def test_missing_refs_fail_with_sanitized_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = _init_repository(root, "repo")
            with self.assertRaises(ReviewInputError) as raised:
                prepare_diff(
                    base_ref="main",
                    head_ref="missing",
                    repository=repository,
                    output=root / "pr.patch",
                )
            self.assertNotIn("missing", str(raised.exception))

    def test_fork_repository_accepts_slug_and_rejects_urls_paths_and_options(self):
        args = parse_prepare_diff_args(
            base_ref="main",
            head_ref="feature",
            head_repository="owner/repo",
        )
        self.assertEqual(args.head_repository, "owner/repo")
        for value in (
            "https://github.com/owner/repo",
            "owner/repo.git",
            "/tmp/repo",
            "-o",
            "owner/repo extra",
            "owner/repo;id",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ReviewInputError):
                    parse_prepare_diff_args(
                        base_ref="main",
                        head_ref="feature",
                        head_repository=value,
                    )

    def test_head_remote_is_a_test_seam_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = _init_repository(root, "repo")
            with self.assertRaises(ReviewInputError):
                prepare_diff(
                    base_ref="main",
                    head_ref="feature",
                    head_repository="owner/repo",
                    head_remote="bad remote",
                    repository=repository,
                    output=root / "pr.patch",
                )

    def test_version_validation(self):
        self.assertEqual(normalize_review_sensei_version("0.1.0"), "0.1.0")
        self.assertEqual(normalize_review_sensei_version("v0.1.0"), "0.1.0")
        for value in (
            "latest",
            "0.1",
            "0.1.0a1",
            "v1",
            "1.2.3.4",
            "0.1.0\n",
            "0.1.0 ",
            "v 0.1.0",
            None,
            "",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ReviewInputError):
                    normalize_review_sensei_version(value)


class WorkflowFixtureTests(unittest.TestCase):
    def test_local_fixture_prepares_bounded_diff(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = _init_repository(root, "repo")
            _git(repository, "checkout", "-b", "feature")
            _write(repository, "feature.txt", "feature\n")
            _git(repository, "add", ".")
            _git(repository, "commit", "-m", "feature")
            output = root / "pr.patch"
            prepare_diff(
                base_ref="main",
                head_ref="feature",
                repository=repository,
                output=output,
            )
            self.assertTrue(output.exists())
            self.assertIn("feature.txt", output.read_text(encoding="utf-8"))

    def test_oversized_diff_is_rejected_and_patch_is_cleaned_up(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = _init_repository(root, "repo")
            _git(repository, "checkout", "-b", "feature")
            _write(repository, "large.txt", "x" * 4096)
            _git(repository, "add", ".")
            _git(repository, "commit", "-m", "feature")
            output = root / "pr.patch"
            with self.assertRaises(ReviewInputError):
                prepare_diff(
                    base_ref="main",
                    head_ref="feature",
                    repository=repository,
                    output=output,
                    max_diff_bytes=64,
                )
            self.assertFalse(output.exists())

    def test_line_file_and_hunk_limit_failures_do_not_publish_patch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = _init_repository(root, "repo")
            _git(repository, "checkout", "-b", "feature")
            _write(repository, "one.txt", "one\n")
            _write(repository, "two.txt", "two\n")
            _git(repository, "add", ".")
            _git(repository, "commit", "-m", "feature")
            output = root / "pr.patch"
            for limits in (
                {"max_diff_lines": 1},
                {"max_diff_files": 1},
                {"max_diff_hunks": 1},
            ):
                with self.subTest(limits=limits):
                    with self.assertRaises(ReviewInputError):
                        prepare_diff(
                            base_ref="main",
                            head_ref="feature",
                            repository=repository,
                            output=output,
                            **limits,
                        )
                    self.assertFalse(output.exists())

    def test_fork_fetch_does_not_checkout_head(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = _init_repository(root, "base")
            fork = root / "fork"
            _git(root, "clone", str(base), str(fork))
            _git(fork, "config", "user.email", "test@example.com")
            _git(fork, "config", "user.name", "Test")
            _git(fork, "checkout", "-b", "feature")
            _write(fork, "fork.txt", "fork\n")
            _git(fork, "add", ".")
            _git(fork, "commit", "-m", "fork feature")
            output = root / "pr.patch"
            prepare_diff(
                base_ref="main",
                head_ref="feature",
                head_repository="owner/fork",
                head_remote=fork.as_posix(),
                repository=base,
                output=output,
            )
            self.assertTrue(output.exists())
            self.assertIn("fork.txt", output.read_text(encoding="utf-8"))
            self.assertFalse((base / "fork.txt").exists())

    def test_shallow_repository_is_unshallowed_before_diffing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            upstream = _init_repository(root, "upstream")
            _git(upstream, "checkout", "-b", "feature")
            _write(upstream, "feature.txt", "feature\n")
            _git(upstream, "add", ".")
            _git(upstream, "commit", "-m", "feature")
            shallow = root / "shallow"
            _git(root, "clone", "--depth", "1", str(upstream), str(shallow))
            _git(shallow, "config", "user.email", "test@example.com")
            _git(shallow, "config", "user.name", "Test")
            output = root / "pr.patch"
            prepare_diff(
                base_ref="main",
                head_ref="feature",
                repository=shallow,
                output=output,
            )
            self.assertTrue(output.exists())
            self.assertIn("feature.txt", output.read_text(encoding="utf-8"))

    def test_divergent_branches_fail_with_sanitized_merge_base_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = _init_repository(root, "repo")
            _git(repository, "checkout", "--orphan", "other")
            _write(repository, "other.txt", "other\n")
            _git(repository, "add", ".")
            _git(repository, "commit", "-m", "other")
            with self.assertRaises(ReviewInputError) as raised:
                prepare_diff(
                    base_ref="main",
                    head_ref="other",
                    repository=repository,
                    output=root / "pr.patch",
                )
            self.assertNotIn("other", str(raised.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
