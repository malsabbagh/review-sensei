"""Current projection data preserves original funds and every old liability."""

import unittest

from review_sensei_github.current_command_projection import pending_accounting

from review_sensei.api_work import ApiRunAccounting, PendingApiRequest
from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from tests.test_api_work import accounting


class CurrentCommandProjectionTests(unittest.TestCase):
    def test_future_known_prefix_retains_every_unknown_and_metadata_bound(self):
        from itertools import combinations

        from review_sensei_github.current_command_projection import future_census_bound
        from review_sensei_github.packed_comment_census import CommentAllocationPlan

        from review_sensei.bounded_evidence import MAX_PART_BYTES

        original = CommentAllocationPlan(
            {11: 32000, 12: 24000, 13: 12000},
            (32000,) * 6,
            metadata_known={11: 120, 12: 140},
            metadata_unknown=373,
        )
        before = (tuple(original.known.items()), original.unknown)
        bound = future_census_bound(original, 2)
        for count in range(3):
            for positions in combinations(range(3 + count), count):
                old = iter(original.known.items())
                future = iter([(21 + i, MAX_PART_BYTES + 512) for i in range(count)])
                known = dict(
                    next(future) if i in positions else next(old)
                    for i in range(3 + count)
                )
                prefix = CommentAllocationPlan(
                    known,
                    original.unknown,
                    metadata_known=original.metadata_known,
                    metadata_unknown=original.metadata_unknown,
                )
                self.assertLessEqual(prefix.worst_read_calls(), bound)
        self.assertEqual((tuple(original.known.items()), original.unknown), before)

    def test_bounded_invocation_preserves_undispatched_funding_and_unknowns(self):
        state = accounting(limit=1152).reserve("ordinary", 380).reserve("terminal", 68)
        old = PendingApiRequest("retained-unknown", 100)
        state = state.dispatch("ordinary", old)
        before = canonical_bytes(state.to_document())
        document = pending_accounting(
            state,
            sequence=124,
            ordinary_id="ordinary",
            terminal_id="terminal",
            ordinary=159,
            terminal=17,
        )
        upper = ApiRunAccounting.from_document(document)
        self.assertEqual(upper.blocks[0].undispatched, 220)
        self.assertEqual(upper.blocks[1].undispatched, 51)
        self.assertIn(old, upper.blocks[0].pending)
        self.assertEqual(upper.remaining, state.remaining)
        self.assertEqual(upper.owner, state.owner)
        self.assertEqual(upper.configuration, state.configuration)
        self.assertEqual(canonical_bytes(state.to_document()), before)

    def test_all_pending_dominates_short_prefixes_and_confirmed_mixtures(self):
        state = accounting(limit=512).reserve("ordinary", 6).reserve("terminal", 4)
        old = PendingApiRequest("retained-unknown", 100)
        state = state.dispatch("ordinary", old)
        upper = ApiRunAccounting.from_document(
            pending_accounting(
                state,
                sequence=254,
                ordinary_id="ordinary",
                terminal_id="terminal",
                ordinary=4,
                terminal=3,
            )
        )
        bound = len(canonical_bytes(upper.to_document()))
        requests = [
            (block.block_id, request)
            for block in upper.blocks
            for request in block.pending
            if request != old
        ]
        prefix = state
        for block, request in requests:
            prefix = prefix.dispatch(block, request)
            self.assertLessEqual(len(canonical_bytes(prefix.to_document())), bound)
        for mask in range(1 << len(requests)):
            mixture = upper
            for index, (_, request) in enumerate(requests):
                if mask & (1 << index):
                    mixture = mixture.confirm(request)
            self.assertLessEqual(len(canonical_bytes(mixture.to_document())), bound)
            self.assertIn(old, mixture.blocks[0].pending)

    def test_no_refill_reuse_or_collision(self):
        state = accounting(limit=512).reserve("ordinary", 6).reserve("terminal", 4)
        for ordinary, terminal_id in ((7, "terminal"), (4, "ordinary"), (4, "absent")):
            with self.subTest(ordinary=ordinary, terminal=terminal_id):
                with self.assertRaises(ReviewInputError):
                    pending_accounting(
                        state,
                        sequence=0,
                        ordinary_id="ordinary",
                        terminal_id=terminal_id,
                        ordinary=ordinary,
                        terminal=1,
                    )
        old = PendingApiRequest(f"c-{state.owner.generation:x}-1", 100)
        state = state.dispatch("ordinary", old)
        with self.assertRaisesRegex(ReviewInputError, "collides"):
            pending_accounting(
                state,
                sequence=0,
                ordinary_id="ordinary",
                terminal_id="terminal",
                ordinary=1,
                terminal=1,
            )
