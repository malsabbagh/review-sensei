"""Report incomplete check reconciliation through the public media command."""

import io
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from fake_github_http import json_response
from review_sensei_github import GitHubApplication, GitHubHttp, GitHubWriteOptions
from review_sensei_github.errors import GitHubBrokerClientError

from review_sensei.broker_diagnostics import BrokerDiagnostic
from review_sensei.disposition import command_exit_code
from review_sensei.models import ReviewResult


class MissingCheckBroker:
    def __init__(self, *, error=False):
        self.error = error
        self.capabilities = []

    def exchange(self, token, *, capability):
        self.capabilities.append(capability)
        if self.error:
            raise RuntimeError("transport exception detail")
        return None


class MediaPublisher:
    def __init__(self, assessment):
        self.assessment = assessment
        self.confirmations = 0

    def approve_reviewed_media(self, **kwargs):
        self.confirmations += 1
        return self.assessment


class MediaCommandReportingTests(unittest.TestCase):
    def test_public_cli_missing_check_context_fails_without_remote_writes(self):
        self.assert_check_failure()

    def test_public_cli_preserves_allowlisted_check_broker_diagnostic(self):
        self.assert_check_failure(
            GitHubBrokerClientError(
                "synthetic-untrusted-exception-detail",
                broker_diagnostic=BrokerDiagnostic(
                    "broker_control_budget_exhausted", "0123456789abcdef-YYZ"
                ),
            )
        )

    def assert_check_failure(self, error=None):
        from urllib.error import HTTPError

        from review_sensei_github.session_ledger import parse_session_comment
        from test_action_run import CommandEntryFixture
        from test_api_work_persistence import IDENTITY

        class CurrentMediaFailureFixture(CommandEntryFixture):
            def _broker_response(fixture, request):
                payload = json.loads(request.data)
                if payload.get("capability") != "check_publish":
                    return super()._broker_response(request)
                fixture._meter(request)
                fixture.broker_requests.append(payload)
                if error is not None:
                    diagnostic = error.broker_diagnostic
                    body = {
                        "error": "synthetic-untrusted-exception-detail",
                        "diagnostic": {
                            "version": 1,
                            "code": diagnostic.code,
                            "stage": diagnostic.stage,
                            "action": diagnostic.action,
                            "correlation_id": diagnostic.correlation_id,
                        },
                    }
                    raise HTTPError(
                        request.full_url,
                        403,
                        "Forbidden",
                        {"cf-ray": diagnostic.correlation_id},
                        io.BytesIO(json.dumps(body).encode()),
                    )
                return json_response(
                    {
                        "token": "",
                        "capability": "check_publish",
                        "control_receipt": {
                            **payload["control_budget"],
                            "dispatched": 1,
                        },
                    }
                )

        with tempfile.TemporaryDirectory() as directory:
            f = CurrentMediaFailureFixture(directory)
            status = f.invoke_command(
                "@reviewsensei I reviewed the media files and I approve"
            )
            self.assertNotEqual(status, 0, (f.error, f.exception))
            if error is None:
                self.assertIn("Broker token response was invalid", f.error)
            else:
                self.assertIn("broker_control_budget_exhausted", f.error)
                self.assertIn("0123456789abcdef-YYZ", f.error)
                self.assertIn("Broker token request was rejected", f.error)
                self.assertNotIn("synthetic-untrusted-exception-detail", f.error)
            self.assertEqual(
                [
                    item["capability"]
                    for item in f.broker_requests
                    if item.get("capability") == "check_publish"
                ],
                ["check_publish"],
            )
            self.assertEqual(f.business_writes, 0)
            self.assertEqual(f.closure_writes, 0)
            self.assertFalse(f.reviews)
            self.assertFalse(
                any(path.endswith("/check-runs") for _, _, path in f.trace)
            )
            record = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(record.api_run_accounting["action"]["phase"], "pending")
            self.assertEqual(len(f.accounted.observations), len(f.trace))

    def test_missing_media_dependencies_fail_before_evaluation_or_writes(self):
        for operation_entry in ("disabled", "enabled"):
            for missing in ("publisher", "result", "request-comment", "source-comment"):
                with self.subTest(operation_entry=operation_entry, missing=missing):
                    broker, http, publisher, store = Mock(), Mock(), Mock(), Mock()
                    app = GitHubApplication(
                        broker=broker,
                        http=http,
                        reviewer=object(),
                        learner=object(),
                        replier=object(),
                    )
                    result = app.apply_maintainer_command(
                        options=GitHubWriteOptions(github_writes=True),
                        oidc_token="synthetic-oidc",
                        repository="owner/repo",
                        repository_id=1,
                        pull_request=2,
                        head_sha="a" * 40,
                        body="@reviewsensei I reviewed the media files and I approve",
                        actor_login="maintainer",
                        actor_permission="admin",
                        association="OWNER",
                        app_slug="reviewsensei[bot]",
                        source_comment_id=None if missing == "source-comment" else 20,
                        request_comment_id=None if missing == "request-comment" else 10,
                        media_publisher=None if missing == "publisher" else publisher,
                        review_result=None
                        if missing == "result"
                        else ReviewResult(
                            summary="synthetic", comments=(), provider="fixture"
                        ),
                        operation_entry=operation_entry,
                        operation_store=store,
                    )
                    self.assertFalse(result.applied)
                    self.assertTrue(result.evaluation_failed)
                    self.assertEqual(command_exit_code(result), 1)
                    self.assertFalse(publisher.mock_calls)
                    self.assertFalse(store.mock_calls)
                    self.assertFalse(http.mock_calls)
                    self.assertFalse(broker.mock_calls)

    def test_missing_media_assessment_is_failure_before_check_publication(self):
        broker, http = Mock(), Mock()
        publisher = MediaPublisher(None)
        app = GitHubApplication(
            broker=broker,
            http=http,
            reviewer=object(),
            learner=object(),
            replier=object(),
        )
        result = app.apply_maintainer_command(
            options=GitHubWriteOptions(github_writes=True),
            oidc_token="synthetic-oidc",
            repository="owner/repo",
            repository_id=1,
            pull_request=2,
            head_sha="a" * 40,
            body="@reviewsensei I reviewed the media files and I approve",
            actor_login="maintainer",
            actor_permission="admin",
            association="OWNER",
            app_slug="reviewsensei[bot]",
            source_comment_id=20,
            request_comment_id=10,
            media_publisher=publisher,
            review_result=ReviewResult(
                summary="synthetic", comments=(), provider="fixture"
            ),
        )
        self.assertFalse(result.applied)
        self.assertTrue(result.evaluation_failed)
        self.assertEqual(command_exit_code(result), 1)
        self.assertFalse(http.mock_calls)
        self.assertFalse(broker.mock_calls)

    def test_completed_withheld_media_assessment_remains_successful(self):
        broker = Mock()
        broker.exchange.return_value = "synthetic-check-capability"
        publisher = MediaPublisher(
            SimpleNamespace(
                approval=SimpleNamespace(
                    approved=False, blockers=("blocking-findings-open",)
                ),
                pending_ids=(),
            )
        )
        calls = []

        def check_transport(request, timeout):
            calls.append((request.method, request.full_url, request.data))
            if request.method == "GET" and "/check-runs?" in request.full_url:
                return json_response({"check_runs": []})
            if request.method == "POST" and request.full_url.endswith("/check-runs"):
                self.assertEqual(json.loads(request.data)["conclusion"], "failure")
                return json_response({"id": 30}, status=201)
            raise AssertionError("withheld media decision must not post an approval")

        app = GitHubApplication(
            broker=broker,
            http=GitHubHttp(opener=check_transport),
            reviewer=object(),
            learner=object(),
            replier=object(),
        )
        result = app.apply_maintainer_command(
            options=GitHubWriteOptions(
                github_writes=True, reviews_policy="auto-approve"
            ),
            oidc_token="synthetic-oidc",
            repository="owner/repo",
            repository_id=1,
            pull_request=2,
            head_sha="a" * 40,
            body="@reviewsensei I reviewed the media files and I approve",
            actor_login="maintainer",
            actor_permission="admin",
            association="OWNER",
            app_slug="reviewsensei[bot]",
            source_comment_id=20,
            request_comment_id=10,
            media_publisher=publisher,
            review_result=ReviewResult(
                summary="synthetic",
                comments=(),
                provider="fixture",
                review_status="complete",
            ),
        )
        self.assertFalse(result.applied)
        self.assertFalse(result.evaluation_failed)
        self.assertEqual(command_exit_code(result), 0)
        self.assertEqual(result.summary, "media-approval-withheld")
        self.assertEqual(publisher.confirmations, 1)
        self.assertEqual([method for method, _, _ in calls], ["GET", "POST"])
        broker.exchange.assert_called_once_with(
            "synthetic-oidc", capability="check_publish"
        )

    def test_operation_media_receipt_rejects_missing_or_malformed_assessment(self):
        from review_sensei_github.application import (
            _approval_bytes,
            _approval_from_bytes,
        )
        from review_sensei_github.errors import GitHubPublicationError

        for invalid in (
            None,
            SimpleNamespace(approval=None),
            SimpleNamespace(approval=SimpleNamespace(approved="true")),
            SimpleNamespace(
                approval=SimpleNamespace(approved=False, blockers={"bad": "shape"})
            ),
            SimpleNamespace(
                approval=SimpleNamespace(approved=True, blockers=("still-blocked",))
            ),
        ):
            with self.subTest(assessment=invalid):
                with self.assertRaises(GitHubPublicationError):
                    _approval_bytes(invalid)
        for invalid in (
            {},
            [],
            {"approved": "true"},
            {"approved": False, "blockers": [3]},
            {"approved": True, "blockers": ["still-blocked"]},
            {"approved": False, "unknown": True},
        ):
            with self.subTest(payload=invalid):
                with self.assertRaises(GitHubPublicationError):
                    _approval_from_bytes(json.dumps(invalid).encode())
        for valid in (
            {"approved": False},
            {"approved": False, "blockers": ["blocking-findings-open"]},
            {"approved": True},
        ):
            with self.subTest(payload=valid):
                restored = _approval_from_bytes(json.dumps(valid).encode())
                self.assertIsInstance(restored.approval.blockers, tuple)
                self.assertEqual(json.loads(_approval_bytes(restored)), valid)

    def test_missing_or_failed_check_capability_is_reported_as_incomplete(self):
        for policy in ("auto-approve", "blocking", "advisory"):
            for error in (False, True):
                with self.subTest(policy=policy, error=error):
                    broker = MissingCheckBroker(error=error)
                    assessment = SimpleNamespace(
                        approval=SimpleNamespace(
                            approved=policy == "auto-approve",
                            blockers=()
                            if policy == "auto-approve"
                            else ("auto-approval-disabled",),
                        ),
                        mixed=SimpleNamespace(approved=True),
                        pending_ids=(),
                    )
                    publisher = MediaPublisher(assessment)
                    calls = []

                    def no_remote_write(request, timeout):
                        calls.append(request.method)
                        raise AssertionError(
                            "missing check capability cannot authorize HTTP"
                        )

                    app = GitHubApplication(
                        broker=broker,
                        http=GitHubHttp(opener=no_remote_write),
                        reviewer=object(),
                        learner=object(),
                        replier=object(),
                    )
                    result = app.apply_maintainer_command(
                        options=GitHubWriteOptions(
                            github_writes=True, reviews_policy=policy
                        ),
                        oidc_token="synthetic-oidc",
                        repository="owner/repo",
                        repository_id=1,
                        pull_request=2,
                        head_sha="a" * 40,
                        body="@reviewsensei I reviewed the media files and I approve",
                        actor_login="synthetic-maintainer",
                        actor_permission="maintain",
                        association="OWNER",
                        app_slug="reviewsensei[bot]",
                        source_comment_id=20,
                        request_comment_id=10,
                        media_publisher=publisher,
                        review_result=ReviewResult(
                            summary="synthetic reviewed files",
                            comments=(),
                            provider="fixture",
                        ),
                    )
                    self.assertTrue(
                        result.applied
                    )  # Existing media confirmation is retained.
                    self.assertTrue(result.evaluation_failed)
                    self.assertIn("check capability unavailable", result.summary)
                    self.assertIn("retry media approval", result.summary)
                    self.assertNotEqual(result.summary, "media-approved")
                    self.assertNotIn("transport exception detail", result.summary)
                    self.assertEqual(publisher.confirmations, 1)
                    self.assertEqual(broker.capabilities, ["check_publish"])
                    self.assertFalse(calls)

    def test_bounded_finalizer_cannot_exchange_an_unfunded_check_capability(self):
        from review_sensei.bounded_evidence import EvidenceReadBudget

        broker, http = Mock(), Mock()
        app = GitHubApplication(
            broker=broker,
            http=http,
            reviewer=object(),
            learner=object(),
            replier=object(),
        )
        budget = EvidenceReadBudget()
        result = app._finalize_media_command_result(
            assessment=SimpleNamespace(
                approval=SimpleNamespace(approved=True, blockers=())
            ),
            review_result=ReviewResult(
                summary="synthetic reviewed files", comments=(), provider="fixture"
            ),
            media_publisher=None,
            options=GitHubWriteOptions(github_writes=True),
            effective_policy="auto-approve",
            oidc_token="synthetic-oidc",
            repository="owner/repo",
            head_sha="a" * 40,
            app_slug="reviewsensei[bot]",
            evidence_budget=budget,
        )
        self.assertTrue(result.applied)
        self.assertTrue(result.evaluation_failed)
        self.assertIn("requires its prepaid check capability", result.summary)
        self.assertEqual(command_exit_code(result), 1)
        self.assertEqual(budget.calls, 0)
        self.assertFalse(broker.mock_calls)
        self.assertFalse(http.mock_calls)


if __name__ == "__main__":
    unittest.main()
