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

from review_sensei_github.broker_client import BrokerClient
from review_sensei_github.cli import main as github_main
from review_sensei_github.errors import (
    GitHubBrokerClientError,
    GitHubHTTPTransientError,
)

from review_sensei.broker_diagnostics import (
    MAX_DIAGNOSTIC_BYTES,
    BrokerDiagnostic,
    exception_broker_diagnostic,
    parse_broker_diagnostic,
)
from review_sensei.cli import _report_broker_failure, main
from review_sensei.errors import ReviewInputError
from review_sensei.outcomes import RunOutcome, render_actions_summary
from review_sensei.schemas import validate_public_document
from tests.test_github_broker_client import current_authority, current_grant

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
    def test_temporary_lookup_reaches_exchange_and_grant_callers_without_replay(self):
        for operation in ("exchange", "session-grant"):
            for upstream, lookup in (
                (
                    403,
                    {"phase": "rest_tag", "failure": "http", "rate_limit_remaining": 0},
                ),
                (503, {"phase": "rest_ref", "failure": "http"}),
                (None, {"phase": "rest_ref", "failure": "timeout"}),
                (200, {"phase": "rest_tag", "failure": "timeout"}),
            ):
                with self.subTest(operation=operation, upstream=upstream):
                    body = payload("github_workflow_lookup_temporary")
                    if upstream is not None:
                        body["diagnostic"]["upstream_status"] = upstream
                    body["lookup"] = lookup
                    calls = []

                    def opener(request, timeout):
                        calls.append(request)
                        raise http_error(json.dumps(body).encode(), status=503)

                    client = BrokerClient(opener=opener, authority=current_authority())
                    with self.assertRaises(GitHubHTTPTransientError) as raised:
                        if operation == "exchange":
                            client.exchange("synthetic-oidc")
                        else:
                            client.verify_session_grant(
                                "sg1.1780000000000." + "a" * 43,
                                current_grant(current_authority()),
                            )
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(calls[0].method, "POST")
                    diagnostic = exception_broker_diagnostic(raised.exception)
                    self.assertEqual(
                        diagnostic.code, "github_workflow_lookup_temporary"
                    )
                    self.assertEqual(diagnostic.upstream_status, upstream)
                    self.assertEqual(
                        diagnostic.metadata()["broker.lookup_failure"],
                        lookup["failure"],
                    )
                    wrapper = ReviewInputError("session grant verification failed")
                    wrapper.__cause__ = raised.exception
                    self.assertEqual(exception_broker_diagnostic(wrapper), diagnostic)
                    self.assertNotIn(SECRET, diagnostic.message())

    def test_legacy_outer_403_is_not_promoted_by_upstream_rate_metadata(self):
        body = payload("github_workflow_tag_unavailable", upstream_status=429)
        body["lookup"] = {"phase": "rest_ref", "failure": "http", "retry_after": 1}
        calls = []

        def opener(request, timeout):
            calls.append(request)
            raise http_error(json.dumps(body).encode(), status=403)

        client = BrokerClient(opener=opener, authority=current_authority())
        with self.assertRaises(GitHubBrokerClientError) as raised:
            client.exchange("synthetic-oidc")
        self.assertEqual(len(calls), 1)
        self.assertEqual(type(raised.exception), GitHubBrokerClientError)
        self.assertEqual(
            exception_broker_diagnostic(raised.exception).upstream_status, 429
        )

    def test_temporary_workflow_lookup_survives_one_exchange_without_retry(self):
        body = {
            "error": SECRET,
            "diagnostic": {
                "version": 1,
                "code": "github_workflow_lookup_temporary",
                "stage": "workflow_identity",
                "action": "inspect_original_attempt",
                "upstream_status": 429,
            },
            "lookup": {"phase": "rest_ref", "failure": "http", "retry_after": 1},
        }
        calls = []

        def opener(request, timeout):
            calls.append(request)
            raise http_error(json.dumps(body).encode(), status=503)

        client = BrokerClient(opener=opener, authority=current_authority())
        with self.assertRaises(GitHubHTTPTransientError) as raised:
            client.exchange("synthetic-oidc")
        self.assertEqual(len(calls), 1)
        diagnostic = exception_broker_diagnostic(raised.exception)
        self.assertEqual(diagnostic.code, "github_workflow_lookup_temporary")
        self.assertEqual(diagnostic.upstream_status, 429)
        self.assertEqual(diagnostic.metadata()["broker.lookup_retry_after"], "1")
        self.assertIn("original attempt", diagnostic.hint)
        self.assertNotIn("Confirm the supported release", str(raised.exception))
        self.assertNotIn(SECRET, str(raised.exception))

    def test_lookup_envelope_is_closed_and_does_not_replace_legacy_reason(self):
        body = payload("github_workflow_tag_unavailable", upstream_status=403)
        body["lookup"] = {
            "phase": "rest_tag",
            "failure": "http",
            "git_failure": "timeout",
            "request_id": "ABCD:1234",
            "rate_limit_remaining": 0,
            "rate_limit_resource": "core",
        }
        result = parse_broker_diagnostic(http_error(json.dumps(body).encode()))
        self.assertEqual(result.code, "github_workflow_tag_unavailable")
        self.assertEqual(result.metadata()["broker.lookup_phase"], "rest_tag")
        self.assertEqual(result.metadata()["broker.lookup_rate_limit_remaining"], "0")
        for key, value in [
            ("authorization", SECRET),
            ("request_id", SECRET + "\\n"),
            ("rate_limit_remaining", True),
            ("failure", SECRET),
            ("phase", SECRET),
        ]:
            with self.subTest(key=key):
                bad = {**body, "lookup": {**body["lookup"], key: value}}
                diagnostic = parse_broker_diagnostic(
                    http_error(json.dumps(bad).encode())
                )
                self.assertEqual(diagnostic.code, "github_workflow_tag_unavailable")
                self.assertEqual(diagnostic.lookup, ())
                self.assertNotIn(SECRET, diagnostic.message())

    def test_control_refusals_keep_original_accounting_actionable_without_remote_text(
        self,
    ):
        for code in (
            "broker_control_invalid",
            "broker_control_binding_invalid",
            "broker_control_budget_exhausted",
            "broker_control_deadline_exhausted",
            "broker_control_clock_regressed",
        ):
            with self.subTest(code=code):
                result = parse_broker_diagnostic(
                    http_error(json.dumps(payload(code)).encode())
                )
                self.assertEqual(result.code, code)
                self.assertIn("do not renew this attempt", result.hint)
                self.assertNotIn(SECRET, result.message())

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

                client = BrokerClient(opener=opener, authority=current_authority())
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
                patch("review_sensei_github.cli.run_github", side_effect=wrapper),
                redirect_stderr(stderr),
            ):
                status = github_main(
                    [
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
