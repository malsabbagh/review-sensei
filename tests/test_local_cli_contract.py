"""The documented local review contract: formats, exits, and local sessions.

Every case here runs a fixture review with no GitHub Actions identity, OIDC
token endpoint, broker, or hosted pull-request metadata, so the tests also
prove a local invocation needs no host prerequisites.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path, PurePosixPath, PureWindowsPath
from unittest.mock import patch

from review_sensei.cli import _parser, main, resolve_review_inference
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewResult
from review_sensei.schemas import validate_public_document
from review_sensei.session import default_local_session_root

try:
    from isolated_working_directory import IsolatedWorkingDirectoryMixin
except ModuleNotFoundError:
    from tests.isolated_working_directory import IsolatedWorkingDirectoryMixin

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "standalone-smoke"
BLOCKING_DIFF = FIXTURES / "blocking-review.patch"
BLOCKING_RESPONSE = FIXTURES / "blocking-review.json"

HOST_ENVIRONMENT_PREFIXES = (
    "ACTIONS_",
    "GH_",
    "GITHUB_",
    "OLLAMA_",
    "OPENAI_",
    "OPENROUTER_",
    "REVIEWSENSEI_",
    "RUNNER_",
)

CLEAN_RESPONSE = {
    "summary": "The pagination slice bounds stay inside the requested page.",
    "comments": [],
}


def clean_host_environment(home: Path) -> dict[str, str]:
    """Return an environment with no host identity and no provider secrets."""

    environment = {"HOME": str(home), "PATH": os.environ.get("PATH", "")}
    for name in ("LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    leaked = sorted(
        name
        for name in environment
        if name == "CI" or name.startswith(HOST_ENVIRONMENT_PREFIXES)
    )
    if leaked:
        raise AssertionError(f"clean environment leaked host variables: {leaked}")
    return environment


class LocalReviewHarness:
    """Shared fixture review invocation for the local contract tests."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)

    def response_path(self, payload: dict[str, object], name: str) -> Path:
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def blocking_response(self) -> dict[str, object]:
        return json.loads(BLOCKING_RESPONSE.read_text(encoding="utf-8"))

    def run_review(
        self,
        *arguments: object,
        environment: dict[str, str] | None = None,
    ) -> tuple[int, str, str]:
        argv = [str(argument) for argument in arguments]
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.dict(os.environ, environment or {}, clear=environment is not None),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            status = main(argv)
        return status, stdout.getvalue(), stderr.getvalue()

    def base_arguments(self, response: dict[str, object]) -> list[object]:
        return [
            "--diff",
            BLOCKING_DIFF,
            "--provider",
            "fixture",
            "--fixture-response",
            self.response_path(response, "response.json"),
            "--no-learning-proposals",
        ]


class LocalReviewContractTests(
    IsolatedWorkingDirectoryMixin, LocalReviewHarness, unittest.TestCase
):
    def test_default_output_is_readable_terminal_text(self):
        status, stdout, stderr = self.run_review(
            *self.base_arguments(self.blocking_response())
        )

        self.assertEqual(status, 1)
        self.assertTrue(stdout.startswith("ReviewSensei review: complete"))
        self.assertIn("Summary:", stdout)
        self.assertIn("src/pagination.py:3", stdout)
        self.assertIn("[required fix]", stdout)
        with self.assertRaises(ValueError):
            json.loads(stdout)
        self.assertIn("review-sensei: reason=required-fixes-remain", stderr)

    def test_markdown_output_is_explicit(self):
        status, stdout, _ = self.run_review(
            *self.base_arguments(self.blocking_response()),
            "--format",
            "markdown",
        )

        self.assertEqual(status, 1)
        self.assertIn("### Required fixes", stdout)
        self.assertIn("**src/pagination.py:3**", stdout)
        self.assertNotIn("ReviewSensei review: complete", stdout)

    def test_json_output_is_the_validated_versioned_document(self):
        status, stdout, _ = self.run_review(
            *self.base_arguments(self.blocking_response()),
            "--format",
            "json",
        )

        self.assertEqual(status, 1)
        document = json.loads(stdout)
        validate_public_document(document, "review-result")
        result = ReviewResult.from_dict(document)
        self.assertEqual(result.review_status, "complete")
        self.assertEqual(result.provider, "fixture")
        self.assertEqual(len(result.comments), 1)

    def test_machine_output_is_not_polluted_by_progress_text(self):
        status, stdout, stderr = self.run_review(
            *self.base_arguments(self.blocking_response()),
            "--format",
            "json",
        )

        self.assertEqual(status, 1)
        self.assertNotIn("review-sensei:", stdout)
        json.loads(stdout)
        self.assertIn("review-sensei:", stderr)

    def test_clean_review_exits_zero_without_a_reason(self):
        status, stdout, stderr = self.run_review(*self.base_arguments(CLEAN_RESPONSE))

        self.assertEqual(status, 0)
        self.assertTrue(stdout)
        self.assertNotIn("reason=", stderr)

    def test_operational_exit_semantics_select_the_legacy_contract(self):
        blocking = self.blocking_response()
        status, document, _ = self.run_review(*self.base_arguments(blocking))
        self.assertEqual(status, 1)

        operational_status, operational_stdout, _ = self.run_review(
            *self.base_arguments(blocking),
            "--exit-semantics",
            "operational",
        )
        self.assertEqual(operational_status, 0)
        self.assertEqual(operational_stdout, document)

    def test_incomplete_review_exits_two_with_its_own_reason(self):
        stages_dir = self.root / "stages"
        stages_dir.mkdir()
        (stages_dir / "01-summary.json").write_text(
            json.dumps(
                {
                    "name": "Summary only",
                    "outputs": ["summary"],
                    "prompt_template": "{diff}",
                }
            ),
            encoding="utf-8",
        )

        status, stdout, stderr = self.run_review(
            *self.base_arguments(CLEAN_RESPONSE),
            "--stages-dir",
            stages_dir,
            "--format",
            "json",
        )

        self.assertEqual(status, 2)
        # A review that could not complete still hands its caller the bounded
        # document it did produce; only the exit and the reason mark it.
        validate_public_document(json.loads(stdout), "review-result")
        self.assertIn("reason=partial_coverage", stderr)

    def test_invalid_input_exits_two_with_its_own_reason(self):
        status, stdout, stderr = self.run_review(
            "--diff",
            self.root / "missing.patch",
            "--provider",
            "fixture",
            "--fixture-response",
            self.response_path(CLEAN_RESPONSE, "response.json"),
        )

        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn("reason=invalid-input", stderr)

    def test_local_review_runs_on_a_clean_host(self):
        environment = clean_host_environment(self.root / "home")

        status, stdout, _ = self.run_review(
            *self.base_arguments(self.blocking_response()),
            environment=environment,
        )

        self.assertEqual(status, 1)
        self.assertTrue(stdout)

    def test_explicit_local_provider_and_model_overrides_are_honored(self):
        environment = clean_host_environment(self.root / "home")
        with patch.dict(os.environ, environment, clear=True):
            argv = [
                "--diff",
                str(BLOCKING_DIFF),
                "--provider",
                "local-ollama",
                "--model",
                "qwen3.5:8b",
            ]
            settings, api_key = resolve_review_inference(
                _parser().parse_args(argv), argv
            )

        self.assertEqual(settings.base_url, "http://127.0.0.1:11434/api")
        self.assertEqual(settings.model, "qwen3.5:8b")
        self.assertIsNone(api_key)

        with patch.dict(os.environ, environment, clear=True):
            status, _, stderr = self.run_review(
                "--diff",
                BLOCKING_DIFF,
                "--provider",
                "local-ollama",
                "--model",
                "not-a-local-model:cloud",
            )

        self.assertEqual(status, 2)
        self.assertIn("reason=invalid-input", stderr)


class LocalSessionTests(
    IsolatedWorkingDirectoryMixin, LocalReviewHarness, unittest.TestCase
):
    def test_local_session_records_state_under_the_platform_default(self):
        state_root = self.root / "state"
        environment = clean_host_environment(self.root / "home")
        blocking = self.blocking_response()

        with patch(
            "review_sensei.session.default_local_session_root",
            return_value=state_root,
        ):
            first_status, first_stdout, _ = self.run_review(
                *self.base_arguments(blocking),
                "--local-session",
                environment=environment,
            )
            stored = sorted(path.name for path in state_root.rglob("*.json"))

            second_status, second_stdout, second_stderr = self.run_review(
                *self.base_arguments(blocking),
                "--local-session",
                environment=environment,
            )

        self.assertEqual(first_status, 1)
        self.assertTrue(first_stdout)
        self.assertTrue(stored, "the local session wrote no durable state")
        # The same unchanged change is already reviewed, so the repeated local
        # run reports the persisted round instead of paying for another one.
        self.assertEqual(second_status, 2)
        self.assertEqual(second_stdout, "")
        self.assertIn("already_published", second_stderr)

    def test_local_session_rejects_hosted_ledger_combination(self):
        status, stdout, stderr = self.run_review(
            *self.base_arguments(CLEAN_RESPONSE),
            "--local-session",
            "--github-session-ledger",
        )

        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn("reason=invalid-input", stderr)


class DefaultLocalSessionRootTests(unittest.TestCase):
    """Each documented platform path is covered without a session run.

    The simulated platform must pin the path class too: ``os.name`` selects
    the concrete ``pathlib`` flavour, so patching it without pinning a pure
    class builds a foreign path type on the real host and fails on Windows
    runners.  The pure classes are host-neutral and compare by value.
    """

    def test_macos_uses_application_support(self):
        with (
            patch("sys.platform", "darwin"),
            patch("review_sensei.session.Path", PurePosixPath),
        ):
            root = default_local_session_root({"HOME": "/Users/tester"})

        self.assertEqual(
            root,
            PurePosixPath("/Users/tester/Library/Application Support/review-sensei"),
        )

    def test_macos_falls_back_to_the_operating_system_home(self):
        with (
            patch("sys.platform", "darwin"),
            patch("review_sensei.session.Path", PurePosixPath),
            patch(
                "review_sensei.session._home_directory", return_value="/Users/tester"
            ),
        ):
            root = default_local_session_root({})

        self.assertEqual(
            root,
            PurePosixPath("/Users/tester/Library/Application Support/review-sensei"),
        )

    def test_windows_uses_local_app_data(self):
        with (
            patch("sys.platform", "win32"),
            patch("os.name", "nt"),
            patch("review_sensei.session.Path", PureWindowsPath),
        ):
            root = default_local_session_root(
                {"LOCALAPPDATA": "C:/Users/tester/AppData/Local"}
            )

        self.assertEqual(
            root, PureWindowsPath("C:/Users/tester/AppData/Local/review-sensei")
        )

    def test_windows_falls_back_to_roaming_app_data(self):
        with (
            patch("sys.platform", "win32"),
            patch("os.name", "nt"),
            patch("review_sensei.session.Path", PureWindowsPath),
        ):
            root = default_local_session_root(
                {"APPDATA": "C:/Users/tester/AppData/Roaming"}
            )

        self.assertEqual(
            root, PureWindowsPath("C:/Users/tester/AppData/Roaming/review-sensei")
        )

    def test_linux_prefers_xdg_state_home(self):
        with (
            patch("sys.platform", "linux"),
            patch("os.name", "posix"),
            patch("review_sensei.session.Path", PurePosixPath),
        ):
            root = default_local_session_root(
                {"XDG_STATE_HOME": "/home/tester/.state", "HOME": "/home/tester"}
            )

        self.assertEqual(root, PurePosixPath("/home/tester/.state/review-sensei"))

    def test_linux_falls_back_to_the_home_state_directory(self):
        with (
            patch("sys.platform", "linux"),
            patch("os.name", "posix"),
            patch("review_sensei.session.Path", PurePosixPath),
        ):
            root = default_local_session_root({"HOME": "/home/tester"})

        self.assertEqual(root, PurePosixPath("/home/tester/.local/state/review-sensei"))

    def test_missing_state_directory_names_the_explicit_alternative(self):
        with (
            patch("sys.platform", "linux"),
            patch("os.name", "posix"),
            patch("review_sensei.session.Path", PurePosixPath),
            patch("review_sensei.session._home_directory", return_value=""),
        ):
            with self.assertRaises(ReviewInputError) as raised:
                default_local_session_root({})

        self.assertIn("--session-ledger", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
