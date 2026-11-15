"""Pure geometry and refusal controls; no command/lease qualification."""

import unittest

from review_sensei_github import historical_journal_census as census
from review_sensei_github.packed_comment_census import CommentAllocationPlan

from review_sensei.errors import ReviewInputError


class HistoricalJournalGeometryTests(unittest.TestCase):
    def test_separate_geometry_preserves_lifetime_unknowns_and_bounds(self):
        lifetime = CommentAllocationPlan({1: 66560, 2: 66560, 3: 1417}, (65536,) * 16)
        selected = census.project(lifetime, (1, 2, 3))
        self.assertEqual(lifetime.unknown, (65536,) * 16)
        self.assertEqual(dict(selected.known), dict(lifetime.known))
        self.assertEqual(selected.unknown, ())
        self.assertLessEqual(selected.worst_read_calls(), 3)

    def test_missing_duplicate_foreign_members_refuse(self):
        plan = CommentAllocationPlan({1: 66560, 2: 66560}, ())
        for members in ((1,), (1, 1), (1, 3)):
            with self.subTest(members=members), self.assertRaises(ReviewInputError):
                census.project(plan, members)
        for rows in ([{"id": 1}], [{"id": 1}, {"id": 3}], [{"id": 1}, {"id": 1}]):
            with self.subTest(rows=rows), self.assertRaises(ReviewInputError):
                census.validate_membership(plan, rows)

    def test_positive_growth_not_replaced_with_observed_bytes(self):
        plan = CommentAllocationPlan({i: 200000 for i in range(1, 11)}, ())
        with self.assertRaisesRegex(ReviewInputError, "exceeds C3"):
            census.project(plan, tuple(plan.known))

    def test_foreign_scope_and_out_of_phase_completion_do_not_follow_callbacks(self):
        class Poison:
            def __getattribute__(self, key):
                raise AssertionError(key)

            def __hash__(self):
                raise AssertionError("hash")

        for operation in (census.require_scope, census.begin):
            with self.assertRaises(ReviewInputError):
                operation(Poison())
        with self.assertRaises(ReviewInputError):
            census.finish(Poison(), [])

    def test_hostile_row_does_not_execute(self):
        class Poison:
            def __getitem__(self, key):
                raise AssertionError(key)

        with self.assertRaises(ReviewInputError):
            census.validate_membership(CommentAllocationPlan({1: 10}, ()), [Poison()])

    def test_unissued_exact_scope_and_copy_refuse_before_owner_access(self):
        from dataclasses import replace

        from review_sensei_github.quarantine_census import _LifecycleCapacity

        marker = object()
        plan = CommentAllocationPlan({1: 66560}, ())
        scope = _LifecycleCapacity(
            marker,
            marker,
            marker,
            marker,
            "root",
            b"",
            b"",
            plan,
            0,
            read_plan=plan,
            read_issuer="historical-journal",
        )
        for candidate in (scope, replace(scope)):
            with self.assertRaisesRegex(ReviewInputError, "copied or changed"):
                census.require_scope(candidate)

    def test_metadata_and_body_maxima_retained_without_aliasing(self):
        known, metadata = {1: 66560}, {1: 373}
        lifetime = CommentAllocationPlan(known, (65536,), metadata_known=metadata)
        selected = census.project(lifetime, (1,))
        known[1], metadata[1] = 1, 1
        self.assertEqual(selected.known[1], 66560)
        self.assertEqual(selected.metadata_known[1], 373)
        self.assertEqual(lifetime.unknown, (65536,))

    def test_substituted_packed_export_never_called(self):
        from unittest.mock import patch

        from review_sensei_github import packed_comment_census

        calls = []

        def fake(*args, **kwargs):
            calls.append(True)
            return [{"id": 1}]

        with patch.object(packed_comment_census, "fetch_packed_comments", fake):
            with self.assertRaises(ReviewInputError):
                census.packed_reader()()
        self.assertEqual(calls, [])

    def test_late_packed_export_replacement_refuses_without_callback(self):
        from unittest.mock import patch

        from review_sensei_github import packed_comment_census

        original = census.packed_reader()
        calls = []

        def fake(*args, **kwargs):
            calls.append(True)
            return [{"id": 1}]

        with patch.object(packed_comment_census, "fetch_packed_comments", fake):
            with self.assertRaises(ReviewInputError):
                census.packed_reader(original)
        self.assertEqual(calls, [])
        self.assertIs(census.packed_reader(original), original)

    def test_substituted_phase_export_never_called(self):
        from unittest.mock import patch

        from review_sensei_github import quarantine_command_phase
        from review_sensei_github import quarantine_historical_execution as historical

        calls = []

        def fake(*args, **kwargs):
            calls.append(True)
            return None

        with patch.object(quarantine_command_phase, "_optional_frame", fake):
            with self.assertRaises(ReviewInputError):
                historical._journal_phase_reader()()
        self.assertEqual(calls, [])

    def test_substituted_geometry_method_never_called(self):
        from unittest.mock import patch

        plan = CommentAllocationPlan({1: 66560}, (65536,))
        calls = []

        def fake(*args, **kwargs):
            calls.append(True)
            return 3

        with patch.object(CommentAllocationPlan, "worst_read_calls", fake):
            with self.assertRaises(ReviewInputError):
                census.project(plan, (1,))
        self.assertEqual(calls, [])
