"""Original progress mutation extraction and inert joint-candidate controls."""

import tempfile
import unittest
from pathlib import Path

from test_review_transaction import IDENTITY, NOW, _checkpoint_with_baseline

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.session import (
    LocalSessionLedger,
    _admitted_blocker_progress_mutation,
    _publication_state_mutation,
    record_admitted_blocker_progress,
)


class PublicationProgressMutationTests(unittest.TestCase):
    def setup_record(self, directory):
        ledger = LocalSessionLedger(Path(directory))
        result = _checkpoint_with_baseline(ledger)
        before = ledger.load(IDENTITY, now=NOW).record
        return ledger, result.transaction, before

    def mutation(self, ledger, transaction, *, suppress=False):
        return _admitted_blocker_progress_mutation(
            ledger,
            IDENTITY,
            transaction,
            blocker_set_sha256="a" * 64,
            blocker_count=1,
            suppress_publication=suppress,
            now=NOW,
        )

    def test_public_writer_matches_original_extracted_mutation(self):
        for suppress in (False, True):
            with self.subTest(suppress=suppress), tempfile.TemporaryDirectory() as d:
                ledger, transaction, before = self.setup_record(d)
                candidate = self.mutation(ledger, transaction, suppress=suppress)(
                    before
                )
                actual = record_admitted_blocker_progress(
                    ledger,
                    IDENTITY,
                    transaction,
                    blocker_set_sha256="a" * 64,
                    blocker_count=1,
                    suppress_publication=suppress,
                    now=NOW,
                )
                self.assertEqual(
                    canonical_bytes(actual.to_dict()),
                    canonical_bytes(candidate.to_dict()),
                )
                self.assertEqual(
                    self.mutation(ledger, transaction, suppress=suppress)(actual),
                    actual,
                )

    def test_joint_projection_preserves_failed_transaction_and_marker_generations(self):
        with tempfile.TemporaryDirectory() as d:
            ledger, transaction, before = self.setup_record(d)
            raw = canonical_bytes(before.to_dict())
            failed = _publication_state_mutation(transaction, published=False, now=NOW)(
                before
            )
            sequential = self.mutation(ledger, transaction)(failed)
            # Projection only: no dispatch or runtime issuance uses this value.
            joint = before.evolve(
                now=NOW,
                generation=before.generation + 1,
                transaction=sequential.transaction,
                convergence_history=sequential.convergence_history,
            )
            self.assertEqual(sequential.generation, before.generation + 2)
            self.assertEqual(joint.transaction, failed.transaction)
            self.assertEqual(joint.transaction.phase, "publication_failed")
            self.assertEqual(joint.transaction.generation, transaction.generation + 1)
            self.assertEqual(joint.convergence_history, sequential.convergence_history)
            self.assertEqual(
                joint.convergence_history["progress"][-1]["generation"],
                joint.transaction.generation,
            )
            self.assertEqual(joint.api_run_accounting, before.api_run_accounting)
            self.assertEqual(self.mutation(ledger, transaction)(joint), joint)
            self.assertEqual(canonical_bytes(before.to_dict()), raw)

    def test_failed_placeholder_still_requires_original_baseline(self):
        with tempfile.TemporaryDirectory() as d:
            ledger, transaction, before = self.setup_record(d)
            failed = _publication_state_mutation(transaction, published=False, now=NOW)(
                before
            )
            broken = failed.evolve(
                now=NOW,
                generation=failed.generation + 1,
                convergence_history={
                    **failed.convergence_history,
                    "progress": [
                        {"event": "completed", "generation": transaction.generation - 1}
                    ],
                },
            )
            with self.assertRaises(ReviewInputError):
                self.mutation(ledger, transaction)(broken)

    def test_conflicting_marker_and_wrong_transaction_are_not_composable(self):
        with tempfile.TemporaryDirectory() as d:
            ledger, transaction, before = self.setup_record(d)
            marked = self.mutation(ledger, transaction)(before)
            conflicting = _admitted_blocker_progress_mutation(
                ledger,
                IDENTITY,
                transaction,
                blocker_set_sha256="b" * 64,
                blocker_count=1,
                suppress_publication=False,
                now=NOW,
            )
            with self.assertRaises(ReviewInputError):
                conflicting(marked)
            with self.assertRaises(ReviewInputError):
                self.mutation(ledger, transaction)(
                    before.evolve(
                        now=NOW, generation=before.generation + 1, transaction=None
                    )
                )
