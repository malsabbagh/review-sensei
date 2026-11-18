"""Actual second-producer faults cannot dispatch the remaining drift suffix."""

import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from fake_github_http import json_response
from review_sensei_github import ordinary_drift_read_contract
from test_quarantine_published_tail_journey import (
    BROKER_QUALIFICATIONS,
    PublishedTailJourneyFixture,
)


class CapturedSecondInvocation(Exception):
    pass


class StoppedDriftFixture(PublishedTailJourneyFixture):
    fault = None
    fault_trace = None
    second_result = None
    source_patch = None
    mutation_target = None
    shadow_calls = 0

    def external(self, request, timeout):
        path, method = urlsplit(request.full_url).path, request.get_method()
        if self.provider_calls == 2 and self.fault_trace is None:
            check_list = (
                method == "GET" and "/commits/" in path and path.endswith("/check-runs")
            )
            create_check = method == "POST" and path.endswith("/check-runs")
            inline_update = method == "PATCH" and "/pulls/comments/" in path
            if self.fault == "changed-read-contract" and check_list:
                response = super().external(request, timeout)
                self.fault_trace = len(self.trace)
                owner, attribute = self.mutation_target

                def shadow(*args, **kwargs):
                    self.shadow_calls += 1
                    raise AssertionError("changed source must not execute")

                self.source_patch = patch.object(owner, attribute, shadow)
                self.source_patch.start()
                return response
            if self.fault == "changed-head" and check_list:
                response = super().external(request, timeout)
                self.fault_trace = len(self.trace)
                self.live_head = "e" * 40
                return response
            if self.fault == "incomplete-checks" and check_list:
                self._meter(request)
                self.fault_trace = len(self.trace)
                return json_response({"check_runs": [], "total_count": 1})
            if self.fault == "failed-check" and create_check:
                self._meter(request)
                self.fault_trace = len(self.trace)
                return json_response({}, 503)
            main_review = method == "POST" and path.endswith("/pulls/7/reviews")
            if (
                self.fault in {"lost-stored-review", "lost-absent-review"}
                and main_review
            ):
                if self.fault == "lost-stored-review":
                    super().external(request, timeout)
                else:
                    self._meter(request)
                self.fault_trace = len(self.trace)
                raise TimeoutError("synthetic main review response loss")
            if self.fault == "lost-stored-inline" and inline_update:
                super().external(request, timeout)
                self.fault_trace = len(self.trace)
                raise TimeoutError("synthetic stored inline response loss")
        return super().external(request, timeout)

    def invoke(self, *args, **kwargs):
        result = super().invoke(*args, **kwargs)
        if self.provider_calls == 2:
            self.second_result = result
            raise CapturedSecondInvocation
        return result


class OrdinaryDriftStoppedJourneyTests(unittest.TestCase):
    def test_failed_lookup_dispatch_and_stored_loss_never_dispatch_suffix(self):
        for fault in (
            "incomplete-checks",
            "failed-check",
            "lost-stored-inline",
            "lost-stored-review",
            "lost-absent-review",
        ):
            with (
                self.subTest(fault=fault),
                tempfile.TemporaryDirectory() as directory,
                patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
            ):
                fixture = StoppedDriftFixture(directory)
                fixture.fault = fault
                with self.assertRaises(CapturedSecondInvocation):
                    fixture.original_origins(failed_count=9, published_count=5)
                self.assertIsNotNone(fixture.fault_trace)
                self.assertNotEqual(fixture.second_result, 0)
                suffix = [
                    (method, path)
                    for _, method, path in fixture.trace[fixture.fault_trace :]
                    if method in {"POST", "PATCH", "PUT"}
                    and ("/pulls/" in path or "/check-runs" in path)
                ]
                self.assertEqual(suffix, [], fixture.error)
                self.assertEqual(fixture.provider_calls, 2)
                self.assertFalse(
                    any(review.get("state") == "APPROVED" for review in fixture.reviews)
                )


class OrdinaryDriftReadSourceMutationTests(unittest.TestCase):
    def test_changed_helper_limits_and_dataclass_stop_before_next_intent(self):
        for owner, attribute in (
            (ordinary_drift_read_contract, "_complete_threads"),
            (ordinary_drift_read_contract, "read_limits"),
            (ordinary_drift_read_contract.DriftReadScope, "__init__"),
        ):
            with (
                self.subTest(attribute=attribute),
                tempfile.TemporaryDirectory() as directory,
                patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
            ):
                fixture = StoppedDriftFixture(directory)
                fixture.fault = "changed-read-contract"
                fixture.mutation_target = owner, attribute
                try:
                    with self.assertRaises(CapturedSecondInvocation):
                        fixture.original_origins(failed_count=9, published_count=5)
                finally:
                    if fixture.source_patch is not None:
                        fixture.source_patch.stop()
                self.assertIsNotNone(fixture.fault_trace)
                self.assertNotEqual(fixture.second_result, 0)
                self.assertEqual(fixture.shadow_calls, 0)
                native = [
                    (method, path)
                    for _, method, path in fixture.trace[fixture.fault_trace :]
                    if method in {"POST", "PATCH", "PUT"}
                    and ("/pulls/" in path or "/check-runs" in path)
                ]
                self.assertEqual(native, [], fixture.error)


class OrdinaryDriftFreshnessTests(unittest.TestCase):
    def test_changed_head_before_suffix_preserves_existing_reviews(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            fixture = StoppedDriftFixture(directory)
            fixture.fault = "changed-head"
            with self.assertRaises(CapturedSecondInvocation):
                fixture.original_origins(failed_count=9, published_count=5)
            self.assertIsNotNone(fixture.fault_trace)
            self.assertNotEqual(fixture.second_result, 0)
            review_writes = [
                (method, path)
                for _, method, path in fixture.trace[fixture.fault_trace :]
                if method in {"POST", "PATCH", "PUT"} and "/pulls/" in path
            ]
            self.assertEqual(review_writes, [], fixture.error)
            self.assertEqual(len(fixture.reviews), 1)
