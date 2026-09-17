import json
import os
import ssl
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import certifi

from review_sensei.errors import ProviderError
from review_sensei.models import ProviderRequest
from review_sensei.providers.openrouter import (
    OpenRouterProvider,
    OpenRouterRoutingPolicy,
    is_allowlisted_openrouter_endpoint,
)
from review_sensei.providers.registry import ProviderSettings, default_registry


class _Response:
    def __init__(self, body: bytes):
        self.body = body
        self.done = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, size: int):
        if self.done:
            return b""
        self.done = True
        return self.body[:size]


_POLICY = OpenRouterRoutingPolicy(upstream_provider="anthropic")


class OpenRouterProviderTests(unittest.TestCase):
    def test_requires_explicit_api_key(self):
        with self.assertRaisesRegex(ValueError, "requires an API key"):
            OpenRouterProvider(
                model="anthropic/claude-3.5-sonnet",
                api_key="",
                routing_policy=_POLICY,
            )

    def test_rejects_non_allowlisted_endpoint(self):
        with self.assertRaisesRegex(ValueError, "not allowlisted"):
            OpenRouterProvider(
                base_url="https://example.test/v1",
                model="anthropic/claude-3.5-sonnet",
                api_key="secret",
                routing_policy=_POLICY,
                opener=lambda request, timeout, context: _Response(b"{}"),
            )

    def test_sends_bounded_chat_completion_request(self):
        captured = {}

        def opener(request, timeout, context):
            captured["request"] = request
            captured["timeout"] = timeout
            return _Response(
                b'{"choices":[{"message":{"content":"{\\"summary\\":\\"ok\\"}"}}]}'
            )

        provider = OpenRouterProvider(
            model="anthropic/claude-3.5-sonnet",
            api_key="secret",
            routing_policy=_POLICY,
            timeout_seconds=7,
            max_output_tokens=123,
            opener=opener,
        )
        result = provider.complete(ProviderRequest(prompt="review", model=None))
        payload = json.loads(captured["request"].data)
        self.assertEqual(result.text, '{"summary":"ok"}')
        self.assertEqual(result.provider, "openrouter")
        self.assertEqual(
            captured["request"].full_url,
            "https://openrouter.ai/api/v1/chat/completions",
        )
        self.assertEqual(captured["timeout"], 7)
        self.assertEqual(payload["model"], "anthropic/claude-3.5-sonnet")
        self.assertEqual(payload["provider"]["allow_fallbacks"], False)
        self.assertEqual(payload["provider"]["zdr"], True)
        self.assertEqual(captured["request"].headers["Authorization"], "Bearer secret")
        self.assertEqual(
            captured["request"].headers["Referer"], "https://reviewsensei.dev"
        )

    def test_error_envelope_is_sanitized(self):
        provider = OpenRouterProvider(
            model="anthropic/claude-3.5-sonnet",
            api_key="secret",
            routing_policy=_POLICY,
            opener=lambda request, timeout, context: _Response(
                b'{"error":{"message":"invalid key"}}'
            ),
        )
        with self.assertRaisesRegex(ProviderError, "reported an error"):
            provider.complete(ProviderRequest(prompt="private"))

    def test_truncation_is_rejected(self):
        provider = OpenRouterProvider(
            model="anthropic/claude-3.5-sonnet",
            api_key="secret",
            routing_policy=_POLICY,
            opener=lambda request, timeout, context: _Response(
                b'{"choices":[{"finish_reason":"length","message":{"content":"partial"}}]}'
            ),
        )
        with self.assertRaisesRegex(ProviderError, "truncated"):
            provider.complete(ProviderRequest(prompt="private"))

    def test_http_402_is_not_transient(self):
        provider = OpenRouterProvider(
            model="anthropic/claude-3.5-sonnet",
            api_key="secret",
            routing_policy=_POLICY,
            opener=lambda request, timeout, context: (_ for _ in ()).throw(
                HTTPError("https://openrouter.ai", 402, "payment", {}, None)
            ),
        )
        with self.assertRaises(ProviderError) as raised:
            provider.complete(ProviderRequest(prompt="private"))
        self.assertFalse(raised.exception.transient)

    def test_registry_requires_routing_policy(self):
        with self.assertRaisesRegex(ProviderError, "routing policy"):
            default_registry().create(
                ProviderSettings(
                    name="openrouter",
                    api_key="secret",
                    model="anthropic/claude-3.5-sonnet",
                )
            )

    def test_allowlisted_endpoint_helper(self):
        self.assertTrue(
            is_allowlisted_openrouter_endpoint("https://openrouter.ai/api/v1")
        )

    def test_ssl_context_uses_certifi_when_ssl_cert_file_is_unset(self):
        dummy = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        with patch.dict(os.environ, {}, clear=True):
            with patch(
                "review_sensei.providers.openrouter.ssl.create_default_context",
                return_value=dummy,
            ) as create:
                OpenRouterProvider(
                    model="anthropic/claude-3.5-sonnet",
                    api_key="secret",
                    routing_policy=_POLICY,
                    opener=lambda request, timeout, context: _Response(
                        b'{"choices":[{"message":{"content":"ok"}}]}'
                    ),
                )
        create.assert_called_once_with(cafile=certifi.where())


if __name__ == "__main__":
    unittest.main()
