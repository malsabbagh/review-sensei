from __future__ import annotations

import io
import json
import unittest
from dataclasses import replace

from review_sensei.budgets import ProviderCapabilities, ReviewWorkBudgets
from review_sensei.errors import ProviderError, ReviewInputError
from review_sensei.execution import (
    OutputAccounting,
    execute_call,
    provider_work_identity,
)
from review_sensei.models import ProviderRequest, ProviderResponse, ReviewRequest
from review_sensei.outcomes import ResourceBudget, ResourceBudgetTracker
from review_sensei.providers.ollama import OllamaProvider
from review_sensei.service import ReviewService, _BudgetedProvider, _CallBudget
from review_sensei.stages import Stage
from review_sensei.validation import ReviewLimits

ENDPOINT = "https://ollama.example/api/generate"
CONTRACT = "bounded-complete-text-v1"
MODEL = "synthetic-model"
TEXT = '{"summary":"Complete synthetic review.","comments":[]}'
DIFF = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"


def complete_document(**changes):
    return {
        "response": TEXT,
        "model": MODEL,
        "done": True,
        "done_reason": "stop",
        "eval_count": 12,
        **changes,
    }


def provider(document, *, strict=False, output=128):
    calls = []

    def open_response(request, **kwargs):
        calls.append(request)
        return io.BytesIO(json.dumps(document).encode())

    options = {"require_completion_metadata": True} if strict else {}
    instance = OllamaProvider(
        base_url="https://ollama.example/api",
        model=MODEL,
        max_output_tokens=output,
        allow_model_override=False,
        opener=open_response,
        **options,
    )
    return instance, calls


def capability(**changes):
    return ProviderCapabilities(
        **{
            "context_tokens": 200000,
            "max_output_tokens": 128,
            "qualified": True,
            "provider_name": "ollama",
            "model": MODEL,
            "endpoint": ENDPOINT,
            "completion_contract": CONTRACT,
            **changes,
        }
    )


class OllamaCompletionGuardTests(unittest.TestCase):
    def test_observed_unsafe_metadata_refuses_even_when_review_json_is_complete(self):
        changes = [
            {"done": False},
            {"done": 1},
            {"done": None},
            {"done_reason": "length"},
            {"done_reason": "unknown"},
            {"done_reason": ["stop"]},
            {"done_reason": None},
            {"eval_count": 129},
            {"eval_count": True},
            {"eval_count": 12.0},
            {"eval_count": -1},
            {"eval_count": None},
        ]
        for change in changes:
            with self.subTest(change=change):
                instance, calls = provider(complete_document(**change))
                with self.assertRaises(ProviderError) as raised:
                    instance.complete(
                        ProviderRequest(prompt="synthetic", max_output_tokens=128)
                    )
                self.assertFalse(raised.exception.transient)
                self.assertEqual(len(calls), 1)

    def test_output_limit_is_intersection_of_constructor_and_request(self):
        for constructor, request in [(128, 32), (32, 128)]:
            instance, _ = provider(complete_document(eval_count=33), output=constructor)
            with self.assertRaises(ProviderError):
                instance.complete(
                    ProviderRequest(prompt="synthetic", max_output_tokens=request)
                )

    def test_strict_mode_requires_complete_metadata_and_exact_model(self):
        for key in ("done", "done_reason", "eval_count", "model"):
            doc = complete_document()
            del doc[key]
            with self.subTest(key=key):
                instance, _ = provider(doc, strict=True)
                with self.assertRaises(ProviderError):
                    instance.complete(ProviderRequest(prompt="synthetic"))
        for model in ("other-model", [MODEL], None):
            instance, _ = provider(complete_document(model=model), strict=True)
            with self.subTest(model=model), self.assertRaises(ProviderError):
                instance.complete(ProviderRequest(prompt="synthetic"))

    def test_strict_mode_returns_only_complete_bounded_response(self):
        instance, _ = provider(complete_document(eval_count=128), strict=True)
        self.assertEqual(instance.completion_contract, CONTRACT)
        response = instance.complete(ProviderRequest(prompt="synthetic"))
        self.assertEqual(response.text, TEXT)

    def test_legacy_missing_metadata_disposition_remains_explicit(self):
        instance, _ = provider({"response": TEXT})
        self.assertIsNone(instance.completion_contract)
        self.assertEqual(
            instance.complete(ProviderRequest(prompt="synthetic")).text, TEXT
        )

    def test_strict_configuration_and_runtime_mutations_refuse_before_transport(self):
        with self.assertRaises(ValueError):
            OllamaProvider(require_completion_metadata="yes")
        with self.assertRaises(ValueError):
            OllamaProvider(require_completion_metadata=True, allow_model_override=True)
        for attribute, value in [
            ("base_url", "https://other.example/api"),
            ("model", "other"),
        ]:
            instance, calls = provider(complete_document(), strict=True)
            setattr(instance, attribute, value)
            with self.subTest(attribute=attribute), self.assertRaises(ProviderError):
                instance.complete(ProviderRequest(prompt="synthetic"))
            self.assertEqual(calls, [])

    def test_strict_missing_output_limit_refuses_without_transport(self):
        instance, calls = provider(complete_document(), strict=True, output=None)
        with self.assertRaises(ProviderError):
            instance.complete(ProviderRequest(prompt="synthetic"))
        self.assertEqual(calls, [])
        for cap in (True, 1.5, 0, -1, 16385):
            with self.subTest(cap=cap), self.assertRaises(ValueError):
                OllamaProvider(max_output_tokens=cap)

    def test_strict_response_failures_are_sanitized_and_nontransient(self):
        instance, calls = provider(
            complete_document(done_reason="private-untrusted-response"), strict=True
        )
        with self.assertRaises(ProviderError) as raised:
            instance.complete(ProviderRequest(prompt="synthetic"))
        self.assertNotIn("private-untrusted-response", str(raised.exception))
        self.assertFalse(raised.exception.transient)
        self.assertEqual(len(calls), 1)

    def test_mutated_constructor_output_caps_refuse_before_transport(self):
        for cap in (-1, 0, True, 1.5, 16385, "128"):
            instance, calls = provider(complete_document(), strict=True)
            instance.max_output_tokens = cap
            with self.subTest(cap=cap), self.assertRaises(ProviderError):
                instance.complete(
                    ProviderRequest(prompt="synthetic", max_output_tokens=128)
                )
            self.assertEqual(calls, [])

    def test_callback_output_cap_mutation_stays_pending_without_transport(self):
        instance, calls = provider(complete_document(), strict=True)
        resource = ResourceBudget.create()
        tracker = ResourceBudgetTracker(resource)
        budgets = ReviewWorkBudgets(mode="unified").effective(
            limits=ReviewLimits(),
            resource=resource,
            mode="reassessment",
            capabilities=capability(),
        )
        result = execute_call(
            ProviderRequest(prompt="synthetic", max_output_tokens=128),
            provider=instance,
            validate=lambda response: response.text,
            tracker=tracker,
            budgets=budgets,
            before_dispatch=lambda digest: setattr(instance, "max_output_tokens", -1),
        )
        self.assertEqual(calls, [])
        self.assertEqual(result.diagnostic, "provider_failed")
        self.assertEqual(tracker.provider_calls, 1)

    def test_length_terminated_valid_json_stays_pending_in_real_review_service(self):
        instance, calls = provider(complete_document(done_reason="length"))
        stage = Stage(
            name="review",
            prompt_template="Review {diff}",
            outputs=("summary", "comments"),
        )
        service = ReviewService(
            instance, stages=(stage,), work_budgets=ReviewWorkBudgets(mode="unified")
        )
        run = service.run(
            ReviewRequest(diff=DIFF, base_sha="a" * 40, head_sha="b" * 40)
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(run.outcome.provider_calls, 1)
        self.assertIsNotNone(run.result)
        self.assertEqual(run.result.review_status, "partial")
        self.assertFalse(run.result.coverage.fully_reviewed)
        self.assertIn("provider_failed", run.result.summary)


class QualifiedEndpointGuardTests(unittest.TestCase):
    def test_qualified_record_requires_exact_endpoint_and_response_contract(self):
        for change in (
            {"endpoint": None},
            {"completion_contract": None},
            {"completion_contract": "unknown"},
        ):
            with self.subTest(change=change), self.assertRaises(ReviewInputError):
                capability(**change)
        for endpoint in [
            "https://user:secret@example.test/api",
            "https://example.test/api?secret=x",
            "https://example.test/api#fragment",
            "https://example.test/a/../b",
            "https://example.test\\other/api",
            "https://example.test/api\n",
            "file:///tmp/api",
        ]:
            with self.subTest(endpoint=endpoint), self.assertRaises(ReviewInputError):
                capability(endpoint=endpoint)

    def test_exact_endpoint_and_contract_match_without_inference(self):
        instance, calls = provider(complete_document(), strict=True)
        cap = capability()
        cap.require_provider(instance, model=MODEL)
        instance.base_url = "https://other.example/api"
        with self.assertRaises(ReviewInputError):
            cap.require_provider(instance, model=MODEL)
        self.assertEqual(calls, [])
        legacy, _ = provider(complete_document())
        with self.assertRaises(ReviewInputError):
            cap.require_provider(legacy, model=MODEL)

    def test_endpoint_getter_failure_is_sanitized(self):
        class Broken:
            name = "ollama"
            model = MODEL

            @property
            def endpoint(self):
                raise RuntimeError("private-secret")

        with self.assertRaises(ReviewInputError) as raised:
            capability().require_provider(Broken(), model=MODEL)
        self.assertNotIn("private-secret", str(raised.exception))

    def test_discovery_checks_endpoint_before_any_provider_call(self):
        instance, calls = provider(complete_document(), strict=True)
        instance.base_url = "https://other.example/api"
        stage = Stage(
            name="review",
            prompt_template="Review {diff}",
            outputs=("summary", "comments"),
        )
        service = ReviewService(
            instance,
            stages=(stage,),
            work_budgets=ReviewWorkBudgets(mode="unified"),
            capabilities=capability(),
        )
        with self.assertRaises(ReviewInputError):
            service.run(ReviewRequest(diff=DIFF, base_sha="a" * 40, head_sha="b" * 40))
        self.assertEqual(calls, [])

    def test_execute_call_checks_binding_before_original_charge(self):
        instance, calls = provider(complete_document(), strict=True)
        resource = ResourceBudget.create()
        tracker = ResourceBudgetTracker(resource)
        budgets = ReviewWorkBudgets(mode="unified").effective(
            limits=ReviewLimits(),
            resource=resource,
            mode="reassessment",
            capabilities=capability(),
        )
        instance.base_url = "https://other.example/api"
        with self.assertRaises(ReviewInputError):
            execute_call(
                ProviderRequest(prompt="synthetic"),
                provider=instance,
                validate=lambda response: response.text,
                tracker=tracker,
                budgets=budgets,
            )
        self.assertEqual(calls, [])
        self.assertEqual(tracker.provider_calls, 0)

    def test_callback_endpoint_change_withholds_dispatch_without_refunding_charge(self):
        class DeclaredAdapter:
            name = "ollama"
            model = MODEL
            endpoint = ENDPOINT
            completion_contract = CONTRACT
            calls = 0

            def complete(self, request):
                self.calls += 1
                return ProviderResponse(text=TEXT)

        instance = DeclaredAdapter()
        resource = ResourceBudget.create()
        tracker = ResourceBudgetTracker(resource)
        accounting = OutputAccounting()
        budgets = ReviewWorkBudgets(mode="unified").effective(
            limits=ReviewLimits(),
            resource=resource,
            mode="reassessment",
            capabilities=capability(),
        )

        def callback(digest):
            self.assertEqual(tracker.provider_calls, 1)
            instance.endpoint = "https://other.example/api/generate"

        result = execute_call(
            ProviderRequest(prompt="synthetic"),
            provider=instance,
            validate=lambda response: response.text,
            tracker=tracker,
            budgets=budgets,
            before_dispatch=callback,
            output_accounting=accounting,
        )
        self.assertEqual(instance.calls, 0)
        self.assertIsNone(result.value)
        self.assertEqual(result.diagnostic, "provider_identity_changed")
        self.assertEqual(tracker.provider_calls, 1)
        self.assertEqual(tracker.response_bytes, 0)
        self.assertEqual(accounting.unknown_response_bytes, budgets.batch_output_bytes)

    def test_budgeted_wrapper_reads_underlying_identity_before_and_after_callback(self):
        class DeclaredAdapter:
            name = "ollama"
            model = MODEL
            endpoint = ENDPOINT
            completion_contract = CONTRACT
            allow_model_override = False
            calls = 0

            def complete(self, request):
                self.calls += 1
                return ProviderResponse(text=TEXT)

        for field, changed in (
            ("model", "changed-model"),
            ("name", "changed-provider"),
        ):
            for moment in ("initial", "callback"):
                with self.subTest(field=field, moment=moment):
                    instance = DeclaredAdapter()
                    wrapped = _BudgetedProvider(instance, budget=_CallBudget(3))
                    resource = ResourceBudget.create()
                    tracker = ResourceBudgetTracker(resource)
                    budgets = ReviewWorkBudgets(mode="unified").effective(
                        limits=ReviewLimits(),
                        resource=resource,
                        mode="reassessment",
                        capabilities=capability(),
                    )
                    if moment == "initial":
                        setattr(instance, field, changed)
                        with self.assertRaises(ReviewInputError):
                            execute_call(
                                ProviderRequest(prompt="synthetic"),
                                provider=wrapped,
                                validate=lambda response: response.text,
                                tracker=tracker,
                                budgets=budgets,
                            )
                        self.assertEqual(tracker.provider_calls, 0)
                    else:
                        result = execute_call(
                            ProviderRequest(prompt="synthetic"),
                            provider=wrapped,
                            validate=lambda response: response.text,
                            tracker=tracker,
                            budgets=budgets,
                            before_dispatch=lambda digest: setattr(
                                instance, field, changed
                            ),
                        )
                        self.assertEqual(result.diagnostic, "provider_identity_changed")
                        self.assertEqual(tracker.provider_calls, 1)
                    self.assertEqual(instance.calls, 0)

    def test_strict_contract_and_endpoint_are_bound_into_work_identity(self):
        instance, _ = provider(complete_document(), strict=True)
        legacy, _ = provider(complete_document())
        self.assertNotEqual(
            provider_work_identity(instance), provider_work_identity(legacy)
        )
        self.assertNotEqual(
            capability().endpoint,
            replace(
                capability(), endpoint="https://other.example/api/generate"
            ).endpoint,
        )

    def test_false_candidate_keeps_legacy_limit_and_does_not_adopt(self):
        cap = capability(qualified=False)
        budgets = ReviewWorkBudgets(mode="unified").effective(
            limits=ReviewLimits(),
            resource=ResourceBudget.create(),
            mode="discovery",
            capabilities=cap,
        )
        self.assertEqual(budgets.batch_prompt_bytes, 49152)
        self.assertFalse(budgets.provider_qualified)
