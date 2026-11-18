"""Detached original accounting transitions, never runtime funding authority."""

import unittest

from review_sensei_github.materialized_publication_projection import (
    _future_accounting,
    _maximum_publication_root,
)

from review_sensei.api_work import ApiRunAccounting, PendingApiRequest
from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from tests.test_api_work import accounting


class MaterializedAccountingProjectionTests(unittest.TestCase):
    def test_all_pending_bound_preserves_old_unknowns_and_dominates_mixtures(self):
        state = accounting(limit=512).reserve("ordinary", 6).reserve("terminal", 4)
        state = state.reserve("untouched-successor", 190)
        unknown = PendingApiRequest("old-unknown", 100)
        known = PendingApiRequest("old-known", 101)
        state = state.dispatch("ordinary", unknown)
        state = state.dispatch("ordinary", known).confirm(known)
        before = canonical_bytes(state.to_document())
        document, counts = _future_accounting(state, ("ordinary", "terminal"))
        self.assertEqual(counts, (4, 4))
        maximum = ApiRunAccounting.from_document(document)
        future = tuple(
            request
            for block in maximum.blocks
            for request in block.pending
            if request != unknown
        )
        self.assertEqual(len(future), 8)
        self.assertTrue(all(len(request.request_id) == 128 for request in future))
        bound = len(canonical_bytes(document))
        prefix = state
        for block in maximum.blocks[:2]:
            for request in block.pending:
                if request == unknown:
                    continue
                prefix = prefix.dispatch(block.block_id, request)
                self.assertLessEqual(len(canonical_bytes(prefix.to_document())), bound)
                if len(prefix.completed) % 2:
                    prefix = prefix.confirm(request)
                self.assertLessEqual(len(canonical_bytes(prefix.to_document())), bound)
        for mask in range(1 << len(future)):
            mixture = maximum
            for index, request in enumerate(future):
                if mask & (1 << index):
                    mixture = mixture.confirm(request)
            self.assertLessEqual(len(canonical_bytes(mixture.to_document())), bound)
            self.assertIn(unknown, mixture.blocks[0].pending)
            self.assertIn(known, mixture.completed)
            self.assertEqual(mixture.blocks[2], state.blocks[2])
        self.assertEqual(canonical_bytes(state.to_document()), before)
        self.assertEqual(maximum.configuration, state.configuration)
        self.assertEqual(maximum.owner, state.owner)

    def test_absent_or_reused_tranche_is_not_projection_authority(self):
        state = accounting(limit=512).reserve("ordinary", 6).reserve("terminal", 4)
        for blocks in (("ordinary", "ordinary"), ("ordinary", "missing")):
            with self.subTest(blocks=blocks), self.assertRaises(ReviewInputError):
                _future_accounting(state, blocks)

    def test_source_width_is_maximum_for_every_future_pending_receipt(self):
        state = accounting(limit=512).reserve("ordinary", 6).reserve("terminal", 4)
        state = state.reserve("untouched-successor", 190)
        unknown = PendingApiRequest("old-unknown", 100)
        state = state.dispatch("ordinary", unknown)
        before = canonical_bytes(state.to_document())
        document, counts = _future_accounting(
            state, ("ordinary", "terminal"), request_id_width=18
        )
        self.assertEqual(counts, (5, 4))
        maximum = ApiRunAccounting.from_document(document)
        future = tuple(
            request
            for block in maximum.blocks
            for request in block.pending
            if request != unknown
        )
        self.assertTrue(all(len(request.request_id) == 18 for request in future))
        bound = len(canonical_bytes(document))
        for mask in range(1 << len(future)):
            mixture = maximum
            for index, request in enumerate(future):
                if mask & (1 << index):
                    mixture = mixture.confirm(request)
            self.assertLessEqual(len(canonical_bytes(mixture.to_document())), bound)
            self.assertEqual(mixture.blocks[2], state.blocks[2])
            self.assertIn(unknown, mixture.blocks[0].pending)
        self.assertEqual(canonical_bytes(state.to_document()), before)
        for invalid in (0, 15, 129, True, None):
            with self.subTest(width=invalid), self.assertRaises(ReviewInputError):
                _future_accounting(
                    state, ("ordinary", "terminal"), request_id_width=invalid
                )

    def test_completion_scalar_widths_do_not_mutate_root_or_accounting(self):
        from review_sensei.models import TRANSACTION_PHASES
        from review_sensei.session import MAX_GENERATION

        state = accounting(limit=512).reserve("ordinary", 6).reserve("terminal", 4)
        upper, _ = _future_accounting(state, ("ordinary", "terminal"))
        document = {
            "api_run_accounting": state.to_document(),
            "generation": 9,
            "completed_initial_reviews": 9,
            "completed_verification_rounds": 9,
            "updated_at": "2026-11-14T00:00:00Z",
            "transaction": {"generation": 9, "phase": "publication_pending"},
        }
        before, upper_before = canonical_bytes(document), canonical_bytes(upper)
        maximum = _maximum_publication_root(document, upper)
        self.assertEqual(maximum["generation"], MAX_GENERATION)
        self.assertEqual(maximum["transaction"]["generation"], MAX_GENERATION)
        self.assertEqual(
            len(maximum["transaction"]["phase"]), max(map(len, TRANSACTION_PHASES))
        )
        self.assertEqual(
            maximum["api_run_accounting"]["terminal_transaction"], "f" * 64
        )
        self.assertEqual(maximum["updated_at"], "9999-12-31T23:59:59.999999Z")
        self.assertEqual(maximum["completed_initial_reviews"], 10)
        self.assertEqual(maximum["completed_verification_rounds"], 10)
        self.assertEqual(canonical_bytes(document), before)
        self.assertEqual(canonical_bytes(upper), upper_before)
        longer = "2026-11-14T00:00:00.123456+00:00"
        document["updated_at"] = longer
        self.assertEqual(
            _maximum_publication_root(document, upper)["updated_at"], longer
        )
