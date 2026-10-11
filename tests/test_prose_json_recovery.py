"""Synthetic analogue of GLM prose/multiple-document output; never raw output."""

import json
import unittest
from pathlib import Path

from review_sensei.budgets import ReviewWorkBudgets
from review_sensei.models import ProviderResponse, ReviewRequest
from review_sensei.outcomes import ResourceBudget
from review_sensei.service import ReviewService
from review_sensei.stages import Stage

DIFF = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"
PROSE = (
    Path(__file__).parent / "fixtures/qualification/synthetic-prose-json.txt"
).read_text()


class SequenceProvider:
    name = "fixture"
    model = "fixture"

    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        return ProviderResponse(next(self.responses), self.name, self.model)


class ProseJsonRecoveryTests(unittest.TestCase):
    def run_review(self, provider, *, calls=8):
        return ReviewService(
            provider,
            stages=(
                Stage(
                    name="review",
                    prompt_template="Review {diff}",
                    outputs=("summary", "comments"),
                ),
            ),
            budget=ResourceBudget.create(max_provider_calls=calls),
            work_budgets=ReviewWorkBudgets(mode="unified"),
        ).run(ReviewRequest(diff=DIFF))

    def test_multiple_json_documents_are_not_extracted_as_complete_review(self):
        provider = SequenceProvider(PROSE, PROSE)
        run = self.run_review(provider)
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(run.outcome.status, "partial")
        self.assertFalse(run.result.coverage.fully_reviewed)
        self.assertNotIn("final interpretation", run.result.summary)
        self.assertNotIn(PROSE, provider.requests[1].prompt)

    def test_fresh_valid_correction_can_complete_without_reusing_draft(self):
        corrected = json.dumps({"summary": "Fresh validated review.", "comments": []})
        provider = SequenceProvider(PROSE, corrected)
        run = self.run_review(provider)
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(run.outcome.status, "reviewed")
        self.assertTrue(run.result.coverage.fully_reviewed)
        self.assertEqual(run.result.summary, "Fresh validated review.")

    def test_correction_cannot_exceed_existing_call_budget(self):
        provider = SequenceProvider(PROSE)
        run = self.run_review(provider, calls=1)
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(run.outcome.status, "partial")
        self.assertFalse(run.result.coverage.fully_reviewed)


if __name__ == "__main__":
    unittest.main()
