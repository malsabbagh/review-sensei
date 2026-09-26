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
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from review_sensei.cli import main
from review_sensei.coverage import coverage_approval_state
from review_sensei.hosting.github.approval import (
    approval_eligibility_from_result,
    has_blocking_findings,
)
from review_sensei.hosting.github.checks import check_outcome_for_result
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
        self.assertEqual(outcome.conclusion, "failure")
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
        shim = Path.cwd() / "standalone-shim"
        shim.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" -m review_sensei "$@"\n',
            encoding="utf-8",
        )
        shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
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
