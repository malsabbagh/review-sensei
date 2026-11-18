"""Optional source profile cannot replace generic behavior or producer code."""

import dis
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from fake_github_http import json_response
from review_sensei_github import ordinary_drift_execution, ordinary_publication_plan
from test_ordinary_drift_stopped_journey import (
    CapturedSecondInvocation,
    StoppedDriftFixture,
)
from test_quarantine_published_tail_journey import BROKER_QUALIFICATIONS


class ResolvedRootFixture(StoppedDriftFixture):
    def external(self, request, timeout):
        if (
            request.get_method() == "POST"
            and urlsplit(request.full_url).path == "/graphql"
        ):
            body = json.loads(request.data)
            if body.get("operationName") == "ReopenFindingThread":
                identifier = body["variables"]["threadId"]
                node = next(n for n in self.threads if n["id"] == identifier)
                self._meter(request)
                node["isResolved"] = False
                return json_response(
                    {
                        "data": {
                            "unresolveReviewThread": {
                                "thread": {"id": identifier, "isResolved": False}
                            }
                        }
                    }
                )
        return super().external(request, timeout)

    def invoke(self, *args, **kwargs):
        result = super().invoke(*args, **kwargs)
        if self.provider_calls == 1:
            for node in self.threads:
                node["isResolved"] = True
        return result


class OrdinaryDriftOptionalProfileTests(unittest.TestCase):
    def test_resolved_native_root_declines_profile_before_generic_reopen(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            fixture = ResolvedRootFixture(directory)
            results = []
            checkpoint_sizes = []
            code = ordinary_drift_execution.prepare_current.__code__

            def observe(frame, event, value):
                if (
                    frame.f_code
                    is ordinary_drift_execution._CHECKPOINT_SOURCE[1].__code__
                    and event == "return"
                    and "packed" in frame.f_locals
                ):
                    from review_sensei.bounded_evidence import canonical_bytes

                    journal = frame.f_locals["self"]
                    checkpoint_sizes.append(
                        (
                            len(canonical_bytes(frame.f_locals["packed"])),
                            frame.f_locals.get("bound"),
                            [
                                (e["method"], e["path"], e["status"])
                                for e in journal.effects
                            ],
                        )
                    )
                if (
                    frame.f_code is code
                    and event == "return"
                    and dis.opname[frame.f_code.co_code[frame.f_lasti]].startswith(
                        "RETURN"
                    )
                ):
                    observer = frame.f_locals["observer"]
                    results.append((value, observer in ordinary_drift_execution._MODES))

            previous = sys.getprofile()
            try:
                sys.setprofile(observe)
                with self.assertRaises(CapturedSecondInvocation):
                    fixture.original_origins(failed_count=1, published_count=1)
            finally:
                sys.setprofile(previous)
            self.assertEqual(results, [(None, False)], fixture.error)
            # This fixture deliberately interrupts only terminal close of its
            # second original producer, after actual publication succeeds.
            self.assertEqual(
                fixture.second_result, 130, str(checkpoint_sizes) + fixture.error
            )
            self.assertIn('"publication_status":"published"', fixture.error)
            self.assertTrue(any(not node["isResolved"] for node in fixture.threads))
            self.assertEqual(fixture.accounted.accounting.configuration.limit, 512)
            self.assertEqual(fixture.accounted.accounting.uncertain, 0)
            self.assertEqual(fixture.provider_calls, 2)

    def test_changed_branch_and_width_producers_stop_before_dispatch(self):
        for attribute in ("current_request_branches", "_capacity_request_identity"):
            with (
                self.subTest(attribute=attribute),
                tempfile.TemporaryDirectory() as directory,
                patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
            ):
                fixture = StoppedDriftFixture(directory)
                fixture.fault = "changed-read-contract"
                fixture.mutation_target = ordinary_publication_plan, attribute
                try:
                    with self.assertRaises(CapturedSecondInvocation):
                        fixture.original_origins(failed_count=2, published_count=1)
                finally:
                    if fixture.source_patch is not None:
                        fixture.source_patch.stop()
                self.assertIsNotNone(fixture.fault_trace, fixture.error)
                self.assertNotEqual(fixture.second_result, 0)
                self.assertEqual(fixture.shadow_calls, 0)
                self.assertEqual(
                    [
                        (method, path)
                        for _, method, path in fixture.trace[fixture.fault_trace :]
                        if method in {"POST", "PATCH", "PUT"}
                        and ("/pulls/" in path or "/check-runs" in path)
                    ],
                    [],
                    fixture.error,
                )
