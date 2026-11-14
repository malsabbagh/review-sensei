"""Original namespace fencing is a size premise, not guarded admission."""

import copy
import unittest

import test_quarantine_command_checkpoint as checkpoint_tests
from review_sensei_github import quarantine_command_namespace as namespace
from review_sensei_github.materialized_publication_projection import _future_accounting

from review_sensei.api_work import MAX_API_REQUEST_LIMIT
from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError


class CommandNamespaceTests(unittest.TestCase):
    def test_original_plain_command_keeps_source_namespace_through_terminal(self):
        captured = []

        def observe(frame, event, arg):
            if frame.f_code is namespace.prepare.__code__ and event == "return":
                run = frame.f_locals["run"]
                caller, state = run.caller, run.caller.accounting
                before = len(caller.observations)
                projected = namespace.project(arg, caller)
                self.assertEqual(projected, namespace.project_current(caller))
                generic, _ = _future_accounting(
                    state, (caller._block, caller._terminal_block)
                )
                self.assertLess(
                    len(canonical_bytes(projected)), len(canonical_bytes(generic))
                )
                self.assertEqual(projected["configuration"], generic["configuration"])
                self.assertEqual(len(caller.observations), before)
                with self.assertRaisesRegex(ReviewInputError, "copied or expired"):
                    namespace.require(copy.copy(arg), caller)
                with self.assertRaisesRegex(
                    ReviewInputError, "original source changed"
                ):
                    namespace.before_sequence(caller, 0)
                ordinary = caller._block
                try:
                    caller._block = caller._terminal_block
                    with self.assertRaisesRegex(
                        ReviewInputError, "ordinary block changed"
                    ):
                        namespace.require(arg, caller)
                finally:
                    caller._block = ordinary
                sequence = caller._sequence
                try:
                    caller._sequence = sequence + 1
                    with self.assertRaisesRegex(
                        ReviewInputError, "physical sequence changed"
                    ):
                        namespace.require(arg, caller)
                finally:
                    caller._sequence = sequence
                namespace.require(arg, caller)
                self.assertEqual(len(caller.observations), before)
                captured.append((arg, caller))

        checkpoint_tests.CommandCheckpointTests().exercise(observer=observe)
        self.assertEqual(len(captured), 1)
        token, caller = captured[0]
        with self.assertRaisesRegex(ReviewInputError, "copied or expired"):
            namespace.require(token, caller)
        self.assertEqual(caller.accounting.action["phase"], "terminal")
        self.assertEqual(caller.accounting.remaining, 190)

    def test_forged_preparation_sequence_cannot_seed_projection(self):
        captured = []
        errors = []

        def trace(frame, event, arg):
            if frame.f_code is not namespace.prepare.__code__:
                return None
            if event == "exception" and isinstance(arg[1], ReviewInputError):
                errors.append(str(arg[1]))
            return trace

        def observe(frame, event, arg):
            if frame.f_code is namespace.prepare.__code__ and event == "call":
                caller = frame.f_locals["run"].caller
                captured.append((caller, caller._sequence, len(caller.observations)))
                caller._sequence = 0

        try:
            checkpoint_tests.CommandCheckpointTests().exercise(
                "namespace-seed", observer=observe, tracer=trace
            )
            self.assertEqual(errors, ["cold namespace preparation scope changed"])
            self.assertEqual(len(captured), 1)
            caller, original_sequence, observations = captured[0]
            self.assertGreater(original_sequence, 0)
            self.assertEqual(len(caller.observations), observations)
            self.assertFalse(caller.accounting.action["execution_started"])
            self.assertEqual(caller.accounting.action["phase"], "pending")
        finally:
            for caller, sequence, _ in captured:
                caller._sequence = sequence

    def test_sequence_and_deadline_edges_match_original_fence(self):
        self.assertEqual(namespace._next_sequence(31, 32, MAX_API_REQUEST_LIMIT), 32)
        for args in (
            (32, 32, 1),
            (33, 32, 1),
            (31, 32, MAX_API_REQUEST_LIMIT + 1),
            (True, 32, 1),
        ):
            with (
                self.subTest(args=args),
                self.assertRaisesRegex(ReviewInputError, "ceiling exceeded"),
            ):
                namespace._next_sequence(*args)

    def test_one_over_deadline_refuses_before_source_effect_or_dispatch(self):
        captured = []
        errors = []

        def trace(frame, event, arg):
            if frame.f_code is not namespace.before_sequence.__code__:
                return None
            if event == "exception" and isinstance(arg[1], ReviewInputError):
                errors.append(str(arg[1]))
            return trace

        def observe(frame, event, arg):
            if frame.f_code is namespace.prepare.__code__ and event == "return":
                caller = frame.f_locals["run"].caller
                captured.append(
                    (caller, caller._sequence, len(caller.observations), caller._clock)
                )
                caller._clock = lambda: MAX_API_REQUEST_LIMIT

        try:
            checkpoint_tests.CommandCheckpointTests().exercise(
                "deadline", observer=observe, tracer=trace
            )
            self.assertEqual(len(captured), 1)
            self.assertEqual(
                errors, ["cold namespace sequence or deadline ceiling exceeded"]
            )
            caller, sequence, observations, _ = captured[0]
            self.assertEqual(caller._sequence, sequence)
            self.assertEqual(len(caller.observations), observations)
            self.assertFalse(caller.accounting.action["execution_started"])
            self.assertEqual(caller.accounting.action["phase"], "pending")
            self.assertEqual(caller.accounting.remaining, 190)
        finally:
            for caller, _, _, clock in captured:
                caller._clock = clock
