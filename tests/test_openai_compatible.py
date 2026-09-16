import json
import os
import ssl
import tempfile
import unittest
from http.client import RemoteDisconnected
from unittest.mock import patch
from urllib.error import URLError

import certifi

from review_sensei.errors import ProviderError
from review_sensei.models import ProviderRequest
from review_sensei.providers.openai_compatible import (
    OpenAICompatibleProvider,
    is_allowlisted_openai_compatible_endpoint,
)


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


class _IgnoringSizeResponse(_Response):
    def read(self, size: int):
        if self.done:
            return b""
        self.done = True
        return self.body


class _ReadTypeErrorResponse(_Response):
    def read(self, size: int):
        raise TypeError("bad response reader")


class _EnterTypeErrorResponse:
    def __enter__(self):
        raise TypeError("credential=secret-token")

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, size: int):
        return b"{}"


class OpenAICompatibleProviderTests(unittest.TestCase):
    def test_requires_explicit_api_key(self):
        with self.assertRaisesRegex(ValueError, "requires an API key"):
            OpenAICompatibleProvider(api_key=None)

    def test_api_key_rejects_control_characters_without_echoing_credential(self):
        for api_key in (
            "secret\r\nX-Injected: yes",
            "secret\x00suffix",
            "secret\u202esuffix",
        ):
            with self.subTest(api_key=repr(api_key)):
                with self.assertRaisesRegex(
                    ValueError, "contains a forbidden control character"
                ) as raised:
                    OpenAICompatibleProvider(api_key=api_key)
                self.assertNotIn(api_key, str(raised.exception))

    def test_api_key_is_bounded_without_echoing_credential(self):
        api_key = "x" * 4_097
        with self.assertRaisesRegex(
            ValueError, "exceeds the configured size limit"
        ) as raised:
            OpenAICompatibleProvider(api_key=api_key)
        self.assertNotIn(api_key, str(raised.exception))

    def test_api_key_rejects_non_ascii_values(self):
        with self.assertRaisesRegex(ValueError, "only ASCII"):
            OpenAICompatibleProvider(api_key="secret-é")

    def test_sends_bounded_chat_completion_request(self):
        captured = {}

        def opener(request, timeout, context):
            captured["request"] = request
            captured["timeout"] = timeout
            captured["context"] = context
            return _Response(
                b'{"choices":[{"message":{"content":"{\\"summary\\":\\"ok\\"}"}}]}'
            )

        provider = OpenAICompatibleProvider(
            base_url="https://example.test/v1",
            model="triage",
            api_key="secret",
            timeout_seconds=7,
            max_output_tokens=123,
            allow_custom_endpoint=True,
            opener=opener,
        )
        result = provider.complete(ProviderRequest(prompt="review", model=None))
        payload = json.loads(captured["request"].data)
        self.assertEqual(result.text, '{"summary":"ok"}')
        self.assertEqual(result.provider, "openai-compatible")
        self.assertEqual(
            captured["request"].full_url, "https://example.test/v1/chat/completions"
        )
        self.assertEqual(captured["timeout"], 7)
        self.assertEqual(payload["model"], "triage")
        self.assertEqual(payload["messages"], [{"role": "user", "content": "review"}])
        self.assertEqual(payload["max_tokens"], 123)
        self.assertEqual(captured["request"].headers["Authorization"], "Bearer secret")

    def test_malformed_envelope_is_sanitized(self):
        provider = OpenAICompatibleProvider(
            api_key="secret",
            opener=lambda request, timeout, context: _Response(b"{}"),
        )
        with self.assertRaisesRegex(ProviderError, "did not contain review text"):
            provider.complete(ProviderRequest(prompt="private"))

    def test_custom_endpoint_requires_explicit_egress_opt_in(self):
        with self.assertRaisesRegex(ValueError, "not allowlisted"):
            OpenAICompatibleProvider(
                base_url="https://example.test/v1",
                api_key="secret",
                opener=lambda request, timeout, context: _Response(b"{}"),
            )

        provider = OpenAICompatibleProvider(
            base_url="https://example.test/v1",
            api_key="secret",
            allow_custom_endpoint=True,
            opener=lambda request, timeout, context: _Response(
                b'{"choices":[{"message":{"content":"ok"}}]}'
            ),
        )
        self.assertEqual(
            provider.complete(ProviderRequest(prompt="private")).text, "ok"
        )

    def test_endpoint_rejects_credentials_and_query(self):
        for endpoint in (
            "https://user:secret@api.openai.com/v1",
            "https://api.openai.com/v1?redirect=https://attacker.test",
            "https://api.openai.com/v1#fragment",
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ValueError):
                    OpenAICompatibleProvider(api_key="secret", base_url=endpoint)

    def test_response_that_ignores_read_size_is_rejected_before_buffer_growth(self):
        response = _IgnoringSizeResponse(
            b'{"choices":[{"message":{"content":"' + (b"x" * 40) + b'"}}]}'
        )
        provider = OpenAICompatibleProvider(
            api_key="secret",
            opener=lambda request, timeout, context: response,
        )
        limits = ProviderRequest(prompt="private", max_response_bytes=16)
        with self.assertRaisesRegex(ProviderError, "exceeded the configured size"):
            provider.complete(limits)

    def test_context_is_reused_for_injected_opener(self):
        contexts = []

        def opener(request, timeout, context):
            contexts.append(context)
            return _Response(b'{"choices":[{"message":{"content":"ok"}}]}')

        provider = OpenAICompatibleProvider(api_key="secret", opener=opener)
        provider.complete(ProviderRequest(prompt="one"))
        provider.complete(ProviderRequest(prompt="two"))
        self.assertEqual(len(contexts), 2)
        self.assertIs(contexts[0], contexts[1])

    def test_custom_opener_without_context_kwarg_raises(self):
        def opener(request):
            return _Response(b'{"choices":[{"message":{"content":"ok"}}]}')

        provider = OpenAICompatibleProvider(api_key="secret", opener=opener)
        with self.assertRaisesRegex(
            ProviderError, "must accept timeout and context kwargs"
        ):
            provider.complete(ProviderRequest(prompt="private"))

    def test_response_reader_type_error_is_not_reported_as_opener_error(self):
        provider = OpenAICompatibleProvider(
            api_key="secret",
            opener=lambda request, timeout, context: _ReadTypeErrorResponse(b"{}"),
        )
        with self.assertRaisesRegex(ProviderError, "response body could not be read"):
            provider.complete(ProviderRequest(prompt="private"))

    def test_context_manager_type_error_is_sanitized(self):
        provider = OpenAICompatibleProvider(
            api_key="secret",
            opener=lambda request, timeout, context: _EnterTypeErrorResponse(),
        )
        with self.assertRaisesRegex(
            ProviderError, "response could not be opened"
        ) as raised:
            provider.complete(ProviderRequest(prompt="private"))
        self.assertNotIn("secret-token", str(raised.exception))
        self.assertNotIn("credential=", str(raised.exception))

    def test_custom_opener_without_context_manager_is_sanitized(self):
        class ResponseWithoutContextManager:
            def read(self, size: int):
                return b"{}"

        provider = OpenAICompatibleProvider(
            api_key="secret",
            opener=lambda request, timeout, context: ResponseWithoutContextManager(),
        )
        with self.assertRaisesRegex(ProviderError, "response could not be opened"):
            provider.complete(ProviderRequest(prompt="private"))

    def test_http_protocol_failure_is_sanitized(self):
        provider = OpenAICompatibleProvider(
            api_key="secret",
            opener=lambda request, timeout, context: (_ for _ in ()).throw(
                RemoteDisconnected("private prompt")
            ),
        )
        with self.assertRaisesRegex(ProviderError, "request failed") as raised:
            provider.complete(ProviderRequest(prompt="private prompt"))
        self.assertNotIn("private prompt", str(raised.exception))

    def test_network_urlerror_is_transient_and_sanitized(self):
        import socket

        secret_reason = "dns failure with credential=private-secret"
        provider = OpenAICompatibleProvider(
            api_key="secret",
            opener=lambda request, timeout, context: (_ for _ in ()).throw(
                URLError(socket.gaierror(socket.EAI_AGAIN, secret_reason))
            ),
        )
        with self.assertRaisesRegex(ProviderError, "request failed") as raised:
            provider.complete(ProviderRequest(prompt="private"))
        self.assertTrue(raised.exception.transient)
        self.assertNotIn(secret_reason, str(raised.exception))

    def test_invalid_ssl_cert_file_fails_closed_without_exposing_path(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = os.path.join(directory, "missing.pem")
            with patch.dict(os.environ, {"SSL_CERT_FILE": missing}):
                with self.assertRaisesRegex(
                    ProviderError, "SSL_CERT_FILE is missing"
                ) as raised:
                    OpenAICompatibleProvider(api_key="secret")
            self.assertNotIn(missing, str(raised.exception))

    def test_ssl_context_uses_certifi_when_ssl_cert_file_is_unset(self):
        dummy = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        with patch.dict(os.environ, {}, clear=True):
            with patch(
                "review_sensei.providers.openai_compatible.ssl.create_default_context",
                return_value=dummy,
            ) as create:
                provider = OpenAICompatibleProvider(
                    api_key="secret",
                    opener=lambda request, timeout, context: _Response(
                        b'{"choices":[{"message":{"content":"ok"}}]}'
                    ),
                )
        create.assert_called_once_with(cafile=certifi.where())
        self.assertIs(provider._ssl_context, dummy)
        self.assertIsNotNone(provider._safe_opener)

    def test_allowlisted_endpoint_helper_rejects_custom_hosts(self):
        self.assertTrue(
            is_allowlisted_openai_compatible_endpoint("https://api.openai.com/v1")
        )
        self.assertFalse(
            is_allowlisted_openai_compatible_endpoint("https://attacker.example/v1")
        )
        self.assertFalse(
            is_allowlisted_openai_compatible_endpoint(
                "https://api.openai.com/v1?redirect=https://attacker.example"
            )
        )
        self.assertTrue(
            is_allowlisted_openai_compatible_endpoint("https://api.openai.com/v1/")
        )
        self.assertTrue(
            is_allowlisted_openai_compatible_endpoint(
                "https://api.openai.com/v1/chat/completions"
            )
        )
        self.assertFalse(
            is_allowlisted_openai_compatible_endpoint("https://api.openai.com/v2")
        )
        self.assertFalse(
            is_allowlisted_openai_compatible_endpoint(
                "https://evil.com@api.openai.com:443/v1"
            )
        )
        self.assertFalse(
            is_allowlisted_openai_compatible_endpoint(
                "https://user:pass@api.openai.com:443/v1"
            )
        )
        self.assertFalse(
            is_allowlisted_openai_compatible_endpoint("https://@api.openai.com/v1")
        )

    def test_builtin_transport_pins_tls_hostname_verification(self):
        provider = OpenAICompatibleProvider(api_key="secret")
        self.assertEqual(provider._expected_hostname, "api.openai.com")
        self.assertTrue(provider._ssl_context.check_hostname)
        self.assertEqual(provider._ssl_context.verify_mode, ssl.CERT_REQUIRED)

    def test_http_rate_limit_and_server_errors_are_transient(self):
        from urllib.error import HTTPError

        for code in (429, 500, 503):
            with self.subTest(code=code):
                provider = OpenAICompatibleProvider(
                    api_key="secret",
                    opener=lambda request, timeout, context: (_ for _ in ()).throw(
                        HTTPError("https://api.openai.com/v1", code, "err", {}, None)
                    ),
                )
                with self.assertRaises(ProviderError) as raised:
                    provider.complete(ProviderRequest(prompt="private"))
                self.assertTrue(raised.exception.transient)

    def test_http_429_honors_bounded_retry_after_seconds(self):
        from email.message import Message
        from urllib.error import HTTPError

        headers = Message()
        headers["Retry-After"] = "8"
        provider = OpenAICompatibleProvider(
            api_key="secret",
            opener=lambda request, timeout, context: (_ for _ in ()).throw(
                HTTPError("https://api.openai.com/v1", 429, "err", headers, None)
            ),
        )
        with self.assertRaises(ProviderError) as raised:
            provider.complete(ProviderRequest(prompt="private"))
        self.assertTrue(raised.exception.transient)
        self.assertEqual(raised.exception.retry_after_seconds, 8.0)


if __name__ == "__main__":
    unittest.main()
