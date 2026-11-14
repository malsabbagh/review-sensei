"""Root preservation accepts explicit v3; all old native writers still refuse."""

import copy
import unittest
from dataclasses import replace

from review_sensei.errors import ReviewInputError
from review_sensei.grouped_publication_declarations import GroupedDeclarations
from review_sensei.publication_staging import (
    PublicationStaging,
    checkpoint_publication_staging,
    normalize_staging,
    require_staging_successor,
)
from review_sensei.session import InMemorySessionLedger, SessionRecord
from tests.test_review_transaction import IDENTITY, NOW, _checkpoint


class GroupedStagingPreservationTests(unittest.TestCase):
    def inputs(self):
        ledger = InMemorySessionLedger()
        result = _checkpoint(ledger)
        record = ledger.load(IDENTITY, now=NOW).record
        tx = result.transaction
        declaration = GroupedDeclarations(
            tx.transaction_id,
            tx.result_sha256,
            "c" * 64,
            ("d" * 32, "e" * 32, "f" * 32),
        )
        return ledger, record, declaration

    def test_roundtrip_evolve_schema_and_old_writer_refusal(self):
        ledger, record, declaration = self.inputs()
        child = record.evolve(
            now=NOW,
            generation=record.generation + 1,
            publication_staging=declaration.to_document(),
        )
        self.assertEqual(SessionRecord.from_dict(child.to_dict()), child)
        next_record = child.evolve(now=NOW, generation=child.generation + 1)
        self.assertEqual(next_record.publication_staging, child.publication_staging)
        from review_sensei.schemas import validate_public_document

        validate_public_document(child.to_dict(), "session-record")
        with self.assertRaises(ReviewInputError):
            PublicationStaging.from_document(child.publication_staging)
        # Existing native writer receives no implicit v3 dispatch route.
        ledger.replace(IDENTITY, lambda current: child, now=NOW)
        old = PublicationStaging.prepare(
            transaction_id=declaration.transaction_id,
            result_sha256=declaration.result_sha256,
            plan_sha256="c" * 64,
            part_count=1,
        )
        with self.assertRaises(ReviewInputError):
            checkpoint_publication_staging(
                ledger, IDENTITY, record.transaction, expected=None, after=old, now=NOW
            )

    def test_downgrade_discard_context_and_unknown_variants_fail(self):
        _, record, declaration = self.inputs()
        doc = declaration.to_document()
        old = PublicationStaging.prepare(
            transaction_id=declaration.transaction_id,
            result_sha256=declaration.result_sha256,
            plan_sha256="c" * 64,
            part_count=1,
        ).to_document()
        for before, after in (
            (doc, None),
            (doc, old),
            (old, doc),
            (doc, replace(declaration, transaction_id="a" * 64).to_document()),
        ):
            with self.subTest(after=after), self.assertRaises(ReviewInputError):
                require_staging_successor(before, after)
        with self.assertRaises(ReviewInputError):
            record.evolve(
                now=NOW,
                generation=record.generation + 1,
                publication_staging=replace(
                    declaration, result_sha256="a" * 64
                ).to_document(),
            )
        for delta in (
            {"schema_version": "publication-staging-v9"},
            {"declarations": []},
            {"extra": True},
        ):
            changed = {**copy.deepcopy(doc), **delta}
            with self.subTest(delta=delta), self.assertRaises(ReviewInputError):
                normalize_staging(changed)
        self.assertEqual(normalize_staging(old), old)
