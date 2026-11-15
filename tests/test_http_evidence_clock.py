"""Clock injection through genuine HTTP only; no hosted application fixtures."""

import io
import json
import time
import unittest

from review_sensei_github.errors import GitHubHTTPError, GitHubHTTPPaginationLimitError
from review_sensei_github.http import GitHubHttp

from review_sensei.evidence import EvidenceSnapshot


class EvidenceClockTests(unittest.TestCase):
    def client(self, **kwargs):
        calls = []

        def opener(request, *, timeout):
            calls.append((request.full_url, timeout))
            value = (
                []
                if "/files?" in request.full_url
                else {
                    "base": {"sha": "a" * 40},
                    "head": {"sha": "b" * 40},
                    "changed_files": 0,
                }
            )
            return io.BytesIO(json.dumps(value).encode())

        return GitHubHttp(opener=opener, **kwargs), calls

    def load(self, http, **kwargs):
        return http.load_review_evidence(
            token="synthetic",
            snapshot=EvidenceSnapshot("owner/repo", 1, "a" * 40, "b" * 40),
            required_paths=(),
            **kwargs,
        )

    def test_constructor_high_uptime_clock_bounds_every_real_read(self):
        start = float.fromhex("0x1.ffff100000003p+22")
        self.assertGreater(start + 60 - start, 60)
        samples = []

        def clock():
            samples.append(start)
            return start

        http, calls = self.client(evidence_monotonic=clock, timeout=120)
        result = self.load(http)
        self.assertTrue(result.enumeration_complete)
        self.assertGreater(len(samples), 3)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(0 < timeout <= 60 for _, timeout in calls))

    def test_actual_budget_wrappers_preserve_clock_and_charge_once(self):
        from review_sensei_github.human_assessment import FeedbackBudgetHttp
        from review_sensei_github.publication_recovery import PublicationBudgetHttp

        from review_sensei.bounded_evidence import EvidenceReadBudget

        for feedback in (False, True):
            with self.subTest(feedback=feedback):
                samples = []

                def clock():
                    samples.append(8388548.000000003)
                    return samples[-1]

                http, calls = self.client(evidence_monotonic=clock, timeout=120)
                budget = EvidenceReadBudget()
                wrapped = (
                    FeedbackBudgetHttp(http=http, before_read=budget.consume)
                    if feedback
                    else http
                )
                wrapped = PublicationBudgetHttp(wrapped, budget)
                self.assertIs(wrapped.evidence_monotonic, clock)
                self.assertIs(wrapped.transport.evidence_monotonic, clock)
                self.assertTrue(self.load(wrapped).enumeration_complete)
                self.assertGreater(len(samples), 3)
                self.assertEqual(len(calls), 3)
                self.assertEqual(budget.calls, 3)
                self.assertTrue(all(0 < timeout <= 60 for _, timeout in calls))

    def test_restricted_wrapper_configuration_only_preserves_clock(self):
        from types import SimpleNamespace

        from review_sensei_github.hosted_media import _MediaTransport
        from review_sensei_github.ordinary_publication_reads import (
            _ClosedPublicationHttp,
        )
        from review_sensei_github.publication_recovery import _RecoveryReadHttp
        from review_sensei_github.review_completion_recovery import _ReadOnlyHttp

        # Construction copies configuration only. No phase/observation authority
        # is manufactured or exercised, and no request is dispatched.
        def clock():
            return 123.0

        http, calls = self.client(evidence_monotonic=clock)
        legacy = SimpleNamespace(
            api_url=http.api_url, timeout=http.timeout, primed_issue_comments={}
        )
        constructors = (
            lambda transport: _RecoveryReadHttp(transport, None),
            lambda transport: _ReadOnlyHttp(transport),
            lambda transport: _MediaTransport(
                transport, read_token="synthetic", app_slug="fixture"
            ),
            lambda transport: _ClosedPublicationHttp(None, transport),
        )
        for constructor in constructors:
            self.assertIs(constructor(http).evidence_monotonic, clock)
            self.assertIs(constructor(legacy).evidence_monotonic, time.monotonic)
        self.assertEqual(calls, [])

    def test_explicit_call_clock_overrides_constructor(self):
        def forbidden():
            self.fail("constructor clock used despite explicit override")

        http, calls = self.client(evidence_monotonic=forbidden)
        samples = []

        def override():
            samples.append(10.0)
            return 10.0

        self.assertTrue(self.load(http, monotonic=override).enumeration_complete)
        self.assertGreater(len(samples), 0)
        self.assertEqual(len(calls), 3)

    def test_expiry_prevents_first_read(self):
        ticks = iter((0.0, 61.0))
        http, calls = self.client(evidence_monotonic=lambda: next(ticks))
        with self.assertRaises(GitHubHTTPPaginationLimitError):
            self.load(http)
        self.assertEqual(calls, [])

    def test_original_default_and_invalid_dependency_and_budgets(self):
        http, calls = self.client()
        self.assertIs(http.evidence_monotonic, time.monotonic)
        self.assertTrue(self.load(http, max_requests=3).enumeration_complete)
        self.assertEqual(len(calls), 3)
        with self.assertRaises(GitHubHTTPError):
            self.client(evidence_monotonic=0)
        for kwargs in (
            {"max_requests": 2},
            {"max_requests": 65},
            {"timeout_seconds": 61},
            {"timeout_seconds": float("inf")},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(GitHubHTTPError):
                self.load(http, **kwargs)
        self.assertEqual(len(calls), 3)
