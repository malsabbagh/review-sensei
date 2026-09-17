from __future__ import annotations

import json
import unittest

from review_sensei.coverage import (
    CoverageManifest,
    FileCoverage,
    coverage_approval_state,
)
from review_sensei.diff import DiffFileRecord, analyze_diff
from review_sensei.errors import ReviewFormatError, ReviewInputError
from review_sensei.evaluation import compare_chunked_against_baseline
from review_sensei.models import (
    ProviderResponse,
    ReviewComment,
    ReviewRequest,
    ReviewResult,
)
from review_sensei.planning import (
    TotalWorkBudget,
    _classify_file,
    _coverage_for,
    apply_chunk_outcomes,
    is_generated_path,
    merge_chunk_coverage,
    plan_change,
)
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

THREE_FILES = """diff --git a/src/a.py b/src/a.py
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
diff --git a/src/c.py b/src/c.py
--- a/src/c.py
+++ b/src/c.py
@@ -1 +1,2 @@
 keep-c
+added-c
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

MULTI_HUNK = """diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -1 +1,2 @@
 line1
+add1
@@ -10 +11,2 @@
 line10
+add10
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

    def test_packed_and_overflow_hunks_on_one_path_are_partially_reviewed(self):
        plan = plan_change(
            MULTI_HUNK,
            limits=ReviewLimits(max_diff_bytes=120),
            orchestrate=True,
            work_budget=TotalWorkBudget(max_chunks=1),
        )
        outcomes = {entry.path: entry.outcome for entry in plan.coverage.files}
        self.assertEqual(outcomes["src/a.py"], "partially-reviewed")

    def test_rejects_contradictory_fully_reviewed_flag(self):
        with self.assertRaises(ReviewInputError):
            CoverageManifest.from_dict(
                {
                    "files": [
                        {
                            "path": "src/a.py",
                            "outcome": "budget-exhausted",
                            "reason": "provider-call-budget",
                        }
                    ],
                    "hunks": [],
                    "enumeration_complete": True,
                    "fully_reviewed": True,
                    "enumerated_paths": ["src/a.py"],
                }
            )

    def test_rejects_file_outcomes_without_enumerated_paths(self):
        with self.assertRaises(ReviewInputError):
            CoverageManifest(
                files=(FileCoverage(path="src/a.py", outcome="reviewed"),),
                enumerated_paths=(),
            )

    def test_rejects_coverage_paths_outside_enumerated_set(self):
        with self.assertRaises(ReviewInputError):
            CoverageManifest(
                files=(FileCoverage(path="src/other.py", outcome="reviewed"),),
                enumerated_paths=("src/a.py",),
            )

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

    def test_max_chunks_overflows_after_limit_without_extra_flush(self):
        limits = ReviewLimits(max_diff_files=1)
        plan = plan_change(
            THREE_FILES,
            limits=limits,
            orchestrate=True,
            work_budget=TotalWorkBudget(max_chunks=2),
        )
        self.assertEqual(len(plan.chunks), 2)
        outcomes = {
            entry.path: (entry.outcome, entry.reason) for entry in plan.coverage.files
        }
        self.assertEqual(outcomes["src/a.py"], ("reviewed", None))
        self.assertEqual(outcomes["src/b.py"], ("reviewed", None))
        self.assertEqual(
            outcomes["src/c.py"],
            ("budget-exhausted", "provider-call-budget"),
        )

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

    def test_hunkless_file_over_byte_limit_is_too_large_file(self):
        analysis = analyze_diff(TWO_FILES)
        record = DiffFileRecord(
            old_path="src/a.py",
            new_path="src/a.py",
            text="--- a/src/a.py\n+++ b/src/a.py\n" + ("x" * 200) + "\n",
            header="--- a/src/a.py\n+++ b/src/a.py\n",
            added_lines=frozenset(),
            deleted_lines=frozenset(),
            hunks=(),
            binary=False,
        )
        outcome, reason = _classify_file(
            record,
            analysis=analysis,
            limits=ReviewLimits(max_diff_bytes=50),
        )
        self.assertEqual(outcome, "unsupported")
        self.assertEqual(reason, "too-large-file")

    def test_from_dict_rejects_unknown_top_level_fields(self):
        with self.assertRaises(ReviewInputError):
            CoverageManifest.from_dict(
                {
                    "schema_version": "1.0",
                    "enumeration_complete": True,
                    "fully_reviewed": True,
                    "enumerated_paths": ["src/a.py"],
                    "files": [{"path": "src/a.py", "outcome": "reviewed"}],
                    "hunks": [],
                    "unexpected": True,
                }
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

    def test_missing_hunk_outcome_does_not_inherit_reviewed_file_status(self):
        analysis = analyze_diff(MULTI_HUNK)
        coverage = _coverage_for(
            analysis,
            file_outcomes={"src/a.py": ("reviewed", None)},
            hunk_outcomes={1: ("reviewed", None)},
            limits=ReviewLimits(),
        )
        outcomes = {entry.index: entry.outcome for entry in coverage.hunks}
        self.assertEqual(outcomes[1], "reviewed")
        self.assertEqual(outcomes[2], "unsupported")

    def test_merge_chunk_coverage_appends_missing_chunk_entries(self):
        aggregate = CoverageManifest(
            files=(FileCoverage(path="src/b.py", outcome="reviewed"),),
            enumeration_complete=False,
            enumerated_paths=("src/a.py", "src/b.py"),
        )
        chunk = CoverageManifest(
            files=(
                FileCoverage(
                    path="src/a.py",
                    outcome="partially-reviewed",
                    reason="cross-file-relationship",
                ),
            ),
            enumerated_paths=("src/a.py",),
        )
        merged = merge_chunk_coverage(
            aggregate,
            chunk,
            paths=("src/a.py",),
            hunk_indexes=(),
        )
        outcomes = {entry.path: entry.outcome for entry in merged.files}
        self.assertEqual(outcomes["src/a.py"], "partially-reviewed")
        self.assertEqual(outcomes["src/b.py"], "reviewed")

    def test_apply_chunk_outcomes_ignores_paths_outside_enumerated_set(self):
        coverage = CoverageManifest(
            files=(FileCoverage(path="src/a.py", outcome="reviewed"),),
            enumerated_paths=("src/a.py",),
        )
        updated = apply_chunk_outcomes(
            coverage,
            paths=("src/a.py", "src/other.py"),
            hunk_indexes=(),
            outcome="partially-reviewed",
            reason="chunk-failed",
        )
        paths = {entry.path for entry in updated.files}
        self.assertEqual(paths, {"src/a.py"})
        self.assertEqual(updated.files[0].outcome, "partially-reviewed")

    def test_merge_chunk_coverage_keeps_worse_outcome(self):
        aggregate = CoverageManifest(
            files=(
                FileCoverage(path="src/a.py", outcome="reviewed"),
                FileCoverage(path="src/b.py", outcome="reviewed"),
            ),
            enumerated_paths=("src/a.py", "src/b.py"),
        )
        chunk = CoverageManifest(
            files=(
                FileCoverage(
                    path="src/a.py",
                    outcome="partially-reviewed",
                    reason="cross-file-relationship",
                ),
            ),
            enumerated_paths=("src/a.py",),
        )
        merged = merge_chunk_coverage(
            aggregate,
            chunk,
            paths=("src/a.py",),
            hunk_indexes=(),
        )
        outcomes = {entry.path: (entry.outcome, entry.reason) for entry in merged.files}
        self.assertEqual(
            outcomes["src/a.py"],
            ("partially-reviewed", "cross-file-relationship"),
        )
        self.assertEqual(outcomes["src/b.py"], ("reviewed", None))

    def test_chunk_subruns_do_not_attach_per_chunk_coverage(self):
        one_file = TWO_FILES.split("diff --git a/src/b.py")[0]
        provider = FakeProvider(['{"summary":"one","comments":[]}'])
        run = ReviewService(provider).run(
            ReviewRequest(diff=one_file, orchestrate_large_changes=False),
            attach_change_coverage=False,
        )
        self.assertIsNotNone(run.result)
        self.assertIsNone(run.result.coverage)

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

    def test_all_chunk_failures_report_no_validated_result(self):
        limits = ReviewLimits(max_diff_files=1)
        provider = FakeProvider([])

        def always_invalid(_request):
            provider.requests.append(_request)
            return ProviderResponse(
                text="not-json", provider=provider.name, model=provider.model
            )

        provider.complete = always_invalid  # type: ignore[method-assign]
        run = ReviewService(provider).run(
            ReviewRequest(
                diff=TWO_FILES,
                limits=limits,
                orchestrate_large_changes=True,
            )
        )
        self.assertEqual(run.outcome.status, "partial")
        self.assertEqual(run.outcome.diagnostic, "no_validated_result")
        self.assertEqual(run.result.review_status, "incomplete")

    def test_provider_budget_does_not_count_failed_attempts(self):
        from review_sensei.service import _BudgetedProvider, _CallBudget

        class FlakyProvider(FakeProvider):
            def complete(self, request):
                self.requests.append(request)
                if len(self.requests) == 1:
                    raise ReviewInputError("transport failed")
                return ProviderResponse(
                    text='{"summary":"ok","comments":[]}',
                    provider=self.name,
                    model=self.model,
                )

        provider = FlakyProvider([])
        budget = _CallBudget(max_calls=1)
        budgeted = _BudgetedProvider(provider, budget=budget)
        with self.assertRaises(ReviewInputError):
            budgeted.complete(None)
        self.assertEqual(budget.calls, 0)
        budgeted.complete(None)
        self.assertEqual(budget.calls, 1)
        with self.assertRaises(ReviewFormatError):
            budgeted.complete(None)

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
        outcomes = {
            entry.path: (entry.outcome, entry.reason) for entry in result.coverage.files
        }
        self.assertEqual(outcomes["src/a.py"], ("reviewed", None))
        self.assertEqual(
            outcomes["src/b.py"],
            ("budget-exhausted", "provider-call-budget"),
        )

    def test_provider_budget_exception_marks_current_chunk_exhausted(self):
        limits = ReviewLimits(max_diff_files=1)

        class BudgetOnSecondCall(FakeProvider):
            def complete(self, request):
                self.requests.append(request)
                if len(self.requests) > 1:
                    from review_sensei.errors import ReviewFormatError

                    raise ReviewFormatError("provider call budget exhausted")
                return ProviderResponse(
                    text='{"summary":"one","comments":[]}',
                    provider=self.name,
                    model=self.model,
                )

        provider = BudgetOnSecondCall([])
        result = ReviewService(provider).review(
            ReviewRequest(
                diff=TWO_FILES,
                limits=limits,
                orchestrate_large_changes=True,
                work_budget=TotalWorkBudget(max_provider_calls=1, max_chunks=2),
            )
        )
        outcomes = {
            entry.path: (entry.outcome, entry.reason) for entry in result.coverage.files
        }
        self.assertEqual(outcomes["src/a.py"], ("reviewed", None))
        self.assertEqual(
            outcomes["src/b.py"],
            ("budget-exhausted", "provider-call-budget"),
        )

    def test_chunk_orchestration_does_not_mutate_service_provider(self):
        limits = ReviewLimits(max_diff_files=1)
        provider = FakeProvider(['{"summary":"one","comments":[]}'])
        service = ReviewService(provider)
        service.review(
            ReviewRequest(
                diff=TWO_FILES,
                limits=limits,
                orchestrate_large_changes=True,
                work_budget=TotalWorkBudget(max_provider_calls=1, max_chunks=2),
            )
        )
        self.assertIs(service.provider, provider)

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
                enumerated_paths=("src/a.py", "src/b.py"),
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
                enumerated_paths=("src/a.py", "src/b.py"),
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
