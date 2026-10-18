"""Installed-wheel OpenRouter contracts using synthetic response bytes only."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))

from packaging_guard import assert_distribution_import  # noqa: E402

assert_distribution_import()

from review_sensei import ProviderRequest, ProviderTokenUsage  # noqa: E402
from review_sensei.openrouter_qualification import (  # noqa: E402
    CURRENT_HOSTED_OPENROUTER_QUALIFICATION_TARGET,
)
from review_sensei.provider_config import hosted_openrouter_upstream  # noqa: E402
from review_sensei.providers.openrouter import (  # noqa: E402
    OpenRouterProvider,
    OpenRouterRoutingPolicy,
)
from review_sensei.schemas import load_schema  # noqa: E402


class _Response:
    def __init__(self, value):
        self._body = json.dumps(value).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size):
        body, self._body = self._body[:size], self._body[size:]
        return body


class InstalledOpenRouterMetadataContractTests(unittest.TestCase):
    def test_installed_protocol_metadata_routing_and_eligibility(self):
        captured = []

        def opener(request, timeout, context):
            captured.append(json.loads(request.data))
            return _Response(
                {
                    "model": "observed/model",
                    "provider": "Synthetic Upstream",
                    "system_fingerprint": "fp_synthetic",
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 1},
                }
            )

        policy = OpenRouterRoutingPolicy("deepinfra/turbo")
        provider = OpenRouterProvider(
            model="requested/model",
            api_key="synthetic",
            routing_policy=policy,
            _test_opener=opener,
        )
        response = provider.complete(ProviderRequest(prompt="synthetic"))
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["provider"], policy.to_request_provider())
        self.assertEqual(response.model, "requested/model")
        self.assertEqual(response.observed_model, "observed/model")
        self.assertEqual(response.observed_upstream, "Synthetic Upstream")
        self.assertEqual(response.revision, "fp_synthetic")
        self.assertEqual(response.revision_source, "provider-system-fingerprint")
        self.assertEqual(response.token_usage, ProviderTokenUsage(2, 1))
        self.assertIsNone(response.token_usage.total_tokens)
        target = CURRENT_HOSTED_OPENROUTER_QUALIFICATION_TARGET
        self.assertEqual(
            hosted_openrouter_upstream(target.model), target.upstream_provider
        )
        metrics = load_schema("evaluation-report")["properties"]["metrics"][
            "properties"
        ]
        self.assertIn("provider_reported_usage", metrics)
