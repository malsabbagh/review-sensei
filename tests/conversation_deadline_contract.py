"""Shared ordinary-discussion budget contract, using no live providers."""

import io
import json
import unittest

from review_sensei.conversation import ConversationService
from review_sensei.errors import ProviderError, ReviewFormatError, ReviewInputError
from review_sensei.models import ConversationContext, ProviderResponse
from review_sensei.outcomes import ResourceBudget, ResourceBudgetTracker
from review_sensei.providers.ollama import OllamaProvider
from review_sensei.providers.openai_compatible import OpenAICompatibleProvider
from review_sensei.providers.openrouter import (
    OpenRouterProvider,
    OpenRouterRoutingPolicy,
)


class Clock:
    value = 0.0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class Provider:
    name = "synthetic"
    model = "synthetic"
    max_output_tokens = 256

    def __init__(self, actions):
        self.actions = iter(actions)
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        value = next(self.actions)
        if isinstance(value, BaseException):
            raise value
        if callable(value):
            value = value(request)
        return ProviderResponse(value, self.name, model=self.model)


def tracker(clock, **overrides):
    return ResourceBudgetTracker(
        ResourceBudget.create(timeout_ms=1000, **overrides),
        monotonic=clock,
        sleeper=clock.sleep,
    )


class ConversationDeadlineContractTests(unittest.TestCase):
    def test_default_public_reply_receives_a_finite_request_deadline(self):
        provider = Provider(['{"body":"ok"}'])
        self.assertEqual(
            ConversationService(provider).reply(ConversationContext()).body, "ok"
        )
        self.assertIsInstance(provider.requests[0].timeout_seconds, float)
        self.assertGreater(provider.requests[0].timeout_seconds, 0)
        self.assertLessEqual(provider.requests[0].timeout_seconds, 120)

    def test_three_real_adapters_share_deadline_and_limits_across_correction(self):
        for kind in ("ollama", "openai-compatible", "openrouter"):
            with self.subTest(kind=kind):
                clock = Clock()
                calls = []

                def opener(request, timeout, context):
                    payload = json.loads(request.data)
                    calls.append((timeout, payload))
                    clock.value += 0.4
                    content = "not json" if len(calls) == 1 else '{"body":"ok"}'
                    body = (
                        {"response": content}
                        if kind == "ollama"
                        else {"choices": [{"message": {"content": content}}]}
                    )
                    return io.BytesIO(json.dumps(body).encode())

                common = dict(
                    model="synthetic/model" if kind == "openrouter" else "synthetic",
                    api_key="synthetic",
                    timeout_seconds=900,
                    max_output_tokens=256,
                )
                if kind == "ollama":
                    provider = OllamaProvider(opener=opener, **common)
                elif kind == "openai-compatible":
                    provider = OpenAICompatibleProvider(opener=opener, **common)
                else:
                    provider = OpenRouterProvider(
                        routing_policy=OpenRouterRoutingPolicy("morph"),
                        _test_opener=opener,
                        **common,
                    )
                budget = tracker(clock)
                reply = ConversationService(provider).reply(
                    ConversationContext(), tracker=budget
                )
                self.assertEqual(reply.body, "ok")
                self.assertEqual(len(calls), 2)
                self.assertAlmostEqual(calls[0][0], 1.0)
                self.assertAlmostEqual(calls[1][0], 0.6)
                self.assertEqual(budget.provider_calls, 2)
                self.assertEqual(budget.structural_retries, 1)
                for _, payload in calls:
                    self.assertEqual(
                        payload["options"]["num_predict"]
                        if kind == "ollama"
                        else payload["max_tokens"],
                        256,
                    )

    def test_expired_and_exhausted_budgets_never_dispatch(self):
        for kwargs, elapsed, reason in (
            ({}, 1.1, "deadline_exceeded"),
            ({"max_provider_calls": 0}, 0, "provider_call_limit"),
            ({"max_output_bytes": 0}, 0, "output_budget"),
            ({"max_prompt_bytes": 0}, 0, "prompt_budget"),
        ):
            with self.subTest(reason=reason):
                clock = Clock()
                budget = tracker(clock, **kwargs)
                clock.value = elapsed
                provider = Provider(['{"body":"must not run"}'])
                with self.assertRaises(ReviewInputError) as raised:
                    ConversationService(provider).reply(
                        ConversationContext(), tracker=budget
                    )
                self.assertEqual(raised.exception.diagnostic, reason)
                self.assertEqual(provider.requests, [])

    def test_correction_cannot_refresh_call_or_output_allowance(self):
        for settings, response, reason in (
            ({"max_provider_calls": 1}, "{", "provider_call_limit"),
            ({"max_output_bytes": 8}, "not json", "output_budget"),
        ):
            with self.subTest(reason=reason):
                budget = tracker(Clock(), **settings)
                provider = Provider([response, '{"body":"must not run"}'])
                with self.assertRaises(ReviewInputError) as raised:
                    ConversationService(provider).reply(
                        ConversationContext(), tracker=budget
                    )
                self.assertEqual(raised.exception.diagnostic, reason)
                self.assertEqual(len(provider.requests), 1)
                self.assertEqual(budget.provider_calls, 1)
                self.assertEqual(budget.response_bytes, len(response))

    def test_correction_cannot_refresh_aggregate_prompt_allowance(self):
        probe = Provider(['{"body":"ok"}'])
        ConversationService(probe).reply(ConversationContext())
        size = len(probe.requests[0].prompt.encode("utf-8"))
        budget = tracker(Clock(), max_prompt_bytes=size + 1)
        provider = Provider(["{", '{"body":"must not run"}'])
        with self.assertRaises(ReviewInputError) as raised:
            ConversationService(provider).reply(ConversationContext(), tracker=budget)
        self.assertEqual(raised.exception.diagnostic, "prompt_budget")
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(budget.prompt_bytes, size)

    def test_correction_without_append_space_keeps_original_prompt_bounded(self):
        probe = Provider(['{"body":"ok"}'])
        ConversationService(probe).reply(ConversationContext())
        size = len(probe.requests[0].prompt.encode("utf-8"))
        provider = Provider(["{", '{"body":"ok"}'])
        reply = ConversationService(provider).reply(
            ConversationContext(), max_prompt_bytes=size
        )
        self.assertEqual(reply.body, "ok")
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(provider.requests[0].prompt, provider.requests[1].prompt)

    def test_transport_retry_and_correction_share_one_original_deadline(self):
        clock = Clock()
        budget = tracker(clock)

        def transient(request):
            clock.value += 0.2
            raise ProviderError(
                "private provider error", transient=True, retry_after_seconds=0.3
            )

        def malformed(request):
            clock.value += 0.2
            return "{"

        provider = Provider([transient, malformed, '{"body":"ok"}'])
        self.assertEqual(
            ConversationService(provider)
            .reply(ConversationContext(), tracker=budget)
            .body,
            "ok",
        )
        self.assertEqual(
            [round(request.timeout_seconds, 3) for request in provider.requests],
            [1.0, 0.5, 0.3],
        )
        self.assertEqual(budget.provider_calls, 3)
        self.assertEqual(budget.transport_retries, 1)
        self.assertEqual(budget.structural_retries, 1)

    def test_response_after_deadline_cannot_publish_or_retry(self):
        clock = Clock()

        def late(request):
            clock.value = 1.1
            return '{"body":"too late"}'

        provider = Provider([late])
        with self.assertRaises(ReviewInputError) as raised:
            ConversationService(provider).reply(
                ConversationContext(), tracker=tracker(clock)
            )
        self.assertEqual(raised.exception.diagnostic, "deadline_exceeded")
        self.assertEqual(len(provider.requests), 1)

    def test_semantic_reply_failure_never_requests_structural_correction(self):
        budget = tracker(Clock())
        provider = Provider(['{"body":3}', '{"body":"must not run"}'])
        with self.assertRaisesRegex(ReviewFormatError, "failed validation"):
            ConversationService(provider).reply(ConversationContext(), tracker=budget)
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(budget.structural_retries, 0)

    def test_cancellation_propagates_without_retry_and_preserves_attempt(self):
        budget = tracker(Clock())
        provider = Provider([KeyboardInterrupt()])
        with self.assertRaises(KeyboardInterrupt):
            ConversationService(provider).reply(ConversationContext(), tracker=budget)
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(budget.provider_calls, 1)
        self.assertEqual(budget.transport_retries, 0)

    def test_provider_failure_is_sanitized(self):
        budget = tracker(Clock())
        provider = Provider([RuntimeError("private provider sentinel")])
        with self.assertRaises(ProviderError) as raised:
            ConversationService(provider).reply(ConversationContext(), tracker=budget)
        self.assertNotIn("private", str(raised.exception))

    def test_parser_safety_failure_is_sanitized_and_bounded(self):
        nested = "[" * 1500 + "]" * 1500
        provider = Provider([nested, nested])
        budget = tracker(Clock())
        with self.assertRaises(ReviewFormatError):
            ConversationService(provider).reply(ConversationContext(), tracker=budget)
        self.assertLessEqual(len(provider.requests), 2)
        self.assertLessEqual(budget.structural_retries, 1)
