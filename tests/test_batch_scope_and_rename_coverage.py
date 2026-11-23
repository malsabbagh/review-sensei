"""Tests for renamed-path pending coverage reconciliation and legacy multi-dir chunk scope."""

from __future__ import annotations

import json
import unittest

from review_sensei.budgets import ReviewWorkBudgets
from review_sensei.diff import analyze_diff
from review_sensei.models import ProviderResponse, ReviewRequest
from review_sensei.outcomes import ResourceBudget
from review_sensei.planning import (
    MAX_RELATED_PATHS,
    _reconcile_file_outcomes_from_hunks,
    plan_change,
)
from review_sensei.service import ReviewService, _chunk_context_note
from review_sensei.stages import Stage
from review_sensei.validation import ReviewLimits


class _RecordingProvider:
    name = "fixture"
    model = "fixture"

    def __init__(self, responses: list[str] | None = None) -> None:
        self.responses = list(responses or [])
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        if self.responses:
            text = self.responses.pop(0)
        else:
            text = json.dumps({"summary": "Chunk reviewed.", "comments": []})
        return ProviderResponse(text=text, provider=self.name, model=self.model)


class RenameCoverageParityTests(unittest.TestCase):
    def test_discovery_pending_renamed_file_with_hunk_downgrades_old_and_new_paths(
        self,
    ) -> None:
        diff = (
            "diff --git a/src/first.py b/src/first.py\n"
            "--- a/src/first.py\n"
            "+++ b/src/first.py\n"
            "@@ -1 +1,2 @@\n"
            " keep\n"
            "+first_change = True\n"
            "diff --git a/src/old_mod.py b/src/new_mod.py\n"
            "similarity index 75%\n"
            "rename from src/old_mod.py\n"
            "rename to src/new_mod.py\n"
            "--- a/src/old_mod.py\n"
            "+++ b/src/new_mod.py\n"
            "@@ -1 +1,2 @@\n"
            " base = 1\n"
            "+renamed_change = True\n"
        )
        provider = _RecordingProvider()
        service = ReviewService(
            provider,
            stages=(Stage("review", "Review:\n{diff}", ("summary", "comments")),),
            budget=ResourceBudget.create(max_provider_calls=1),
            work_budgets=ReviewWorkBudgets(mode="unified", batch_diff_bytes=260),
        )
        run = service.run(ReviewRequest(diff=diff))
        self.assertEqual(run.outcome.status, "partial")
        self.assertIsNotNone(run.result)
        assert run.result is not None and run.result.coverage is not None
        self.assertFalse(run.result.coverage.fully_reviewed)
        outcomes = {
            entry.path: (entry.outcome, entry.reason)
            for entry in run.result.coverage.files
        }
        self.assertEqual(outcomes["src/first.py"], ("reviewed", None))
        self.assertEqual(
            outcomes["src/old_mod.py"],
            ("budget-exhausted", "provider-call-budget"),
        )
        self.assertEqual(
            outcomes["src/new_mod.py"],
            ("budget-exhausted", "provider-call-budget"),
        )

    def test_discovery_pending_pure_rename_without_hunks_downgrades_both_paths(
        self,
    ) -> None:
        diff = (
            "diff --git a/a/first.py b/a/first.py\n"
            "--- a/a/first.py\n"
            "+++ b/a/first.py\n"
            "@@ -1 +1,2 @@\n"
            " keep\n"
            "+first_change = True\n"
            "diff --git a/docs/old_guide.md b/guides/new_guide.md\n"
            "similarity index 100%\n"
            "rename from docs/old_guide.md\n"
            "rename to guides/new_guide.md\n"
        )
        provider = _RecordingProvider()
        service = ReviewService(
            provider,
            stages=(Stage("review", "Review:\n{diff}", ("summary", "comments")),),
            budget=ResourceBudget.create(max_provider_calls=1),
            work_budgets=ReviewWorkBudgets(mode="unified", batch_diff_bytes=160),
        )
        run = service.run(ReviewRequest(diff=diff))
        self.assertEqual(run.outcome.status, "partial")
        self.assertIsNotNone(run.result)
        assert run.result is not None and run.result.coverage is not None
        self.assertFalse(run.result.coverage.fully_reviewed)
        outcomes = {
            entry.path: (entry.outcome, entry.reason)
            for entry in run.result.coverage.files
        }
        self.assertEqual(outcomes["a/first.py"], ("reviewed", None))
        self.assertEqual(
            outcomes["docs/old_guide.md"],
            ("budget-exhausted", "provider-call-budget"),
        )
        self.assertEqual(
            outcomes["guides/new_guide.md"],
            ("budget-exhausted", "provider-call-budget"),
        )

    def test_discovery_partial_multi_hunk_rename_marks_both_paths_partially_reviewed(
        self,
    ) -> None:
        diff = (
            "diff --git a/src/old_two.py b/src/new_two.py\n"
            "similarity index 60%\n"
            "rename from src/old_two.py\n"
            "rename to src/new_two.py\n"
            "--- a/src/old_two.py\n"
            "+++ b/src/new_two.py\n"
            "@@ -1 +1,2 @@\n"
            " a = 1\n"
            "+" + ("x" * 100) + "\n"
            "@@ -10 +11,2 @@\n"
            " b = 2\n"
            "+" + ("y" * 100) + "\n"
        )
        provider = _RecordingProvider()
        service = ReviewService(
            provider,
            stages=(Stage("review", "Review:\n{diff}", ("summary", "comments")),),
            budget=ResourceBudget.create(max_provider_calls=1),
            work_budgets=ReviewWorkBudgets(mode="unified", batch_diff_bytes=360),
        )
        run = service.run(ReviewRequest(diff=diff))
        self.assertEqual(run.outcome.status, "partial")
        self.assertIsNotNone(run.result)
        assert run.result is not None and run.result.coverage is not None
        self.assertFalse(run.result.coverage.fully_reviewed)
        outcomes = {
            entry.path: (entry.outcome, entry.reason)
            for entry in run.result.coverage.files
        }
        self.assertEqual(
            outcomes["src/old_two.py"],
            ("partially-reviewed", "provider-call-budget"),
        )
        self.assertEqual(
            outcomes["src/new_two.py"],
            ("partially-reviewed", "provider-call-budget"),
        )

    def test_reconcile_file_outcomes_from_hunks_handles_renamed_paths(self) -> None:
        diff = (
            "diff --git a/legacy/old.py b/modern/new.py\n"
            "similarity index 60%\n"
            "rename from legacy/old.py\n"
            "rename to modern/new.py\n"
            "--- a/legacy/old.py\n"
            "+++ b/modern/new.py\n"
            "@@ -1 +1,2 @@\n"
            " keep1\n"
            "+add1\n"
            "@@ -10 +11,2 @@\n"
            " keep2\n"
            "+add2\n"
        )
        analysis = analyze_diff(diff)

        # Mixed hunks: hunk 1 reviewed, hunk 2 budget-exhausted -> both paths partially-reviewed.
        mixed_files = {
            "legacy/old.py": ("budget-exhausted", "provider-call-budget"),
            "modern/new.py": ("budget-exhausted", "provider-call-budget"),
        }
        _reconcile_file_outcomes_from_hunks(
            analysis,
            file_outcomes=mixed_files,
            hunk_outcomes={
                1: ("reviewed", None),
                2: ("budget-exhausted", "provider-call-budget"),
            },
        )
        self.assertEqual(
            mixed_files["legacy/old.py"],
            ("partially-reviewed", "provider-call-budget"),
        )
        self.assertEqual(
            mixed_files["modern/new.py"],
            ("partially-reviewed", "provider-call-budget"),
        )

        # All hunks failed -> neither path becomes reviewed.
        failed_files = {
            "legacy/old.py": ("budget-exhausted", "chunk-failed"),
            "modern/new.py": ("budget-exhausted", "chunk-failed"),
        }
        _reconcile_file_outcomes_from_hunks(
            analysis,
            file_outcomes=failed_files,
            hunk_outcomes={
                1: ("budget-exhausted", "chunk-failed"),
                2: ("budget-exhausted", "chunk-failed"),
            },
        )
        self.assertEqual(
            failed_files["legacy/old.py"],
            ("budget-exhausted", "chunk-failed"),
        )
        self.assertEqual(
            failed_files["modern/new.py"],
            ("budget-exhausted", "chunk-failed"),
        )

        # All hunks reviewed -> both paths reviewed.
        reviewed_files = {
            "legacy/old.py": ("partially-reviewed", "prior"),
            "modern/new.py": ("partially-reviewed", "prior"),
        }
        _reconcile_file_outcomes_from_hunks(
            analysis,
            file_outcomes=reviewed_files,
            hunk_outcomes={1: ("reviewed", None), 2: ("reviewed", None)},
        )
        self.assertEqual(reviewed_files["legacy/old.py"], ("reviewed", None))
        self.assertEqual(reviewed_files["modern/new.py"], ("reviewed", None))


class LegacyMultiDirChunkScopeTests(unittest.TestCase):
    def test_legacy_multi_directory_chunking_includes_cross_dir_paths_and_labels_summary(
        self,
    ) -> None:
        diff = (
            "diff --git a/src/core/engine.py b/src/core/engine.py\n"
            "--- a/src/core/engine.py\n"
            "+++ b/src/core/engine.py\n"
            "@@ -1 +1,2 @@\n"
            " def run():\n"
            "+    return 1\n"
            "diff --git a/packages/gh/client.py b/packages/gh/client.py\n"
            "--- a/packages/gh/client.py\n"
            "+++ b/packages/gh/client.py\n"
            "@@ -1 +1,2 @@\n"
            " def post():\n"
            "+    return 2\n"
            "diff --git a/tests/test_engine.py b/tests/test_engine.py\n"
            "--- a/tests/test_engine.py\n"
            "+++ b/tests/test_engine.py\n"
            "@@ -1 +1,2 @@\n"
            " def test_run():\n"
            "+    assert True\n"
        )
        plan = plan_change(
            diff,
            limits=ReviewLimits(max_diff_files=1),
            orchestrate=True,
        )
        self.assertEqual(len(plan.chunks), 3)
        self.assertEqual(
            plan.chunks[0].related_paths,
            ("packages/gh/client.py", "tests/test_engine.py"),
        )
        self.assertEqual(
            plan.chunks[1].related_paths,
            ("src/core/engine.py", "tests/test_engine.py"),
        )
        self.assertEqual(
            plan.chunks[2].related_paths,
            ("src/core/engine.py", "packages/gh/client.py"),
        )

        provider = _RecordingProvider(
            [
                json.dumps({"summary": "Engine updated.", "comments": []}),
                json.dumps({"summary": "Client updated.", "comments": []}),
                json.dumps({"summary": "Tests updated.", "comments": []}),
            ]
        )
        result = ReviewService(provider).review(
            ReviewRequest(
                diff=diff,
                limits=ReviewLimits(max_diff_files=1),
                orchestrate_large_changes=True,
            )
        )
        self.assertEqual(result.review_status, "complete")
        self.assertEqual(len(provider.requests), 3)

        expected_cross_dirs = (
            ("packages/gh/client.py", "tests/test_engine.py"),
            ("src/core/engine.py", "tests/test_engine.py"),
            ("src/core/engine.py", "packages/gh/client.py"),
        )
        for req, expected_others in zip(
            provider.requests, expected_cross_dirs, strict=True
        ):
            self.assertIn("<related-changed-paths-not-in-chunk>", req.prompt)
            note_block = req.prompt.split("<related-changed-paths-not-in-chunk>", 1)[
                1
            ].split("</related-changed-paths-not-in-chunk>", 1)[0]
            for other_path in expected_others:
                self.assertIn(other_path, note_block)
            self.assertIn("Do NOT report absent sibling files", req.prompt)
            self.assertIn("Scope `summary` strictly", req.prompt)

        self.assertIn(
            "**Chunk 1/3 (src/core/engine.py):** Engine updated.", result.summary
        )
        self.assertIn(
            "**Chunk 2/3 (packages/gh/client.py):** Client updated.", result.summary
        )
        self.assertIn(
            "**Chunk 3/3 (tests/test_engine.py):** Tests updated.", result.summary
        )

    def test_legacy_chunk_label_respects_max_summary_bytes(self) -> None:
        diff = (
            "diff --git a/src/a.py b/src/a.py\n"
            "--- a/src/a.py\n"
            "+++ b/src/a.py\n"
            "@@ -1 +1,2 @@\n"
            " a = 1\n"
            "+a = 2\n"
            "diff --git a/tests/test_a.py b/tests/test_a.py\n"
            "--- a/tests/test_a.py\n"
            "+++ b/tests/test_a.py\n"
            "@@ -1 +1,2 @@\n"
            " t = 1\n"
            "+t = 2\n"
        )
        provider = _RecordingProvider(
            [
                json.dumps({"summary": "s" * 40, "comments": []}),
                json.dumps({"summary": "t" * 40, "comments": []}),
            ]
        )
        # Raw summaries total 82 bytes, but prefixed summaries exceed 100 bytes.
        run = ReviewService(provider).run(
            ReviewRequest(
                diff=diff,
                limits=ReviewLimits(max_diff_files=1, max_summary_bytes=100),
                orchestrate_large_changes=True,
            )
        )
        self.assertEqual(run.outcome.status, "budget_exhausted")
        self.assertEqual(run.outcome.diagnostic, "output_budget")
        self.assertIsNone(run.result)

    def test_chunk_context_note_bounds_path_list_at_64_paths(self) -> None:
        related = tuple(f"pkg_{i}/mod_{i}.py" for i in range(MAX_RELATED_PATHS + 10))
        note = _chunk_context_note(
            related,
            chunk_index=1,
            total_chunks=2,
            chunk_paths=("src/main.py",),
            total_changed_paths=len(related) + 1,
        )
        self.assertIn("pkg_0/mod_0.py", note)
        self.assertIn(
            f"pkg_{MAX_RELATED_PATHS - 1}/mod_{MAX_RELATED_PATHS - 1}.py", note
        )
        self.assertNotIn(f"pkg_{MAX_RELATED_PATHS}/mod_{MAX_RELATED_PATHS}.py", note)
        self.assertIn("(+10 more)", note)


if __name__ == "__main__":
    unittest.main()
