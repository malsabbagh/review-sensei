"""Pure identity/ordering controls, not live scope or command qualification."""

import ast
import inspect
import unittest

from review_sensei_github import quarantine_command_checkpoint as checkpoint
from review_sensei_github.packed_comment_census import CommentAllocationPlan


class ColdCensusPlanIdentityTests(unittest.TestCase):
    def test_lifetime_and_exact_historical_reader_only(self):
        lifetime = CommentAllocationPlan({1: 66560}, (65536,) * 16)
        selected = CommentAllocationPlan(lifetime.known, ())
        self.assertTrue(
            checkpoint._matches_census_plan(
                lifetime, lifetime, selected, "historical-journal"
            )
        )
        self.assertTrue(
            checkpoint._matches_census_plan(
                selected, lifetime, selected, "historical-journal"
            )
        )
        self.assertEqual(lifetime.unknown, (65536,) * 16)
        copied = CommentAllocationPlan(selected.known, ())
        self.assertFalse(
            checkpoint._matches_census_plan(
                copied, lifetime, selected, "historical-journal"
            )
        )
        for issuer in (None, "materialized", "foreign"):
            self.assertFalse(
                checkpoint._matches_census_plan(selected, lifetime, selected, issuer)
            )
        self.assertFalse(
            checkpoint._matches_census_plan(None, lifetime, None, "historical-journal")
        )

    def test_hostile_equality_is_never_called(self):
        class Poison:
            def __eq__(self, other):
                raise AssertionError("equality callback")

        lifetime, selected = object(), object()
        self.assertFalse(
            checkpoint._matches_census_plan(
                Poison(), lifetime, selected, "historical-journal"
            )
        )
        self.assertFalse(
            checkpoint._matches_census_plan(selected, lifetime, selected, Poison())
        )

    def test_original_scope_validation_precedes_identity_selection(self):
        tree = ast.parse(inspect.getsource(checkpoint))
        function = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "begin_census"
        )
        calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]
        validate = next(
            node
            for node in calls
            if isinstance(node.func, ast.Attribute)
            and node.func.attr == "_require_scope"
        )
        select = next(
            node
            for node in calls
            if isinstance(node.func, ast.Name)
            and node.func.id == "_matches_census_plan"
        )
        self.assertLess(validate.lineno, select.lineno)
        self.assertFalse(
            any(
                isinstance(node, (ast.Assign, ast.AugAssign))
                and any(
                    isinstance(child, ast.Attribute)
                    and child.attr == "plan"
                    and isinstance(child.ctx, ast.Store)
                    for child in ast.walk(node)
                )
                for node in ast.walk(function)
            )
        )
