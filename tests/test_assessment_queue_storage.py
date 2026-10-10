"""Dedicated queue-slot storage contract; C owns complete journal semantics.

Hosted fixtures qualify object ownership/storage, not broker grant issuance for
multiple checkpoints. The consuming broker contract remains an integration gate.
"""

import copy
import random
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from review_sensei.bounded_evidence import EvidenceReadBudget, canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.schemas import validate_public_document
from review_sensei.session import (
    MAX_SESSION_RECORD_BYTES,
    LocalSessionLedger,
    SessionRecord,
    assessment_queue_manifest_capacity,
    checkpoint_baseline_capacity,
    read_session_assessment_queue,
    read_session_baseline,
)
from tests import test_review_transaction as fixture
from tests.test_authenticated_partitions import GitHubState, binding, checkpoint, packed


def active_summary():
    return {
        "operation_id": "1" * 64,
        "source_digest": "2" * 64,
        "authority_digest": "3" * 64,
        "execution_identity": "4" * 32,
        "deadline_at": "2026-10-10T00:02:00Z",
        "expires_at": "2026-10-10T06:00:00Z",
        "provider_calls": 8,
        "transport_retries": 2,
        "structural_retries": 2,
        "prompt_bytes": 2_097_152,
        "response_bytes": 524_288,
        "response_bytes_reserved": 524_288,
        "read_accounting": {"calls": 64, "deadline_at_ms": 1_791_590_520_000},
    }


def queue_root(manifest, *, active=None):
    return {
        "schema_version": "1.0",
        "inventory_digest": "5" * 64,
        "inventory_generation": manifest["binding"]["generation"],
        "state_manifest": manifest,
        "active_operation": active,
    }


def stored_queue_record(root):
    return SessionRecord.create(
        fixture.IDENTITY, now=fixture.NOW, generation=2, assessment_queue=root
    )


def activate(ledger, *, payload=None, active=None):
    """One permitted storage mutation; no inference, source ack or receipt logic."""
    payload = payload or {"schema_version": "1.0", "synthetic_journal": [1, 2, 3]}

    def mutation(record):
        context = binding(
            producer=ledger.evidence_producer(),
            generation=record.generation,
            purpose="queue",
        )
        manifest = ledger.stage_evidence(
            fixture.IDENTITY,
            binding=context,
            document=payload,
            item_count=3,
            max_manifest_bytes=assessment_queue_manifest_capacity(record),
        )
        return record.evolve(
            assessment_queue=queue_root(manifest, active=active),
            generation=record.generation + 1,
            now=fixture.NOW,
        )

    return ledger.replace(fixture.IDENTITY, mutation, now=fixture.NOW), payload


class RootParserTests(unittest.TestCase):
    def setUp(self):
        manifest, _ = packed(
            {"schema_version": "1.0", "synthetic_journal": []},
            binding(generation=1, purpose="queue"),
        )
        self.root = queue_root(manifest, active=active_summary())

    def test_closed_root_round_trip_digest_and_absent_legacy_bytes(self):
        record = stored_queue_record(self.root)
        validate_public_document(record.to_dict(), "session-record")
        self.assertEqual(SessionRecord.from_dict(record.to_dict()), record)
        updated = record.evolve(generation=3, now=fixture.NOW)
        self.assertEqual(updated.assessment_queue, record.assessment_queue)
        self.assertNotEqual(updated.record_sha256, record.record_sha256)
        plain = SessionRecord.create(fixture.IDENTITY, now=fixture.NOW)
        self.assertNotIn("assessment_queue", plain.to_dict())
        with self.assertRaisesRegex(ReviewInputError, "absence"):
            SessionRecord.from_dict(dict(plain.to_dict(), assessment_queue=None))
        self.assertEqual(
            SessionRecord.from_dict(plain.to_dict()).to_dict(), plain.to_dict()
        )
        tampered = record.to_dict()
        tampered["assessment_queue"]["inventory_digest"] = "f" * 64
        with self.assertRaisesRegex(ReviewInputError, "integrity"):
            SessionRecord.from_dict(tampered)

    def test_schema_and_parser_reject_closed_shape_and_scalar_bounds(self):
        for field, value in (
            ("response_bytes_reserved", True),
            ("response_bytes_reserved", 1_048_577),
            ("response_bytes_reserved", -1),
            ("provider_calls", 9),
            ("prompt_bytes", 2_097_153),
            ("execution_identity", "e" * 64),
            ("deadline_at", "2026-10-10T00:02:00+01:00"),
            ("deadline_at", "2026-10-10T00:02:00"),
        ):
            with self.subTest(field=field, value=value):
                root = copy.deepcopy(self.root)
                root["active_operation"][field] = value
                with self.assertRaises(ReviewInputError):
                    validate_public_document(root, "assessment-queue-root")
                with self.assertRaises(ReviewInputError):
                    stored_queue_record(root)
        for mutate in (
            lambda root: root.update(unknown=True),
            lambda root: root["active_operation"].pop("response_bytes_reserved"),
            lambda root: root["active_operation"]["read_accounting"].update(extra=1),
            lambda root: root["state_manifest"]["binding"].update(purpose="baseline"),
        ):
            root = copy.deepcopy(self.root)
            mutate(root)
            with self.assertRaises(ReviewInputError):
                validate_public_document(root, "assessment-queue-root")
            with self.assertRaises(ReviewInputError):
                stored_queue_record(root)

    def test_runtime_cross_field_response_generation_identity_and_utc_guards(self):
        # JSON Schema bounds each scalar; the authoritative runtime also checks
        # cross-field arithmetic and binding/date semantics.
        for mutate in (
            lambda root: root["active_operation"].update(response_bytes=524_289),
            lambda root: root["active_operation"].update(
                deadline_at="2026-02-30T00:00:00Z"
            ),
            lambda root: root["active_operation"].update(
                expires_at="2026-10-09T00:00:00Z"
            ),
            lambda root: root.update(inventory_generation=0),
            lambda root: root["state_manifest"]["binding"].update(
                repository="other/repo"
            ),
            lambda root: root["state_manifest"]["binding"].update(generation=3),
        ):
            with self.subTest(mutate=mutate):
                root = copy.deepcopy(self.root)
                mutate(root)
                with self.assertRaises(ReviewInputError):
                    stored_queue_record(root)
        for measured, reserved in ((0, 1_048_576), (1_048_576, 0), (524_288, 524_288)):
            root = copy.deepcopy(self.root)
            root["active_operation"].update(
                response_bytes=measured, response_bytes_reserved=reserved
            )
            self.assertIsNotNone(stored_queue_record(root))

    def test_total_root_includes_summary_and_ref_cardinality(self):
        root = copy.deepcopy(self.root)
        part = root["state_manifest"]["parts"][0]
        root["state_manifest"]["parts"] = [
            dict(part, storage_id=f"{i:064d}") for i in range(32)
        ]
        root["state_manifest"]["encoded_bytes"] = 32 * (part["bytes"] + 512)
        self.assertLess(len(canonical_bytes(root["state_manifest"])), 8192)
        self.assertLess(len(canonical_bytes(root)), 8192)
        self.assertIsNotNone(stored_queue_record(root))
        # An actual reduced allocation cannot charge only manifest bytes.
        with patch(
            "review_sensei.session.MAX_ASSESSMENT_QUEUE_ROOT_BYTES",
            len(canonical_bytes(root)) - 1,
        ):
            with self.assertRaisesRegex(ReviewInputError, "allocation"):
                stored_queue_record(root)
        root["state_manifest"]["parts"].append(dict(part, storage_id="z" * 64))
        with self.assertRaises(ReviewInputError):
            stored_queue_record(root)


class QueueAdapterTests(unittest.TestCase):
    def test_local_hosted_whole_journal_round_trip_restart_and_tombstone(self):
        with tempfile.TemporaryDirectory() as directory:
            local = LocalSessionLedger(Path(directory), enable_partition_writes=True)
            local.initialize(fixture.IDENTITY, now=fixture.NOW)
            record, payload = activate(local, active=active_summary())
            restarted = LocalSessionLedger(Path(directory))
            loaded = restarted.load(fixture.IDENTITY, now=fixture.NOW)
            self.assertEqual(loaded.status, "ok")
            self.assertEqual(
                read_session_assessment_queue(restarted, loaded.record), payload
            )
            self.assertEqual(loaded.record.assessment_queue, record.assessment_queue)
            with self.assertRaisesRegex(ReviewInputError, "tombstone"):
                restarted.replace(
                    fixture.IDENTITY,
                    lambda item: item.evolve(assessment_queue=None),
                    now=fixture.NOW,
                )
            self.assertEqual(
                restarted.load(fixture.IDENTITY, now=fixture.NOW).record, record
            )
        state = GitHubState()
        ledger = state.ledger()
        ledger.initialize(fixture.IDENTITY, now=fixture.NOW)
        record, payload = activate(ledger, active=active_summary())
        restarted = state.ledger()
        loaded = restarted.load(fixture.IDENTITY, now=fixture.NOW)
        self.assertEqual(loaded.status, "ok")
        self.assertEqual(
            read_session_assessment_queue(restarted, loaded.record), payload
        )
        self.assertEqual(loaded.record.assessment_queue, record.assessment_queue)
        with self.assertRaisesRegex(ReviewInputError, "tombstone"):
            restarted.replace(
                fixture.IDENTITY,
                lambda item: item.evolve(assessment_queue=None),
                now=fixture.NOW,
            )

    def test_queue_part_missing_wrong_owner_association_and_default_reader_fail_closed(
        self,
    ):
        for fault in ("missing", "owner", "association", "no-budget"):
            with self.subTest(fault=fault):
                state = GitHubState()
                ledger = state.ledger()
                ledger.initialize(fixture.IDENTITY, now=fixture.NOW)
                activate(ledger)
                part_id = next(
                    identity
                    for identity, item in state.comments.items()
                    if "evidence-part:v1" in item["body"]
                )
                if fault == "missing":
                    del state.comments[part_id]
                elif fault == "owner":
                    state.comments[part_id]["user"]["id"] = 56
                elif fault == "association":
                    state.comments[part_id]["issue_url"] = (
                        "https://api.github.test/repos/other/repo/issues/146"
                    )
                reader = state.ledger()
                if fault == "no-budget":
                    reader.evidence_budget = None
                self.assertEqual(
                    reader.load(fixture.IDENTITY, now=fixture.NOW).status,
                    "integrity-failed",
                )
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory), enable_partition_writes=True)
            ledger.initialize(fixture.IDENTITY, now=fixture.NOW)
            activate(ledger)
            next(Path(directory).glob(".evidence/*/*/*")).unlink()
            self.assertEqual(
                LocalSessionLedger(Path(directory))
                .load(fixture.IDENTITY, now=fixture.NOW)
                .status,
                "integrity-failed",
            )

    def test_allocation_preserves_baseline_and_future_summary_growth_both_adapters(
        self,
    ):
        for count in (100, 250):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as directory:
                local = LocalSessionLedger(
                    Path(directory), enable_partition_writes=True
                )
                checkpoint(local, count)
                prior = local.load(fixture.IDENTITY, now=fixture.NOW).record
                previous_capacity = checkpoint_baseline_capacity(prior)
                # The original acquisition allowance also included enrollment,
                # reservation and analysis. It cannot silently replenish to admit
                # a new queue. This is a distinct initial queue admission with no
                # persisted active operation/receipt to resume.
                with self.assertRaises(ReviewInputError):
                    activate(local)
                local = LocalSessionLedger(
                    Path(directory), enable_partition_writes=True
                )
                record, _ = activate(local)
                self.assertGreater(assessment_queue_manifest_capacity(record), 0)
                self.assertLess(checkpoint_baseline_capacity(record), previous_capacity)
                grown = record.evolve(
                    assessment_queue=dict(
                        record.assessment_queue, active_operation=active_summary()
                    ),
                    now=fixture.NOW,
                )
                self.assertLess(
                    len(canonical_bytes(grown.to_dict())), MAX_SESSION_RECORD_BYTES
                )
                state = GitHubState()
                checkpoint(state.ledger(), count)
                record, _ = activate(state.ledger())
                self.assertLess(
                    len(canonical_bytes(record.to_dict())), MAX_SESSION_RECORD_BYTES
                )
                self.assertEqual(
                    state.ledger().load(fixture.IDENTITY, now=fixture.NOW).status, "ok"
                )

    def test_interleaved_newer_root_survives_conditional_activation_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            local = LocalSessionLedger(Path(directory), enable_partition_writes=True)
            local.initialize(fixture.IDENTITY, now=fixture.NOW)
            activate(local)
            competing = LocalSessionLedger(Path(directory))

            def interleave(record):
                competing.replace(
                    fixture.IDENTITY,
                    lambda item: item.evolve(
                        operator_paused=True, generation=item.generation + 1
                    ),
                    now=fixture.NOW,
                )
                return record.evolve(generation=record.generation + 1)

            with self.assertRaisesRegex(ReviewInputError, "conflict"):
                local.replace(fixture.IDENTITY, interleave, now=fixture.NOW)
            self.assertTrue(
                competing.load(fixture.IDENTITY, now=fixture.NOW).record.operator_paused
            )
        state = GitHubState()
        ledger = state.ledger()
        ledger.initialize(fixture.IDENTITY, now=fixture.NOW)
        activate(ledger)

        def interleave_hosted(record):
            state.ledger().replace(
                fixture.IDENTITY,
                lambda item: item.evolve(
                    operator_paused=True, generation=item.generation + 1
                ),
                now=fixture.NOW,
            )
            return record.evolve(generation=record.generation + 1)

        with self.assertRaisesRegex(ReviewInputError, "conflict"):
            state.ledger().replace(fixture.IDENTITY, interleave_hosted, now=fixture.NOW)
        self.assertTrue(
            state.ledger()
            .load(fixture.IDENTITY, now=fixture.NOW)
            .record.operator_paused
        )

    def test_orphans_share_retention_and_scans_with_active_queue_parts(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory), enable_partition_writes=True)
            ledger.initialize(fixture.IDENTITY, now=fixture.NOW)
            original, _ = activate(ledger)
            part_dir = next(Path(directory).glob(".evidence/*/*"))
            for index in range(255):
                (part_dir / f"{index:064x}").write_text("retained orphan")
            with self.assertRaises(ReviewInputError):
                activate(
                    ledger, payload={"schema_version": "1.0", "synthetic_journal": [4]}
                )
            self.assertEqual(
                LocalSessionLedger(Path(directory))
                .load(fixture.IDENTITY, now=fixture.NOW)
                .record,
                original,
            )
            self.assertEqual(len(list(part_dir.iterdir())), 256)
        state = GitHubState()
        ledger = state.ledger()
        ledger.initialize(fixture.IDENTITY, now=fixture.NOW)
        original, _ = activate(ledger)
        item = next(
            item
            for item in state.comments.values()
            if "evidence-part:v1" in item["body"]
        )
        for index in range(255):
            orphan = copy.deepcopy(item)
            orphan["id"] = 2000 + index
            state.comments[orphan["id"]] = orphan
        before = copy.deepcopy(state.comments)
        with self.assertRaises(ReviewInputError):
            activate(
                state.ledger(),
                payload={"schema_version": "1.0", "synthetic_journal": [4]},
            )
        self.assertEqual(state.comments, before)
        self.assertEqual(
            state.ledger().load(fixture.IDENTITY, now=fixture.NOW).record, original
        )

    def test_unsupported_custom_resolver_or_missing_budget_is_sanitized_refusal(self):
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory), enable_partition_writes=True)
            checkpoint(ledger, 250)
            ledger = LocalSessionLedger(Path(directory), enable_partition_writes=True)
            record, _ = activate(ledger)
            invalid = (
                object(),
                SimpleNamespace(evidence_producer=lambda: "local-ledger"),
                SimpleNamespace(
                    evidence_producer=lambda: "local-ledger",
                    read_evidence=lambda *args, **kwargs: {},
                ),
                SimpleNamespace(
                    evidence_producer=None,
                    read_evidence=None,
                    evidence_budget=EvidenceReadBudget(),
                ),
            )
            for resolver in invalid:
                for read in (read_session_baseline, read_session_assessment_queue):
                    with self.subTest(resolver=resolver, reader=read):
                        with self.assertRaisesRegex(
                            ReviewInputError, "trusted resolver"
                        ):
                            read(resolver, record)
            plain = SessionRecord.create(fixture.IDENTITY, now=fixture.NOW)
            self.assertIsNone(read_session_baseline(object(), plain))
            self.assertIsNone(read_session_assessment_queue(object(), plain))

    def test_expired_queue_reenrollment_refuses_without_dropping_receipts(self):
        later = fixture.NOW + timedelta(days=31)
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory), enable_partition_writes=True)
            ledger.initialize(fixture.IDENTITY, now=fixture.NOW)
            original, _ = activate(ledger)
            reader = LocalSessionLedger(Path(directory))
            with self.assertRaisesRegex(ReviewInputError, "tombstone"):
                reader.reenroll(fixture.IDENTITY, now=later)
            self.assertEqual(
                reader.load(fixture.IDENTITY, now=fixture.NOW).record, original
            )
        state = GitHubState()
        ledger = state.ledger()
        ledger.initialize(fixture.IDENTITY, now=fixture.NOW)
        activate(ledger)
        before = copy.deepcopy(state.comments)
        with self.assertRaisesRegex(ReviewInputError, "tombstone"):
            state.ledger().reenroll(fixture.IDENTITY, now=later)
        self.assertEqual(state.comments, before)

    def test_writers_off_and_shared_original_budget_refuse_before_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory))
            original = ledger.initialize(fixture.IDENTITY, now=fixture.NOW)
            with self.assertRaisesRegex(ReviewInputError, "enablement"):
                activate(ledger)
            ledger.enable_partition_writes = True
            ledger.evidence_budget.calls = 59
            with self.assertRaises(ReviewInputError):
                activate(
                    ledger,
                    payload={
                        "schema_version": "1.0",
                        "text": random.Random(242).randbytes(40_000).hex(),
                    },
                )
            reader = LocalSessionLedger(Path(directory))
            self.assertEqual(
                reader.load(fixture.IDENTITY, now=fixture.NOW).record, original
            )
            self.assertEqual(list(Path(directory).glob(".evidence/*/*/*")), [])


if __name__ == "__main__":
    unittest.main()
