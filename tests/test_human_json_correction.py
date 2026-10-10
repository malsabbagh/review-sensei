"""Synthetic public-service regressions; no provider or repository credentials."""

import io
import json
import traceback
import unittest
from contextlib import redirect_stderr
from dataclasses import replace
from unittest.mock import patch

from review_sensei.cli import _print_offline_error
from review_sensei.errors import ProviderError, ReviewInputError
from review_sensei.human_assessment import (
    HumanAssessmentService,
    HumanAssessmentValidationError,
)
from review_sensei.models import ProviderResponse
from review_sensei.outcomes import ResourceBudget, ResourceBudgetTracker
from tests.test_human_assessment import HUMAN, State, response_for
from tests.test_provider_completion_guards import complete_document
from tests.test_provider_completion_guards import provider as ollama_fixture


class SequenceProvider:
    name = "synthetic-offline"
    model = "fixture"
    max_output_tokens = 2048

    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def complete(self, request):
        self.calls.append(request)
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return ProviderResponse(value, self.name)


class HumanJsonCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.state = State()
        _, self.prepared = self.state.bridge()
        self.valid = json.dumps(response_for(self.state.eligibility))

    def reply(self, provider, **kwargs):
        return HumanAssessmentService(provider).reply(
            context=self.prepared.conversation.context,
            pending=self.prepared.eligibility.human_review,
            source_body=HUMAN,
            **kwargs,
        )

    def test_valid_json_uses_one_call(self):
        provider = SequenceProvider(self.valid)
        reply = self.reply(provider)
        self.assertEqual(len(reply.decisions), 1)
        self.assertEqual(len(provider.calls), 1)

    def test_malformed_then_valid_uses_one_fresh_correction(self):
        provider = SequenceProvider("synthetic malformed JSON", self.valid)
        reply = self.reply(provider)
        self.assertEqual(len(reply.decisions), 1)
        self.assertEqual(len(provider.calls), 2)
        first, corrected = provider.calls
        self.assertTrue(corrected.prompt.startswith(first.prompt))
        self.assertNotIn("synthetic malformed JSON", corrected.prompt)
        self.assertEqual(corrected.max_output_tokens, first.max_output_tokens)
        self.assertGreater(first.timeout_seconds, 0)
        self.assertLessEqual(corrected.timeout_seconds, first.timeout_seconds)

    def tracker(self, **limits):
        return ResourceBudgetTracker(
            ResourceBudget.create(**limits), monotonic=lambda: 10.0
        )

    def test_correction_counts_original_calls_bytes_and_retry(self):
        tracker = self.tracker()
        original = (tracker.started, tracker.execution_identity)
        malformed = "bad synthetic json"
        provider = SequenceProvider(malformed, self.valid)
        self.reply(provider, tracker=tracker)
        self.assertEqual((tracker.started, tracker.execution_identity), original)
        self.assertEqual(tracker.provider_calls, 2)
        self.assertEqual(tracker.structural_retries, 1)
        self.assertEqual(tracker.transport_retries, 0)
        self.assertEqual(
            tracker.prompt_bytes, sum(len(r.prompt.encode()) for r in provider.calls)
        )
        self.assertEqual(
            tracker.response_bytes, len(malformed.encode()) + len(self.valid.encode())
        )
        self.assertEqual([r.max_output_tokens for r in provider.calls], [2048, 2048])

    def test_repeated_malformed_stops_after_two_calls_with_safe_geometry(self):
        secret = "synthetic-private-marker"
        provider = SequenceProvider("{\n" + secret, "{\n" + secret, self.valid)
        tracker = self.tracker()
        with self.assertRaises(HumanAssessmentValidationError) as caught:
            self.reply(provider, tracker=tracker)
        error = caught.exception
        self.assertEqual(error.diagnostic, "human_assessment_invalid_json")
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(error.details["json_line"], 2)
        self.assertEqual(error.details["json_column"], 1)
        self.assertEqual(error.details["json_offset"], 2)
        self.assertEqual(
            error.details["response_bytes"], len(("{\n" + secret).encode())
        )
        self.assertTrue(error.details["correction_attempted"])
        self.assertTrue(error.__suppress_context__)
        self.assertNotIn(secret, "".join(traceback.format_exception(error)))
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            _print_offline_error(error)
        self.assertIn("json_line=2", stderr.getvalue())
        self.assertNotIn(secret, stderr.getvalue())
        self.assertNotIn(HUMAN, stderr.getvalue())
        self.assertFalse(self.prepared.eligibility.human_review.resolved)

    def test_call_and_retry_exhaustion_never_dispatch_correction(self):
        for limits, reason in (
            ({"max_provider_calls": 1}, "provider_call_limit"),
            ({"max_retry_attempts": 0}, "structural_retry_limit"),
        ):
            with self.subTest(reason=reason):
                tracker = self.tracker(**limits)
                provider = SequenceProvider("bad json", self.valid)
                with self.assertRaises(HumanAssessmentValidationError) as caught:
                    self.reply(provider, tracker=tracker)
                self.assertEqual(caught.exception.details["budget"], reason)
                self.assertFalse(caught.exception.details["correction_attempted"])
                self.assertEqual(len(provider.calls), 1)

    def test_existing_charges_and_retry_allowance_are_not_reset(self):
        tracker = self.tracker()
        tracker.provider_calls = 3
        tracker.structural_retries = 1
        tracker.prompt_bytes = 100
        tracker.response_bytes = 200
        identity = (tracker.started, tracker.execution_identity)
        provider = SequenceProvider("bad json", self.valid)
        self.reply(provider, tracker=tracker)
        self.assertEqual((tracker.started, tracker.execution_identity), identity)
        self.assertEqual(tracker.provider_calls, 5)
        self.assertEqual(tracker.structural_retries, 2)
        self.assertEqual(
            tracker.prompt_bytes,
            100 + sum(len(r.prompt.encode()) for r in provider.calls),
        )
        self.assertEqual(
            tracker.response_bytes, 200 + len("bad json") + len(self.valid.encode())
        )
        blocked = SequenceProvider("bad json", self.valid)
        with self.assertRaises(HumanAssessmentValidationError):
            self.reply(blocked, tracker=tracker)
        self.assertEqual(len(blocked.calls), 1)
        self.assertEqual(tracker.structural_retries, 2)

    def test_prompt_and_output_aggregate_bounds_stop_correction(self):
        first = SequenceProvider(self.valid)
        self.reply(first)
        prompt_size = len(first.calls[0].prompt.encode())
        for limits, response, reason in (
            ({"max_prompt_bytes": prompt_size + 500}, "bad json", "prompt_budget"),
            ({"max_prompt_bytes": prompt_size}, "bad json", "prompt_budget"),
            ({"max_output_bytes": 8}, "bad json", "output_budget"),
        ):
            with self.subTest(limits=limits):
                provider = SequenceProvider(response, self.valid)
                tracker = self.tracker(**limits)
                with self.assertRaises(HumanAssessmentValidationError) as caught:
                    self.reply(provider, tracker=tracker)
                self.assertEqual(caught.exception.details["budget"], reason)
                self.assertFalse(caught.exception.details["correction_attempted"])
                self.assertEqual(len(provider.calls), 1)

    def test_expired_original_budget_never_calls_provider(self):
        clock = [0.0]
        tracker = ResourceBudgetTracker(
            ResourceBudget.create(), monotonic=lambda: clock[0]
        )
        clock[0] = 120.0
        provider = SequenceProvider(self.valid)
        with self.assertRaises(ReviewInputError) as caught:
            self.reply(provider, tracker=tracker)
        self.assertEqual(caught.exception.diagnostic, "deadline_exceeded")
        self.assertEqual(provider.calls, [])

    def test_schema_and_citations_are_not_repaired(self):
        for field, value, reason in (
            ("body", None, "human_assessment_reply_body_invalid"),
            (
                "human_evidence",
                "Unsupported synthetic explanation.",
                "human_assessment_human_evidence_mismatch",
            ),
            (
                "diff_evidence",
                "+not_in_the_current_diff",
                "human_assessment_diff_evidence_mismatch",
            ),
        ):
            for corrected in (False, True):
                with self.subTest(field=field, corrected=corrected):
                    payload = json.loads(self.valid)
                    target = payload if field == "body" else payload["assessments"][0]
                    target[field] = value
                    responses = [json.dumps(payload), self.valid]
                    if corrected:
                        responses.insert(0, "bad json")
                    provider = SequenceProvider(*responses)
                    with self.assertRaises(HumanAssessmentValidationError) as caught:
                        self.reply(provider)
                    self.assertEqual(caught.exception.diagnostic, reason)
                    self.assertEqual(
                        caught.exception.details["correction_attempted"], corrected
                    )
                    self.assertEqual(len(provider.calls), 2 if corrected else 1)

    def test_provider_transport_or_completion_failure_is_not_json_correction(self):
        provider = SequenceProvider(
            ProviderError("synthetic private payload", transient=True), self.valid
        )
        tracker = self.tracker()
        with self.assertRaises(ProviderError) as caught:
            self.reply(provider, tracker=tracker)
        self.assertNotIn(
            "synthetic private payload",
            "".join(traceback.format_exception(caught.exception)),
        )
        self.assertNotIn("invalid_json", str(caught.exception))
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(tracker.provider_calls, 1)
        self.assertEqual(tracker.structural_retries, 0)

    def test_oversized_normalized_response_is_not_corrected(self):
        provider = SequenceProvider("x" * (16 * 1024 + 1), self.valid)
        with self.assertRaises(ReviewInputError):
            self.reply(provider)
        self.assertEqual(len(provider.calls), 1)

    def test_adapter_exception_details_never_leak_or_trigger_correction(self):
        for exception, expected in (
            (
                ReviewInputError("synthetic-private-marker"),
                HumanAssessmentValidationError,
            ),
            (RuntimeError("synthetic-private-marker"), ProviderError),
        ):
            with self.subTest(exception=type(exception).__name__):
                provider = SequenceProvider(exception, self.valid)
                with self.assertRaises(expected) as caught:
                    self.reply(provider)
                self.assertNotIn(
                    "synthetic-private-marker",
                    "".join(traceback.format_exception(caught.exception)),
                )
                self.assertEqual(len(provider.calls), 1)

    def test_remaining_output_cap_and_timeout_tighten_second_request(self):
        tracker = self.tracker(max_output_bytes=16000)
        provider = SequenceProvider("x" * 10000, self.valid)
        self.reply(provider, tracker=tracker)
        self.assertEqual([r.max_response_bytes for r in provider.calls], [16000, 6000])

    def test_actual_adapter_truncation_metadata_never_enters_json_correction(self):
        for changes in (
            {"done": False},
            {"done_reason": "length"},
            {"eval_count": 129},
        ):
            with self.subTest(changes=changes):
                instance, transports = ollama_fixture(
                    complete_document(response=self.valid, **changes)
                )
                tracker = self.tracker()
                with self.assertRaises(ProviderError) as caught:
                    self.reply(instance, tracker=tracker)
                self.assertNotIn("invalid_json", str(caught.exception))
                self.assertEqual(len(transports), 1)
                self.assertEqual(tracker.provider_calls, 1)
                self.assertEqual(tracker.structural_retries, 0)
                self.assertFalse(self.prepared.eligibility.human_review.resolved)

    def test_actual_adapter_malformed_http_envelope_never_enters_json_correction(self):
        from review_sensei.providers.ollama import OllamaProvider

        calls = []

        def open_response(request, **kwargs):
            calls.append(request)
            return io.BytesIO(b'{"response":')

        instance = OllamaProvider(opener=open_response, max_output_tokens=128)
        tracker = self.tracker()
        with self.assertRaises(ProviderError):
            self.reply(instance, tracker=tracker)
        self.assertEqual(len(calls), 1)
        self.assertEqual(tracker.structural_retries, 0)

    def test_deadline_during_first_call_stops_before_correction(self):
        clock = [0.0]
        tracker = ResourceBudgetTracker(
            ResourceBudget.create(), monotonic=lambda: clock[0]
        )
        provider = SequenceProvider("bad json", self.valid)
        complete = provider.complete

        def after_deadline(request):
            response = complete(request)
            clock[0] = 120.0
            return response

        provider.complete = after_deadline
        with self.assertRaises(ReviewInputError) as caught:
            self.reply(provider, tracker=tracker)
        self.assertEqual(caught.exception.diagnostic, "deadline_exceeded")
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(tracker.response_bytes, len("bad json"))

    def test_deep_invalid_response_refuses_without_correction_or_payload(self):
        provider = SequenceProvider("[" * 2000 + "0" + "]" * 2000, self.valid)
        with self.assertRaises(HumanAssessmentValidationError) as caught:
            self.reply(provider)
        self.assertIn(
            caught.exception.diagnostic,
            {
                "human_assessment_invalid_response",
                "human_assessment_reply_fields_invalid",
            },
        )
        self.assertEqual(len(provider.calls), 1)

    def test_public_application_legacy_fallback_shares_existing_tracker(self):
        from review_sensei.hosting.github.human_assessment import (
            HumanAssessmentPublisher,
        )

        prepare = HumanAssessmentPublisher.prepare
        retained = []

        def without_bundle(assessor, **kwargs):
            retained.append(kwargs["work_tracker"])
            prepared = prepare(assessor, **kwargs)
            return replace(prepared, evidence_bundle=None)

        provider = SequenceProvider("bad json", self.valid)
        with patch.object(HumanAssessmentPublisher, "prepare", without_bundle):
            outcome, broker = self.state.application_reply(provider)
        self.assertEqual(outcome.status, "replied")
        self.assertEqual(outcome.approval_status, "approved")
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(retained[0].provider_calls, 2)
        self.assertEqual(retained[0].structural_retries, 1)
        self.assertEqual(self.state.reply_count, 1)
        self.assertEqual(broker.capabilities, ["issue_reply", "review_publish"])

    def test_public_application_exhausted_call_budget_never_writes(self):
        from review_sensei.hosting.github.application import GitHubApplication
        from review_sensei.hosting.github.human_assessment import (
            HumanAssessmentPublisher,
        )

        prepare = HumanAssessmentPublisher.prepare

        def without_bundle(assessor, **kwargs):
            prepared = prepare(assessor, **kwargs)
            return replace(prepared, evidence_bundle=None)

        generate = GitHubApplication.generate_and_publish_reply

        def limited(application, **kwargs):
            kwargs["budget"] = ResourceBudget.create(max_provider_calls=1)
            return generate(application, **kwargs)

        provider = SequenceProvider("bad json", self.valid)
        with (
            patch.object(HumanAssessmentPublisher, "prepare", without_bundle),
            patch.object(GitHubApplication, "generate_and_publish_reply", limited),
        ):
            with self.assertRaises(HumanAssessmentValidationError):
                self.state.application_reply(provider)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(self.state.events(), [])
        self.assertEqual(self.state.reply_count, 0)
