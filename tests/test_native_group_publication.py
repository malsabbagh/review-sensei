"""Ledger/physical-transport staging faults; not whole-transaction qualification."""

import copy
import json
import unittest
from urllib.parse import urlsplit

import test_native_review_groups as grouping_fixture
from fake_github_http import json_response
from review_sensei_github.errors import GitHubPublicationTransientError
from review_sensei_github.http import GitHubHttp
from review_sensei_github.native_group_publication import NativeGroupPublication
from review_sensei_github.native_review_groups import NativeInline, NativeReviewPlan
from review_sensei_github.native_union import NativeUnionReceipt
from review_sensei_github.publication import ReviewPublisher
from review_sensei_github.quarantine_capacity import NativeCapacityGuard
from test_native_review_census import NativeCensusBackend
from test_quarantine_capacity import no_reserve
from test_review_transaction import (
    BASE_SHA,
    CONFIGURATION_DIGEST,
    EVIDENCE_DIGEST,
    HEAD_SHA,
    IDENTITY,
    NOW,
    POLICY,
)

from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.diff import DiffAnalysis
from review_sensei.models import ReviewComment, ReviewResult
from review_sensei.placement import HostPlacementFacts
from review_sensei.session import (
    InMemorySessionLedger,
    checkpoint_review_analysis,
    prepare_review_transaction,
)


class GroupWriterBackend(NativeCensusBackend):
    def __init__(self, fault=None):
        self.ledger = InMemorySessionLedger()
        original = ReviewResult(
            summary="Complete.",
            comments=tuple(
                ReviewComment(
                    path="f.py",
                    line=line,
                    body=f"Complete explanation {line}.",
                    blocking=True,
                )
                for line in (1, 2)
            ),
            provider="fixture",
            model="fixture-model",
            review_status="complete",
        )
        transaction = prepare_review_transaction(
            self.ledger,
            IDENTITY,
            POLICY,
            reservation_id="e" * 64,
            base_sha=BASE_SHA,
            head_sha=HEAD_SHA,
            configuration_digest=CONFIGURATION_DIGEST,
            evidence_digest=EVIDENCE_DIGEST,
            now=NOW,
        )
        self.result = checkpoint_review_analysis(
            self.ledger, IDENTITY, transaction, original, now=NOW
        )
        analysis = DiffAnalysis(
            changed_lines={"f.py": frozenset({1, 2})},
            changed_paths=("f.py",),
            diff_bytes=100,
            diff_files=1,
            diff_hunks=1,
            diff_lines=5,
        )
        assembled = ReviewPublisher._assemble_review_body(
            object.__new__(ReviewPublisher),
            result=self.result,
            analysis=analysis,
            convergence_policy=ReviewConvergencePolicy(mode="legacy"),
            read_facts=lambda: HostPlacementFacts(),
            auto_approve=True,
            repository_id=IDENTITY.repository_id,
            pull_request=IDENTITY.pull_request,
            head_sha=HEAD_SHA,
            base_sha=BASE_SHA,
            literal_malformed_fences=True,
        )
        self.plan = NativeReviewPlan.prepare(
            repository_id=IDENTITY.repository_id,
            pull_request=IDENTITY.pull_request,
            base_sha=BASE_SHA,
            head_sha=HEAD_SHA,
            transaction_id=self.result.transaction.transaction_id,
            result_sha256=self.result.transaction.result_sha256,
            items=tuple(
                NativeInline.from_prepared(comment, body, anchor=anchor)
                for comment, _, body, anchor in assembled.inline_comments
            ),
        )
        self.fault = fault
        self.calls, self.reviews, self.group_payloads, self.roots = [], [], [], []
        self.old_rows, self.old_nodes = [], {}
        self.main_id, self.main_body = 2**63 - 1000, ""
        self.post_count = 0
        self.change = None
        self.pending = None
        self.http = GitHubHttp(api_url="https://api.github.test", opener=self.opened)
        # Actual physical guard for native charge retention. The complete-tail
        # admission seam below is synthetic; this is not a public-entry gate.
        self.guard = NativeCapacityGuard(identity=IDENTITY, reserve=no_reserve())
        self.http._quarantine_capacity_guard = self.guard
        self.host = NativeGroupPublication(
            http=self.http,
            ledger=self.ledger,
            identity=IDENTITY,
            transaction=self.result.transaction,
            plan=self.plan,
            token="synthetic",
            producer_id=999,
            admission=lambda plan: self.assert_plan(plan),
            authority_fence=lambda: True,
            now=NOW,
        )

    def assert_plan(self, plan):
        assert plan == self.plan

    def materialize(self):
        assert not self.reviews
        fixture = grouping_fixture.NativeReviewPlanTests()
        response = fixture.response(self.plan, 0)
        self.group_payloads.append(response)
        node = response["data"]["node"]
        self.reviews.append(
            {
                "i": "part:0",
                "n": node["n"],
                "s": "COMMENTED",
                "a": node["a"],
                "c": node["c"],
                "x": {
                    "totalCount": len(node["x"]["nodes"]),
                    "nodes": [{"n": node["x"]["nodes"][0]["n"], "q": None}],
                },
            }
        )
        self.roots.extend(self.root(comment) for comment in node["x"]["nodes"])

    def opened(self, request, timeout):
        if request.get_method() == "POST" and urlsplit(request.full_url).path.endswith(
            "/reviews"
        ):
            self.post_count += 1
            self.pending = json.loads(request.data)
            if self.pending["comments"] == []:
                self.main_body = self.pending["body"]
                if self.fault != "final_absent":
                    self.materialize_final()
                if self.fault in {"final_absent", "final_stored"}:
                    raise TimeoutError("synthetic final acknowledgement unavailable")
                return json_response({"id": self.main_id}, 201)
            assert self.pending == self.plan.part_payload(0)
            if self.fault != "absent":
                self.materialize()
            if self.fault in {"absent", "stored"}:
                raise TimeoutError("synthetic native acknowledgement unavailable")
            return json_response({"id": int(self.reviews[0]["n"])}, 201)
        return super().opened(request, timeout)

    def materialize_final(self):
        self.reviews.append(
            {
                "i": "main",
                "n": str(self.main_id),
                "s": "COMMENTED",
                "a": {"t": "Bot", "n": 999},
                "c": {"o": self.plan.head_sha},
                "x": {"totalCount": 0, "nodes": []},
            }
        )

    @staticmethod
    def body_for_receipt(receipt: NativeUnionReceipt):
        # Exact final-frame construction is independently tested by the v8
        # codec. This component fixture exercises the durable physical writer.
        return json.dumps(receipt.to_document(), sort_keys=True)

    def publish_final(self):
        return self.host.publish_final(
            body_for_receipt=self.body_for_receipt,
            event="COMMENT",
            old_root_nodes={},
        )


class NativeGroupPublicationTests(unittest.TestCase):
    def test_final_frame_reconciles_original_union_without_reposting(self):
        for fault in (None, "final_stored"):
            with self.subTest(fault=fault):
                backend = GroupWriterBackend(fault)
                state, evidence = backend.publish_final()
                self.assertTrue(state.complete)
                self.assertEqual(evidence.main_review_id, backend.main_id)
                self.assertEqual(backend.post_count, 2)
                self.assertLessEqual(evidence.physical_reads, 10)
                charges = copy.deepcopy(backend.guard.stats)
                again, _ = backend.publish_final()
                self.assertEqual(again, state)
                self.assertEqual(backend.post_count, 2)
                self.assertEqual(backend.guard.stats, charges)

    def test_absent_final_never_reposts_and_late_union_can_confirm(self):
        backend = GroupWriterBackend("final_absent")
        for attempt in range(2):
            with (
                self.subTest(attempt=attempt),
                self.assertRaises(GitHubPublicationTransientError),
            ):
                backend.publish_final()
            self.assertEqual(backend.post_count, 2)
        charges = copy.deepcopy(backend.guard.stats)
        backend.materialize_final()
        state, _ = backend.publish_final()
        self.assertTrue(state.complete)
        self.assertEqual(backend.post_count, 2)
        self.assertEqual(backend.guard.stats, charges)

    def test_stored_lost_ack_confirms_the_original_without_another_post(self):
        for fault in (None, "stored"):
            backend = GroupWriterBackend(fault)
            state = backend.host.publish_parts()
            self.assertEqual(backend.post_count, 1)
            self.assertEqual(state.attempted_parts, 1)
            self.assertEqual(len(state.receipts), 1)
            self.assertFalse(state.final_attempted)
            self.assertFalse(state.complete)
            self.assertEqual(backend.guard.stats["reviews"]["count"], 1)
            self.assertEqual(backend.guard.stats["inline_replies"]["count"], 2)

    def test_absence_and_late_materialization_preserve_native_charges_and_plan(self):
        backend = GroupWriterBackend("absent")
        for attempt in range(2):
            with (
                self.subTest(attempt=attempt),
                self.assertRaises(GitHubPublicationTransientError),
            ):
                backend.host.publish_parts()
            self.assertEqual(backend.post_count, 1)
        before = copy.deepcopy(backend.guard.stats)
        retained = backend.ledger.load(IDENTITY, now=NOW).record.publication_staging
        backend.materialize()
        state = backend.host.publish_parts()
        self.assertEqual(backend.post_count, 1)
        self.assertEqual(backend.guard.stats, before)
        self.assertEqual(state.plan_sha256, retained["plan_sha256"])
        self.assertEqual(len(state.receipts), 1)
        self.assertFalse(state.complete)
