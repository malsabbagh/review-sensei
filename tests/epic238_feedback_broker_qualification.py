"""Explicit cross-runtime broker qualification, not default Python discovery.

Run `python -m tests.epic238_feedback_broker_qualification -v` after installing
the pinned Worker npm dependencies. Uses actual production policy/SQLite/OIDC.
It qualifies the feedback protocol, never host root/witness/lifecycle authority.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import unittest
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.errors import ReviewInputError
from review_sensei.evidence import evidence_digest
from review_sensei.hosting.github.errors import GitHubBrokerClientError
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
        self.request = self.bridge.call("configure_feedback")["result"]
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

    def assert_consumed(self, grant):
        client = self.bridge.client()
        self.assertEqual(
            client.verify_session_grant(grant.grant, grant.attestation),
            grant.attestation,
        )
        self.assertEqual(self.bridge.call("grant_count")["result"], 0)
        with self.assertRaises(GitHubBrokerClientError):
            client.verify_session_grant(grant.grant, grant.attestation)

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

    def rebound_selection(self, complete):
        request = copy.deepcopy(self.request)
        metadata = copy.deepcopy(complete)
        metadata["selection_digest"] = evidence_digest(complete)
        metadata["event_key"] = evidence_digest(
            {
                "domain": "reviewsensei:feedback-event:v1",
                "repository": complete["repository"],
                "pull_request": complete["pull_request"],
                "trigger": {
                    "kind": complete["trigger"]["kind"],
                    "comment_id": complete["trigger"]["comment_id"],
                },
            }
        )
        for source in metadata["sources"]:
            source.pop("body")
        request["source_comment_id"] = complete["trigger"]["comment_id"]
        request["feedback"] = metadata
        return request

    def test_complete_unicode_feedback_is_body_free_and_consumed_once(self):
        self.assertGreater(self.vector["selection"]["sources"][0]["body_bytes"], 4096)
        budget = EvidenceReadBudget(
            snapshot=self.request["mutation"]["read_accounting"]
        )
        original_deadline = budget.wall_deadline_ms
        client = self.bridge.client(before_request=budget.consume)
        grant = self.issue(client=client)
        self.assertEqual(grant.attestation["operation"], "feedback")
        self.assertEqual(grant.attestation["version"], 2)
        self.assertEqual(grant.attestation["actor_id"], 12345678)
        self.assertEqual(grant.attestation["feedback"], self.request["feedback"])
        self.assertEqual(grant.attestation["mutation"], self.request["mutation"])
        for source in self.vector["selection"]["sources"]:
            self.assertNotIn(
                source["body"], json.dumps(grant.attestation, ensure_ascii=False)
            )
        client.verify_session_grant(grant.grant, grant.attestation)
        self.assertEqual(budget.calls, 23)  # 20 original + three caller transports.
        self.assertEqual(budget.wall_deadline_ms, original_deadline)
        attempts = self.trace.attempts()
        self.assertEqual(sum(item["origin"] == "caller-broker" for item in attempts), 3)
        self.assertGreater(
            sum(item["origin"] == "broker-internal" for item in attempts), 0
        )
        self.assertEqual(
            Counter(
                item["action"] for item in attempts if item["origin"] == "broker-ledger"
            ),
            Counter(
                {
                    "admit": 1,
                    "claim": 1,
                    "session_enroll": 1,
                    "session_issue": 1,
                    "session_verify": 1,
                }
            ),
        )
        # The caller callback does not account Worker internals. Preserve their
        # physical count separately; this is not whole-operation64 acceptance.
        self.assertEqual(self.bridge.call("grant_count")["result"], 0)
        with self.assertRaises(GitHubBrokerClientError):
            client.verify_session_grant(grant.grant, grant.attestation)
        self.assertEqual(budget.calls, 24)

    def test_live_actor_source_body_deletion_and_canonical_endpoint_refuse(self):
        changes = [
            ("actor-id", "set_source", {"authorId": 999}),
            ("login", "set_source", {"login": "impostor"}),
            ("bot", "set_source", {"userType": "Bot"}),
            ("association", "set_source", {"association": "CONTRIBUTOR"}),
            ("timestamp", "set_source", {"updatedAt": "edited"}),
            ("wrong-comment", "set_source", {"id": 13580}),
            (
                "foreign-pr",
                "set_source",
                {"url": "https://api.github.test/repos/acme/widgets/issues/8"},
            ),
            ("deleted", "set_source", {"status": 404}),
            ("inline-root", "set_inline_source", {"rootCommentId": 999}),
            ("inline-actor", "set_inline_source", {"authorId": 99}),
            ("inline-deleted", "set_inline_source", {"status": 404}),
        ]
        original = self.vector["selection"]["sources"][0]["body"]
        changed = original[:-1] + "改"
        self.assertEqual(original.encode()[:4096], changed.encode()[:4096])
        changes.append(("late-unicode-body", "set_source", {"body": changed}))
        for label, action, change in changes:
            with self.subTest(label=label):
                self.fresh_case()
                self.bridge.call(action, source=change)
                self.refuse()

    def test_live_pr_numeric_state_base_and_head_refuse(self):
        for change in (
            {"number": 8},
            {"state": "closed"},
            {"draft": True},
            {"base": "e" * 40},
            {"head": "f" * 40},
        ):
            with self.subTest(change=change):
                self.fresh_case()
                self.bridge.call("set_snapshot", snapshot=change)
                self.refuse()

    def test_real_signed_oidc_numeric_actor_and_repository_mismatch_refuse(self):
        for change in (
            {"actor_id": "99"},
            {"actor": "impostor"},
            {"repository_id": "99"},
            {"event_name": "pull_request"},
        ):
            with self.subTest(change=change):
                self.fresh_case()
                self.bridge.oidc_overrides = change
                self.refuse()

    def test_missing_mention_refuses_even_with_exact_new_body_digest(self):
        complete = copy.deepcopy(self.vector["selection"])
        source = complete["sources"][0]
        source["body"] = source["body"].replace("@sensei", "Already reviewed", 1)
        source["body_bytes"] = len(source["body"].encode("utf-8"))
        source["body_sha256"] = hashlib.sha256(source["body"].encode()).hexdigest()
        complete["total_bytes"] = sum(
            item["body_bytes"] for item in complete["sources"]
        )
        self.bridge.call("set_source", source={"body": source["body"]})
        self.refuse(self.rebound_selection(complete))

    def test_exact_leading_bom_unicode_body_is_retained_during_live_issuance(self):
        complete = copy.deepcopy(self.vector["selection"])
        source = complete["sources"][0]
        source["body"] = "\ufeff\n" + source["body"]
        source["body_bytes"] = len(source["body"].encode("utf-8"))
        source["body_sha256"] = hashlib.sha256(source["body"].encode()).hexdigest()
        complete["total_bytes"] = sum(
            item["body_bytes"] for item in complete["sources"]
        )
        self.bridge.call("set_source", source={"body": source["body"]})
        request = self.rebound_selection(complete)
        grant = self.issue(request)
        self.assertEqual(grant.attestation["feedback"], request["feedback"])
        self.assertEqual(
            grant.attestation["feedback"]["sources"][0]["body_sha256"],
            hashlib.sha256(source["body"].encode()).hexdigest(),
        )
        self.assert_consumed(grant)

    def test_python_float_trigger_refuses_before_worker_issuance(self):
        request = copy.deepcopy(self.request)
        request["feedback"]["trigger"]["comment_id"] = 13579.0
        # Recompute the legacy-compatible event hash so rejection exercises
        # exact integer typing rather than an unrelated stale-hash failure.
        request["feedback"]["event_key"] = evidence_digest(
            {
                "domain": "reviewsensei:feedback-event:v1",
                "repository": request["feedback"]["repository"],
                "pull_request": request["feedback"]["pull_request"],
                "trigger": {"kind": "issue", "comment_id": 13579.0},
            }
        )
        self.refuse(request)
        self.assertEqual(len(self.trace.attempts()), 1)  # The OIDC GET only.

    def test_worker_closed_enums_refuse_arrays_from_raw_authenticated_client(self):
        cases = [
            (("mutation", "reason"), ["dispatch"]),
            (("feedback", "trigger", "kind"), ["issue"]),
            (("feedback", "sources", 0, "kind"), ["issue"]),
            (("feedback", "sources", 0, "association"), ["OWNER"]),
        ]
        for path, value in cases:
            with self.subTest(path=path):
                self.fresh_case()
                attestation = copy.deepcopy(self.request)
                current = attestation
                for key in path[:-1]:
                    current = current[key]
                current[path[-1]] = value
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
                        json.loads(caught.exception.fp.read(256))["error"],
                        "broker_feedback_attestation_invalid",
                    )
                finally:
                    caught.exception.close()
                self.assertEqual(self.bridge.call("grant_count")["result"], 0)

    def test_inline_trigger_has_distinct_event_and_real_numeric_actor(self):
        complete = copy.deepcopy(self.vector["selection"])
        source = complete["sources"][1]
        source["body"] = "@sensei " + source["body"]
        source["body_bytes"] = len(source["body"].encode())
        source["body_sha256"] = hashlib.sha256(source["body"].encode()).hexdigest()
        complete["trigger"] = {
            key: source[key] for key in ("kind", "comment_id", "updated_at")
        }
        complete["total_bytes"] = sum(
            item["body_bytes"] for item in complete["sources"]
        )
        self.bridge.call("set_inline_source", source={"body": source["body"]})
        self.bridge.oidc_overrides = {
            "actor": "collaborator",
            "actor_id": "42",
            "event_name": "pull_request_review_comment",
        }
        request = self.rebound_selection(complete)
        self.assertNotEqual(
            request["feedback"]["event_key"], self.request["feedback"]["event_key"]
        )
        grant = self.issue(request)
        self.assertEqual(grant.attestation["actor_id"], 42)
        self.assertEqual(grant.attestation["association"], "COLLABORATOR")
        self.assert_consumed(grant)

    def test_ordered_sources_targets_and_selection_digest_are_recomputed(self):
        for path, value in (
            (("feedback", "target_ids"), ["e" * 64]),
            (
                ("feedback", "sources"),
                list(reversed(self.request["feedback"]["sources"])),
            ),
            (("feedback", "selection_digest"), "f" * 64),
        ):
            with self.subTest(path=path):
                self.fresh_case()
                request = copy.deepcopy(self.request)
                request[path[0]][path[1]] = value
                self.refuse(request)

    def test_every_mutation_reference_and_target_tamper_preserves_original_grant(self):
        grant = self.issue()
        mutations = {
            "operation_id": "e" * 64,
            "source_digest": "e" * 64,
            "authority_digest": "e" * 64,
            "execution_identity": "e" * 32,
            "inventory_digest": "e" * 64,
            "inventory_generation": 2,
            "reservation_id": "other-attempt",
            "root_digest": "e" * 64,
            "root_generation": 2,
            "reason": "accepted",
            "request_digest": None,
            "dispatch_digest": None,
            "read_accounting": {
                **grant.attestation["mutation"]["read_accounting"],
                "calls": 19,
            },
        }
        client = self.bridge.client()
        for key, value in mutations.items():
            with self.subTest(key=key):
                changed = copy.deepcopy(grant.attestation)
                changed["mutation"][key] = value
                with self.assertRaises(GitHubBrokerClientError):
                    client.verify_session_grant(grant.grant, changed)
                self.assertEqual(self.bridge.call("grant_count")["result"], 1)
        changed = copy.deepcopy(grant.attestation)
        changed["feedback"]["target_ids"] = ["e" * 64]
        with self.assertRaises(GitHubBrokerClientError):
            client.verify_session_grant(grant.grant, changed)
        self.assertEqual(self.bridge.call("grant_count")["result"], 1)
        self.assert_consumed(grant)

    def test_original_caller_budget_refuses_before_dispatch_without_deadline_reset(
        self,
    ):
        snapshot = {**self.request["mutation"]["read_accounting"], "calls": 59}
        budget = EvidenceReadBudget(snapshot=snapshot)
        original_deadline = budget.wall_deadline_ms
        client = self.bridge.client(before_request=budget.consume)
        assertion = self.oidc(client)
        self.assertEqual(budget.calls, 60)
        with self.assertRaises(GitHubBrokerClientError) as caught:
            client.authorize_session_mutation(
                assertion,
                repository_id=987654321,
                pull_request=7,
                head_sha="a" * 40,
                session_attestation=self.request,
            )
        self.assertIsInstance(caught.exception.__cause__, ReviewInputError)
        self.assertEqual(budget.calls, 60)
        self.assertEqual(budget.wall_deadline_ms, original_deadline)
        self.assertEqual(len(self.trace.attempts()), 1)
        self.assertEqual(self.bridge.call("grant_count")["result"], 0)

    def test_fresh_oidc_required_and_consumed_grant_not_reissued_by_replay(self):
        client = self.bridge.client()
        assertion = self.oidc(client)
        grant = client.authorize_session_mutation(
            assertion,
            repository_id=987654321,
            pull_request=7,
            head_sha="a" * 40,
            session_attestation=self.request,
        )
        self.assert_consumed(grant)
        with self.assertRaises(GitHubBrokerClientError):
            client.authorize_session_mutation(
                assertion,
                repository_id=987654321,
                pull_request=7,
                head_sha="a" * 40,
                session_attestation=self.request,
            )
        self.assertEqual(self.bridge.call("grant_count")["result"], 0)
        self.assert_consumed(self.issue())

    def test_real_parent_kill_before_verification_and_after_committed_consumption(self):
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
                durable_attempts = self.trace.attempts()
                self.assertEqual(
                    sum(
                        item["origin"] == "broker-ledger"
                        and item["action"] == "session_verify"
                        for item in durable_attempts
                    ),
                    0 if before else 1,
                )
                self.bridge.process.kill()
                self.bridge.process.wait(timeout=5)
                self.assertEqual(
                    self.bridge.process.returncode, 1 if os.name == "nt" else -9
                )
                self.assertEqual(self.trace.attempts(), durable_attempts)
                self.bridge.close()
                self.bridge = ProductionBrokerBridge(self.directory, self.trace)
                self.assertEqual(
                    self.bridge.call("grant_count")["result"], 1 if before else 0
                )
                if before:
                    self.assert_consumed(grant)
                else:
                    with self.assertRaises(GitHubBrokerClientError):
                        self.bridge.client().verify_session_grant(
                            grant.grant, grant.attestation
                        )

    def test_consumption_does_not_supply_the_pending_host_source_fence(self):
        grant = self.issue()
        self.bridge.call(
            "set_source", source={"body": "@sensei Changed after issuance."}
        )
        before = self.bridge.internal_dispatches()["transport"]
        self.assert_consumed(grant)
        after = self.bridge.internal_dispatches()["transport"]
        self.assertTrue(
            all(
                item["path"].endswith("/git/ref/tags/v5")
                for item in after[len(before) :]
            )
        )


if __name__ == "__main__":
    program = unittest.main(exit=False)
    artifact = os.getenv("EPIC238_F_MEASUREMENTS")
    if artifact:
        Path(artifact).write_text(json.dumps(MEASUREMENTS, indent=2) + "\n")
    raise SystemExit(not program.result.wasSuccessful())
