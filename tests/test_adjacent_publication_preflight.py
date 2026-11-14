"""Original grouped prefix keeps both parsers and shares only an adjacent read."""

import copy
import inspect
import sys
import tempfile
import unittest
from unittest.mock import patch

from review_sensei_github import adjacent_publication_preflight as receipt
from review_sensei_github import quarantine_native_scope, trust_effects
from review_sensei_github.guarded_publication_plan import require_grouped_durable_floor
from review_sensei_github.publication import ReviewPublisher
from review_sensei_github.quarantine_published_tail import BROKER_QUALIFICATIONS
from test_quarantine_activation import registered_resource
from test_quarantine_published_tail_journey import NativePublishedTailJourneyFixture

from review_sensei.errors import ReviewInputError


class AdjacentPublicationPreflightTests(unittest.TestCase):
    def exercise(self, disturbance=None):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            fixture = NativePublishedTailJourneyFixture(directory)
            old, registration = fixture.original_origins()
            prior = copy.deepcopy(old.api_run_accounting)
            reviews, checks = (
                copy.deepcopy(fixture.reviews),
                copy.deepcopy(fixture.check_runs),
            )
            fixture.provider_comments = [
                {
                    "path": "src/app.py",
                    "line": 3,
                    "side": "RIGHT",
                    "body": "Current published assessment 0 requires independent human judgment.",
                    "severity": "high",
                    "blocking": False,
                    "needs_human": True,
                }
            ]
            observations, consumed, remaining, errors, parser_calls = [], [], [], [], []
            refusals, physical_preflights = [], []
            changed_budget = None
            original_budget_owner = None
            disturbed = False
            index_code = inspect.unwrap(ReviewPublisher._published_state_index).__code__
            preflight_code = inspect.unwrap(ReviewPublisher._preflight_pr).__code__
            original_profile = trust_effects.verification_profile
            original_filter = quarantine_native_scope.filter_reviews

            def observe(frame, event, value):
                nonlocal disturbed, changed_budget, original_budget_owner
                try:
                    if (
                        event == "call"
                        and frame.f_code
                        is NativePublishedTailJourneyFixture.external.__code__
                    ):
                        parent = frame.f_back
                        while parent is not None:
                            if (
                                parent.f_code is preflight_code
                                and parent.f_locals.get("_adjacent_observation")
                                is not None
                            ):
                                physical_preflights.append(
                                    frame.f_locals["request"].get_method()
                                )
                                break
                            parent = parent.f_back
                    if (
                        event == "return"
                        and frame.f_code is receipt.begin.__code__
                        and value is not None
                    ):
                        observations.append((value, frame.f_locals["owner"]))
                    if (
                        event == "return"
                        and frame.f_code is receipt.consume.__code__
                        and value is not None
                    ):
                        consumed.append(value)
                    if (
                        event == "call"
                        and frame.f_code is require_grouped_durable_floor.__code__
                    ):
                        remaining.append(frame.f_locals["journal"].ledger.caller.usable)
                    if (
                        event == "call"
                        and frame.f_code is preflight_code
                        and frame.f_locals.get("_adjacent_observation") is not None
                    ):
                        parser_calls.append(frame.f_locals["_adjacent_observation"])
                    if (
                        event == "call"
                        and frame.f_code is index_code
                        and observations
                        and not disturbed
                    ):
                        disturbed = True
                        observation, owner = observations[-1]
                        # Possession, a shallow copy, and replay outside the exact
                        # original consumer cannot turn a receipt into authority.
                        for selected in (observation, copy.copy(observation)):
                            with self.assertRaises(ReviewInputError):
                                receipt.consume(selected, owner)
                        with self.assertRaises(ReviewInputError):
                            receipt.index(
                                owner, frame.f_locals["native_host"], {"x"}, set(), None
                            )
                        if disturbance == "request":
                            owner.http.request(
                                "GET",
                                owner.http.repository_path("owner/repo", "/pulls/7"),
                                token=frame.f_locals["token"],
                            )
                        elif disturbance == "path":
                            original_path = owner.http.repository_path
                            owner.http.repository_path = lambda repository, suffix="": (
                                original_path(repository, suffix)
                            )
                        elif disturbance == "budget_owner":
                            changed_budget = owner.http.publication_budget
                            original_budget_owner = changed_budget.caller
                            # This inert copy is never dispatched. Restore the
                            # owner before fallback so its real GET is charged
                            # to the unchanged original physical caller.
                            changed_budget.caller = copy.copy(original_budget_owner)
                        elif disturbance == "transport":
                            owner.http.timeout += 1
                        elif disturbance == "filter":
                            quarantine_native_scope.filter_reviews = (
                                lambda *args, **kwargs: []
                            )
                        elif disturbance == "source":
                            trust_effects.verification_profile = lambda caller: None
                    if event == "return" and frame.f_code is index_code:
                        trust_effects.verification_profile = original_profile
                        quarantine_native_scope.filter_reviews = original_filter
                        if changed_budget is not None:
                            changed_budget.caller = original_budget_owner
                except Exception as exc:
                    errors.append(exc)

            with registered_resource([registration]):
                previous = sys.getprofile()
                try:
                    sys.setprofile(observe)
                    rc = fixture.invoke()
                finally:
                    sys.setprofile(previous)
                    trust_effects.verification_profile = original_profile
                    quarantine_native_scope.filter_reviews = original_filter
                    if changed_budget is not None:
                        changed_budget.caller = original_budget_owner
            self.assertEqual(errors, [])
            self.assertEqual(rc, 1, fixture.error)
            self.assertIn("cannot fund the complete operation", fixture.error)
            self.assertEqual(fixture.accounted.accounting.configuration.limit, 512)
            self.assertEqual(old.api_run_accounting, prior)
            self.assertEqual(fixture.reviews, reviews)
            self.assertEqual(fixture.check_runs, checks)
            self.assertEqual(len(observations), 1, refusals)
            self.assertEqual(len(parser_calls), 2)
            self.assertIs(parser_calls[0], parser_calls[1])
            self.assertEqual(len(consumed), int(disturbance is None))
            self.assertEqual(
                physical_preflights, ["GET"] * (1 if disturbance is None else 2)
            )
            self.assertEqual(
                remaining,
                [64 if disturbance is None else 62 if disturbance == "request" else 63],
            )
            observation, owner = observations[0]
            with self.assertRaises(ReviewInputError):
                receipt.consume(observation, owner)

    def test_original_empty_index_saves_one_request_with_both_parsers(self):
        self.exercise()

    def test_intervening_real_request_invalidates_receipt(self):
        self.exercise("request")

    def test_changed_transport_context_invalidates_receipt(self):
        self.exercise("transport")

    def test_replaced_trust_export_is_not_a_zero_verification_profile(self):
        self.exercise("source")

    def test_replaced_native_filter_cannot_preserve_receipt(self):
        self.exercise("filter")

    def test_shadowed_journal_path_constructor_forces_fresh_read(self):
        self.exercise("path")

    def test_changed_budget_owner_forces_fresh_read(self):
        self.exercise("budget_owner")

    def test_unregistered_callers_cannot_issue_receipts(self):
        self.assertIsNone(receipt.begin(object(), object()))
        self.assertIsNone(receipt.consume(None, object()))
        with self.assertRaises(ReviewInputError):
            receipt.install_source(ReviewPublisher)
