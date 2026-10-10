"""Physical local authority I/O shares original count and activation deadline."""

import json
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.errors import ReviewInputError
from review_sensei.session import LocalSessionLedger, SessionRecord
from tests import test_review_transaction as fixture
from tests.test_authenticated_partitions import binding, checkpoint


class LocalAccountingTests(unittest.TestCase):
    def test_root_reads_and_four_final_fences_share_one_original_allowance(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(
                Path(directory), evidence_budget=EvidenceReadBudget()
            )
            ledger.initialize(fixture.IDENTITY, now=fixture.NOW)
            ledger.evidence_budget.calls = 59
            self.assertEqual(
                ledger.load(fixture.IDENTITY, now=fixture.NOW).status, "ok"
            )
            self.assertEqual(ledger.evidence_budget.calls, 60)
            self.assertEqual(
                ledger.load(fixture.IDENTITY, now=fixture.NOW).status,
                "integrity-failed",
            )
            for _ in range(4):
                self.assertIsNotNone(
                    ledger._read_document(ledger._path(fixture.IDENTITY), fence=True)
                )
            self.assertEqual(ledger.evidence_budget.calls, 64)
            with self.assertRaises(ReviewInputError):
                ledger._read_document(ledger._path(fixture.IDENTITY), fence=True)

    def test_enrollment_read_write_and_scan_entry_dispatches_are_charged(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory), enable_partition_writes=True)
            initial = ledger.evidence_budget.calls
            self.assertEqual(
                ledger.load(fixture.IDENTITY, now=fixture.NOW).status, "missing"
            )
            # root read, bounded enrollment directory/path inspection and read
            self.assertEqual(ledger.evidence_budget.calls - initial, 4)
            ledger.initialize(fixture.IDENTITY, now=fixture.NOW)
            before = ledger.evidence_budget.calls
            self.assertTrue(ledger._has_enrollment_witness(fixture.IDENTITY))
            self.assertEqual(ledger.evidence_budget.calls - before, 3)
            parts = ledger._part_directory(fixture.IDENTITY)
            parts.mkdir(parents=True)
            for index in range(8):
                (parts / f"{index:064x}").write_text("orphan")
            ledger.evidence_budget.calls = 55
            with self.assertRaises(ReviewInputError):
                ledger.stage_evidence(
                    fixture.IDENTITY,
                    binding=binding(),
                    document={"x": 1},
                    item_count=1,
                    max_manifest_bytes=8192,
                )
            self.assertEqual(ledger.evidence_budget.calls, 60)
            self.assertEqual(len(list(parts.iterdir())), 8)

    def test_expiry_during_temporary_file_sync_refuses_activation_and_cleans_temp(self):
        now = [0.0]
        budget = EvidenceReadBudget(clock=lambda: now[0], wall_clock=lambda: 1000.0)
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory), evidence_budget=budget)
            prior = ledger.initialize(fixture.IDENTITY, now=fixture.NOW)
            before = budget.calls
            sync = os.fsync

            def expire_on_sync(fd):
                sync(fd)
                now[0] = 60.0

            with (
                patch("review_sensei.session.os.fsync", side_effect=expire_on_sync),
                patch("review_sensei.session.os.replace") as activate,
            ):
                with self.assertRaisesRegex(ReviewInputError, "deadline"):
                    ledger.replace(
                        fixture.IDENTITY,
                        lambda record: record.evolve(generation=record.generation + 1),
                        now=fixture.NOW,
                    )
                activate.assert_not_called()
            self.assertGreater(budget.calls, before)
            self.assertEqual(
                SessionRecord.from_dict(
                    json.loads(ledger._path(fixture.IDENTITY).read_text())
                ),
                prior,
            )
            self.assertEqual(
                list(ledger._path(fixture.IDENTITY).parent.glob("*.tmp")), []
            )
            with self.assertRaisesRegex(ReviewInputError, "expired"):
                EvidenceReadBudget(
                    snapshot=budget.snapshot(),
                    clock=lambda: 9000.0,
                    wall_clock=lambda: 1060.0,
                )

    def test_expiry_after_activation_is_ambiguous_and_never_acknowledged(self):
        now = [0.0]
        budget = EvidenceReadBudget(clock=lambda: now[0])
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory), evidence_budget=budget)
            prior = ledger.initialize(fixture.IDENTITY, now=fixture.NOW)
            installed = [False]
            original_replace = os.replace
            original_sync = os.fsync

            def replace(source, target):
                original_replace(source, target)
                installed[0] = True
                now[0] = 60.0

            def sync(fd):
                original_sync(fd)
                if installed[0]:
                    now[0] = 60.0

            with (
                patch("review_sensei.session.os.replace", side_effect=replace),
                patch("review_sensei.session.os.fsync", side_effect=sync),
            ):
                with self.assertRaisesRegex(ReviewInputError, "deadline"):
                    ledger.replace(
                        fixture.IDENTITY,
                        lambda record: record.evolve(generation=record.generation + 1),
                        now=fixture.NOW,
                    )
            persisted = SessionRecord.from_dict(
                json.loads(ledger._path(fixture.IDENTITY).read_text())
            )
            self.assertEqual(persisted.generation, prior.generation + 1)
            self.assertEqual(
                ledger.load(fixture.IDENTITY, now=fixture.NOW).status,
                "integrity-failed",
            )

    def test_restored_accounting_survives_new_process_clock_and_is_not_replenished(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            LocalSessionLedger(Path(directory)).initialize(
                fixture.IDENTITY, now=fixture.NOW
            )
            budget = EvidenceReadBudget(clock=lambda: 0.0, wall_clock=lambda: 1000.0)
            budget.calls = 58
            restored = EvidenceReadBudget(
                snapshot=budget.snapshot(),
                clock=lambda: 9000.0,
                wall_clock=lambda: 1030.0,
            )
            ledger = LocalSessionLedger(Path(directory), evidence_budget=restored)
            self.assertEqual(
                ledger.load(fixture.IDENTITY, now=fixture.NOW).status, "ok"
            )
            self.assertEqual(restored.calls, 59)
            self.assertEqual(restored.deadline, 9030.0)
            with self.assertRaises(ReviewInputError):
                ledger.replace(
                    fixture.IDENTITY,
                    lambda record: record.evolve(generation=record.generation + 1),
                    now=fixture.NOW,
                )
            self.assertEqual(restored.calls, 61)

    def test_default_inline_only_reused_ledger_keeps_legacy_lifetime(self):
        now = [0.0]
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory))
            ledger.initialize(fixture.IDENTITY, now=fixture.NOW)
            # Default inline-only API did not declare one original evidence
            # operation spanning all independent legacy invocations.
            ledger.evidence_budget = EvidenceReadBudget(clock=lambda: now[0])
            now[0] = 61.0
            for _ in range(80):
                self.assertEqual(
                    ledger.load(fixture.IDENTITY, now=fixture.NOW).status, "ok"
                )
            self.assertEqual(ledger.evidence_budget.calls, 0)

    def test_default_richer_root_read_enables_complete_accounting_without_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint(
                LocalSessionLedger(Path(directory), enable_partition_writes=True), 250
            )
            reader = LocalSessionLedger(Path(directory))
            self.assertEqual(
                reader.load(fixture.IDENTITY, now=fixture.NOW).status, "ok"
            )
            self.assertEqual(
                reader.evidence_budget.calls, 7
            )  # root plus 2 bounded part reads/inspections
            original = reader.evidence_budget
            original.calls = 60
            self.assertEqual(
                reader.load(fixture.IDENTITY, now=fixture.NOW).status,
                "integrity-failed",
            )
            self.assertIs(reader.evidence_budget, original)

    def test_expired_v01_without_queue_preserves_explicit_migration_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(
                Path(directory), evidence_budget=EvidenceReadBudget()
            )
            created = SessionRecord.create(
                fixture.IDENTITY, now=fixture.NOW - timedelta(days=60)
            )
            legacy = {
                "schema_version": "0.1",
                "repository": fixture.IDENTITY.repository,
                "pull_request_number": fixture.IDENTITY.pull_request,
                "repository_id": fixture.IDENTITY.repository_id,
                "completed_initial_reviews": 1,
                "completed_verification_rounds": 0,
                "failed_attempts": 0,
                "generation": 0,
                "created_at": created.created_at,
                "updated_at": created.updated_at,
            }
            path = ledger._path(fixture.IDENTITY)
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(legacy))
            self.assertEqual(
                ledger.load(fixture.IDENTITY, now=fixture.NOW).status, "expired"
            )
            self.assertEqual(
                ledger.reenroll(
                    fixture.IDENTITY, now=fixture.NOW
                ).completed_initial_reviews,
                0,
            )
            # The narrow migration exemption never removes a queue-bearing root.
            legacy["assessment_queue"] = {"untrusted": True}
            path.write_text(json.dumps(legacy))
            with self.assertRaises(ReviewInputError):
                ledger.reenroll(fixture.IDENTITY, now=fixture.NOW)
            self.assertEqual(json.loads(path.read_text()), legacy)

    def test_complete_250_checkpoint_retains_finite_physical_accounting(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory), enable_partition_writes=True)
            checkpoint(ledger, 250)
            self.assertLessEqual(ledger.evidence_budget.calls, 64)
            self.assertGreater(ledger.evidence_budget.calls, 40)
            # The same acquisition cannot obtain a fresh queue/staging allowance.
            with self.assertRaises(ReviewInputError):
                ledger.stage_evidence(
                    fixture.IDENTITY,
                    binding=binding(),
                    document={"x": 1},
                    item_count=1,
                    max_manifest_bytes=8192,
                )


if __name__ == "__main__":
    unittest.main()
