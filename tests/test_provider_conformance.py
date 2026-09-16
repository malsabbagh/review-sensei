from __future__ import annotations

import json
import socket
import tempfile
import unittest
from http.client import RemoteDisconnected
from pathlib import Path
from urllib.error import HTTPError, URLError

from review_sensei.errors import ProviderError
from review_sensei.models import ProviderRequest, ProviderResponse
from review_sensei.providers.fixture import FixtureProvider
from review_sensei.providers.ollama import OllamaProvider
from review_sensei.providers.openai_compatible import OpenAICompatibleProvider
from review_sensei.validation import ReviewLimits


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


def _ollama(opener, **kwargs):
    return OllamaProvider(
        base_url=kwargs.get("base_url", "http://127.0.0.1:11434/api"),
        model="qwen3.5:4b",
        api_key=kwargs.get("api_key"),
        opener=opener,
    )


def _openai(opener, **kwargs):
    return OpenAICompatibleProvider(
        api_key=kwargs.get("api_key", "secret"),
        opener=opener,
    )


class ProviderConformanceTests(unittest.TestCase):
    """Shared adapter contracts for fixture, Ollama, and openai-compatible."""

    def test_successful_complete_returns_provider_and_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ok.json"
            path.write_text('{"summary":"ok"}', encoding="utf-8")
            fixture = FixtureProvider(path, model="fixture-v1")
            result = fixture.complete(ProviderRequest(prompt="review"))
        self.assertIsInstance(result, ProviderResponse)
        self.assertEqual(result.provider, "fixture")
        self.assertEqual(result.model, "fixture-v1")

        ollama = _ollama(
            lambda request, timeout, context: _Response(
                b'{"response":"{\\"summary\\":\\"ok\\"}","model":"qwen3.5:4b"}'
            )
        )
        ollama_result = ollama.complete(ProviderRequest(prompt="review"))
        self.assertEqual(ollama_result.provider, "ollama")
        self.assertEqual(ollama_result.model, "qwen3.5:4b")
        self.assertEqual(ollama_result.revision, "qwen3.5:4b")

        openai = _openai(
            lambda request, timeout, context: _Response(
                b'{"choices":[{"message":{"content":"ok"}}],'
                b'"model":"gpt-4o-mini-2024-07-18",'
                b'"system_fingerprint":"fp_test"}'
            )
        )
        openai_result = openai.complete(ProviderRequest(prompt="review"))
        self.assertEqual(openai_result.provider, "openai-compatible")
        self.assertEqual(openai_result.model, "gpt-4o-mini")
        self.assertEqual(openai_result.revision, "fp_test")

    def test_malformed_output_is_sanitized(self) -> None:
        prompt = "private prompt with secret"
        ollama = _ollama(lambda request, timeout, context: _Response(b"{}"))
        with self.assertRaisesRegex(
            ProviderError, "did not contain review text"
        ) as raised:
            ollama.complete(ProviderRequest(prompt=prompt))
        self.assertNotIn(prompt, str(raised.exception))

        openai = _openai(lambda request, timeout, context: _Response(b"{}"))
        with self.assertRaisesRegex(
            ProviderError, "did not contain review text"
        ) as raised:
            openai.complete(ProviderRequest(prompt=prompt))
        self.assertNotIn(prompt, str(raised.exception))

    def test_resource_limit_rejects_oversized_bodies(self) -> None:
        secret = "oversized-response-secret"
        limits = ReviewLimits(max_provider_response_bytes=8)
        request = ProviderRequest(prompt="review", limits=limits)
        for factory, label, body in (
            (
                _ollama,
                "Ollama",
                b'{"response":"' + secret.encode() + b'"}',
            ),
            (
                _openai,
                "OpenAI-compatible",
                b'{"choices":[{"message":{"content":"' + secret.encode() + b'"}}]}',
            ),
        ):
            with self.subTest(adapter=label):
                provider = factory(
                    lambda request, timeout, context, body=body: _Response(body)
                )
                with self.assertRaisesRegex(
                    ProviderError, "exceeded the configured size limit"
                ) as raised:
                    provider.complete(request)
                self.assertNotIn(secret, str(raised.exception))
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "big.json"
            path.write_text(secret, encoding="utf-8")
            with self.assertRaises(ProviderError) as raised:
                FixtureProvider(path).complete(request)
            self.assertNotIn(secret, str(raised.exception))
            self.assertRegex(str(raised.exception), "size|read safely")

    def test_fixture_provider_does_not_use_transient_transport(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ok.json"
            path.write_text('{"summary":"ok"}', encoding="utf-8")
            result = FixtureProvider(path).complete(ProviderRequest(prompt="review"))
        self.assertEqual(result.provider, "fixture")

    def test_timeout_and_cancellation_are_transient_and_sanitized(self) -> None:
        secret = "private-secret"
        for factory, label in ((_ollama, "Ollama"), (_openai, "OpenAI-compatible")):
            with self.subTest(adapter=label):
                provider = factory(
                    lambda request, timeout, context: (_ for _ in ()).throw(
                        TimeoutError(f"timed out {secret}")
                    ),
                    api_key=secret if label == "Ollama" else "secret",
                )
                with self.assertRaisesRegex(ProviderError, "timed out") as raised:
                    provider.complete(ProviderRequest(prompt="private"))
                self.assertTrue(raised.exception.transient)
                self.assertNotIn(secret, str(raised.exception))

                cancelled = factory(
                    lambda request, timeout, context: (_ for _ in ()).throw(
                        RemoteDisconnected(f"closed {secret}")
                    ),
                    api_key=secret if label == "Ollama" else "secret",
                )
                with self.assertRaises(ProviderError) as raised:
                    cancelled.complete(ProviderRequest(prompt="private"))
                self.assertTrue(raised.exception.transient)
                self.assertNotIn(secret, str(raised.exception))

    def test_rate_limit_and_server_failures_are_transient(self) -> None:
        for factory in (_ollama, _openai):
            for code in (429, 503):
                with self.subTest(adapter=factory.__name__, code=code):
                    provider = factory(
                        lambda request, timeout, context: (_ for _ in ()).throw(
                            HTTPError("https://example.test", code, "err", {}, None)
                        )
                    )
                    with self.assertRaises(ProviderError) as raised:
                        provider.complete(ProviderRequest(prompt="private"))
                    self.assertTrue(raised.exception.transient)
                    self.assertIn(str(code), str(raised.exception))

    def test_network_failure_does_not_echo_secret(self) -> None:
        secret = "credential-value"
        for factory, label in ((_ollama, "Ollama"), (_openai, "OpenAI-compatible")):
            with self.subTest(adapter=label):
                provider = factory(
                    lambda request, timeout, context: (_ for _ in ()).throw(
                        URLError(socket.gaierror(socket.EAI_AGAIN, f"dns {secret}"))
                    ),
                    api_key=secret if label == "Ollama" else "secret",
                )
                with self.assertRaisesRegex(ProviderError, "request failed") as raised:
                    provider.complete(ProviderRequest(prompt="private"))
                self.assertTrue(raised.exception.transient)
                self.assertNotIn(secret, str(raised.exception))

    def test_permanent_url_error_is_not_transient(self) -> None:
        for factory, label in ((_ollama, "Ollama"), (_openai, "OpenAI-compatible")):
            with self.subTest(adapter=label):
                provider = factory(
                    lambda request, timeout, context: (_ for _ in ()).throw(
                        URLError("unknown url type")
                    ),
                    api_key="secret" if label == "Ollama" else "secret",
                )
                with self.assertRaisesRegex(ProviderError, "request failed") as raised:
                    provider.complete(ProviderRequest(prompt="private"))
                self.assertFalse(raised.exception.transient)

    def test_missing_credentials_fail_before_request(self) -> None:
        with self.assertRaisesRegex(ValueError, "API key"):
            OpenAICompatibleProvider(api_key=None)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ok.json"
            path.write_text("{}", encoding="utf-8")
            FixtureProvider(path).complete(ProviderRequest(prompt="review"))

    def test_json_mode_is_requested_when_supported(self) -> None:
        captured = {}

        def opener(request, timeout, context):
            captured["payload"] = json.loads(request.data)
            return _Response(b'{"response":"{\\"summary\\":\\"ok\\"}"}')

        _ollama(opener).complete(ProviderRequest(prompt="review", json_mode=True))
        self.assertEqual(captured["payload"]["format"], "json")

        def openai_opener(request, timeout, context):
            captured["openai"] = json.loads(request.data)
            return _Response(b'{"choices":[{"message":{"content":"ok"}}]}')

        _openai(openai_opener).complete(
            ProviderRequest(prompt="review", json_mode=True)
        )
        self.assertEqual(captured["openai"]["response_format"], {"type": "json_object"})


if __name__ == "__main__":
    unittest.main()
