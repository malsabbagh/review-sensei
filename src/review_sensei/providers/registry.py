from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..errors import ProviderError
from .base import ReviewProvider
from .fixture import FixtureProvider
from .ollama import OllamaProvider


@dataclass(frozen=True)
class ProviderSettings:
    name: str
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    fixture_response: Path | None = None
    timeout_seconds: float = 900


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
        name = settings.name.strip().lower()
        try:
            factory = self._factories[name]
        except KeyError as exc:
            available = ", ".join(sorted(self._factories)) or "none"
            raise ProviderError(
                f"Unknown provider '{settings.name}'. Available providers: {available}"
            ) from exc
        return factory(settings)


def default_registry() -> ProviderRegistry:
    registry = ProviderRegistry()

    def ollama_factory(settings: ProviderSettings) -> ReviewProvider:
        return OllamaProvider(
            base_url=settings.base_url or "http://127.0.0.1:11434/api",
            model=settings.model or "qwen3.5:4b",
            api_key=settings.api_key,
            timeout_seconds=settings.timeout_seconds,
        )

    registry.register("ollama", ollama_factory)

    def fixture_factory(settings: ProviderSettings) -> ReviewProvider:
        if settings.fixture_response is None:
            raise ProviderError("fixture provider requires --fixture-response")
        return FixtureProvider(
            response_path=settings.fixture_response,
            model=settings.model or "fixture-v1",
        )

    registry.register("fixture", fixture_factory)
    return registry
