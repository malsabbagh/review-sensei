"""Prospective DATA controls only; no command or source-issued lease probes."""

import unittest

from review_sensei_github import historical_journal_census as census
from review_sensei_github.packed_comment_census import CommentAllocationPlan

from review_sensei.errors import ReviewInputError


class HistoricalJournalOptinTests(unittest.TestCase):
    def test_one_unknown_event_no_fabricated_identity_and_retained_lifetime(self):
        known = {1: 66560, 2: 66560, 3: 200000, 4: 200000, 5: 160000}
        lifetime = CommentAllocationPlan(known, (32768,) * 16)
        descriptor = census.prospective_geometry(known, lifetime, 1417)
        self.assertIsNotNone(descriptor)
        self.assertEqual(descriptor["event_unknown"], [1417])
        self.assertEqual(descriptor["known"], list(map(list, known.items())))
        self.assertEqual(descriptor["metadata_unknown"], 1024)
        self.assertLessEqual(descriptor["complete_calls"], 3)
        self.assertEqual(descriptor["lifetime_unknown"], [32768] * 16)
        self.assertEqual(lifetime.unknown, (32768,) * 16)

    def test_geometry_nonfit_only_returns_none_before_effect(self):
        known = {i: 200000 for i in range(1, 11)}
        lifetime = CommentAllocationPlan(known, (32768,))
        self.assertIsNone(census.prospective_geometry(known, lifetime, 1417))
        with self.assertRaises(ReviewInputError):
            census.prospective_geometry({1: 1}, lifetime, 1417)
        with self.assertRaises(ReviewInputError):
            census.prospective_geometry(known, lifetime, True)

    def test_actual_membership_growth_and_old_unknowns_must_be_dominated(self):
        known = {1: 66560, 2: 66560}
        lifetime = CommentAllocationPlan(known, (32768, 65536))
        descriptor = census.prospective_geometry(known, lifetime, 1417)
        actual = CommentAllocationPlan({**known, 3: 1417}, lifetime.unknown)
        census.validate_actual_plan(descriptor, actual, (1, 2, 3))
        for changed in (
            CommentAllocationPlan({**known, 3: 1418}, lifetime.unknown),
            CommentAllocationPlan({**known, 3: 1417, 4: 10}, lifetime.unknown),
            CommentAllocationPlan({**known, 3: 1417}, (32768,)),
            CommentAllocationPlan({1: 66561, 2: 66560, 3: 1417}, lifetime.unknown),
        ):
            with self.assertRaises(ReviewInputError):
                census.validate_actual_plan(descriptor, changed, (1, 2, 3))

    def test_descriptor_requires_original_prepare_or_activation_frame(self):
        class Poison:
            def __getattribute__(self, key):
                raise AssertionError(key)

        with self.assertRaises(ReviewInputError):
            census.proposal_descriptor(Poison(), Poison(), Poison(), Poison())

    def test_source_tamper_is_not_numeric_fallback(self):
        from unittest.mock import patch

        plan = CommentAllocationPlan({1: 66560}, ())
        calls = []

        def fake(*args):
            calls.append(True)
            return 3

        with patch.object(CommentAllocationPlan, "worst_read_calls", fake):
            with self.assertRaises(ReviewInputError):
                census.prospective_geometry({1: 66560}, plan, 1417)
        self.assertEqual(calls, [])

    def test_event_cannot_alias_a_retained_member(self):
        lifetime = CommentAllocationPlan({1: 66560, 2: 66560}, (32768,))
        descriptor = census.prospective_geometry(dict(lifetime.known), lifetime, 1417)
        with self.assertRaises(ReviewInputError):
            census.validate_actual_plan(descriptor, lifetime, (1, 2, 2))

    def test_poisoned_plan_fields_and_instance_shadow_are_callback_free(self):
        calls = []

        class Poison:
            def items(self):
                calls.append("items")
                return ()

        for field, value in (
            ("known", Poison()),
            ("unknown", Poison()),
            ("worst_read_calls", lambda: calls.append("method")),
        ):
            plan = CommentAllocationPlan({1: 66560}, (32768,))
            object.__setattr__(plan, field, value)
            with self.assertRaises(ReviewInputError):
                census.prospective_geometry({1: 66560}, plan, 1417)
            with self.assertRaises(ReviewInputError):
                census.validate_actual_plan({}, plan, (1, 2))
        self.assertEqual(calls, [])

    def test_hostile_data_keys_and_metaclass_never_execute(self):
        calls = []

        class Meta(type):
            def __eq__(self, other):
                calls.append("meta")
                return False

        class Poison(metaclass=Meta):
            def __hash__(self):
                return 1

            def __eq__(self, other):
                calls.append("key")
                return False

        plan = CommentAllocationPlan({1: 66560, 2: 1417}, (32768,))
        for descriptor, membership in (
            ({"known": Poison()}, (1, 2)),
            ({Poison(): []}, (1, 2)),
            ({}, (Poison(), 2)),
        ):
            with self.assertRaises(ReviewInputError):
                census.validate_actual_plan(descriptor, plan, membership)
        self.assertEqual(calls, [])

    def test_event_interleaving_allowed_but_known_reorder_holds(self):
        lifetime = CommentAllocationPlan({1: 66560, 2: 66560}, (32768,))
        descriptor = census.prospective_geometry(dict(lifetime.known), lifetime, 1417)
        for known in ({3: 1417, 1: 66560, 2: 66560}, {1: 66560, 3: 1417, 2: 66560}):
            census.validate_actual_plan(
                descriptor, CommentAllocationPlan(known, lifetime.unknown), (1, 2, 3)
            )
        with self.assertRaises(ReviewInputError):
            census.validate_actual_plan(
                descriptor,
                CommentAllocationPlan({2: 66560, 1: 66560, 3: 1417}, lifetime.unknown),
                (1, 2, 3),
            )
