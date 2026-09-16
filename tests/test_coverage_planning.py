from __future__ import annotations

import json
import unittest

from review_sensei.coverage import (
    CoverageManifest,
    FileCoverage,
    coverage_approval_state,
)
from review_sensei.diff import analyze_diff
from review_sensei.evaluation import compare_chunked_against_baseline
from review_sensei.models import (
    ProviderResponse,
    ReviewComment,
    ReviewRequest,
    ReviewResult,
)
from review_sensei.planning import TotalWorkBudget, is_generated_path, plan_change
from review_sensei.schemas import validate_public_document
from review_sensei.service import ReviewService
from review_sensei.validation import ReviewLimits

TWO_FILES = """diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -1 +1,2 @@
 keep-a
+added-a
diff --git a/src/b.py b/src/b.py
--- a/src/b.py
+++ b/src/b.py
@@ -1 +1,2 @@
 keep-b
+added-b
"""

DELETION = """diff --git a/src/legacy.py b/src/legacy.py
deleted file mode 100644
--- a/src/legacy.py
+++ /dev/null
@@ -1 +0,0 @@
-legacy = True
"""

BINARY = """diff --git a/assets/logo.png b/assets/logo.png
index 1111111..2222222 100644
Binary files a/assets/logo.png and b/assets/logo.png differ
"""

GENERATED = """diff --git a/dist/app.min.js b/dist/app.min.js
--- a/dist/app.min.js
+++ b/dist/app.min.js
@@ -1 +1,2 @@
 keep
+generated
"""

RENAME = """diff --git a/docs/old.md b/guides/new.md
similarity index 100%
rename from docs/old.md
rename to guides/new.md
"""


class FakeProvider:
    name = "fake"
    model = "fake-model"

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        text = (
            self.responses.pop(0)
            if self.responses
            else ('{"summary":"ok","comments":[]}')
        )
        return ProviderResponse(text=text, provider=self.name, model=self.model)


class CoveragePlanningTests(unittest.TestCase):
    def test_every_changed_file_has_a_coverage_outcome(self):
        plan = plan_change(TWO_FILES)
        paths = {entry.path for entry in plan.coverage.files}
        self.assertEqual(paths, {"src/a.py", "src/b.py"})
        self.assertTrue(plan.coverage.enumeration_complete)
        self.assertTrue(plan.coverage.fully_reviewed)
        self.assertEqual(coverage_approval_state(plan.coverage), "reviewed")

    def test_incomplete_enumeration_is_never_fully_reviewed(self):
        plan = plan_change(
            TWO_FILES,
            orchestrate=True,
            work_budget=TotalWorkBudget(max_total_files=1),
        )
        self.assertFalse(plan.analysis.enumeration_complete)
        self.assertFalse(plan.coverage.enumeration_complete)
        self.assertFalse(plan.coverage.fully_reviewed)
        self.assertEqual(plan.coverage.approval_state(), "incomplete")

    def test_chunk_order_is_deterministic(self):
        limits = ReviewLimits(max_diff_files=1)
        first = plan_change(TWO_FILES, limits=limits, orchestrate=True)
        second = plan_change(TWO_FILES, limits=limits, orchestrate=True)
        self.assertEqual(
            [chunk.paths for chunk in first.chunks],
            [chunk.paths for chunk in second.chunks],
        )
        self.assertEqual(len(first.chunks), 2)
        self.assertEqual(first.chunks[0].paths, ("src/a.py",))
        self.assertEqual(first.chunks[1].paths, ("src/b.py",))
        self.assertEqual(first.chunks[0].related_paths, ("src/b.py",))
        self.assertEqual(first.chunks[1].related_paths, ("src/a.py",))

    def test_binary_generated_and_rename_coverage(self):
        binary = plan_change(BINARY)
        self.assertEqual(binary.coverage.files[0].outcome, "unsupported")
        self.assertEqual(binary.coverage.files[0].reason, "binary")
        generated = plan_change(GENERATED)
        self.assertTrue(is_generated_path("dist/app.min.js"))
        self.assertEqual(generated.coverage.files[0].outcome, "excluded-by-policy")
        rename = plan_change(RENAME)
        paths = {entry.path: entry.outcome for entry in rename.coverage.files}
        self.assertEqual(
            paths, {"docs/old.md": "reviewed", "guides/new.md": "reviewed"}
        )

    def test_oversized_hunk_is_unsupported_instead_of_truncated(self) -> None:
        added = "+" + ("x" * 400)
        huge = f"""diff --git a/src/huge.py b/src/huge.py
--- a/src/huge.py
+++ b/src/huge.py
@@ -1 +1,2 @@
 keep
{added}
"""
        plan = plan_change(
            huge,
            limits=ReviewLimits(max_diff_bytes=80),
            orchestrate=True,
        )
        self.assertEqual(plan.coverage.files[0].path, "src/huge.py")
        self.assertEqual(plan.coverage.files[0].outcome, "unsupported")
        self.assertEqual(plan.coverage.files[0].reason, "too-large-hunk")
        self.assertEqual(plan.chunks, ())
        self.assertTrue(plan.coverage.hunks)
        self.assertEqual(plan.coverage.hunks[0].outcome, "unsupported")

    def test_deleted_lines_are_validated_on_the_left_side(self):
        analysis = analyze_diff(DELETION)
        self.assertEqual(analysis.deleted_lines["src/legacy.py"], frozenset({1}))
        comment = ReviewComment(
            path="src/legacy.py",
            line=1,
            body="Removing this flag is unsafe.",
            side="LEFT",
        )
        provider = FakeProvider(
            [
                json.dumps(
                    {
                        "summary": "Deletion risk.",
                        "comments": [comment.to_dict()],
                    }
                )
            ]
        )
        result = ReviewService(provider).review(ReviewRequest(diff=DELETION))
        self.assertEqual(result.comments[0].side, "LEFT")
        self.assertEqual(result.comments[0].line, 1)
        self.assertTrue(result.coverage is not None)
        self.assertEqual(result.coverage.files[0].outcome, "reviewed")

    def test_legacy_right_side_results_remain_valid(self):
        result = ReviewResult.from_dict(
            {
                "summary": "ok",
                "comments": [
                    {"path": "src/app.py", "line": 2, "body": "Use a constant."}
                ],
                "provider": "fixture",
            }
        )
        self.assertEqual(result.comments[0].side, "RIGHT")
        self.assertNotIn("side", result.comments[0].to_dict())
        validate_public_document(result.to_dict(), "review-result")
        self.assertEqual(coverage_approval_state(result.coverage), "unknown")

    def test_chunk_failure_keeps_valid_findings(self):
        limits = ReviewLimits(max_diff_files=1)
        provider = FakeProvider(
            [
                json.dumps(
                    {
                        "summary": "First chunk.",
                        "comments": [
                            {
                                "path": "src/a.py",
                                "line": 2,
                                "body": "Name this constant.",
                            }
                        ],
                    }
                ),
                "not-json",
                "not-json",
            ]
        )
        result = ReviewService(provider).review(
            ReviewRequest(
                diff=TWO_FILES,
                limits=limits,
                orchestrate_large_changes=True,
            )
        )
        self.assertEqual(result.review_status, "partial")
        self.assertEqual([comment.path for comment in result.comments], ["src/a.py"])
        outcomes = {entry.path: entry.outcome for entry in result.coverage.files}
        self.assertEqual(outcomes["src/a.py"], "reviewed")
        self.assertEqual(outcomes["src/b.py"], "partially-reviewed")

    def test_provider_budget_stops_unbounded_calls(self):
        limits = ReviewLimits(max_diff_files=1)
        provider = FakeProvider(
            [
                '{"summary":"one","comments":[]}',
                '{"summary":"two","comments":[]}',
            ]
        )
        result = ReviewService(provider).review(
            ReviewRequest(
                diff=TWO_FILES,
                limits=limits,
                orchestrate_large_changes=True,
                work_budget=TotalWorkBudget(max_provider_calls=1, max_chunks=2),
            )
        )
        self.assertEqual(len(provider.requests), 1)
        outcomes = {entry.path: entry.outcome for entry in result.coverage.files}
        self.assertIn(outcomes["src/b.py"], {"budget-exhausted", "partially-reviewed"})

    def test_chunked_evaluation_reports_cross_boundary_misses(self):
        baseline = ReviewResult(
            summary="complete",
            comments=(
                ReviewComment(
                    path="src/a.py",
                    line=2,
                    body="a and b together overflow the guard",
                ),
            ),
            provider="fake",
            review_status="complete",
            coverage=CoverageManifest(
                files=(
                    FileCoverage(path="src/a.py", outcome="reviewed"),
                    FileCoverage(path="src/b.py", outcome="reviewed"),
                ),
                enumeration_complete=True,
            ),
        )
        chunked = ReviewResult(
            summary="partial",
            comments=(),
            provider="fake",
            review_status="partial",
            coverage=CoverageManifest(
                files=(
                    FileCoverage(path="src/a.py", outcome="reviewed"),
                    FileCoverage(path="src/b.py", outcome="reviewed"),
                ),
                enumeration_complete=True,
            ),
        )
        report = compare_chunked_against_baseline(
            baseline=baseline,
            chunked=chunked,
            baseline_usage={"calls": 1, "prompt_bytes": 100},
            chunked_usage={"calls": 2, "prompt_bytes": 160},
        )
        self.assertEqual(report["recall"], 0.0)
        self.assertEqual(report["cross_boundary_misses"], 1)
        self.assertEqual(report["chunked_calls"], 2)


if __name__ == "__main__":
    unittest.main()
