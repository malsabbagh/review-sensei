"""Tests for mandatory-context-and-evidence-oversized diagnostics and sizing."""

from __future__ import annotations

import hashlib
import json
import unittest

from review_sensei.budgets import (
    COMPLETE_TEXT_CONTRACT,
    LEGACY_ASSESSMENT_PROMPT_BYTES,
    ProviderCapabilities,
    ReviewWorkBudgets,
)
from review_sensei.context import PromptContextOverflow, admit_prompt_context
from review_sensei.diff import analyze_diff
from review_sensei.discovery_work import PENDING_APPENDIX_RESERVE, _batch_scope_note
from review_sensei.document_context import DocumentDecision, DocumentSelectionReport
from review_sensei.evidence import EvidenceBundle, EvidenceRecord, EvidenceSnapshot
from review_sensei.hunk_subdivision import SubdivisionAllowance, subdivide_hunk
from review_sensei.models import (
    ProviderRequest,
    ProviderResponse,
    ReviewDocument,
    ReviewLensContext,
    ReviewRequest,
)
from review_sensei.operational_boundaries import (
    classify_mandatory_prompt_overflow,
    classify_work_plan_unprocessed,
)
from review_sensei.outcomes import ResourceBudget
from review_sensei.planning import WorkBatch, WorkRequirement, plan_work
from review_sensei.providers.base import ReviewProvider
from review_sensei.service import ReviewService
from review_sensei.stages import ReviewCategory, Stage
from review_sensei.validation import ReviewLimits


class _RecordingProvider(ReviewProvider):
    name = "fake"
    model = "fake-model"
    allow_model_override = True

    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        return ProviderResponse(
            text=json.dumps({"summary": "Reviewed.", "comments": []}),
            provider=self.name,
            model=self.model,
        )


def _make_lens(category_id: str, *, doc_path: str, size: int) -> ReviewLensContext:
    content = "x" * size
    sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
    doc = ReviewDocument(
        path=doc_path,
        content=content,
        sha256=sha,
    )
    report = DocumentSelectionReport(
        (
            DocumentDecision(
                path=doc_path,
                status="selected",
                reason="pinned",
                source_sha256=doc.sha256,
                payload_sha256=doc.sha256,
                source_bytes=len(doc.content.encode("utf-8")),
            ),
        ),
    )
    return ReviewLensContext(
        category_id=category_id,
        documents=(doc,),
        document_selection=report,
    )


class MandatoryOverflowDiagnosticsTests(unittest.TestCase):
    def test_effective_prompt_ceiling_bytes_reports_legacy_clamp(self) -> None:
        budgets = ReviewWorkBudgets(mode="unified")
        unqualified = budgets.effective(
            limits=ReviewLimits(),
            resource=ResourceBudget.create(),
            mode="discovery",
            capabilities=None,
        )
        self.assertEqual(
            unqualified.effective_prompt_ceiling_bytes(),
            (LEGACY_ASSESSMENT_PROMPT_BYTES, True),
        )

        small = ReviewWorkBudgets(mode="unified", batch_prompt_bytes=16384).effective(
            limits=ReviewLimits(),
            resource=ResourceBudget.create(),
            mode="discovery",
            capabilities=None,
        )
        self.assertEqual(small.effective_prompt_ceiling_bytes(), (16384, False))

        caps = ProviderCapabilities(
            context_tokens=128_000,
            max_output_tokens=4096,
            qualified=True,
            framing_tokens=512,
            provider_name="fake",
            model="fake-model",
            endpoint="https://api.example.com/v1",
            completion_contract=COMPLETE_TEXT_CONTRACT,
        )
        qualified = budgets.effective(
            limits=ReviewLimits(),
            resource=ResourceBudget.create(),
            mode="discovery",
            capabilities=caps,
        )
        ceiling, clamped = qualified.effective_prompt_ceiling_bytes(output_tokens=4096)
        self.assertFalse(clamped)
        margin = max(512, (128_000 + 19) // 20)
        self.assertEqual(
            ceiling,
            min(qualified.batch_prompt_bytes, 128_000 - 512 - 4096 - margin),
        )

    def test_admit_prompt_context_captures_minimal_failure_breakdown(self) -> None:
        diff = """diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -10,1 +10,2 @@
 keep
+added
"""
        lens = _make_lens("arch", doc_path="docs/mandatory.md", size=1200)
        req = ReviewRequest(
            diff=diff,
            active_category_ids=("arch",),
            lens_contexts=(lens,),
        )
        with self.assertRaises(PromptContextOverflow) as ctx:
            admit_prompt_context(
                req,
                active_category_ids=("arch",),
                changed_paths=("src/app.py",),
                render=lambda selected: (
                    "HDR:"
                    + "".join(
                        d.content for c in selected.lens_contexts for d in c.documents
                    )
                    + selected.diff
                ),
                fits=lambda prompt: len(prompt.encode("utf-8")) <= 1000,
                prompt_ceiling_bytes=1000,
                extra_prompt_bytes=50,
                clamped_by_legacy_48k=False,
            )
        exc = ctx.exception
        self.assertEqual(exc.prompt_ceiling_bytes, 1000)
        self.assertEqual(exc.mandatory_doc_paths, ("docs/mandatory.md",))
        self.assertEqual(exc.mandatory_doc_bytes, 1200)
        self.assertEqual(exc.evidence_bytes, len(diff.encode("utf-8")))
        expected_rendered = 4 + 1200 + len(diff.encode("utf-8")) + 50
        self.assertEqual(exc.rendered_prompt_bytes, expected_rendered)
        self.assertEqual(exc.over_budget_bytes, expected_rendered - 1000)
        self.assertEqual(
            exc.framing_bytes, expected_rendered - exc.evidence_bytes - 1200
        )
        self.assertFalse(exc.clamped_by_legacy_48k)

    def test_subdivide_hunk_records_all_four_stop_reasons(self) -> None:
        snapshot = EvidenceSnapshot(base_sha="a" * 40, head_sha="b" * 40)
        # 1. single-line-oversized
        one_line_diff = (
            "diff --git a/src/a.py b/src/a.py\n"
            "new file mode 100644\n"
            "index 0000000..2222222\n"
            "--- /dev/null\n"
            "+++ b/src/a.py\n"
            "@@ -0,0 +1,1 @@\n"
            "+" + ("x" * 200) + "\n"
        )
        rec1 = analyze_diff(one_line_diff).file_records[0]
        reasons1: dict[str, str] = {}
        parts1 = subdivide_hunk(
            rec1,
            rec1.hunks[0],
            snapshot,
            admit=lambda part: False,
            unadmitted_reasons=reasons1,
        )
        self.assertEqual(reasons1[parts1[0].evidence_id], "single-line-oversized")

        # 2. per-hunk-segment-limit (max_parts=1)
        multi_line_diff = (
            "diff --git a/src/a.py b/src/a.py\n"
            "new file mode 100644\n"
            "index 0000000..2222222\n"
            "--- /dev/null\n"
            "+++ b/src/a.py\n"
            "@@ -0,0 +1,4 @@\n"
            "+line1\n+line2\n+line3\n+line4\n"
        )
        rec2 = analyze_diff(multi_line_diff).file_records[0]
        reasons2: dict[str, str] = {}
        parts2 = subdivide_hunk(
            rec2,
            rec2.hunks[0],
            snapshot,
            admit=lambda part: False,
            max_parts=1,
            unadmitted_reasons=reasons2,
        )
        self.assertEqual(reasons2[parts2[0].evidence_id], "per-hunk-segment-limit")

        # 3. run-subdivision-allowance-exhausted
        reasons3: dict[str, str] = {}
        parts3 = subdivide_hunk(
            rec2,
            rec2.hunks[0],
            snapshot,
            admit=lambda part: False,
            allowance=SubdivisionAllowance(remaining=0),
            unadmitted_reasons=reasons3,
        )
        self.assertEqual(
            reasons3[parts3[0].evidence_id], "run-subdivision-allowance-exhausted"
        )

        # 4. planning-deadline-exhausted
        def _expire() -> None:
            from review_sensei.context import PromptContextDeadline

            raise PromptContextDeadline("expired")

        reasons4: dict[str, str] = {}
        parts4 = subdivide_hunk(
            rec2,
            rec2.hunks[0],
            snapshot,
            admit=lambda part: False,
            before_probe=_expire,
            unadmitted_reasons=reasons4,
        )
        self.assertEqual(reasons4[parts4[0].evidence_id], "planning-deadline-exhausted")

    def test_per_stage_subdivision_allowance_prevents_cross_stage_starvation(
        self,
    ) -> None:
        lines = "\n".join(f"+line_{idx} = {idx}" for idx in range(1, 20))
        diff = (
            "diff --git a/src/big.py b/src/big.py\n"
            "new file mode 100644\n"
            "index 0000000..2222222\n"
            "--- /dev/null\n"
            "+++ b/src/big.py\n"
            "@@ -0,0 +1,19 @@\n"
            f"{lines}\n"
        )
        provider = _RecordingProvider()
        cat1 = ReviewCategory("c1", "C1", ("focus1",))
        cat2 = ReviewCategory("c2", "C2", ("focus2",))
        s1 = Stage(
            "stage_one",
            "Categories: {review_categories}\nDiff:\n<diff>{diff}</diff>",
            ("summary", "comments"),
            categories=(cat1,),
        )
        s2 = Stage(
            "stage_two",
            "Categories: {review_categories}\nDiff:\n<diff>{diff}</diff>",
            ("summary", "comments"),
            categories=(cat2,),
        )
        svc = ReviewService(
            provider,
            stages=(s1, s2),
            budget=ResourceBudget.create(max_provider_calls=16),
            work_budgets=ReviewWorkBudgets(mode="unified", batch_diff_bytes=350),
        )
        run = svc.run(ReviewRequest(diff=diff))
        self.assertEqual(run.outcome.status, "reviewed")
        self.assertTrue(run.result.coverage.fully_reviewed)

    def test_batch_scope_note_scales_conservatively_under_64k_prompt_ceiling(
        self,
    ) -> None:
        snapshot = EvidenceSnapshot(base_sha="a" * 40, head_sha="b" * 40)
        rec = EvidenceRecord(
            "src/a.py",
            "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1,1 +1,2 @@\n a\n+b\n",
            snapshot,
        )
        batch = WorkBatch(
            "discovery",
            (WorkRequirement("file:src/a.py", (rec.evidence_id,), (1,)),),
            (rec,),
        )
        many_paths = tuple(f"pkg/sub_{i}/module_{i}.py" for i in range(200))
        small_note = _batch_scope_note(
            batch,
            changed_paths=("src/a.py", *many_paths),
            max_other_paths_bytes=min(
                2048, max(512, LEGACY_ASSESSMENT_PROMPT_BYTES // 16)
            ),
        )
        self.assertLessEqual(len(small_note.encode("utf-8")), 4096)

    def test_plan_work_discovery_and_operational_boundaries_report_full_telemetry(
        self,
    ) -> None:
        # 25 hunks that all fail mandatory overflow -> summary stays bounded within PENDING_APPENDIX_RESERVE
        hunks = []
        for idx in range(1, 26):
            start = idx * 10
            hunks.append(
                f"@@ -{start},2 +{start},3 @@\n ctx_{idx}\n-old_{idx}\n+new_{idx}\n+extra_{idx}"
            )
        diff = (
            "diff --git a/src/large.py b/src/large.py\n"
            "index 1111111..2222222 100644\n"
            "--- a/src/large.py\n"
            "+++ b/src/large.py\n" + "\n".join(hunks) + "\n"
        )
        lens = _make_lens("arch", doc_path="docs/mandatory_arch.md", size=48_500)
        cat = ReviewCategory("arch", "Architecture", ("check",))
        stage = Stage(
            "review",
            "Categories:\n{review_categories}\nContext:\n{review_context}\nDiff:\n{diff}",
            ("summary", "comments"),
            categories=(cat,),
        )
        provider = _RecordingProvider()
        svc = ReviewService(
            provider,
            stages=(stage,),
            budget=ResourceBudget.create(max_provider_calls=4),
            work_budgets=ReviewWorkBudgets(mode="unified"),
        )
        req = ReviewRequest(
            diff=diff,
            active_category_ids=("arch",),
            lens_contexts=(lens,),
            limits=ReviewLimits(max_summary_bytes=4096),
        )
        run = svc.run(req)
        self.assertEqual(len(provider.requests), 0)
        assert run.result is not None
        summary = run.result.summary
        self.assertLessEqual(len(summary.encode("utf-8")), 4096)
        self.assertIn("mandatory-context-and-evidence-oversized", summary)
        self.assertIn("49152-byte ceiling", summary)
        self.assertIn("overage", summary)
        self.assertIn("payload size, not overage", summary)
        self.assertIn("48500 UTF-8 payload bytes", summary)
        self.assertIn("evidence ", summary)
        self.assertIn("src/large.py hunk #", summary)
        self.assertIn(
            "(+", summary
        )  # bounded truncation (+N more) when 25 hunks overflow

        # Check appendix portion stays within PENDING_APPENDIX_RESERVE
        appendix = summary.split("\n\nDocument context admission sha256:", 1)[1]
        self.assertLessEqual(
            len(appendix.encode("utf-8")), PENDING_APPENDIX_RESERVE + 100
        )

        # Structured context_admission details
        self.assertIsNotNone(run.context_admission)
        assert run.context_admission is not None
        diag = run.context_admission["overflow_diagnostics"]
        self.assertTrue(diag)
        first = diag[0]
        self.assertEqual(first["prompt_ceiling_bytes"], LEGACY_ASSESSMENT_PROMPT_BYTES)
        self.assertGreater(
            first["rendered_prompt_bytes"], LEGACY_ASSESSMENT_PROMPT_BYTES
        )
        self.assertEqual(
            first["over_budget_bytes"],
            first["rendered_prompt_bytes"] - LEGACY_ASSESSMENT_PROMPT_BYTES,
        )
        self.assertEqual(first["mandatory_doc_bytes"], 48_500)
        self.assertTrue(first["clamped_by_legacy_48k"])
        self.assertEqual(first["rejected_paths"], ["src/large.py"])
        self.assertTrue(first["hunk_ranges"])

        # Direct plan_work + classify_work_plan_unprocessed + classify_mandatory_prompt_overflow
        snapshot = EvidenceSnapshot(base_sha="a" * 40, head_sha="b" * 40)
        rec = EvidenceRecord("src/large.py", diff, snapshot)
        bundle = EvidenceBundle(snapshot, (rec,), True)
        work_req = WorkRequirement("file:src/large.py", (rec.evidence_id,), (1,))
        eff = ReviewWorkBudgets(mode="unified").effective(
            limits=ReviewLimits(),
            resource=ResourceBudget.create(),
            mode="discovery",
            capabilities=None,
        )

        def _overflow_render(batch: WorkBatch) -> ProviderRequest:
            raise PromptContextOverflow(
                "overflow",
                rendered_prompt_bytes=52_000,
                prompt_ceiling_bytes=49_152,
                over_budget_bytes=2848,
                mandatory_doc_bytes=48_500,
                mandatory_doc_paths=("docs/mandatory_arch.md",),
                evidence_bytes=len(batch.diff_context.encode("utf-8")),
                framing_bytes=1000,
                clamped_by_legacy_48k=True,
            )

        plan = plan_work(
            "discovery",
            bundle,
            (work_req,),
            budgets=eff,
            render=_overflow_render,
            subdivision_stop_reasons={rec.evidence_id: "single-line-oversized"},
        )
        self.assertEqual(len(plan.unprocessed_diagnostics), 1)
        # Backward-compatible plan_id
        from dataclasses import replace as dc_replace

        stripped_plan = dc_replace(plan, unprocessed_diagnostics=())
        self.assertEqual(plan.plan_id, stripped_plan.plan_id)

        units = classify_work_plan_unprocessed(
            plan,
            max_diff_bytes=128 * 1024,
            max_prompt_bytes=LEGACY_ASSESSMENT_PROMPT_BYTES,
        )
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].unit_bytes, 52_000)
        self.assertEqual(units[0].limit_bytes, 49_152)
        self.assertIn("52000 bytes", units[0].diagnostic)
        self.assertIn("+2848 overage", units[0].diagnostic)
        self.assertIn("-10..11/+10..12", units[0].diagnostic)
        self.assertIn("single-line-oversized", units[0].diagnostic)

        overflow_err = PromptContextOverflow(
            "overflow",
            rendered_prompt_bytes=52_000,
            prompt_ceiling_bytes=49_152,
            over_budget_bytes=2848,
            mandatory_doc_bytes=48_500,
            mandatory_doc_paths=("docs/mandatory_arch.md",),
        )
        report = classify_mandatory_prompt_overflow(
            req,
            active_category_ids=("arch",),
            max_prompt_bytes=LEGACY_ASSESSMENT_PROMPT_BYTES,
            error=overflow_err,
            result=run.result,
        )
        self.assertIn("rendered 52000 bytes", report.units[0].diagnostic)
        self.assertIn("+2848 overage", report.units[0].diagnostic)
        self.assertIn("49152-byte rendered prompt ceiling", report.units[0].diagnostic)


if __name__ == "__main__":
    unittest.main()
