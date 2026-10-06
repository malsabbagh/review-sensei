import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError

from review_sensei.broker_diagnostics import (
    MAX_DIAGNOSTIC_BYTES,
    BrokerDiagnostic,
    exception_broker_diagnostic,
    parse_broker_diagnostic,
)
from review_sensei.cli import _report_broker_failure, main
from review_sensei.errors import ReviewInputError
from review_sensei.hosting.github.broker_client import BrokerClient
from review_sensei.hosting.github.errors import (
    GitHubBrokerClientError,
    GitHubHTTPTransientError,
)
from review_sensei.outcomes import RunOutcome, render_actions_summary
from review_sensei.schemas import validate_public_document

RAY = "0123456789abcdef-YYZ"
SECRET = "ghs_synthetic_secret_do_not_log"


def payload(code="broker_workflow_rejected", **extra):
    diagnostic = BrokerDiagnostic(code)
    return {
        "error": SECRET,
        "diagnostic": {
            "version": 1,
            "code": code,
            "stage": diagnostic.stage,
            "action": diagnostic.action,
            **extra,
        },
    }


def http_error(body, status=403, ray=RAY):
    return HTTPError(
        "https://broker.invalid/token",
        status,
        SECRET,
        {"cf-ray": ray},
        io.BytesIO(body),
    )


class BrokerDiagnosticTests(unittest.TestCase):
    def test_known_reason_uses_only_local_hint_and_validated_facts(self):
        result = parse_broker_diagnostic(http_error(json.dumps(payload()).encode()))
        self.assertEqual(result.code, "broker_workflow_rejected")
        self.assertEqual(result.stage, "workflow_identity")
        self.assertEqual(result.correlation_id, RAY)
        self.assertNotIn(SECRET, result.message())
        self.assertIn("Confirm the supported release", result.hint)

    def test_error_body_inspection_is_bounded_and_best_effort(self):
        for raw in (
            b"",
            b"not-json",
            b"\xff",
            b"[]",
            b"null",
            b"{" + b" " * MAX_DIAGNOSTIC_BYTES,
            b'{"diagnostic":{"code":"broker_workflow_rejected","code":"broker_rate_limited"}}',
            b'{"x":' + b"[" * 1500,
        ):
            with self.subTest(raw=raw[:20]):
                result = parse_broker_diagnostic(http_error(raw))
                self.assertEqual(result.code, "broker_unknown")
                self.assertEqual(result.correlation_id, RAY)
        for failure in (OSError(SECRET), RuntimeError(SECRET)):
            error = http_error(b"")
            with patch.object(error, "read", side_effect=failure) as read:
                self.assertEqual(parse_broker_diagnostic(error).code, "broker_unknown")
                read.assert_called_once_with(MAX_DIAGNOSTIC_BYTES + 1)

    def test_malformed_or_unknown_metadata_falls_back_without_echoing(self):
        invalid = [
            {"version": True},
            {"version": 2},
            {"code": SECRET},
            {"code": []},
            {"stage": SECRET},
            {"action": SECRET},
            {"hint": SECRET},
            {"upstream_status": True},
            {"upstream_status": "500"},
            {"upstream_status": 500},
            {"upstream_status": None},
            {"correlation_id": SECRET},
            {"correlation_id": None},
        ]
        for extra in invalid:
            value = payload()
            value["diagnostic"].update(extra)
            with self.subTest(extra=extra):
                result = parse_broker_diagnostic(
                    http_error(json.dumps(value).encode(), ray=SECRET)
                )
                self.assertEqual(result.code, "broker_unknown")
                self.assertIsNone(result.correlation_id)
                self.assertNotIn(SECRET, result.message())
        result = parse_broker_diagnostic(
            http_error(b'{"error":"capability_not_issued"}')
        )
        self.assertEqual(result.code, "broker_unknown")

    def test_outer_status_keeps_existing_exception_class_independent_of_upstream(self):
        for status in (403, 429, 503):
            for upstream in (401, 500):
                error = http_error(
                    json.dumps(
                        payload(
                            "github_capability_issue_failed", upstream_status=upstream
                        )
                    ).encode(),
                    status,
                )

                def opener(*args, **kwargs):
                    raise error

                client = BrokerClient(opener=opener)
                expected = (
                    GitHubBrokerClientError
                    if status == 403
                    else GitHubHTTPTransientError
                )
                with (
                    self.subTest(status=status, upstream=upstream),
                    self.assertRaises(expected) as raised,
                ):
                    client.exchange("synthetic-oidc")
                diagnostic = exception_broker_diagnostic(raised.exception)
                self.assertEqual(diagnostic.upstream_status, upstream)
                self.assertNotIn(SECRET, str(raised.exception))

    def test_invalid_upstream_status_and_correlation_cannot_be_constructed(self):
        for status in (True, "500", 99, 600):
            with self.assertRaises(ValueError):
                BrokerDiagnostic(
                    "github_capability_issue_failed", upstream_status=status
                )
        with self.assertRaises(ValueError):
            BrokerDiagnostic("oidc_token_invalid", upstream_status=500)
        with self.assertRaises(ValueError):
            BrokerDiagnostic("broker_unknown", correlation_id=SECRET)

    def test_outcome_has_operational_summary_and_validated_metadata(self):
        diagnostic = BrokerDiagnostic("github_capability_issue_failed", RAY, 422)
        outcome = RunOutcome(
            status="action_required",
            diagnostic="broker_rejected",
            stage_summary=diagnostic.metadata(),
        )
        validate_public_document(outcome.to_dict(), "run-outcome")
        summary = render_actions_summary(outcome)
        self.assertIn("Broker request could not complete", summary)
        self.assertIn("broker.upstream_status=422", summary)
        self.assertNotIn("provider_calls", summary)
        self.assertNotIn("Review completed", summary)
        # A normal review stage with the same prefix must not select the broker title.
        normal = RunOutcome(status="reviewed", stage_summary=diagnostic.metadata())
        self.assertNotIn(
            "Broker request could not complete", render_actions_summary(normal)
        )
        spoof = RunOutcome(
            status="action_required",
            diagnostic="broker_rejected",
            stage_summary={"broker.code": "broker_unknown", "broker.action": SECRET},
        )
        self.assertNotIn(
            "Broker request could not complete", render_actions_summary(spoof)
        )

    def test_cli_surfaces_wrapped_rejection_and_keeps_exit_status(self):
        diagnostic = BrokerDiagnostic("broker_session_grant_invalid", RAY)
        rejection = GitHubBrokerClientError(SECRET, broker_diagnostic=diagnostic)
        wrapper = ReviewInputError("session grant verification failed")
        wrapper.__cause__ = rejection
        with tempfile.TemporaryDirectory() as tmp:
            summary = Path(tmp) / "summary.md"
            output = Path(tmp) / "outputs"
            stderr = io.StringIO()
            with (
                patch.dict(
                    os.environ,
                    {"GITHUB_STEP_SUMMARY": str(summary), "GITHUB_OUTPUT": str(output)},
                    clear=True,
                ),
                patch("review_sensei.cli._run_github", side_effect=wrapper),
                redirect_stderr(stderr),
            ):
                status = main(
                    [
                        "github",
                        "reply",
                        "--repository",
                        "owner/repo",
                        "--pull-request",
                        "1",
                        "--source-comment-id",
                        "2",
                        "--source-updated-at",
                        "2026-10-05T00:00:00Z",
                    ]
                )
            self.assertEqual(status, 1)
            self.assertIn("broker_session_grant_invalid", stderr.getvalue())
            self.assertIn("reason=broker_rejected", stderr.getvalue())
            self.assertNotIn(
                SECRET, stderr.getvalue() + summary.read_text() + output.read_text()
            )
            self.assertIn("outcome_diagnostic=broker_rejected", output.read_text())

    def test_supplementary_output_failure_and_unrelated_transient(self):
        diagnostic = BrokerDiagnostic("broker_ledger_unavailable")
        stderr = io.StringIO()
        with (
            patch("review_sensei.cli.emit_host_outcome", side_effect=OSError(SECRET)),
            redirect_stderr(stderr),
        ):
            self.assertTrue(
                _report_broker_failure(
                    GitHubHTTPTransientError(SECRET, broker_diagnostic=diagnostic),
                    SimpleNamespace(),
                )
            )
        self.assertIn("reason=broker_temporarily_unavailable", stderr.getvalue())
        self.assertNotIn(SECRET, stderr.getvalue())
        self.assertFalse(
            _report_broker_failure(
                GitHubHTTPTransientError("ordinary API failure"), SimpleNamespace()
            )
        )
        cycle = ReviewInputError("wrapped")
        cycle.__cause__ = cycle
        self.assertIsNone(exception_broker_diagnostic(cycle))

    def test_review_cli_failure_keeps_both_exit_conventions_and_emits_outcome(self):
        diagnostic = BrokerDiagnostic("broker_workflow_rejected", RAY)
        error = GitHubBrokerClientError("rejected", broker_diagnostic=diagnostic)
        with tempfile.TemporaryDirectory() as tmp:
            outcome = Path(tmp) / "outcome.json"
            for options, expected in (
                ([], 2),
                (["--exit-semantics", "operational"], 1),
            ):
                stderr = io.StringIO()
                with (
                    patch.dict(os.environ, {}, clear=True),
                    patch(
                        "review_sensei.cli._validate_live_profile_gates",
                        side_effect=error,
                    ),
                    redirect_stderr(stderr),
                ):
                    status = main(
                        ["--diff", "unused.patch", "--outcome", str(outcome), *options]
                    )
                self.assertEqual(status, expected)
                self.assertNotIn("reason=invalid-input", stderr.getvalue())
                self.assertEqual(
                    json.loads(outcome.read_text())["stage_summary"],
                    diagnostic.metadata(),
                )
