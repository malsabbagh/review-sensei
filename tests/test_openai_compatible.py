import json
import unittest

from review_sensei.errors import ProviderError
from review_sensei.models import ProviderRequest
from review_sensei.providers.openai_compatible import OpenAICompatibleProvider


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


class OpenAICompatibleProviderTests(unittest.TestCase):
    def test_requires_explicit_api_key(self):
        with self.assertRaisesRegex(ValueError, "requires an API key"):
            OpenAICompatibleProvider(api_key=None)

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
            api_key="secret", opener=lambda request, timeout, context: _Response(b"{}")
        )
        with self.assertRaisesRegex(ProviderError, "did not contain review text"):
            provider.complete(ProviderRequest(prompt="private"))


if __name__ == "__main__":
    unittest.main()
