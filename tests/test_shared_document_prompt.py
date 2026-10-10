"""Lossless prompt composition exercised through the public review run."""

import hashlib
import json
import unittest

from review_sensei.budgets import ReviewWorkBudgets
from review_sensei.models import (
    ProviderResponse,
    ReviewDocument,
    ReviewLensContext,
    ReviewRequest,
)
from review_sensei.service import ReviewService
from review_sensei.stages import ReviewCategory, Stage

DIFF = (
    "diff --git a/src/app.py b/src/app.py\n"
    "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-old\n+new\n"
)


class Provider:
    name = "fixture"
    model = "synthetic"

    def __init__(self):
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        return ProviderResponse(
            '{"summary":"Reviewed.","comments":[]}', self.name, self.model
        )


class SharedDocumentPromptTests(unittest.TestCase):
    def run_review(self, content, *, active=4):
        categories = tuple(
            ReviewCategory(id=f"lens{i}", title=f"Lens {i}", focus=("correctness",))
            for i in range(4)
        )
        document = ReviewDocument(
            "docs/architecture.md",
            content,
            hashlib.sha256(content.encode()).hexdigest(),
        )
        contexts = tuple(
            ReviewLensContext(category.id, documents=(document,))
            for category in categories
        )
        stage = Stage(
            name="review",
            prompt_template="{review_context}\nCATEGORIES:\n{review_categories}\nDIFF:\n{diff}",
            outputs=("summary", "comments"),
            categories=categories,
        )
        provider = Provider()
        service = ReviewService(
            provider, stages=(stage,), work_budgets=ReviewWorkBudgets(mode="unified")
        )
        run = service.run(
            ReviewRequest(
                diff=DIFF,
                active_category_ids=tuple(
                    category.id for category in categories[:active]
                ),
                lens_contexts=contexts[:active],
                propose_learnings=False,
            )
        )
        return run, provider, contexts, document

    def test_four_lenses_share_exact_unicode_document_under_existing_prompt_limit(self):
        content = 'Rule: preserve "receipts" and paths \\ 東京 🧭.\n' * 420
        self.assertGreater(len(content.encode()), 20_000)
        run, provider, contexts, document = self.run_review(content)
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(run.result.review_status, "complete")
        prompt = provider.requests[0].prompt
        self.assertLessEqual(len(prompt.encode()), 49_152)
        payload = json.loads(prompt.split("\nCATEGORIES:\n", 1)[0])
        self.assertEqual(payload[0]["documents"], [document.to_prompt_dict()])
        self.assertEqual(len(payload[1:]), 4)
        for lens, original in zip(payload[1:], contexts, strict=True):
            self.assertEqual(lens["category_id"], original.category_id)
            self.assertEqual(lens["learnings"], original.to_prompt_dict()["learnings"])
            self.assertEqual(
                lens["documents"],
                [{"document_ref": document.path, "sha256": document.sha256}],
            )
        self.assertEqual(contexts[0].documents[0].content, content)

    def test_mandatory_atomic_overflow_stays_pending_without_provider_calls(self):
        run, provider, _, _ = self.run_review("Repository rule.\n" * 4_500)
        self.assertEqual(provider.requests, [])
        self.assertEqual(run.result.review_status, "partial")

    def test_inactive_lenses_do_not_enter_shared_table(self):
        run, provider, contexts, _ = self.run_review(
            "Complete repository rule.", active=1
        )
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(run.result.review_status, "complete")
        payload = json.loads(provider.requests[0].prompt.split("\nCATEGORIES:\n", 1)[0])
        self.assertEqual(payload, [contexts[0].to_prompt_dict()])
