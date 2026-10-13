from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from review_sensei_github import GitHubApplication, GitHubHttp
from review_sensei_github.cli import main as github_main

from review_sensei.session import InMemorySessionLedger, SessionIdentity

HEAD = "b" * 40
FINGERPRINT = "abc123" + "0" * 58
BODY = "@reviewsensei override RS-ABC123 acceptable risk"
IDENTITY = SessionIdentity("owner/repo", 136, repository_id=99)


def _marker(*, head=HEAD, repository_id=99, pull_request=136):
    from review_sensei_github.publication import IndexedFinding, finding_index_marker

    return finding_index_marker(
        repository_id=repository_id,
        pull_request=pull_request,
        head_sha=head,
        base_sha="c" * 40,
        result_digest="d" * 64,
        entries=(
            IndexedFinding(
                instance=FINGERPRINT,
                lifecycle="e" * 64,
                blocking=True,
                needs_human=False,
            ),
        ),
    )


def _review(*, review_id=1, head=HEAD, actor="reviewsensei[bot]", body=None):
    return {
        "id": review_id,
        "commit_id": head,
        "user": {"login": actor},
        "body": _marker() if body is None else body,
    }


class HostedOverrideMetadataTests(unittest.TestCase):
    def _run(
        self,
        *,
        permission=None,
        environment=None,
        reviews=None,
        permission_status=200,
        review_status=200,
        actor="alice",
        actor_type="User",
        association="MEMBER",
        request_changes=None,
        broker_changes=None,
        allow_write=True,
    ):
        """Run the public CLI and real handler with synthetic external seams."""
        http = GitHubHttp()
        review_page = [_review()] if reviews is None else reviews
        calls = []

        def respond(method, url, token=None, **kwargs):
            calls.append(unittest.mock.call(method, url, token=token))
            if "/collaborators/" in url:
                return permission_status, permission
            if "/comments" in url:
                return 200, []
            if "/reviews" in url:
                if review_status != 200:
                    return review_status, {"message": "no"}
                return 200, review_page
            return 404, {}

        reads = Mock(side_effect=respond)
        attestation = {
            "version": 1,
            "repository": "owner/repo",
            "repository_id": 99,
            "pull_request": 136,
            "head_sha": HEAD,
            "operation": "command",
            "source_comment_id": 501,
            "run_id": "42",
            "issued_at": 1,
            "concurrency_group": "reviewsensei-session-99-136",
            "job_workflow_ref": "owner/repo/.github/workflows/review.yml@main",
            "job_workflow_sha": "c" * 40,
        }
        broker_attestation = {
            **attestation,
            "actor": actor,
            "actor_type": actor_type,
            "association": association,
            "command_id": 501,
            "command_digest": sha256(BODY.encode()).hexdigest(),
            **(broker_changes or {}),
        }
        broker = Mock()
        broker.authorize_session_mutation.return_value = SimpleNamespace(
            token="synthetic-session-capability",
            grant="synthetic-one-use-grant",
            attestation=broker_attestation,
        )
        ledger = InMemorySessionLedger()
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            request = root / "attestation.json"
            request.write_text(json.dumps({**attestation, **(request_changes or {})}))
            argv = [
                "github",
                "command",
                "--comment-body",
                BODY,
                "--actor",
                actor,
                "--actor-type",
                actor_type,
                "--association",
                association,
                "--app-slug",
                "reviewsensei[bot]",
                "--repository",
                "owner/repo",
                "--repository-id",
                "99",
                "--pull-request",
                "136",
                "--head-sha",
                HEAD,
                "--source-comment-id",
                "501",
                "--session-attestation",
                str(request),
                "--github-session-ledger",
                "--oidc-token",
                "synthetic-oidc",
            ]
            if allow_write:
                argv.append("--allow-write")
            with (
                patch.dict(os.environ, environment or {}, clear=True),
                patch.object(http, "request", reads),
                patch("review_sensei_github.GitHubHttp", return_value=http),
                patch("review_sensei_github.BrokerClient", return_value=broker),
                patch.object(
                    GitHubApplication, "_session_ledger_for_token", return_value=ledger
                ) as ledger_factory,
                redirect_stdout(output),
                redirect_stderr(output),
            ):
                status = github_main(argv)
        return SimpleNamespace(
            status=status,
            output=output.getvalue(),
            reads=reads,
            broker=broker,
            ledger=ledger,
            ledger_factory=ledger_factory,
            attestation=attestation,
        )

    def test_admin_and_maintain_apply_through_oidc_not_the_read_token(self):
        for permission in (
            {"permission": "admin", "role_name": "admin"},
            {"permission": "write", "role_name": "maintain"},
            {"permission": "maintain"},
        ):
            for token_name in ("GITHUB_TOKEN", "GH_TOKEN"):
                with self.subTest(permission=permission, token=token_name):
                    result = self._run(
                        permission=permission,
                        environment={token_name: " synthetic-read-token "},
                    )
                    self.assertEqual(result.status, 0, result.output)
                    record = result.ledger.load(IDENTITY).record
                    self.assertIsNotNone(record)
                    self.assertEqual(len(record.dispositions), 1)
                    self.assertEqual(record.dispositions[0]["fingerprint"], FINGERPRINT)
                    self.assertEqual(record.dispositions[0]["head_sha"], HEAD)
                    self.assertEqual(record.dispositions[0]["actor"], "alice")
                    observed = [
                        (call.args[0], call.args[1], call.kwargs.get("token"))
                        for call in result.reads.call_args_list[:2]
                    ]
                    self.assertEqual(
                        observed,
                        [
                            (
                                "GET",
                                "/repos/owner/repo/collaborators/alice/permission",
                                "synthetic-read-token",
                            ),
                            (
                                "GET",
                                "/repos/owner/repo/pulls/136/reviews?per_page=100&page=1",
                                "synthetic-read-token",
                            ),
                        ],
                    )
                    result.broker.authorize_session_mutation.assert_called_once_with(
                        "synthetic-oidc",
                        repository_id=99,
                        pull_request=136,
                        head_sha=HEAD,
                        session_attestation=result.attestation,
                    )
                    self.assertEqual(
                        result.ledger_factory.call_args.args,
                        ("synthetic-session-capability",),
                    )
                    self.assertEqual(
                        result.ledger_factory.call_args.kwargs["session_grant"],
                        "synthetic-one-use-grant",
                    )
                    capabilities = [
                        call.kwargs.get("capability")
                        for call in result.broker.exchange.call_args_list
                    ]
                    self.assertTrue(
                        set(capabilities)
                        <= {"review_publish", "check_publish", "issue_reply"}
                    )

    def test_missing_or_blank_tokens_fail_before_metadata_or_broker_calls(self):
        for environment in ({}, {"GITHUB_TOKEN": " "}, {"GH_TOKEN": "\t"}):
            with self.subTest(environment=environment):
                result = self._run(environment=environment)
                self.assertEqual(result.status, 1, result.output)
                self.assertIn("requires GitHub metadata access", result.output)
                result.reads.assert_not_called()
                result.broker.authorize_session_mutation.assert_not_called()
                result.ledger_factory.assert_not_called()

    def test_write_and_untrusted_custom_roles_do_not_gain_override_authority(self):
        for permission in (
            {"permission": "write"},
            {"permission": "write", "role_name": "write"},
            {"permission": "write", "role_name": "custom-maintainer"},
            {"permission": "write", "role_name": "admin"},
            {"permission": "write", "role_name": "Maintain"},
            {"permission": "write", "role_name": "maintain "},
            {"permission": "read", "role_name": "maintain"},
            {"permission": "none", "role_name": "maintain"},
            {"permission": "triage", "role_name": "maintain"},
        ):
            with self.subTest(permission=permission):
                result = self._run(
                    permission=permission,
                    environment={"GITHUB_TOKEN": "synthetic-read-token"},
                )
                self.assertEqual(result.status, 0, result.output)
                self.assertIn("unauthorized", result.output)
                result.ledger_factory.assert_not_called()
                self.assertIsNone(result.ledger.load(IDENTITY).record)

    def test_unauthorized_actors_fail_before_metadata_or_broker_calls(self):
        for changes in (
            {"association": "CONTRIBUTOR"},
            {"actor_type": "Bot"},
            {"actor": "reviewsensei[bot]"},
            {"actor": "alice/../bob"},
            {"allow_write": False},
        ):
            with self.subTest(changes=changes):
                result = self._run(
                    permission={"permission": "admin"},
                    environment={"GITHUB_TOKEN": "synthetic-read-token"},
                    **changes,
                )
                self.assertEqual(result.status, 1, result.output)
                result.reads.assert_not_called()
                result.broker.authorize_session_mutation.assert_not_called()
                result.ledger_factory.assert_not_called()

    def test_request_identity_mismatch_fails_before_network(self):
        for changes in (
            {"head_sha": "d" * 40},
            {"repository_id": 100},
            {"pull_request": 137},
            {"source_comment_id": 502},
        ):
            with self.subTest(changes=changes):
                result = self._run(
                    environment={"GITHUB_TOKEN": "synthetic-read-token"},
                    request_changes=changes,
                )
                self.assertEqual(result.status, 1, result.output)
                self.assertIn("does not match", result.output)
                result.reads.assert_not_called()
                result.broker.authorize_session_mutation.assert_not_called()
                result.ledger_factory.assert_not_called()

    def test_broker_rechecks_actor_head_and_comment_body_before_ledger_access(self):
        for changes in (
            {"head_sha": "d" * 40},
            {"association": "CONTRIBUTOR"},
            {"actor_type": "Bot"},
            {"actor": "reviewsensei[bot]"},
            {"command_digest": sha256(b"edited comment").hexdigest()},
        ):
            with self.subTest(changes=changes):
                result = self._run(
                    permission={"permission": "write", "role_name": "maintain"},
                    environment={"GITHUB_TOKEN": "synthetic-read-token"},
                    broker_changes=changes,
                )
                self.assertEqual(result.status, 1, result.output)
                result.broker.authorize_session_mutation.assert_called_once()
                result.ledger_factory.assert_not_called()
                self.assertIsNone(result.ledger.load(IDENTITY).record)

    def test_only_latest_app_review_on_current_head_supplies_fingerprints(self):
        result = self._run(
            permission={"permission": "admin"},
            environment={"GITHUB_TOKEN": "synthetic-read-token"},
            reviews=[
                _review(review_id=1, body="no finding"),
                _review(review_id=2, actor="ReviewSensei[bot]"),
                _review(review_id=3, actor="mallory", body="foreign review"),
                _review(review_id=4, head="d" * 40, body="stale review"),
            ],
        )
        self.assertEqual(result.status, 0, result.output)
        self.assertEqual(
            result.ledger.load(IDENTITY).record.dispositions[0]["fingerprint"],
            FINGERPRINT,
        )

    def test_stale_foreign_or_superseded_finding_markers_cannot_be_overridden(self):
        for reviews in (
            [_review(head="d" * 40)],
            [_review(actor="mallory")],
            [_review(body=_marker(head="d" * 40))],
            [_review(body=_marker(repository_id=100))],
            [_review(body=_marker(pull_request=137))],
            [_review(), _review(review_id=2, body="no current finding")],
        ):
            with self.subTest(reviews=reviews):
                result = self._run(
                    permission={"permission": "admin"},
                    environment={"GITHUB_TOKEN": "synthetic-read-token"},
                    reviews=reviews,
                )
                self.assertEqual(result.status, 1, result.output)
                self.assertIn("finding id needs the current review", result.output)
                record = result.ledger.load(IDENTITY).record
                self.assertTrue(record is None or not record.dispositions)

    def test_unavailable_metadata_never_exchanges_write_authority(self):
        for changes in (
            {"permission_status": 403},
            {"permission": {"role_name": "maintain"}},
            {"permission": {"permission": None}},
            {"review_status": 403},
        ):
            with self.subTest(changes=changes):
                result = self._run(
                    environment={"GITHUB_TOKEN": "synthetic-read-token"},
                    **({"permission": {"permission": "admin"}} | changes),
                )
                self.assertEqual(result.status, 1, result.output)
                self.assertIn("unavailable", result.output)
                result.broker.authorize_session_mutation.assert_not_called()
                result.ledger_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
