"""Partial live fields must never be promoted into a complete trust observation.

Explicit snapshots below exercise model seams, not production authentication.
"""

import copy
import unittest

from review_sensei_github import ReviewPublisher

from review_sensei.errors import ReviewInputError
from tests.test_repository_trust_transitions_g15 import (
    HEAD_SHA,
    _enrolled_snapshot,
    _TrustGitHubHost,
)


class TrustObservationCompletenessTests(unittest.TestCase):
    def setUp(self):
        self.host = _TrustGitHubHost()
        self.publisher = ReviewPublisher(http=self.host.http)
        self.enrolled = _enrolled_snapshot()
        self.publisher.enroll_trust_snapshot(snapshot=self.enrolled)
        self.payload = {
            "base": {
                "repo": {
                    "id": 9001,
                    "full_name": "acme/widgets",
                    "fork": False,
                    "owner": {"id": 501, "login": "acme"},
                }
            }
        }

    def live(self, payload, observed=True):
        return self.publisher.finalizer._evaluate_live_pr_trust_boundary(
            payload,
            repository="acme/widgets",
            repository_id=9001,
            pull_request=88,
            head_sha=HEAD_SHA,
            observed_override=self.enrolled if observed else None,
        )

    def test_login_and_even_complete_individual_hints_cannot_fabricate_snapshot(self):
        decision = self.publisher.check_trust_boundary(
            repository_id=9001,
            pull_request=88,
            phase="before_admission",
            repository_slug="acme/widgets",
            owner_id=501,
            owner_login="acme",
            is_fork=False,
            head_sha=HEAD_SHA,
            actor_login="another-maintainer",
            actor_association="MEMBER",
            actor_permission="maintain",
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, "trust-observation-incomplete")
        self.assertEqual(
            self.publisher.trust_snapshot(repository_id=9001, pull_request=88),
            self.enrolled,
        )
        self.assertEqual(self.host.calls, [])

    def test_missing_live_fields_are_not_filled_from_enrollment_or_override(self):
        for path in (
            ("id",),
            ("full_name",),
            ("fork",),
            ("owner",),
            ("owner", "id"),
            ("owner", "login"),
        ):
            payload = copy.deepcopy(self.payload)
            target = payload["base"]["repo"]
            for part in path[:-1]:
                target = target[part]
            del target[path[-1]]
            with self.subTest(path=path):
                decision = self.live(payload)
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason_code, "trust-observation-incomplete")
        self.assertFalse(self.live(self.payload, observed=False).allowed)
        self.assertEqual(self.host.calls, [])

    def test_complete_model_and_live_identity_remain_supported_without_invention(self):
        self.assertTrue(self.live(self.payload).allowed)
        renamed = copy.deepcopy(self.payload)
        renamed["base"]["repo"]["full_name"] = "acme/renamed"
        decision = self.live(renamed)
        self.assertEqual(decision.detected_transitions, ("repository_rename",))
        self.assertIn("authenticated-trust-migration", decision.recovery_routes)
        malformed = copy.deepcopy(self.payload)
        malformed["base"]["repo"]["full_name"] = "other/widgets"
        with self.assertRaises(ReviewInputError):
            self.live(malformed)

    def test_unknown_effect_retained_with_missing_observation(self):
        self.publisher._unknown_effect_holds.add((9001, 88))
        decision = self.live({}, observed=False)
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.unknown_effect_held)
        self.assertTrue(decision.charges_held)
        self.assertTrue(decision.obligations_preserved)
        self.assertFalse(decision.fresh_allowance_minted)
