"""Shared provider configuration resolution for CLI and diagnostics."""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlparse

from .errors import ReviewInputError
from .providers.openrouter import (
    DEFAULT_OPENROUTER_BASE_URL,
    OpenRouterRoutingPolicy,
    is_allowlisted_openrouter_endpoint,
)
from .providers.profiles import ProviderProfile, get_provider_profile
from .validation import DEFAULT_REVIEW_LIMITS, validate_bounded_text

DEFAULT_LOCAL_MODEL = "qwen3.5:4b"
DEFAULT_CLOUD_MODEL = "deepseek-v4.1-flash:cloud"
DEFAULT_LOCAL_BASE_URL = "http://127.0.0.1:11434/api"
DEFAULT_CLOUD_BASE_URL = "https://ollama.com/api"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_OPENROUTER_MODEL = "deepseek/deepseek-v4.1-flash"
DEFAULT_OPENROUTER_UPSTREAM = "deepseek"
LOCAL_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
HOSTED_OPENROUTER_DEFAULTS = frozenset(
    {(DEFAULT_OPENROUTER_MODEL, DEFAULT_OPENROUTER_UPSTREAM)}
)
_OPENROUTER_MODEL_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*$")
_OLLAMA_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]+$")
_OPENROUTER_VENDORS = frozenset({"openai", "anthropic", "deepseek"})


def published_hosted_openrouter_models() -> frozenset[str]:
    """Return OpenRouter model slugs approved for hosted workflow defaults."""

    return frozenset(model for model, _ in HOSTED_OPENROUTER_DEFAULTS)


def validate_hosted_workflow_model(
    *,
    provider_mode: str,
    workflow_mode: str,
    model: str,
) -> None:
    """Validate a hosted workflow model string for the selected backend."""

    value = model.strip()
    if not value:
        return
    validate_bounded_text(
        value,
        DEFAULT_REVIEW_LIMITS.max_model_bytes,
        label="model",
        allow_empty=False,
    )
    if value.startswith("-"):
        raise ReviewInputError("model must not start with '-'")
    backend = _effective_hosted_backend(provider_mode, workflow_mode)
    if backend == "openrouter":
        if ":cloud" in value:
            raise ReviewInputError("openrouter model must not use ollama cloud suffix")
        if not _OPENROUTER_MODEL_PATTERN.fullmatch(value):
            raise ReviewInputError("openrouter model must be vendor/model slug")
        vendor = value.split("/", 1)[0]
        if vendor not in _OPENROUTER_VENDORS:
            raise ReviewInputError("openrouter model vendor is not allowlisted")
        return
    if "/" in value:
        raise ReviewInputError("ollama model must not use vendor/model openrouter slug")
    if not _OLLAMA_MODEL_PATTERN.fullmatch(value):
        raise ReviewInputError("ollama model slug is invalid")


def _effective_hosted_backend(provider_mode: str, workflow_mode: str) -> str:
    mode = provider_mode.strip().lower()
    if mode == "openrouter":
        return "openrouter"
    if mode in {"local", "local-ollama", "cloud", "cloud-ollama"}:
        return "ollama"
    if mode != "":
        raise ReviewInputError("provider mode is unsupported")
    wf = workflow_mode.strip().lower()
    if wf not in {"automatic", "manual"}:
        raise ReviewInputError("workflow mode is invalid")
    return "ollama"


def _positive_env_float(value: str, *, source: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ReviewInputError(f"{source} must be a positive number") from exc
    if parsed <= 0:
        raise ReviewInputError(f"{source} must be a positive number")
    return parsed


def openrouter_timeout_default() -> float:
    configured = os.getenv("REVIEWSENSEI_OPENROUTER_TIMEOUT_SECONDS")
    source = "REVIEWSENSEI_OPENROUTER_TIMEOUT_SECONDS"
    if configured is None:
        configured = os.getenv("OPENROUTER_TIMEOUT_SECONDS", "120")
        source = "OPENROUTER_TIMEOUT_SECONDS"
    return _positive_env_float(configured, source=source)


def openrouter_upstream_default() -> str:
    configured = os.getenv("OPENROUTER_UPSTREAM_PROVIDER", DEFAULT_OPENROUTER_UPSTREAM)
    if not isinstance(configured, str) or not configured.strip():
        raise ReviewInputError("OPENROUTER_UPSTREAM_PROVIDER must be non-empty")
    return configured.strip()


def openrouter_policy_from_env() -> OpenRouterRoutingPolicy:
    try:
        return OpenRouterRoutingPolicy(upstream_provider=openrouter_upstream_default())
    except ValueError as exc:
        raise ReviewInputError(str(exc)) from exc


def validate_profile_provider_match(
    *,
    profile_name: str,
    provider_name: str | None,
) -> None:
    """Reject mismatched profile and provider selections."""

    selected = get_provider_profile(profile_name)
    if provider_name is None:
        return
    normalized = provider_name.strip().lower()
    if not normalized:
        raise ReviewInputError("provider must be non-empty")
    if normalized != selected.provider:
        raise ReviewInputError(
            f"--provider {normalized} does not match profile "
            f"'{selected.name}' (requires {selected.provider})"
        )


def provider_mode_default(provider_mode: str | None) -> str:
    mode = (
        (
            provider_mode
            if provider_mode is not None
            else os.getenv("REVIEWSENSEI_PROVIDER_MODE", "local")
        )
        .strip()
        .lower()
    )
    if mode not in {"local", "cloud"}:
        raise ReviewInputError("provider mode must be local or cloud")
    return mode


def _default_ollama_base_url(provider_mode: str) -> str:
    configured = os.getenv("OLLAMA_BASE_URL")
    if configured:
        return configured
    return (
        DEFAULT_CLOUD_BASE_URL if provider_mode == "cloud" else DEFAULT_LOCAL_BASE_URL
    )


def _default_ollama_model(provider_mode: str) -> str:
    configured = os.getenv("OLLAMA_MODEL")
    if configured:
        return configured
    if provider_mode == "cloud":
        return os.getenv("REVIEWSENSEI_CLOUD_MODEL", DEFAULT_CLOUD_MODEL)
    return os.getenv("REVIEWSENSEI_LOCAL_MODEL", DEFAULT_LOCAL_MODEL)


def _is_loopback_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").strip().casefold()
    return host in LOCAL_LOOPBACK_HOSTS


def _inference_location(base_url: str) -> str:
    return "local" if _is_loopback_url(base_url) else "remote"


def _openrouter_policy_summary(
    policy: OpenRouterRoutingPolicy | None,
) -> dict[str, object] | None:
    if policy is None:
        return None
    return dict(policy.identity_fields())


def resolve_effective_provider_configuration(
    *,
    profile: str | None = None,
    provider: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    api_key_env: str | None = None,
    provider_mode: str | None = None,
) -> dict[str, Any]:
    """Return the effective provider configuration without reading secret values."""

    mode = provider_mode_default(provider_mode)
    selected_profile: ProviderProfile | None = None
    if profile is not None:
        if not isinstance(profile, str) or not profile.strip():
            raise ReviewInputError("profile must be a non-empty string")
        validate_profile_provider_match(profile_name=profile, provider_name=provider)
        selected_profile = get_provider_profile(profile)
        provider_name = selected_profile.provider
        resolved_model = selected_profile.model
        resolved_base_url = selected_profile.base_url
        credential_env = selected_profile.api_key_env
        qualification_status: str = selected_profile.qualification_status
        openrouter_policy = selected_profile.openrouter_policy
        timeout_seconds = selected_profile.timeout_seconds
    else:
        provider_name = (
            (
                provider
                if provider is not None
                else os.getenv("REVIEWSENSEI_PROVIDER", "ollama")
            )
            .strip()
            .lower()
        )
        if not provider_name:
            raise ReviewInputError("provider must be non-empty")
        if provider_name == "openai-compatible":
            resolved_base_url = (
                base_url or os.getenv("OPENAI_BASE_URL") or DEFAULT_OPENAI_BASE_URL
            ).strip()
            resolved_model = (
                model or os.getenv("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL
            ).strip()
            credential_env = api_key_env or "OPENAI_API_KEY"
            openrouter_policy = None
            timeout_seconds = None
        elif provider_name == "openrouter":
            resolved_base_url = (
                base_url
                or os.getenv("OPENROUTER_BASE_URL")
                or DEFAULT_OPENROUTER_BASE_URL
            ).strip()
            resolved_model = (
                model or os.getenv("OPENROUTER_MODEL") or DEFAULT_OPENROUTER_MODEL
            ).strip()
            credential_env = api_key_env or "OPENROUTER_API_KEY"
            openrouter_policy = openrouter_policy_from_env()
            timeout_seconds = openrouter_timeout_default()
        elif provider_name == "fixture":
            resolved_base_url = (base_url or "").strip()
            resolved_model = (model or "fixture-v1").strip()
            credential_env = None
            openrouter_policy = None
            timeout_seconds = None
        else:
            resolved_base_url = (base_url or _default_ollama_base_url(mode)).strip()
            resolved_model = (model or _default_ollama_model(mode)).strip()
            credential_env = api_key_env or "OLLAMA_API_KEY"
            openrouter_policy = None
            timeout_seconds = None
        qualification_status = "unknown"
    if selected_profile is not None:
        credential_required = selected_profile.requires_api_key
    elif provider_name in {"openrouter", "openai-compatible"}:
        credential_required = True
    else:
        credential_required = False
    credential_present = bool(credential_env and os.getenv(credential_env))
    if provider_name == "openrouter" and not is_allowlisted_openrouter_endpoint(
        resolved_base_url
    ):
        raise ReviewInputError("openrouter endpoint is not allowlisted")
    return {
        "profile": selected_profile.name if selected_profile is not None else None,
        "provider": provider_name,
        "model": resolved_model,
        "base_url": resolved_base_url,
        "execution_location": "local",
        "inference_location": _inference_location(resolved_base_url),
        "credential_env": credential_env,
        "credential_required": credential_required,
        "credential_present": credential_present,
        "openrouter_policy": _openrouter_policy_summary(openrouter_policy),
        "timeout_seconds": timeout_seconds,
        "qualification_status": qualification_status,
    }


__all__ = [
    "DEFAULT_OPENROUTER_MODEL",
    "DEFAULT_OPENROUTER_UPSTREAM",
    "HOSTED_OPENROUTER_DEFAULTS",
    "openrouter_policy_from_env",
    "openrouter_timeout_default",
    "openrouter_upstream_default",
    "provider_mode_default",
    "published_hosted_openrouter_models",
    "resolve_effective_provider_configuration",
    "validate_hosted_workflow_model",
    "validate_profile_provider_match",
]
