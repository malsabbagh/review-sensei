from __future__ import annotations

import copy
import hashlib
import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from review_sensei.assessment_queue import (
    AssessmentCheckpoint,
    AssessmentJournal,
    AssessmentQueue,
    AssessmentQueueHostAdapter,
    FactoredAssessmentJournal,
    QueueHostState,
)
from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.models import ProviderResponse
from review_sensei.outcomes import ResourceBudget, ResourceBudgetTracker
from tests.test_assessment_queue import DocumentStore, QueueFixture, fixture
from tests.test_review_work import AssessingProvider


class FactoredHistoryTests(QueueFixture, unittest.TestCase):
    def receipt_fixture(self):
        pending, bundle, queue = fixture(4)
        store = DocumentStore()
        self.run_work(
            pending,
            bundle,
            queue,
            provider=AssessingProvider(),
            checkpoint=store.checkpoint(),
        )
        return queue, store.document

    def test_legacy_migration_and_exact_receipt_round_trip(self):
        queue, receipt = self.receipt_fixture()
        legacy = AssessmentJournal(queue).record(
            queue=queue, source_digest="d" * 64, receipt=receipt
        )
        packed = FactoredAssessmentJournal.from_legacy(legacy)
        loaded = FactoredAssessmentJournal.from_document(packed.to_document())
        self.assertEqual(
            loaded.receipt(source_digest="d" * 64, operation_id="c" * 64), receipt
        )
        self.assertEqual(loaded.to_document()["queue"], legacy.to_document()["queue"])
        with self.assertRaisesRegex(ReviewInputError, "authority changed"):
            loaded.receipt(source_digest="d" * 64, operation_id="e" * 64)
        with self.assertRaises(ReviewInputError):
            AssessmentJournal.from_document(packed.to_document())

    def test_closed_canonical_reference_table_and_no_alias_amplification(self):
        queue, receipt = self.receipt_fixture()
        packed = FactoredAssessmentJournal(
            queue,
            (
                {
                    "source_digest": "d" * 64,
                    "operation_id": "c" * 64,
                    "receipt": receipt,
                },
            ),
        ).to_document()
        for change in (
            lambda value: value.update(extra=True),
            lambda value: value["strings"].append("unused"),
            lambda value: value["strings"].append(value["strings"][0]),
            lambda value: value["operations"].append(-1),
            lambda value: value["operations"].append({"integer": True}),
            lambda value: value["operations"].append({"fields": {}, "extra": 0}),
        ):
            value = copy.deepcopy(packed)
            change(value)
            with self.assertRaises(ReviewInputError):
                FactoredAssessmentJournal.from_document(value)
        value = copy.deepcopy(packed)
        value["strings"][0] = "x" * (2 * 1024 * 1024)
        with self.assertRaisesRegex(ReviewInputError, "aggregate storage"):
            FactoredAssessmentJournal.from_document(value)
        value = copy.deepcopy(packed)
        value["strings"] = ["x" * 50_000]
        value["operations"] = [{"fields": {str(index): 0 for index in range(100)}}]
        with self.assertRaisesRegex(ReviewInputError, "reconstruction bounds"):
            FactoredAssessmentJournal.from_document(value)

    def test_active_admissions_remain_32_and_settled_history_is_separate(self):
        queue, receipt = self.receipt_fixture()
        receipt["completed"] = []
        receipt["counters"]["provider_calls"] = 0
        operations = []
        for index in range(33):
            operation = hashlib.sha256(f"operation:{index}".encode()).hexdigest()
            row = copy.deepcopy(receipt)
            row["operation_id"] = operation
            operations.append(
                {
                    "source_digest": hashlib.sha256(
                        f"source:{index}".encode()
                    ).hexdigest(),
                    "operation_id": operation,
                    "receipt": row,
                }
            )
        FactoredAssessmentJournal(queue, tuple(operations[:32]))
        with self.assertRaisesRegex(ReviewInputError, "active operation bounds"):
            FactoredAssessmentJournal(queue, tuple(operations))

    def test_256_finite_settled_records_do_not_evict_old_authority(self):
        queue, receipt = self.receipt_fixture()
        operations = []
        for index in range(257):
            operation = hashlib.sha256(f"operation:{index}".encode()).hexdigest()
            row = copy.deepcopy(receipt)
            row["operation_id"] = operation
            operations.append(
                {
                    "source_digest": hashlib.sha256(
                        f"source:{index}".encode()
                    ).hexdigest(),
                    "operation_id": operation,
                    "receipt": row,
                }
            )
        journal = FactoredAssessmentJournal(queue, tuple(operations[:256]))
        loaded = FactoredAssessmentJournal.from_document(journal.to_document())
        for row in operations[:256]:
            self.assertEqual(
                loaded.receipt(
                    source_digest=row["source_digest"], operation_id=row["operation_id"]
                ),
                row["receipt"],
            )
        with self.assertRaisesRegex(ReviewInputError, "retained operation bounds"):
            FactoredAssessmentJournal(queue, tuple(operations))

    def profile(self, count, observe=None):
        try:
            pending, bundle, queue = fixture(count)
        except ReviewInputError:
            self.skipTest("requires frozen B complete inventory admission")
        state = [FactoredAssessmentJournal(queue)]
        clock = [datetime(2026, 10, 9, tzinfo=timezone.utc)]
        measurements = []

        class RejectingProvider(AssessingProvider):
            def complete(self, request):
                value = json.loads(super().complete(request).text)
                value["assessments"][0]["diff_evidence"] = "fabricated citation"
                return ProviderResponse(json.dumps(value), self.name)

        def checkpoint(index, operation=None):
            source = hashlib.sha256(f"source:{index}".encode()).hexdigest()
            operation = (
                operation or hashlib.sha256(f"operation:{index}".encode()).hexdigest()
            )

            def write(document):
                current = AssessmentQueue.from_document(state[0].to_document()["queue"])
                state[0] = state[0].record(
                    queue=current, source_digest=source, receipt=document
                )
                if observe is not None:
                    observe(state[0].to_document(), document, index)

            return AssessmentCheckpoint(
                operation_id=operation,
                read=lambda: state[0].receipt(
                    source_digest=source, operation_id=operation
                ),
                write=write,
                now=lambda: clock[0],
            )

        index = 0
        while pending.pending:
            provider = RejectingProvider() if index == 0 else AssessingProvider()
            tracker = ResourceBudgetTracker(ResourceBudget.create(max_provider_calls=1))
            work = self.run_work(
                pending,
                bundle,
                queue,
                provider=provider,
                checkpoint=checkpoint(index),
                tracker=tracker,
            )
            pending, queue = work.apply_to(pending), work.queue
            self.assertEqual(tracker.provider_calls, 1)
            measurements.append(len(canonical_bytes(state[0].to_document())))
            if index == 0:
                before = state[0].to_document()
                replay_provider = RejectingProvider()
                self.run_work(
                    pending,
                    bundle,
                    queue,
                    provider=replay_provider,
                    checkpoint=checkpoint(0),
                    tracker=ResourceBudgetTracker(
                        ResourceBudget.create(max_provider_calls=1)
                    ),
                )
                self.assertEqual(replay_provider.calls, [])
                before = state[0].to_document()
                edited_provider = AssessingProvider()
                with self.assertRaisesRegex(ReviewInputError, "authority changed"):
                    self.run_work(
                        pending,
                        bundle,
                        queue,
                        provider=edited_provider,
                        checkpoint=checkpoint(0, "e" * 64),
                    )
                self.assertEqual(edited_provider.calls, [])
                self.assertEqual(state[0].to_document(), before)
            if index == 32:
                self.assertEqual(len(state[0].to_document()["operations"]), 33)
                self.assertTrue(pending.pending)
            index += 1
        self.assertEqual(index, 1 + (count + 3) // 4)
        loaded = FactoredAssessmentJournal.from_document(state[0].to_document())
        self.assertEqual(
            AssessmentQueue.from_document(loaded.to_document()["queue"]).resolved_ids,
            pending.resolved,
        )
        self.assertLess(max(measurements), 2 * 1024 * 1024)
        self.assertLess(len(canonical_bytes(loaded._operations())), 8 * 1024 * 1024)
        before = state[0].to_document()
        clock[0] += timedelta(hours=7)
        expired_provider = AssessingProvider()
        with self.assertRaisesRegex(ReviewInputError, "expired"):
            self.run_work(
                pending,
                bundle,
                queue,
                provider=expired_provider,
                checkpoint=checkpoint(1),
                tracker=ResourceBudgetTracker(
                    ResourceBudget.create(max_provider_calls=1)
                ),
            )
        self.assertEqual(expired_provider.calls, [])
        self.assertEqual(state[0].to_document(), before)
        return {
            "inventory": count,
            "sources": index,
            "decoded_bytes": len(canonical_bytes(loaded.to_document())),
            "reconstructed_bytes": len(canonical_bytes(loaded._operations())),
            "largest_decoded_bytes": max(measurements),
        }

    def test_real_one_call_100_source_lifecycle(self):
        self.profile(100)

    def test_real_one_call_250_source_lifecycle(self):
        self.profile(250)


class QueueHostAdapterTests(QueueFixture, unittest.TestCase):
    def adapter_fixture(self, *, witness="original-attempt", after=None):
        pending, bundle, queue = fixture(4)
        binding = {"purpose": "queue", "inventory": queue.inventory_digest}
        state = [QueueHostState(None, 0, binding, 0, witness)]
        requests = []

        def activate(request):
            # Synthetic boundary-contract fixture, not actual broker proof.
            self.assertEqual(request.expected_generation, state[0].root_generation)
            self.assertEqual(request.attempt_reservation_id, "original-attempt")
            self.assertEqual(
                request.mutation.root_generation, request.expected_generation
            )
            self.assertEqual(request.item_count, 4)
            self.assertEqual(request.source_digest, "d" * 64)
            self.assertEqual(request.operation_id, "c" * 64)
            self.assertEqual(request.binding, binding)
            self.assertNotIn(
                request.request_identity, [item.request_identity for item in requests]
            )
            requests.append(request)
            state[0] = QueueHostState(
                copy.deepcopy(request.envelope),
                state[0].root_generation + 1,
                binding,
                4,
                "original-attempt",
            )
            return after(state[0]) if after else state[0]

        adapter = AssessmentQueueHostAdapter(
            queue=queue,
            binding=binding,
            source_digest="d" * 64,
            operation_id="c" * 64,
            attempt_reservation_id="original-attempt",
            read=lambda: state[0],
            activate=activate,
        )
        return pending, bundle, queue, state, requests, adapter

    def test_three_exact_mutations_closed_envelope_and_receipt_before_return(self):
        pending, bundle, queue, state, requests, adapter = self.adapter_fixture()
        provider = AssessingProvider()
        result = self.run_work(
            pending, bundle, queue, provider=provider, checkpoint=adapter.checkpoint()
        )
        self.assertEqual(
            [item.mutation.reason for item in requests],
            ["admission", "dispatch", "accepted"],
        )
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(len(result.reply.decisions), 4)
        self.assertEqual(set(state[0].envelope), {"schema_version", "kind", "journal"})
        receipt = FactoredAssessmentJournal.from_document(
            state[0].envelope["journal"]
        ).receipt(source_digest="d" * 64, operation_id="c" * 64)
        self.assertEqual(len(receipt["completed"]), 1)
        self.assertEqual(receipt["response_bytes_reserved"], 0)

    def test_absent_original_witness_refuses_before_provider_or_activation(self):
        pending, bundle, queue, state, requests, adapter = self.adapter_fixture(
            witness=None
        )
        before = state[0]
        provider = AssessingProvider()
        with self.assertRaisesRegex(ReviewInputError, "original attempt witness"):
            self.run_work(
                pending,
                bundle,
                queue,
                provider=provider,
                checkpoint=adapter.checkpoint(),
            )
        self.assertEqual(provider.calls, [])
        self.assertEqual(requests, [])
        self.assertEqual(state[0], before)

    def test_readback_ambiguity_withholds_provider_and_has_no_fallback(self):
        for change in (
            lambda state: replace(state, root_generation=0),
            lambda state: replace(state, attempt_witness=None),
            lambda state: replace(state, envelope=None, item_count=0),
            lambda state: replace(state, binding={"purpose": "foreign"}),
            lambda state: replace(state, item_count=3),
        ):
            pending, bundle, queue, _, requests, adapter = self.adapter_fixture(
                after=change
            )
            provider = AssessingProvider()
            with self.assertRaises(ReviewInputError):
                self.run_work(
                    pending,
                    bundle,
                    queue,
                    provider=provider,
                    checkpoint=adapter.checkpoint(),
                )
            self.assertEqual(provider.calls, [])
            self.assertEqual(len(requests), 1)

    def test_bad_host_read_shape_binding_envelope_or_count_refuses(self):
        for change in (
            lambda state: replace(state, root_generation=True),
            lambda state: replace(state, item_count=1),
            lambda state: replace(state, binding={"purpose": "foreign"}),
            lambda state: replace(state, binding=None),
            lambda state: replace(state, envelope={"kind": "assessment-queue-state"}),
            lambda state: replace(state, attempt_witness="other-attempt"),
        ):
            pending, bundle, queue, state, requests, adapter = self.adapter_fixture()
            state[0] = change(state[0])
            provider = AssessingProvider()
            with self.assertRaises(ReviewInputError):
                self.run_work(
                    pending,
                    bundle,
                    queue,
                    provider=provider,
                    checkpoint=adapter.checkpoint(),
                )
            self.assertEqual(provider.calls, [])
            self.assertEqual(requests, [])

    def test_callback_refusal_propagates_and_preserves_old_root(self):
        pending, bundle, queue, state, requests, adapter = self.adapter_fixture()
        before = state[0]

        def refuse(_request):
            raise ReviewInputError("original control tail is exhausted")

        adapter.activate = refuse
        provider = AssessingProvider()
        with self.assertRaisesRegex(ReviewInputError, "control tail"):
            self.run_work(
                pending,
                bundle,
                queue,
                provider=provider,
                checkpoint=adapter.checkpoint(),
            )
        self.assertEqual(provider.calls, [])
        self.assertEqual(requests, [])
        self.assertEqual(state[0], before)

    def test_same_generation_changed_document_is_not_hidden_by_alias(self):
        pending, bundle, queue, state, _, adapter = self.adapter_fixture()
        work = self.run_work(
            pending,
            bundle,
            queue,
            provider=AssessingProvider(),
            checkpoint=adapter.checkpoint(),
        )
        state[0].envelope["extra"] = "unfenced mutation"
        with self.assertRaises(ReviewInputError):
            self.run_work(
                work.apply_to(pending),
                bundle,
                work.queue,
                provider=AssessingProvider(),
                checkpoint=adapter.checkpoint(),
            )
