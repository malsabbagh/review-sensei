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
            from dataclasses import replace

            original_state = caller._state
            try:
                terminal = next(
                    b for b in original_state.blocks if b.block_id == original
                )
                for altered in (
                    replace(terminal, undispatched=terminal.undispatched - 1),
                    replace(
                        terminal,
                        issued_by=replace(
                            terminal.issued_by,
                            generation=terminal.issued_by.generation + 1,
                        ),
                    ),
                ):
                    # Deliberately corrupt a copied runtime snapshot without
                    # issuing a new accounting state. This is a custody negative,
                    # not permission to relax the original conservation rules.
                    changed_state = copy.copy(original_state)
                    object.__setattr__(
                        changed_state,
                        "blocks",
                        tuple(
                            altered if b is terminal else b
                            for b in original_state.blocks
                        ),
                    )
                    caller._state = changed_state
                    with self.assertRaises(ReviewInputError):
                        _terminal_reservation(journal)
            finally:
                caller._state = original_state
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
            self.assertEqual(code, 0, fixture.error)
            self.assertEqual(visited, [True])
            self.assertEqual(callbacks, [])

    def test_nonmatching_shapes_decline_before_specialized_source_or_price(self):
        from types import SimpleNamespace

        from review_sensei_github import guarded_pending_publication as plan

        base = dict(
            publisher=object(),
            journal=SimpleNamespace(_record=SimpleNamespace(quarantine_lineage={})),
            result=SimpleNamespace(comments=()),
            assembled=SimpleNamespace(inline_comments=(), index_entries=()),
            eligibility=SimpleNamespace(
                historical_assessments=None,
                quarantine_assessments={"pending": [1]},
                facts=SimpleNamespace(has_human_adjudication_findings=True),
            ),
            marker="",
            outcome=SimpleNamespace(conclusion="neutral"),
            policy=None,
            complete_inventory=True,
            checks_enabled=True,
            grouped=False,
            planned_window=None,
            planned_drift=None,
            continuation=None,
            retained_eligibility=None,
            superseded_threads=(),
            convergence_policy=None,
        )
        for changed in (
            {"result": SimpleNamespace(comments=[object()])},
            {"checks_enabled": False},
            {"grouped": True},
            {"complete_inventory": False},
            {"continuation": object()},
            {"retained_eligibility": object()},
            {"superseded_threads": (object(),)},
        ):
            with (
                self.subTest(changed=tuple(changed)),
                patch.object(
                    plan,
                    "_source",
                    side_effect=AssertionError("specialized source invoked"),
                ) as source,
            ):
                self.assertFalse(plan.admit(**{**base, **changed}))
                source.assert_not_called()
