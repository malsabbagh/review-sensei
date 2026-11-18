"""Synthetic controls for original-entry interruption, without product fixtures."""

import sys
import unittest
from types import SimpleNamespace

from original_finish_interrupt import interrupt_original_finish


class Caller:
    def __init__(self, *, run, attempt):
        self.finished = False

    def finish_review_run(self):
        self.finished = True


class OriginalFinishInterruptTests(unittest.TestCase):
    def test_exact_constructed_owner_interrupts_before_body_and_restores(self):
        prior = sys.getprofile()
        seen = []

        def observer(frame, event, value):
            if frame.f_code is Caller.finish_review_run.__code__:
                seen.append(event)
            if prior is not None:
                prior(frame, event, value)

        sys.setprofile(observer)
        try:
            with self.assertRaises(KeyboardInterrupt):
                with interrupt_original_finish(Caller, run_id=7, attempt=1) as hits:
                    caller = Caller(run=SimpleNamespace(run_id=7), attempt=1)
                    caller.finish_review_run()
            self.assertEqual(hits, [True])
            self.assertFalse(caller.finished)
            self.assertIs(sys.getprofile(), observer)
            self.assertIn("call", seen)
        finally:
            sys.setprofile(prior)

    def test_wrong_instance_run_and_attempt_are_not_interrupted(self):
        previous = sys.getprofile()
        old = Caller(run=SimpleNamespace(run_id=7), attempt=1)
        with interrupt_original_finish(Caller, run_id=7, attempt=1) as hits:
            other = Caller(run=SimpleNamespace(run_id=8), attempt=1)
            retry = Caller(run=SimpleNamespace(run_id=7), attempt=2)
            old.finish_review_run()
            other.finish_review_run()
            retry.finish_review_run()
        self.assertEqual(hits, [])
        self.assertTrue(all(c.finished for c in (old, other, retry)))
        self.assertIs(sys.getprofile(), previous)

    def test_exceptional_constructor_return_is_not_captured(self):
        class Broken:
            def __init__(self, *, run, attempt):
                raise ValueError("construction refused")

            def finish_review_run(self):
                self.finished = True

        caller = object.__new__(Broken)
        with interrupt_original_finish(Broken, run_id=7, attempt=1) as hits:
            with self.assertRaises(ValueError):
                caller.__init__(run=SimpleNamespace(run_id=7), attempt=1)
            caller.finish_review_run()
        self.assertEqual(hits, [])
        self.assertTrue(caller.finished)

    def test_codes_are_pinned_before_function_mutation(self):
        original_init = Caller.__init__.__code__
        original_finish = Caller.finish_review_run.__code__

        def changed_init(self, *, run, attempt):
            self.finished = False

        def changed_finish(self):
            self.finished = True

        try:
            with self.assertRaises(KeyboardInterrupt):
                with interrupt_original_finish(Caller, run_id=7, attempt=1) as hits:
                    Caller.__init__.__code__ = changed_init.__code__
                    unobserved = Caller(run=SimpleNamespace(run_id=7), attempt=1)
                    Caller.__init__.__code__ = original_init
                    unobserved.finish_review_run()
                    caller = Caller(run=SimpleNamespace(run_id=7), attempt=1)
                    Caller.finish_review_run.__code__ = changed_finish.__code__
                    caller.finish_review_run()
                    self.assertEqual(hits, [])
                    Caller.finish_review_run.__code__ = original_finish
                    caller.finish_review_run()
            self.assertEqual(hits, [True])
        finally:
            Caller.__init__.__code__ = original_init
            Caller.finish_review_run.__code__ = original_finish

    def test_duplicate_matching_constructor_refuses_and_restores(self):
        previous = sys.getprofile()
        with self.assertRaisesRegex(AssertionError, "duplicate matching"):
            with interrupt_original_finish(Caller, run_id=7, attempt=1):
                Caller(run=SimpleNamespace(run_id=7), attempt=1)
                Caller(run=SimpleNamespace(run_id=7), attempt=1)
        self.assertIs(sys.getprofile(), previous)
