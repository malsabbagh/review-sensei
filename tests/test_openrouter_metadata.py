"""Synthetic protocol metadata tests; no provider access or support claims."""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jsonschema import Draft202012Validator, ValidationError

from review_sensei.cli import main
from review_sensei.errors import ProviderError, ReviewInputError
from review_sensei.evaluation import MeasuredProvider, configuration_digest, load_corpus
from review_sensei.models import ProviderRequest, ProviderResponse, ProviderTokenUsage
from review_sensei.openrouter_qualification import (
    CURRENT_HOSTED_OPENROUTER_QUALIFICATION_TARGET,
    OpenRouterQualificationTarget,
    qualification_record_from_reports,
    routing_policy_digest,
)
from review_sensei.provider_config import (
    HOSTED_OPENROUTER_DEFAULTS,
    hosted_openrouter_upstream,
)
from review_sensei.providers.openrouter import (
    OpenRouterProvider,
    OpenRouterRoutingPolicy,
)
from review_sensei.schemas import load_schema, validate_public_document
from tests.test_evaluation import _write_corpus
from tests.test_openrouter import _Response
from tests.test_openrouter_qualification import (
    EVALUATED_AT,
    REPRO,
    _make_openrouter_report,
    _retained_artifacts,
)


class OpenRouterMetadataTests(unittest.TestCase):
    def complete(self, metadata):
        calls = []

        def opener(request, timeout, context):
            calls.append(json.loads(request.data))
            return _Response(
                json.dumps(
                    {"choices": [{"message": {"content": "ok"}}], **metadata}
                ).encode()
            )

        provider = OpenRouterProvider(
            model="requested/model",
            api_key="synthetic",
            routing_policy=OpenRouterRoutingPolicy("morph"),
            _test_opener=opener,
        )
        result = provider.complete(ProviderRequest(prompt="synthetic"))
        self.assertEqual(len(calls), 1)
        return result

    def test_requested_observed_fingerprint_and_upstream_are_independent(self):
        result = self.complete(
            {
                "model": "observed/model-v2",
                "provider": "Morph",
                "system_fingerprint": "real-fingerprint-sentinel",
            }
        )
        self.assertEqual(result.model, "requested/model")
        self.assertEqual(result.observed_model, "observed/model-v2")
        self.assertEqual(result.observed_upstream, "Morph")
        self.assertEqual(result.revision, "real-fingerprint-sentinel")
        self.assertEqual(result.revision_source, "provider-system-fingerprint")

    def test_missing_identity_is_unknown_and_model_is_never_revision(self):
        for metadata in (
            {},
            {"model": "observed/model"},
            {"system_fingerprint": "fp_1"},
        ):
            with self.subTest(metadata=metadata):
                result = self.complete(metadata)
                self.assertEqual(result.revision, metadata.get("system_fingerprint"))
                self.assertEqual(result.observed_model, metadata.get("model"))
                self.assertIsNone(result.observed_upstream)

    def test_malformed_optional_identity_is_discarded_without_echoing(self):
        for value in (
            "",
            "private\ntext",
            "private\u202etext",
            "x" * 513,
            {},
            4,
            True,
            "\ud800",
        ):
            with self.subTest(kind=type(value).__name__):
                result = self.complete(
                    {"model": value, "provider": value, "system_fingerprint": value}
                )
                self.assertIsNone(result.observed_model)
                self.assertIsNone(result.observed_upstream)
                self.assertIsNone(result.revision)
                self.assertIsNone(result.revision_source)

    def test_usage_complete_partial_absent_zero_and_malformed(self):
        result = self.complete(
            {
                "usage": {
                    "prompt_tokens": 7,
                    "completion_tokens": 3,
                    "total_tokens": 10,
                    "cost": 999,
                    "reasoning": "private",
                }
            }
        )
        self.assertEqual(result.token_usage, ProviderTokenUsage(7, 3, 10))
        self.assertEqual(
            result.token_usage.to_dict(),
            {
                "source": "provider-reported",
                "prompt_tokens": 7,
                "completion_tokens": 3,
                "total_tokens": 10,
            },
        )
        for value in (
            None,
            [],
            "private",
            {},
            {"prompt_tokens": False},
            {"prompt_tokens": -1},
            {"prompt_tokens": 1.5},
            {"prompt_tokens": float("inf")},
            {"prompt_tokens": 2**63},
        ):
            with self.subTest(value=value):
                self.assertIsNone(self.complete({"usage": value}).token_usage)
        partial = self.complete(
            {
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": "private",
                    "total_tokens": None,
                }
            }
        ).token_usage
        self.assertEqual(partial, ProviderTokenUsage(prompt_tokens=0))
        self.assertIsNone(partial.total_tokens)

    def test_unparseable_oversized_integer_is_a_sanitized_provider_failure(self):
        provider = OpenRouterProvider(
            model="requested/model",
            api_key="synthetic",
            routing_policy=OpenRouterRoutingPolicy("morph"),
            _test_opener=lambda request, timeout, context: _Response(
                b'{"usage":{"total_tokens":'
                + b"9" * 5000
                + b'},"choices":[{"message":{"content":"private"}}]}'
            ),
        )
        with self.assertRaisesRegex(ProviderError, "was not valid JSON") as raised:
            provider.complete(ProviderRequest(prompt="synthetic"))
        self.assertNotIn("private", str(raised.exception))

    def test_typed_usage_rejects_invalid_counters(self):
        for value in (True, -1, 1.1, 2**63):
            with self.assertRaises(ReviewInputError):
                ProviderTokenUsage(prompt_tokens=value)

    def test_typed_observations_validate_bounds_and_provenance(self):
        for fields in (
            {"observed_model": "bad\nidentifier"},
            {"observed_upstream": "x" * 129},
            {"revision_source": "operator-attested", "revision": "fp_1"},
            {"revision_source": "provider-system-fingerprint"},
            {"token_usage": {"total_tokens": 1}},
        ):
            with self.subTest(fields=list(fields)), self.assertRaises(ReviewInputError):
                ProviderResponse("ok", "synthetic", **fields)
        self.assertEqual(
            self.complete(
                {
                    "usage": {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                    }
                }
            ).token_usage,
            ProviderTokenUsage(0, 0, 0),
        )

    def test_specific_endpoint_is_preserved_with_strict_policy(self):
        for slug in (
            "deepinfra/turbo",
            "google-vertex/us-east5",
            "baseten/fp4/us/priority",
        ):
            with self.subTest(slug=slug):
                policy = OpenRouterRoutingPolicy(slug)
                captured = []

                def opener(request, timeout, context):
                    captured.append(json.loads(request.data))
                    raise ProviderError("upstream rejected")

                provider = OpenRouterProvider(
                    model="requested/model",
                    api_key="synthetic",
                    routing_policy=policy,
                    _test_opener=opener,
                )
                with self.assertRaisesRegex(ProviderError, "upstream rejected"):
                    provider.complete(ProviderRequest(prompt="synthetic"))
                self.assertEqual(len(captured), 1)
                self.assertEqual(
                    captured[0]["provider"],
                    {
                        "order": [slug],
                        "allow_fallbacks": False,
                        "require_parameters": True,
                        "data_collection": "deny",
                        "zdr": True,
                    },
                )
                self.assertNotEqual(
                    routing_policy_digest(policy),
                    routing_policy_digest(OpenRouterRoutingPolicy(slug.split("/")[0])),
                )

    def test_endpoint_grammar_rejects_unsupported_syntax(self):
        for slug in (
            "",
            "a/",
            "/a",
            "a//b",
            "a/../b",
            "a/./b",
            "a/b/c/d/e",
            "a/b?x",
            "a/b#x",
            "https://x",
            "a/%2e",
            "a\\b",
            "a/\n",
            "A/b",
            "a/" + "b" * 127,
            "a/\u200bb",
        ):
            with self.subTest(slug=slug), self.assertRaises(ValueError):
                OpenRouterRoutingPolicy(slug)


class ProviderUsageAccountingTests(unittest.TestCase):
    def test_failed_cancelled_and_correction_attempts_are_counted_once(self):
        class SequenceProvider:
            name = "synthetic"
            model = "synthetic"

            def __init__(self):
                self.index = 0

            def complete(self, request):
                self.index += 1
                if self.index == 1:
                    raise ProviderError("truncated")
                if self.index == 2:
                    raise KeyboardInterrupt()
                return ProviderResponse(
                    "ok", self.name, token_usage=ProviderTokenUsage(7, 3, 10)
                )

        measured = MeasuredProvider(SequenceProvider())
        request = ProviderRequest(prompt="synthetic")
        for error in (ProviderError, KeyboardInterrupt):
            with self.assertRaises(error):
                measured.complete(request)
        measured.complete(request)
        self.assertEqual(measured.calls, 3)
        self.assertEqual(measured.prompt_bytes, 27)
        self.assertEqual(len(measured.elapsed_ms), 3)
        usage = measured.provider_reported_usage()
        self.assertEqual(usage["attempts"], 3)
        self.assertEqual(usage["reported_attempts"], 1)
        self.assertIsNone(usage["total_tokens"])
        self.assertFalse(usage["complete"])

    def test_complete_usage_sums_and_partial_usage_does_not_fabricate_totals(self):
        class SequenceProvider:
            name = "synthetic"
            model = "synthetic"
            usage = ProviderTokenUsage(7, 3, 10)

            def complete(self, request):
                return ProviderResponse("ok", self.name, token_usage=self.usage)

        provider = SequenceProvider()
        measured = MeasuredProvider(provider)
        self.assertIsNone(measured.provider_reported_usage()["total_tokens"])
        for _ in range(2):
            measured.complete(ProviderRequest(prompt="synthetic"))
        usage = measured.provider_reported_usage()
        self.assertTrue(usage["complete"])
        self.assertEqual(usage["total_tokens"], 20)
        provider.usage = ProviderTokenUsage(prompt_tokens=0)
        measured.complete(ProviderRequest(prompt="synthetic"))
        usage = measured.provider_reported_usage()
        self.assertFalse(usage["complete"])
        self.assertEqual(usage["prompt_tokens"], 14)
        self.assertIsNone(usage["completion_tokens"])
        self.assertIsNone(usage["total_tokens"])

    def test_aggregate_overflow_is_unknown(self):
        class Provider:
            name = "synthetic"
            model = "synthetic"

            def complete(self, request):
                return ProviderResponse(
                    "ok",
                    self.name,
                    token_usage=ProviderTokenUsage(total_tokens=2**63 - 1),
                )

        measured = MeasuredProvider(Provider())
        for _ in range(2):
            measured.complete(ProviderRequest(prompt="synthetic"))
        self.assertIsNone(measured.provider_reported_usage()["total_tokens"])
        self.assertFalse(measured.provider_reported_usage()["complete"])


class HostedQualificationConfigurationTests(unittest.TestCase):
    def test_current_hosted_target_is_eligible_without_promoting_other_combinations(
        self,
    ):
        target = CURRENT_HOSTED_OPENROUTER_QUALIFICATION_TARGET
        self.assertIn(
            (target.model, target.upstream_provider), HOSTED_OPENROUTER_DEFAULTS
        )
        self.assertEqual(
            hosted_openrouter_upstream(target.model), target.upstream_provider
        )
        self.assertEqual(
            OpenRouterQualificationTarget(target.model, target.upstream_provider),
            target,
        )
        for model, upstream in HOSTED_OPENROUTER_DEFAULTS - {
            (target.model, target.upstream_provider)
        }:
            with self.assertRaisesRegex(ReviewInputError, "outside the published"):
                OpenRouterQualificationTarget(model, upstream)
        with self.assertRaisesRegex(ReviewInputError, "outside the published"):
            OpenRouterQualificationTarget(target.model, "morph/unknown")

        # New target eligibility cannot inherit trusted support from the
        # allowlist, copied artifacts or fixture-only quality evidence.
        for mode, count, retain in (
            ("fixture", 3, True),
            ("live", 1, True),
            ("live", 3, False),
        ):
            reports = [
                _make_openrouter_report(
                    mode=mode,
                    model=target.model,
                    invocation_id=f"synthetic-target-{index}",
                )
                for index in range(count)
            ]
            with (
                self.subTest(mode=mode, reports=count, retained=retain),
                self.assertRaises(ReviewInputError),
            ):
                qualification_record_from_reports(
                    reports,
                    target=target,
                    observed_revision="synthetic-operator-attestation",
                    reproducibility=REPRO,
                    evaluated_at=EVALUATED_AT,
                    report_artifacts=_retained_artifacts(reports) if retain else None,
                    status="supported",
                )

    def test_documented_evaluate_command_captures_the_effective_target_and_schema(self):
        target = CURRENT_HOSTED_OPENROUTER_QUALIFICATION_TARGET
        payloads = []

        def opener(request, timeout):
            payloads.append(json.loads(request.data))
            return _Response(
                json.dumps(
                    {
                        "model": target.model,
                        "provider": "Morph",
                        "system_fingerprint": "fp_synthetic",
                        "choices": [
                            {"message": {"content": '{"summary":"ok","comments":[]}'}}
                        ],
                        "usage": {
                            "prompt_tokens": 7,
                            "completion_tokens": 3,
                            "total_tokens": 10,
                        },
                    }
                ).encode()
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            corpus_path = _write_corpus(Path(temp_dir))
            corpus = load_corpus(corpus_path)
            output = Path(temp_dir) / "report.json"
            with (
                patch.dict(
                    "os.environ",
                    {
                        "OPENROUTER_API_KEY": "synthetic-only",
                        "OPENROUTER_UPSTREAM_PROVIDER": "morph",
                    },
                    clear=True,
                ),
                patch(
                    "review_sensei.providers.openrouter.build_opener",
                    return_value=SimpleNamespace(open=opener),
                ),
                redirect_stdout(io.StringIO()),
            ):
                code = main(
                    [
                        "evaluate",
                        "--mode",
                        "live",
                        "--corpus",
                        str(corpus_path),
                        "--provider",
                        "openrouter",
                        "--base-url",
                        target.base_url,
                        "--model",
                        target.model,
                        "--provider-version",
                        "operator-attested-sentinel",
                        "--allow-live-model",
                        "--allow-data-egress",
                        "--output",
                        str(output),
                    ]
                )
            # The deterministic corpus pins fixture provider identity, so this
            # synthetic live result correctly fails its quality gate. Transport
            # correctness and eligibility do not manufacture passing evidence.
            self.assertEqual(code, 1)
            report = json.loads(output.read_text())
            self.assertEqual(
                report["run"]["configuration_digest"],
                configuration_digest(
                    corpus,
                    "live",
                    "openrouter",
                    target.model,
                    openrouter_policy=target.routing_policy().identity_fields(),
                ),
            )
            variant_digest = configuration_digest(
                corpus,
                "live",
                "openrouter",
                target.model,
                openrouter_policy=OpenRouterRoutingPolicy(
                    "morph/variant"
                ).identity_fields(),
            )
            self.assertNotEqual(report["run"]["configuration_digest"], variant_digest)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["model"], target.model)
        self.assertEqual(
            payloads[0]["provider"], target.routing_policy().to_request_provider()
        )
        # This CLI field remains explicitly operator-attested. A returned alias
        # or fingerprint cannot replace it or establish qualification authority.
        self.assertEqual(
            report["run"]["provider_version"], "operator-attested-sentinel"
        )
        self.assertEqual(
            report["metrics"]["provider_reported_usage"]["total_tokens"], 10
        )
        validate_public_document(report, "evaluation-report")
        legacy = copy.deepcopy(report)
        del legacy["metrics"]["provider_reported_usage"]
        validate_public_document(legacy, "evaluation-report")
        # The prior closed v1 reader rejects the additive object. Migrating the
        # reader to the current packaged schema is required; old report bytes
        # still validate and must not be rewritten as qualification evidence.
        old_schema = copy.deepcopy(load_schema("evaluation-report"))
        del old_schema["properties"]["metrics"]["properties"]["provider_reported_usage"]
        with self.assertRaises(ValidationError):
            Draft202012Validator(old_schema).validate(report)
        Draft202012Validator(old_schema).validate(legacy)
        for key, value in (
            ("total_tokens", -1),
            ("prompt_tokens", True),
            ("completion_tokens", 2**63),
            ("cost", 1),
        ):
            invalid = copy.deepcopy(report)
            invalid["metrics"]["provider_reported_usage"][key] = value
            with self.subTest(key=key), self.assertRaises(ReviewInputError):
                validate_public_document(invalid, "evaluation-report")
