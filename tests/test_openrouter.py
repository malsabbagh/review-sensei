import json
import os
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

import certifi

from review_sensei.errors import ProviderError, ReviewInputError
from review_sensei.models import ProviderRequest
from review_sensei.providers.openrouter import (
    DEFAULT_OPENROUTER_BASE_URL,
    OpenRouterProvider,
    OpenRouterRoutingPolicy,
    _NoRedirect,
    _VerifiedHTTPSHandler,
    is_allowlisted_openrouter_endpoint,
)
from review_sensei.providers.registry import ProviderSettings, default_registry


class _Response:
    def __init__(self, body: bytes):
        self.body = body
        self._offset = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, size: int):
        if size <= 0:
            return b""
        chunk = self.body[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


class _ShortReadHandle:
    def __init__(self, chunks: list[bytes]):
        self._chunks = list(chunks)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, size: int) -> bytes:
        if not self._chunks:
            return b""
        chunk = self._chunks.pop(0)
        return chunk[:size]


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
                _test_opener=lambda request, timeout, context: _Response(b"{}"),
            )

    def test_rejects_header_values_with_control_characters(self):
        with self.assertRaisesRegex(ValueError, "forbidden control character"):
            OpenRouterProvider(
                model="anthropic/claude-3.5-sonnet",
                api_key="secret",
                routing_policy=_POLICY,
                app_referer="https://reviewsensei.dev\n",
                _test_opener=lambda request, timeout, context: _Response(b"{}"),
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
            _test_opener=opener,
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
        self.assertEqual(payload["provider"]["order"], ["anthropic"])
        self.assertEqual(captured["request"].headers["Authorization"], "Bearer secret")
        self.assertEqual(
            captured["request"].headers["Referer"], "https://reviewsensei.dev"
        )

    def test_error_envelope_is_sanitized(self):
        provider = OpenRouterProvider(
            model="anthropic/claude-3.5-sonnet",
            api_key="secret",
            routing_policy=_POLICY,
            _test_opener=lambda request, timeout, context: _Response(
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
            _test_opener=lambda request, timeout, context: _Response(
                b'{"choices":[{"finish_reason":"length","message":{"content":"partial"}}]}'
            ),
        )
        with self.assertRaisesRegex(ProviderError, "truncated"):
            provider.complete(ProviderRequest(prompt="private"))

    def test_content_filter_is_rejected(self):
        provider = OpenRouterProvider(
            model="anthropic/claude-3.5-sonnet",
            api_key="secret",
            routing_policy=_POLICY,
            _test_opener=lambda request, timeout, context: _Response(
                b'{"choices":[{"finish_reason":"content_filter","message":{"content":""}}]}'
            ),
        )
        with self.assertRaisesRegex(ProviderError, "filtered"):
            provider.complete(ProviderRequest(prompt="private"))

    def test_http_402_is_not_transient(self):
        provider = OpenRouterProvider(
            model="anthropic/claude-3.5-sonnet",
            api_key="secret",
            routing_policy=_POLICY,
            _test_opener=lambda request, timeout, context: (_ for _ in ()).throw(
                HTTPError("https://openrouter.ai", 402, "payment", {}, None)
            ),
        )
        with self.assertRaises(ProviderError) as raised:
            provider.complete(ProviderRequest(prompt="private"))
        self.assertFalse(raised.exception.transient)

    def test_http_429_is_transient(self):
        provider = OpenRouterProvider(
            model="anthropic/claude-3.5-sonnet",
            api_key="secret",
            routing_policy=_POLICY,
            _test_opener=lambda request, timeout, context: (_ for _ in ()).throw(
                HTTPError("https://openrouter.ai", 429, "rate", {}, None)
            ),
        )
        with self.assertRaises(ProviderError) as raised:
            provider.complete(ProviderRequest(prompt="private"))
        self.assertTrue(raised.exception.transient)

    def test_url_error_timeout_is_transient(self):
        provider = OpenRouterProvider(
            model="anthropic/claude-3.5-sonnet",
            api_key="secret",
            routing_policy=_POLICY,
            _test_opener=lambda request, timeout, context: (_ for _ in ()).throw(
                URLError("timed out")
            ),
        )
        with self.assertRaises(ProviderError) as raised:
            provider.complete(ProviderRequest(prompt="private"))
        self.assertTrue(raised.exception.transient)

    def test_revision_prefers_observed_model(self):
        provider = OpenRouterProvider(
            model="anthropic/claude-3.5-sonnet",
            api_key="secret",
            routing_policy=_POLICY,
            _test_opener=lambda request, timeout, context: _Response(
                b'{"model":"anthropic/claude-3.5-sonnet","system_fingerprint":"fp_123",'
                b'"choices":[{"message":{"content":"ok"}}]}'
            ),
        )
        result = provider.complete(ProviderRequest(prompt="review"))
        self.assertEqual(result.revision, "anthropic/claude-3.5-sonnet")

    def test_allow_model_override_false_ignores_request_model(self):
        captured: dict[str, object] = {}

        def opener(request, timeout, context):
            captured["payload"] = json.loads(request.data)
            return _Response(b'{"choices":[{"message":{"content":"ok"}}]}')

        provider = OpenRouterProvider(
            model="anthropic/claude-3.5-sonnet",
            api_key="secret",
            routing_policy=_POLICY,
            allow_model_override=False,
            _test_opener=opener,
        )
        provider.complete(ProviderRequest(prompt="review", model="openai/gpt-4o-mini"))
        self.assertEqual(captured["payload"]["model"], "anthropic/claude-3.5-sonnet")

    def test_registry_requires_routing_policy(self):
        with self.assertRaisesRegex(ProviderError, "routing policy"):
            default_registry().create(
                ProviderSettings(
                    name="openrouter",
                    api_key="secret",
                    model="anthropic/claude-3.5-sonnet",
                )
            )

    def test_registry_requires_model(self):
        with patch.dict(
            "os.environ",
            {"OPENROUTER_UPSTREAM_PROVIDER": "anthropic"},
            clear=True,
        ):
            with self.assertRaisesRegex(ProviderError, "requires a model"):
                default_registry().create(
                    ProviderSettings(
                        name="openrouter",
                        api_key="secret",
                        openrouter_policy=_POLICY,
                    )
                )

    def test_registry_uses_default_base_url_constant(self):
        with patch.dict(
            "os.environ",
            {"OPENROUTER_UPSTREAM_PROVIDER": "anthropic"},
            clear=True,
        ):
            provider = default_registry().create(
                ProviderSettings(
                    name="openrouter",
                    api_key="secret",
                    model="anthropic/claude-3.5-sonnet",
                    openrouter_policy=_POLICY,
                )
            )
            self.assertEqual(provider.base_url, DEFAULT_OPENROUTER_BASE_URL)

    def test_allowlisted_endpoint_helper(self):
        accepted = [
            "https://openrouter.ai/api/v1",
            "https://openrouter.ai/api/v1/",
            "https://openrouter.ai/api/v1/chat/completions",
            "https://OPENROUTER.AI/api/v1",
        ]
        rejected = [
            "",
            "http://openrouter.ai/api/v1",
            "https://user:pass@openrouter.ai/api/v1",
            "https://openrouter.ai/api/v1?x=1",
            "https://openrouter.ai/api/v1#frag",
            "https://openrouter.ai:8443/api/v1",
            "https://openrouter.ai.evil/api/v1",
            "https://openrouter.ai/api/v1x",
            "https://openrouter.ai/api/v1/../evil",
            "https://openrouter.ai/api/v1/sub/../evil",
        ]
        for url in accepted:
            with self.subTest(url=url):
                self.assertTrue(is_allowlisted_openrouter_endpoint(url))
        for url in rejected:
            with self.subTest(url=url):
                self.assertFalse(is_allowlisted_openrouter_endpoint(url))

    def test_routing_policy_validation(self):
        with self.assertRaisesRegex(ValueError, "must be normalized"):
            OpenRouterRoutingPolicy(upstream_provider=" Anthropic")
        with self.assertRaisesRegex(ValueError, "must be non-empty"):
            OpenRouterRoutingPolicy(upstream_provider="   ")
        with self.assertRaisesRegex(ValueError, "slug is invalid"):
            OpenRouterRoutingPolicy(upstream_provider="bad!")

    def test_routing_policy_identity_fields_track_request_provider(self):
        policy = OpenRouterRoutingPolicy(upstream_provider="anthropic")
        self.assertEqual(
            policy.identity_fields()["allow_fallbacks"],
            policy.to_request_provider()["allow_fallbacks"],
        )
        self.assertEqual(policy.identity_fields()["schema_version"], 1)

    def test_no_redirect_rejects_redirects(self):
        handler = _NoRedirect()
        with self.assertRaisesRegex(ProviderError, "redirected"):
            handler.redirect_request(None, None, 302, "", {}, None)

    def test_verified_https_handler_rejects_host_mismatch(self):
        context = ssl.create_default_context()
        handler = _VerifiedHTTPSHandler(context, expected_hostname="openrouter.ai")
        captured: dict[str, object] = {}

        def fake_do_open(factory, req):
            captured["factory"] = factory
            raise ProviderError("stop")

        handler.do_open = fake_do_open
        request = Request("https://openrouter.ai/api/v1/chat/completions")
        with self.assertRaises(ProviderError):
            handler.https_open(request)
        factory = captured["factory"]
        assert callable(factory)
        with self.assertRaisesRegex(ProviderError, "TLS host mismatch"):
            factory("evil.example", 443)

    def test_safe_opener_rejects_redirect_end_to_end(self):
        class _RedirectingOpener:
            def open(self, request, timeout):
                handler = _NoRedirect()
                handler.redirect_request(request, None, 302, "", {}, None)
                raise AssertionError("redirect_request should have raised")

        with patch(
            "review_sensei.providers.openrouter.build_opener",
            return_value=_RedirectingOpener(),
        ):
            provider = OpenRouterProvider(
                model="anthropic/claude-3.5-sonnet",
                api_key="secret",
                routing_policy=_POLICY,
            )
            with self.assertRaisesRegex(ProviderError, "redirected"):
                provider.complete(ProviderRequest(prompt="review"))

    def test_http_409_is_not_transient(self):
        provider = OpenRouterProvider(
            model="anthropic/claude-3.5-sonnet",
            api_key="secret",
            routing_policy=_POLICY,
            _test_opener=lambda request, timeout, context: (_ for _ in ()).throw(
                HTTPError("https://openrouter.ai", 409, "conflict", {}, None)
            ),
        )
        with self.assertRaises(ProviderError) as raised:
            provider.complete(ProviderRequest(prompt="private"))
        self.assertFalse(raised.exception.transient)

    def test_default_opener_wires_safe_handlers(self):
        captured: dict[str, object] = {}

        class _CapturingOpener:
            def open(self, request, timeout):
                captured["request"] = request
                return _Response(b'{"choices":[{"message":{"content":"ok"}}]}')

        with patch(
            "review_sensei.providers.openrouter.build_opener",
            return_value=_CapturingOpener(),
        ) as build:
            provider = OpenRouterProvider(
                model="anthropic/claude-3.5-sonnet",
                api_key="secret",
                routing_policy=_POLICY,
            )
            provider.complete(ProviderRequest(prompt="review"))
        build.assert_called_once()
        handlers = build.call_args.args
        self.assertIsInstance(handlers[0], _NoRedirect)
        self.assertIsInstance(handlers[1], _VerifiedHTTPSHandler)

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
                    _test_opener=lambda request, timeout, context: _Response(
                        b'{"choices":[{"message":{"content":"ok"}}]}'
                    ),
                )
        create.assert_called_once_with(cafile=certifi.where())

    def test_ssl_context_loads_ssl_cert_file(self):
        with patch.dict(os.environ, {"SSL_CERT_FILE": certifi.where()}):
            provider = OpenRouterProvider(
                model="anthropic/claude-3.5-sonnet",
                api_key="secret",
                routing_policy=_POLICY,
                _test_opener=lambda request, timeout, context: _Response(
                    b'{"choices":[{"message":{"content":"ok"}}]}'
                ),
            )
        self.assertTrue(provider._ssl_context.check_hostname)

    def test_load_ca_bundle_missing_file(self):
        with self.assertRaisesRegex(ProviderError, "missing"):
            OpenRouterProvider._load_ca_bundle(Path("/does/not/exist.pem"))

    def test_load_ca_bundle_rejects_non_regular_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir)
            with self.assertRaisesRegex(ProviderError, "not a regular file"):
                OpenRouterProvider._load_ca_bundle(path)

    def test_load_ca_bundle_rejects_oversized_bundle(self):
        with tempfile.NamedTemporaryFile("wb", delete=False) as handle:
            handle.write(b"x" * (1_048_577))
            path = Path(handle.name)
        try:
            with self.assertRaisesRegex(ProviderError, "size limit"):
                OpenRouterProvider._load_ca_bundle(path)
        finally:
            path.unlink(missing_ok=True)

    def test_load_ca_bundle_reads_short_reads_until_eof(self):
        stat_result = os.stat(__file__)
        context = MagicMock()
        with patch("review_sensei.providers.openrouter.os.open", return_value=5):
            with patch(
                "review_sensei.providers.openrouter.os.fdopen",
                return_value=_ShortReadHandle([b"abc", b"def", b""]),
            ):
                with patch(
                    "review_sensei.providers.openrouter.os.stat",
                    return_value=stat_result,
                ):
                    with patch(
                        "review_sensei.providers.openrouter.ssl.create_default_context",
                        return_value=context,
                    ):
                        OpenRouterProvider._load_ca_bundle(Path("bundle.pem"))
        context.load_verify_locations.assert_called_once_with(cadata="abcdef")

    def test_test_opener_rejects_non_allowlisted_request_url(self):
        provider = OpenRouterProvider(
            base_url="https://openrouter.ai/api/v1",
            model="anthropic/claude-3.5-sonnet",
            api_key="secret",
            routing_policy=_POLICY,
            _test_opener=lambda request, timeout, context: _Response(b"{}"),
        )
        request = Request("https://evil.example/v1/chat/completions")
        with self.assertRaisesRegex(ProviderError, "not allowlisted"):
            provider._call_test_opener(request)

    def test_production_provider_has_no_test_opener(self):
        provider = OpenRouterProvider(
            model="anthropic/claude-3.5-sonnet",
            api_key="secret",
            routing_policy=_POLICY,
        )
        self.assertIsNone(provider._test_opener)


class ProviderConfigTests(unittest.TestCase):
    def test_openrouter_timeout_prefers_reviewsensei_env(self):
        from review_sensei.provider_config import openrouter_timeout_default

        with patch.dict(
            "os.environ",
            {
                "REVIEWSENSEI_OPENROUTER_TIMEOUT_SECONDS": "300",
                "OPENROUTER_TIMEOUT_SECONDS": "120",
            },
            clear=True,
        ):
            self.assertEqual(openrouter_timeout_default(), 300.0)

    def test_openrouter_upstream_slug_is_validated(self):
        from review_sensei.provider_config import openrouter_policy_from_env

        with patch.dict(
            "os.environ",
            {"OPENROUTER_UPSTREAM_PROVIDER": "not a slug!"},
            clear=True,
        ):
            with self.assertRaises(ReviewInputError):
                openrouter_policy_from_env()


if __name__ == "__main__":
    unittest.main()
