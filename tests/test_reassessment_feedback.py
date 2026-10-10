"""Owned C handoff tests; full models/codecs are frozen D/B dependencies."""

from __future__ import annotations

import importlib.util
import json
import unittest
from dataclasses import replace

from review_sensei.budgets import ReviewWorkBudgets
from review_sensei.errors import ReviewInputError
from review_sensei.models import ProviderResponse
from review_sensei.reassessment_work import reassess, validate_work
from tests.test_assessment_queue import DocumentStore, fixture
from tests.test_review_work import HUMAN, AssessingProvider

HAS_FEEDBACK = importlib.util.find_spec("review_sensei.feedback") is not None
if HAS_FEEDBACK:
    from review_sensei.feedback import (
        FeedbackReference,
        FeedbackSelection,
        FeedbackSource,
    )


@unittest.skipUnless(HAS_FEEDBACK, "requires frozen D feedback-v1 and B d855365 codecs")
class ReassessmentFeedbackTests(unittest.TestCase):
    def selection(self, bundle, *, target_ids=(), bodies=None):
        bodies = bodies or ("@sensei selected source " + '界\\"' * 1800 + HUMAN,)
        references = tuple(
            FeedbackReference("issue", index + 10, "2026-10-10T01:00:00Z")
            for index in range(len(bodies))
        )
        return FeedbackSelection(
            repository=bundle.snapshot.repository,
            pull_request=bundle.snapshot.pull_request,
            base_sha=bundle.snapshot.base_sha,
            head_sha=bundle.snapshot.head_sha,
            trigger=references[0],
            target_ids=target_ids,
            sources=tuple(
                FeedbackSource(ref, "alice", 7, "MEMBER", body)
                for ref, body in zip(references, bodies, strict=True)
            ),
        )

    def run_work(
        self, pending, bundle, queue, feedback, *, store=None, provider=None, **kwargs
    ):
        store = store or DocumentStore()
        provider = provider or AssessingProvider()
        work = reassess(
            pending=pending,
            bundle=bundle,
            queue=queue,
            feedback=feedback,
            source_body="unused legacy body" * 1000,
            authority_digest="f" * 64,
            provider=provider,
            checkpoint=store.checkpoint(),
            work_budgets=kwargs.pop("work_budgets", ReviewWorkBudgets(mode="unified")),
            **kwargs,
        )
        return work, store, provider

    def test_complete_body_over_legacy_limit_and_tail_citation_reach_late_target(self):
        pending, bundle, queue = fixture(50)
        feedback = self.selection(bundle, target_ids=(queue.pending_order[-1],))
        work, store, provider = self.run_work(pending, bundle, queue, feedback)
        self.assertGreater(feedback.total_bytes, 4096)
        self.assertEqual(len(provider.calls), 1)
        prompt = provider.calls[0].prompt
        reference = json.loads(prompt[prompt.index('{"feedback":') :])
        self.assertEqual(reference["feedback"], feedback.to_dict())
        self.assertNotIn("human_reply", reference["assessment_context"])
        self.assertEqual(
            tuple(item.fingerprint for item in work.reply.decisions),
            feedback.target_ids,
        )
        self.assertEqual(work.feedback_digest, feedback.digest)
        self.assertEqual(len(store.writes), 3)
        validate_work(
            work,
            pending=pending,
            bundle=bundle,
            source_body="not citation evidence",
            authority_digest="f" * 64,
            feedback=feedback,
        )

    def test_replay_uses_complete_original_selected_bodies_without_dispatch(self):
        pending, bundle, queue = fixture(4)
        feedback = self.selection(bundle)
        first, store, _ = self.run_work(pending, bundle, queue, feedback)
        latest = first.apply_to(pending)
        provider = AssessingProvider()
        replay, _, _ = self.run_work(
            latest, bundle, first.queue, feedback, store=store, provider=provider
        )
        self.assertEqual(provider.calls, [])
        self.assertEqual(replay.reply.decisions, first.reply.decisions)
        self.assertEqual(store.document["counters"]["provider_calls"], 1)

    def test_changed_selected_body_refuses_cached_receipt_without_new_allowance(self):
        pending, bundle, queue = fixture(4)
        feedback = self.selection(bundle)
        first, store, _ = self.run_work(pending, bundle, queue, feedback)
        changed = replace(
            feedback,
            sources=(
                replace(feedback.sources[0], body=feedback.sources[0].body + "edited"),
            ),
        )
        provider = AssessingProvider()
        count = len(store.writes)
        with self.assertRaises(ReviewInputError):
            self.run_work(
                pending, bundle, queue, changed, store=store, provider=provider
            )
        self.assertEqual(provider.calls, [])
        self.assertEqual(len(store.writes), count)
        with self.assertRaisesRegex(
            ReviewInputError, "feedback receipt authority changed"
        ):
            validate_work(
                first,
                pending=pending,
                bundle=bundle,
                source_body=HUMAN,
                authority_digest="f" * 64,
                feedback=changed,
            )

    def test_snapshot_or_target_conflict_refuses_before_dispatch(self):
        pending, bundle, queue = fixture(4)
        feedback = self.selection(bundle, target_ids=(queue.pending_order[-1],))
        provider = AssessingProvider()
        for selection, arguments in (
            (replace(feedback, head_sha="c" * 40), {}),
            (feedback, {"targets": (queue.pending_order[0],)}),
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ReviewInputError):
                self.run_work(
                    pending, bundle, queue, selection, provider=provider, **arguments
                )
        self.assertEqual(provider.calls, [])

    def test_prompt_refusal_preserves_whole_selection_and_all_obligations(self):
        pending, bundle, queue = fixture(4)
        feedback = self.selection(bundle)
        before = feedback.to_dict()
        work, store, provider = self.run_work(
            pending,
            bundle,
            queue,
            feedback,
            work_budgets=ReviewWorkBudgets(mode="unified", batch_prompt_bytes=8192),
        )
        self.assertEqual(provider.calls, [])
        self.assertEqual(feedback.to_dict(), before)
        self.assertEqual(len(work.execution.pending), 4)
        self.assertEqual(store.document["counters"]["provider_calls"], 0)
        self.assertEqual(work.queue.pending_order, queue.pending_order)

    def test_metadata_or_cross_source_citation_rejects_without_satisfaction_retry(self):
        pending, bundle, queue = fixture(4)
        feedback = self.selection(
            bundle,
            bodies=(
                "@sensei This path is intentionally",
                " local-only; proved locally",
            ),
        )
        for citation in (
            "This path is intentionally local-only",
            '"association":"MEMBER"',
        ):
            with self.subTest(citation=citation):

                class ForgingProvider(AssessingProvider):
                    def complete(self, request):
                        result = json.loads(super().complete(request).text)
                        for item in result["assessments"]:
                            item["human_evidence"] = citation
                        return ProviderResponse(json.dumps(result), self.name)

                provider = ForgingProvider()
                work, _, _ = self.run_work(
                    pending, bundle, queue, feedback, provider=provider
                )
                self.assertEqual(len(provider.calls), 1)
                self.assertEqual(work.reply.decisions, ())
                self.assertEqual(len(work.queue.pending_order), 4)


class FeedbackCompatibilityTests(unittest.TestCase):
    def test_invalid_or_unavailable_feedback_refuses_without_inference(self):
        pending, bundle, queue = fixture(4)
        provider = AssessingProvider()
        with self.assertRaises(ReviewInputError):
            reassess(
                pending=pending,
                bundle=bundle,
                queue=queue,
                feedback=object(),
                source_body=HUMAN,
                authority_digest="f" * 64,
                provider=provider,
                work_budgets=ReviewWorkBudgets(mode="unified"),
                checkpoint=DocumentStore().checkpoint(),
            )
        self.assertEqual(provider.calls, [])
