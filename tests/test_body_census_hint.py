"""Scheduling DATA cannot replace complete original allocation validation."""

import copy
import unittest

from review_sensei_github import body_census_hint as hint
from review_sensei_github.packed_comment_census import (
    CommentAllocationPlan,
    fetch_packed_comments,
    wire,
)
from test_packed_comment_census import PackedBackend
from test_quarantine_census import IDENTITY

from review_sensei.errors import ReviewInputError

ARCHIVE = {"target_isolation": {"contract": "body-limit-prepublication-v1"}}


class BodyCensusHintTests(unittest.TestCase):
    def prepare(self):
        backend = PackedBackend(["a" * 1000, "b" * 2000])
        plan = CommentAllocationPlan(
            {
                int(row["fullDatabaseId"]): wire({"body": row["body"]})
                for row in backend.rows
            },
            (32768,) * 16,
        )
        return backend, plan

    def test_initial_paid_walk_then_four_fresh_queries_and_lexical_retirement(self):
        backend, plan = self.prepare()
        snapshot = (tuple(plan.known.items()), plan.unknown)
        with hint.window():
            original = fetch_packed_comments(backend.ledger(), IDENTITY, plan=plan)
            initial = len(backend.physical)
            self.assertGreater(initial, 1)
            hint.enable(ARCHIVE)
            for _ in range(4):
                self.assertEqual(
                    fetch_packed_comments(backend.ledger(), IDENTITY, plan=plan),
                    original,
                )
            self.assertEqual(len(backend.physical), initial + 4)
            self.assertTrue(
                all(
                    row["variables"]["first"] == 3 for row in backend.physical[initial:]
                )
            )
        self.assertIsNone(hint.first())
        self.assertEqual((tuple(plan.known.items()), plan.unknown), snapshot)

    def test_forged_hint_still_cannot_omit_mandatory_identity_or_return_prefix(self):
        for change in ("missing", "growth", "order", "body", "metadata", "duplicate"):
            with self.subTest(change=change), hint.window():
                backend, plan = self.prepare()
                # Explicitly forged hint: it confers no authority.
                hint._HINT.set((True, (2, 1)))
                if change == "missing":
                    backend.rows.pop()
                elif change == "growth":
                    backend.rows.append(backend.node(3, "new"))
                elif change == "order":
                    backend.rows.reverse()
                elif change == "body":
                    backend.rows[0]["body"] += "x"
                elif change == "metadata":
                    backend.rows[0]["extra"] = "x" * 1100
                else:
                    backend.rows[1] = copy.deepcopy(backend.rows[0])
                with self.assertRaises(ReviewInputError):
                    fetch_packed_comments(backend.ledger(), IDENTITY, plan=plan)
                self.assertEqual(len(backend.physical), 1)

    def test_published_tail_hint_keeps_full_membership_and_exact_contract_opt_in(self):
        for contract, enabled in (
            ("published-tail-reply-isolation-v1", True),
            ("failed-published-tail-isolation-v1", False),
            ("body-limit-execution-isolation-v1", False),
            ("published-tail-reply-isolation-v2", False),
        ):
            with self.subTest(contract=contract), hint.window():
                backend, plan = self.prepare()
                snapshot = (tuple(plan.known.items()), plan.unknown)
                original = fetch_packed_comments(backend.ledger(), IDENTITY, plan=plan)
                before = len(backend.physical)
                hint.enable({"target_isolation": {"contract": contract}})
                self.assertEqual(
                    fetch_packed_comments(backend.ledger(), IDENTITY, plan=plan),
                    original,
                )
                actual = len(backend.physical) - before
                if enabled:
                    self.assertEqual(actual, 1)
                else:
                    self.assertGreater(actual, 1)
                self.assertEqual((tuple(plan.known.items()), plan.unknown), snapshot)

    def test_unknown_slots_are_matched_without_consuming_original_promises(self):
        backend, original = self.prepare()
        plan = CommentAllocationPlan({next(iter(original.known)): 1011}, (2011, 32768))
        with hint.window():
            hint._HINT.set((True, (2, 1)))
            self.assertEqual(
                len(fetch_packed_comments(backend.ledger(), IDENTITY, plan=plan)), 2
            )
        self.assertEqual(plan.unknown, (2011, 32768))
        short = CommentAllocationPlan(plan.known, (100,))
        with hint.window():
            hint._HINT.set((True, (2, 1)))
            with self.assertRaisesRegex(ReviewInputError, "allocation"):
                fetch_packed_comments(backend.ledger(), IDENTITY, plan=short)

    def test_ineligible_and_reset_hints_use_original_route(self):
        for value in (None, (False, (2, 1)), (True, (100, 1)), (True, (2, 524289))):
            with self.subTest(value=value), hint.window():
                backend, plan = self.prepare()
                hint._HINT.set(value)
                self.assertEqual(
                    len(fetch_packed_comments(backend.ledger(), IDENTITY, plan=plan)), 2
                )
                self.assertGreater(len(backend.physical), 1)

    def test_response_failure_has_no_fallback_and_context_expires(self):
        backend, plan = self.prepare()
        backend.change = lambda value, connection, count: connection.update(
            totalCount=99
        )
        with self.assertRaises(ReviewInputError), hint.window():
            hint._HINT.set((True, (2, 1)))
            fetch_packed_comments(backend.ledger(), IDENTITY, plan=plan)
        self.assertEqual(len(backend.physical), 1)
        self.assertIsNone(hint.first())

    def test_plan_mutation_during_response_refuses(self):
        backend, plan = self.prepare()

        def change(value, connection, count):
            object.__setattr__(plan, "unknown", ())

        backend.change = change
        with hint.window():
            hint._HINT.set((True, (2, 1)))
            with self.assertRaisesRegex(ReviewInputError, "plan changed"):
                fetch_packed_comments(backend.ledger(), IDENTITY, plan=plan)
        self.assertEqual(len(backend.physical), 1)

    def test_hint_metaclass_and_response_objects_do_not_execute_callbacks(self):
        from review_sensei_github.packed_comment_census import _single_plain

        callbacks = []

        class Meta(type):
            def __eq__(cls, other):
                callbacks.append("eq")
                return False

        class Hostile(metaclass=Meta):
            pass

        with hint.window():
            hint._HINT.set((True, (Hostile(), 1)))
            self.assertIsNone(hint.first())
            with self.assertRaisesRegex(ReviewInputError, "not plain"):
                _single_plain({"data": Hostile()})
        self.assertEqual(callbacks, [])
