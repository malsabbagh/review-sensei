"""Negative terminal source seams do not issue execution or funding."""

import gc
import unittest
import weakref
from dataclasses import replace
from unittest.mock import patch

from review_sensei_github import historical_terminal_fence as fence
from review_sensei_github import trust_effects
from review_sensei_github.caller_operation import CallerOperation

from review_sensei.errors import ReviewInputError


class HistoricalTerminalFenceTests(unittest.TestCase):
    def test_inactive_lifetime_releases_frame_on_success_and_exception(self):
        class Caller:
            pass

        def run(raises):
            caller = Caller()
            reference = weakref.ref(caller)
            try:
                with fence.execution_scope():
                    if raises:
                        raise ReviewInputError("synthetic inactive exit")
            except ReviewInputError:
                if not raises:
                    raise
            return reference

        for raises in (False, True):
            with self.subTest(raises=raises):
                reference = run(raises)
                gc.collect()
                self.assertIsNone(reference())

    def test_unissued_source_cannot_start_and_inactive_hooks_do_not_traverse(self):
        calls = []

        class Poison:
            def __getattribute__(self, name):
                calls.append(name)
                raise AssertionError("unissued object was traversed")

            def __hash__(self):
                calls.append("hash")
                raise AssertionError("unissued object was hashed")

        caller = Poison()
        with self.assertRaisesRegex(ReviewInputError, "closed source activation"):
            fence.start(caller, Poison())
        fence.before_terminal(caller, Poison(), Poison())
        fence.before_request(caller)
        fence.confirm_checkpoint(caller, Poison(), Poison(), Poison())
        self.assertEqual(calls, [])

    def test_operation_shape_keeps_original_sixteen_and_never_creates_capacity(self):
        original = CallerOperation(16, 3, "original-terminal")
        fence._operation_shape(original)
        for change in (
            {"capacity": 15},
            {"capacity": 17},
            {"capacity": True},
            {"used": 17},
            {"used": True},
            {"write_tail": 4},
            {"write_attempted": 1},
            {"confirmed": 1},
        ):
            with self.subTest(change=change), self.assertRaises(ReviewInputError):
                fence._operation_shape(replace(original, **change))
        self.assertEqual(
            (original.capacity, original.used, original.write_tail), (16, 0, 3)
        )
        with self.assertRaisesRegex(ReviewInputError, "operation type"):
            fence._operation_shape(object())

    def test_replaced_profile_or_refresh_export_is_never_called(self):
        calls = []

        def fake(caller):
            calls.append(caller)
            return None

        for name in ("verification_profile", "verification_calls"):
            with self.subTest(name=name), patch.object(trust_effects, name, fake):
                with self.assertRaisesRegex(ReviewInputError, "trust source changed"):
                    fence._zero_fences(object())
        self.assertEqual(calls, [])
