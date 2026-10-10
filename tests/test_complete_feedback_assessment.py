"""Complete feedback enters the existing exact-head assessment seam as data."""

import json
import unittest
from dataclasses import replace

from review_sensei.errors import ReviewInputError
from review_sensei.feedback import FeedbackReference, FeedbackSelection, FeedbackSource
from review_sensei.human_assessment import (
    HumanAssessmentService,
    HumanAssessmentValidationError,
)
from tests.test_human_assessment import (
    BASE,
    DIFF,
    HEAD,
    Provider,
    State,
    prior,
    response_for,
)


def selection(pending, bodies, **changes):
    refs = tuple(
        FeedbackReference("issue", i + 10, "2026-10-10T01:00:00Z")
        for i in range(len(bodies))
    )
    return FeedbackSelection(
        repository="owner/repo",
        pull_request=1,
        base_sha=BASE,
        head_sha=HEAD,
        trigger=refs[0],
        sources=tuple(
            FeedbackSource(ref, "alice", 7, "MEMBER", body)
            for ref, body in zip(refs, bodies, strict=True)
        ),
        target_ids=tuple(item.fingerprint for item in pending.pending),
        **changes,
    )


class CompleteFeedbackAssessmentTests(unittest.TestCase):
    def test_complete_sources_over_legacy_limit_are_json_data_and_citable(self):
        eligibility = prior()
        pending = eligibility.human_review
        bodies = (
            "@sensei selected trigger " + '界\\"' * 1500,
            "This path is intentionally local-only; tail evidence",
        )
        feedback = selection(pending, bodies)
        provider = Provider(response_for(eligibility))
        context = replace(State(eligibility).prepared().context, pull_request_number=1)
        result = HumanAssessmentService(provider).reply(
            context=context, pending=pending, feedback=feedback
        )
        self.assertEqual(len(result.decisions), 1)
        request = provider.calls[0]
        reference = json.loads(request.prompt[request.prompt.index('{"feedback":') :])
        self.assertEqual(reference["feedback"], feedback.to_dict())
        self.assertNotIn("human_reply", reference["assessment_context"])
        self.assertLess(len(request.prompt.encode()), request.max_prompt_bytes)
        self.assertEqual(pending.apply(result.decisions).pending, ())

    def test_non_unresolved_citation_cannot_span_sources_or_use_framing(self):
        eligibility = prior()
        pending = eligibility.human_review
        feedback = selection(
            pending,
            ("@sensei This path is intentionally", " local-only; verified elsewhere"),
        )
        for citation in (
            "This path is intentionally local-only",
            '"association":"MEMBER"',
            "Human evidence must occur wholly within ONE original selected source body",
        ):
            with self.subTest(citation=citation):
                payload = response_for(eligibility)
                payload["assessments"][0]["human_evidence"] = citation
                provider = Provider(payload)
                with self.assertRaises(HumanAssessmentValidationError) as caught:
                    HumanAssessmentService(provider).reply(
                        context=State(eligibility).prepared().context,
                        pending=pending,
                        feedback=feedback,
                    )
                self.assertEqual(
                    caught.exception.diagnostic,
                    "human_assessment_human_evidence_mismatch",
                )
                self.assertTrue(pending.pending)

    def test_legacy_source_cannot_override_selected_source_citation(self):
        eligibility = prior()
        pending = eligibility.human_review
        feedback = selection(
            pending, ("@sensei unrelated selected explanation remains unresolved",)
        )
        with self.assertRaises(HumanAssessmentValidationError):
            HumanAssessmentService(Provider(response_for(eligibility))).reply(
                context=State(eligibility).prepared().context,
                pending=pending,
                source_body="This path is intentionally local-only",
                feedback=feedback,
            )

    def test_full_prompt_framing_diff_and_pending_are_preflighted(self):
        eligibility = prior()
        pending = eligibility.human_review
        feedback = selection(
            pending, ("@sensei This path is intentionally local-only " + "字" * 17500,)
        )
        provider = Provider(response_for(eligibility))
        context = State(eligibility).prepared().context
        with self.assertRaises(ReviewInputError):
            HumanAssessmentService(provider).reply(
                context=context, pending=pending, feedback=feedback
            )
        self.assertEqual(provider.calls, [])
        small = selection(pending, ("@sensei This path is intentionally local-only",))
        request = HumanAssessmentService._request(
            head_sha=HEAD,
            pending=pending,
            source_body="",
            diff_context="path=src/app.py\n" + DIFF,
            feedback=small,
        )
        exact = len(request.prompt.encode())
        HumanAssessmentService._request(
            head_sha=HEAD,
            pending=pending,
            source_body="",
            diff_context="path=src/app.py\n" + DIFF,
            feedback=small,
            max_prompt_bytes=exact,
        )
        with self.assertRaises(ReviewInputError):
            HumanAssessmentService._request(
                head_sha=HEAD,
                pending=pending,
                source_body="",
                diff_context="path=src/app.py\n" + DIFF,
                feedback=small,
                max_prompt_bytes=exact - 1,
            )

    def test_snapshot_and_explicit_target_conflicts_fail_before_provider(self):
        eligibility = prior()
        pending = eligibility.human_review
        original = selection(
            pending, ("@sensei This path is intentionally local-only",)
        )
        context = replace(State(eligibility).prepared().context, pull_request_number=1)
        for feedback in (
            replace(original, head_sha="c" * 40),
            replace(original, base_sha="d" * 40),
            replace(original, pull_request=2),
            replace(original, target_ids=("e" * 64,)),
        ):
            provider = Provider(response_for(eligibility))
            with (
                self.subTest(feedback=feedback.digest),
                self.assertRaises(ReviewInputError),
            ):
                HumanAssessmentService(provider).reply(
                    context=context, pending=pending, feedback=feedback
                )
            self.assertEqual(provider.calls, [])

    def test_unresolved_without_citation_retains_obligation(self):
        eligibility = prior()
        pending = eligibility.human_review
        payload = response_for(eligibility, decision="unresolved")
        payload["assessments"][0]["human_evidence"] = ""
        payload["assessments"][0]["diff_evidence"] = ""
        feedback = selection(
            pending, ("@sensei please reassess this complete original comment",)
        )
        result = HumanAssessmentService(Provider(payload)).reply(
            context=State(eligibility).prepared().context,
            pending=pending,
            feedback=feedback,
        )
        self.assertTrue(pending.apply(result.decisions).pending)

    def test_legacy_single_source_4096_byte_limit_is_unchanged(self):
        eligibility = prior()
        provider = Provider(response_for(eligibility))
        with self.assertRaises(ReviewInputError):
            HumanAssessmentService(provider).reply(
                context=State(eligibility).prepared().context,
                pending=eligibility.human_review,
                source_body="a" * 4097,
            )
        self.assertEqual(provider.calls, [])


if __name__ == "__main__":
    unittest.main()
