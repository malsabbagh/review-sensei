"""Inert finite-ID data and token negatives, never a writer qualification."""

import json
import unittest
from unittest.mock import patch

from review_sensei_github import guarded_hot_namespace as namespace
from review_sensei_github import guarded_hot_namespace_projection as projection
from test_api_work import accounting

from review_sensei.api_work import (
    MAX_API_REQUEST_LIMIT,
    ApiRunAccounting,
    PendingApiRequest,
)
from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError


class GuardedHotNamespaceProjectionTests(unittest.TestCase):
    def test_all_initial_future_slots_and_old_liabilities_are_preserved(self):
        old = PendingApiRequest("x" * 128, 12)
        collision = PendingApiRequest("p" + "0" * 30, 13)
        completed = PendingApiRequest("already-confirmed", 14)
        protected = PendingApiRequest("protected-unknown", 15)
        state = (
            accounting(limit=512, prior=7)
            .reserve("ordinary", 93)
            .reserve("terminal", 50)
            .reserve("successor", 190)
        )
        state = state.dispatch("ordinary", old).dispatch("ordinary", collision)
        state = state.dispatch("ordinary", completed).confirm(completed)
        state = state.dispatch("successor", protected)
        original = canonical_bytes(state.to_document())
        bound = ApiRunAccounting.from_document(
            json.loads(
                projection.project_initial_accounting(
                    original, ("ordinary", "terminal")
                )
            )
        )
        self.assertEqual(bound.completed, state.completed)
        self.assertEqual(bound.blocks[2], state.blocks[2])
        self.assertEqual(bound.prior_nonstartup_liability, 7)
        self.assertEqual(bound.startup, state.startup)
        self.assertEqual(bound.startup_used, state.startup_used)
        self.assertEqual(bound.remaining, state.remaining)
        self.assertEqual(bound.owner, state.owner)
        self.assertEqual(bound.configuration, state.configuration)
        for index, count in ((0, 90), (1, 50)):
            before, after = state.blocks[index], bound.blocks[index]
            self.assertEqual(after.pending[: len(before.pending)], before.pending)
            added = after.pending[len(before.pending) :]
            self.assertEqual(len(added), count)
            self.assertEqual(after.undispatched, 0)
            self.assertTrue(
                all(
                    len(item.request_id) == 31
                    and item.deadline_unix_ms == MAX_API_REQUEST_LIMIT
                    for item in added
                )
            )
            self.assertNotIn(collision, added)
        self.assertEqual(canonical_bytes(state.to_document()), original)

    def test_unissued_and_callback_bearing_tokens_never_execute(self):
        calls = []

        class Bomb:
            def __hash__(self):
                calls.append("hash")
                raise AssertionError("callback")

        for token in (object(), Bomb()):
            with self.assertRaises(ReviewInputError):
                namespace.sizing_bound(token, object(), object())
        self.assertEqual(calls, [])

    def test_projection_source_replacement_refuses_before_callback(self):
        from review_sensei import api_work

        with patch.object(
            api_work.ApiRunAccounting,
            "from_document",
            side_effect=AssertionError("callback"),
        ) as fake:
            with self.assertRaises(ReviewInputError):
                projection.project_initial_accounting(b"{}", ("ordinary", "terminal"))
            fake.assert_not_called()


class GuardedHotNamespaceProjectionSourceTests(unittest.TestCase):
    def test_actual_original_begin_issues_only_inert_full_initial_projection(self):
        import copy

        from test_guarded_hot_namespace_held_route import (
            GuardedHotNamespaceHeldRouteTests,
        )

        outer = self
        captured = []

        class ObservedHeldRoute(GuardedHotNamespaceHeldRouteTests):
            def inspect_namespace_entry(self, entry):
                token = entry["sizing_token"]
                captured.append((token, entry["caller"], entry["record"]))
                initial = ApiRunAccounting.from_document(
                    json.loads(entry["initial_bytes"])
                )
                bound = ApiRunAccounting.from_document(
                    json.loads(entry["sizing_observed"])
                )
                outer.assertEqual(entry["sizing_observed"], entry["sizing_bytes"])
                outer.assertEqual(bound.completed, initial.completed)
                outer.assertEqual(bound.owner, initial.owner)
                outer.assertEqual(bound.configuration, initial.configuration)
                outer.assertEqual(
                    tuple(
                        next(
                            block for block in initial.blocks if block.block_id == key
                        ).undispatched
                        for key in entry["blocks"]
                    ),
                    (90, 50),
                )
                for before, after in zip(initial.blocks, bound.blocks, strict=True):
                    if before.block_id in entry["blocks"]:
                        outer.assertEqual(
                            after.pending[: len(before.pending)], before.pending
                        )
                        added = after.pending[len(before.pending) :]
                        outer.assertEqual(len(added), before.undispatched)
                        outer.assertEqual(after.undispatched, 0)
                        outer.assertTrue(
                            all(
                                len(item.request_id) == 31
                                and item.deadline_unix_ms == MAX_API_REQUEST_LIMIT
                                for item in added
                            )
                        )
                    else:
                        outer.assertEqual(after, before)
                # Copied tokens have no registry identity. Even the actual
                # token is not callable by an external observer/consumer.
                for candidate in (copy.copy(token), token):
                    with outer.assertRaises(ReviewInputError):
                        namespace.sizing_bound(
                            candidate, entry["caller"], entry["record"]
                        )

        case = ObservedHeldRoute(
            "test_original_held512_confirms_root_and_preserves_namespace_until_cleanup"
        )
        case.test_original_held512_confirms_root_and_preserves_namespace_until_cleanup()
        outer.assertEqual(len(captured), 1)
        with outer.assertRaises(ReviewInputError):
            namespace.sizing_bound(*captured[0])
