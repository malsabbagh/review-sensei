import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from review_sensei.errors import ProviderError
from review_sensei.models import ProviderRequest, ProviderResponse
from review_sensei.providers.fixture import FixtureProvider
from review_sensei.validation import ReviewLimits


class FixtureProviderTests(unittest.TestCase):
    def test_reads_bounded_utf8_response_and_normalizes_provider_model(self) -> None:
        payload = json.dumps({"summary": "Fixture review."})
        with tempfile.TemporaryDirectory() as temp_dir:
            response_path = Path(temp_dir) / "response.json"
            response_path.write_text(payload, encoding="utf-8")
            provider = FixtureProvider(response_path, model="fixture-v1")

            response = provider.complete(
                ProviderRequest(prompt="prompt", model="request-model")
            )

        self.assertIsInstance(response, ProviderResponse)
        self.assertEqual(response.text, payload)
        self.assertEqual(response.provider, "fixture")
        self.assertEqual(response.model, "request-model")

    def test_rejects_oversized_response_with_sanitized_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            response_path = Path(temp_dir) / "response.json"
            response_path.write_bytes(b"x" * 100)
            limits = ReviewLimits(max_provider_response_bytes=8)
            provider = FixtureProvider(response_path, model="fixture-v1")

            with self.assertRaises(ProviderError):
                provider.complete(ProviderRequest(prompt="prompt", limits=limits))

    def test_missing_response_path_fails_before_read(self) -> None:
        provider = FixtureProvider(Path("/definitely/missing/response.json"))

        with self.assertRaises(ProviderError):
            provider.complete(ProviderRequest(prompt="prompt"))

    def test_fixture_provider_does_not_read_api_key_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            response_path = Path(temp_dir) / "response.json"
            response_path.write_text("{}", encoding="utf-8")
            with patch.dict(os.environ, {"OLLAMA_API_KEY": "secret-key"}):
                provider = FixtureProvider(response_path, model="fixture-v1")
                response = provider.complete(ProviderRequest(prompt="prompt"))

        self.assertEqual(response.provider, "fixture")


if __name__ == "__main__":
    unittest.main()
