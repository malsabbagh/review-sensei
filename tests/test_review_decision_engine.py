"""Provider-free contracts for opt-in aggregate review decisions."""

from __future__ import annotations

import json
import re
import unittest
from dataclasses import replace

from review_sensei.budgets import ReviewWorkBudgets
from review_sensei.context import ReviewContextCache
from review_sensei.decision_configuration import DecisionConfiguration
from review_sensei.decision_provider import canonical
from review_sensei.errors import ProviderError
from review_sensei.models import ProviderResponse, ReviewRequest, ReviewResult
from review_sensei.outcomes import ResourceBudget, ResourceBudgetTracker
from review_sensei.review_decision_contract import (
    ReviewDecisionAnswer,
    ReviewDecisionResponse,
    strict_json,
)
from review_sensei.service import ReviewService
from review_sensei.stages import ReviewCategory, Stage
from review_sensei.validation import ReviewLimits


def diff_for(*paths):
    return "".join(
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n+++ b/{path}\n"
        "@@ -1 +1 @@\n-old\n+new\n"
        for path in paths
    )


def comment(path="src/app.py", *, blocking=True, body="A concrete defect."):
    return {
        "path": path,
        "line": 1,
        "body": body,
        "blocking": blocking,
        "severity": "high" if blocking else "low",
    }


def configuration(**changes):
    return replace(
        DecisionConfiguration(enabled=True, provider="ollama", model="decision-v1"),
        **changes,
    )


class ReviewFixture:
    name = "fixture"
    model = "review-fixture"

    def __init__(self, *, comments=None, verification="supported", after_review=None):
        self.comments = comments
        self.verification = verification
        self.after_review = after_review
        self.review_requests = []
        self.verification_requests = []

    def complete(self, request):
        if request.prompt.startswith('{"'):
            packet = json.loads(request.prompt)
            if "candidates" in packet and "instructions" in packet:
                self.verification_requests.append(request)
                if self.verification == "malformed":
                    text = "not JSON"
                elif self.verification == "transport_failure":
                    raise ProviderError(
                        "synthetic verification failure", transient=True
                    )
                else:
                    text = canonical(
                        {
                            p["candidate_id"]: {
                                "outcome": self.verification,
                                "evidence_sha256": [e["sha256"] for e in p["evidence"]],
                            }
                            for p in packet["candidates"]
                        }
                    )
                return ProviderResponse(text, self.name, self.model)
        self.review_requests.append(request)
        comments = self.comments
        if comments is None:
            paths = dict.fromkeys(re.findall(r"^\+\+\+ b/(.+)$", request.prompt, re.M))
            comments = [comment(path) for path in paths]
        if self.after_review is not None:
            self.after_review()
        return ProviderResponse(
            canonical({"summary": "Reviewed synthetic changes.", "comments": comments}),
            self.name,
            self.model,
        )


class DecisionFixture:
    """Use the same admitted execution seam as an installed decision adapter."""

    name = "ollama"

    def __init__(
        self, *, model="decision-v1", answers=None, failure=None, after_send=None
    ):
        self.model = model
        self.answers = {
            "kind": "behavioral_defect",
            "evidence": "supported",
            "next": "continue",
            "duplicate": "different_concern",
            **(answers or {}),
        }
        self.failure = failure
        self.after_send = after_send
        self.requests = []
        self.executions = []
        self.dispatches = []

    def evaluate(self, request, execution):
        self.requests.append(request)
        self.executions.append(execution)
        payload = canonical(request.to_dict())

        def send(timeout, maximum):
            self.dispatches.append((payload, timeout, maximum))
            if self.after_send is not None:
                self.after_send()
            if self.failure == "transport":
                raise ProviderError("synthetic decision failure", transient=True)
            if self.failure == "malformed":
                return "not JSON"
            answers = {
                q.name: self.answers[q.name.rsplit("_", 1)[1]]
                for q in request.questions
            }
            if self.failure == "incomplete":
                answers.pop(next(iter(answers)))
            return canonical(answers)

        value = strict_json(execution.execute(payload, send))
        response = ReviewDecisionResponse(
            request.digest(),
            self.name,
            self.model,
            self.model,
            tuple(ReviewDecisionAnswer(name, choice) for name, choice in value.items()),
        )
        response.validate(request)
        return response


class ReviewDecisionEngineTests(unittest.TestCase):
    def request(self, *paths, **changes):
        return ReviewRequest(
            diff=diff_for(*(paths or ("src/app.py",))),
            model="review-fixture",
            **changes,
        )

    def service(self, review, decision, *, settings=None, stages=None, **kwargs):
        return ReviewService(
            review,
            stages=stages
            or (Stage("review", "Review {diff}", ("summary", "comments")),),
            decision_configuration=settings or configuration(),
            decision_provider=decision,
            **kwargs,
        )

    def test_disabled_configuration_preserves_serialized_result_and_digest(self):
        request = self.request()
        stages = (Stage("review", "Review {diff}", ("summary", "comments")),)
        baseline = ReviewService(ReviewFixture(), stages=stages).run(request)
        disabled = ReviewService(
            ReviewFixture(),
            stages=stages,
            decision_configuration=configuration(enabled=False),
        ).run(request)
        self.assertEqual(baseline.result.to_dict(), disabled.result.to_dict())
        self.assertEqual(
            baseline.result.content_digest(), disabled.result.content_digest()
        )
        self.assertNotIn("decision_pass", disabled.result.to_dict())
        self.assertEqual(disabled.outcome.provider_calls, 1)

    def test_decision_runs_on_aggregate_after_all_review_stages(self):
        review = ReviewFixture()
        decision = DecisionFixture()
        stages = tuple(
            Stage(name, "Review {diff}", ("summary", "comments"))
            for name in ("first", "second")
        )
        run = self.service(review, decision, stages=stages).run(self.request())
        self.assertEqual(len(review.review_requests), 2)
        self.assertEqual(len(decision.dispatches), 1)
        self.assertEqual(len(json.loads(decision.requests[0].state)["candidates"]), 1)
        self.assertEqual(run.outcome.provider_calls, 3)
        self.assertEqual(run.result.review_status, "complete")
        self.assertTrue(run.result.comments[0].blocks_approval)

    def test_chunked_review_decides_on_complete_aggregate_once(self):
        for mode in ("legacy", "unified"):
            with self.subTest(mode=mode):
                review = ReviewFixture()
                decision = DecisionFixture()
                request = self.request(
                    "src/one.py",
                    "src/two.py",
                    orchestrate_large_changes=True,
                    limits=ReviewLimits(max_diff_files=1),
                )
                run = self.service(
                    review, decision, work_budgets=ReviewWorkBudgets(mode=mode)
                ).run(request)
                self.assertGreaterEqual(len(review.review_requests), 2)
                self.assertEqual(len(decision.dispatches), 1)
                packets = json.loads(decision.requests[0].state)["candidates"]
                self.assertEqual(
                    {p["original"]["path"] for p in packets},
                    {"src/one.py", "src/two.py"},
                )
                self.assertEqual(len(run.result.comments), 2)
                self.assertEqual(
                    run.outcome.provider_calls, len(review.review_requests) + 1
                )

    def test_zero_candidates_do_not_dispatch_decision_or_verification(self):
        review = ReviewFixture(comments=[])
        decision = DecisionFixture()
        run = self.service(review, decision).run(self.request())
        self.assertEqual(decision.requests, [])
        self.assertEqual(review.verification_requests, [])
        self.assertEqual(run.outcome.provider_calls, 1)
        self.assertEqual(run.result.comments, ())
        self.assertEqual(run.result.review_status, "complete")

    def test_decisions_retain_original_tracker_and_prior_charges(self):
        tracker = ResourceBudgetTracker(ResourceBudget.create())
        tracker.record_call("previous admitted work", "previous bounded response")
        decision = DecisionFixture()
        run = self.service(ReviewFixture(), decision).run(
            self.request(), tracker=tracker
        )
        self.assertIs(decision.executions[0].tracker, tracker)
        self.assertEqual(tracker.provider_calls, 3)
        self.assertEqual(run.outcome.provider_calls, 3)
        self.assertEqual(run.outcome.prompt_bytes, tracker.prompt_bytes)
        self.assertEqual(run.outcome.response_bytes, tracker.response_bytes)
        self.assertGreater(tracker.prompt_bytes, decision.executions[0].input_bytes)

    def test_original_call_budget_refuses_decision_and_retains_blocker(self):
        review = ReviewFixture()
        decision = DecisionFixture()
        tracker = ResourceBudgetTracker(ResourceBudget.create(max_provider_calls=1))
        run = self.service(review, decision).run(self.request(), tracker=tracker)
        self.assertEqual(decision.dispatches, [])
        self.assertEqual(tracker.provider_calls, 1)
        self.assertEqual(run.outcome.status, "partial")
        self.assertEqual(run.result.review_status, "partial")
        self.assertEqual(run.result.comments[0].to_dict(), comment())
        self.assertTrue(run.result.comments[0].blocks_approval)
        self.assertEqual(run.result.decision_pass["status"], "incomplete")

    def test_work_call_limit_also_caps_decision_dispatch(self):
        request = self.request()
        request = replace(
            request, work_budget=replace(request.work_budget, max_provider_calls=1)
        )
        decision = DecisionFixture()
        run = self.service(ReviewFixture(), decision).run(request)
        self.assertEqual(decision.dispatches, [])
        self.assertEqual(run.outcome.provider_calls, 1)
        self.assertEqual(run.result.review_status, "partial")

    def test_subordinate_batch_budget_retains_every_unprocessed_candidate(self):
        originals = [comment(body=f"Distinct concern {i}.") for i in range(4)]
        decision = DecisionFixture()
        run = self.service(
            ReviewFixture(comments=originals),
            decision,
            settings=configuration(max_calls=1),
        ).run(self.request())
        self.assertEqual(len(decision.dispatches), 1)
        self.assertEqual(run.outcome.provider_calls, 2)
        self.assertEqual([c.to_dict() for c in run.result.comments], originals)
        self.assertEqual(len(run.result.decision_pass["candidates"]), 4)
        self.assertEqual(
            run.result.decision_pass["candidates"][-1]["status"], "unprocessed"
        )
        self.assertEqual(run.result.review_status, "partial")

    def test_unknown_output_reservation_is_not_refunded_for_later_batch(self):
        originals = [comment(body=f"Distinct concern {i}.") for i in range(4)]
        decision = DecisionFixture(failure="transport")
        run = self.service(
            ReviewFixture(comments=originals),
            decision,
            settings=configuration(max_output_bytes=8192),
        ).run(self.request())
        self.assertEqual(len(decision.dispatches), 1)
        self.assertEqual(run.outcome.provider_calls, 2)
        self.assertEqual(run.outcome.retry_attempts, 0)
        self.assertEqual(run.result.decision_pass["output_reserved"], 8192)
        self.assertEqual([c.to_dict() for c in run.result.comments], originals)
        self.assertEqual(run.result.review_status, "partial")

    def test_oversized_extension_output_stops_later_dispatch_and_retains_charges(self):
        for unknown_first in (False, True):
            with self.subTest(unknown_first=unknown_first):

                class OversizedDecision(DecisionFixture):
                    def evaluate(self, request, execution):
                        if not self.requests:
                            self.before_response_bytes = (
                                execution.tracker.response_bytes
                            )
                        self.requests.append(request)

                        def send(timeout, maximum):
                            self.dispatches.append((request.state, timeout, maximum))
                            if unknown_first and len(self.dispatches) == 1:
                                raise ProviderError("synthetic unknown response")
                            return "x" * (maximum + 1)

                        execution.execute(request.state, send)
                        raise AssertionError("oversized output must not be accepted")

                expected_calls = 2 if unknown_first else 1
                originals = [
                    comment(body=f"Distinct concern {i}.")
                    for i in range(7 if unknown_first else 4)
                ]
                decision = OversizedDecision()
                run = self.service(ReviewFixture(comments=originals), decision).run(
                    self.request()
                )
                self.assertEqual(len(decision.dispatches), expected_calls)
                receipt = run.result.decision_pass
                self.assertEqual(receipt["calls"], expected_calls)
                self.assertEqual(receipt["output_bytes"], 8193)
                self.assertEqual(receipt["output_reserved"], expected_calls * 8192)
                self.assertEqual(run.outcome.provider_calls, 1 + expected_calls)
                self.assertEqual(
                    run.outcome.response_bytes, decision.before_response_bytes + 8193
                )
                self.assertEqual(run.outcome.retry_attempts, 0)
                self.assertEqual([c.to_dict() for c in run.result.comments], originals)
                self.assertEqual(receipt["status"], "incomplete")
                self.assertEqual(run.result.review_status, "partial")

    def test_decision_input_bound_refuses_before_dispatch_without_losing_candidate(
        self,
    ):
        decision = DecisionFixture()
        run = self.service(
            ReviewFixture(), decision, settings=configuration(max_input_bytes=1)
        ).run(self.request())
        self.assertEqual(decision.dispatches, [])
        self.assertEqual(run.outcome.provider_calls, 1)
        self.assertEqual(run.result.comments[0].to_dict(), comment())
        self.assertEqual(run.result.review_status, "partial")

    def test_original_deadline_caps_decision_and_late_completion_is_partial(self):
        clock = [0.0]
        tracker = ResourceBudgetTracker(
            ResourceBudget.create(timeout_ms=1000), monotonic=lambda: clock[0]
        )
        review = ReviewFixture(after_review=lambda: clock.__setitem__(0, 0.8))
        decision = DecisionFixture(after_send=lambda: clock.__setitem__(0, 1.1))
        run = self.service(review, decision).run(self.request(), tracker=tracker)
        self.assertEqual(len(decision.dispatches), 1)
        self.assertAlmostEqual(decision.dispatches[0][1], 0.2)
        self.assertEqual(run.outcome.provider_calls, 2)
        self.assertEqual(run.result.review_status, "partial")
        self.assertTrue(run.result.comments[0].blocks_approval)

    def test_malformed_incomplete_and_transport_failures_retain_candidates(self):
        for failure in ("malformed", "incomplete", "transport"):
            with self.subTest(failure=failure):
                decision = DecisionFixture(failure=failure)
                run = self.service(ReviewFixture(), decision).run(self.request())
                self.assertEqual(len(decision.dispatches), 1)
                self.assertEqual(run.outcome.provider_calls, 2)
                self.assertEqual(run.result.review_status, "partial")
                self.assertEqual(run.result.comments[0].to_dict(), comment())
                self.assertEqual(run.result.decision_pass["status"], "incomplete")
                self.assertFalse(run.result.coverage_only_partial)
                self.assertIsNone(run.result.bounded_work_receipt)

    def test_optional_nonblocking_classification_retains_original_comment(self):
        original = comment(blocking=False)
        decision = DecisionFixture(answers={"kind": "optional_improvement"})
        run = self.service(ReviewFixture(comments=[original]), decision).run(
            self.request()
        )
        self.assertEqual(run.result.comments[0].to_dict(), original)
        self.assertFalse(run.result.comments[0].blocks_approval)
        self.assertEqual(run.result.review_status, "complete")
        self.assertEqual(
            run.result.decision_pass["candidates"][0]["action"], "optional_suggestion"
        )

    def test_optional_classification_cannot_waive_existing_blocker(self):
        decision = DecisionFixture(answers={"kind": "optional_improvement"})
        review = ReviewFixture()
        run = self.service(review, decision).run(self.request())
        self.assertEqual(run.result.comments[0].to_dict(), comment())
        self.assertTrue(run.result.comments[0].blocks_approval)
        self.assertEqual(run.result.review_status, "partial")
        self.assertEqual(
            run.result.decision_pass["candidates"][0]["action"], "human_review"
        )
        self.assertEqual(review.verification_requests, [])

    def test_duplicate_assessment_does_not_delete_candidates(self):
        originals = [comment(body="First concern."), comment(body="Second concern.")]
        decision = DecisionFixture(answers={"duplicate": "same_concern"})
        run = self.service(ReviewFixture(comments=originals), decision).run(
            self.request()
        )
        self.assertEqual([c.to_dict() for c in run.result.comments], originals)
        records = run.result.decision_pass["candidates"]
        self.assertEqual(len(records), 2)
        self.assertTrue(records[1]["suspected_duplicate"])
        self.assertTrue(all(c.blocks_approval for c in run.result.comments))

    def test_targeted_followup_batches_all_requested_candidates_once(self):
        review = ReviewFixture()
        decision = DecisionFixture(answers={"next": "verify_further"})
        run = self.service(review, decision).run(self.request("src/a.py", "src/b.py"))
        self.assertEqual(len(decision.dispatches), 1)
        self.assertEqual(len(review.verification_requests), 1)
        packet = json.loads(review.verification_requests[0].prompt)
        self.assertEqual(len(packet["candidates"]), 2)
        self.assertEqual(run.outcome.provider_calls, 3)
        self.assertEqual(len(run.result.comments), 2)
        self.assertTrue(all(c.blocks_approval for c in run.result.comments))
        self.assertTrue(run.result.decision_pass["verification_called"])
        self.assertEqual(run.result.review_status, "complete")

    def test_failed_verification_does_not_retry_or_clear_candidates(self):
        for outcome in (
            "malformed",
            "transport_failure",
            "contradicted",
            "insufficient_evidence",
        ):
            with self.subTest(outcome=outcome):
                review = ReviewFixture(verification=outcome)
                decision = DecisionFixture(answers={"next": "verify_further"})
                run = self.service(review, decision).run(self.request())
                self.assertEqual(len(review.verification_requests), 1)
                self.assertEqual(len(decision.dispatches), 1)
                self.assertEqual(run.outcome.provider_calls, 3)
                self.assertEqual(run.outcome.retry_attempts, 0)
                self.assertEqual(run.result.review_status, "partial")
                self.assertEqual(run.result.comments[0].to_dict(), comment())

    def test_followup_respects_remaining_original_call_budget(self):
        tracker = ResourceBudgetTracker(ResourceBudget.create(max_provider_calls=2))
        decision = DecisionFixture(answers={"next": "verify_further"})
        review = ReviewFixture()
        run = self.service(review, decision).run(self.request(), tracker=tracker)
        self.assertEqual(len(decision.dispatches), 1)
        self.assertEqual(review.verification_requests, [])
        self.assertEqual(run.outcome.provider_calls, 2)
        self.assertEqual(run.result.review_status, "partial")
        self.assertTrue(run.result.comments[0].blocks_approval)

    def test_followup_preserves_explicit_review_model_selection(self):
        review = ReviewFixture()
        decision = DecisionFixture(answers={"next": "verify_further"})
        request = replace(self.request(), model="explicit-review-model")
        self.service(review, decision).run(request)
        self.assertEqual(review.review_requests[0].model, "explicit-review-model")
        self.assertEqual(len(review.verification_requests), 1)
        self.assertEqual(review.verification_requests[0].model, "explicit-review-model")

    def test_partial_analysis_refuses_decisions_and_preserves_incomplete_status(self):
        inactive = ReviewCategory(
            id="docs",
            title="Documentation",
            focus=("Accuracy",),
            applies_to=("docs/**",),
        )
        stages = (
            Stage("review", "Review {diff}", ("summary", "comments")),
            Stage(
                "docs",
                "Review {review_categories} {diff}",
                ("comments",),
                categories=(inactive,),
            ),
        )
        decision = DecisionFixture()
        run = self.service(ReviewFixture(), decision, stages=stages).run(
            replace(self.request(), active_category_ids=())
        )
        self.assertEqual(decision.dispatches, [])
        self.assertEqual(run.result.decision_pass["status"], "incomplete")
        self.assertEqual(run.result.review_status, "partial")
        self.assertEqual(run.outcome.status, "partial")

    def test_terminal_analysis_failure_keeps_unknown_output_reserved(self):
        class FailsSecondBatch(ReviewFixture):
            def complete(self, request):
                if self.review_requests:
                    self.review_requests.append(request)
                    raise ProviderError("synthetic terminal failure", transient=False)
                return super().complete(request)

        review = FailsSecondBatch()
        decision = DecisionFixture()
        tracker = ResourceBudgetTracker(ResourceBudget.create())
        request = self.request(
            "src/a.py",
            "src/b.py",
            orchestrate_large_changes=True,
            limits=ReviewLimits(max_diff_files=1),
        )
        run = self.service(
            review,
            decision,
            work_budgets=ReviewWorkBudgets(mode="unified", max_total_output_bytes=4096),
        ).run(request, tracker=tracker)
        self.assertEqual(len(review.review_requests), 2)
        self.assertGreater(review.review_requests[1].max_response_bytes, 0)
        self.assertEqual(tracker.transport_retries, 0)
        self.assertEqual(decision.dispatches, [])
        self.assertEqual(run.outcome.provider_calls, 2)
        self.assertEqual(run.result.review_status, "partial")
        self.assertEqual(run.result.comments[0].path, "src/a.py")
        self.assertTrue(run.result.comments[0].blocks_approval)
        self.assertEqual(run.result.decision_pass["status"], "incomplete")

    def test_full_summary_omits_optional_appendix_and_preserves_receipt(self):
        summary = "Reviewed synthetic changes."
        request = self.request(
            limits=ReviewLimits(max_summary_bytes=len(summary.encode()))
        )
        run = self.service(ReviewFixture(), DecisionFixture()).run(request)
        self.assertEqual(run.result.summary, summary)
        self.assertEqual(run.result.comments[0].to_dict(), comment())
        self.assertEqual(run.result.decision_pass["status"], "complete")
        self.assertEqual(run.result.review_status, "complete")
        self.assertEqual(run.outcome.provider_calls, 2)

    def test_result_capacity_failure_preserves_candidates_as_partial_without_receipt(
        self,
    ):
        stages = (Stage("review", "Review {diff}", ("summary", "comments")),)
        baseline = ReviewService(ReviewFixture(), stages=stages).run(self.request())
        original_bytes = len(
            json.dumps(baseline.result.to_dict(), separators=(",", ":")).encode()
        )
        request = self.request(limits=ReviewLimits(max_result_bytes=original_bytes))
        run = self.service(ReviewFixture(), DecisionFixture(), stages=stages).run(
            request
        )
        self.assertIsNotNone(run.result)
        self.assertEqual(run.result.summary, baseline.result.summary)
        self.assertEqual(run.result.comments, baseline.result.comments)
        self.assertIsNone(run.result.decision_pass)
        self.assertEqual(run.result.review_status, "partial")
        self.assertEqual(run.outcome.status, "partial")
        self.assertEqual(run.outcome.stage_summary["decision"], "incomplete")
        self.assertEqual(run.outcome.provider_calls, 2)

    def test_failed_decision_does_not_cache_complete_analysis_or_evict_prior(self):
        cache = ReviewContextCache()
        request = self.request(
            repository="org/repo",
            pull_request_number=1,
            base_sha="a" * 40,
            head_sha="b" * 40,
        )
        service = self.service(
            ReviewFixture(), DecisionFixture(failure="malformed"), cache=cache
        )
        current_key = service.context_cache_key(request)
        prior_key = replace(current_key, head_sha="c" * 40, model="older-model")
        cache.put(prior_key, (1, "full", 1))
        run = service.run(request)
        self.assertEqual(run.result.review_status, "partial")
        self.assertIsNone(cache.get(current_key))
        self.assertEqual(cache.get(prior_key), (1, "full", 1))

    def test_receipt_roundtrip_preserves_digest_and_candidates(self):
        run = self.service(ReviewFixture(), DecisionFixture()).run(self.request())
        document = json.loads(json.dumps(run.result.to_dict()))
        restored = ReviewResult.from_dict(document)
        self.assertEqual(restored.to_dict(), document)
        self.assertEqual(restored.content_digest(), run.result.content_digest())
        self.assertEqual(restored.comments, run.result.comments)

    def test_decision_model_and_policy_change_cache_identity(self):
        request = self.request(
            repository="org/repo",
            pull_request_number=1,
            base_sha="a" * 40,
            head_sha="b" * 40,
        )
        original = self.service(ReviewFixture(), DecisionFixture())
        changed_model = self.service(
            ReviewFixture(),
            DecisionFixture(model="decision-v2"),
            settings=configuration(model="decision-v2"),
        )
        changed_budget = self.service(
            ReviewFixture(), DecisionFixture(), settings=configuration(max_calls=2)
        )
        original_key = original.context_cache_key(request)
        self.assertIsNotNone(original_key)
        self.assertNotEqual(original_key, changed_model.context_cache_key(request))
        self.assertNotEqual(original_key, changed_budget.context_cache_key(request))
        disabled = ReviewService(
            ReviewFixture(),
            stages=original.stages,
            decision_configuration=configuration(enabled=False),
        )
        baseline = ReviewService(ReviewFixture(), stages=original.stages)
        self.assertEqual(
            disabled.context_cache_key(request), baseline.context_cache_key(request)
        )
        self.assertNotEqual(original_key, baseline.context_cache_key(request))


if __name__ == "__main__":
    unittest.main()
