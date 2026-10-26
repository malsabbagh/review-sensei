"""Explicit cross-runtime broker qualification, not default Python discovery.

Run `python -m tests.epic238_feedback_broker_qualification -v` after installing
the pinned Worker npm dependencies. Uses the production signed-grant policy.
The worker stores nothing. It qualifies the feedback protocol, never host
root/witness/lifecycle authority.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import time
import unittest
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

from review_sensei_github.broker_control import BrokerDispatchAccounting
from review_sensei_github.errors import GitHubBrokerClientError

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.errors import ReviewInputError
from tests.epic238_public_dispatch_support import (
    PhysicalDispatchTrace,
    ProductionBrokerBridge,
)

ROOT = Path(__file__).resolve().parents[1]
MEASUREMENTS = {}


class FeedbackBrokerQualification(unittest.TestCase):
    def setUp(self):
        if not hasattr(self, "segments"):
            self.segments = []
        self.temporary = TemporaryDirectory(prefix="epic238-feedback-broker-")
        self.directory = Path(self.temporary.name)
        self.trace = PhysicalDispatchTrace(self.directory)
        self.bridge = ProductionBrokerBridge(self.directory, self.trace)
        self.bridge.call("configure_feedback")
        self.request = self.selected()
        self.vector = json.loads(
            (ROOT / "tests/fixtures/feedback-session-attestation.json").read_text(
                encoding="utf-8"
            )
        )

    def tearDown(self):
        try:
            self.bridge.close()
            self.assertIsNotNone(self.bridge.process.poll())
            self.assertFalse(self.bridge.reader.is_alive())
            self.record_segment()
            MEASUREMENTS[self._testMethodName] = {
                "scope": "Independent fixture segments, not one host operation allowance",
                "segments": self.segments,
                "attempts": sum(item["attempts"] for item in self.segments),
            }
        finally:
            self.temporary.cleanup()

    def oidc(self, client):
        with patch.dict(
            "os.environ",
            {
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic-oidc-request",
                "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.github.test/fixture-oidc?fixture=1",
            },
            clear=True,
        ):
            return client.request_oidc_token()

    def issue(self, request=None, *, client=None):
        client = client or self.bridge.client()
        return client.authorize_session_mutation(
            self.oidc(client),
            repository_id=987654321,
            pull_request=7,
            head_sha="a" * 40,
            session_attestation=request or self.request,
        )

    def refuse(self, request=None):
        with self.assertRaises(GitHubBrokerClientError):
            self.issue(request)
        self.assertEqual(self.bridge.call("grant_count")["result"], 0)
        self.assertNotIn("session_issue", self.bridge.internal_dispatches()["ledger"])

    def assert_signed_grant(self, grant):
        """A signed grant verifies again because the worker does not store it."""
        client = self.bridge.client()
        self.assertEqual(
            client.verify_session_grant(grant.grant, grant.attestation),
            grant.attestation,
        )
        self.assertEqual(self.bridge.call("grant_count")["result"], 0)
        self.assertEqual(
            client.verify_session_grant(grant.grant, grant.attestation),
            grant.attestation,
        )

    def fresh_case(self):
        """Independent negative cases, not renewed allowance for one lifecycle."""
        self.bridge.close()
        self.record_segment()
        self.temporary.cleanup()
        self.setUp()

    def record_segment(self):
        attempts = self.trace.attempts()
        self.segments.append(
            {
                "attempts": len(attempts),
                "origins": dict(Counter(item["origin"] for item in attempts)),
                "paths": dict(Counter(item["path"] for item in attempts)),
                "ledger_actions": dict(
                    Counter(
                        item["action"]
                        for item in attempts
                        if item["origin"] == "broker-ledger"
                    )
                ),
            }
        )

    def selected(self, *, purpose="reply", kind="issue_comment"):
        """Use the actual Python producer; GitHub source metadata is synthetic."""
        from review_sensei_github.run_authority import request_authority

        metadata = self.bridge.call("authority", purpose=purpose, kind=kind)["result"]
        with patch.dict(
            "os.environ",
            {
                "GITHUB_RUN_ID": "10000000001",
                "GITHUB_RUN_ATTEMPT": "1",
                "REVIEW_SENSEI_WORKFLOW_REF": metadata["job_workflow_ref"],
                "REVIEW_SENSEI_WORKFLOW_SHA": metadata["job_workflow_sha"],
            },
            clear=True,
        ):
            return request_authority(
                purpose=purpose,
                repository="acme/widgets",
                repository_id=987654321,
                pull_request=7,
                head_sha="a" * 40,
                job="command" if purpose == "command" else "hosted",
                source=metadata["source"],
            )

    def controlled_client(self, budget, *, ceiling=60):
        receipts = []

        class RecordingControl(BrokerDispatchAccounting):
            def verify(self, receipt, request):
                super().verify(receipt, request)
                receipts.append(copy.deepcopy(receipt))

        client = self.bridge.client(before_request=budget.consume)
        client.control = RecordingControl(budget, "c" * 64, ceiling, ceiling)
        client.qualification_receipts = receipts
        return client

    def test_complete_unicode_source_is_body_free_and_only_caller_posts_are_charged(
        self,
    ):
        body = self.vector["selection"]["sources"][0]["body"]
        self.assertGreater(len(body.encode()), 4096)
        budget = EvidenceReadBudget()
        deadline = budget.wall_deadline_ms
        client = self.controlled_client(budget)
        grant = self.issue(client=client)
        self.assertEqual(grant.attestation["version"], 3)
        self.assertEqual(grant.attestation["purpose"], "reply")
        self.assertEqual(grant.attestation["source"], self.request["source"])
        self.assertEqual(
            grant.attestation["actor"],
            {"login": "octocat", "type": "User", "association": "OWNER"},
        )
        self.assertNotIn(body, json.dumps(grant.attestation, ensure_ascii=False))
        self.assertEqual(
            grant.attestation["source"]["body_sha256"],
            hashlib.sha256(body.encode()).hexdigest(),
        )
        client.verify_session_grant(grant.grant, grant.attestation)
        self.assertEqual(budget.calls, 3)  # OIDC GET + issue POST + verify POST.
        self.assertEqual(budget.wall_deadline_ms, deadline)
        attempts = self.trace.attempts()
        self.assertEqual(sum(x["origin"] == "caller-broker" for x in attempts), 3)
        self.assertGreater(sum(x["origin"] == "broker-internal" for x in attempts), 3)
        self.assertFalse(any(x["origin"] == "broker-ledger" for x in attempts))
        self.assertEqual(len(client.qualification_receipts), 2)
        self.assertEqual(
            sum(r["dispatched"] for r in client.qualification_receipts),
            sum(x["origin"] == "broker-internal" for x in attempts),
        )
        self.assertTrue(
            all(
                r["max_dispatches"] == 60 and r["deadline_unix_ms"] == deadline
                for r in client.qualification_receipts
            )
        )
        client.verify_session_grant(grant.grant, grant.attestation)
        self.assertEqual(
            budget.calls, 4
        )  # Verification is stateless, not host replay authority.
        self.assertEqual(self.bridge.call("grant_count")["result"], 0)

    def test_live_actor_source_body_deletion_and_canonical_endpoint_refuse(self):
        original = self.vector["selection"]["sources"][0]["body"]
        changed = original[:-1] + "改"
        self.assertEqual(original.encode()[:4096], changed.encode()[:4096])
        changes = (
            ("actor-id", {"authorId": 999}),
            ("login", {"login": "impostor"}),
            ("bot", {"userType": "Bot"}),
            ("association", {"association": "CONTRIBUTOR"}),
            ("timestamp", {"updatedAt": "edited"}),
            ("wrong-comment", {"id": 13580}),
            (
                "foreign-pr",
                {"url": "https://api.github.test/repos/acme/widgets/issues/8"},
            ),
            ("deleted", {"status": 404}),
            ("late-unicode-body", {"body": changed}),
        )
        for label, change in changes:
            with self.subTest(label=label):
                self.fresh_case()
                self.bridge.call("set_source", source=change)
                self.refuse()

    def test_inline_current_source_actor_deletion_and_canonical_parent_refuse(self):
        for change in (
            {"rootCommentId": 0},
            {"authorId": 99},
            {"status": 404},
            {"url": "https://api.github.test/repos/acme/widgets/pulls/8"},
            {"userType": "Bot"},
            {"body": "@sensei edited after selection"},
        ):
            with self.subTest(change=change):
                self.fresh_case()
                self.prepare_inline()
                request = self.selected(kind="review_comment")
                self.bridge.call("set_inline_source", source=change)
                self.refuse(request)

    def test_live_pr_numeric_identity_repository_and_head_refuse(self):
        for change in (
            {"number": 8},
            {"head": "f" * 40},
            {"repositoryId": 99},
            {"repository": "other/repo"},
        ):
            with self.subTest(change=change):
                self.fresh_case()
                self.bridge.call("set_snapshot", snapshot=change)
                self.refuse()

    def test_worker_scope_is_head_source_not_a_base_or_open_state_completion_witness(
        self,
    ):
        # V3 intentionally contains no reviewed base, draft or PR-state claim.
        # The caller separately enforces those before entry; issuance alone
        # cannot prove approval eligibility or authorize a host action replay.
        for change in ({"state": "closed"}, {"draft": True}, {"base": "e" * 40}):
            with self.subTest(change=change):
                self.fresh_case()
                self.bridge.call("set_snapshot", snapshot=change)
                grant = self.issue()
                self.assertNotIn("base_sha", grant.attestation)
                self.assert_signed_grant(grant)

    def test_current_caller_rejects_closed_draft_or_conflicting_reviewed_base_before_effects(
        self,
    ):
        from urllib.parse import urlsplit

        from tests.fake_github_http import json_response
        from tests.test_review_run import EntryFixture

        for change in ({"state": "closed"}, {"draft": True}, {"base": "e" * 40}):
            with self.subTest(change=change), TemporaryDirectory() as directory:
                fixture = EntryFixture(directory)
                original = fixture.external

                def external(request, timeout):
                    response = original(request, timeout)
                    if urlsplit(request.full_url).path.endswith("/pulls/7"):
                        value = json.loads(response.read(524288))
                        if "base" in change:
                            value["base"]["sha"] = change["base"]
                        else:
                            value.update(change)
                        return json_response(value)
                    return response

                fixture.external = external
                self.assertEqual(fixture.invoke(), 1)
                self.assertEqual(fixture.provider_calls, 0)
                self.assertIsNone(fixture.comment)
                self.assertFalse(fixture.reviews)

    def test_real_signed_oidc_numeric_actor_repository_run_and_workflow_mismatch_refuse(
        self,
    ):
        for change in (
            {"actor_id": "99"},
            {"actor": "impostor"},
            {"repository_id": "99"},
            {"event_name": "pull_request"},
            {"run_id": "999"},
            {"run_attempt": "2"},
            {"job_workflow_sha": "f" * 40},
        ):
            with self.subTest(change=change):
                self.fresh_case()
                self.bridge.oidc_overrides = change
                self.refuse()

    def test_missing_mention_refuses_even_with_exact_new_body_digest(self):
        body = self.vector["selection"]["sources"][0]["body"].replace(
            "@sensei", "Already reviewed", 1
        )
        self.bridge.call("set_source", source={"body": body})
        self.refuse(self.selected())

    def test_exact_leading_bom_unicode_body_is_retained_during_live_issuance(self):
        body = "\ufeff\n" + self.vector["selection"]["sources"][0]["body"]
        self.bridge.call("set_source", source={"body": body})
        request = self.selected()
        grant = self.issue(request)
        self.assertEqual(grant.attestation["source"], request["source"])
        self.assertEqual(
            grant.attestation["source"]["body_sha256"],
            hashlib.sha256(body.encode()).hexdigest(),
        )
        self.assert_signed_grant(grant)

    def test_python_float_source_identifier_refuses_before_worker_issuance(self):
        request = copy.deepcopy(self.request)
        request["source"]["id"] = 13579.0
        self.refuse(request)
        self.assertEqual(len(self.trace.attempts()), 1)  # OIDC GET only.

    def test_worker_closed_v3_enums_and_extensions_refuse_raw_authenticated_client(
        self,
    ):
        cases = (
            ("purpose", ["reply"]),
            ("job", ["hosted"]),
            ("audience", ["reviewsensei-run-action"]),
            ("source", {**self.request["source"], "kind": ["issue_comment"]}),
            ("actor", {"login": "octocat", "type": "User", "association": "OWNER"}),
            ("feedback", self.vector["request"]["feedback"]),
            ("mutation", self.vector["request"]["mutation"]),
        )
        for key, value in cases:
            with self.subTest(key=key):
                self.fresh_case()
                attestation = {**self.request, key: value}
                body = {
                    "oidc_token": self.oidc(self.bridge.client()),
                    "capability": "review_session",
                    "session": {
                        "repository_id": 987654321,
                        "pull_request": 7,
                        "head_sha": "a" * 40,
                    },
                    "session_attestation": attestation,
                }
                request = Request(
                    "https://broker.github.test/api/github/token",
                    data=json.dumps(body).encode(),
                    method="POST",
                )
                opened = self.trace.opener(self.bridge.open, origin="caller-broker")
                with self.assertRaises(HTTPError) as caught:
                    opened(request, timeout=20)
                self.assertEqual(caught.exception.code, 403)
                try:
                    self.assertEqual(
                        json.loads(caught.exception.fp.read(512))["error"],
                        "broker_session_attestation_invalid",
                    )
                finally:
                    caught.exception.close()
                self.assertEqual(self.bridge.call("grant_count")["result"], 0)

    def prepare_inline(self):
        body = "@sensei " + self.vector["selection"]["sources"][1]["body"]
        self.bridge.call("set_inline_source", source={"body": body})
        self.bridge.oidc_overrides = {
            "actor": "collaborator",
            "actor_id": "42",
            "event_name": "pull_request_review_comment",
        }

    def test_inline_trigger_has_distinct_current_source_and_real_numeric_actor(self):
        self.prepare_inline()
        request = self.selected(kind="review_comment")
        self.assertNotEqual(request["source"], self.request["source"])
        grant = self.issue(request)
        self.assertEqual(grant.attestation["source"]["kind"], "review_comment")
        self.assertEqual(
            grant.attestation["actor"],
            {"login": "collaborator", "type": "User", "association": "COLLABORATOR"},
        )
        self.assert_signed_grant(grant)

    def test_command_review_and_reply_share_current_producer_and_grant_signatures(self):
        for purpose in ("command", "review", "reply"):
            with self.subTest(purpose=purpose):
                self.fresh_case()
                if purpose == "command":
                    self.bridge.call(
                        "set_source", source={"body": "@reviewsensei review pause"}
                    )
                request = self.selected(purpose=purpose)
                grant = self.issue(request)
                self.assertEqual(grant.attestation["purpose"], purpose)
                self.assertEqual(
                    grant.attestation["job"],
                    "command" if purpose == "command" else "hosted",
                )
                self.assert_signed_grant(grant)

    def test_every_current_scope_field_tamper_preserves_original_signed_grant(self):
        grant = self.issue()
        changes = {
            "repository": "other/repo",
            "repository_id": 99,
            "pull_request": 8,
            "head_sha": "f" * 40,
            "run_id": "999",
            "run_attempt": 2,
            "job": "local",
            "job_workflow_ref": self.request["job_workflow_ref"].replace("v5", "v6"),
            "job_workflow_sha": "f" * 40,
            "issued_at": grant.attestation["issued_at"] - 1,
            "expires_at": grant.attestation["expires_at"] - 1,
            "source": {**grant.attestation["source"], "id": 13580},
            "actor": {**grant.attestation["actor"], "login": "impostor"},
        }
        client = self.bridge.client()
        for key, value in changes.items():
            with self.subTest(key=key):
                changed = {**grant.attestation, key: value}
                with self.assertRaises(GitHubBrokerClientError):
                    client.verify_session_grant(grant.grant, changed)
                self.assertEqual(self.bridge.call("grant_count")["result"], 0)
        for key, value in (
            ("body_sha256", "e" * 64),
            ("updated_at", "2026-10-11T00:00:00Z"),
            ("kind", "review_comment"),
        ):
            with self.subTest(source=key):
                changed = {
                    **grant.attestation,
                    "source": {**grant.attestation["source"], key: value},
                }
                with self.assertRaises(GitHubBrokerClientError):
                    client.verify_session_grant(grant.grant, changed)
        for changed in (
            {**grant.attestation, "purpose": "command", "job": "command"},
            {
                **grant.attestation,
                "actor": {**grant.attestation["actor"], "association": "MEMBER"},
            },
        ):
            with self.subTest(scope=changed):
                with self.assertRaises(GitHubBrokerClientError):
                    client.verify_session_grant(grant.grant, changed)
        self.assert_signed_grant(grant)

    def test_original_caller_budget_refuses_before_dispatch_without_deadline_reset(
        self,
    ):
        budget = EvidenceReadBudget()
        for _ in range(59):
            budget.consume()
        deadline = budget.wall_deadline_ms
        client = self.controlled_client(budget)
        assertion = self.oidc(client)
        self.assertEqual(budget.calls, 60)
        with self.assertRaisesRegex(ReviewInputError, "exceeds caller budget"):
            client.authorize_session_mutation(
                assertion,
                repository_id=987654321,
                pull_request=7,
                head_sha="a" * 40,
                session_attestation=self.request,
            )
        self.assertEqual(budget.calls, 60)
        self.assertEqual(budget.wall_deadline_ms, deadline)
        self.assertEqual(len(self.trace.attempts()), 1)
        self.assertEqual(self.bridge.call("grant_count")["result"], 0)

    def test_finite_worker_ceiling_refuses_without_internal_caller_predebits(self):
        budget = EvidenceReadBudget()
        client = self.controlled_client(budget, ceiling=1)
        with self.assertRaises(GitHubBrokerClientError):
            self.issue(client=client)
        self.assertEqual(budget.calls, 2)  # OIDC and one refused Worker POST.
        self.assertEqual(
            sum(x["origin"] == "broker-internal" for x in self.trace.attempts()), 1
        )
        self.assertEqual(self.bridge.call("grant_count")["result"], 0)

    def test_oidc_replay_inside_five_minutes_issues_again_and_an_older_one_refuses(
        self,
    ):
        client = self.bridge.client()
        assertion = self.oidc(client)
        kwargs = dict(
            repository_id=987654321,
            pull_request=7,
            head_sha="a" * 40,
            session_attestation=self.request,
        )
        grant = client.authorize_session_mutation(assertion, **kwargs)
        self.assert_signed_grant(grant)
        self.assert_signed_grant(client.authorize_session_mutation(assertion, **kwargs))
        stale = self.bridge.call("oidc", overrides={"iat": int(time.time()) - 301})[
            "result"
        ]
        with self.assertRaises(GitHubBrokerClientError):
            client.authorize_session_mutation(stale, **kwargs)
        self.assertEqual(self.bridge.call("grant_count")["result"], 0)

    def test_process_kill_before_or_after_stateless_verification_never_grants_host_completion(
        self,
    ):
        for before in (True, False):
            with self.subTest(before_verification=before):
                self.fresh_case()
                grant = self.issue()
                reply = self.bridge.call(
                    "verify",
                    body={
                        "session_grant": grant.grant,
                        "session_attestation": grant.attestation,
                    },
                    **(
                        {"pause_before_verify": True}
                        if before
                        else {"pause_after_consume": True}
                    ),
                )
                self.assertEqual(
                    reply["checkpoint"],
                    "before-grant-verification" if before else "grant-consumed",
                )
                self.assertNotIn("status", reply)
                durable = self.trace.attempts()
                self.assertFalse(any(x["origin"] == "broker-ledger" for x in durable))
                self.bridge.process.kill()
                self.bridge.process.wait(timeout=5)
                self.assertEqual(
                    self.bridge.process.returncode, 1 if os.name == "nt" else -9
                )
                self.assertEqual(self.trace.attempts(), durable)
                self.bridge.close()
                self.bridge = ProductionBrokerBridge(self.directory, self.trace)
                self.assertEqual(self.bridge.call("grant_count")["result"], 0)
                self.assert_signed_grant(grant)

    def test_verification_does_not_supply_pending_host_source_fence(self):
        grant = self.issue()
        self.bridge.call(
            "set_source", source={"body": "@sensei Changed after issuance."}
        )
        before = self.bridge.internal_dispatches()["transport"]
        self.assert_signed_grant(grant)
        after = self.bridge.internal_dispatches()["transport"]
        self.assertTrue(
            all(
                item["path"].endswith("/git/ref/tags/v5")
                for item in after[len(before) :]
            )
        )
        # A fresh issuance independently sees the changed live source and refuses.
        self.refuse()


if __name__ == "__main__":
    program = unittest.main(exit=False)
    artifact = os.getenv("EPIC238_F_MEASUREMENTS")
    if artifact:
        Path(artifact).write_text(json.dumps(MEASUREMENTS, indent=2) + "\n")
    raise SystemExit(not program.result.wasSuccessful())
