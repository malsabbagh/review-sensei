"""Deterministic per-stage provider profile routing.

A run selects at most one named profile.  A stage may pin the same profile or a
narrower local/private profile.  Routing never falls back to a remote endpoint
from a local run, never switches adapters, and never forwards one profile's
credential to another endpoint.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from ..errors import ProviderError
from ..stages import Stage
from .base import ReviewProvider
from .profiles import get_provider_profile
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


def bind_stage_providers(
    *,
    registry: ProviderRegistry,
    settings: ProviderSettings,
    stages: Sequence[Stage],
) -> tuple[ReviewProvider, Mapping[str, ReviewProvider]]:
    """Create the run provider and any stage-specific providers.

    Stage-specific providers are created only when the resolved profile or
    model differs from the run default.  Narrowing to ``local-private`` builds
    a new credential-free adapter; the run credential is never reused.
    """

    if settings.name.strip().lower() == "fixture" and any(
        stage.provider_profile for stage in stages
    ):
        raise ProviderError(
            "fixture provider cannot be combined with stage provider_profile"
        )
    default_provider = registry.create(settings)
    run_profile_name = (
        get_provider_profile(settings.profile).name
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
        if resolved != run_profile_name:
            # Narrowing to local/private must not receive the remote credential.
            mapping[stage.name] = registry.create(
                ProviderSettings.for_profile(resolved)
            )
            continue
        mapping[stage.name] = registry.create(replace(settings, model=model))
    return default_provider, mapping
