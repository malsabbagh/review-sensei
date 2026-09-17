import unittest
from types import SimpleNamespace

from review_sensei.errors import ProviderError
from review_sensei.models import ProviderRequest, ProviderResponse
from review_sensei.providers.openrouter import OpenRouterRoutingPolicy
from review_sensei.providers.profiles import (
    ProviderProfile,
    get_provider_profile,
    profile_names,
)
from review_sensei.providers.registry import (
    ProviderRegistry,
    ProviderSettings,
    default_registry,
)


class FakeProvider:
    name = "fake"
    model = "fake-model"

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        return ProviderResponse("{}", self.name, self.model)


class InvalidProvider:
    name = "invalid"
    model = "invalid-model"
    complete = 1


class MissingNameProvider:
    model = "missing-name"

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        return ProviderResponse("{}", "missing", self.model)


class BrokenNameProvider:
    @property
    def name(self) -> str:
        raise RuntimeError("boom")

    model = "broken-model"

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        return ProviderResponse("{}", "broken", self.model)


class BrokenCompleteProvider:
    name = "broken-complete"
    model = "broken-model"

    @property
    def complete(self):
        raise RuntimeError("boom")


class PropertyCompleteProvider:
    name = "property-complete"
    model = "property-model"

    @property
    def complete(self):
        def _complete(request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse("{}", self.name, self.model)

        return _complete


class ProviderRegistryTests(unittest.TestCase):
    def test_custom_provider_can_be_registered_without_changing_review_service(self):
        registry = ProviderRegistry()
        registry.register("fake", lambda settings: FakeProvider())

        provider = registry.create(ProviderSettings(name="FAKE"))

        self.assertIsInstance(provider, FakeProvider)

    def test_registry_rejects_non_callable_completion_boundary(self):
        registry = ProviderRegistry()
        registry.register("invalid", lambda settings: InvalidProvider())

        with self.assertRaisesRegex(ProviderError, "complete"):
            registry.create(ProviderSettings(name="invalid"))

    def test_registry_rejects_provider_missing_name_attribute(self):
        registry = ProviderRegistry()
        registry.register("missing-name", lambda settings: MissingNameProvider())

        with self.assertRaisesRegex(ProviderError, "name, model, and complete"):
            registry.create(ProviderSettings(name="missing-name"))

    def test_registry_rejects_provider_when_name_property_raises(self):
        registry = ProviderRegistry()
        registry.register("broken-name", lambda settings: BrokenNameProvider())

        with self.assertRaisesRegex(ProviderError, "name could not be read"):
            registry.create(ProviderSettings(name="broken-name"))

    def test_registry_rejects_provider_when_complete_property_raises(self):
        registry = ProviderRegistry()
        registry.register("broken-complete", lambda settings: BrokenCompleteProvider())

        with self.assertRaisesRegex(ProviderError, "complete could not be read"):
            registry.create(ProviderSettings(name="broken-complete"))

    def test_registry_accepts_complete_property_that_returns_callable(self):
        registry = ProviderRegistry()
        registry.register(
            "property-complete", lambda settings: PropertyCompleteProvider()
        )

        provider = registry.create(ProviderSettings(name="property-complete"))
        self.assertEqual(provider.name, "property-complete")

    def test_unknown_provider_fails_with_available_names(self):
        registry = ProviderRegistry()
        registry.register("fake", lambda settings: FakeProvider())

        with self.assertRaisesRegex(ProviderError, "Available providers: fake"):
            registry.create(ProviderSettings(name="missing"))

    def test_profiles_are_deterministic_and_local_profile_has_no_credential(self):
        self.assertEqual(
            profile_names(),
            (
                "deep-verification",
                "fast-triage",
                "local-private",
                "openrouter-gpt",
                "openrouter-sonnet",
            ),
        )
        self.assertEqual(get_provider_profile("local").name, "local-private")
        self.assertEqual(get_provider_profile("local/private").name, "local-private")
        settings = ProviderSettings.for_profile("local/private")
        provider = default_registry().create(settings)
        self.assertEqual(provider.name, "ollama")
        self.assertIsNone(settings.api_key)

    def test_profile_rejects_unknown_endpoint_scope_at_runtime(self):
        with self.assertRaisesRegex(ValueError, "endpoint_scope"):
            ProviderProfile(
                name="broken",
                provider="ollama",
                model="qwen3.5:4b",
                base_url="http://127.0.0.1:11434/api",
                endpoint_scope="invalid",
                timeout_seconds=1,
                max_output_tokens=1,
            )

    def test_credentialed_profile_requires_explicit_key(self):
        with self.assertRaisesRegex(ProviderError, "requires an explicit API key"):
            ProviderSettings.for_profile("fast-triage")
        settings = ProviderSettings.for_profile("fast-triage", api_key="test")
        self.assertEqual(settings.name, "openai-compatible")
        self.assertEqual(settings.model, "gpt-4o-mini")

    def test_local_profile_rejects_credential_forwarding(self):
        with self.assertRaisesRegex(ProviderError, "does not accept an API key"):
            ProviderSettings.for_profile("local-private", api_key="secret")

    def test_named_profile_rejects_endpoint_and_model_overrides(self):
        registry = default_registry()
        with self.assertRaisesRegex(ProviderError, "does not accept an API key"):
            registry.create(
                ProviderSettings(
                    name="ollama",
                    profile="local-private",
                    api_key="secret",
                )
            )
        with self.assertRaisesRegex(ProviderError, "requires an explicit API key"):
            registry.create(
                ProviderSettings(
                    name="openai-compatible",
                    profile="fast-triage",
                )
            )
        with self.assertRaisesRegex(ProviderError, "endpoint cannot be overridden"):
            registry.create(
                ProviderSettings(
                    name="ollama",
                    profile="deep-verification",
                    base_url="https://attacker.example/api",
                    api_key="secret",
                )
            )
        with self.assertRaisesRegex(ProviderError, "model cannot be overridden"):
            registry.create(
                ProviderSettings(
                    name="openai-compatible",
                    profile="fast-triage",
                    model="attacker-model",
                    api_key="secret",
                )
            )

    def test_profile_locks_model_and_output_budget(self):
        provider = default_registry().create(
            ProviderSettings.for_profile("deep-verification", api_key="secret")
        )
        self.assertEqual(provider.model, "deepseek-v4-flash:cloud")
        self.assertEqual(provider.max_output_tokens, 8192)

    def test_profile_rejects_explicit_default_values_that_conflict(self):
        # None is the only omitted-value marker.  A generic default must not
        # accidentally bypass the profile's timeout or output budget.
        with self.assertRaisesRegex(ProviderError, "timeout cannot be overridden"):
            default_registry().create(
                ProviderSettings(
                    name="openai-compatible",
                    profile="fast-triage",
                    api_key="secret",
                    timeout_seconds=900,
                )
            )
        with self.assertRaisesRegex(
            ProviderError, "output budget cannot be overridden"
        ):
            default_registry().create(
                ProviderSettings(
                    name="ollama",
                    profile="deep-verification",
                    api_key="secret",
                    max_output_tokens=2048,
                )
            )

    def test_profile_with_omitted_values_uses_canonical_budget(self):
        settings = ProviderSettings(
            name="openai-compatible",
            profile="fast-triage",
            api_key="secret",
        )
        provider = default_registry().create(settings)
        self.assertEqual(provider.timeout_seconds, 120)
        self.assertEqual(provider.max_output_tokens, 2048)

    def test_unprofiled_ollama_keeps_unbounded_output_budget(self):
        provider = default_registry().create(ProviderSettings(name="ollama"))
        self.assertIsNone(provider.max_output_tokens)

    def test_custom_endpoint_requires_explicit_registry_opt_in(self):
        with self.assertRaisesRegex(ValueError, "not allowlisted"):
            default_registry().create(
                ProviderSettings(
                    name="openai-compatible",
                    base_url="https://example.test/v1",
                    api_key="secret",
                )
            )

    def test_profile_cli_settings_with_omitted_values_use_canonical_budget(self):
        from review_sensei.cli import _provider_settings_from_args

        args = SimpleNamespace(
            provider="openai-compatible",
            profile="fast-triage",
            model="gpt-4o-mini",
            base_url="https://api.openai.com/v1",
            timeout_seconds=120.0,
            allow_custom_endpoint=False,
            api_key_env="OLLAMA_API_KEY",
        )
        settings = _provider_settings_from_args(
            args,
            api_key="secret",
            argv=[
                "--profile",
                "fast-triage",
                "--provider",
                "openai-compatible",
            ],
        )
        provider = default_registry().create(settings)
        self.assertEqual(provider.model, "gpt-4o-mini")
        self.assertEqual(provider.timeout_seconds, 120)
        self.assertEqual(provider.max_output_tokens, 2048)

    def test_profile_rejects_allow_custom_endpoint(self):
        with self.assertRaisesRegex(
            ProviderError, "allow_custom_endpoint cannot be used"
        ):
            default_registry().create(
                ProviderSettings(
                    name="openai-compatible",
                    profile="fast-triage",
                    api_key="secret",
                    allow_custom_endpoint=True,
                )
            )

    def test_openrouter_profile_requires_explicit_key_and_policy(self):
        with self.assertRaisesRegex(ProviderError, "requires an explicit API key"):
            ProviderSettings.for_profile("openrouter-sonnet")
        settings = ProviderSettings.for_profile(
            "openrouter-sonnet", api_key="router-secret"
        )
        self.assertEqual(settings.name, "openrouter")
        self.assertEqual(settings.model, "anthropic/claude-3.5-sonnet")
        self.assertEqual(
            settings.openrouter_policy,
            OpenRouterRoutingPolicy(upstream_provider="anthropic"),
        )
        provider = default_registry().create(settings)
        self.assertEqual(provider.name, "openrouter")
        self.assertEqual(provider.routing_policy.upstream_provider, "anthropic")

    def test_openrouter_profile_rejects_routing_policy_override(self):
        with self.assertRaisesRegex(
            ProviderError, "routing policy cannot be overridden"
        ):
            default_registry().create(
                ProviderSettings(
                    name="openrouter",
                    profile="openrouter-sonnet",
                    api_key="secret",
                    openrouter_policy=OpenRouterRoutingPolicy(
                        upstream_provider="openai"
                    ),
                )
            )

    def test_openrouter_profile_is_unqualified(self):
        profile = get_provider_profile("openrouter-sonnet")
        self.assertEqual(profile.qualification_status, "unqualified")

    def test_openrouter_unprofiled_policy_must_match_env(self):
        with self.assertRaisesRegex(
            ProviderError, "does not match OPENROUTER_UPSTREAM_PROVIDER"
        ):
            default_registry().create(
                ProviderSettings(
                    name="openrouter",
                    model="anthropic/claude-3.5-sonnet",
                    api_key="secret",
                    openrouter_policy=OpenRouterRoutingPolicy(
                        upstream_provider="openai"
                    ),
                )
            )
