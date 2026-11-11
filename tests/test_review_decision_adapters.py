"""Decision wire contracts through synthetic transports; no provider access."""

import io
import json
import socket
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler

from review_sensei.decision_configuration import DecisionConfiguration
from review_sensei.decision_execution import DecisionExecution
from review_sensei.errors import ProviderError, ReviewInputError
from review_sensei.outcomes import ResourceBudget, ResourceBudgetTracker
from review_sensei.providers import decision
from review_sensei.providers.decision import (
    OllamaDecisionProvider,
    OpenRouterDecisionProvider,
    default_decision_registry,
)
from review_sensei.review_decision_contract import (
    ReviewDecisionQuestion,
    ReviewDecisionRequest,
)


def settings(provider="ollama", **overrides):
    values = {"enabled": True, "provider": provider, "model": "local-small:1"}
    if provider == "openrouter":
        values.update(
            model="typesafe/jev-1.13",
            allow_remote=True,
            api_key_env="DECISION_TEST_KEY",
            upstream_provider="typesafe",
        )
    values.update(overrides)
    return DecisionConfiguration(**values)


def request_for(configuration):
    return ReviewDecisionRequest(
        state=json.dumps({"candidate_id": "c1", "claim": "synthetic evidence"}),
        questions=(
            ReviewDecisionQuestion(
                "concern",
                "c1",
                "Classify the supplied concern.",
                ("behavioral_defect", "uncertain"),
            ),
        ),
        provider=configuration.provider,
        model=configuration.model,
    )


def response_for(configuration):
    if configuration.provider == "ollama":
        return {
            "model": configuration.model,
            "done": True,
            "done_reason": "stop",
            "response": json.dumps({"concern": "uncertain"}),
            "prompt_eval_count": 23,
            "eval_count": 7,
        }
    return {
        "model": "typesafe/jev-1.13-20260917",
        "provider": "TypeSafe",
        "answers": {
            "concern": {
                "type": "choice",
                "choice": "uncertain",
                "probabilities": {"behavioral_defect": 0.25, "uncertain": 0.75},
                "confidence": 0.6,
            }
        },
        "usage": {"input_tokens": 23, "output_tokens": 7},
    }


class Wire:
    def __init__(self, document=None, *, raw=None, error=None, after_open=None):
        self.raw = raw if raw is not None else json.dumps(document).encode()
        self.error = error
        self.after_open = after_open
        self.requests = []

    def __call__(self, request, *, timeout):
        self.requests.append((request, timeout))
        if self.after_open:
            self.after_open()
        if self.error is not None:
            raise self.error
        response = io.BytesIO(self.raw)
        response.status = 200
        return response


class ReviewDecisionAdapterTests(unittest.TestCase):
    def test_positional_execution_limits_keep_their_original_meaning(self):
        tracker = ResourceBudgetTracker(ResourceBudget.create())
        execution = DecisionExecution(tracker, settings(), 8, 0, 0, 0, 0, 0, 2, 7)
        dispatches = []

        def send(timeout, maximum):
            dispatches.append(maximum)
            return "1234567"

        with self.assertRaises(ProviderError):
            execution.execute("abc", send)
        self.assertEqual(dispatches, [])
        self.assertEqual(execution.execute("a", send), "1234567")
        with self.assertRaises(ProviderError):
            execution.execute("a", send)
        self.assertEqual(dispatches, [7])
        self.assertEqual(tracker.provider_calls, 1)

    def test_tls_setup_failure_is_bounded_unavailable_work(self):
        configuration = settings("openrouter")
        tracker = ResourceBudgetTracker(ResourceBudget.create())
        execution = DecisionExecution(tracker, configuration, 8)
        provider = OpenRouterDecisionProvider(configuration, key="synthetic-key")
        with (
            patch.object(
                decision.ssl,
                "create_default_context",
                side_effect=FileNotFoundError("synthetic private path"),
            ),
            patch.object(decision, "build_opener") as opener,
        ):
            with self.assertRaisesRegex(
                ProviderError, "TLS trust store unavailable"
            ) as caught:
                provider.evaluate(request_for(configuration), execution)
        opener.assert_not_called()
        self.assertNotIn("private path", str(caught.exception))
        self.assertEqual(tracker.provider_calls, 1)
        self.assertGreater(execution.output_reserved, 0)

    def setUp(self):
        self.enterContext(
            patch.object(
                socket,
                "create_connection",
                side_effect=AssertionError("live network forbidden"),
            )
        )

    def execution(self, configuration, *, tracker=None, total_call_limit=8):
        return DecisionExecution(
            tracker if tracker is not None else ResourceBudgetTracker(ResourceBudget()),
            configuration,
            total_call_limit,
        )

    def adapter(self, configuration, wire):
        if configuration.provider == "ollama":
            return OllamaDecisionProvider(configuration, opener=wire)
        return OpenRouterDecisionProvider(
            configuration, key="synthetic-key", opener=wire
        )

    def test_ollama_uses_local_generate_and_exact_choice_schema(self):
        configuration = settings()
        request = request_for(configuration)
        wire = Wire(response_for(configuration))
        execution = self.execution(configuration)
        result = self.adapter(configuration, wire).evaluate(request, execution)
        sent, timeout = wire.requests[0]
        self.assertEqual(sent.full_url, "http://127.0.0.1:11434/api/generate")
        self.assertEqual(sent.get_method(), "POST")
        self.assertIsNone(sent.get_header("Authorization"))
        payload = json.loads(sent.data)
        self.assertEqual(payload["model"], configuration.model)
        self.assertIs(payload["stream"], False)
        self.assertEqual(
            payload["format"],
            {
                "type": "object",
                "properties": {
                    "concern": {
                        "type": "string",
                        "enum": ["behavioral_defect", "uncertain"],
                    }
                },
                "required": ["concern"],
                "additionalProperties": False,
            },
        )
        prompt = json.loads(payload["prompt"])
        self.assertEqual(prompt["untrusted_state"], request.state)
        self.assertEqual(prompt["questions"], [q.to_dict() for q in request.questions])
        self.assertEqual(payload["options"]["temperature"], 0)
        self.assertGreater(payload["options"]["num_predict"], 0)
        self.assertLessEqual(payload["options"]["num_predict"], 1024)
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, configuration.timeout_ms / 1000)
        result.validate(request)
        self.assertEqual((result.input_tokens, result.output_tokens), (23, 7))
        self.assertIsNone(result.answers[0].probabilities)
        self.assertIsNone(result.answers[0].confidence)
        self.assertEqual(execution.tracker.provider_calls, 1)

    def test_systemone_native_questions_routing_and_observed_identity(self):
        configuration = settings("openrouter")
        request = request_for(configuration)
        wire = Wire(response_for(configuration))
        result = self.adapter(configuration, wire).evaluate(
            request, self.execution(configuration)
        )
        sent, _ = wire.requests[0]
        self.assertEqual(sent.full_url, "https://openrouter.ai/api/v1/systemone")
        self.assertEqual(sent.get_method(), "POST")
        self.assertEqual(sent.get_header("Authorization"), "Bearer synthetic-key")
        payload = json.loads(sent.data)
        self.assertEqual(set(payload), {"model", "state", "questions", "provider"})
        self.assertEqual(payload["state"], json.loads(request.state))
        question = payload["questions"]["concern"]
        self.assertEqual(question["type"], "choice")
        self.assertIn(request.questions[0].description, question["instructions"])
        self.assertEqual(set(question["criteria"]), set(request.questions[0].choices))
        self.assertTrue(
            all(isinstance(v, str) and v for v in question["criteria"].values())
        )
        self.assertEqual(
            payload["provider"],
            {
                "order": ["typesafe"],
                "allow_fallbacks": False,
                "require_parameters": True,
                "data_collection": "deny",
                "zdr": True,
            },
        )
        result.validate(request)
        self.assertEqual(result.request_sha256, request.digest())
        self.assertEqual(result.requested_model, "typesafe/jev-1.13")
        self.assertEqual(result.actual_model, "typesafe/jev-1.13-20260917")
        self.assertEqual(result.actual_provider, "TypeSafe")
        self.assertEqual(
            dict(result.answers[0].probabilities),
            {"behavioral_defect": 0.25, "uncertain": 0.75},
        )
        self.assertEqual(result.answers[0].confidence, 0.6)
        self.assertEqual((result.input_tokens, result.output_tokens), (23, 7))

    def test_optional_metadata_remains_unknown_without_invented_probabilities(self):
        for provider in ("ollama", "openrouter"):
            with self.subTest(provider=provider):
                configuration = settings(provider)
                document = response_for(configuration)
                if provider == "ollama":
                    del document["prompt_eval_count"]
                    del document["eval_count"]
                else:
                    document["answers"]["concern"] = {
                        "type": "choice",
                        "choice": "uncertain",
                    }
                    document["usage"] = {}
                    del document["provider"]
                result = self.adapter(configuration, Wire(document)).evaluate(
                    request_for(configuration), self.execution(configuration)
                )
                self.assertIsNone(result.input_tokens)
                self.assertIsNone(result.output_tokens)
                self.assertIsNone(result.answers[0].probabilities)
                self.assertIsNone(result.answers[0].confidence)

    def test_request_binding_refuses_before_dispatch(self):
        for provider in ("ollama", "openrouter"):
            configuration = settings(provider)
            for changes in ({"provider": "other"}, {"model": "other-model"}):
                with self.subTest(provider=provider, changes=changes):
                    wire = Wire(response_for(configuration))
                    execution = self.execution(configuration)
                    with self.assertRaises(ReviewInputError):
                        self.adapter(configuration, wire).evaluate(
                            replace(request_for(configuration), **changes), execution
                        )
                    self.assertEqual(wire.requests, [])
                    self.assertEqual(execution.tracker.provider_calls, 0)

    def test_systemone_invalid_answers_and_distributions_remain_charged(self):
        configuration = settings("openrouter")
        answers = [
            {},
            {"unknown": {"type": "choice", "choice": "uncertain"}},
            {"concern": {"type": "refusal"}},
            {"concern": {"type": "choice", "choice": "unsupported"}},
        ]
        for probabilities in (
            {"behavioral_defect": 10**400, "uncertain": 0.5},
            [],
            {"uncertain": 1},
            {"other": 0.5, "uncertain": 0.5},
            {"behavioral_defect": 0.25, "uncertain": 0.25},
            {"behavioral_defect": True, "uncertain": 0},
            {"behavioral_defect": -0.1, "uncertain": 1.1},
            {"behavioral_defect": float("nan"), "uncertain": 0.5},
            {"behavioral_defect": float("inf"), "uncertain": 0.5},
        ):
            answers.append(
                {
                    "concern": {
                        "type": "choice",
                        "choice": "uncertain",
                        "probabilities": probabilities,
                    }
                }
            )
        for confidence in (True, -0.1, 1.1, "0.5", 10**400):
            answers.append(
                {
                    "concern": {
                        "type": "choice",
                        "choice": "uncertain",
                        "confidence": confidence,
                    }
                }
            )
        for index, answer in enumerate(answers):
            with self.subTest(case=index):
                document = response_for(configuration)
                document["answers"] = answer
                wire = Wire(document)
                execution = self.execution(configuration)
                with self.assertRaises((ReviewInputError, ProviderError)):
                    self.adapter(configuration, wire).evaluate(
                        request_for(configuration), execution
                    )
                self.assertEqual(len(wire.requests), 1)
                self.assertEqual(execution.tracker.provider_calls, 1)

    def test_invalid_usage_never_becomes_zero(self):
        for provider in ("ollama", "openrouter"):
            configuration = settings(provider)
            for invalid in (True, -1, 1.5, "7", 9_007_199_254_740_992):
                with self.subTest(provider=provider, invalid=invalid):
                    document = response_for(configuration)
                    if provider == "ollama":
                        document["eval_count"] = invalid
                    else:
                        document["usage"]["output_tokens"] = invalid
                    execution = self.execution(configuration)
                    with self.assertRaises(ReviewInputError):
                        self.adapter(configuration, Wire(document)).evaluate(
                            request_for(configuration), execution
                        )
                    self.assertEqual(execution.tracker.provider_calls, 1)

    def test_ollama_incomplete_or_wrong_model_and_inner_answers_refuse(self):
        configuration = settings()
        for changes in (
            {"done": False},
            {"done_reason": "length"},
            {"model": "other-model"},
            {"response": "{}"},
            {"response": '{"concern":"unsupported"}'},
            {"response": '{"concern":"uncertain","extra":"uncertain"}'},
            {"response": '{"concern":"uncertain","concern":"behavioral_defect"}'},
            {"response": "not json"},
            {"eval_count": 1025},
        ):
            with self.subTest(changes=changes):
                document = response_for(configuration)
                document.update(changes)
                execution = self.execution(configuration)
                with self.assertRaises((ReviewInputError, ProviderError)):
                    self.adapter(configuration, Wire(document)).evaluate(
                        request_for(configuration), execution
                    )
                self.assertEqual(execution.tracker.provider_calls, 1)

    def test_response_cannot_be_rebound_to_another_packet(self):
        configuration = settings("openrouter")
        request = request_for(configuration)
        result = self.adapter(
            configuration, Wire(response_for(configuration))
        ).evaluate(request, self.execution(configuration))
        for changed in (
            replace(request, state='{"candidate_id":"c2"}'),
            replace(request, model="other-model"),
        ):
            with self.subTest(changed=changed):
                with self.assertRaises(ReviewInputError):
                    result.validate(changed)

    def test_invalid_outer_json_and_utf8_remain_single_attempt(self):
        configuration = settings("openrouter")
        for raw in (
            b"not json",
            b"\xff",
            b'{"answers":{},"answers":{}}',
            b'{"model":NaN}',
        ):
            with self.subTest(raw=raw):
                wire = Wire(raw=raw)
                execution = self.execution(configuration)
                with self.assertRaises((ReviewInputError, ProviderError)):
                    self.adapter(configuration, wire).evaluate(
                        request_for(configuration), execution
                    )
                self.assertEqual(len(wire.requests), 1)
                self.assertEqual(execution.tracker.provider_calls, 1)

    def test_configuration_rejects_egress_credentials_and_unknown_controls(self):
        for overrides in (
            {"base_url": "http://localhost:11434/api"},
            {"base_url": "https://ollama.com/api"},
            {"base_url": "http://192.0.2.1:11434/api"},
            {"base_url": "http://127.0.0.1:11434/api?target=remote"},
            {"api_key_env": "UNRELATED_KEY"},
            {"model": "model:cloud"},
            {"model": "gpt-oss:120b-cloud"},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ReviewInputError):
                settings(**overrides)
        for overrides in (
            {"allow_remote": False},
            {"api_key_env": None},
            {"upstream_provider": None},
            {"base_url": "https://other.example/api/v1"},
            {"base_url": "http://openrouter.ai/api/v1"},
            {"base_url": "https://openrouter.ai:444/api/v1"},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ReviewInputError):
                settings("openrouter", **overrides)
        with self.assertRaises(ReviewInputError):
            DecisionConfiguration.from_dict(
                {
                    **settings("openrouter").to_dict(),
                    "required_privacy_control": "unsupported",
                }
            )

    def test_adapter_constructor_preserves_provider_and_local_credential_scope(self):
        for factory in (
            lambda: OllamaDecisionProvider(settings("openrouter"), opener=Wire()),
            lambda: OpenRouterDecisionProvider(
                settings(), key="synthetic-key", opener=Wire()
            ),
            lambda: OllamaDecisionProvider(
                settings(), key="synthetic-key", opener=Wire()
            ),
        ):
            with (
                self.subTest(factory=factory),
                self.assertRaises((ReviewInputError, TypeError)),
            ):
                factory()

    def test_openrouter_uses_only_explicit_decision_credential(self):
        configuration = settings("openrouter")
        for key in (None, "", "has space", "line\nbreak", "é", "x" * 4097):
            with (
                self.subTest(key_length=len(key) if key else 0),
                self.assertRaises(ReviewInputError),
            ):
                OpenRouterDecisionProvider(configuration, key=key, opener=Wire())
        registry = default_decision_registry()
        with self.assertRaises(ReviewInputError):
            registry.create(
                configuration,
                {"OPENROUTER_API_KEY": "unrelated", "OLLAMA_API_KEY": "unrelated"},
            )
        self.assertIsNone(registry.create(DecisionConfiguration(), {}))

    def test_default_local_transport_disables_environment_proxies(self):
        configuration = settings()
        wire = Wire(response_for(configuration))
        captured = []

        def build(*handlers):
            captured.extend(handlers)
            return SimpleNamespace(open=wire)

        with patch.object(decision, "build_opener", side_effect=build):
            OllamaDecisionProvider(configuration).evaluate(
                request_for(configuration), self.execution(configuration)
            )
        proxies = [h for h in captured if isinstance(h, ProxyHandler)]
        self.assertEqual(len(proxies), 1)
        self.assertEqual(proxies[0].proxies, {})
        self.assertEqual(len(wire.requests), 1)

    def test_default_transport_redirect_refusal_retains_attempt(self):
        configuration = settings()
        dispatches = []

        def build(*handlers):
            redirect = next(h for h in handlers if isinstance(h, decision._NoRedirect))

            def send(request, *, timeout):
                dispatches.append(request.full_url)
                return redirect.redirect_request(
                    request, None, 302, "redirect", {}, "https://remote.example/"
                )

            return SimpleNamespace(open=send)

        execution = self.execution(configuration)
        with (
            patch.object(decision, "build_opener", side_effect=build),
            self.assertRaises(ProviderError),
        ):
            OllamaDecisionProvider(configuration).evaluate(
                request_for(configuration), execution
            )
        self.assertEqual(dispatches, ["http://127.0.0.1:11434/api/generate"])
        self.assertEqual(execution.tracker.provider_calls, 1)
        self.assertEqual(execution.output_reserved, 8192)

    def test_transport_failures_never_retry_or_refund_unknown_output(self):
        configuration = settings("openrouter", max_output_bytes=8192)
        for error in (
            TimeoutError("synthetic timeout"),
            URLError("synthetic failure"),
            HTTPError(
                "https://openrouter.ai/api/v1/systemone", 429, "synthetic", {}, None
            ),
        ):
            with self.subTest(error=type(error).__name__):
                wire = Wire(error=error)
                execution = self.execution(configuration)
                adapter = self.adapter(configuration, wire)
                with self.assertRaises(ProviderError):
                    adapter.evaluate(request_for(configuration), execution)
                self.assertEqual(execution.output_reserved, 8192)
                with self.assertRaises(ProviderError):
                    adapter.evaluate(request_for(configuration), execution)
                self.assertEqual(len(wire.requests), 1)
                self.assertEqual(execution.tracker.provider_calls, 1)
                self.assertEqual(execution.tracker.transport_retries, 0)

    def test_response_byte_limit_accepts_exact_and_rejects_one_extra(self):
        configuration = settings()
        raw = json.dumps(response_for(configuration)).encode()
        for extra in (b"", b" "):
            with self.subTest(extra=extra):
                bounded = replace(configuration, max_output_bytes=len(raw))
                execution = self.execution(bounded)
                adapter = self.adapter(bounded, Wire(raw=raw + extra))
                if extra:
                    with self.assertRaises(ProviderError):
                        adapter.evaluate(request_for(bounded), execution)
                else:
                    adapter.evaluate(request_for(bounded), execution)
                    self.assertEqual(execution.output_bytes, len(raw))
                self.assertEqual(execution.tracker.provider_calls, 1)

    def test_decision_calls_share_original_review_call_allowance(self):
        configuration = settings()
        tracker = ResourceBudgetTracker(ResourceBudget(max_provider_calls=4))
        tracker.record_call("already performed review work")
        execution = self.execution(configuration, tracker=tracker, total_call_limit=2)
        wire = Wire(response_for(configuration))
        adapter = self.adapter(configuration, wire)
        adapter.evaluate(request_for(configuration), execution)
        with self.assertRaises(ProviderError):
            adapter.evaluate(request_for(configuration), execution)
        self.assertIs(execution.tracker, tracker)
        self.assertEqual(tracker.provider_calls, 2)
        self.assertEqual(len(wire.requests), 1)

    def test_original_tracker_and_subordinate_call_limits_both_apply(self):
        for original_limit, decision_limit in ((1, 4), (4, 1)):
            with self.subTest(original=original_limit, decision=decision_limit):
                configuration = settings(max_calls=decision_limit)
                tracker = ResourceBudgetTracker(
                    ResourceBudget(max_provider_calls=original_limit)
                )
                execution = self.execution(configuration, tracker=tracker)
                wire = Wire(response_for(configuration))
                adapter = self.adapter(configuration, wire)
                adapter.evaluate(request_for(configuration), execution)
                with self.assertRaises(ProviderError):
                    adapter.evaluate(request_for(configuration), execution)
                self.assertEqual(len(wire.requests), 1)

    def test_input_and_output_limits_accumulate_across_decision_batches(self):
        configuration = settings()
        probe = Wire(response_for(configuration))
        self.adapter(configuration, probe).evaluate(
            request_for(configuration), self.execution(configuration)
        )
        payload_bytes = len(probe.requests[0][0].data)
        output_bytes = len(probe.raw)
        for changes in (
            {"max_input_bytes": payload_bytes * 2},
            {"max_output_bytes": output_bytes * 2},
        ):
            with self.subTest(changes=changes):
                bounded = replace(configuration, **changes)
                wire = Wire(response_for(bounded))
                execution = self.execution(bounded)
                adapter = self.adapter(bounded, wire)
                for _ in range(2):
                    adapter.evaluate(request_for(bounded), execution)
                with self.assertRaises(ProviderError):
                    adapter.evaluate(request_for(bounded), execution)
                self.assertEqual(len(wire.requests), 2)
                self.assertEqual(execution.tracker.provider_calls, 2)

    def test_original_deadline_caps_transport_and_late_response_cannot_succeed(self):
        for late in (False, True):
            with self.subTest(late=late):
                clock = [0.0]
                configuration = settings(timeout_ms=5000)
                tracker = ResourceBudgetTracker(
                    ResourceBudget(timeout_ms=1000), monotonic=lambda: clock[0]
                )
                execution = self.execution(configuration, tracker=tracker)
                clock[0] = 0.8
                wire = Wire(
                    response_for(configuration),
                    after_open=(lambda: clock.__setitem__(0, 1.1)) if late else None,
                )
                if late:
                    with self.assertRaises(ProviderError):
                        self.adapter(configuration, wire).evaluate(
                            request_for(configuration), execution
                        )
                else:
                    self.adapter(configuration, wire).evaluate(
                        request_for(configuration), execution
                    )
                self.assertAlmostEqual(wire.requests[0][1], 0.2)
                self.assertEqual(tracker.provider_calls, 1)
                clock[0] = 2
                with self.assertRaises(ProviderError):
                    self.adapter(configuration, wire).evaluate(
                        request_for(configuration), execution
                    )
                self.assertEqual(len(wire.requests), 1)
