"""Deterministic, named provider profiles.

Profiles are configuration presets, not failover routes.  Selecting one chooses
exactly one provider endpoint and model; callers must provide a credential when
the profile requires one.  Optional per-stage models stay on that same adapter
and endpoint.  No environment lookup happens in this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Mapping

from ..errors import ProviderError, UnknownProviderProfileError
from .openrouter import (
    DEFAULT_OPENROUTER_BASE_URL,
    OpenRouterRoutingPolicy,
)

EndpointScope = Literal["local", "remote"]
StructuredOutput = Literal["json_object"]
FallbackPolicy = Literal["none"]
QualificationStatus = Literal["qualified", "unqualified"]


@dataclass(frozen=True)
class ProviderProfile:
    """A bounded provider preset with explicit endpoint/credential policy."""

    name: str
    provider: str
    model: str
    base_url: str
    endpoint_scope: EndpointScope
    timeout_seconds: float
    max_output_tokens: int
    api_key_env: str | None = None
    requires_api_key: bool = False
    structured_output: StructuredOutput = "json_object"
    permitted_fallback: FallbackPolicy = "none"
    stage_models: tuple[tuple[str, str], ...] = ()
    openrouter_policy: OpenRouterRoutingPolicy | None = None
    qualification_status: QualificationStatus = "qualified"
    _stage_models_map: dict[str, str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.provider.strip() or not self.model.strip():
            raise ValueError("provider profile identifiers must be non-empty")
        if not self.base_url.strip():
            raise ValueError("provider profile base_url must be non-empty")
        if self.endpoint_scope not in ("local", "remote"):
            raise ValueError(
                "provider profile endpoint_scope must be 'local' or 'remote'"
            )
        if self.timeout_seconds <= 0:
            raise ValueError("provider profile timeout_seconds must be positive")
        if isinstance(self.max_output_tokens, bool) or self.max_output_tokens < 1:
            raise ValueError("provider profile max_output_tokens must be positive")
        if self.requires_api_key and not self.api_key_env:
            raise ValueError("credentialed provider profiles require api_key_env")
        if self.endpoint_scope == "remote" and not self.base_url.lower().startswith(
            "https://"
        ):
            raise ValueError("remote provider profiles require an HTTPS endpoint")
        if self.structured_output != "json_object":
            raise ValueError("provider profile structured_output must be 'json_object'")
        if self.permitted_fallback != "none":
            raise ValueError("provider profiles do not permit fallback")
        if self.qualification_status not in ("qualified", "unqualified"):
            raise ValueError(
                "provider profile qualification_status must be "
                "'qualified' or 'unqualified'"
            )
        if self.provider == "openrouter":
            if self.openrouter_policy is None:
                raise ValueError(
                    "openrouter provider profiles require openrouter_policy"
                )
        elif self.openrouter_policy is not None:
            raise ValueError(
                "openrouter_policy is only valid for openrouter provider profiles"
            )
        self._validate_stage_models()

    def _validate_stage_models(self) -> None:
        """Ensure ``stage_models`` keys match ``Stage.name`` values exactly."""
        if not isinstance(self.stage_models, tuple):
            raise ValueError("provider profile stage_models must be a tuple")
        seen: set[str] = set()
        for item in self.stage_models:
            if (
                not isinstance(item, tuple)
                or len(item) != 2
                or not isinstance(item[0], str)
                or not isinstance(item[1], str)
            ):
                raise ValueError(
                    "provider profile stage_models must be name/model pairs"
                )
            stage_name, model = item[0].strip(), item[1].strip()
            if not stage_name or not model:
                raise ValueError("provider profile stage_models must be non-empty")
            if stage_name in seen:
                raise ValueError("provider profile stage_models must be unique")
            seen.add(stage_name)
        object.__setattr__(
            self,
            "_stage_models_map",
            {name: model for name, model in self.stage_models},
        )

    def model_for_stage(self, stage_name: str) -> str:
        """Return the profile model for ``stage_name``, defaulting to the run model."""

        if not isinstance(stage_name, str) or not stage_name.strip():
            raise ValueError("stage name must be a non-empty string")
        mapping: Mapping[str, str] = self._stage_models_map
        return mapping.get(stage_name.strip(), self.model)

    def allowed_models(self) -> frozenset[str]:
        """Return the closed set of models this profile may select."""

        return frozenset((self.model, *(model for _, model in self.stage_models)))

    def policy_identity_fields(self) -> Mapping[str, object] | None:
        """Return non-secret routing policy fields for configuration digests."""

        if self.openrouter_policy is None:
            return None
        return dict(self.openrouter_policy.identity_fields())


PROVIDER_PROFILES: dict[str, ProviderProfile] = {
    "local-private": ProviderProfile(
        name="local-private",
        provider="ollama",
        model="qwen3.5:4b",
        base_url="http://127.0.0.1:11434/api",
        endpoint_scope="local",
        timeout_seconds=900,
        max_output_tokens=4096,
    ),
    "fast-triage": ProviderProfile(
        name="fast-triage",
        provider="openai-compatible",
        model="gpt-4o-mini",
        base_url="https://api.openai.com/v1",
        endpoint_scope="remote",
        timeout_seconds=120,
        max_output_tokens=2048,
        api_key_env="OPENAI_API_KEY",
        requires_api_key=True,
    ),
    # deepseek-v4-flash:cloud is the documented Ollama Cloud default across README,
    # workflows, and installation docs; it requires a provisioned Ollama Cloud
    # account and OLLAMA_API_KEY rather than a local model pull.
    "deep-verification": ProviderProfile(
        name="deep-verification",
        provider="ollama",
        model="deepseek-v4-flash:cloud",
        base_url="https://ollama.com/api",
        endpoint_scope="remote",
        timeout_seconds=900,
        max_output_tokens=8192,
        api_key_env="OLLAMA_API_KEY",
        requires_api_key=True,
    ),
    "openrouter-sonnet": ProviderProfile(
        name="openrouter-sonnet",
        provider="openrouter",
        model="anthropic/claude-3.5-sonnet",
        base_url=DEFAULT_OPENROUTER_BASE_URL,
        endpoint_scope="remote",
        timeout_seconds=120,
        max_output_tokens=4096,
        api_key_env="OPENROUTER_API_KEY",
        requires_api_key=True,
        openrouter_policy=OpenRouterRoutingPolicy(upstream_provider="anthropic"),
        qualification_status="unqualified",
    ),
    "openrouter-gpt": ProviderProfile(
        name="openrouter-gpt",
        provider="openrouter",
        model="openai/gpt-4o-mini",
        base_url=DEFAULT_OPENROUTER_BASE_URL,
        endpoint_scope="remote",
        timeout_seconds=120,
        max_output_tokens=2048,
        api_key_env="OPENROUTER_API_KEY",
        requires_api_key=True,
        openrouter_policy=OpenRouterRoutingPolicy(upstream_provider="openai"),
        qualification_status="unqualified",
    ),
}

_ALIASES = {
    "local": "local-private",
    "private": "local-private",
    "local/private": "local-private",
}


def canonical_profile_name(name: str) -> str:
    """Return the canonical profile name, resolving aliases."""

    return get_provider_profile(name).name


def get_provider_profile(name: str) -> ProviderProfile:
    """Return a canonical profile, rejecting unknown names before any call."""

    if not isinstance(name, str) or not name.strip():
        raise ProviderError("provider profile must be a non-empty name")
    key = name.strip().lower()
    key = _ALIASES.get(key, key)
    try:
        return PROVIDER_PROFILES[key]
    except KeyError as exc:
        available = ", ".join(sorted(PROVIDER_PROFILES))
        raise UnknownProviderProfileError(
            f"Unknown provider profile '{name}'. Available profiles: {available}"
        ) from exc


def profile_names() -> tuple[str, ...]:
    """Return canonical profile names in deterministic order."""

    return tuple(sorted(PROVIDER_PROFILES))
