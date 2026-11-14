"""Actual guarded preparation rejects a substituted cached current reader."""

import copy
import sys
import tempfile
import unittest
from unittest.mock import patch

from review_sensei_github.guarded_pending_publication import admit

from tests.test_quarantine_activation import registered_resource
from tests.test_quarantine_published_tail_journey import (
    BROKER_QUALIFICATIONS,
    PublishedTailJourneyFixture,
)


class GuardedPendingReaderCustodyTests(unittest.TestCase):
    def test_cached_reader_override_cannot_acquire_fixed_tail_price(self):
        callbacks = []
        visited = []

        def observe(frame, event, value):
            if event == "call" and frame.f_code is admit.__code__:
                journal = frame.f_locals["journal"]
                factory = journal.reviewer.finalizer.quarantine_reader_factory
                app = factory.__self__
                reader = app._quarantine_hold_readers[
                    (journal.identity.repository, journal.identity.pull_request)
                ]
                reader.read = lambda **kwargs: callbacks.append(kwargs)
                visited.append(True)
            return None

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            fixture = PublishedTailJourneyFixture(directory)
            _, registration = fixture.original_origins(
                failed_count=1, published_count=1
            )
            reviews = copy.deepcopy(fixture.reviews)
            checks = copy.deepcopy(fixture.check_runs)
            previous = sys.gettrace()
            try:
                sys.settrace(observe)
                with registered_resource([registration]):
                    code = fixture.invoke()
            finally:
                sys.settrace(previous)
            self.assertNotEqual(code, 0)
            self.assertEqual(visited, [True])
            self.assertEqual(callbacks, [])
            self.assertEqual(fixture.reviews, reviews)
            self.assertEqual(fixture.check_runs, checks)
            self.assertIn("source callable was shadowed", fixture.error)

    def test_terminal_quote_rejects_replaced_helpers_before_invocation(self):
        from review_sensei_github import api_run_plan, caller_operation, trust_effects
        from review_sensei_github.guarded_pending_publication import (
            _terminal_reservation,
        )

        from review_sensei.errors import ReviewInputError

        callbacks, visited = [], []

        def changed(*args, **kwargs):
            callbacks.append(True)
            return 0

        def observe(frame, event, value):
            if event != "call" or frame.f_code is not admit.__code__:
                return None
            journal = frame.f_locals["journal"]
            for module, name in (
                (api_run_plan, "terminal_requests"),
                (caller_operation, "prewrite_fence_requests"),
                (trust_effects, "verification_calls"),
                (trust_effects, "verification_profile"),
            ):
                with patch.object(module, name, changed):
                    with self.assertRaises(ReviewInputError):
                        _terminal_reservation(journal)
            caller = journal.ledger.caller
            original = caller._terminal_block
            try:
                caller._terminal_block = caller._block
                with self.assertRaisesRegex(ReviewInputError, "terminal name"):
                    _terminal_reservation(journal)
            finally:
                caller._terminal_block = original
            with patch.object(journal.ledger, "_terminal_claimed", True):
                with self.assertRaisesRegex(ReviewInputError, "terminal reservation"):
                    _terminal_reservation(journal)
            visited.append(True)
            return None

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            fixture = PublishedTailJourneyFixture(directory)
            _, registration = fixture.original_origins(
                failed_count=1, published_count=1
            )
            previous = sys.gettrace()
            try:
                sys.settrace(observe)
                with registered_resource([registration]):
                    code = fixture.invoke()
            finally:
                sys.settrace(previous)
            self.assertEqual(visited, [True])
            self.assertEqual(callbacks, [])
            self.assertEqual(code, 0, fixture.error)
