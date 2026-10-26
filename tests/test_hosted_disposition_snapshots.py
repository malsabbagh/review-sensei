"""Current command runs with production Worker grants and durable snapshots.

Only external HTTP is synthetic. Each selected run owns one physical caller
allowance; Worker fanout is independently bounded and never predebited.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from review_sensei_github.session_ledger import parse_session_comment

from review_sensei.api_work import ApiRunAccounting
from review_sensei.disposition import session_dispositions
from tests.action_run_worker_qualification import ProductionCommandEntryFixture
from tests.fake_github_http import json_response
from tests.test_action_run import CommandEntryFixture
from tests.test_api_work_persistence import HEAD, IDENTITY


class ProductionDecisionFixture(ProductionCommandEntryFixture):
    def __init__(self, directory, *, limit=512):
        super().__init__(directory, limit=limit)
        self.part_fault = None
        self.snapshot_fault = None
        self.part_posts = 0
        self.snapshot_writes = 0
        self.graphql_refused = False
        self.check_refused = False

    def external(self, request, timeout):
        # The completed initial review is a synthetic external starting state;
        # every subsequent command uses the actual production Worker boundary.
        if not self.command_active:
            return CommandEntryFixture.external(self, request, timeout)
        target = urlsplit(request.full_url)
        path, method = target.path, request.method
        payload = json.loads(request.data) if request.data else {}
        if (
            self.check_refused
            and target.hostname == "broker.github.test"
            and payload.get("capability") == "check_publish"
        ):
            self._meter(request)
            return json_response({"error": "synthetic refusal"}, 403)
        if self.graphql_refused and path == "/graphql":
            self._meter(request)
            return json_response(
                {"errors": [{"type": "FORBIDDEN", "message": "SECRET-server"}]}
            )
        part = (
            method == "POST"
            and payload.get("body", "").startswith(
                "ReviewSensei immutable evidence part v1"
            )
            and self.selected is not None
            and self.selected.ledger._mutation_active
        )
        if part:
            self.part_posts += 1
            fault, self.part_fault = self.part_fault, None
            if fault == "absent":
                self._meter(request)
                raise TimeoutError("synthetic absent part response lost")
            if fault == "concurrent-root":
                response = super().external(request, timeout)
                self.rewrite_record(generation_delta=1)
                return response
            response = super().external(request, timeout)
            if fault == "stored":
                raise TimeoutError("synthetic committed part response lost")
            return response
        if method == "PATCH" and path == "/repos/owner/repo/issues/comments/1000000":
            candidate = parse_session_comment(payload["body"], identity=IDENTITY)
            previous = parse_session_comment(self.comment["body"], identity=IDENTITY)
            if candidate.disposition_snapshot != previous.disposition_snapshot:
                self.snapshot_writes += 1
                response = super().external(request, timeout)
                if self.snapshot_fault == "stored":
                    self.snapshot_fault = None
                    raise TimeoutError("synthetic committed snapshot response lost")
                return response
        return super().external(request, timeout)

    def seed(self):
        self.provider_comments = [
            {
                "path": "src/app.py",
                "line": 2,
                "side": "RIGHT",
                "body": f"Confirm independent caller contract {index}.",
                "severity": "high",
                "blocking": False,
                "needs_human": True,
            }
            for index in range(25)
        ]
        if self.invoke() != 0:
            raise AssertionError(self.error)
        from review_sensei_github.publication import parse_finding_index

        index = parse_finding_index(
            self.reviews[-1]["body"], repository_id=123, pull_request=7, head_sha=HEAD
        )
        self.labels = ["RS-" + entry.instance[:6].upper() for entry in index.entries]
        return self

    def command(self, body, identifier):
        self.SOURCE_ID = identifier
        self.select_command(body, new_run=True)
        return self.invoke_command()

    def override(self, start, identifier):
        return self.command(
            "@reviewsensei override "
            + " ".join(self.labels[start : start + 10])
            + " acceptable risk",
            identifier,
        )

    def decisions(self):
        ledger = self.selected.ledger
        record = ledger.load(IDENTITY).record
        return record, session_dispositions(record, ledger=ledger)


@unittest.skipUnless(
    os.environ.get("REVIEWSENSEI_MEDIA_BROKER_QUALIFICATION") == "1",
    "explicit production broker qualification required",
)
class HostedDecisionSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.f = ProductionDecisionFixture(self.directory.name)
        self.addCleanup(self.f.bridge.close)
        self.f.seed()

    def batches(self):
        for start, event in ((0, 20), (10, 21), (20, 22)):
            self.assertEqual(
                self.f.override(start, event), 0, (self.f.error, self.f.exception)
            )
            self.assertEqual(len(self.f.decisions()[1]), min(start + 10, 25))
            self.assert_accounting()

    def assert_accounting(self):
        f = self.f
        accounting = f.accounted.accounting
        self.assertEqual(len(f.trace), len(f.accounted.observations))
        self.assertEqual(accounting.configuration.limit, f.limit)
        self.assertEqual(
            f.limit,
            accounting.startup.liability
            + accounting.prior_nonstartup_liability
            + accounting.confirmed
            + accounting.uncertain
            + accounting.reserved_undispatched
            + accounting.remaining,
        )
        self.assertTrue(
            all(
                request["session_attestation"]["version"] == 3
                for request in f.broker_requests
            )
        )
        self.assertTrue(
            all(
                receipt["dispatched"] <= receipt["max_dispatches"] <= 60
                for receipt in f.worker_receipts
            )
        )
        self.assertEqual(
            sum(receipt["dispatched"] for receipt in f.worker_receipts),
            len(f.bridge.internal_dispatches()["transport"]),
        )

    def test_twenty_five_audited_decisions_reach_one_native_approval(self):
        self.batches()
        record, decisions = self.f.decisions()
        self.assertIsNotNone(record.disposition_snapshot)
        self.assertEqual(len(decisions), 25)
        self.assertTrue(
            all(d.actor_role == "admin" and d.provenance for d in decisions)
        )
        self.assertEqual(
            self.f.command("@reviewsensei verify", 30),
            0,
            (self.f.error, self.f.exception),
        )
        self.assertEqual(sum(r["state"] == "APPROVED" for r in self.f.reviews), 1)
        self.assertEqual(self.f.check_runs[-1]["conclusion"], "success")
        self.assertEqual(self.f.accounted.accounting.action["phase"], "terminal")
        self.assert_accounting()

    def test_final_override_reports_graphql_failure_without_claiming_approval(self):
        for start, event in ((0, 20), (10, 21)):
            self.assertEqual(self.f.override(start, event), 0, self.f.error)
        self.f.graphql_refused = True
        self.f.override(20, 22)
        self.assertEqual(len(self.f.decisions()[1]), 25)
        self.assertFalse(any(r["state"] == "APPROVED" for r in self.f.reviews))
        self.assertNotIn("SECRET", self.f.stdout + self.f.error)
        self.assertIn("graphql-rejected", self.f.stdout + self.f.error)
        self.assertEqual(self.f.check_runs[-1]["conclusion"], "success")
        self.assert_accounting()

    def test_lost_part_response_does_not_activate_or_invent_decisions(self):
        for start, event in ((0, 20), (10, 21)):
            self.assertEqual(self.f.override(start, event), 0, self.f.error)
        self.f.part_fault = "stored"
        self.assertEqual(self.f.override(20, 22), 1, self.f.error)
        record, decisions = self.f.decisions()
        self.assertEqual(len(decisions), 20)
        self.assertIsNone(record.disposition_snapshot)
        self.assertGreater(self.f.part_posts, 0)
        self.assertTrue(
            any(
                item["body"].startswith("ReviewSensei immutable evidence part v1")
                for item in self.f.extra_comments.values()
            )
        )
        self.assertEqual(self.f.accounted.accounting.action["phase"], "pending")
        self.assert_accounting()
        # An unknown started action cannot become a new allowance/replay.
        before = parse_session_comment(self.f.comment["body"], identity=IDENTITY)
        old = ApiRunAccounting.from_document(before.api_run_accounting)
        writes = self.f.part_posts
        self.f.attempt = 2
        self.assertEqual(self.f.invoke_command(), 1)
        after = parse_session_comment(self.f.comment["body"], identity=IDENTITY)
        self.assertEqual(after.dispositions, before.dispositions)
        self.assertEqual(after.disposition_snapshot, before.disposition_snapshot)
        self.assertEqual(after.transaction, before.transaction)
        state = ApiRunAccounting.from_document(after.api_run_accounting)
        self.assertEqual(state.blocks[: len(old.blocks)], old.blocks)
        self.assertEqual(state.action["phase"], "pending")
        self.assertEqual(self.f.part_posts, writes)

    def test_absent_part_response_never_reposts_or_activates_a_snapshot(self):
        for start, event in ((0, 20), (10, 21)):
            self.assertEqual(self.f.override(start, event), 0, self.f.error)
        self.f.part_fault = "absent"
        self.assertEqual(self.f.override(20, 22), 1, self.f.error)
        record, decisions = self.f.decisions()
        self.assertEqual(len(decisions), 20)
        self.assertIsNone(record.disposition_snapshot)
        self.assertEqual(self.f.part_posts, 1)

        self.assertTrue(
            any(
                observation["mutation"] and observation["outcome"] == "unknown"
                for observation in self.f.accounted.observations
            )
        )
        self.assertEqual(self.f.accounted.accounting.action["phase"], "pending")
        self.assert_accounting()
        self.f.attempt = 2
        self.assertEqual(self.f.invoke_command(), 1)
        after = parse_session_comment(self.f.comment["body"], identity=IDENTITY)
        self.assertEqual(after.dispositions, record.dispositions)
        self.assertIsNone(after.disposition_snapshot)
        self.assertEqual(self.f.part_posts, 1)

    def test_unfunded_part_and_root_tails_refuse_before_part_dispatch(self):
        for start, event in ((0, 20), (10, 21)):
            self.assertEqual(self.f.override(start, event), 0, self.f.error)
        with patch("review_sensei_github.api_run_plan.NATIVE_OPERATION_REQUESTS", 5):
            self.assertEqual(self.f.override(20, 22), 1, self.f.error)
        self.assertIn("part and root tails are unfunded", self.f.error)
        self.assertEqual(self.f.part_posts, 0)
        record, decisions = self.f.decisions()
        self.assertEqual(len(decisions), 20)
        self.assertIsNone(record.disposition_snapshot)
        self.assertEqual(self.f.accounted.accounting.action["phase"], "pending")
        self.assert_accounting()

    def test_lost_root_response_reloads_committed_decisions_without_duplicate(self):
        for start, event in ((0, 20), (10, 21)):
            self.assertEqual(self.f.override(start, event), 0, self.f.error)
        self.f.snapshot_fault = "stored"
        self.assertEqual(self.f.override(20, 22), 0, self.f.error)
        record, decisions = self.f.decisions()
        self.assertEqual(len(decisions), 25)
        self.assertIsNotNone(record.disposition_snapshot)
        writes = self.f.snapshot_writes
        approved = copy.deepcopy(
            [review for review in self.f.reviews if review["state"] == "APPROVED"]
        )
        self.assertEqual(len(approved), 1)
        # A new duplicate override cannot reach past the newer native approval
        # to an older finding index. Its refusal must preserve the stored proof.
        self.assertEqual(self.f.override(20, 23), 1, self.f.error)
        self.assertIn("complete current review index", self.f.error)
        replay, replayed = self.f.decisions()
        self.assertEqual(decisions, replayed)
        self.assertEqual(self.f.snapshot_writes, writes)
        self.assertEqual(
            [review for review in self.f.reviews if review["state"] == "APPROVED"],
            approved,
        )
        self.assertEqual(replay.disposition_snapshot, record.disposition_snapshot)
        self.assert_accounting()

    def test_concurrent_root_change_leaves_orphan_parts_without_decision_authority(
        self,
    ):
        for start, event in ((0, 20), (10, 21)):
            self.assertEqual(self.f.override(start, event), 0, self.f.error)
        self.f.part_fault = "concurrent-root"
        self.assertEqual(self.f.override(20, 22), 1, self.f.error)
        record, decisions = self.f.decisions()
        self.assertEqual(len(decisions), 20)
        self.assertIsNone(record.disposition_snapshot)
        self.assertGreater(self.f.part_posts, 0)
        self.assertFalse(any(r["state"] == "APPROVED" for r in self.f.reviews))
        self.assert_accounting()

    def test_command_reserve_funds_check_and_approval_when_cleanup_refuses(self):
        for start, event in ((0, 20), (10, 21)):
            self.assertEqual(self.f.override(start, event), 0, self.f.error)
        resolved = self.f.resolved_threads.copy()
        # Failure to obtain cleanup authority does not fabricate resolved threads.
        original = self.f.external

        def refused(request, timeout):
            payload = json.loads(request.data) if request.data else {}
            if (
                urlsplit(request.full_url).hostname == "broker.github.test"
                and payload.get("capability") == "thread_resolve"
            ):
                self.f._meter(request)
                return json_response({"error": "synthetic cleanup refusal"}, 403)
            return original(request, timeout)

        self.f.external = refused
        self.f.override(20, 22)
        self.assertEqual(len(self.f.decisions()[1]), 25)
        self.assertEqual(self.f.resolved_threads, resolved)
        self.assertEqual(sum(r["state"] == "APPROVED" for r in self.f.reviews), 1)
        self.assertEqual(self.f.check_runs[-1]["conclusion"], "success")
        self.assertIn("cleanup.capability", self.f.stdout + self.f.error)
        self.assert_accounting()

    def test_refused_deferred_check_preserves_existing_approval_without_new_write(self):
        self.batches()
        self.assertEqual(self.f.command("@reviewsensei verify", 30), 0, self.f.error)
        approved = copy.deepcopy(
            [r for r in self.f.reviews if r["state"] == "APPROVED"]
        )
        self.assertEqual(len(approved), 1)
        self.f.check_refused = True
        self.f.command("@reviewsensei verify", 31)
        self.assertEqual(
            [r for r in self.f.reviews if r["state"] == "APPROVED"], approved
        )
        self.assertEqual(len(self.f.decisions()[1]), 25)
        self.assertIn("check.capability", self.f.stdout + self.f.error)
        self.assert_accounting()
