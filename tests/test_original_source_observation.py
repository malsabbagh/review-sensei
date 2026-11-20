"""Observer matching, fallback caching, generator lifecycle, and profiler restoration."""

import dis
import gc
import sys
import threading
import unittest
import weakref
from contextlib import ExitStack, contextmanager
from types import FunctionType
from unittest.mock import patch

import original_source_observation as observation
from original_source_observation import observe_original, watch_code, watch_fresh_calls


def _target(value):
    return value + 1


def _failing():
    raise KeyError("target failed")


def _producer():
    yield 1
    return 2


def _send_producer():
    first = yield 1
    second = yield first
    return second


def _cm_producer():
    class CM:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    with CM():
        yield 1


def _catching_producer():
    try:
        yield 1
    except ValueError:
        yield 2
    return 3


class OriginalSourceObservationTests(unittest.TestCase):
    def _engines(self):
        engines = [("profile", lambda: patch.object(observation, "_monitoring", None))]
        if observation._monitoring is not None:
            engines.insert(0, ("monitoring", lambda: contextmanager(lambda: (yield))()))
        return engines

    def test_exact_source_success_excludes_lookalike_globals_and_exception(self):
        def target(fail=False):
            if fail:
                raise ValueError("synthetic")
            return 7

        other = FunctionType(target.__code__, {"ValueError": ValueError})
        for name, engine in self._engines():
            with self.subTest(engine=name), engine():
                events = []
                previous = sys.getprofile()
                with observe_original(
                    target,
                    entered=lambda frame: events.append(
                        ("call", frame.f_locals["fail"])
                    ),
                    returned=lambda frame, value: events.append(("return", value)),
                ):
                    self.assertEqual(target(), 7)
                    self.assertEqual(other(False), 7)
                    with self.assertRaises(ValueError):
                        target(True)
                self.assertIs(sys.getprofile(), previous)
                self.assertEqual(
                    events, [("call", False), ("return", 7), ("call", True)]
                )

    def test_chains_and_restores_prior_profiler_after_callback_fault(self):
        def target():
            return 7

        def fault(frame):
            raise RuntimeError("fixture fault")

        for name, engine in self._engines():
            with self.subTest(engine=name), engine():
                previous = sys.getprofile()
                seen = []

                def prior(frame, event, value):
                    if frame.f_code is target.__code__:
                        seen.append(event)

                try:
                    sys.setprofile(prior)
                    with self.assertRaisesRegex(RuntimeError, "fixture fault"):
                        with observe_original(target, entered=fault):
                            target()
                    self.assertIs(sys.getprofile(), prior)
                    expected = ["call", "return"] if name == "monitoring" else ["call"]
                    self.assertEqual(seen, expected)
                finally:
                    sys.setprofile(previous)

    def test_entered_and_actual_returns_only(self):
        for name, engine in self._engines():
            with self.subTest(engine=name), engine():
                events = []
                with watch_code(
                    _target.__code__,
                    entered=lambda frame: events.append(
                        ("entered", frame.f_locals["value"])
                    ),
                    returned=lambda frame, value: events.append(("returned", value)),
                ):
                    self.assertEqual(_target(1), 2)
                with watch_code(
                    _failing.__code__,
                    entered=lambda frame: events.append(("entered", "failing")),
                    returned=lambda frame, value: events.append(("returned", value)),
                ):
                    with self.assertRaises(KeyError):
                        _failing()
                with watch_code(
                    _producer.__code__,
                    returned=lambda frame, value: events.append(("returned", value)),
                ):
                    self.assertEqual(list(_producer()), [1])
                _target(5)
                self.assertEqual(
                    events,
                    [
                        ("entered", 1),
                        ("returned", 2),
                        ("entered", "failing"),
                        ("returned", 2),
                    ],
                )

    def test_generator_start_resume_yield_return_and_unwind_matching(self):
        for name, engine in self._engines():
            with self.subTest(engine=name), engine():
                events = []
                with watch_code(
                    _send_producer.__code__,
                    entered=lambda frame: events.append(("entered",)),
                    resumed=lambda frame: events.append(("resumed",)),
                    yielded=lambda frame, value: events.append(("yielded", value)),
                    returned=lambda frame, value: events.append(("returned", value)),
                ):
                    gen = _send_producer()
                    self.assertEqual(next(gen), 1)
                    self.assertEqual(gen.send(42), 42)
                    with self.assertRaises(StopIteration) as stop:
                        gen.send(99)
                    self.assertEqual(stop.exception.value, 99)

                self.assertEqual(
                    events,
                    [
                        ("entered",),
                        ("yielded", 1),
                        ("resumed",),
                        ("yielded", 42),
                        ("resumed",),
                        ("returned", 99),
                    ],
                )

                # Unhandled throw and close while suspended must never emit
                # entered, yielded, or returned.
                unwind_events = []
                with watch_code(
                    _send_producer.__code__,
                    entered=lambda frame: unwind_events.append(("entered",)),
                    yielded=lambda frame, value: unwind_events.append(
                        ("yielded", value)
                    ),
                    returned=lambda frame, value: unwind_events.append(
                        ("returned", value)
                    ),
                ):
                    unhandled = _send_producer()
                    self.assertEqual(next(unhandled), 1)
                    with self.assertRaises(KeyError):
                        unhandled.throw(KeyError("boom"))

                    closed = _send_producer()
                    self.assertEqual(next(closed), 1)
                    closed.close()

                self.assertEqual(
                    unwind_events,
                    [
                        ("entered",),
                        ("yielded", 1),
                        ("entered",),
                        ("yielded", 1),
                    ],
                )

                cm_events = []
                with watch_code(
                    _cm_producer.__code__,
                    entered=lambda frame: cm_events.append(("entered",)),
                    yielded=lambda frame, value: cm_events.append(("yielded", value)),
                    returned=lambda frame, value: cm_events.append(("returned", value)),
                ):
                    gen_cm = _cm_producer()
                    self.assertEqual(next(gen_cm), 1)
                    with self.assertRaises(RuntimeError):
                        gen_cm.throw(RuntimeError("unwind"))
                    gen_cm_closed = _cm_producer()
                    self.assertEqual(next(gen_cm_closed), 1)
                    gen_cm_closed.close()
                self.assertEqual(
                    cm_events,
                    [
                        ("entered",),
                        ("yielded", 1),
                        ("entered",),
                        ("yielded", 1),
                    ],
                )

                catch_events = []
                with watch_code(
                    _catching_producer.__code__,
                    entered=lambda frame: catch_events.append(("entered",)),
                    yielded=lambda frame, value: catch_events.append(
                        ("yielded", value)
                    ),
                    returned=lambda frame, value: catch_events.append(
                        ("returned", value)
                    ),
                ):
                    gen_catch = _catching_producer()
                    self.assertEqual(next(gen_catch), 1)
                    self.assertEqual(gen_catch.throw(ValueError("handled")), 2)
                    with self.assertRaises(StopIteration) as stop:
                        next(gen_catch)
                    self.assertEqual(stop.exception.value, 3)
                self.assertEqual(
                    catch_events,
                    [
                        ("entered",),
                        ("yielded", 1),
                        ("yielded", 2),
                        ("returned", 3),
                    ],
                )

    def test_entered_exception_is_raised_by_the_observed_call(self):
        def refuse(frame):
            raise AssertionError("refused at entry")

        for name, engine in self._engines():
            with self.subTest(engine=name), engine():
                with watch_code(_target.__code__, entered=refuse):
                    with self.assertRaisesRegex(AssertionError, "refused at entry"):
                        _target(1)
                self.assertEqual(_target(1), 2)

    def test_namespace_must_be_the_defining_globals(self):
        clone = type(_target)(_target.__code__, {"__builtins__": __builtins__})
        for name, engine in self._engines():
            with self.subTest(engine=name), engine():
                seen = []
                with watch_code(
                    _target.__code__,
                    namespace=_target.__globals__,
                    entered=lambda frame: seen.append(
                        frame.f_globals is _target.__globals__
                    ),
                ):
                    clone(1)
                    _target(1)
                self.assertEqual(seen, [True])

    def test_other_threads_are_not_observed(self):
        for name, engine in self._engines():
            with self.subTest(engine=name), engine():
                seen = []
                with watch_code(_target.__code__, entered=lambda frame: seen.append(1)):
                    worker = threading.Thread(target=_target, args=(1,))
                    worker.start()
                    worker.join()
                    _target(1)
                self.assertEqual(seen, [1])

    def test_nested_watches_on_one_code_each_fire_until_exit(self):
        for name, engine in self._engines():
            with self.subTest(engine=name), engine():
                seen = []
                with watch_code(
                    _target.__code__, entered=lambda frame: seen.append("outer")
                ):
                    with watch_code(
                        _target.__code__, entered=lambda frame: seen.append("inner")
                    ):
                        _target(1)
                    _target(1)
                _target(1)
                self.assertEqual(sorted(seen[:2]), ["inner", "outer"])
                self.assertEqual(seen[2:], ["outer"])

    def test_an_existing_profile_function_keeps_receiving_events(self):
        for name, engine in self._engines():
            with self.subTest(engine=name), engine():
                calls = []

                def outer(frame, event, value):
                    if frame.f_code is _target.__code__ and event == "call":
                        calls.append("outer")

                previous = sys.getprofile()
                sys.setprofile(outer)
                try:
                    with watch_code(
                        _target.__code__, entered=lambda frame: calls.append("watch")
                    ):
                        _target(1)
                finally:
                    sys.setprofile(previous)
                self.assertEqual(sorted(calls), ["outer", "watch"])

    def test_profile_uses_exact_code_identity_and_chains_every_event(self):
        def target():
            len(())
            try:
                [].index("absent")
            except ValueError:
                pass

        clone = FunctionType(target.__code__.replace(), target.__globals__)
        self.assertIsNot(clone.__code__, target.__code__)
        self.assertEqual(clone.__code__, target.__code__)
        seen = []
        prior_events = []

        def prior(frame, event, value):
            if frame.f_code is target.__code__:
                prior_events.append(event)

        previous = sys.getprofile()
        try:
            sys.setprofile(prior)
            target()
            baseline = prior_events.copy()
            self.assertTrue(
                {"call", "return", "c_call", "c_return", "c_exception"} <= set(baseline)
            )
            prior_events.clear()
            with (
                patch.object(observation, "_monitoring", None),
                observe_original(
                    target,
                    entered=lambda f: seen.append("call"),
                    returned=lambda f, v: seen.append("return"),
                ),
            ):
                clone()
                target()
            self.assertIs(sys.getprofile(), prior)
        finally:
            sys.setprofile(previous)
        self.assertEqual(prior_events, baseline)
        self.assertEqual(seen, ["call", "return"])

    def test_profile_watch_retains_unowned_code_and_releases_its_identity(self):
        seen = []
        with (
            patch.object(observation, "_monitoring", None),
            watch_code(_target.__code__, entered=lambda frame: seen.append("outer")),
        ):
            dispatcher = sys.getprofile()
            # Don't execute this code: a coverage profiler may legitimately
            # retain executed code independently of the source observer.
            code = compile("pass", "<observer-lifetime>", "exec")
            reference = weakref.ref(code)
            key = id(code)
            with watch_code(code, entered=lambda frame: self.fail("never executed")):
                del code
                gc.collect()
                self.assertIsNotNone(reference())
                self.assertIn(key, dispatcher._watch_watches)
            gc.collect()
            self.assertIsNone(reference())
            self.assertNotIn(key, dispatcher._watch_watches)
            self.assertIs(sys.getprofile(), dispatcher)
            _target(1)
        self.assertEqual(seen, ["outer"])

    def test_profile_fallback_caches_code_instruction_offsets(self):
        def fresh_target(x):
            return x + 1

        observation._CODE_EVENTS.pop(fresh_target.__code__, None)
        with (
            patch.object(observation, "_monitoring", None),
            patch.object(
                dis, "get_instructions", wraps=dis.get_instructions
            ) as disassemble,
        ):
            for _ in range(3):
                with watch_code(fresh_target.__code__, entered=lambda frame: None):
                    self.assertEqual(fresh_target(1), 2)
        self.assertEqual(disassemble.call_count, 1)

    def test_fresh_calls_observe_only_exact_normal_entries_and_returns(self):
        seen = []

        def target(fail=False):
            if fail:
                raise KeyError("target failed")
            return 7

        pattern = target.__code__
        clone = FunctionType(pattern.replace(), target.__globals__)
        wrong_globals = FunctionType(pattern, {"KeyError": KeyError})
        previous_trace, previous_profile = sys.gettrace(), sys.getprofile()
        with watch_fresh_calls(
            pattern,
            namespace=target.__globals__,
            entered=lambda frame: seen.append(("entered", frame.f_locals["fail"])),
            returned=lambda frame, value: seen.append(("returned", value)),
        ):
            if (
                sys.version_info[:2] == (3, 11)
                and previous_trace is previous_profile is None
            ):
                self.assertIsNone(sys.getprofile())
                self.assertIsNotNone(sys.gettrace())
            self.assertEqual(clone(False), 7)
            self.assertEqual(wrong_globals(False), 7)
            self.assertEqual(target(), 7)
            with self.assertRaises(KeyError):
                target(True)
        self.assertEqual(seen, [("entered", False), ("returned", 7), ("entered", True)])
        self.assertIs(sys.gettrace(), previous_trace)
        self.assertIs(sys.getprofile(), previous_profile)

    @unittest.skipUnless(sys.version_info[:2] == (3, 11), "3.11 scoped trace only")
    def test_fresh_trace_matches_nested_calls_not_a_late_registered_return(self):
        if sys.gettrace() is not None or sys.getprofile() is not None:
            self.skipTest("requires a clean thread")
        seen = []
        with ExitStack() as scopes:

            def target(scoped=True):
                if scoped:
                    scopes.enter_context(
                        watch_fresh_calls(
                            target.__code__,
                            returned=lambda frame, value: seen.append(("late", value)),
                        )
                    )
                    return target(False) + 1
                return 10

            with watch_fresh_calls(
                target.__code__,
                returned=lambda frame, value: seen.append(("initial", value)),
            ):
                dispatcher = sys.gettrace()
                self.assertEqual(target(), 11)
                self.assertEqual(seen, [("initial", 10), ("late", 10), ("initial", 11)])
            self.assertIs(sys.gettrace(), dispatcher)
            self.assertEqual(target(False), 10)
            self.assertEqual(seen[-1], ("late", 10))
        self.assertIsNone(sys.gettrace())

    @unittest.skipUnless(sys.version_info[:2] == (3, 11), "3.11 scoped trace only")
    def test_fresh_watch_keeps_an_existing_trace_and_observer_receives_returns(self):
        previous = sys.gettrace()
        seen = []
        observed = []

        def existing(frame, event, value):
            if frame.f_code is _target.__code__:
                seen.append(event)
            return existing

        try:
            sys.settrace(existing)
            with watch_fresh_calls(
                _target.__code__, returned=lambda frame, value: observed.append(value)
            ):
                self.assertIs(sys.gettrace(), existing)
                self.assertEqual(_target(4), 5)
            self.assertIs(sys.gettrace(), existing)
        finally:
            sys.settrace(previous)
        self.assertEqual(seen, ["call", "line", "return"])
        self.assertEqual(observed, [5])

    def test_fresh_source_callback_fault_is_raised_and_hooks_are_restored(self):
        trace, profile = sys.gettrace(), sys.getprofile()
        for boundary in ("entered", "returned"):

            def fault(*_):
                raise AssertionError("fixture fault from original frame")

            with (
                self.subTest(boundary=boundary),
                watch_fresh_calls(_target.__code__, **{boundary: fault}),
                self.assertRaisesRegex(AssertionError, "original frame"),
            ):
                _target(1)
            self.assertIs(sys.gettrace(), trace)
            self.assertIs(sys.getprofile(), profile)

    def test_monitoring_tool_is_released_after_the_last_watch(self):
        if observation._monitoring is None:
            self.skipTest("sys.monitoring requires Python 3.12+")
        with watch_code(_target.__code__, entered=lambda frame: None):
            tool = observation._tool[0]
            self.assertIsNotNone(tool)
            self.assertEqual(
                sys.monitoring.get_tool(tool), "review-sensei-test-observation"
            )
        self.assertIsNone(observation._tool[0])
        self.assertIsNone(sys.monitoring.get_tool(tool))
        self.assertEqual(observation._watches, {})


if __name__ == "__main__":
    unittest.main()
