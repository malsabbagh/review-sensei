from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from review_sensei.budgets import ProviderCapabilities, ReviewWorkBudgets
from review_sensei.context import (
    IncrementalReviewPlan,
    PromptContextOverflow,
    ReviewContextCache,
    admit_prompt_context,
    build_review_context_cache_key,
    finding_lifecycle_for_comment,
)
from review_sensei.convergence import derive_blocker_candidate
from review_sensei.discovery_work import CONTEXT_APPENDIX_RESERVE
from review_sensei.document_context import DocumentDecision, DocumentSelectionReport
from review_sensei.errors import ReviewInputError
from review_sensei.hosting.github import ReviewPublisher
from review_sensei.hosting.github.approval import (
    ReviewApprovalEligibility,
    evaluate_approval_facts,
)
from review_sensei.hosting.github.publication import (
    ReviewApprovalFinalizer,
    approval_eligibility_from_body,
)
from review_sensei.human_assessment import (
    HumanAssessmentDecision,
    validate_assessment_evidence,
)
from review_sensei.models import (
    ProviderResponse,
    ReviewComment,
    ReviewDocument,
    ReviewLensContext,
    ReviewRequest,
    ReviewResult,
)
from review_sensei.outcomes import ResourceBudget, ResourceBudgetTracker
from review_sensei.service import _PROVIDER_OUTPUT_CORRECTION, ReviewService
from review_sensei.session import (
    LocalSessionLedger,
    checkpoint_review_analysis,
    complete_review_publication,
    load_review_transaction_for_publication,
    prepare_review_transaction,
    session_reservation_id,
)
from review_sensei.stages import ReviewCategory, Stage
from review_sensei.validation import ReviewLimits
from review_sensei.work_recovery import WorkRecoveryStore
from tests.fake_github_http import json_response, make_http
from tests.test_github_publication import (
    DIFF,
    graphql_review_threads_response,
    posted_events,
    pr_payload,
    review_payloads,
)
from tests.test_review_transaction import (
    CONFIGURATION_DIGEST,
    EVIDENCE_DIGEST,
    IDENTITY,
    NOW,
    POLICY,
    _checkpoint_with_baseline,
)


def lens(category="documents", *, size=7000, count=64, omitted=9, pinned=False):
    documents = tuple(
        ReviewDocument(
            f"docs/{i:03}.md",
            ('€"\\\n' + "z" * size),
            hashlib.sha256(('€"\\\n' + "z" * size).encode()).hexdigest(),
        )
        for i in range(count)
    )
    decisions = tuple(
        DocumentDecision(
            doc.path,
            "selected",
            "pinned" if pinned else "configured-source",
            doc.sha256,
            doc.sha256,
            len(doc.content.encode()),
        )
        for doc in documents
    )
    decisions += tuple(
        DocumentDecision(f"docs/{i:03}.md", "omitted", "file-budget")
        for i in range(count, count + omitted)
    )
    return ReviewLensContext(
        category,
        documents=documents,
        document_selection=DocumentSelectionReport(decisions),
    )


class Provider:
    name = "fixture"
    model = "fixture-model"
    endpoint = "https://fixture.example/api"
    completion_contract = "bounded-complete-text-v1"

    def __init__(self, summary="Reviewed current diff.", finding=False):
        self.calls = []
        self.summary = summary
        self.finding = finding

    def complete(self, request):
        self.calls.append(request)
        comments = (
            [
                {
                    "path": "src/app.py",
                    "line": 2,
                    "body": "Verify the changed input.",
                    "blocking": False,
                    "requires_human": True,
                }
            ]
            if self.finding
            else []
        )
        return ProviderResponse(
            json.dumps({"summary": self.summary, "comments": comments}),
            self.name,
            self.model,
        )


def service(provider, *, limits=None):
    category = ReviewCategory("documents", "Document requirements", ("correctness",))
    stage = Stage(
        name="review",
        prompt_template="Review {diff}\n{review_context}\n{review_categories}",
        outputs=("summary", "comments"),
        categories=(category,),
    )
    return ReviewService(
        provider,
        stages=(stage,),
        budget=ResourceBudget.create(max_provider_calls=8),
        work_budgets=ReviewWorkBudgets(mode="unified"),
    )


def request(context=None, **kwargs):
    return ReviewRequest(
        diff=DIFF,
        repository="owner/repo",
        pull_request_number=146,
        base_sha="a" * 40,
        head_sha="b" * 40,
        active_category_ids=("documents",),
        lens_contexts=(context or lens(),),
        **kwargs,
    )


class PromptContextAdmissionTests(unittest.TestCase):
    def test_exact_rendered_utf8_boundary_and_original_report_identity(self):
        original = request(lens(size=1, count=2, omitted=0))

        def render(value):
            return (
                json.dumps(
                    [context.to_prompt_dict() for context in value.lens_contexts],
                    ensure_ascii=False,
                )
                + "\nframe"
            )

        size = len(render(original).encode())
        for bound in (size - 1, size, size + 1):
            with self.subTest(bound=bound):
                selected = admit_prompt_context(
                    original,
                    active_category_ids=("documents",),
                    changed_paths=("src/app.py",),
                    render=render,
                    fits=lambda prompt: len(prompt.encode()) <= bound,
                )
                self.assertEqual(selected.prompt_omitted, int(bound < size))
                self.assertLessEqual(len(selected.prompt.encode()), bound)
                self.assertEqual(
                    selected.original_reports[0][1],
                    original.lens_contexts[0].document_selection.to_prompt_dict()[
                        "inventory_sha256"
                    ],
                )
                self.assertEqual(selected.complete, bound >= size)
                self.assertEqual(
                    original.lens_contexts[0].documents,
                    original.lens_contexts[0].documents,
                )

    def test_shared_pinned_document_cannot_be_removed_in_any_lens(self):
        optional = lens(size=100, count=1, omitted=0)
        mandatory = replace(
            optional,
            category_id="mandatory",
            document_selection=DocumentSelectionReport(
                tuple(
                    replace(item, reason="pinned")
                    for item in optional.document_selection.decisions
                )
            ),
        )
        original = replace(
            request(optional),
            active_category_ids=("documents", "mandatory"),
            lens_contexts=(optional, mandatory),
        )
        with self.assertRaises(PromptContextOverflow):
            admit_prompt_context(
                original,
                active_category_ids=original.active_category_ids,
                changed_paths=("src/app.py",),
                render=lambda value: json.dumps(
                    [context.to_prompt_dict() for context in value.lens_contexts]
                ),
                fits=lambda prompt: len(prompt.encode()) < 100,
            )

    def test_unreported_context_is_mandatory(self):
        context = replace(lens(size=100, count=1, omitted=0), document_selection=None)
        with self.assertRaises(PromptContextOverflow):
            admit_prompt_context(
                request(context),
                active_category_ids=("documents",),
                changed_paths=(),
                render=lambda value: value.lens_contexts[0].documents[0].content,
                fits=lambda prompt: len(prompt.encode()) < 100,
            )

    def test_pr231_shaped_inventory_executes_and_keeps_partial_diagnostics(self):
        provider = Provider()
        original = request()
        run = service(provider).run(original)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(run.result.review_status, "partial")
        self.assertTrue(run.result.coverage.fully_reviewed)
        prompt = provider.calls[0].prompt
        self.assertLessEqual(
            len((prompt + "\n\n" + _PROVIDER_OUTPUT_CORRECTION).encode()), 49152
        )
        self.assertEqual(
            provider.calls[0].max_response_bytes,
            request().limits.max_provider_response_bytes - CONTEXT_APPENDIX_RESERVE,
        )
        diagnostics = json.loads(
            run.result.summary.split("executed batches only):\n")[1]
        )
        self.assertEqual(len(diagnostics["batches"]), 1)  # no singleton/packing probes
        inventory = diagnostics["original_inventories"][0]
        self.assertEqual(
            (inventory["discovered"], inventory["selected"], inventory["omitted"]),
            (73, 64, 9),
        )
        self.assertEqual(
            inventory["inventory_sha256"],
            original.lens_contexts[0].document_selection.to_prompt_dict()[
                "inventory_sha256"
            ],
        )
        self.assertGreater(diagnostics["batches"][0]["prompt_omitted"], 0)
        self.assertNotIn("zzzz", run.result.summary)
        self.assertNotEqual(
            diagnostics["batches"][0]["executed_selection_sha256"],
            inventory["inventory_sha256"],
        )

    def test_mandatory_only_overflow_has_zero_calls_and_precise_pending(self):
        provider = Provider()
        run = service(provider).run(
            request(lens(size=70000, count=1, omitted=0, pinned=True))
        )
        self.assertFalse(provider.calls)
        self.assertEqual(run.result.review_status, "partial")
        self.assertFalse(run.result.coverage.fully_reviewed)
        self.assertIn("mandatory-context-and-evidence-oversized", run.result.summary)
        self.assertNotIn("executed batches only", run.result.summary)

    def test_summary_reserve_boundary_refuses_without_clipping_provider_prose(self):
        for bound in (CONTEXT_APPENDIX_RESERVE - 1, CONTEXT_APPENDIX_RESERVE):
            provider = Provider()
            run = service(provider).run(
                request(
                    lens(size=10, count=1, omitted=0),
                    limits=ReviewLimits(max_summary_bytes=bound),
                )
            )
            self.assertFalse(provider.calls)
            self.assertIn("host-context-output-oversized", run.result.summary)

    def test_selection_is_deterministic_and_binds_source_change(self):
        runs = []
        for size in (7000, 7000, 7001):
            provider = Provider()
            runs.append(service(provider).run(request(lens(size=size))).result)
        self.assertEqual(runs[0].summary, runs[1].summary)
        self.assertNotEqual(runs[0].content_digest(), runs[2].content_digest())

    def test_partial_does_not_close_prior_findings_or_overwrite_cache(self):
        provider = Provider()
        reviewer = service(provider)
        cache = ReviewContextCache()
        reviewer.cache = cache
        original = replace(request(), work_policy_digest=reviewer.work_policy_digest)
        key = build_review_context_cache_key(
            original,
            provider_name=provider.name,
            stages=reviewer.stages,
            profile="default",
        )
        previous_key = replace(key, head_sha="c" * 40)
        cache.put(previous_key, (1, "previous-baseline"))
        prior = finding_lifecycle_for_comment(
            ReviewComment("src/app.py", 2, "Prior concern."), generation=1
        )
        incremental = IncrementalReviewPlan(
            previous_key, (prior,), reviewed_paths=(), generation=1
        )
        run = reviewer.run(original, incremental=incremental, current_key=key)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(run.result.coverage_mode, "fallback-full")
        self.assertEqual(run.result.review_status, "partial")
        self.assertEqual(cache.get(previous_key), (1, "previous-baseline"))
        self.assertIsNone(cache.get(key))
        self.assertEqual(run.result.finding_lifecycles[0].state, "uncertain")

    def test_partial_checkpoint_preserves_old_completed_baseline(self):
        result = service(Provider()).run(request()).result
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory))
            initial = _checkpoint_with_baseline(ledger)
            complete_review_publication(
                ledger, IDENTITY, initial.transaction, published=True, now=NOW
            )
            old = ledger.load(IDENTITY, now=NOW).record.convergence_history["baseline"]
            prepared = prepare_review_transaction(
                ledger,
                IDENTITY,
                POLICY,
                reservation_id="f" * 64,
                base_sha="a" * 40,
                head_sha="b" * 40,
                configuration_digest=CONFIGURATION_DIGEST,
                evidence_digest=EVIDENCE_DIGEST,
                now=NOW,
            )
            checkpoint_review_analysis(ledger, IDENTITY, prepared, result, now=NOW)
            restored = (
                LocalSessionLedger(Path(directory)).load(IDENTITY, now=NOW).record
            )
            self.assertEqual(restored.convergence_history["baseline"], old)

    def test_recovery_revalidates_context_and_retains_original_elapsed_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            first_provider = Provider()
            first = service(first_provider).run(
                request(),
                work_recovery=WorkRecoveryStore(
                    Path(directory),
                    key=b"k" * 32,
                    artifacts="diagnostics",
                    now=lambda: NOW,
                ),
            )
            self.assertEqual(len(first_provider.calls), 1)
            second_provider = Provider()
            second = service(second_provider).run(
                request(),
                work_recovery=WorkRecoveryStore(
                    Path(directory),
                    key=b"k" * 32,
                    artifacts="diagnostics",
                    now=lambda: NOW + timedelta(seconds=5),
                ),
            )
            self.assertFalse(second_provider.calls)
            self.assertEqual(second.outcome.provider_calls, 1)
            self.assertGreaterEqual(second.outcome.elapsed_ms, 5000)
            self.assertEqual(
                first.result.content_digest(), second.result.content_digest()
            )
            changed_provider = Provider()
            service(changed_provider).run(
                request(lens(size=7001)),
                work_recovery=WorkRecoveryStore(
                    Path(directory),
                    key=b"k" * 32,
                    artifacts="diagnostics",
                    now=lambda: NOW,
                ),
            )
            self.assertEqual(len(changed_provider.calls), 1)
            policy_provider = Provider()
            with patch(
                "review_sensei.discovery_work.PROMPT_CONTEXT_POLICY",
                "fixture-policy-v2",
            ):
                service(policy_provider).run(
                    request(),
                    work_recovery=WorkRecoveryStore(
                        Path(directory),
                        key=b"k" * 32,
                        artifacts="diagnostics",
                        now=lambda: NOW,
                    ),
                )
            self.assertEqual(len(policy_provider.calls), 1)

    def test_expired_original_tracker_refuses_with_precise_deadline_without_dispatch(
        self,
    ):
        provider = Provider()
        tracker = ResourceBudgetTracker(
            ResourceBudget.create(timeout_ms=1000), monotonic=lambda: 2.0
        )
        tracker.started = 0.0
        run = service(provider).run(request(), tracker=tracker)
        self.assertFalse(provider.calls)
        self.assertIn("planning-deadline-exceeded", run.result.summary)
        self.assertNotIn("oversized", run.result.summary)

    def test_qualified_profile_uses_configured_output_token_reservation(self):
        provider = Provider()
        provider.max_output_tokens = 1024
        reviewer = service(provider)
        reviewer.capabilities = ProviderCapabilities(
            context_tokens=24000,
            max_output_tokens=16384,
            qualified=True,
            provider_name=provider.name,
            model=provider.model,
            endpoint=provider.endpoint,
            completion_contract=provider.completion_contract,
        )
        run = reviewer.run(request(lens(size=10000, count=1, omitted=0, pinned=True)))
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(provider.calls[0].max_output_tokens, 1024)
        self.assertEqual(run.result.review_status, "complete")

    def test_pending_framing_keeps_accepted_summary_and_refuses_overflow_without_clipping(
        self,
    ):
        # One valid batch, then a genuinely oversized atomic hunk.
        diff = (
            DIFF
            + "diff --git a/src/big.py b/src/big.py\n--- a/src/big.py\n+++ b/src/big.py\n@@ -1 +1 @@\n-old\n+"
            + "x" * 60000
            + "\n"
        )
        for summary_size in (29000, 32768):
            provider = Provider(summary="s" * summary_size, finding=True)
            run = service(provider).run(
                replace(request(lens(size=10, count=1, omitted=0)), diff=diff)
            )
            self.assertIsNotNone(run.result)
            self.assertEqual(run.result.review_status, "partial")
            self.assertLessEqual(len(run.result.summary.encode()), 32768)
            self.assertIn("Required work pending:", run.result.summary)
            if summary_size == 29000:
                self.assertIn("s" * summary_size, run.result.summary)
                self.assertTrue(run.result.comments)
            else:
                self.assertNotIn("ssss", run.result.summary)
                self.assertIn("output_budget", run.result.summary)

    def test_positive_partial_checkpoint_restart_publication_readback_human_noapprove(
        self,
    ):
        provider = Provider(finding=True)
        result = service(provider).run(request()).result
        self.assertTrue(result.comments)
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory))
            prepared = prepare_review_transaction(
                ledger,
                IDENTITY,
                POLICY,
                reservation_id=session_reservation_id(
                    repository=IDENTITY.repository,
                    pull_request=IDENTITY.pull_request,
                    head_sha="b" * 40,
                    kind="publish",
                ),
                base_sha="a" * 40,
                head_sha="b" * 40,
                configuration_digest=CONFIGURATION_DIGEST,
                evidence_digest=EVIDENCE_DIGEST,
                now=NOW,
            )
            checkpoint = checkpoint_review_analysis(
                ledger, IDENTITY, prepared, result, now=NOW
            )
            restored = ReviewResult.from_dict(checkpoint.to_dict())
            record = load_review_transaction_for_publication(
                LocalSessionLedger(Path(directory)),
                IDENTITY,
                restored,
                base_sha="a" * 40,
                head_sha="b" * 40,
                policy_digest=POLICY.digest(),
                configuration_digest=CONFIGURATION_DIGEST,
                evidence_digest=EVIDENCE_DIGEST,
                now=NOW,
            )
            self.assertIsNotNone(record.transaction)
            for changed in ("f" * 64,):
                with self.assertRaises(ReviewInputError):
                    load_review_transaction_for_publication(
                        ledger,
                        IDENTITY,
                        restored,
                        base_sha="a" * 40,
                        head_sha="b" * 40,
                        policy_digest=POLICY.digest(),
                        configuration_digest=changed,
                        evidence_digest=EVIDENCE_DIGEST,
                        now=NOW,
                    )
        # Publisher validates the analysis content; transaction persistence was tested above.
        publishable = replace(restored, transaction=None)
        head = "b" * 40
        http, calls = make_http(
            [
                json_response(pr_payload(head_sha=head)),
                json_response([]),
                json_response(pr_payload(head_sha=head)),
                json_response({"id": 5}),
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(),
                json_response(pr_payload(head_sha=head)),
                json_response([]),
                json_response({"id": 6}),
            ]
        )
        outcome = ReviewPublisher(http=http).publish(
            token="token",
            repository="owner/repo",
            repository_id=1,
            pull_request=2,
            head_sha=head,
            base_branch="main",
            base_sha="a" * 40,
            result=publishable,
            diff=DIFF,
            app_slug="reviewsensei[bot]",
            auto_approve=True,
            convergence_policy=POLICY,
            blocker_candidates=tuple(
                replace(
                    derive_blocker_candidate(comment, on_changed_path=True),
                    needs_human=True,
                )
                for comment in publishable.comments
            ),
        )
        self.assertEqual(outcome.status, "published")
        self.assertEqual(posted_events(calls), ["COMMENT"])
        eligibility = approval_eligibility_from_body(review_payloads(calls)[0]["body"])
        self.assertIsNotNone(eligibility)
        pending = eligibility.human_review
        self.assertTrue(pending.pending)
        decisions = tuple(
            HumanAssessmentDecision(
                item.fingerprint,
                "dismissed",
                "Current cited input is permitted.",
                "The current input is permitted.",
                "@@ -1 +1,2 @@\n keep\n+change",
            )
            for item in pending.pending
        )
        for item, assessment in zip(pending.pending, decisions):
            validate_assessment_evidence(
                assessment,
                item,
                source_body="The current input is permitted.",
                diff_context="path=src/app.py\n" + DIFF,
            )
        resolved = replace(
            eligibility,
            human_review=pending.apply(decisions),
            facts=replace(eligibility.facts, has_human_adjudication_findings=False),
        )
        durable = ReviewApprovalEligibility.from_dict(resolved.to_dict())
        self.assertFalse(durable.human_review.pending)
        decision = evaluate_approval_facts(
            replace(durable.facts, has_open_review_threads=False)
        )
        self.assertFalse(decision.approved)
        self.assertIn("review-partial", decision.blockers)
        final_http, final_calls = make_http([])
        final = ReviewApprovalFinalizer(http=final_http).finalize(
            token="token",
            repository="owner/repo",
            pull_request=2,
            head_sha=head,
            app_slug="reviewsensei[bot]",
            eligibility=durable,
        )
        self.assertEqual(final.status, "approval_withheld")
        self.assertFalse(final_calls)
