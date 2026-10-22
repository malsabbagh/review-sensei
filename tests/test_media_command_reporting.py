"""Report incomplete check reconciliation through the public media command."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from review_sensei_github import GitHubApplication, GitHubHttp, GitHubWriteOptions
from review_sensei_github.cli import main as github_main

from review_sensei.disposition import command_exit_code
from review_sensei.models import ReviewResult
from tests.fake_github_http import json_response


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
    def test_public_cli_missing_media_context_fails_without_remote_writes(self):
        import review_sensei_github as github_module

        body = "@reviewsensei I reviewed the media files and I approve"
        attestation = {
            "version": 1,
            "repository": "owner/repo",
            "repository_id": 1,
            "pull_request": 2,
            "head_sha": "a" * 40,
            "operation": "command",
            "source_comment_id": 20,
            "concurrency_group": "reviewsensei-session-1-2",
        }
        broker = Mock()
        broker.authorize_session_mutation.return_value = SimpleNamespace(
            token="synthetic-command-capability",
            grant="synthetic-command-grant",
            attestation=attestation
            | {
                "actor": "maintainer",
                "actor_type": "User",
                "association": "OWNER",
                "command_id": 20,
                "command_digest": sha256(body.encode()).hexdigest(),
            },
        )
        calls = []

        def metadata_only(request, timeout):
            calls.append((request.method, request.full_url))
            self.assertEqual(request.method, "GET")
            if "/collaborators/maintainer/permission" in request.full_url:
                return json_response({"permission": "admin"})
            if "/pulls/2/reviews?" in request.full_url:
                return json_response([])
            raise AssertionError("unexpected command metadata request")

        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "attestation.json"
            path.write_text(json.dumps(attestation), encoding="utf-8")
            with (
                patch.dict(
                    os.environ, {"GITHUB_TOKEN": "synthetic-metadata-token"}, clear=True
                ),
                patch.object(github_module, "BrokerClient", return_value=broker),
                patch.object(
                    github_module,
                    "GitHubHttp",
                    return_value=GitHubHttp(opener=metadata_only),
                ),
                patch.object(
                    GitHubApplication, "_session_ledger_for_token"
                ) as ledger_factory,
                redirect_stdout(stdout),
            ):
                status = github_main(
                    [
                        "github",
                        "command",
                        "--comment-body",
                        body,
                        "--actor",
                        "maintainer",
                        "--association",
                        "OWNER",
                        "--repository",
                        "owner/repo",
                        "--repository-id",
                        "1",
                        "--pull-request",
                        "2",
                        "--head-sha",
                        "a" * 40,
                        "--source-comment-id",
                        "20",
                        "--session-attestation",
                        str(path),
                        "--github-session-ledger",
                        "--oidc-token",
                        "synthetic-oidc",
                        "--allow-write",
                    ]
                )
        self.assertEqual(status, 1)
        self.assertIn(
            "media approval requires the current review result", stdout.getvalue()
        )
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(method == "GET" for method, _ in calls))
        broker.authorize_session_mutation.assert_called_once()
        broker.exchange.assert_not_called()
        ledger_factory.assert_not_called()

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


if __name__ == "__main__":
    unittest.main()
