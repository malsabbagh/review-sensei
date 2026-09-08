import unittest

from review_sensei.errors import ProviderError
from review_sensei.models import ProviderRequest, ProviderResponse
from review_sensei.providers.registry import ProviderRegistry, ProviderSettings


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
