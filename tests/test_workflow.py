import os
import re
import subprocess
import sys
import tempfile
import textwrap
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
    def test_reusable_workflow_declares_only_invocation_inputs(self):
        from review_sensei.configuration import (
            RETIRED_BEHAVIOR_ENVIRONMENT_SETTINGS,
            RETIRED_ENVIRONMENT_SETTINGS,
        )

        workflow = _reusable_workflow()
        inputs = workflow.split("    inputs:\n", 1)[1].split("\n    secrets:", 1)[0]
        declared = re.findall(r"^      ([a-z_]+):$", inputs, re.M)
        self.assertEqual(
            declared,
            [
                "mode",
                "operation",
                "repository",
                "repository_id",
                "pull_request_number",
                "base_ref",
                "base_sha",
                "head_ref",
                "head_repository",
                "head_sha",
                "source_kind",
                "source_comment_id",
                "source_updated_at",
                "root_comment_id",
                "comment_body",
                "comment_actor",
                "comment_actor_type",
                "comment_association",
            ],
        )
        # An invocation carries identity and payload only. Backend, model,
        # endpoint, credential, and policy are package decisions, so no input
        # may name any of them.
        for token in (
            "provider",
            "model",
            "base_url",
            "credential",
            "review_mode",
            "enable_",
        ):
            self.assertNotIn(token, inputs)
        # The complete set of repository variables this workflow reads: the two
        # supported overrides, plus the retired names the diagnostic report maps
        # so it can warn about them. Nothing else is read from `vars`.
        retired = (
            set(RETIRED_BEHAVIOR_ENVIRONMENT_SETTINGS)
            | set(RETIRED_ENVIRONMENT_SETTINGS)
        )
        managed = sorted(
            name for name in retired if name.startswith("REVIEWSENSEI_")
        ) + ["REVIEWSENSEI_MODEL", "REVIEWSENSEI_PROVIDER"]
        mapped = re.findall(
            r"^          ([A-Z][A-Z0-9_]*): \$\{\{ vars\.([A-Z][A-Z0-9_]*) \}\}$",
            workflow,
            re.M,
        )
        self.assertEqual(sorted(mapped), [(name, name) for name in sorted(managed)])
        # A repository variable is always addressable only through its
        # prefixed name, so the map above is the workflow's complete `vars`
        # surface; the unprefixed historical names must not be read at all.
        for name in sorted(retired - set(managed)):
            self.assertIsNone(
                re.search(rf"(?<![A-Z0-9_]){name}(?![A-Z0-9_])", workflow),
                f"{name} must not appear in the workflow",
            )

    def test_retired_variables_are_reported_and_change_no_behavior(self):
        # The workflow's own retired-variable report is executed here, not just
        # matched as text: a stored value that no longer maps to configuration
        # must produce one warning naming its replacement, and must never reach
        # a later step.
        workflow = _reusable_workflow()
        step_start = workflow.index("- name: Report retired repository variables")
        step_end = workflow.find("\n      - name:", step_start + 1)
        self.assertNotEqual(step_end, -1)
        report = workflow[step_start:step_end]
        self.assertNotIn("id:", report)
        self.assertNotIn("GITHUB_ENV", report)
        self.assertNotIn("GITHUB_OUTPUT", report)
        block = report.split("python\" - <<'PY'\n", 1)[1]
        block = textwrap.dedent(re.split(r"\n\s*PY\s*\n?$", block, maxsplit=1)[0])

        source_root = str(Path(__file__).resolve().parents[1] / "src")
        environment = {
            **os.environ,
            "PYTHONPATH": source_root + os.pathsep + os.environ.get("PYTHONPATH", ""),
            "REVIEWSENSEI_REVIEW_MODE": "legacy",
            "REVIEWSENSEI_AUTO_APPROVE": "true",
        }
        reported = subprocess.run(
            [sys.executable, "-c", block],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(reported.returncode, 0, reported.stderr)
        self.assertIn(
            "::warning::REVIEWSENSEI_REVIEW_MODE is retired and has no effect; "
            "one evidence-focused pipeline is the only engine; set github.reviews",
            reported.stderr,
        )
        self.assertIn(
            "::warning::REVIEWSENSEI_AUTO_APPROVE is retired and has no effect; "
            "set github.reviews in .reviewsensei.yml",
            reported.stderr,
        )
        self.assertNotIn("::error::", reported.stderr)
        self.assertEqual(reported.stdout, "")

        quiet = subprocess.run(
            [sys.executable, "-c", block],
            env={
                **environment,
                "REVIEWSENSEI_REVIEW_MODE": "",
                "REVIEWSENSEI_AUTO_APPROVE": " ",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(quiet.returncode, 0, quiet.stderr)
        self.assertNotIn("::warning::", quiet.stderr)
        self.assertNotIn("::error::", quiet.stderr)

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
