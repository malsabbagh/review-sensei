"""Measure actual pre-check drift source without granting discounted execution."""

import copy
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch
from urllib.parse import urlsplit

from review_sensei_github.approval import ReviewApprovalEligibility
from review_sensei_github.ordinary_drift_plan import observe_current
from review_sensei_github.publication import (
    ReviewPublisher,
    approval_eligibility_from_body,
    approval_eligibility_marker,
)

from review_sensei.bounded_evidence import canonical_bytes
from tests.test_quarantine_published_tail_journey import (
    BROKER_QUALIFICATIONS,
    PublishedTailJourneyFixture,
)


class ChangedPreparationSource(PublishedTailJourneyFixture):
    observed_publisher = None
    shadow_calls = 0
    changed = False

    def external(self, request, timeout):
        response = super().external(request, timeout)
        if self.observed_publisher is not None and not self.changed:
            body = json.loads(request.data or b"{}")
            if urlsplit(request.full_url).path == "/graphql" and "query" in body:
                self.changed = True

                def shadow(**kwargs):
                    self.shadow_calls += 1
                    raise AssertionError("substituted preparation must not execute")

                self.observed_publisher._prepare_existing_drift_updates = shadow
        return response


class OrdinaryDriftPreparationTests(unittest.TestCase):
    def test_source_change_during_census_stops_before_callback_or_business(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            fixture = ChangedPreparationSource(directory)
            source = observe_current.__code__

            def inspect(frame, event, value):
                if frame.f_code is source and event == "call":
                    frame.f_locals["publisher"]._ordinary_drift_measurement = True
                    planned = frame.f_locals["planned"]
                    if planned is not None and any(
                        ob.thread_binding is not None
                        for ob in planned.inventory.obligations
                    ):
                        fixture.observed_publisher = frame.f_locals["publisher"]

            previous = sys.getprofile()
            try:
                sys.setprofile(inspect)
                with self.assertRaises(AssertionError):
                    fixture.original_origins(failed_count=9, published_count=5)
            finally:
                sys.setprofile(previous)
            self.assertTrue(fixture.changed)
            self.assertEqual(fixture.shadow_calls, 0)
            self.assertEqual(fixture.accounted.accounting.configuration.limit, 512)
            native = [
                (method, path)
                for attempt, method, path in fixture.trace
                if attempt == fixture.attempt
                and method in {"POST", "PATCH", "PUT", "DELETE"}
                and ("/check-runs" in path or "/pulls/" in path)
            ]
            self.assertEqual(native, [])

    def test_original_source_measures_all_prefixes_before_checks(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            fixture = PublishedTailJourneyFixture(directory)
            observations, actual_updates, sources, later_bodies = [], [], [], []
            code = observe_current.__code__
            update_code = ReviewPublisher._prepare_existing_drift_updates.__code__

            def inspect(frame, event, value):
                if frame.f_code is code and event == "call":
                    # Diagnostic-only selection, not an executable profile or
                    # replacement of any source method/callback.
                    frame.f_locals["publisher"]._ordinary_drift_measurement = True
                if (
                    event == "call"
                    and frame.f_code
                    is ReviewPublisher._update_existing_drift_threads.__code__
                    and observations
                ):
                    later_bodies.append(frame.f_back.f_locals["body"])
                if event != "return":
                    return
                if frame.f_code is code and value is not None:
                    journal = frame.f_locals["journal"]
                    self.assertEqual(journal.effects, [])
                    self.assertEqual(
                        journal.ledger.caller.accounting.configuration.limit, 512
                    )
                    self.assertEqual(journal._record.record_sha256, value["root"])
                    self.assertEqual(
                        canonical_bytes(journal._record.to_dict()), value["record"]
                    )
                    observations.append(copy.deepcopy(value))
                    sources.append(
                        (
                            frame.f_locals["publisher"],
                            frame.f_locals["assembled"],
                            frame.f_locals["eligibility"],
                        )
                    )
                if frame.f_code is update_code and value and observations:
                    actual_updates.append(canonical_bytes(value))

            previous = sys.getprofile()
            try:
                sys.setprofile(inspect)
                # The old downstream generic batch gate deliberately remains.
                # This test proves preparation, not completed publication or a
                # new permission to spend the experimental read envelope.
                with self.assertRaises(AssertionError):
                    fixture.original_origins(failed_count=9, published_count=5)
            finally:
                sys.setprofile(previous)
            self.assertEqual(len(observations), 1, fixture.error)
            observed = observations[0]
            self.assertEqual(observed["census_calls"], 1)
            self.assertEqual(
                (observed["usable_before"], observed["usable_after"]), (92, 91)
            )
            self.assertTrue(actual_updates)
            self.assertTrue(
                all(value == observed["updates"] for value in actual_updates)
            )
            self.assertEqual(len(later_bodies), 1)
            actual_eligibility = approval_eligibility_from_body(later_bodies[0])
            self.assertIsInstance(actual_eligibility, ReviewApprovalEligibility)
            assert isinstance(actual_eligibility, ReviewApprovalEligibility)
            normalized_eligibility = replace(
                actual_eligibility,
                facts=replace(actual_eligibility.facts, check_published=False),
            )
            normalized_body = later_bodies[0].replace(
                approval_eligibility_marker(actual_eligibility),
                approval_eligibility_marker(normalized_eligibility),
                1,
            )
            for requests, _, _ in observed["projections"]:
                self.assertEqual(requests[-1][2]["body"], normalized_body)
            quote = observed["quote"]
            self.assertEqual(quote["conditional_calls"], 63)
            self.assertEqual(quote["reserved_tail_calls"], 28)
            self.assertEqual(quote["added_parts"], 9)
            self.assertEqual(len(quote["checkpoints"]), 9)
            for checkpoint in quote["checkpoints"]:
                self.assertFalse(checkpoint["inline"])
                self.assertEqual(checkpoint["parts"], 1)
                self.assertEqual(checkpoint["conditional_calls"], 7)
            self.assertEqual(len(observed["projections"]), 2)
            for requests, documents, count in observed["projections"]:
                self.assertEqual((len(requests), len(documents), count), (8, 9, 9))
                self.assertEqual(
                    sum(
                        path.startswith("/repos/owner/repo/pulls/comments/")
                        for _, path, _, _ in requests
                    ),
                    5,
                )
                self.assertEqual(len(requests[-1][2]["comments"]), 4)
            self.assertEqual(fixture.provider_calls, 2)
            publisher, assembled, eligibility = sources[0]
            key = (123, 7)
            state = publisher._drift_states[key]
            threads = tuple(
                (ob.thread_binding.thread_id, ob.criterion.original_fingerprint)
                for ob in state.inventory.obligations
                if ob.thread_binding is not None
                and ob.thread_binding.comment_id is not None
            )
            self.assertEqual(len(threads), 5)
            arguments = dict(
                threads=threads,
                repository_id=123,
                pull_request=7,
                head_sha=state.head_sha,
                replacements=assembled.inline_comments,
            )
            before = tuple(fixture.trace)
            self.assertEqual(publisher._matching_superseded_threads(**arguments), {})
            clone = copy.copy(publisher)
            clone._pending_thread_cleanup = None
            clone._schedule_thread_cleanup(
                **arguments,
                token="synthetic-unused",
                repository="owner/repo",
                base_sha=state.base_sha,
                app_slug="reviewsensei[bot]",
                eligibility=eligibility,
                review_id=999,
            )
            # Generic scheduling remains deferred. Early elision is not
            # enabled until the selected route proves lifetime custody.
            self.assertTrue(callable(clone._pending_thread_cleanup))
            clone._pending_thread_cleanup = None
            # Removing the retained matching state is not an empty proof. The
            # generic nonempty callback remains scheduled, never executed here.
            clone._drift_states = {}
            clone._active_window_states = {}
            self.assertTrue(clone._matching_superseded_threads(**arguments))
            clone._schedule_thread_cleanup(
                **arguments,
                token="synthetic-unused",
                repository="owner/repo",
                base_sha=state.base_sha,
                app_slug="reviewsensei[bot]",
                eligibility=eligibility,
                review_id=999,
            )
            self.assertTrue(callable(clone._pending_thread_cleanup))
            self.assertEqual(tuple(fixture.trace), before)


if __name__ == "__main__":
    unittest.main()
