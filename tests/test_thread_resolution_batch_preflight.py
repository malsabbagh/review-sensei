"""Complete discovery batch wiring never substitutes for live authority fences."""

import unittest
from unittest.mock import patch

from review_sensei_github.http import GitHubHttp
from review_sensei_github.thread_resolution import FindingThreadResolver

from review_sensei.errors import ReviewInputError
from tests import test_thread_cleanup_capability as cleanup_helpers
from tests.test_command_capacity_recovery import CommandCapacityState


class CleanupBatchPreflightTests(unittest.TestCase):
    def run_cleanup(self, *, bound=None, reject=False, fence=None, **kwargs):
        plans = []
        original = FindingThreadResolver.resolve

        def resolve(resolver, **arguments):
            return original(resolver, **arguments, batch_fence_calls=bound)

        def preflight(http, requests, *, native_calls):
            plans.append((requests, native_calls))
            if reject:
                raise ReviewInputError("synthetic complete batch capacity refusal")

        with (
            patch.object(FindingThreadResolver, "resolve", resolve),
            patch.object(GitHubHttp, "preflight_native_batch", preflight, create=True),
        ):
            result = cleanup_helpers.CleanupCapabilityTests().resolve(
                fence=fence, **kwargs
            )
        return (*result, plans)

    def test_unknown_callback_batch_is_unqualified_before_capability_or_mutation(self):
        state, result, calls, acquired, plans = self.run_cleanup()
        self.assertEqual(len(result.unconfirmed), 3)
        self.assertEqual(state.resolved_threads, set())
        self.assertEqual(acquired, [])
        self.assertEqual(plans, [])
        self.assertFalse(
            any(
                row[2] and row[2].get("operationName") == "ResolveReviewThread"
                for row in calls
            )
        )

    def test_complete_batch_capacity_refusal_preserves_all_targets_and_history(self):
        state, result, calls, acquired, plans = self.run_cleanup(bound=3, reject=True)
        self.assertEqual(len(plans), 1)
        self.assertEqual(len(plans[0][0]), 3)
        self.assertEqual(
            plans[0][1], 23
        )  # 3*(root1+fence3+native/readback2)+capability1+repeat4
        self.assertEqual(len(result.unconfirmed), 3)
        self.assertEqual(state.resolved_threads, set())
        self.assertEqual(acquired, [])
        self.assertFalse(any(row[0] == "GET" for row in calls))

    def test_exact_discovered_requests_are_planned_once_before_live_fences(self):
        state, result, calls, acquired, plans = self.run_cleanup(
            bound=0, resolved=(100,)
        )
        self.assertFalse(result.unconfirmed)
        self.assertEqual(len(plans), 1)
        actual = tuple(
            (method, "/graphql", body)
            for method, _, body, _ in calls
            if body and body.get("operationName") == "ResolveReviewThread"
        )
        self.assertEqual(plans[0][0], actual)
        self.assertEqual(len(actual), 2)
        self.assertEqual(plans[0][1], 8)
        self.assertEqual(len(acquired), 1)
        self.assertEqual(state.resolved_threads, {100, 101, 102})

    def test_successful_capacity_plan_cannot_bypass_changed_authority(self):
        state, result, _, acquired, plans = self.run_cleanup(
            bound=0, fence=lambda state: False
        )
        self.assertEqual(len(plans), 1)
        self.assertEqual(len(result.unconfirmed), 3)
        self.assertEqual(state.resolved_threads, set())
        self.assertEqual(acquired, [])

    def test_single_target_retains_existing_guard_without_batch_claim(self):
        sample = CommandCapacityState(
            schema="1", inline_range=(0, 3), human_count=3, padded=False
        )
        state, result, _, acquired, plans = self.run_cleanup(
            instances=frozenset({sample.entries[0].instance})
        )
        self.assertFalse(result.unconfirmed)
        self.assertEqual(plans, [])
        self.assertEqual(state.resolved_threads, {100})
        self.assertEqual(len(acquired), 1)
