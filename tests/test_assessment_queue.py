from __future__ import annotations

import copy
import hashlib
import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from review_sensei.assessment_queue import (
    AssessmentCheckpoint,
    AssessmentJournal,
    AssessmentQueue,
    assessment_operation_id,
    inventory_digest,
)
from review_sensei.budgets import ReviewWorkBudgets
from review_sensei.errors import ProviderError, ReviewInputError
from review_sensei.evidence import EvidenceBundle, EvidenceRecord, EvidenceSnapshot
from review_sensei.execution import CheckpointMutation
from review_sensei.human_assessment import (
    HumanAssessmentService,
    HumanReviewFinding,
    PendingHumanReview,
)
from review_sensei.models import ProviderResponse
from review_sensei.outcomes import ResourceBudget, ResourceBudgetTracker
from review_sensei.reassessment_work import reassess, validate_work
from tests.test_review_work import HUMAN, AssessingProvider, large_patch


def fixture(count=50):
    snapshot = EvidenceSnapshot("owner/repo", 42, "a" * 40, "b" * 40)
    records = tuple(
        EvidenceRecord(f"src/{index:02}.py", large_patch(100), snapshot)
        for index in range(min(count, 32))
    )
    pending = PendingHumanReview(
        snapshot.base_sha,
        tuple(
            HumanReviewFinding(
                hashlib.sha256(f"instance:{index}".encode()).hexdigest(),
                records[index % len(records)].path,
                f"Confirm request handler {index} rejects transmission before authorization.",
            )
            for index in range(count)
        ),
    )
    bundle = EvidenceBundle(snapshot, records)
    return pending, bundle, AssessmentQueue.create(pending, snapshot)


class UnresolvedProvider(AssessingProvider):
    def complete(self, request):
        result = json.loads(super().complete(request).text)
        for decision in result["assessments"]:
            decision["decision"] = "unresolved"
            decision["human_evidence"] = ""
            decision["diff_evidence"] = ""
        return ProviderResponse(json.dumps(result), self.name)


class DocumentStore:
    """Synthetic authenticated CAS fixture; production storage belongs to A."""

    def __init__(self):
        self.document = None
        self.generation = 0
        self.writes = []
        self.after_write = None

    def checkpoint(self, *, now=None, operation_id="c" * 64):
        generation = [self.generation]

        def read():
            generation[0] = self.generation
            return copy.deepcopy(self.document)

        def write(document):
            if generation[0] != self.generation:
                raise ReviewInputError("stale writer")
            self.document = copy.deepcopy(document)
            self.writes.append(copy.deepcopy(document))
            self.generation += 1
            generation[0] = self.generation
            if self.after_write is not None:
                self.after_write(document)

        return AssessmentCheckpoint(
            operation_id=operation_id,
            read=read,
            write=write,
            **({"now": now} if now is not None else {}),
        )


class QueueFixture:
    def run_work(self, pending, bundle, queue, **kwargs):
        if queue is not None and "checkpoint" not in kwargs:
            kwargs["checkpoint"] = DocumentStore().checkpoint()
        return reassess(
            pending=pending,
            bundle=bundle,
            queue=queue,
            source_body=HUMAN,
            authority_digest="f" * 64,
            work_budgets=kwargs.pop("work_budgets", ReviewWorkBudgets(mode="unified")),
            **kwargs,
        )


class AssessmentQueueTests(QueueFixture, unittest.TestCase):
    def test_explicit_late_target_reached_and_complete_inventory_retained(self):
        pending, bundle, queue = fixture()
        late = queue.pending_order[-1]
        provider = AssessingProvider()
        work = self.run_work(pending, bundle, queue, provider=provider, targets=(late,))
        self.assertEqual([item.fingerprint for item in work.reply.decisions], [late])
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(len(work.inventory.findings), 50)
        self.assertEqual(len(work.inventory.apply(work.reply.decisions).pending), 49)
        self.assertEqual(len(work.execution.pending), 49)
        self.assertEqual(
            set(work.execution.pending_ids), set(queue.pending_order) - {late}
        )
        self.assertEqual(work.queue.resolved_ids, (late,))
        self.assertIn("49 pending findings were not assessed", work.reply.body)
        validate_work(
            work,
            pending=pending,
            bundle=bundle,
            source_body=HUMAN,
            authority_digest="f" * 64,
        )

    def test_unknown_foreign_short_duplicate_and_resolved_targets_refuse(self):
        pending, bundle, queue = fixture(4)
        resolved = queue.pending_order[0]
        current = replace(pending, resolved=(resolved,))
        queue = AssessmentQueue.create(current, bundle.snapshot)
        for targets in (
            ("0" * 64,),
            ("RS-" + resolved[:6],),
            (resolved[:12],),
            (resolved,),
            (),
            (queue.pending_order[0],) * 2,
            [queue.pending_order[0]],
        ):
            with self.subTest(targets=targets):
                provider = AssessingProvider()
                with self.assertRaises(ReviewInputError):
                    self.run_work(
                        current, bundle, queue, provider=provider, targets=targets
                    )
                self.assertFalse(provider.calls)

    def test_targeting_without_queue_or_stale_queue_refuses_before_dispatch(self):
        pending, bundle, queue = fixture(4)
        for stale in (
            None,
            replace(queue, inventory_digest="e" * 64),
            replace(queue, snapshot=replace(bundle.snapshot, head_sha="d" * 40)),
        ):
            provider = AssessingProvider()
            with self.assertRaises(ReviewInputError):
                self.run_work(
                    pending,
                    bundle,
                    stale,
                    provider=provider,
                    targets=(queue.pending_order[-1],),
                )
            self.assertFalse(provider.calls)

    def test_fairness_visits_all_fifty_despite_early_unresolved_decisions(self):
        pending, bundle, queue = fixture()
        visited = set()
        counts = []
        for _ in range(2):
            work = self.run_work(pending, bundle, queue, provider=UnresolvedProvider())
            visited.update(item.fingerprint for item in work.reply.decisions)
            counts.append(len(work.reply.decisions))
            self.assertEqual(work.inventory.apply(work.reply.decisions), pending)
            self.assertEqual(len(work.queue.pending_order), 50)
            queue = work.queue
        self.assertEqual(len(visited), 50)
        self.assertEqual(counts, [32, 32])

    def test_semantic_rejections_rotate_without_retry_or_resolution(self):
        pending, bundle, queue = fixture()

        class BadEvidence(AssessingProvider):
            def complete(self, request):
                value = json.loads(super().complete(request).text)
                value["assessments"][0]["human_evidence"] = "not in the human source"
                return ProviderResponse(json.dumps(value), self.name)

        visited = set()
        for _ in range(2):
            provider = BadEvidence()
            work = self.run_work(pending, bundle, queue, provider=provider)
            self.assertEqual(len(provider.calls), 8)
            self.assertFalse(work.reply.decisions)
            self.assertEqual(len(work.queue.pending_order), 50)
            visited.update(work.selected_ids)
            queue = work.queue
        self.assertEqual(len(visited), 50)

    def test_missing_evidence_stays_unvisited_and_does_not_clear_obligations(self):
        pending, bundle, queue = fixture()
        bundle = EvidenceBundle(bundle.snapshot, ())
        visited = set()
        for _ in range(2):
            provider = AssessingProvider()
            work = self.run_work(pending, bundle, queue, provider=provider)
            self.assertFalse(provider.calls)
            self.assertEqual(len(work.queue.pending_order), 50)
            self.assertFalse(work.execution.attempted_ids)
            self.assertEqual(work.queue, queue)
            visited.update(work.selected_ids)
            queue = work.queue
        self.assertFalse(visited)

    def test_unavailable_early_groups_do_not_starve_admitted_late_work(self):
        pending, bundle, queue = fixture()
        early = set(queue.pending_order[:32])
        pending = replace(
            pending,
            findings=tuple(
                replace(item, required_paths=(item.path, "missing.py"))
                if item.fingerprint in early
                else item
                for item in pending.findings
            ),
        )
        queue = AssessmentQueue.create(pending, bundle.snapshot)
        provider = AssessingProvider()
        work = self.run_work(pending, bundle, queue, provider=provider)
        self.assertEqual(len(work.reply.decisions), 18)
        self.assertEqual(set(work.execution.pending_ids), early)
        self.assertEqual(
            {reason for _, reason in work.execution.pending},
            {"required-evidence-missing"},
        )
        self.assertEqual(
            set(work.execution.attempted_ids), set(queue.pending_order) - early
        )
        self.assertEqual(work.queue.pending_order, queue.pending_order[:32])

    def test_hundred_finishes_in_bounded_runs_with_exact_counts(self):
        for count, expected in ((100, [68, 36, 4, 0]),):
            with self.subTest(count=count):
                pending, bundle, queue = fixture(count)
                counts = []
                for _ in expected:
                    provider = AssessingProvider()
                    tracker = ResourceBudgetTracker(ResourceBudget.create())
                    work = self.run_work(
                        pending, bundle, queue, provider=provider, tracker=tracker
                    )
                    self.assertLessEqual(tracker.provider_calls, 8)
                    pending = pending.apply(work.reply.decisions)
                    queue = work.queue
                    counts.append(len(pending.pending))
                self.assertEqual(counts, expected)
                self.assertEqual(len(queue.inventory_ids), count)

    def test_varied_two_fifty_remains_explicit_inventory_admission_blocker(self):
        # B owns large complete inventory persistence. Do not bypass or weaken
        # its current marker preflight just to advertise a full lifecycle pass.
        try:
            pending, _, _ = fixture(250)
        except ReviewInputError as exc:
            self.assertIn("inventory exceeds", str(exc))
        else:
            # B admits complete domain inventories independently, but the old
            # marker writer must still refuse without the partition adapter.
            with self.assertRaises(ReviewInputError):
                pending.to_dict()

    def test_queue_accounts_for_two_fifty_instances_without_storage_claim(self):
        ids = tuple(
            sorted(
                hashlib.sha256(f"instance:{index}".encode()).hexdigest()
                for index in range(250)
            )
        )
        queue = AssessmentQueue(
            EvidenceSnapshot("owner/repo", 42, "a" * 40, "b" * 40), "f" * 64, ids, ids
        )
        counts = []
        while queue.pending_order:
            selected = queue.select(limit=32)
            queue = queue.advance(
                visited=selected, resolved=tuple(sorted(queue.resolved_ids + selected))
            )
            counts.append(len(queue.pending_order))
        self.assertEqual(counts, [218, 186, 154, 122, 90, 58, 26, 0])
        self.assertEqual(len(queue.inventory_ids), 250)

    def test_fully_resolved_queue_replays_finalizer_without_inference(self):
        pending, bundle, _ = fixture(4)
        pending = replace(
            pending, resolved=tuple(item.fingerprint for item in pending.findings)
        )
        queue = AssessmentQueue.create(pending, bundle.snapshot)
        provider = AssessingProvider()
        work = self.run_work(pending, bundle, queue, provider=provider)
        self.assertFalse(provider.calls)
        self.assertFalse(work.reply.decisions)
        self.assertFalse(work.execution.pending)
        self.assertFalse(work.queue.pending_order)

    def test_reconcile_new_findings_preserves_order_and_resolutions(self):
        pending, bundle, queue = fixture(4)
        resolved = queue.pending_order[0]
        queue = queue.advance(visited=(resolved,), resolved=(resolved,))
        new = HumanReviewFinding(
            "0" * 64, pending.findings[0].path, "New independent obligation."
        )
        expanded = replace(
            pending, findings=pending.findings + (new,), resolved=(resolved,)
        )
        joined = queue.reconcile(expanded)
        self.assertEqual(joined.pending_order, queue.pending_order + (new.fingerprint,))
        self.assertEqual(joined.resolved_ids, (resolved,))
        with self.assertRaises(ReviewInputError):
            joined.reconcile(replace(expanded, findings=expanded.findings[1:]))
        with self.assertRaises(ReviewInputError):
            joined.reconcile(replace(expanded, resolved=()))

    def test_queue_roundtrip_and_malformed_inventory_refuse(self):
        _, _, queue = fixture(4)
        self.assertEqual(AssessmentQueue.from_document(queue.to_document()), queue)
        for field, value in (
            ("schema_version", "newer"),
            ("pending_order", []),
            ("resolved_ids", [queue.pending_order[0]]),
            ("inventory_ids", "bad"),
            ("snapshot", {"head_sha": "b" * 40}),
        ):
            document = queue.to_document()
            document[field] = value
            with self.subTest(field=field), self.assertRaises(ReviewInputError):
                AssessmentQueue.from_document(document)

    def test_zero_call_allowance_keeps_unvisited_queue(self):
        pending, bundle, queue = fixture()
        provider = AssessingProvider()
        work = self.run_work(
            pending,
            bundle,
            queue,
            provider=provider,
            tracker=ResourceBudgetTracker(ResourceBudget.create(max_provider_calls=0)),
        )
        self.assertEqual(work.queue, queue)
        self.assertFalse(provider.calls)


class AssessmentCheckpointTests(QueueFixture, unittest.TestCase):
    def test_original_deadline_restores_before_render_and_completed_receipt_reuses(
        self,
    ):
        pending, bundle, queue = fixture(4)
        store = DocumentStore()
        now = [datetime(2026, 10, 10, tzinfo=timezone.utc)]
        self.run_work(
            pending,
            bundle,
            queue,
            provider=AssessingProvider(),
            checkpoint=store.checkpoint(now=lambda: now[0]),
        )
        original = copy.deepcopy(store.document)
        now[0] += timedelta(seconds=121)
        tracker = ResourceBudgetTracker(ResourceBudget.create())
        render = HumanAssessmentService._request
        observed = []

        def deadline_independent_render(**kwargs):
            observed.append((tracker.provider_calls, tracker.elapsed_ms()))
            return render(**kwargs)

        provider = AssessingProvider()
        with patch.object(
            HumanAssessmentService, "_request", side_effect=deadline_independent_render
        ):
            work = self.run_work(
                pending,
                bundle,
                queue,
                provider=provider,
                tracker=tracker,
                checkpoint=store.checkpoint(now=lambda: now[0]),
            )
        # Planner preflight precedes the executor; its bounded render is not a
        # dispatch. The executor's final render sees restored original charges.
        self.assertEqual(observed[-1][0], 1)
        self.assertGreaterEqual(observed[-1][1], 121000)
        self.assertEqual(provider.calls, [])
        self.assertEqual(len(work.reply.decisions), 4)
        self.assertEqual(store.document["deadline_at"], original["deadline_at"])
        self.assertEqual(store.document["counters"], original["counters"])

        tracker = ResourceBudgetTracker(ResourceBudget.create())

        def deadline_sensitive_render(**kwargs):
            if tracker.elapsed_ms() >= tracker.budget.timeout_ms:
                raise ReviewInputError("request timeout expired")
            return render(**kwargs)

        count = len(store.writes)
        with (
            patch.object(
                HumanAssessmentService,
                "_request",
                side_effect=deadline_sensitive_render,
            ),
            self.assertRaisesRegex(
                ReviewInputError, "deadline exceeded before request binding"
            ),
        ):
            self.run_work(
                pending,
                bundle,
                queue,
                provider=provider,
                tracker=tracker,
                checkpoint=store.checkpoint(now=lambda: now[0]),
            )
        self.assertEqual(len(store.writes), count)
        self.assertEqual(provider.calls, [])

    def test_mutations_expose_fenced_generation_and_actual_dispatch_identity(self):
        pending, bundle, queue = fixture(4)
        store = DocumentStore()
        contexts = []
        checkpoint = store.checkpoint()
        legacy_write = checkpoint.write

        def activate(document, context):
            self.assertEqual(context.root_generation, store.generation)
            contexts.append(context)
            legacy_write(document)

        checkpoint.on_mutation = activate
        checkpoint.root_generation = lambda: store.generation

        class CorrectingProvider(AssessingProvider):
            def complete(self, request):
                if not self.calls:
                    self.calls.append(request)
                    return ProviderResponse("malformed", self.name)
                return super().complete(request)

        work = self.run_work(
            pending, bundle, queue, provider=CorrectingProvider(), checkpoint=checkpoint
        )
        self.assertEqual(contexts[0].reason, "admission")
        self.assertEqual(contexts[-1].reason, "accepted")
        dispatches = [item for item in contexts if item.reason == "dispatch"]
        self.assertEqual(len(dispatches), 2)
        self.assertEqual(dispatches[0].request_digest, dispatches[1].request_digest)
        self.assertNotEqual(
            dispatches[0].dispatch_digest, dispatches[1].dispatch_digest
        )
        self.assertEqual(
            [item.root_generation for item in contexts], list(range(len(contexts)))
        )
        self.assertEqual(store.document["counters"]["provider_calls"], 2)
        self.assertNotIn("root_generation", store.document)
        self.assertEqual(len(work.reply.decisions), 4)

        resumed = store.checkpoint()
        resumed.on_mutation = activate
        resumed.root_generation = lambda: store.generation
        original_budget = copy.deepcopy(store.document["resource_budget"])
        provider = CorrectingProvider()
        self.run_work(pending, bundle, queue, provider=provider, checkpoint=resumed)
        self.assertEqual(provider.calls, [])
        self.assertTrue(any(item.reason == "replay" for item in contexts))
        self.assertEqual(store.document["resource_budget"], original_budget)
        self.assertEqual(store.document["counters"]["provider_calls"], 2)

    def test_rejected_mutation_grant_prevents_dispatch_without_legacy_fallback(self):
        pending, bundle, queue = fixture(4)
        store = DocumentStore()
        checkpoint = store.checkpoint()
        write = checkpoint.write

        def activate(document, context):
            if context.reason == "dispatch":
                raise ReviewInputError("fresh one-attempt grant rejected")
            write(document)

        checkpoint.on_mutation = activate
        checkpoint.root_generation = lambda: store.generation
        provider = AssessingProvider()
        with self.assertRaisesRegex(ReviewInputError, "grant rejected"):
            self.run_work(
                pending, bundle, queue, provider=provider, checkpoint=checkpoint
            )
        self.assertEqual(provider.calls, [])
        self.assertEqual(len(store.writes), 1)
        self.assertEqual(store.document["counters"]["provider_calls"], 0)

    def test_mutation_metadata_refuses_invalid_bindings(self):
        for fields in (
            {"reason": "unlimited"},
            {"reason": "dispatch", "root_generation": True},
            {"reason": "dispatch", "dispatch_digest": "short"},
        ):
            with self.subTest(fields=fields), self.assertRaises(ReviewInputError):
                CheckpointMutation(**fields)
        with self.assertRaisesRegex(ReviewInputError, "generation getter"):
            AssessmentCheckpoint(
                operation_id="a" * 64,
                read=lambda: None,
                write=lambda value: None,
                on_mutation=lambda value, context: None,
            )

    def test_crash_after_response_before_checkpoint_restores_conservative_reservation(
        self,
    ):
        pending, bundle, queue = fixture(8)
        store = DocumentStore()
        checkpoint = store.checkpoint()
        original_write = checkpoint.write

        def kill_before_response_receipt(document):
            if document["counters"]["response_bytes"]:
                raise KeyboardInterrupt()
            original_write(document)

        checkpoint.write = kill_before_response_receipt
        policy = ReviewWorkBudgets(mode="unified", max_total_output_bytes=16 * 1024)
        with self.assertRaises(KeyboardInterrupt):
            self.run_work(
                pending,
                bundle,
                queue,
                provider=AssessingProvider(),
                checkpoint=checkpoint,
                work_budgets=policy,
            )
        self.assertEqual(store.document["counters"]["response_bytes"], 0)
        self.assertEqual(store.document["response_bytes_reserved"], 16 * 1024)
        provider = AssessingProvider()
        tracker = ResourceBudgetTracker(ResourceBudget.create())
        work = self.run_work(
            pending,
            bundle,
            queue,
            provider=provider,
            tracker=tracker,
            checkpoint=store.checkpoint(),
            work_budgets=policy,
        )
        self.assertFalse(provider.calls)
        self.assertEqual(tracker.provider_calls, 1)
        self.assertEqual(tracker.response_bytes, 0)
        self.assertEqual(work.execution.response_bytes_reserved, 16 * 1024)
        self.assertIn("output_budget", {reason for _, reason in work.execution.pending})
        self.assertFalse(work.reply.decisions)

    def test_known_response_atomically_replaces_reservation_with_actual_bytes(self):
        pending, bundle, queue = fixture(4)
        store = DocumentStore()
        tracker = ResourceBudgetTracker(ResourceBudget.create())
        work = self.run_work(
            pending,
            bundle,
            queue,
            provider=AssessingProvider(),
            tracker=tracker,
            checkpoint=store.checkpoint(),
        )
        self.assertTrue(
            any(
                value["response_bytes_reserved"] == 16 * 1024
                and value["counters"]["response_bytes"] == 0
                for value in store.writes
            )
        )
        self.assertEqual(store.document["response_bytes_reserved"], 0)
        self.assertEqual(
            store.document["counters"]["response_bytes"], tracker.response_bytes
        )
        self.assertGreater(tracker.response_bytes, 0)
        self.assertEqual(work.execution.response_bytes_reserved, 0)
        self.assertEqual(len(store.writes), 3)
        self.assertTrue(store.writes[-1]["completed"])
        self.assertFalse(store.writes[-1]["pending"])
        self.assertTrue(
            all(
                value["completed"]
                for value in store.writes
                if value["counters"]["response_bytes"] > 0
            )
        )

    def test_single_target_complete_receipt_has_three_saves_and_retains_scoped_out_work(
        self,
    ):
        pending, bundle, queue = fixture(100)
        store = DocumentStore()
        work = self.run_work(
            pending,
            bundle,
            queue,
            targets=(queue.pending_order[-1],),
            provider=AssessingProvider(),
            checkpoint=store.checkpoint(),
        )
        self.assertEqual(len(store.writes), 3)
        receipt = store.writes[-1]
        self.assertEqual(len(receipt["completed"]), 1)
        self.assertEqual(len(receipt["pending"]), 99)
        self.assertEqual(receipt["response_bytes_reserved"], 0)
        self.assertEqual(
            set(work.execution.pending_ids),
            set(queue.pending_order) - {queue.pending_order[-1]},
        )

    def test_one_call_two_fifty_profile_refuses_registry_before_satisfying_inventory(
        self,
    ):
        try:
            pending, bundle, queue = fixture(250)
        except ReviewInputError:
            self.skipTest("requires frozen B complete inventory admission")
        state = [AssessmentJournal(queue)]

        class RejectingProvider(AssessingProvider):
            def complete(self, request):
                value = json.loads(super().complete(request).text)
                value["assessments"][0]["diff_evidence"] = "fabricated citation"
                return ProviderResponse(json.dumps(value), self.name)

        for index in range(1, 34):
            source = hashlib.sha256(f"source:{index}".encode()).hexdigest()
            operation = hashlib.sha256(f"operation:{index}".encode()).hexdigest()

            def write(document):
                current = AssessmentQueue.from_document(state[0].to_document()["queue"])
                state[0] = state[0].record(
                    queue=current, source_digest=source, receipt=document
                )

            checkpoint = AssessmentCheckpoint(
                operation_id=operation,
                read=lambda: state[0].receipt(
                    source_digest=source, operation_id=operation
                ),
                write=write,
            )
            tracker = ResourceBudgetTracker(ResourceBudget.create(max_provider_calls=1))
            provider = RejectingProvider() if index == 1 else AssessingProvider()
            before = state[0].to_document()
            if index == 33:
                with self.assertRaisesRegex(
                    ReviewInputError, "retained operation bounds"
                ):
                    self.run_work(
                        pending,
                        bundle,
                        queue,
                        provider=provider,
                        checkpoint=checkpoint,
                        tracker=tracker,
                    )
                self.assertEqual(provider.calls, [])
                self.assertEqual(state[0].to_document(), before)
                break
            work = self.run_work(
                pending,
                bundle,
                queue,
                provider=provider,
                checkpoint=checkpoint,
                tracker=tracker,
            )
            pending, queue = work.apply_to(pending), work.queue
            if index == 1:
                replay_provider = RejectingProvider()
                replay = self.run_work(
                    pending,
                    bundle,
                    queue,
                    provider=replay_provider,
                    checkpoint=checkpoint,
                    tracker=ResourceBudgetTracker(
                        ResourceBudget.create(max_provider_calls=1)
                    ),
                )
                self.assertEqual(replay_provider.calls, [])
                self.assertEqual(replay.reply.decisions, ())
        self.assertEqual(len(pending.pending), 126)
        self.assertEqual(len(state[0].to_document()["operations"]), 32)
        self.assertLess(
            len(json.dumps(state[0].to_document()).encode()), 2 * 1024 * 1024
        )

    def test_admitted_two_fifty_rejection_replay_and_new_sources_fit_bounded_journal(
        self,
    ):
        try:
            pending, bundle, queue = fixture(250)
        except ReviewInputError:
            self.skipTest(
                "B complete 250-instance inventory admission is not integrated on main"
            )
        state = [AssessmentJournal(queue)]

        def checkpoint(index):
            source = hashlib.sha256(f"source:{index}".encode()).hexdigest()
            operation = hashlib.sha256(f"operation:{index}".encode()).hexdigest()

            def write(document):
                current = AssessmentQueue.from_document(state[0].to_document()["queue"])
                state[0] = state[0].record(
                    queue=current, source_digest=source, receipt=document
                )

            return AssessmentCheckpoint(
                operation_id=operation,
                read=lambda: state[0].receipt(
                    source_digest=source, operation_id=operation
                ),
                write=write,
            )

        class RejectingProvider(AssessingProvider):
            def complete(self, request):
                result = json.loads(super().complete(request).text)
                result["assessments"][0]["human_evidence"] = (
                    "unsupported source citation"
                )
                return ProviderResponse(json.dumps(result), self.name)

        rejected = self.run_work(
            pending,
            bundle,
            queue,
            provider=RejectingProvider(),
            checkpoint=checkpoint(0),
        )
        provider = RejectingProvider()
        self.run_work(
            pending, bundle, rejected.queue, provider=provider, checkpoint=checkpoint(0)
        )
        self.assertFalse(provider.calls)
        queue = AssessmentQueue.from_document(state[0].to_document()["queue"])
        counts = []
        for index in range(1, 9):
            work = self.run_work(
                pending,
                bundle,
                queue,
                provider=AssessingProvider(),
                checkpoint=checkpoint(index),
            )
            pending = work.apply_to(pending)
            queue = AssessmentQueue.from_document(state[0].to_document()["queue"])
            self.assertEqual(queue.resolved_ids, pending.resolved)
            counts.append(len(pending.pending))
        self.assertEqual(counts, [218, 186, 154, 122, 90, 58, 26, 0])
        self.assertEqual(len(state[0].to_document()["operations"]), 9)

    def test_journal_charge_and_accepted_resolution_activate_with_receipt(self):
        pending, bundle, queue = fixture(8)
        state = [AssessmentJournal(queue)]
        source = "a" * 64

        def write(document):
            current_queue = AssessmentQueue.from_document(
                state[0].to_document()["queue"]
            )
            state[0] = state[0].record(
                queue=current_queue, source_digest=source, receipt=document
            )

        checkpoint = AssessmentCheckpoint(
            operation_id="c" * 64,
            read=lambda: state[0].receipt(source_digest=source, operation_id="c" * 64),
            write=write,
        )
        self_case = self

        class JournalProvider(AssessingProvider):
            def complete(self, request):
                current = AssessmentQueue.from_document(state[0].to_document()["queue"])
                receipt = state[0].receipt(source_digest=source, operation_id="c" * 64)
                self_case.assertTrue(receipt["attempted_ids"])
                self_case.assertNotEqual(current.pending_order, queue.pending_order)
                return super().complete(request)

        work = self.run_work(
            pending, bundle, queue, provider=JournalProvider(), checkpoint=checkpoint
        )
        current = AssessmentQueue.from_document(state[0].to_document()["queue"])
        self.assertEqual(current.resolved_ids, work.queue.resolved_ids)
        self.assertEqual(len(current.resolved_ids), 8)

    def test_journal_updates_cannot_refund_charges_or_lose_accepted_results(self):
        pending, bundle, queue = fixture(4)
        store = DocumentStore()
        work = self.run_work(
            pending,
            bundle,
            queue,
            provider=AssessingProvider(),
            checkpoint=store.checkpoint(),
        )
        journal = AssessmentJournal(queue).record(
            queue=work.queue, source_digest="a" * 64, receipt=store.document
        )
        for field, value in (
            ("counters", {**store.document["counters"], "provider_calls": 0}),
            ("completed", []),
            ("attempted_ids", []),
            ("created_at", "changed"),
        ):
            with self.subTest(field=field), self.assertRaises(ReviewInputError):
                journal.record(
                    queue=work.queue,
                    source_digest="a" * 64,
                    receipt={**store.document, field: value},
                )

    def test_clock_regression_between_checkpoints_fails_before_receipt_activation(self):
        pending, bundle, queue = fixture(4)
        origin = datetime(2026, 10, 10, tzinfo=timezone.utc)
        clock = iter(
            (origin, origin + timedelta(seconds=5), origin + timedelta(seconds=4))
        )
        store = DocumentStore()
        with self.assertRaisesRegex(ReviewInputError, "clock regressed"):
            self.run_work(
                pending,
                bundle,
                queue,
                provider=AssessingProvider(),
                tracker=ResourceBudgetTracker(
                    ResourceBudget.create(), monotonic=lambda: 0.0
                ),
                checkpoint=store.checkpoint(now=lambda: next(clock)),
            )
        self.assertEqual(store.document["counters"]["provider_calls"], 1)
        self.assertFalse(store.document["completed"])

    def test_journal_replay_registry_refuses_changed_source_binding_and_eviction(self):
        pending, bundle, queue = fixture(4)
        store = DocumentStore()
        work = self.run_work(
            pending,
            bundle,
            queue,
            provider=AssessingProvider(),
            checkpoint=store.checkpoint(),
        )
        journal = AssessmentJournal(queue)
        source = "a" * 64
        journal = journal.record(
            queue=work.queue, source_digest=source, receipt=store.document
        )
        self.assertEqual(
            AssessmentJournal.from_document(journal.to_document()).to_document(),
            journal.to_document(),
        )
        self.assertEqual(
            journal.receipt(source_digest=source, operation_id="c" * 64), store.document
        )
        with self.assertRaisesRegex(ReviewInputError, "source authority changed"):
            journal.receipt(source_digest=source, operation_id="d" * 64)
        returned = journal.to_document()
        returned["operations"].clear()
        self.assertEqual(len(journal.to_document()["operations"]), 1)
        for index in range(1, 32):
            receipt = {**store.document, "operation_id": f"{index:064x}"}
            journal = journal.record(
                queue=work.queue, source_digest=f"{index:064x}", receipt=receipt
            )
        with self.assertRaisesRegex(ReviewInputError, "retained operation bounds"):
            journal.record(
                queue=work.queue,
                source_digest="f" * 64,
                receipt={**store.document, "operation_id": "f" * 64},
            )
        with self.assertRaisesRegex(ReviewInputError, "loses obligations"):
            journal.record(queue=queue, source_digest=source, receipt=store.document)

    def test_journal_malformed_registry_and_aggregate_growth_refuse(self):
        _, _, queue = fixture(4)
        for operations in (
            ({"source_digest": "bad", "operation_id": "f" * 64, "receipt": {}},),
            (
                dict(
                    source_digest="a" * 64,
                    operation_id="b" * 64,
                    receipt={"operation_id": "c" * 64},
                ),
            ),
            (
                dict(
                    source_digest="a" * 64,
                    operation_id="b" * 64,
                    receipt={"operation_id": "b" * 64},
                ),
            )
            * 2,
        ):
            with (
                self.subTest(operations=operations),
                self.assertRaises(ReviewInputError),
            ):
                AssessmentJournal(queue, operations)
        with self.assertRaisesRegex(ReviewInputError, "aggregate storage"):
            AssessmentJournal(
                queue,
                (
                    {
                        "source_digest": "a" * 64,
                        "operation_id": "b" * 64,
                        "receipt": {
                            "operation_id": "b" * 64,
                            "oversized": "x" * (2 * 1024 * 1024),
                        },
                    },
                ),
            )
        value = AssessmentJournal(queue).to_document()
        value["extra"] = "unknown"
        with self.assertRaises(ReviewInputError):
            AssessmentJournal.from_document(value)

    def test_replay_after_target_resolved_returns_receipt_without_new_calls(self):
        pending, bundle, queue = fixture(8)
        target = queue.pending_order[-1]
        store = DocumentStore()
        first = self.run_work(
            pending,
            bundle,
            queue,
            provider=AssessingProvider(),
            targets=(target,),
            checkpoint=store.checkpoint(),
        )
        latest = first.apply_to(pending)
        provider = AssessingProvider()
        tracker = ResourceBudgetTracker(ResourceBudget.create())
        replay = self.run_work(
            latest,
            bundle,
            first.queue,
            provider=provider,
            targets=(target,),
            tracker=tracker,
            checkpoint=store.checkpoint(),
        )
        self.assertFalse(provider.calls)
        self.assertEqual(tracker.provider_calls, 1)
        self.assertEqual(replay.reply.decisions, first.reply.decisions)
        self.assertEqual(replay.apply_to(latest), latest)
        self.assertEqual(replay.queue.resolved_ids, latest.resolved)
        self.assertIn("0 addressed", replay.reply.body)

    def test_replay_unions_concurrent_resolution_and_receipt_without_reopening(self):
        pending, bundle, queue = fixture(8)
        target = queue.pending_order[-1]
        store = DocumentStore()
        first = self.run_work(
            pending,
            bundle,
            queue,
            provider=AssessingProvider(),
            targets=(target,),
            checkpoint=store.checkpoint(),
        )
        other = queue.pending_order[0]
        latest = replace(
            first.apply_to(pending), resolved=tuple(sorted((target, other)))
        )
        latest_queue = AssessmentQueue.create(latest, bundle.snapshot)
        provider = AssessingProvider()
        replay = self.run_work(
            latest,
            bundle,
            latest_queue,
            provider=provider,
            targets=(target,),
            checkpoint=store.checkpoint(),
        )
        self.assertFalse(provider.calls)
        self.assertEqual(replay.apply_to(latest), latest)
        self.assertEqual(replay.queue.resolved_ids, latest.resolved)

    def test_receipt_admission_is_persisted_before_first_dispatch(self):
        pending, bundle, queue = fixture(4)
        store = DocumentStore()

        class ObservingProvider(AssessingProvider):
            def complete(self, request):
                document = store.document
                self_case.assertEqual(
                    document["counters"]["provider_calls"], len(self.calls) + 1
                )
                self_case.assertEqual(
                    set(document["attempted_ids"]), set(queue.pending_order)
                )
                self_case.assertEqual(document["admitted_queue"], queue.to_document())
                return super().complete(request)

        self_case = self
        self.run_work(
            pending,
            bundle,
            queue,
            provider=ObservingProvider(),
            checkpoint=store.checkpoint(),
        )
        self.assertEqual(store.writes[0]["counters"]["provider_calls"], 0)
        self.assertFalse(store.writes[0]["attempted_ids"])

    def test_restart_after_dispatch_retains_charge_and_never_replays_unknown(self):
        pending, bundle, queue = fixture(4)
        store = DocumentStore()

        class KilledProvider(AssessingProvider):
            def complete(self, request):
                super().complete(request)
                raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            self.run_work(
                pending,
                bundle,
                queue,
                provider=KilledProvider(),
                checkpoint=store.checkpoint(),
            )
        self.assertEqual(store.document["counters"]["provider_calls"], 1)
        self.assertEqual(
            {reason for _, reason in store.document["pending"]},
            {"dispatch-outcome-unknown"},
        )
        # Provider identity is bound by request digests. Resume must use the same
        # configured adapter class, rather than switching models or providers.
        tracker = ResourceBudgetTracker(ResourceBudget.create())
        provider = KilledProvider()
        work = self.run_work(
            pending,
            bundle,
            queue,
            provider=provider,
            tracker=tracker,
            checkpoint=store.checkpoint(),
        )
        self.assertFalse(provider.calls)
        self.assertEqual(tracker.provider_calls, 1)
        self.assertFalse(work.reply.decisions)
        self.assertEqual(len(work.queue.pending_order), 4)

    def test_restart_after_accepted_decision_reuses_receipt_and_original_budget(self):
        pending, bundle, queue = fixture(8)
        store = DocumentStore()
        tracker = ResourceBudgetTracker(ResourceBudget.create(max_provider_calls=1))

        def kill_after_accept(document):
            if document["completed"]:
                raise KeyboardInterrupt()

        store.after_write = kill_after_accept
        with self.assertRaises(KeyboardInterrupt):
            self.run_work(
                pending,
                bundle,
                queue,
                provider=AssessingProvider(),
                tracker=tracker,
                checkpoint=store.checkpoint(),
            )
        store.after_write = None
        self.assertEqual(len(store.document["completed"]), 1)
        provider = AssessingProvider()
        restarted = ResourceBudgetTracker(ResourceBudget.create(max_provider_calls=1))
        work = self.run_work(
            pending,
            bundle,
            queue,
            provider=provider,
            tracker=restarted,
            checkpoint=store.checkpoint(),
        )
        self.assertFalse(provider.calls)
        self.assertEqual(restarted.provider_calls, 1)
        self.assertEqual(len(work.reply.decisions), 4)
        self.assertEqual(len(work.queue.pending_order), 4)
        self.assertEqual(
            work.inventory.apply(work.reply.decisions).resolved, work.queue.resolved_ids
        )

    def test_semantic_terminal_receipt_cannot_gain_retry_on_restart(self):
        pending, bundle, queue = fixture(4)
        store = DocumentStore()

        class BadProvider(AssessingProvider):
            def complete(self, request):
                response = json.loads(super().complete(request).text)
                response["assessments"][0]["diff_evidence"] = "invented citation"
                return ProviderResponse(json.dumps(response), self.name)

        first = self.run_work(
            pending,
            bundle,
            queue,
            provider=BadProvider(),
            checkpoint=store.checkpoint(),
        )
        self.assertFalse(first.reply.decisions)
        provider = BadProvider()
        tracker = ResourceBudgetTracker(ResourceBudget.create())
        self.run_work(
            pending,
            bundle,
            queue,
            provider=provider,
            tracker=tracker,
            checkpoint=store.checkpoint(),
        )
        self.assertFalse(provider.calls)
        self.assertEqual(tracker.provider_calls, 1)

    def test_transport_and_structural_retries_checkpoint_existing_counters(self):
        pending, bundle, queue = fixture(4)
        for mode in ("transport", "structural"):
            with self.subTest(mode=mode):
                store = DocumentStore()

                class RetryProvider(AssessingProvider):
                    def complete(self, request):
                        if not self.calls:
                            self.calls.append(request)
                            if mode == "transport":
                                raise ProviderError("temporary", transient=True)
                            return ProviderResponse("malformed", self.name)
                        return super().complete(request)

                tracker = ResourceBudgetTracker(ResourceBudget.create())
                work = self.run_work(
                    pending,
                    bundle,
                    queue,
                    provider=RetryProvider(),
                    tracker=tracker,
                    checkpoint=store.checkpoint(),
                )
                self.assertEqual(tracker.provider_calls, 2)
                self.assertEqual(len(work.reply.decisions), 4)
                counters = store.document["counters"]
                self.assertEqual(counters["provider_calls"], 2)
                self.assertEqual(
                    counters["transport_retries"]
                    if mode == "transport"
                    else counters["structural_retries"],
                    1,
                )
                self.assertTrue(
                    any(
                        document["counters"]["provider_calls"] == 1
                        and document["counters"]["response_bytes"] > 0
                        for document in store.writes
                    )
                    if mode == "structural"
                    else True
                )

    def test_absolute_deadline_and_expiry_survive_new_process_clocks(self):
        pending, bundle, queue = fixture(8)
        now = [datetime(2026, 10, 10, tzinfo=timezone.utc)]
        store = DocumentStore()

        def kill_before_dispatch(document):
            if document["counters"]["provider_calls"]:
                raise KeyboardInterrupt()

        store.after_write = kill_before_dispatch
        with self.assertRaises(KeyboardInterrupt):
            self.run_work(
                pending,
                bundle,
                queue,
                provider=AssessingProvider(),
                checkpoint=store.checkpoint(now=lambda: now[0]),
            )
        deadline = store.document["deadline_at"]
        expiry = store.document["expires_at"]
        store.after_write = None
        now[0] += timedelta(seconds=121)
        provider = AssessingProvider()
        tracker = ResourceBudgetTracker(ResourceBudget.create(), monotonic=lambda: 5.0)
        work = self.run_work(
            pending,
            bundle,
            queue,
            provider=provider,
            tracker=tracker,
            checkpoint=store.checkpoint(now=lambda: now[0]),
        )
        self.assertFalse(provider.calls)
        self.assertEqual(tracker.provider_calls, 1)
        self.assertTrue(
            any(reason == "deadline_exceeded" for _, reason in work.execution.pending)
        )
        self.assertEqual(store.document["deadline_at"], deadline)
        self.assertEqual(store.document["expires_at"], expiry)
        now[0] += timedelta(days=1)
        with self.assertRaises(ReviewInputError):
            self.run_work(
                pending,
                bundle,
                queue,
                provider=AssessingProvider(),
                checkpoint=store.checkpoint(now=lambda: now[0]),
            )

    def test_changed_authority_source_evidence_policy_model_refuses_reuse(self):
        pending, bundle, queue = fixture(4)
        store = DocumentStore()
        self.run_work(
            pending,
            bundle,
            queue,
            provider=AssessingProvider(),
            checkpoint=store.checkpoint(),
        )
        original = copy.deepcopy(store.document)
        mutations = [
            {"operation_id": "0" * 64},
            {"plan_id": "0" * 64},
            {"execution_identity": "invalid"},
            {"elapsed_ms": True},
            {"counters": {"provider_calls": 0}},
            {"expires_at": "not a date"},
            {"completed": []},
            {"extra": "unknown"},
        ]
        for mutation in mutations:
            store.document = {**copy.deepcopy(original), **mutation}
            provider = AssessingProvider()
            with self.subTest(mutation=mutation), self.assertRaises(ReviewInputError):
                self.run_work(
                    pending,
                    bundle,
                    queue,
                    provider=provider,
                    checkpoint=store.checkpoint(),
                )
            self.assertFalse(provider.calls)
        store.document = original
        with self.assertRaises(ReviewInputError):
            reassess(
                pending=pending,
                bundle=bundle,
                queue=queue,
                provider=AssessingProvider(),
                source_body=HUMAN + " Changed.",
                authority_digest="f" * 64,
                work_budgets=ReviewWorkBudgets(mode="unified"),
                checkpoint=store.checkpoint(),
            )

    def test_stale_writer_failure_stops_before_provider_dispatch(self):
        pending, bundle, queue = fixture(4)
        store = DocumentStore()
        checkpoint = store.checkpoint()
        original_write = checkpoint.write

        def write(document):
            if document["counters"]["provider_calls"]:
                store.generation += 1
            original_write(document)

        checkpoint.write = write
        provider = AssessingProvider()
        with self.assertRaisesRegex(ReviewInputError, "stale writer"):
            self.run_work(
                pending, bundle, queue, provider=provider, checkpoint=checkpoint
            )
        self.assertFalse(provider.calls)

    def test_operation_binds_every_identity_and_replay_is_stable(self):
        pending, bundle, queue = fixture(4)
        arguments = dict(
            snapshot=bundle.snapshot,
            inventory=inventory_digest(pending),
            authority="a" * 64,
            source="b" * 64,
            provider="c" * 64,
            policy="d" * 64,
            evidence=bundle.digest,
            targets=None,
        )
        original = assessment_operation_id(**arguments)
        self.assertEqual(original, assessment_operation_id(**arguments))
        for field in (
            "inventory",
            "authority",
            "source",
            "provider",
            "policy",
            "evidence",
        ):
            with self.subTest(field=field):
                self.assertNotEqual(
                    original, assessment_operation_id(**{**arguments, field: "e" * 64})
                )
        self.assertNotEqual(
            original,
            assessment_operation_id(
                **{**arguments, "targets": (queue.pending_order[0],)}
            ),
        )
        self.assertNotEqual(
            original,
            assessment_operation_id(
                **{**arguments, "snapshot": replace(bundle.snapshot, head_sha="c" * 40)}
            ),
        )


if __name__ == "__main__":
    unittest.main()
