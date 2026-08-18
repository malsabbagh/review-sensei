import json
import unittest

from review_sensei.errors import ProviderError
from review_sensei.models import ProviderRequest
from review_sensei.providers.ollama import OllamaProvider
from review_sensei.validation import ReviewLimits


class FakeResponse:
    def __init__(self, body):
        self.body = body
        self.read_sizes = []
        self.exhausted = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, size):
        self.read_sizes.append(size)
        if self.exhausted:
            return b""
        self.exhausted = True
        return self.body[:size]


class UnsizedOnlyResponse(FakeResponse):
    def read(self):
        return self.body


class SizedFakeResponse(FakeResponse):
    def __init__(self, body):
        super().__init__(body)
        self.requested = []

    def read(self, size):
        self.requested.append(size)
        if self.exhausted:
            return b""
        self.exhausted = True
        return self.body[:size]


class ShortReadFakeResponse:
    def __init__(self, body, chunk_size):
        self.body = body
        self.chunk_size = chunk_size
        self.offset = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, size):
        end = min(self.offset + self.chunk_size, len(self.body))
        chunk = self.body[self.offset : end]
        self.offset = end
        return chunk


class OllamaProviderTests(unittest.TestCase):
    def test_sends_non_streaming_json_review_request(self):
        captured = {}
        raw_response = FakeResponse(
            b'{"response":"{\\"summary\\":\\"ok\\",\\"comments\\":[]}"}'
        )

        def opener(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return raw_response

        provider = OllamaProvider(
            base_url="https://ollama.example/api",
            model="review-model",
            api_key="test-secret",
            timeout_seconds=42,
            opener=opener,
        )

        response = provider.complete(ProviderRequest(prompt="review this", model=None))
        payload = json.loads(captured["request"].data)

        self.assertEqual(response.text, '{"summary":"ok","comments":[]}')
        self.assertEqual(
            captured["request"].full_url, "https://ollama.example/api/generate"
        )
        self.assertEqual(captured["timeout"], 42)
        self.assertEqual(payload["model"], "review-model")
        self.assertEqual(payload["prompt"], "review this")
        self.assertFalse(payload["stream"])
        self.assertFalse(payload["think"])
        self.assertEqual(payload["format"], "json")
        self.assertEqual(
            captured["request"].headers["Authorization"], "Bearer test-secret"
        )
        max_bytes = ProviderRequest(prompt="x").max_response_bytes
        self.assertEqual(
            raw_response.read_sizes,
            [max_bytes + 1, max_bytes + 1 - len(raw_response.body)],
        )

    def test_api_key_none_sends_no_authorization_header(self):
        captured = {}

        def opener(request, timeout):
            captured["request"] = request
            return FakeResponse(b'{"response":"ok"}')

        provider = OllamaProvider(
            base_url="http://127.0.0.1:11434/api",
            model="local-model",
            api_key=None,
            opener=opener,
        )
        provider.complete(ProviderRequest(prompt="review this", model=None))
        self.assertNotIn("Authorization", captured["request"].headers)

    def test_unsized_response_read_fails_without_an_unbounded_retry(self):
        provider = OllamaProvider(
            opener=lambda request, timeout: UnsizedOnlyResponse(b"{}")
        )
        with self.assertRaises(ProviderError) as raised:
            provider.complete(ProviderRequest(prompt="private prompt"))
        self.assertEqual(str(raised.exception), "Ollama request failed")

    def test_effective_constructor_model_respects_request_model_limit_before_open(self):
        opened = []

        def opener(request, timeout):
            opened.append(request)
            return SizedFakeResponse(b'{"response":"ok"}')

        limits = ReviewLimits(max_model_bytes=3)
        provider = OllamaProvider(model="model-too-long", opener=opener)
        with self.assertRaises(ProviderError) as raised:
            provider.complete(ProviderRequest(prompt="review", limits=limits))
        self.assertEqual(
            str(raised.exception), "Ollama model exceeds the configured size limit"
        )
        self.assertEqual(opened, [])

    def test_surfaces_provider_transport_errors_without_exposing_prompt(self):
        def opener(request, timeout):
            raise TimeoutError("timed out")

        with self.assertRaisesRegex(ProviderError, "Ollama request timed out"):
            OllamaProvider(opener=opener).complete(
                ProviderRequest(prompt="private diff contents", model=None)
            )

    def test_reads_exactly_one_byte_beyond_the_response_ceiling(self):
        response = SizedFakeResponse(b'{"response":"ok"}')

        def opener(request, timeout):
            return response

        limits = ReviewLimits(max_provider_response_bytes=64)
        provider = OllamaProvider(opener=opener)
        provider.complete(
            ProviderRequest(
                prompt="review",
                max_response_bytes=limits.max_provider_response_bytes,
                limits=limits,
            )
        )
        self.assertEqual(response.requested, [65, 65 - len(response.body)])

    def test_short_reads_accumulate_to_the_response_ceiling(self):
        body = b'{"response":"ok"}'
        response = ShortReadFakeResponse(body, 4)

        def opener(request, timeout):
            return response

        limits = ReviewLimits(max_provider_response_bytes=64)
        provider = OllamaProvider(opener=opener)
        result = provider.complete(
            ProviderRequest(
                prompt="review",
                max_response_bytes=limits.max_provider_response_bytes,
                limits=limits,
            )
        )
        self.assertEqual(result.text, "ok")
        self.assertEqual(response.offset, len(body))

    def test_short_reads_reject_an_oversized_response_after_eof(self):
        body = b'{"response":"ok"}'
        response = ShortReadFakeResponse(body, 4)

        def opener(request, timeout):
            return response

        limits = ReviewLimits(max_provider_response_bytes=8)
        provider = OllamaProvider(opener=opener)
        with self.assertRaises(ProviderError) as raised:
            provider.complete(
                ProviderRequest(
                    prompt="review",
                    max_response_bytes=limits.max_provider_response_bytes,
                    limits=limits,
                )
            )
        self.assertEqual(
            str(raised.exception), "Ollama response exceeded the configured size limit"
        )

    def test_oversize_invalid_utf8_and_malformed_envelope_are_sanitized(self):
        marker = "PRIVATE_RESPONSE_MARKER"
        bodies = (
            b'{"response":"' + (b"x" * 20) + b'"}',
            b"\xff\xfe",
            b'{"error":"' + marker.encode() + b'"}',
        )
        limits = ReviewLimits(max_provider_response_bytes=8)
        for body in bodies:
            with self.subTest(body=body):
                provider = OllamaProvider(
                    opener=lambda request, timeout, body=body: SizedFakeResponse(body)
                )
                with self.assertRaises(ProviderError) as raised:
                    provider.complete(
                        ProviderRequest(
                            prompt=marker,
                            max_response_bytes=limits.max_provider_response_bytes,
                            limits=limits,
                        )
                    )
                self.assertNotIn(marker, str(raised.exception))
