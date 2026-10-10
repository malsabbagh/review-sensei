from __future__ import annotations

import copy
import hashlib
import json
import random
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from review_sensei import bounded_evidence as codec
from review_sensei.assessment_history_index import (
    HistoryPartReference,
    IndexedAssessmentJournal,
    history_envelope,
    resolve_history_part,
)
from review_sensei.assessment_queue import (
    AssessmentCheckpoint,
    AssessmentJournal,
    AssessmentQueue,
    FactoredAssessmentJournal,
    QueueHostState,
)
from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.evidence import EvidenceBundle, EvidenceRecord
from review_sensei.human_assessment import PendingHumanReview
from review_sensei.models import ProviderResponse
from review_sensei.outcomes import ResourceBudget, ResourceBudgetTracker
from tests.test_assessment_queue import QueueFixture, fixture
from tests.test_review_work import AssessingProvider, large_patch


class IndexedHistoryTests(QueueFixture, unittest.TestCase):
    def codec(self):
        if not callable(getattr(codec, "partition_evidence", None)):
            self.skipTest("requires frozen A authenticated partition codec")
        return codec

    def profile(
        self, count=250, *, observer=None, full_feedback=False, frozen_inventory=False
    ):
        c = self.codec()
        try:
            pending, bundle, _ = fixture(count)
            pending = PendingHumanReview(
                pending.base_sha,
                tuple(
                    replace(
                        f,
                        body=f'{f.body} Unicode 深🙂 \\" quoted={random.Random(index).randbytes(48).hex()}',
                    )
                    for index, f in enumerate(pending.findings)
                ),
            )
            if frozen_inventory:
                from tests.test_evidence_capacity import human_result, varied_comments

                comments = tuple(
                    replace(
                        item, body=item.body + ' Unicode 深/🙂/é \\" exact evidence.'
                    )
                    for item in varied_comments(count, detail=3, unique_paths=count)
                )
                pending = PendingHumanReview.from_result(
                    human_result(comments), bundle.snapshot.base_sha
                )
                bundle = EvidenceBundle(
                    bundle.snapshot,
                    tuple(
                        EvidenceRecord(item.path, large_patch(100), bundle.snapshot)
                        for item in comments
                    ),
                )
        except ReviewInputError:
            self.skipTest("requires frozen B complete inventory admission")
        queue = AssessmentQueue.create(pending, bundle.snapshot)
        binding = {
            "repository": "owner/repo",
            "repository_id": None,
            "pull_request": 42,
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
            "policy_digest": "f" * 64,
            "configuration_digest": "e" * 64,
            "generation": 0,
            "producer": "local-ledger",
            "purpose": "queue",
            "schema_version": "1.0",
        }
        store = {}
        root = [IndexedAssessmentJournal(queue)]
        reads = []
        phases = []
        budgets = {}
        feedbacks = {}
        clock = [datetime(2026, 10, 9, tzinfo=timezone.utc)]

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
            # Same source reuses its original control object, never a refill.
            if source not in budgets:
                budgets[source] = c.EvidenceReadBudget()
            control = budgets[source]

            def reader(reference):
                def fetch(storage_id):
                    control.consume()
                    reads.append(storage_id)
                    if storage_id not in store:
                        raise ReviewInputError("missing authenticated history part")
                    return c.AuthenticatedPart(
                        copy.deepcopy(store[storage_id]), binding["producer"]
                    )

                return resolve_history_part(
                    reference,
                    reader=fetch,
                    expected_binding=binding,
                    budget=control,
                    item_count=count,
                )

            def latest():
                root[0] = IndexedAssessmentJournal.from_document(
                    root[0].to_document(),
                    current_reference=root[0].current_reference,
                    current_read_accounting=root[0].current_read_accounting,
                    reader=reader,
                )
                return root[0].receipt(source_digest=source, operation_id=operation)

            def write(receipt):
                current = root[0].queue
                candidate = root[0].record(
                    queue=current, source_digest=source, receipt=receipt
                )
                document = candidate.to_document()
                envelope = history_envelope(document)
                parts = c.partition_evidence(
                    envelope, binding=binding, item_count=count
                )
                # Strict one-part supported profile, preflight before any write.
                if len(parts) != 1:
                    raise ReviewInputError(
                        "indexed current checkpoint requires multiple parts"
                    )
                part = parts[0]
                raw = canonical_bytes(part)
                identity = hashlib.sha256(raw).hexdigest()
                prospective = {**store, identity: part}
                if (
                    len(prospective) > 256
                    or sum(
                        len(canonical_bytes(p)) + c.PART_FRAMING_RESERVE
                        for p in prospective.values()
                    )
                    > 8 * 1024 * 1024
                ):
                    raise ReviewInputError(
                        "indexed complete graph retention is exhausted"
                    )
                if observer is not None:
                    observer(document, part, prospective, index)
                control.consume()  # physical write
                store[identity] = part
                reference = HistoryPartReference(
                    identity, identity, len(raw), len(canonical_bytes(envelope))
                )
                # Actual A authenticated decode/readback, charged once.
                restored = reader(reference)
                accounting = control.snapshot()
                root[0] = IndexedAssessmentJournal.from_document(
                    restored,
                    current_reference=reference,
                    current_read_accounting={
                        "calls": accounting["calls"],
                        "deadline_at_ms": accounting["deadline_unix_ms"],
                    },
                    reader=reader,
                )
                phases.append(
                    {
                        "source": index,
                        "calls": receipt["counters"]["provider_calls"],
                        "accepted": bool(receipt["completed"]),
                        "decoded_bytes": len(canonical_bytes(envelope)),
                        "framed_bytes": len(raw) + c.PART_FRAMING_RESERVE,
                        "stored_parts": len(store),
                        "stored_framed_bytes": sum(
                            len(canonical_bytes(p)) + c.PART_FRAMING_RESERVE
                            for p in store.values()
                        ),
                        "control_calls": control.calls,
                    }
                )

            return AssessmentCheckpoint(
                operation_id=operation, read=latest, write=write, now=lambda: clock[0]
            )

        index = 0
        while pending.pending:
            options = {}
            if full_feedback:
                try:
                    from review_sensei.feedback import (
                        FeedbackReference,
                        FeedbackSelection,
                        FeedbackSource,
                    )
                except ImportError:
                    self.skipTest("requires frozen D complete feedback selection")
                from tests.test_review_work import HUMAN

                reference = FeedbackReference(
                    "issue", index + 1000, "2026-10-09T00:00:00Z"
                )
                feedbacks[index] = FeedbackSelection(
                    "owner/repo",
                    42,
                    "a" * 40,
                    "b" * 40,
                    reference,
                    (
                        FeedbackSource(
                            reference,
                            "alice",
                            7,
                            "MEMBER",
                            HUMAN + f" source {index} 深🙂",
                        ),
                    ),
                    queue.pending_order[:4],
                )
                options["feedback"] = feedbacks[index]
            provider = RejectingProvider() if index == 0 else AssessingProvider()
            work = self.run_work(
                pending,
                bundle,
                queue,
                provider=provider,
                checkpoint=checkpoint(index),
                tracker=ResourceBudgetTracker(
                    ResourceBudget.create(max_provider_calls=1)
                ),
                **options,
            )
            pending, queue = work.apply_to(pending), root[0].queue
            self.assertEqual(len(provider.calls), 1)
            if index == 0:
                replay = RejectingProvider()
                self.run_work(
                    pending,
                    bundle,
                    queue,
                    provider=replay,
                    checkpoint=checkpoint(index),
                    tracker=ResourceBudgetTracker(
                        ResourceBudget.create(max_provider_calls=1)
                    ),
                    **options,
                )
                self.assertEqual(replay.calls, [])
            if index == 32:
                self.assertEqual(len(root[0].to_document()["sources"]), 32)
                self.assertTrue(pending.pending)
            index += 1
        self.assertEqual(index, 1 + (count + 3) // 4)
        self.assertEqual(root[0].queue.resolved_ids, pending.resolved)
        # Direct old-source lookup, never recursive omitted-history traversal.
        reads.clear()
        latest_source = hashlib.sha256(b"source:1").hexdigest()
        latest_operation = hashlib.sha256(b"operation:1").hexdigest()
        cp = checkpoint(1)
        receipt = cp.read()
        self.assertEqual(len(reads), 1)
        self.assertEqual(receipt["operation_id"], latest_operation)
        old_calls = budgets[latest_source].calls
        cp.read()
        self.assertEqual(len(reads), 2)  # fresh root read creates fresh exact proof
        self.assertEqual(budgets[latest_source].calls, old_calls + 1)
        if full_feedback:
            replay = AssessingProvider()
            work = self.run_work(
                pending,
                bundle,
                queue,
                provider=replay,
                checkpoint=checkpoint(1),
                feedback=feedbacks[1],
                tracker=ResourceBudgetTracker(
                    ResourceBudget.create(max_provider_calls=1)
                ),
            )
            self.assertEqual(replay.calls, [])
            self.assertEqual(work.apply_to(pending).resolved, pending.resolved)
        final = root[0].to_document()
        provider = AssessingProvider()
        with self.assertRaisesRegex(ReviewInputError, "authority changed"):
            self.run_work(
                pending,
                bundle,
                queue,
                provider=provider,
                checkpoint=checkpoint(1, "e" * 64),
            )
        self.assertEqual(provider.calls, [])
        self.assertEqual(root[0].to_document(), final)
        clock[0] += timedelta(hours=7)
        with self.assertRaisesRegex(ReviewInputError, "expired"):
            self.run_work(
                pending,
                bundle,
                queue,
                provider=AssessingProvider(),
                checkpoint=checkpoint(1),
                tracker=ResourceBudgetTracker(
                    ResourceBudget.create(max_provider_calls=1)
                ),
            )
        self.assertEqual(root[0].to_document(), final)
        self.last_profile = (root[0], store, binding, budgets, phases)
        return {
            "inventory": count,
            "sources": index,
            "parts": len(store),
            "framed_bytes": phases[-1]["stored_framed_bytes"],
            "maximum_current_decoded_bytes": max(p["decoded_bytes"] for p in phases),
            "maximum_current_framed_bytes": max(p["framed_bytes"] for p in phases),
            "maximum_source_synthetic_control_calls": max(
                p["control_calls"] for p in phases
            ),
            "direct_source_references": len(root[0].history_references()),
            "maximum_lookup_decoded_bytes": len(
                canonical_bytes(history_envelope(final))
            )
            + max(ref.decoded_bytes for ref in root[0].history_references()),
            "all_index_references_decoded_bytes": sum(
                ref.decoded_bytes for ref in root[0].history_references()
            ),
            "phases": phases,
        }

    def test_varied_100_every_checkpoint_is_one_part(self):
        self.profile(100)

    def test_varied_250_every_checkpoint_is_one_part_and_complete_retention(self):
        self.profile(250)

    def test_varied_250_full_feedback_exact_targets_are_preserved(self):
        self.profile(250, full_feedback=True)

    def test_frozen_varied_unicode_100_250_complete_feedback_profiles(self):
        for count in (100, 250):
            with self.subTest(count=count):
                self.profile(count, full_feedback=True, frozen_inventory=True)

    def test_direct_resolution_rejects_missing_tampered_foreign_and_wrong_envelope(
        self,
    ):
        c = self.codec()
        self.profile(8)
        root, store, binding, _, _ = self.last_profile
        reference = root.history_references()[0]
        original = store[reference.storage_id]

        def resolve(part, *, producer="local-ledger", ref=reference, domain=binding):
            return resolve_history_part(
                ref,
                reader=lambda _: c.AuthenticatedPart(copy.deepcopy(part), producer),
                expected_binding=domain,
                budget=c.EvidenceReadBudget(),
                item_count=8,
            )

        with self.assertRaisesRegex(ReviewInputError, "malformed"):
            resolve(None)
        with self.assertRaisesRegex(ReviewInputError, "owner"):
            resolve(original, producer="github-bot:123")
        changed = copy.deepcopy(original)
        changed["data"] = changed["data"][:-1] + (
            "A" if changed["data"][-1] != "A" else "B"
        )
        with self.assertRaisesRegex(ReviewInputError, "integrity"):
            resolve(changed)
        with self.assertRaises(ReviewInputError):
            resolve(original, domain={**binding, "head_sha": "c" * 40})
        with self.assertRaises(ReviewInputError):
            resolve(
                original,
                ref=replace(reference, decoded_bytes=reference.decoded_bytes + 1),
            )
        bad = c.partition_evidence(
            {"schema_version": "1.0", "kind": "diagnostic", "journal": {}},
            binding=binding,
            item_count=8,
        )[0]
        wire = canonical_bytes(bad)
        bad_ref = HistoryPartReference(
            hashlib.sha256(wire).hexdigest(),
            hashlib.sha256(wire).hexdigest(),
            len(wire),
            len(
                canonical_bytes(
                    {"schema_version": "1.0", "kind": "diagnostic", "journal": {}}
                )
            ),
        )
        with self.assertRaisesRegex(ReviewInputError, "envelope"):
            resolve(bad, ref=bad_ref)

    def test_archived_identity_accounting_and_progress_never_replenish(self):
        self.profile(8)
        root, _, binding, budgets, _ = self.last_profile
        source = hashlib.sha256(b"source:1").hexdigest()
        operation = hashlib.sha256(b"operation:1").hexdigest()
        entry = next(
            item
            for item in root.to_document()["sources"]
            if item["source_digest"] == source
        )
        proof = QueueHostState(
            history_envelope(root.to_document()), 78, binding, 8, None
        )
        kwargs = {"authenticated_root": proof, "expected_binding": binding}
        accounting = root.source_read_accounting(
            source_digest=source, operation_id=operation, **kwargs
        )
        self.assertEqual(accounting, entry["read_accounting"])
        self.assertLess(accounting["calls"], budgets[source].calls)
        accounting["calls"] = 0
        self.assertEqual(
            root.source_read_accounting(
                source_digest=source, operation_id=operation, **kwargs
            ),
            entry["read_accounting"],
        )
        with self.assertRaisesRegex(ReviewInputError, "authority changed"):
            root.source_read_accounting(
                source_digest=source, operation_id="e" * 64, **kwargs
            )
        for bad in (
            replace(proof, item_count=7),
            replace(proof, envelope={}),
            replace(proof, binding={**binding, "head_sha": "c" * 40}),
        ):
            with self.assertRaisesRegex(ReviewInputError, "authenticated latest root"):
                root.source_read_accounting(
                    source_digest=source,
                    operation_id=operation,
                    authenticated_root=bad,
                    expected_binding=binding,
                )
        receipt = root.receipt(source_digest=source, operation_id=operation)
        receipt["counters"]["provider_calls"] = 0
        with self.assertRaises(ReviewInputError):
            root.record(queue=root.queue, source_digest=source, receipt=receipt)
        self.assertEqual(len(root.history_references()), 2)

    def test_exact_child_source_and_closed_index_shapes(self):
        self.profile(8)
        root, _, _, _, _ = self.last_profile
        document = root.to_document()
        source = document["sources"][1]
        source["reference"] = document["sources"][0]["reference"]
        with self.assertRaisesRegex(ReviewInputError, "aliases"):
            IndexedAssessmentJournal.from_document(document)
        document = root.to_document()
        document["sources"][0]["read_accounting"]["calls"] = 65
        with self.assertRaisesRegex(ReviewInputError, "accounting"):
            IndexedAssessmentJournal.from_document(document)
        document = root.to_document()
        document["extra"] = True
        with self.assertRaisesRegex(ReviewInputError, "document"):
            IndexedAssessmentJournal.from_document(document)
        document = root.to_document()
        first, second = document["sources"]
        first["reference"], second["reference"] = (
            second["reference"],
            first["reference"],
        )
        swapped = IndexedAssessmentJournal.from_document(document, reader=root.reader)
        with self.assertRaisesRegex(ReviewInputError, "segment/source"):
            swapped.receipt(
                source_digest=first["source_digest"], operation_id=first["operation_id"]
            )

    def test_retained_and_active_bounds_are_independent_finite_refusals(self):
        self.profile(8)
        root, _, _, _, _ = self.last_profile
        document = root.to_document()
        template = document["sources"][0]
        rows = []
        for index in range(256):
            identity = hashlib.sha256(f"finite:{index}".encode()).hexdigest()
            row = copy.deepcopy(template)
            row["source_digest"] = identity
            row["operation_id"] = identity
            row["reference"]["storage_id"] = identity
            row["active"] = False
            rows.append(row)
        document["sources"] = rows[:255]
        self.assertEqual(
            len(IndexedAssessmentJournal.from_document(document).history_references()),
            255,
        )
        document["sources"] = rows
        with self.assertRaisesRegex(ReviewInputError, "retained"):
            IndexedAssessmentJournal.from_document(document)
        document["sources"] = copy.deepcopy(rows[:32])
        for row in document["sources"]:
            row["active"] = True
        IndexedAssessmentJournal.from_document(document)
        document["sources"].append({**copy.deepcopy(rows[32]), "active": True})
        with self.assertRaisesRegex(ReviewInputError, "active"):
            IndexedAssessmentJournal.from_document(document)

    def test_archival_requires_original_owned_readback_not_planned_reference(self):
        self.profile(8)
        root, _, _, _, _ = self.last_profile
        unowned = IndexedAssessmentJournal.from_document(
            root.to_document(), reader=root.reader
        )
        source = hashlib.sha256(b"source:1").hexdigest()
        operation = hashlib.sha256(b"operation:1").hexdigest()
        receipt = root.receipt(source_digest=source, operation_id=operation)
        with self.assertRaisesRegex(ReviewInputError, "owned readback"):
            unowned.record(queue=root.queue, source_digest=source, receipt=receipt)

    def test_explicit_v1_v2_migration_preserves_all_receipts_and_refuses_omission(self):
        self.profile(8)
        root, _, binding, _, _ = self.last_profile
        document = root.to_document()
        sources = list(document["sources"])
        current = root._current()
        self.assertIsNotNone(current)
        sources.append(
            {
                "source_digest": current["source_digest"],
                "operation_id": current["operation_id"],
                "reference": root.current_reference.to_document(),
                "read_accounting": root.current_read_accounting,
            }
        )
        rows = tuple(
            {
                "source_digest": item["source_digest"],
                "operation_id": item["operation_id"],
                "receipt": root.receipt(
                    source_digest=item["source_digest"],
                    operation_id=item["operation_id"],
                ),
            }
            for item in sources
        )
        refs = {
            item["source_digest"]: HistoryPartReference.from_document(item["reference"])
            for item in sources
        }
        accounting = {
            item["source_digest"]: item["read_accounting"] for item in sources
        }
        for legacy in (
            AssessmentJournal(root.queue, rows),
            FactoredAssessmentJournal(root.queue, rows),
        ):
            migrated = IndexedAssessmentJournal.from_retained(
                legacy,
                owned_segments=refs,
                original_read_accounting=accounting,
                reader=root.reader,
            )
            self.assertEqual(len(migrated.history_references()), 3)
            self.assertEqual(migrated.queue, root.queue)
            for row in rows:
                self.assertEqual(
                    migrated.receipt(
                        source_digest=row["source_digest"],
                        operation_id=row["operation_id"],
                    ),
                    row["receipt"],
                )
            with self.assertRaisesRegex(ReviewInputError, "every source"):
                IndexedAssessmentJournal.from_retained(
                    legacy,
                    owned_segments=dict(list(refs.items())[1:]),
                    original_read_accounting=accounting,
                    reader=root.reader,
                )
            with self.assertRaisesRegex(ReviewInputError, "every source"):
                IndexedAssessmentJournal.from_retained(
                    legacy,
                    owned_segments={**refs, "e" * 64: next(iter(refs.values()))},
                    original_read_accounting=accounting,
                    reader=root.reader,
                )
            changed = {
                **refs,
                sources[0]["source_digest"]: refs[sources[1]["source_digest"]],
            }
            with self.assertRaisesRegex(ReviewInputError, "changed receipt"):
                IndexedAssessmentJournal.from_retained(
                    legacy,
                    owned_segments=changed,
                    original_read_accounting=accounting,
                    reader=root.reader,
                )

    def test_reference_closed_shapes_and_limits(self):
        for value in (
            {},
            {
                "storage_id": "../../outside",
                "sha256": "a" * 64,
                "bytes": 1,
                "decoded_bytes": 1,
            },
            {
                "storage_id": "a" * 64,
                "sha256": "a" * 64,
                "bytes": True,
                "decoded_bytes": 1,
            },
            {
                "storage_id": "a" * 64,
                "sha256": "a" * 64,
                "bytes": 32768,
                "decoded_bytes": 1,
            },
            {
                "storage_id": "a" * 64,
                "sha256": "a" * 64,
                "bytes": 1,
                "decoded_bytes": 2097153,
            },
        ):
            with self.assertRaises(ReviewInputError):
                HistoryPartReference.from_document(value)
