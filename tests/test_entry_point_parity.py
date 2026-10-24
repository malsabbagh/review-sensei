"""One trusted input through every entry point: library, CLI, native, host.

The acceptance matrix requires that the same trusted inputs, configuration, and
fake provider responses yield equivalent findings and coverage across the
library, the CLI, the native launcher, and the hosted entry points, and that
host enforcement differs only where the ADRs define the difference.  The
fixture pair used here is the same one the native lane consumes: the standalone
smoke compares the built executable with ``python -m review_sensei`` on
``blocking-review.patch`` and ``blocking-review.json`` for byte-equal output,
and this module drives that smoke's own local-review case list so the case
contract cannot drift.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from review_sensei_github.approval import (
    approval_eligibility_from_result,
    has_blocking_findings,
)
from review_sensei_github.checks import check_outcome_for_result

from review_sensei.cli import main
from review_sensei.coverage import coverage_approval_state
from review_sensei.models import ReviewRequest, ReviewResult
from review_sensei.providers.fixture import FixtureProvider
from review_sensei.schemas import validate_public_document
from review_sensei.service import ReviewService

try:
    from isolated_working_directory import IsolatedWorkingDirectoryMixin
except ModuleNotFoundError:
    from tests.isolated_working_directory import IsolatedWorkingDirectoryMixin

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "standalone-smoke"
PATCH = FIXTURES / "blocking-review.patch"
BLOCKING_RESPONSE = FIXTURES / "blocking-review.json"
HEAD_SHA = "b" * 40
REPOSITORY = "owner/repository"
PULL_REQUEST = 7
NATIVE_CASES = [
    "local-review-text",
    "local-review-markdown",
    "local-review-json",
    "local-review-operational",
    "local-provider-model-override",
    "local-review-output-file",
]
CLEAN_RESPONSE = {
    "summary": "The pagination slice bounds stay inside the requested page.",
    "comments": [],
    "learning_proposals": [],
}


def _write_native_shim() -> Path:
    """Write a launcher the smoke can execute on this platform."""

    if os.name == "nt":
        shim = Path.cwd() / "standalone-shim.cmd"
        shim.write_text(
            f'@echo off\r\n"{sys.executable}" -m review_sensei %*\r\n',
            encoding="utf-8",
        )
        return shim
    shim = Path.cwd() / "standalone-shim"
    shim.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" -m review_sensei "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    return shim


def projection(result: ReviewResult) -> dict[str, object]:
    """The parity surface: findings, coverage, and the merge disposition."""

    coverage = result.coverage
    return {
        "review_status": result.review_status,
        "evidence_policy": result.evidence_policy,
        "findings": sorted(
            (
                comment.path,
                comment.line,
                comment.side,
                comment.blocking,
                comment.severity,
                comment.category,
                comment.fix_effort,
                comment.defect_kind,
                comment.symbol,
            )
            for comment in result.comments
        ),
        "coverage": None
        if coverage is None
        else (
            coverage.enumeration_complete,
            tuple(sorted((entry.path, entry.outcome) for entry in coverage.files)),
            coverage_approval_state(coverage),
        ),
        "blocks_approval": has_blocking_findings(result),
    }


def load_smoke_module():
    spec = importlib.util.spec_from_file_location(
        "standalone_smoke", ROOT / "scripts" / "standalone_smoke.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EntryPointParityTests(IsolatedWorkingDirectoryMixin, unittest.TestCase):
    def test_generated_diff_fixtures_keep_bytes_with_windows_newline_defaults(self):
        from tests import test_openai_configuration as openai_fixture
        from tests.test_baseline_recovery import BaselineRecoveryTests
        from tests.test_default_publication_cli_journey import (
            DefaultPublicationCliJourneyTests,
        )
        from tests.test_review_transaction import ReviewTransactionTests

        original_write_text = Path.write_text
        original_open = Path.open
        written = []

        def windows_open(
            path, mode="r", buffering=-1, encoding=None, errors=None, newline=None
        ):
            if (
                path.suffix in {".patch", ".diff"}
                and "b" not in mode
                and any(flag in mode for flag in ("w", "a"))
                and newline is None
            ):
                newline = "\r\n"
            return original_open(path, mode, buffering, encoding, errors, newline)

        def windows_write_text(path, data, *args, **kwargs):
            if path.suffix in {".patch", ".diff"}:
                count = original_write_text(path, data, *args, **kwargs)
                written.append((path.read_bytes(), data.encode("utf-8")))
                return count
            return original_write_text(path, data, *args, **kwargs)

        # Git headers keep LF framing while source records retain their CRs
        # and every non-LF separator, even under Windows text-write defaults.
        source = "\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"
        diff = openai_fixture.PATCH.replace("+x = 1\n", f"+x = {source}\r\n")
        cases = unittest.TestSuite(
            [
                openai_fixture.OpenAIConfigurationTests(
                    "test_yaml_only_permission_reaches_real_adapter"
                ),
                DefaultPublicationCliJourneyTests(
                    "test_trusted_complete_config_publishes_all_77_through_actual_cli_and_reader"
                ),
                ReviewTransactionTests(
                    "test_cli_checkpoint_uses_one_provider_call_and_one_reservation"
                ),
                BaselineRecoveryTests(
                    "test_base_change_runs_full_and_refreshes_only_after_checkpoint"
                ),
            ]
        )
        result = unittest.TestResult()
        with (
            mock.patch.object(Path, "write_text", windows_write_text),
            mock.patch.object(Path, "open", windows_open),
            mock.patch.object(openai_fixture, "PATCH", diff),
        ):
            cases.run(result)
        self.assertTrue(result.wasSuccessful(), result.failures + result.errors)
        self.assertEqual(result.testsRun, 4)
        self.assertGreaterEqual(len(written), 4)
        for actual, expected in written:
            self.assertEqual(actual, expected)
        self.assertIn(diff.encode("utf-8"), [actual for actual, _ in written])

    def test_smoke_fixture_keeps_git_framing_in_windows_style_checkout(self):
        relative = "tests/fixtures/standalone-smoke/blocking-review.patch"
        expected = PATCH.read_bytes()
        self.assertNotIn(b"\r", expected)
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            fixture = checkout / relative
            fixture.parent.mkdir(parents=True)
            fixture.write_bytes(expected)
            (checkout / ".gitattributes").write_bytes(
                (ROOT / ".gitattributes").read_bytes()
            )
            environment = {
                name: os.environ[name]
                for name in (
                    "PATH",
                    "SYSTEMROOT",
                    "WINDIR",
                    "COMSPEC",
                    "PATHEXT",
                    "TMP",
                    "TEMP",
                )
                if name in os.environ
            }
            environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)

            def git(*arguments):
                return subprocess.run(
                    ["git", "-C", directory, *arguments],
                    env=environment,
                    check=True,
                    capture_output=True,
                    timeout=10,
                )

            git("init", "--quiet")
            git("-c", "core.autocrlf=false", "add", "--", ".gitattributes", relative)
            git("config", "core.autocrlf", "true")
            git("config", "core.eol", "crlf")
            fixture.unlink()
            git("checkout-index", "--force", "--", relative)
            self.assertEqual(fixture.read_bytes(), expected)
            attributes = git("check-attr", "--cached", "text", "eol", "--", relative)
            self.assertEqual(
                attributes.stdout.decode().splitlines(),
                [f"{relative}: text: set", f"{relative}: eol: lf"],
            )
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                mock.patch.dict(os.environ, environment, clear=True),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                status = main(
                    [
                        "--diff",
                        str(fixture),
                        "--provider",
                        "fixture",
                        "--fixture-response",
                        str(BLOCKING_RESPONSE),
                        "--no-learning-proposals",
                        "--format",
                        "json",
                    ]
                )
            self.assertEqual(status, 1, stderr.getvalue())
            reviewed = ReviewResult.from_dict(json.loads(stdout.getvalue()))
            self.assertEqual(reviewed.review_status, "complete")
            self.assertTrue(has_blocking_findings(reviewed))

    def _response_path(self, name: str, payload: dict[str, object]) -> Path:
        path = Path.cwd() / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _library_result(self, response_path: Path) -> ReviewResult:
        provider = FixtureProvider(response_path, model="fixture-v1")
        return ReviewService(provider).review(
            ReviewRequest(
                diff=PATCH.read_text(encoding="utf-8"),
                repository=REPOSITORY,
                pull_request_number=PULL_REQUEST,
            )
        )

    def _cli_result(self, response_path: Path) -> tuple[int, ReviewResult]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = main(
                [
                    "--diff",
                    str(PATCH),
                    "--provider",
                    "fixture",
                    "--fixture-response",
                    str(response_path),
                    "--format",
                    "json",
                    "--no-learning-proposals",
                ]
            )
        document = json.loads(stdout.getvalue())
        validate_public_document(document, "review-result")
        return status, ReviewResult.from_dict(document)

    def test_library_and_cli_agree_on_findings_and_coverage(self):
        blocking = self._response_path(
            "blocking.json", json.loads(BLOCKING_RESPONSE.read_text(encoding="utf-8"))
        )
        clean = self._response_path("clean.json", CLEAN_RESPONSE)

        for label, response_path in (("blocking", blocking), ("clean", clean)):
            with self.subTest(response=label):
                library = self._library_result(response_path)
                status, cli = self._cli_result(response_path)
                self.assertEqual(projection(cli), projection(library))
                self.assertEqual(
                    status,
                    1 if projection(library)["blocks_approval"] else 0,
                )

    def test_cli_exit_semantics_do_not_change_the_review_document(self):
        blocking = self._response_path(
            "blocking.json", json.loads(BLOCKING_RESPONSE.read_text(encoding="utf-8"))
        )
        status, reviewed = self._cli_result(blocking)
        self.assertEqual(status, 1)

        stdout = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            operational = main(
                [
                    "--diff",
                    str(PATCH),
                    "--provider",
                    "fixture",
                    "--fixture-response",
                    str(blocking),
                    "--format",
                    "json",
                    "--no-learning-proposals",
                    "--exit-semantics",
                    "operational",
                ]
            )
        self.assertEqual(operational, 0)
        self.assertEqual(
            projection(ReviewResult.from_dict(json.loads(stdout.getvalue()))),
            projection(reviewed),
        )

    def test_hosted_enforcement_derives_from_the_same_result(self):
        blocking = self._response_path(
            "blocking.json", json.loads(BLOCKING_RESPONSE.read_text(encoding="utf-8"))
        )
        clean = self._response_path("clean.json", CLEAN_RESPONSE)

        reviewed = self._library_result(blocking)
        outcome = check_outcome_for_result(reviewed, policy="auto-approve")
        eligibility = approval_eligibility_from_result(
            reviewed, head_sha=HEAD_SHA, enabled=True, app_authored=False
        )
        decision = eligibility.evaluate(
            app_authored=False, has_open_review_threads=False
        )
        blocked = check_outcome_for_result(reviewed, policy="blocking")
        self.assertEqual(outcome.conclusion, "failure")
        self.assertEqual(outcome.title, "Required fixes remain")
        self.assertEqual(blocked.conclusion, "failure")
        self.assertFalse(decision.approved)
        self.assertIn("blocking-findings-open", decision.blockers)

        clean_result = self._library_result(clean)
        clean_outcome = check_outcome_for_result(clean_result, policy="auto-approve")
        clean_eligibility = approval_eligibility_from_result(
            clean_result, head_sha=HEAD_SHA, enabled=True, app_authored=False
        )
        clean_decision = clean_eligibility.evaluate(
            app_authored=False, has_open_review_threads=False
        )
        self.assertEqual(clean_outcome.conclusion, "success")
        self.assertTrue(clean_decision.approved)
        self.assertEqual(clean_decision.blockers, ())

    def test_host_facts_change_only_the_enforcement_decision(self):
        clean = self._response_path("clean.json", CLEAN_RESPONSE)
        reviewed = self._library_result(clean)
        reviewed_projection = projection(reviewed)
        eligibility = approval_eligibility_from_result(
            reviewed, head_sha=HEAD_SHA, enabled=True, app_authored=False
        )

        open_threads = eligibility.evaluate(
            app_authored=False, has_open_review_threads=True
        )
        unknown_threads = eligibility.evaluate(
            app_authored=False, has_open_review_threads=None
        )
        disabled = approval_eligibility_from_result(
            reviewed, head_sha=HEAD_SHA, enabled=False, app_authored=False
        ).evaluate(app_authored=False, has_open_review_threads=False)

        for decision in (open_threads, unknown_threads, disabled):
            with self.subTest(blockers=decision.blockers):
                self.assertFalse(decision.approved)
        self.assertIn("review-threads-open", open_threads.blockers)
        self.assertIn("review-threads-incomplete", unknown_threads.blockers)
        self.assertIn("auto-approval-disabled", disabled.blockers)
        # Only the defined host facts moved; the reviewed result did not.
        self.assertEqual(projection(reviewed), reviewed_projection)

    def test_native_lane_runs_the_documented_cli_contract_on_this_fixture(self):
        smoke = load_smoke_module()
        self.assertEqual(smoke.smoke_fixture_root(), FIXTURES)
        shim = _write_native_shim()
        repository = Path(tempfile.mkdtemp(dir=Path.cwd()))
        # The smoke runs its cases from the repository directory, so a relative
        # import path would resolve there instead of at this checkout.
        with mock.patch.dict(os.environ, {"PYTHONPATH": str(ROOT / "src")}):
            cases = smoke._local_review_cases(
                executable=shim,
                python_executable=sys.executable,
                repository=repository,
            )
        self.assertEqual([case["case"] for case in cases], NATIVE_CASES)
        self.assertEqual(cases[0]["returncode"], 1)
        self.assertEqual(cases[3]["returncode"], 0)
        self.assertEqual(cases[4]["returncode"], 2)


if __name__ == "__main__":
    unittest.main()
