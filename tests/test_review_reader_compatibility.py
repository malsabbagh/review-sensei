from __future__ import annotations

import base64
import json
import unittest
from copy import deepcopy
from dataclasses import replace

from review_sensei.errors import ReviewInputError
from review_sensei.hosting.github.approval import ReviewApprovalEligibility
from review_sensei.hosting.github.errors import GitHubPublicationError
from review_sensei.hosting.github.publication import (
    ReviewApprovalFinalizer,
    approval_eligibility_from_body,
    approval_eligibility_marker,
)
from review_sensei.human_assessment import PendingHumanReview
from tests.test_human_assessment import APP, HEAD, State, prior


class ReaderCompatibilityTests(unittest.TestCase):
    def test_v1_wire_shape_and_fingerprints_remain_unchanged(self):
        original = prior()
        self.assertEqual(original.to_dict()["schema_version"], "1")
        self.assertNotIn("schema_version", original.human_review.to_dict())
        self.assertEqual(
            ReviewApprovalEligibility.from_dict(original.to_dict()), original
        )
        self.assertEqual(
            set(original.human_review.to_dict()["findings"][0]),
            {"fingerprint", "path", "body"},
        )

    def test_cross_file_payload_uses_recognized_envelope_and_explicit_version(self):
        original = prior()
        finding = replace(
            original.human_review.findings[0],
            required_paths=("src/app.py", "src/caller.py"),
        )
        inventory = PendingHumanReview(original.human_review.base_sha, (finding,))
        richer = replace(original, human_review=inventory)
        self.assertEqual(richer.to_dict()["schema_version"], "2.0")
        self.assertEqual(richer.to_dict()["facts"]["schema_version"], "1")
        marker = approval_eligibility_marker(richer)
        self.assertTrue(marker.startswith("<!-- reviewsensei:eligibility:v1 "))
        self.assertEqual(approval_eligibility_from_body(marker), richer)
        self.assertEqual(
            finding.fingerprint, original.human_review.findings[0].fingerprint
        )
        downgraded = richer.to_dict()
        downgraded["schema_version"] = "1"
        with self.assertRaises(ReviewInputError):
            ReviewApprovalEligibility.from_dict(downgraded)
        unsupported = original.to_dict()
        unsupported["schema_version"] = "2.0"
        with self.assertRaises(ReviewInputError):
            ReviewApprovalEligibility.from_dict(unsupported)

    def test_unknown_latest_payload_never_exposes_previous_clean_authority(self):
        state = State()
        clean = replace(
            state.eligibility,
            human_review=None,
            facts=replace(
                state.eligibility.facts, has_human_adjudication_findings=False
            ),
        )
        state.reviews = [state.review(clean, 20)]
        for version in ("3.0", "future", [], {}, True, None):
            with self.subTest(version=version):
                document = deepcopy(clean.to_dict())
                document["schema_version"] = version
                encoded = base64.urlsafe_b64encode(
                    json.dumps(document).encode()
                ).decode()
                state.reviews[:] = [
                    state.review(clean, 20),
                    {
                        "id": 21,
                        "commit_id": HEAD,
                        "user": {"login": APP, "type": "Bot"},
                        "body": f"<!-- reviewsensei:eligibility:v1 {encoded} -->",
                    },
                ]
                finalizer = ReviewApprovalFinalizer(http=state.http)
                arguments = dict(
                    token="synthetic",
                    repository="owner/repo",
                    pull_request=1,
                    head_sha=HEAD,
                    app_slug=APP,
                )
                self.assertIsNone(finalizer.load_eligibility(**arguments))
                with self.assertRaises(GitHubPublicationError):
                    finalizer.load_eligibility(**arguments, require_valid=True)
                self.assertEqual(state.events(), [])


if __name__ == "__main__":
    unittest.main()
