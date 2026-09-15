from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from ..errors import ProviderError
from .base import ReviewProvider, validate_provider_contract
from .fixture import FixtureProvider
from .ollama import OllamaProvider
from .openai_compatible import OpenAICompatibleProvider
from .profiles import ProviderProfile, get_provider_profile


@dataclass(frozen=True)
class ProviderSettings:
    name: str
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    fixture_response: Path | None = None
    timeout_seconds: float | None = None
    max_output_tokens: int | None = None
    profile: str | None = None
    allow_custom_endpoint: bool = False

    @classmethod
    def for_profile(
        cls, profile: str, *, api_key: str | None = None
    ) -> "ProviderSettings":
        """Build settings from a named profile without reading the environment."""

        selected = get_provider_profile(profile)
        if selected.requires_api_key and (
            not isinstance(api_key, str) or not api_key.strip()
        ):
            raise ProviderError(
                f"provider profile '{selected.name}' requires an explicit API key"
            )
        if not selected.requires_api_key and api_key is not None:
            raise ProviderError(
                f"provider profile '{selected.name}' does not accept an API key"
            )
        return cls(
            name=selected.provider,
            model=selected.model,
            base_url=selected.base_url,
            api_key=api_key,
            timeout_seconds=selected.timeout_seconds,
            max_output_tokens=selected.max_output_tokens,
            profile=selected.name,
        )


ProviderFactory = Callable[[ProviderSettings], ReviewProvider]


class ProviderRegistry:
    """Explicit provider factory registry for future adapters."""

    def __init__(self) -> None:
        self._factories: dict[str, ProviderFactory] = {}

    def register(self, name: str, factory: ProviderFactory) -> None:
        normalized = name.strip().lower()
        if not normalized:
            raise ValueError("provider name must be non-empty")
        self._factories[normalized] = factory

    def create(self, settings: ProviderSettings) -> ReviewProvider:
        if settings.profile is not None:
            profile = get_provider_profile(settings.profile)
            if settings.allow_custom_endpoint:
                raise ProviderError(
                    "allow_custom_endpoint cannot be used with a named provider profile"
                )
            if settings.name.strip().lower() != profile.provider:
                raise ProviderError(
                    "provider profile and provider name must select the same adapter"
                )
            if profile.requires_api_key and (
                not isinstance(settings.api_key, str) or not settings.api_key.strip()
            ):
                raise ProviderError(
                    f"provider profile '{profile.name}' requires an explicit API key"
                )
            if not profile.requires_api_key and settings.api_key is not None:
                raise ProviderError(
                    f"provider profile '{profile.name}' does not accept an API key"
                )
            # A named profile is an immutable routing and budget policy.  None
            # means that the caller omitted a value; every explicit value must
            # match the canonical profile before the settings are replaced by
            # the profile's complete, canonical values below.
            if settings.model is not None and settings.model != profile.model:
                raise ProviderError("provider profile model cannot be overridden")
            if settings.base_url is not None and settings.base_url != profile.base_url:
                raise ProviderError("provider profile endpoint cannot be overridden")
            if (
                settings.timeout_seconds is not None
                and settings.timeout_seconds != profile.timeout_seconds
            ):
                raise ProviderError("provider profile timeout cannot be overridden")
            if (
                settings.max_output_tokens is not None
                and settings.max_output_tokens != profile.max_output_tokens
            ):
                raise ProviderError(
                    "provider profile output budget cannot be overridden"
                )
            settings = replace(
                settings,
                name=profile.provider,
                model=profile.model,
                base_url=profile.base_url,
                timeout_seconds=profile.timeout_seconds,
                max_output_tokens=profile.max_output_tokens,
                profile=profile.name,
                allow_custom_endpoint=False,
            )
        name = settings.name.strip().lower()
        try:
            factory = self._factories[name]
        except KeyError as exc:
            available = ", ".join(sorted(self._factories)) or "none"
            raise ProviderError(
                f"Unknown provider '{settings.name}'. Available providers: {available}"
            ) from exc
        return validate_provider_contract(factory(settings))


def default_registry() -> ProviderRegistry:
    registry = ProviderRegistry()

    def ollama_factory(settings: ProviderSettings) -> ReviewProvider:
        return OllamaProvider(
            base_url=settings.base_url or "http://127.0.0.1:11434/api",
            model=settings.model or "qwen3.5:4b",
            api_key=settings.api_key,
            timeout_seconds=(
                settings.timeout_seconds
                if settings.timeout_seconds is not None
                else 900
            ),
            max_output_tokens=settings.max_output_tokens,
            allow_model_override=settings.profile is None,
        )

    registry.register("ollama", ollama_factory)

    def openai_compatible_factory(settings: ProviderSettings) -> ReviewProvider:
        if settings.api_key is None:
            raise ProviderError("openai-compatible provider requires an API key")
        return OpenAICompatibleProvider(
            base_url=settings.base_url or "https://api.openai.com/v1",
            model=settings.model or "gpt-4o-mini",
            api_key=settings.api_key,
            timeout_seconds=(
                settings.timeout_seconds
                if settings.timeout_seconds is not None
                else 120
            ),
            max_output_tokens=(
                settings.max_output_tokens
                if settings.max_output_tokens is not None
                else 2048
            ),
            allow_model_override=settings.profile is None,
            allow_custom_endpoint=settings.allow_custom_endpoint,
        )

    registry.register("openai-compatible", openai_compatible_factory)

    def fixture_factory(settings: ProviderSettings) -> ReviewProvider:
        if settings.fixture_response is None:
            raise ProviderError("fixture provider requires --fixture-response")
        return FixtureProvider(
            response_path=settings.fixture_response,
            model=settings.model or "fixture-v1",
        )

    registry.register("fixture", fixture_factory)
    return registry


__all__ = [
    "ProviderProfile",
    "ProviderRegistry",
    "ProviderSettings",
    "default_registry",
    "get_provider_profile",
]
