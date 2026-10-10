"""PR253 D1: verbatim architecture rules stay inside the unqualified prompt cap."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path

from review_sensei.budgets import LEGACY_ASSESSMENT_PROMPT_BYTES, ReviewWorkBudgets
from review_sensei.context import (
    PromptContextOverflow,
    RepositoryContextStore,
    admit_prompt_context,
)
from review_sensei.diff import analyze_diff
from review_sensei.discovery_work import CONTEXT_APPENDIX_RESERVE
from review_sensei.document_context import DocumentDecision, DocumentSelectionReport
from review_sensei.errors import ContextLoadError, ReviewInputError
from review_sensei.models import (
    ProviderResponse,
    ReviewDocument,
    ReviewLensContext,
    ReviewRequest,
)
from review_sensei.outcomes import ResourceBudget
from review_sensei.scope import CONTEXT_REQUEST_INSTRUCTION
from review_sensei.service import (
    _PROVIDER_OUTPUT_CORRECTION,
    DEFAULT_STAGES,
    ReviewService,
)
from review_sensei.stages import ContextDocumentSource, ReviewCategory, Stage

ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = ROOT / "docs/architecture/mandatory-rules.md"
GUIDE_PATH = ROOT / "docs/architecture.md"
RULES_DOCUMENT = "docs/architecture/mandatory-rules.md"
GUIDE_DOCUMENT = "docs/architecture.md"
COMPLETE_DIFF = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1,2 @@
 keep
+change
"""
NORMATIVE = re.compile(
    r"\b(MUST(?: NOT)?|must not|must|never|only|refus(?:e|es|ed|al)|retain(?:s|ed)?)\b",
    re.I,
)
LEDGER = re.compile(
    r"(?s)<!--.*?-->|```supplemental-ledger.*?```",
)


class _Provider:
    name = "fixture"
    model = "fixture-model"
    endpoint = "https://fixture.example/api"
    completion_contract = "bounded-complete-text-v1"

    def __init__(self) -> None:
        self.calls = []

    def complete(self, request):
        self.calls.append(request)
        return ProviderResponse(
            json.dumps({"summary": "Reviewed the current diff.", "comments": []}),
            self.name,
            self.model,
        )


def _document(path: str, content: str) -> ReviewDocument:
    return ReviewDocument(
        path,
        content,
        hashlib.sha256(content.encode("utf-8")).hexdigest(),
    )


def _decision(document: ReviewDocument, *, reason: str) -> DocumentDecision:
    size = len(document.content.encode("utf-8"))
    return DocumentDecision(
        document.path,
        "selected",
        reason,
        document.sha256,
        document.sha256,
        size,
    )


def _request(*documents: tuple[ReviewDocument, str]) -> ReviewRequest:
    ordered = tuple(sorted(documents, key=lambda item: item[0].path))
    lens = ReviewLensContext(
        "architecture",
        documents=tuple(document for document, _reason in ordered),
        document_selection=DocumentSelectionReport(
            tuple(_decision(document, reason=reason) for document, reason in ordered)
        ),
    )
    return ReviewRequest(
        diff=COMPLETE_DIFF,
        repository="owner/repo",
        pull_request_number=253,
        base_sha="a" * 40,
        head_sha="b" * 40,
        active_category_ids=("architecture",),
        lens_contexts=(lens,),
    )


def _service(provider: _Provider) -> ReviewService:
    category = ReviewCategory.from_dict(
        json.loads(
            (
                ROOT / "src/review_sensei/default_categories/03-architecture.json"
            ).read_text(encoding="utf-8")
        )
    )
    stage = Stage(
        name="review",
        prompt_template=DEFAULT_STAGES[0].prompt_template,
        outputs=("summary", "comments"),
        categories=(category,),
    )
    return ReviewService(
        provider,
        stages=(stage,),
        budget=ResourceBudget.create(max_provider_calls=4),
        work_budgets=ReviewWorkBudgets(mode="unified"),
    )


def _suffix(request: ReviewRequest) -> str:
    reserve = CONTEXT_APPENDIX_RESERVE
    return (
        "\n"
        + CONTEXT_REQUEST_INSTRUCTION
        + (
            f"\nHost reserves {reserve} UTF-8 summary bytes for admission diagnostics. "
            f"Provider summary must fit {request.limits.max_summary_bytes - reserve} "
            "UTF-8 bytes."
        )
    )


def _rendered_prompt(service: ReviewService, request: ReviewRequest) -> str:
    budgets = service.work_budgets.effective(
        limits=request.limits,
        resource=service.budget,
        mode="discovery",
        capabilities=None,
        output_tokens=None,
    )
    suffix = _suffix(request)
    frame_bytes = len(suffix.encode("utf-8")) + len(
        ("\n\n" + _PROVIDER_OUTPUT_CORRECTION).encode("utf-8")
    )
    category = service.stages[0].categories[0]
    return (
        service._format_prompt(
            service.stages[0],
            request,
            active_categories=(category,),
            coverage_mode="full",
            reviewed_paths=("src/app.py",),
            related_paths=(),
            max_prompt_bytes=max(0, budgets.batch_prompt_bytes - frame_bytes),
            analysis=analyze_diff(request.diff, limits=request.limits),
        )
        + suffix
    )


def _complete_frame(service: ReviewService, request: ReviewRequest) -> str:
    return _rendered_prompt(service, request) + "\n\n" + _PROVIDER_OUTPUT_CORRECTION


def _admit(service: ReviewService, request: ReviewRequest):
    budgets = service.work_budgets.effective(
        limits=request.limits,
        resource=service.budget,
        mode="discovery",
        capabilities=None,
        output_tokens=None,
    )
    return admit_prompt_context(
        request,
        active_category_ids=("architecture",),
        changed_paths=("src/app.py",),
        render=lambda selected: _rendered_prompt(service, selected),
        fits=lambda prompt: budgets.fits_prompt(
            prompt + "\n\n" + _PROVIDER_OUTPUT_CORRECTION
        ),
    )


def _normative_body(text: str) -> list[str]:
    stripped = LEDGER.sub("", text)
    return [line for line in stripped.splitlines() if line.strip()]


class Pr253ContextPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rules = RULES_PATH.read_text(encoding="utf-8")
        cls.guide = GUIDE_PATH.read_text(encoding="utf-8")
        cls.service = _service(_Provider())

    def test_mandatory_rules_and_small_complete_evidence_admit(self) -> None:
        analysis = analyze_diff(COMPLETE_DIFF)
        self.assertTrue(analysis.enumeration_complete)
        rules = _document(RULES_DOCUMENT, self.rules)
        request = _request((rules, "pinned"))
        frame = _complete_frame(self.service, request)
        self.assertLessEqual(len(frame.encode("utf-8")), LEGACY_ASSESSMENT_PROMPT_BYTES)
        self.assertEqual(LEGACY_ASSESSMENT_PROMPT_BYTES, 49152)

        admitted = _admit(self.service, request)
        admitted_frame = (
            admitted.prompt + "\n\n" + _PROVIDER_OUTPUT_CORRECTION
        ).encode("utf-8")
        self.assertLessEqual(len(admitted_frame), LEGACY_ASSESSMENT_PROMPT_BYTES)
        self.assertEqual(admitted.request.lens_contexts[0].documents, (rules,))
        self.assertIn(json.dumps(self.rules, ensure_ascii=False)[1:-1], admitted.prompt)
        self.assertIn(COMPLETE_DIFF.strip(), admitted.prompt)
        self.assertTrue(admitted.complete)

        provider = _Provider()
        run = _service(provider).run(request)
        self.assertEqual(len(provider.calls), 1)
        sent = (provider.calls[0].prompt + "\n\n" + _PROVIDER_OUTPUT_CORRECTION).encode(
            "utf-8"
        )
        self.assertLessEqual(len(sent), LEGACY_ASSESSMENT_PROMPT_BYTES)
        self.assertEqual(run.result.review_status, "complete")

    def test_exact_ceiling_admits_and_one_extra_byte_makes_zero_provider_calls(
        self,
    ) -> None:
        def size(count: int) -> int:
            content = "rule " + ("a" * count)
            return len(
                _complete_frame(
                    self.service,
                    _request((_document(RULES_DOCUMENT, content), "pinned")),
                ).encode("utf-8")
            )

        low = 1
        high = 80_000
        self.assertLess(size(low), LEGACY_ASSESSMENT_PROMPT_BYTES)
        self.assertGreater(size(high), LEGACY_ASSESSMENT_PROMPT_BYTES)
        while low < high:
            mid = (low + high) // 2
            if size(mid) < LEGACY_ASSESSMENT_PROMPT_BYTES:
                low = mid + 1
            else:
                high = mid
        if size(low) != LEGACY_ASSESSMENT_PROMPT_BYTES:
            low -= 1
        self.assertEqual(size(low), LEGACY_ASSESSMENT_PROMPT_BYTES)
        self.assertEqual(size(low + 1), LEGACY_ASSESSMENT_PROMPT_BYTES + 1)

        fitting = _request((_document(RULES_DOCUMENT, "rule " + ("a" * low)), "pinned"))
        admitted = _admit(self.service, fitting)
        self.assertEqual(
            len((admitted.prompt + "\n\n" + _PROVIDER_OUTPUT_CORRECTION).encode()),
            LEGACY_ASSESSMENT_PROMPT_BYTES,
        )

        oversized = _request(
            (_document(RULES_DOCUMENT, "rule " + ("a" * (low + 1))), "pinned")
        )
        with self.assertRaises(PromptContextOverflow):
            _admit(self.service, oversized)
        provider = _Provider()
        run = _service(provider).run(oversized)
        self.assertFalse(provider.calls)
        self.assertEqual(run.result.review_status, "partial")
        self.assertIn("mandatory-context-and-evidence-oversized", run.result.summary)

    def test_supplemental_guide_can_be_omitted_without_dropping_required_rules(
        self,
    ) -> None:
        rules = _document(RULES_DOCUMENT, self.rules)
        guide = _document(GUIDE_DOCUMENT, self.guide)
        request = _request((rules, "pinned"), (guide, "configured-source"))
        self.assertGreater(
            len(_complete_frame(self.service, request).encode("utf-8")),
            LEGACY_ASSESSMENT_PROMPT_BYTES,
        )
        admitted = _admit(self.service, request)
        self.assertEqual(
            tuple(
                document.path
                for document in admitted.request.lens_contexts[0].documents
            ),
            (RULES_DOCUMENT,),
        )
        self.assertEqual(
            admitted.request.lens_contexts[0].documents[0].content, self.rules
        )
        self.assertGreater(admitted.prompt_omitted, 0)
        self.assertFalse(admitted.complete)

    def test_omitted_or_altered_mandatory_document_is_not_silent_success(self) -> None:
        source = ContextDocumentSource(path=RULES_DOCUMENT, required=True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = RepositoryContextStore(root)
            with self.assertRaises(ContextLoadError):
                store.for_sources((source,))
            target = root / RULES_DOCUMENT
            target.parent.mkdir(parents=True)
            target.write_text(" \n\t", encoding="utf-8")
            with self.assertRaises(ContextLoadError):
                store.for_sources((source,))

        altered = self.rules.replace("never", "sometimes", 1)
        self.assertNotEqual(altered, self.rules)
        with self.assertRaises(ReviewInputError):
            ReviewDocument(
                RULES_DOCUMENT,
                altered,
                hashlib.sha256(self.rules.encode("utf-8")).hexdigest(),
            )
        with self.assertRaises(ReviewInputError):
            DocumentDecision(
                RULES_DOCUMENT,
                "summarized",
                "pinned",
                "a" * 64,
                "b" * 64,
                10,
                ((1, 1),),
            )
        with self.assertRaises(ReviewInputError):
            DocumentDecision(RULES_DOCUMENT, "omitted", "pinned")

    def test_packaged_architecture_category_does_not_require_the_long_guide(
        self,
    ) -> None:
        paths = (
            ROOT / "src/review_sensei/default_categories/03-architecture.json",
            ROOT / "examples/categories/03-architecture.json",
        )
        for path in paths:
            category = ReviewCategory.from_dict(
                json.loads(path.read_text(encoding="utf-8"))
            )
            with self.subTest(path=path.name):
                required = [
                    source.path
                    for source in category.document_sources
                    if source.required
                ]
                self.assertEqual(required, [RULES_DOCUMENT])
                self.assertFalse(
                    any(
                        source.path == GUIDE_DOCUMENT and source.required
                        for source in category.document_sources
                    )
                )
                self.assertTrue(
                    any(
                        source.path == "docs"
                        and "architecture.md" in source.include
                        and not source.required
                        for source in category.document_sources
                    )
                )
                self.assertTrue(
                    any(
                        source.path == "docs/architecture" and not source.required
                        for source in category.document_sources
                    )
                )
                self.assertTrue(
                    any(
                        source.path == "docs/adr" and not source.required
                        for source in category.document_sources
                    )
                )

        discovered = RepositoryContextStore(ROOT).discover_sources(
            ReviewCategory.from_dict(
                json.loads(paths[0].read_text(encoding="utf-8"))
            ).document_sources
        )
        self.assertTrue(discovered[RULES_DOCUMENT][1])
        self.assertIn(GUIDE_DOCUMENT, discovered)
        self.assertFalse(discovered[GUIDE_DOCUMENT][1])
        self.assertFalse(discovered["docs/architecture/index.md"][1])
        self.assertTrue(
            any(
                path.startswith("docs/adr/") and not pinned
                for path, (_resolved, pinned) in discovered.items()
            )
        )

    def test_extracted_normative_lines_remain_verbatim_in_the_guide(self) -> None:
        body = _normative_body(self.rules)
        self.assertTrue(body)
        guide_lines = set(self.guide.splitlines())
        for line in body:
            self.assertIn(line, guide_lines)
        extracted = set(body)
        missing = [
            (index + 1, line)
            for index, line in enumerate(self.guide.splitlines())
            if NORMATIVE.search(line) and line not in extracted
        ]
        self.assertEqual(missing, [])
        self.assertIn("selected-design", self.rules)
        self.assertNotIn("## Selected design", extracted)
