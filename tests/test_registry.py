import unittest

from review_sensei.errors import ProviderError
from review_sensei.models import ProviderRequest, ProviderResponse
from review_sensei.providers.registry import (
    ProviderRegistry,
    ProviderSettings,
    default_registry,
)
from review_sensei.providers.profiles import get_provider_profile, profile_names


class FakeProvider:
    name = "fake"
    model = "fake-model"

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        return ProviderResponse("{}", self.name, self.model)


class ProviderRegistryTests(unittest.TestCase):
    def test_custom_provider_can_be_registered_without_changing_review_service(self):
        registry = ProviderRegistry()
        registry.register("fake", lambda settings: FakeProvider())

        provider = registry.create(ProviderSettings(name="FAKE"))

        self.assertIsInstance(provider, FakeProvider)

    def test_unknown_provider_fails_with_available_names(self):
        registry = ProviderRegistry()
        registry.register("fake", lambda settings: FakeProvider())

        with self.assertRaisesRegex(ProviderError, "Available providers: fake"):
            registry.create(ProviderSettings(name="missing"))

    def test_profiles_are_deterministic_and_local_profile_has_no_credential(self):
        self.assertEqual(
            profile_names(), ("deep-verification", "fast-triage", "local-private")
        )
        self.assertEqual(get_provider_profile("local").name, "local-private")
        settings = ProviderSettings.for_profile("local/private")
        provider = default_registry().create(settings)
        self.assertEqual(provider.name, "ollama")
        self.assertIsNone(settings.api_key)

    def test_credentialed_profile_requires_explicit_key(self):
        with self.assertRaisesRegex(ProviderError, "requires an explicit API key"):
            ProviderSettings.for_profile("fast-triage")
        settings = ProviderSettings.for_profile("fast-triage", api_key="test")
        self.assertEqual(settings.name, "openai-compatible")
        self.assertEqual(settings.model, "gpt-4o-mini")

    def test_local_profile_rejects_credential_forwarding(self):
        with self.assertRaisesRegex(ProviderError, "does not accept an API key"):
            ProviderSettings.for_profile("local-private", api_key="secret")
