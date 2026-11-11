"""G09: Oversized indivisible work classification and bounded recovery."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from dataclasses import replace
from datetime import datetime, timezone

from review_sensei_github.approval import approval_eligibility_from_result
from review_sensei_github.checks import review_check_outcome

from review_sensei.budgets import LEGACY_ASSESSMENT_PROMPT_BYTES, ReviewWorkBudgets
from review_sensei.context import PromptContextOverflow, admit_prompt_context
from review_sensei.coverage import CoverageManifest, FileCoverage, HunkCoverage
from review_sensei.diff import analyze_diff
from review_sensei.disposition import (
    apply_session_command,
    incomplete_coverage_from_result,
    parse_maintainer_command,
    session_honors_coverage_uncertainty,
)
from review_sensei.document_context import DocumentDecision, DocumentSelectionReport
from review_sensei.errors import ReviewInputError
from review_sensei.evidence import EvidenceBundle, EvidenceRecord, EvidenceSnapshot
from review_sensei.human_file_review import UnsupportedFile, coverage_only_binary
from review_sensei.hunk_subdivision import subdivide_hunk
from review_sensei.models import (
    ProviderRequest,
    ProviderResponse,
    ReviewComment,
    ReviewDocument,
    ReviewLensContext,
    ReviewRequest,
)
from review_sensei.operational_boundaries import (
    DEFERRED_FITTING_UNIT_KINDS,
    PERMITTED_RECOVERY_ROUTES,
    RECOVERY_COMMAND_TEMPLATES,
    IndivisibleWorkReport,
    IndivisibleWorkUnit,
    classify_change_coverage,
    classify_mandatory_prompt_overflow,
    classify_work_plan_unprocessed,
)
from review_sensei.outcomes import ResourceBudget
from review_sensei.planning import (
    TotalWorkBudget,
    WorkBatch,
    WorkRequirement,
    plan_change,
    plan_work,
)
from review_sensei.service import ReviewService
from review_sensei.session import InMemorySessionLedger, SessionIdentity
from review_sensei.stages import ReviewCategory, Stage
from review_sensei.validation import ReviewLimits


class _RecordingProvider:
    name = "fixture"
    model: str | None = "fixture-model"
    endpoint = "https://fixture.example/api"
    completion_contract = "bounded-complete-text-v1"

    def __init__(self, responses: list[str] | None = None) -> None:
        self.responses = list(responses or [])
        self.requests: list[ProviderRequest] = []

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        payload = (
            self.responses.pop(0)
            if self.responses
            else json.dumps({"summary": "Reviewed fitting chunk.", "comments": []})
        )
        return ProviderResponse(text=payload, provider=self.name, model=self.model)


def _doc(path: str, content: str) -> ReviewDocument:
    return ReviewDocument(
        path,
        content,
        hashlib.sha256(content.encode("utf-8")).hexdigest(),
    )


def _decision(doc: ReviewDocument, *, reason: str) -> DocumentDecision:
    return DocumentDecision(
        doc.path,
        "selected",
        reason,
        doc.sha256,
        doc.sha256,
        len(doc.content.encode("utf-8")),
    )


class IndivisibleOverflowG09Tests(unittest.TestCase):
    def test_single_oversized_hunk_refuses_without_truncation_or_retry_suggestion(
        self,
    ) -> None:
        giant_line = "+" + ("x" * 600)
        diff = (
            "diff --git a/src/huge.py b/src/huge.py\n"
            "--- a/src/huge.py\n"
            "+++ b/src/huge.py\n"
            "@@ -1 +1,2 @@\n"
            " keep = True\n"
            f"{giant_line}\n"
        )
        limits = ReviewLimits(max_diff_bytes=160)
        plan = plan_change(
            diff,
            limits=limits,
            orchestrate=True,
            work_budget=TotalWorkBudget(max_chunks=16),
        )
        self.assertEqual(plan.chunks, ())
        self.assertFalse(plan.coverage.fully_reviewed)
        self.assertEqual(plan.coverage.approval_state(), "partial")

        # Even hunk subdivision preserves the single giant line intact rather than truncating.
        record = (plan.analysis.file_records or ())[0]
        hunk = record.hunks[0]
        snapshot = EvidenceSnapshot()
        fragments = subdivide_hunk(
            record,
            hunk,
            snapshot,
            admit=lambda part: len(part.diff.encode("utf-8")) <= 160,
        )
        self.assertEqual(len(fragments), 2)
        self.assertTrue(any(giant_line in part.diff for part in fragments))

        report = classify_change_coverage(plan)
        self.assertFalse(report.fully_reviewed)
        self.assertTrue(report.enumeration_complete)
        self.assertEqual(report.approval_state, "partial")
        self.assertFalse(report.truncated_mandatory_context)
        self.assertFalse(report.unbounded_retry_suggested)
        self.assertEqual(len(report.units), 1)

        unit = report.units[0]
        self.assertEqual(unit.unit_kind, "oversized_hunk")
        self.assertEqual(unit.path, "src/huge.py")
        self.assertEqual(unit.hunk_index, 1)
        self.assertIsNotNone(unit.unit_bytes)
        assert unit.unit_bytes is not None
        self.assertGreater(unit.unit_bytes, 160)
        self.assertEqual(unit.limit_bytes, 160)
        self.assertTrue(unit.indivisible)
        self.assertNotIn("review_remaining", unit.permitted_routes)
        self.assertEqual(
            unit.permitted_routes,
            ("split_oversized_change", "accept_coverage_uncertainty"),
        )
        self.assertIn("src/huge.py hunk #1", unit.diagnostic)
        self.assertIn(
            "accept-coverage-uncertainty --reason", report.summary_diagnostic()
        )

    def test_oversized_hunkless_file_is_classified_with_exact_bytes(self) -> None:
        long_path = "src/deep/nested/module_with_oversized_hunkless_mode_header.py"
        hunkless_diff = (
            f"diff --git a/{long_path} b/{long_path}\n"
            "old mode 100644\n"
            "new mode 100755\n"
        )
        limits = ReviewLimits(max_diff_bytes=80)
        plan = plan_change(
            hunkless_diff,
            limits=limits,
            orchestrate=True,
            work_budget=TotalWorkBudget(max_chunks=32),
        )
        self.assertEqual(plan.chunks, ())
        record = (plan.analysis.file_records or ())[0]
        self.assertEqual(record.hunks, ())
        self.assertEqual(
            [(fcov.path, fcov.outcome, fcov.reason) for fcov in plan.coverage.files],
            [(long_path, "unsupported", "too-large-file")],
        )

        report = classify_change_coverage(plan)
        self.assertEqual(len(report.units), 1)
        unit = report.units[0]
        self.assertEqual(unit.unit_kind, "oversized_hunkless_file")
        self.assertEqual(unit.path, long_path)
        self.assertIsNone(unit.hunk_index)
        self.assertEqual(unit.unit_bytes, len(record.text.encode("utf-8")))
        assert unit.unit_bytes is not None
        self.assertGreater(unit.unit_bytes, 80)
        self.assertEqual(unit.limit_bytes, 80)
        self.assertTrue(unit.indivisible)
        self.assertNotIn("review_remaining", unit.permitted_routes)

    def test_mandatory_prompt_overflow_preserves_pinned_documents_with_zero_calls(
        self,
    ) -> None:
        small_diff = (
            "diff --git a/src/app.py b/src/app.py\n"
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1 +1,2 @@\n"
            " keep\n"
            "+change\n"
        )
        pinned = _doc("docs/architecture/mandatory-rules.md", "MUST " + ("r" * 52_000))
        optional = _doc("docs/guide.md", "Note " + ("g" * 52_000))
        lens = ReviewLensContext(
            "architecture",
            documents=(pinned, optional),
            document_selection=DocumentSelectionReport(
                (
                    _decision(pinned, reason="pinned"),
                    _decision(optional, reason="configured-source"),
                )
            ),
        )
        request = ReviewRequest(
            diff=small_diff,
            repository="owner/repo",
            pull_request_number=9,
            base_sha="1" * 40,
            head_sha="2" * 40,
            active_category_ids=("architecture",),
            lens_contexts=(lens,),
        )
        with self.assertRaises(PromptContextOverflow) as overflow_ctx:
            admit_prompt_context(
                request,
                active_category_ids=("architecture",),
                changed_paths=("src/app.py",),
                render=lambda selected: (
                    "\n".join(
                        doc.content
                        for ctx in selected.lens_contexts
                        for doc in ctx.documents
                    )
                    + "\n"
                    + selected.diff
                ),
                fits=lambda prompt: (
                    len(prompt.encode("utf-8")) <= LEGACY_ASSESSMENT_PROMPT_BYTES
                ),
            )

        provider = _RecordingProvider()
        category = ReviewCategory(
            id="architecture",
            title="Architecture",
            focus=("Check mandatory architecture rules.",),
        )
        service = ReviewService(
            provider,
            stages=(
                Stage(
                    "review",
                    "Categories:\n{review_categories}\nRules:\n{review_context}\nDiff:\n{diff}",
                    ("summary", "comments"),
                    categories=(category,),
                ),
            ),
            budget=ResourceBudget.create(max_provider_calls=4),
            work_budgets=ReviewWorkBudgets(mode="unified"),
        )
        run = service.run(request)
        self.assertEqual(len(provider.requests), 0)
        self.assertIsNotNone(run.result)
        assert run.result is not None
        self.assertEqual(run.result.review_status, "partial")
        self.assertIn("mandatory-context-and-evidence-oversized", run.result.summary)

        report = classify_mandatory_prompt_overflow(
            request,
            active_category_ids=("architecture",),
            max_prompt_bytes=LEGACY_ASSESSMENT_PROMPT_BYTES,
            error=overflow_ctx.exception,
            result=run.result,
        )
        self.assertFalse(report.fully_reviewed)
        self.assertFalse(report.truncated_mandatory_context)
        self.assertEqual(len(report.units), 1)
        unit = report.units[0]
        self.assertEqual(unit.unit_kind, "mandatory_prompt_overflow")
        self.assertEqual(unit.path, "docs/architecture/mandatory-rules.md")
        self.assertEqual(
            unit.mandatory_documents, ("docs/architecture/mandatory-rules.md",)
        )
        self.assertNotIn("docs/guide.md", unit.mandatory_documents)
        self.assertEqual(unit.unit_bytes, len(pinned.content.encode("utf-8")))
        self.assertEqual(unit.limit_bytes, LEGACY_ASSESSMENT_PROMPT_BYTES)
        self.assertEqual(unit.coverage_reason, "chunk-preflight-failed")
        self.assertTrue(unit.indivisible)
        self.assertNotIn("review_remaining", unit.permitted_routes)
        self.assertEqual(
            request.lens_contexts[0].documents[0].content,
            pinned.content,
        )
        self.assertTrue(report.incomplete_coverage_tokens)
        self.assertIn("docs/architecture/mandatory-rules.md", unit.diagnostic)
        self.assertIn(str(LEGACY_ASSESSMENT_PROMPT_BYTES), unit.diagnostic)

    def test_binary_generated_lockfile_rename_and_deletion_classifications(
        self,
    ) -> None:
        mixed_diff = (
            "diff --git a/assets/logo.png b/assets/logo.png\n"
            "index 1111111..2222222 100644\n"
            "Binary files a/assets/logo.png and b/assets/logo.png differ\n"
            "diff --git a/dist/app.min.js b/dist/app.min.js\n"
            "--- a/dist/app.min.js\n"
            "+++ b/dist/app.min.js\n"
            "@@ -1 +1,2 @@\n"
            " keep\n"
            "+minified\n"
            "diff --git a/Cargo.lock b/Cargo.lock\n"
            "--- a/Cargo.lock\n"
            "+++ b/Cargo.lock\n"
            "@@ -1 +1,2 @@\n"
            " version = 3\n"
            "+name = 'pkg'\n"
            "diff --git a/docs/old.md b/guides/new.md\n"
            "similarity index 100%\n"
            "rename from docs/old.md\n"
            "rename to guides/new.md\n"
            "diff --git a/src/legacy.py b/src/legacy.py\n"
            "deleted file mode 100644\n"
            "--- a/src/legacy.py\n"
            "+++ /dev/null\n"
            "@@ -1 +0,0 @@\n"
            "-legacy_flag = True\n"
        )
        plan = plan_change(mixed_diff, orchestrate=True)
        report = classify_change_coverage(plan)
        self.assertFalse(report.fully_reviewed)
        self.assertEqual(
            report.reviewed_paths,
            ("docs/old.md", "guides/new.md", "src/legacy.py"),
        )
        by_path = {unit.path: unit for unit in report.units}
        self.assertEqual(
            set(by_path), {"assets/logo.png", "dist/app.min.js", "Cargo.lock"}
        )
        self.assertEqual(by_path["assets/logo.png"].unit_kind, "binary_file")
        self.assertEqual(
            by_path["assets/logo.png"].permitted_routes,
            ("human_file_review", "accept_coverage_uncertainty"),
        )
        self.assertEqual(by_path["dist/app.min.js"].unit_kind, "generated_file")
        self.assertEqual(by_path["Cargo.lock"].unit_kind, "unsupported_lockfile")

        # Renamed file with an oversized hunk emits a single unit without alias double-counting.
        renamed_oversized = (
            "diff --git a/src/old_big.py b/src/new_big.py\n"
            "similarity index 50%\n"
            "rename from src/old_big.py\n"
            "rename to src/new_big.py\n"
            "--- a/src/old_big.py\n"
            "+++ b/src/new_big.py\n"
            "@@ -1 +1,2 @@\n"
            " base = 1\n"
            "+" + ("x" * 600) + "\n"
        )
        renamed_plan = plan_change(
            renamed_oversized,
            limits=ReviewLimits(max_diff_bytes=160),
            orchestrate=True,
        )
        renamed_report = classify_change_coverage(renamed_plan)
        self.assertEqual(len(renamed_report.units), 1)
        self.assertEqual(renamed_report.units[0].unit_kind, "oversized_hunk")
        self.assertEqual(renamed_report.units[0].path, "src/new_big.py")
        assert renamed_report.units[0].unit_bytes is not None
        self.assertGreater(renamed_report.units[0].unit_bytes, 160)

        # Deleted-side comment anchors remain valid on LEFT.
        deletion_only = (
            "diff --git a/src/legacy.py b/src/legacy.py\n"
            "deleted file mode 100644\n"
            "--- a/src/legacy.py\n"
            "+++ /dev/null\n"
            "@@ -1 +0,0 @@\n"
            "-legacy_flag = True\n"
        )
        provider = _RecordingProvider(
            [
                json.dumps(
                    {
                        "summary": "Reviewed deletion.",
                        "comments": [
                            ReviewComment(
                                path="src/legacy.py",
                                line=1,
                                body="Confirm callers stopped importing legacy_flag.",
                                side="LEFT",
                            ).to_dict()
                        ],
                    }
                )
            ]
        )
        del_plan = plan_change(deletion_only)
        del_result = ReviewService(provider).review(ReviewRequest(diff=deletion_only))
        del_report = classify_change_coverage(del_plan, result=del_result)
        self.assertTrue(del_report.fully_reviewed)
        self.assertEqual(del_report.units, ())
        self.assertIn("Complete coverage verified", del_report.summary_diagnostic())

    def test_incomplete_enumeration_budget_exhaustion_and_work_plan_unprocessed(
        self,
    ) -> None:
        two_files = (
            "diff --git a/src/a.py b/src/a.py\n"
            "--- a/src/a.py\n"
            "+++ b/src/a.py\n"
            "@@ -1 +1,2 @@\n"
            " a\n"
            "+a2\n"
            "diff --git a/src/b.py b/src/b.py\n"
            "--- a/src/b.py\n"
            "+++ b/src/b.py\n"
            "@@ -1 +1,2 @@\n"
            " b\n"
            "+b2\n"
        )
        inc_plan = plan_change(
            two_files,
            orchestrate=True,
            work_budget=TotalWorkBudget(max_total_files=1),
        )
        inc_report = classify_change_coverage(inc_plan)
        self.assertFalse(inc_report.enumeration_complete)
        self.assertEqual(inc_report.approval_state, "incomplete")
        self.assertTrue(
            any(u.unit_kind == "incomplete_enumeration" for u in inc_report.units)
        )

        # Call-budget exhaustion is distinct from indivisibility and permits review_remaining.
        budget_plan = plan_change(
            two_files,
            limits=ReviewLimits(max_diff_files=1),
            orchestrate=True,
            work_budget=TotalWorkBudget(max_chunks=1),
        )
        budget_report = classify_change_coverage(budget_plan)
        self.assertEqual(len(budget_report.units), 1)
        b_unit = budget_report.units[0]
        self.assertEqual(b_unit.unit_kind, "budget_exhausted")
        self.assertFalse(b_unit.indivisible)
        self.assertIn("review_remaining", b_unit.permitted_routes)

        # Also classify partial_chunk_failure and plan_work unprocessed reasons.
        failed_cov = CoverageManifest(
            files=(
                FileCoverage(path="src/a.py", outcome="reviewed"),
                FileCoverage(
                    path="src/b.py",
                    outcome="partially-reviewed",
                    reason="chunk-failed",
                ),
            ),
            hunks=(
                HunkCoverage(index=1, path="src/a.py", outcome="reviewed"),
                HunkCoverage(
                    index=2,
                    path="src/b.py",
                    outcome="partially-reviewed",
                    reason="chunk-failed",
                ),
            ),
            enumerated_paths=("src/a.py", "src/b.py"),
        )
        failed_report = classify_change_coverage(
            replace(budget_plan, coverage=failed_cov)
        )
        self.assertEqual(failed_report.units[0].unit_kind, "partial_chunk_failure")
        self.assertFalse(failed_report.units[0].indivisible)

        # Non-transient coverage reasons fail closed with indivisible=True and never offer review_remaining.
        non_retryable_cov = CoverageManifest(
            files=(
                FileCoverage(
                    path="src/a.py",
                    outcome="unsupported",
                    reason="no-review-stage",
                ),
                FileCoverage(
                    path="src/b.py",
                    outcome="unsupported",
                    reason="chunk-preflight-failed",
                ),
            ),
            enumerated_paths=("src/a.py", "src/b.py"),
        )
        non_retryable_report = classify_change_coverage(
            replace(budget_plan, coverage=non_retryable_cov)
        )
        for non_retry_unit in non_retryable_report.units:
            self.assertTrue(non_retry_unit.indivisible)
            self.assertNotIn("review_remaining", non_retry_unit.permitted_routes)

        snapshot = EvidenceSnapshot()
        analysis = analyze_diff(two_files)
        records = tuple(
            EvidenceRecord.from_diff_record(rec, snapshot)
            for rec in (analysis.file_records or ())
        )
        bundle = EvidenceBundle(snapshot, records)
        fake_missing_id = "f" * 64
        reqs = (
            WorkRequirement("req-fit", (records[0].evidence_id,), (1,)),
            WorkRequirement("req-budget", (records[1].evidence_id,), (2,)),
            WorkRequirement("req-missing", (fake_missing_id,), ()),
        )
        one_record_bytes = len(records[0].diff.encode("utf-8"))
        effective_budget = ReviewWorkBudgets(
            mode="unified", batch_diff_bytes=one_record_bytes + 10
        ).effective(
            limits=ReviewLimits(),
            resource=ResourceBudget.create(max_provider_calls=1),
            mode="discovery",
            capabilities=None,
            output_tokens=None,
        )
        wplan = plan_work(
            "discovery",
            bundle,
            reqs,
            budgets=effective_budget,
            render=lambda batch: ProviderRequest(prompt=batch.diff_context),
            max_batches=1,
        )
        wunits = classify_work_plan_unprocessed(
            wplan, max_diff_bytes=4096, max_prompt_bytes=49152
        )
        kinds = {u.unit_kind for u in wunits}
        self.assertEqual(kinds, {"budget_exhausted", "incomplete_enumeration"})

        # Mandatory prompt overflow + oversized hunk inside plan_work.
        def overflow_renderer(batch: WorkBatch) -> ProviderRequest:
            if "req-prompt" in batch.requirement_ids:
                raise PromptContextOverflow("mandatory prompt overflow")
            return ProviderRequest(prompt=batch.diff_context)

        tiny_budget = ReviewWorkBudgets(mode="unified", batch_diff_bytes=10).effective(
            limits=ReviewLimits(),
            resource=ResourceBudget.create(max_provider_calls=4),
            mode="discovery",
            capabilities=None,
            output_tokens=None,
        )
        wplan2 = plan_work(
            "discovery",
            bundle,
            (WorkRequirement("req-oversized", (records[0].evidence_id,), (1,)),),
            budgets=tiny_budget,
            render=overflow_renderer,
        )
        wunits2 = classify_work_plan_unprocessed(
            wplan2, max_diff_bytes=10, max_prompt_bytes=49152
        )
        self.assertEqual(wunits2[0].unit_kind, "oversized_hunk")

        prompt_budget = ReviewWorkBudgets(mode="unified").effective(
            limits=ReviewLimits(),
            resource=ResourceBudget.create(max_provider_calls=4),
            mode="discovery",
            capabilities=None,
            output_tokens=None,
        )
        wplan3 = plan_work(
            "discovery",
            bundle,
            (WorkRequirement("req-prompt", (records[0].evidence_id,), (1,)),),
            budgets=prompt_budget,
            render=overflow_renderer,
        )
        wunits3 = classify_work_plan_unprocessed(
            wplan3, max_diff_bytes=4096, max_prompt_bytes=49152
        )
        self.assertEqual(wunits3[0].unit_kind, "mandatory_prompt_overflow")

        # Stale/synthetic unprocessed identity absent from work_plan.requirements fails closed without KeyError.
        orphan_plan = copy.copy(wplan3)
        object.__setattr__(
            orphan_plan,
            "unprocessed",
            (*wplan3.unprocessed, ("req-orphan", "provider-call-budget")),
        )
        orphan_units = classify_work_plan_unprocessed(
            orphan_plan, max_diff_bytes=4096, max_prompt_bytes=49152
        )
        self.assertEqual(len(orphan_units), 2)
        self.assertEqual(orphan_units[1].unit_kind, "incomplete_enumeration")
        self.assertTrue(orphan_units[1].indivisible)
        self.assertIn("req-orphan", orphan_units[1].diagnostic)

    def test_report_and_unit_validation_guards_fail_closed(self) -> None:
        self.assertEqual(
            PERMITTED_RECOVERY_ROUTES, frozenset(RECOVERY_COMMAND_TEMPLATES)
        )
        self.assertEqual(
            DEFERRED_FITTING_UNIT_KINDS,
            frozenset({"budget_exhausted", "partial_chunk_failure"}),
        )
        with self.assertRaisesRegex(
            ReviewInputError, "never suggest review_remaining retries"
        ):
            IndivisibleWorkUnit(
                unit_kind="oversized_hunk",
                path="src/huge.py",
                hunk_index=1,
                unit_bytes=500,
                limit_bytes=100,
                coverage_outcome="unsupported",
                coverage_reason="too-large-hunk",
                indivisible=True,
                permitted_routes=("review_remaining",),
                diagnostic="invalid retry suggestion on indivisible unit",
            )
        with self.assertRaisesRegex(ReviewInputError, "must set indivisible=False"):
            IndivisibleWorkUnit(
                unit_kind="budget_exhausted",
                path="src/huge.py",
                hunk_index=1,
                unit_bytes=50,
                limit_bytes=100,
                coverage_outcome="budget-exhausted",
                coverage_reason="provider-call-budget",
                indivisible=True,
                permitted_routes=("accept_coverage_uncertainty",),
                diagnostic="budget exhausted flipped to indivisible",
            )
        with self.assertRaisesRegex(ReviewInputError, "must set indivisible=True"):
            IndivisibleWorkUnit(
                unit_kind="oversized_hunk",
                path="src/huge.py",
                hunk_index=1,
                unit_bytes=500,
                limit_bytes=100,
                coverage_outcome="unsupported",
                coverage_reason="too-large-hunk",
                indivisible=False,
                permitted_routes=("accept_coverage_uncertainty",),
                diagnostic="oversized hunk flipped to non-indivisible",
            )
        valid_unit = IndivisibleWorkUnit(
            unit_kind="oversized_hunk",
            path="src/huge.py",
            hunk_index=1,
            unit_bytes=500,
            limit_bytes=100,
            coverage_outcome="unsupported",
            coverage_reason="too-large-hunk",
            indivisible=True,
            permitted_routes=("accept_coverage_uncertainty",),
            diagnostic="valid unit",
        )
        base_report_kwargs: dict[str, object] = {
            "fully_reviewed": False,
            "enumeration_complete": True,
            "approval_state": "partial",
            "truncated_mandatory_context": False,
            "unbounded_retry_suggested": False,
            "units": (valid_unit,),
        }
        isolated_cases: tuple[tuple[str, dict[str, object], str], ...] = (
            (
                "truncated_mandatory_context",
                {"truncated_mandatory_context": True},
                "mandatory context truncation is forbidden",
            ),
            (
                "unbounded_retry_suggested",
                {"unbounded_retry_suggested": True},
                "unbounded retry suggestions are forbidden",
            ),
            (
                "fully_reviewed_with_units",
                {"fully_reviewed": True},
                "fully_reviewed cannot be true",
            ),
            (
                "fully_reviewed_incomplete_enumeration",
                {
                    "fully_reviewed": True,
                    "enumeration_complete": False,
                    "approval_state": "reviewed",
                    "units": (),
                },
                "fully_reviewed cannot be true",
            ),
            (
                "approval_state",
                {"approval_state": "invalid"},
                "approval_state is unsupported",
            ),
        )
        for label, overrides, expected_message in isolated_cases:
            self.assertTrue(set(overrides).issubset(base_report_kwargs))
            merged = {**base_report_kwargs, **overrides}
            with (
                self.subTest(case=label),
                self.assertRaisesRegex(ReviewInputError, expected_message),
            ):
                IndivisibleWorkReport(**merged)  # type: ignore[arg-type]
        serialized = IndivisibleWorkReport(**base_report_kwargs).to_dict()  # type: ignore[arg-type]
        self.assertFalse(serialized["fully_reviewed"])
        self.assertEqual(len(serialized["units"]), 1)  # type: ignore[arg-type]

    def test_end_to_end_recovery_via_accept_coverage_uncertainty_and_split_change(
        self,
    ) -> None:
        head_1 = "1" * 40
        base_sha = "0" * 40
        policy_digest = "a" * 64
        max_diff_bytes = 180
        giant_added = "+" + ("z" * (max_diff_bytes * 4))
        mixed_diff = (
            "diff --git a/src/safe.py b/src/safe.py\n"
            "--- a/src/safe.py\n"
            "+++ b/src/safe.py\n"
            "@@ -1 +1,2 @@\n"
            " safe = 1\n"
            "+safe = 2\n"
            "diff --git a/src/huge.py b/src/huge.py\n"
            "--- a/src/huge.py\n"
            "+++ b/src/huge.py\n"
            "@@ -1 +1,2 @@\n"
            " table = []\n"
            f"{giant_added}\n"
        )
        limits = ReviewLimits(max_diff_bytes=max_diff_bytes, max_diff_files=1)
        provider = _RecordingProvider()
        service = ReviewService(
            provider,
            stages=(Stage("review", "Review:\n{diff}", ("summary", "comments")),),
            budget=ResourceBudget.create(max_provider_calls=4),
        )
        req_1 = ReviewRequest(
            diff=mixed_diff,
            repository="owner/repo",
            pull_request_number=42,
            base_sha=base_sha,
            head_sha=head_1,
            limits=limits,
            orchestrate_large_changes=True,
        )
        plan_1 = plan_change(mixed_diff, limits=limits, orchestrate=True)
        run_1 = service.run(req_1)
        self.assertIsNotNone(run_1.result)
        assert run_1.result is not None
        result_1 = run_1.result

        # Only src/safe.py was sent to the provider; src/huge.py was NEVER truncated.
        self.assertEqual(len(provider.requests), 1)
        self.assertIn("src/safe.py", provider.requests[0].prompt)
        self.assertNotIn("src/huge.py", provider.requests[0].prompt)
        self.assertEqual(result_1.review_status, "partial")

        report_1 = classify_change_coverage(plan_1, result=result_1)
        self.assertEqual(report_1.reviewed_paths, ("src/safe.py",))
        self.assertEqual(len(report_1.units), 1)
        self.assertEqual(report_1.units[0].path, "src/huge.py")
        assert (
            report_1.units[0].unit_bytes is not None
            and report_1.units[0].limit_bytes is not None
        )
        self.assertGreater(report_1.units[0].unit_bytes, report_1.units[0].limit_bytes)

        # Blind `review remaining` makes zero extra provider calls and flags no progress.
        cont_run = service.run(req_1, prior_partial=result_1)
        self.assertEqual(len(provider.requests), 1)
        assert cont_run.result is not None
        self.assertIn("accept-coverage-uncertainty", cont_run.result.summary)

        # Before recovery: merge gate stays action_required and approval is withheld.
        eligibility = approval_eligibility_from_result(
            result_1,
            head_sha=head_1,
            base_sha=base_sha,
            enabled=True,
            app_authored=False,
        )
        self.assertFalse(
            eligibility.evaluate(
                app_authored=False, has_open_review_threads=False
            ).approved
        )
        pre_check = review_check_outcome(
            policy="blocking",
            review_status=result_1.review_status,
            required_fixes=False,
            coverage_uncertainty_accepted=False,
        )
        self.assertEqual(pre_check.conclusion, "action_required")

        # Recovery Route A: Maintainer records explicit bounded coverage-uncertainty disposition.
        ledger = InMemorySessionLedger()
        identity = SessionIdentity(
            repository="owner/repo", pull_request=42, repository_id=123
        )
        now = datetime(2026, 11, 11, 4, 0, tzinfo=timezone.utc)
        incomplete_tokens = incomplete_coverage_from_result(result_1)
        self.assertEqual(incomplete_tokens, report_1.incomplete_coverage_tokens)

        cmd = parse_maintainer_command(
            "@reviewsensei accept-coverage-uncertainty --reason Manual audit of generated table in src/huge.py",
            actor="alice",
            head_sha=head_1,
        )
        assert cmd is not None
        record, cmd_result = apply_session_command(
            ledger,
            identity,
            cmd,
            now=now,
            incomplete_coverage=incomplete_tokens,
            actor_role="maintain",
            policy_digest=policy_digest,
            provenance="comment:1001",
        )
        self.assertTrue(cmd_result.applied)
        self.assertTrue(
            session_honors_coverage_uncertainty(
                record,
                head_sha=head_1,
                incomplete_coverage=incomplete_tokens,
                policy_digest=policy_digest,
                now=now,
            )
        )
        post_check = review_check_outcome(
            policy="blocking",
            review_status=result_1.review_status,
            required_fixes=False,
            coverage_uncertainty_accepted=True,
        )
        self.assertEqual(post_check.conclusion, "success")
        self.assertEqual(
            post_check.title, "Coverage uncertainty accepted by maintainer"
        )

        # Waiver never carries over to a different head or altered incomplete scope.
        self.assertFalse(
            session_honors_coverage_uncertainty(
                record,
                head_sha="2" * 40,
                incomplete_coverage=incomplete_tokens,
                policy_digest=policy_digest,
                now=now,
            )
        )
        self.assertFalse(
            session_honors_coverage_uncertainty(
                record,
                head_sha=head_1,
                incomplete_coverage=(
                    *incomplete_tokens,
                    "file:src/extra.py:unsupported",
                ),
                policy_digest=policy_digest,
                now=now,
            )
        )

        # Recovery Route B: Binary-only partial converges cleanly once human_file_review is satisfied.
        binary_diff = (
            "diff --git a/src/safe.py b/src/safe.py\n"
            "--- a/src/safe.py\n"
            "+++ b/src/safe.py\n"
            "@@ -1 +1,2 @@\n"
            " safe = 1\n"
            "+safe = 2\n"
            "diff --git a/assets/diagram.png b/assets/diagram.png\n"
            "new file mode 100644\n"
            "index 0000000..1111111\n"
            "Binary files /dev/null and b/assets/diagram.png differ\n"
        )
        bin_provider = _RecordingProvider()
        bin_service = ReviewService(
            bin_provider,
            stages=(Stage("review", "Review:\n{diff}", ("summary", "comments")),),
        )
        bin_run = bin_service.run(
            ReviewRequest(diff=binary_diff, orchestrate_large_changes=True)
        )
        assert bin_run.result is not None and bin_run.result.coverage is not None
        self.assertTrue(coverage_only_binary(bin_run.result.coverage))
        self.assertTrue(bin_run.result.coverage_only_partial)
        blob = UnsupportedFile(
            path="assets/diagram.png",
            change="added",
            old_blob=None,
            new_blob="b" * 40,
            new_mode="100644",
        )
        self.assertEqual(blob.paths, ("assets/diagram.png",))
        bin_check = review_check_outcome(
            policy="blocking",
            review_status=bin_run.result.review_status,
            required_fixes=False,
            mixed_coverage_satisfied=True,
        )
        self.assertEqual(bin_check.conclusion, "success")

        # Recovery Route C: Splitting the oversized hunk on head_2 converges under unchanged limits.
        head_2 = "2" * 40
        split_diff = (
            "diff --git a/src/safe.py b/src/safe.py\n"
            "--- a/src/safe.py\n"
            "+++ b/src/safe.py\n"
            "@@ -1 +1,2 @@\n"
            " safe = 1\n"
            "+safe = 2\n"
            "diff --git a/src/huge.py b/src/huge.py\n"
            "--- a/src/huge.py\n"
            "+++ b/src/huge.py\n"
            "@@ -1,2 +1,3 @@\n"
            " table = []\n"
            "+part_1 = [1, 2]\n"
            " end = True\n"
            "@@ -10,2 +11,3 @@\n"
            " more = []\n"
            "+part_2 = [3, 4]\n"
            " done = True\n"
        )
        split_provider = _RecordingProvider()
        split_service = ReviewService(
            split_provider,
            stages=(Stage("review", "Review:\n{diff}", ("summary", "comments")),),
            budget=ResourceBudget.create(max_provider_calls=4),
        )
        req_2 = replace(req_1, diff=split_diff, head_sha=head_2)
        plan_2 = plan_change(split_diff, limits=limits, orchestrate=True)
        run_2 = split_service.run(req_2)
        assert run_2.result is not None and run_2.result.coverage is not None
        self.assertEqual(run_2.result.review_status, "complete")
        self.assertTrue(run_2.result.coverage.fully_reviewed)
        report_2 = classify_change_coverage(plan_2, result=run_2.result)
        self.assertTrue(report_2.fully_reviewed)
        self.assertEqual(report_2.approval_state, "reviewed")
        self.assertEqual(report_2.units, ())


if __name__ == "__main__":
    unittest.main()
