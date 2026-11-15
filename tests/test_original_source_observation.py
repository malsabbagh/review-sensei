"""Observer matching and restoration, including exceptional profile returns."""

import sys
import unittest
from types import FunctionType

from original_source_observation import observe_original


class OriginalSourceObservationTests(unittest.TestCase):
    def test_exact_source_success_excludes_lookalike_globals_and_exception(self):
        def target(fail=False):
            if fail:
                raise ValueError("synthetic")
            return 7

        other = FunctionType(target.__code__, {"ValueError": ValueError})
        events = []
        previous = sys.getprofile()
        with observe_original(
            target,
            entered=lambda frame: events.append(("call", frame.f_locals["fail"])),
            returned=lambda frame, value: events.append(("return", value)),
        ):
            self.assertEqual(target(), 7)
            self.assertEqual(other(False), 7)
            with self.assertRaises(ValueError):
                target(True)
        self.assertIs(sys.getprofile(), previous)
        self.assertEqual(events, [("call", False), ("return", 7), ("call", True)])

    def test_chains_and_restores_prior_profiler_after_callback_fault(self):
        def target():
            return 7

        previous = sys.getprofile()
        seen = []

        def prior(frame, event, value):
            if frame.f_code is target.__code__:
                seen.append(event)

        def fault(frame):
            raise RuntimeError("fixture fault")

        try:
            sys.setprofile(prior)
            with self.assertRaisesRegex(RuntimeError, "fixture fault"):
                with observe_original(target, entered=fault):
                    target()
            self.assertIs(sys.getprofile(), prior)
            self.assertEqual(seen, ["call"])
        finally:
            sys.setprofile(previous)


if __name__ == "__main__":
    unittest.main()
