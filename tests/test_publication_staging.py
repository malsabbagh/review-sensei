"""One durable outstanding publication effect; absence never retries it."""

import copy
import unittest

from review_sensei.errors import ReviewInputError
from review_sensei.publication_staging import PublicationStaging


class PublicationStagingTests(unittest.TestCase):
    def test_required_manifest_is_a_separate_durable_unknown_frontier(self):
        from dataclasses import replace

        initial = replace(self.initial(0), manifest_required=True)
        with self.assertRaises(ReviewInputError):
            initial.attempt_final()
        attempted = initial.attempt_manifest()
        initial.require_successor(attempted)
        self.assertFalse(attempted.complete)
        for operation in (attempted.attempt_manifest, attempted.attempt_final):
            with self.assertRaises(ReviewInputError):
                operation()
        confirmed = attempted.confirm_manifest(
            native_id="issue:7", content_sha256="d" * 64
        )
        attempted.require_successor(confirmed)
        self.assertEqual(
            confirmed.confirm_manifest(native_id="issue:7", content_sha256="d" * 64),
            confirmed,
        )
        with self.assertRaises(ReviewInputError):
            confirmed.confirm_manifest(native_id="issue:8", content_sha256="d" * 64)
        final = confirmed.attempt_final()
        confirmed.require_successor(final)
        complete = final.confirm_final(native_id="8", content_sha256="e" * 64)
        self.assertTrue(complete.complete)
        self.assertEqual(
            PublicationStaging.from_document(complete.to_document()), complete
        )

    def test_stale_callers_cannot_share_an_attempt_claim(self):
        from test_review_transaction import IDENTITY, NOW, _checkpoint

        from review_sensei.publication_staging import checkpoint_publication_staging
        from review_sensei.session import InMemorySessionLedger

        ledger = InMemorySessionLedger()
        transaction = _checkpoint(ledger).transaction
        initial = PublicationStaging.prepare(
            transaction_id=transaction.transaction_id,
            result_sha256=transaction.result_sha256,
            plan_sha256="c" * 64,
            part_count=1,
        )
        checkpoint_publication_staging(
            ledger, IDENTITY, transaction, expected=None, after=initial, now=NOW
        )
        first, second = initial.attempt_part(0), initial.attempt_part(0)
        self.assertNotEqual(first.attempt_tokens, second.attempt_tokens)
        checkpoint_publication_staging(
            ledger, IDENTITY, transaction, expected=initial, after=first, now=NOW
        )
        # The same unique proposal can reconcile a lost storage ACK.
        self.assertEqual(
            checkpoint_publication_staging(
                ledger, IDENTITY, transaction, expected=initial, after=first, now=NOW
            ),
            first,
        )
        # A separately minted claim from the same stale frontier cannot POST.
        with self.assertRaisesRegex(ReviewInputError, "expected frontier changed"):
            checkpoint_publication_staging(
                ledger, IDENTITY, transaction, expected=initial, after=second, now=NOW
            )
        settled = first.confirm_part(0, native_id="9", content_sha256="d" * 64)
        checkpoint_publication_staging(
            ledger, IDENTITY, transaction, expected=first, after=settled, now=NOW
        )
        final_a, final_b = settled.attempt_final(), settled.attempt_final()
        self.assertNotEqual(final_a.final_token, final_b.final_token)
        checkpoint_publication_staging(
            ledger, IDENTITY, transaction, expected=settled, after=final_a, now=NOW
        )
        with self.assertRaisesRegex(ReviewInputError, "expected frontier changed"):
            checkpoint_publication_staging(
                ledger, IDENTITY, transaction, expected=settled, after=final_b, now=NOW
            )

    def test_actual_ledger_round_trip_preserves_unknown_and_blocks_false_success(self):
        from test_review_transaction import IDENTITY, NOW, _checkpoint

        from review_sensei.publication_staging import checkpoint_publication_staging
        from review_sensei.session import (
            InMemorySessionLedger,
            SessionRecord,
            complete_review_publication,
        )

        ledger = InMemorySessionLedger()
        result = _checkpoint(ledger)
        transaction = result.transaction
        state = PublicationStaging.prepare(
            transaction_id=transaction.transaction_id,
            result_sha256=transaction.result_sha256,
            plan_sha256="c" * 64,
            part_count=2,
        )
        checkpoint_publication_staging(
            ledger, IDENTITY, transaction, expected=None, after=state, now=NOW
        )
        attempted = state.attempt_part(0)
        checkpoint_publication_staging(
            ledger, IDENTITY, transaction, expected=state, after=attempted, now=NOW
        )
        record = ledger.load(IDENTITY, now=NOW).record
        self.assertEqual(SessionRecord.from_dict(record.to_dict()), record)
        self.assertEqual(
            PublicationStaging.from_document(record.publication_staging), attempted
        )
        for mutation in (
            lambda: record.evolve(now=NOW, publication_staging=None),
            lambda: record.evolve(now=NOW, transaction=None),
            lambda: complete_review_publication(
                ledger, IDENTITY, transaction, published=True, now=NOW
            ),
        ):
            with self.subTest(mutation=mutation), self.assertRaises(ReviewInputError):
                mutation()
        with self.assertRaises(ReviewInputError):
            checkpoint_publication_staging(
                ledger,
                IDENTITY,
                transaction,
                expected=state,
                after=attempted.confirm_part(0, native_id="9", content_sha256="d" * 64),
                now=NOW,
            )
        confirmed = checkpoint_publication_staging(
            ledger,
            IDENTITY,
            transaction,
            expected=attempted,
            after=attempted.confirm_part(0, native_id="9", content_sha256="d" * 64),
            now=NOW,
        )
        self.assertEqual(confirmed.receipts[0].native_id, "9")

    def initial(self, count=3):
        return PublicationStaging.prepare(
            transaction_id="a" * 64,
            result_sha256="b" * 64,
            plan_sha256="c" * 64,
            part_count=count,
        )

    def test_unknown_part_never_allows_another_part_or_final_dispatch(self):
        initial = self.initial()
        attempted = initial.attempt_part(0)
        self.assertEqual(attempted.outstanding_part, 0)
        for operation in (
            lambda: attempted.attempt_part(0),
            lambda: attempted.attempt_part(1),
            attempted.attempt_final,
        ):
            with self.subTest(operation=operation), self.assertRaises(ReviewInputError):
                operation()
        initial.require_successor(attempted)
        with self.assertRaises(ReviewInputError):
            attempted.require_successor(initial)
        # A cold load preserves the original unknown attempt byte for byte.
        self.assertEqual(
            PublicationStaging.from_document(attempted.to_document()), attempted
        )

    def test_both_original_and_late_exact_confirmations_are_idempotent(self):
        state = self.initial(2)
        for ordinal, native_id in enumerate(("921", "922")):
            attempted = state.attempt_part(ordinal)
            confirmed = attempted.confirm_part(
                ordinal, native_id=native_id, content_sha256=str(ordinal) * 64
            )
            attempted.require_successor(confirmed)
            self.assertEqual(
                confirmed.confirm_part(
                    ordinal, native_id=native_id, content_sha256=str(ordinal) * 64
                ),
                confirmed,
            )
            for replacement in (
                dict(native_id="999", content_sha256=str(ordinal) * 64),
                dict(native_id=native_id, content_sha256="f" * 64),
            ):
                with (
                    self.subTest(replacement=replacement),
                    self.assertRaises(ReviewInputError),
                ):
                    confirmed.confirm_part(ordinal, **replacement)
            state = confirmed
        final = state.attempt_final()
        self.assertTrue(final.final_attempted)
        self.assertFalse(final.complete)
        with self.assertRaises(ReviewInputError):
            final.attempt_final()
        complete = final.confirm_final(native_id="923", content_sha256="e" * 64)
        final.require_successor(complete)
        self.assertTrue(complete.complete)

    def test_false_receipts_changed_plans_and_skipped_frontiers_are_rejected(self):
        initial = self.initial()
        invalid = []
        for field, value in (
            ("part_count", 9),
            ("attempted_parts", 2),
            ("final_attempted", True),
            ("final_receipt", {"native_id": "9", "content_sha256": "d" * 64}),
        ):
            document = initial.to_document()
            document[field] = value
            invalid.append(document)
        document = initial.to_document()
        document["receipts"] = [{"native_id": "8", "content_sha256": "d" * 64}]
        invalid.append(document)
        document = initial.to_document()
        document["unknown_extra"] = True
        invalid.append(document)
        for document in invalid:
            with self.subTest(document=document), self.assertRaises(ReviewInputError):
                PublicationStaging.from_document(document)
        altered = copy.deepcopy(initial.to_document())
        altered["plan_sha256"] = "d" * 64
        with self.assertRaises(ReviewInputError):
            initial.require_successor(PublicationStaging.from_document(altered))

    def test_empty_inline_plan_still_requires_the_final_attempt_receipt(self):
        initial = self.initial(0)
        self.assertFalse(initial.complete)
        with self.assertRaises(ReviewInputError):
            initial.confirm_final(native_id="8", content_sha256="d" * 64)
        complete = initial.attempt_final().confirm_final(
            native_id="8", content_sha256="d" * 64
        )
        self.assertTrue(complete.complete)
        self.assertLess(len(str(complete.to_document())), 4096)
