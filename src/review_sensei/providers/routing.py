"""Deterministic per-stage provider profile routing.

A run selects at most one named profile.  A stage may pin the same profile or a
narrower local/private profile.  Routing never falls back to a remote endpoint
from a local run and never forwards one profile's credential to another
endpoint.  Per-stage model overrides stay on the same adapter and endpoint as
the named profile that declares them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from ..errors import ProviderError
from ..stages import Stage
from .base import ReviewProvider
from .profiles import canonical_profile_name, get_provider_profile
from .registry import ProviderRegistry, ProviderSettings


def resolve_stage_profile_name(
    *,
    run_profile: str | None,
    stage_profile: str | None,
) -> str | None:
    """Return the canonical profile used for one stage.

    ``None`` means the run has no named profile and the stage inherits the
    unprofiled provider.  Local/private and unprofiled runs cannot select a
    remote stage profile.  Remote runs may narrow to ``local-private``.
    """

    if run_profile is not None:
        run_profile = canonical_profile_name(run_profile)
    if stage_profile is None:
        return run_profile
    selected = get_provider_profile(stage_profile)
    if run_profile is None:
        if selected.endpoint_scope == "remote":
            raise ProviderError(
                "unprofiled local runs cannot select a remote stage profile"
            )
        return selected.name
    run = get_provider_profile(run_profile)
    if selected.name == run.name:
        return selected.name
    if run.endpoint_scope == "local" and selected.endpoint_scope == "remote":
        raise ProviderError(
            "local/private profile cannot fall back to a remote stage profile"
        )
    if selected.endpoint_scope == "local":
        return selected.name
    if selected.provider != run.provider or selected.base_url != run.base_url:
        raise ProviderError("stage profile cannot switch providers")
    if selected.api_key_env != run.api_key_env:
        raise ProviderError("stage profile cannot switch credentials")
    return selected.name


def _run_uses_fixture_provider(settings: ProviderSettings) -> bool:
    return (
        settings.name.strip().lower() == "fixture"
        or settings.fixture_response is not None
    )


def _stage_provider_settings(
    *,
    resolved: str,
    model: str,
    api_key: str | None,
) -> ProviderSettings:
    """Build canonical profile settings for one stage provider."""

    selected = get_provider_profile(resolved)
    settings = ProviderSettings.for_profile(resolved, api_key=api_key)
    if model != selected.model:
        settings = replace(settings, model=model, allow_profile_stage_model=True)
    return settings


def bind_stage_providers(
    *,
    registry: ProviderRegistry,
    settings: ProviderSettings,
    stages: Sequence[Stage],
) -> tuple[ReviewProvider, Mapping[str, ReviewProvider]]:
    """Create the run provider and any stage-specific providers.

    The default provider serves every stage unless the stage explicitly sets
    ``provider_profile`` or the run profile declares a per-stage model that
    differs from the run default.  Profile narrowing always builds a fresh
    credential-free adapter from ``ProviderSettings.for_profile``; per-stage
    providers on the same profile re-derive canonical profile settings and only
    reuse the run ``api_key`` when the profile requires one.
    """

    if _run_uses_fixture_provider(settings) and any(
        stage.provider_profile for stage in stages
    ):
        raise ProviderError(
            "fixture provider cannot be combined with stage provider_profile"
        )
    if any(
        stage.provider_profile is not None
        and stage.provider_profile.strip().lower() == "fixture"
        for stage in stages
    ):
        raise ProviderError(
            "fixture provider cannot be combined with stage provider_profile"
        )
    default_provider = registry.create(settings)
    run_profile_name = (
        canonical_profile_name(settings.profile)
        if settings.profile is not None
        else None
    )
    mapping: dict[str, ReviewProvider] = {}
    for stage in stages:
        resolved = resolve_stage_profile_name(
            run_profile=settings.profile,
            stage_profile=stage.provider_profile,
        )
        if resolved is None:
            continue
        profile = get_provider_profile(resolved)
        model = profile.model_for_stage(stage.name)
        default_model = default_provider.model or profile.model
        if resolved == run_profile_name and model == default_model:
            continue
        if stage.provider_profile is not None:
            if resolved != run_profile_name:
                mapping[stage.name] = registry.create(
                    ProviderSettings.for_profile(resolved)
                )
                continue
            if model != default_model:
                mapping[stage.name] = registry.create(
                    _stage_provider_settings(
                        resolved=resolved,
                        model=model,
                        api_key=settings.api_key,
                    )
                )
            continue
        if model != profile.model:
            mapping[stage.name] = registry.create(
                _stage_provider_settings(
                    resolved=resolved,
                    model=model,
                    api_key=settings.api_key,
                )
            )
    return default_provider, mapping
