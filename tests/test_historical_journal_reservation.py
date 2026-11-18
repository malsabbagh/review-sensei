"""Reservation selection controls, not genuine lifecycle issuance evidence."""

import ast
import unittest
from pathlib import Path

from review_sensei_github import quarantine_census as census
from review_sensei_github.packed_comment_census import CommentAllocationPlan

from review_sensei.errors import ReviewInputError


class HistoricalJournalReservationTests(unittest.TestCase):
    def test_source_selection_preserves_lifetime_and_other_readers(self):
        # Extract only the pure selection tail. No registry/authority is minted.
        source = ast.parse(Path(census.__file__).read_text())
        function = next(
            n
            for n in source.body
            if isinstance(n, ast.FunctionDef) and n.name == "reservation_plan"
        )
        selection = ast.FunctionDef(
            name="select",
            args=ast.arguments(
                posonlyargs=[],
                args=[
                    ast.arg(arg="scope"),
                    ast.arg(arg="issuer"),
                    ast.arg(arg="fields"),
                ],
                kwonlyargs=[],
                kw_defaults=[],
                defaults=[],
            ),
            body=function.body[-2:],
            decorator_list=[],
        )
        namespace = {"_LifecycleCapacity": census._LifecycleCapacity}
        exec(
            compile(
                ast.fix_missing_locations(
                    ast.Module(body=[selection], type_ignores=[])
                ),
                "<reservation-selection>",
                "exec",
            ),
            namespace,
        )
        known = {i: 11000 for i in range(1, 65)}
        lifetime = CommentAllocationPlan(known, (43008,) * 16)
        read = CommentAllocationPlan(known, ())
        self.assertEqual(lifetime.worst_read_calls(), 5)
        self.assertEqual(read.worst_read_calls(), 3)
        for issuer, expected in (
            ("historical-journal", lifetime),
            ("materialized", read),
            (None, read),
        ):
            scope = census._LifecycleCapacity(
                None,
                None,
                None,
                None,
                "",
                b"",
                b"",
                lifetime,
                0,
                read_plan=read,
                read_issuer=issuer,
            )
            self.assertIs(namespace["select"](scope, issuer, vars(scope)), expected)
            self.assertIs(census._LifecycleCapacity.enumeration_plan(scope), read)

    def test_unissued_and_hostile_issuer_refuse_without_callbacks(self):
        calls = []

        class Poison:
            def __eq__(self, other):
                calls.append(True)
                return True

        plan = CommentAllocationPlan({1: 66560}, ())
        scope = census._LifecycleCapacity(None, None, None, None, "", b"", b"", plan, 0)
        with self.assertRaises(ReviewInputError):
            census.reservation_plan(scope)
        object.__setattr__(scope, "read_issuer", Poison())
        with self.assertRaises(ReviewInputError):
            census.reservation_plan(scope)
        self.assertEqual(calls, [])

    def test_all_three_reservation_sites_use_helper(self):
        source = ast.parse(Path(census.__file__).read_text())
        for name in ("lifecycle_checkpoint_operation", "lifecycle_checkpoint_pricing"):
            function = next(
                n
                for n in source.body
                if isinstance(n, ast.FunctionDef) and n.name == name
            )
            self.assertTrue(
                any(
                    isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Name)
                    and n.func.id == "reservation_plan"
                    for n in ast.walk(function)
                )
            )
        path = Path(census.__file__).with_name("api_run_requests.py")
        source = ast.parse(path.read_text())
        function = next(
            n
            for n in ast.walk(source)
            if isinstance(n, ast.FunctionDef)
            and n.name == "replace_finding_lifecycle_parts"
        )
        self.assertTrue(
            any(
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id == "reservation_plan"
                for n in ast.walk(function)
            )
        )

    def test_registry_transition_data_without_issuing_a_scope(self):
        seal = ("original", 123)
        changed = ("changed", 123)
        self.assertTrue(census._reservation_state_matches(seal, seal, None))
        self.assertTrue(
            census._reservation_state_matches(seal, None, (seal, None, None))
        )
        for issued, active in (
            (None, None),
            (changed, None),
            (None, (changed, None, None)),
            (seal, (seal, None, None)),
            (None, (seal,)),
            (list(seal), None),
            (None, [seal, None, None]),
        ):
            self.assertFalse(census._reservation_state_matches(seal, issued, active))

    def test_issued_copy_remains_unissued(self):
        from dataclasses import replace

        plan = CommentAllocationPlan({1: 66560}, ())
        scope = census._LifecycleCapacity(None, None, None, None, "", b"", b"", plan, 0)
        for candidate in (scope, replace(scope)):
            with self.assertRaises(ReviewInputError):
                census.reservation_plan(candidate)
