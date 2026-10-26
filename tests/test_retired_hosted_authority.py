"""Retired hosted authority must refuse before metadata or mutation effects."""

from __future__ import annotations

import argparse
import io
import os
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from review_sensei_github import cli, commands
from review_sensei_github.broker_client import BrokerClient, BrokerSessionGrant
from review_sensei_github.http import GitHubHttp
from review_sensei_github.run_authority import AUDIENCE, parse_authority
from review_sensei_github.session_ledger import GitHubIssueCommentSessionLedger

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.errors import ReviewInputError
from review_sensei.session import SessionIdentity

HEAD = "b" * 40
IDENTITY = SessionIdentity("owner/repo", 7, 123)
GRANT = "sg1.1700000000000." + "a" * 43


def attestation(version):
    document = {
        "version": 3,
        "purpose": "command",
        "repository": "owner/repo",
        "repository_id": 123,
        "pull_request": 7,
        "head_sha": HEAD,
        "run_id": "456",
        "run_attempt": 1,
        "job": "command",
        "job_workflow_ref": "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@refs/tags/v5",
        "job_workflow_sha": "c" * 40,
        "issued_at": 1_700_000_000,
        "expires_at": 1_700_000_600,
        "audience": AUDIENCE,
        "source": {
            "kind": "issue_comment",
            "id": 81,
            "updated_at": "2026-10-26T00:00:00Z",
            "body_sha256": "d" * 64,
        },
        "actor": {"login": "alice", "type": "User", "association": "OWNER"},
    }
    # Version 3 is a valid current grant, not malformed input which happens to
    # fail parsing before reaching the retired writer.
    parse_authority(document, grant=True)
    return {**document, "version": version}


class RetiredHostedAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.effects = []

        def forbidden(request, timeout):
            self.effects.append(request.get_method())
            raise AssertionError("retired authority reached HTTP")

        self.http = GitHubHttp(opener=forbidden)
        self.broker = BrokerClient(opener=forbidden)

    def ledger(self, **kwargs):
        return GitHubIssueCommentSessionLedger(
            self.http, token="synthetic", app_slug="reviewsensei[bot]", **kwargs
        )

    def test_versions_one_two_and_current_three_cannot_bind_old_writer(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                with self.assertRaisesRegex(
                    ReviewInputError, "phase ledgers are retired"
                ):
                    self.ledger(
                        broker=self.broker,
                        session_grant=GRANT,
                        session_attestation=attestation(version),
                        head_sha=HEAD,
                    )
                self.assertEqual(self.effects, [])

    def test_partial_old_authority_arguments_refuse_before_http(self):
        for key, value in (
            ("broker", self.broker),
            ("session_grant", GRANT),
            ("session_attestation", attestation(3)),
            ("head_sha", HEAD),
        ):
            with self.subTest(argument=key):
                with self.assertRaisesRegex(
                    ReviewInputError, "phase ledgers are retired"
                ):
                    self.ledger(**{key: value})
                self.assertEqual(self.effects, [])

    def test_old_tail_binder_rejects_every_version_without_charging_or_installing(self):
        budget = EvidenceReadBudget()
        ledger = self.ledger(evidence_budget=budget, enable_partition_writes=True)
        for version in (1, 2, 3):
            with self.subTest(version=version):
                grant = BrokerSessionGrant(
                    "synthetic-replacement", "known", GRANT, attestation(version)
                )
                with self.assertRaisesRegex(
                    ReviewInputError, "activation grants are retired"
                ):
                    ledger.bind_tail_grant(
                        IDENTITY,
                        grant,
                        operation_binding={"operation_id": "e" * 32},
                        attempt_reservation_id="f" * 64,
                        max_scan_pages=1,
                    )
                self.assertEqual(budget.calls, 0)
                self.assertEqual(ledger.token, "synthetic")
                self.assertIsNone(ledger._session_attestation)
                self.assertEqual(ledger._bound_tail_grants, set())
                self.assertEqual(self.effects, [])

    def test_old_review_and_aliases_never_enter_engine_or_publication(self):
        for arguments, status in (
            (["review"], 1),
            (["github", "review", "--help"], 2),
            (["github", "publish", "--help"], 2),
            (["github", "command", "--help"], 2),
        ):
            with (
                self.subTest(arguments=arguments),
                patch.object(
                    cli,
                    "engine_main",
                    side_effect=AssertionError("engine"),
                    create=True,
                ),
                patch.object(
                    cli, "_publication", side_effect=AssertionError("publication")
                ),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(cli.main(arguments), status)
        self.assertEqual(self.effects, [])

    def test_direct_actions_publication_and_reply_refuse_before_arguments_or_clients(
        self,
    ):
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "456"}, clear=True):
            for command in ("review", "publish", "reply"):
                with self.subTest(command=command):
                    with self.assertRaisesRegex(
                        ReviewInputError, "Actions phase entry is retired"
                    ):
                        commands.run_github(
                            argparse.Namespace(command=command), argv=[]
                        )
        self.assertEqual(self.effects, [])

    def test_old_hosted_command_refuses_without_resolving_attestation_or_client(self):
        args = argparse.Namespace(
            command="command",
            comment_body="@reviewsensei review pause",
            actor="alice",
            actor_type="User",
            association="OWNER",
            app_slug="reviewsensei[bot]",
            head_sha=HEAD,
            allow_write=True,
            github_session_ledger=True,
        )
        with self.assertRaisesRegex(
            ReviewInputError, "hosted command phase entry is retired"
        ):
            commands.run_github(args, argv=[])
        self.assertEqual(self.effects, [])

    def test_unbound_native_ledger_still_round_trips_normal_state(self):
        from tests.test_authenticated_partitions import GitHubState, fixture

        state = GitHubState()
        ledger = state.ledger()
        created = ledger.initialize(fixture.IDENTITY, now=fixture.NOW)
        loaded = ledger.load(fixture.IDENTITY, now=fixture.NOW)
        self.assertEqual(loaded.status, "ok")
        self.assertEqual(loaded.record, created)
        self.assertIsNone(ledger._broker)
        self.assertIsNone(ledger._session_attestation)
        self.assertTrue(state.calls)


if __name__ == "__main__":
    unittest.main()
