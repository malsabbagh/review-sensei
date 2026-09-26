"""Hosted execution planning: trusted policy and runner requirements.

The reusable workflow resolves one read-only plan before it selects a runner or
injects a backend credential. The plan is the canonical configuration — the
repository-root ``.reviewsensei.yml`` at the trusted base-policy commit, the two
optional Actions overrides mapped to ``REVIEWSENSEI_PROVIDER`` and
``REVIEWSENSEI_MODEL``, and the documented packaged defaults — projected into
the non-secret values the Actions layer needs:

* the effective backend/model/endpoint/routing identity,
* the runner requirement (``hosted`` or ``local``) so backend and execution
  environment agree,
* the single named credential the selected adapter reads,
* the GitHub policy fields that decide whether a job runs at all.

Nothing here writes to a host: the command that renders the plan is invoked
explicitly by the workflow, which owns ``$GITHUB_OUTPUT``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .configuration import (
    ConfigurationError,
    ProductConfiguration,
    Provenance,
    ResolvedInference,
    default_configuration,
    load_configuration,
    resolve_inference,
)

RUNNER_KINDS = ("hosted", "local")
_OUTPUT_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class HostedPolicy:
    """The trusted GitHub policy for one hosted plan."""

    automatic_reviews: bool
    writes: bool
    reviews: str
    mentions: bool
    learning: str
    artifacts: str

    def to_dict(self) -> dict[str, object]:
        return {
            "automatic_reviews": self.automatic_reviews,
            "writes": self.writes,
            "reviews": self.reviews,
            "mentions": self.mentions,
            "learning": self.learning,
            "artifacts": self.artifacts,
        }


@dataclass(frozen=True)
class HostedPlan:
    """The effective, validated hosted configuration and its provenance."""

    backend: str
    model: str
    base_url: str
    credential_env: str | None
    credential_required: bool
    credential_present: bool
    runner_kind: str
    inference_location: str
    upstream_provider: str | None
    timeout_seconds: float | None
    policy: HostedPolicy
    source: str | None
    provenance: Mapping[str, Provenance]

    def source_of(self, field_name: str) -> Provenance:
        return self.provenance[field_name]

    def to_dict(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "model": self.model,
            "base_url": self.base_url,
            "credential_env": self.credential_env,
            "credential_required": self.credential_required,
            "credential_present": self.credential_present,
            "runner_kind": self.runner_kind,
            "inference_location": self.inference_location,
            "upstream_provider": self.upstream_provider,
            "timeout_seconds": self.timeout_seconds,
            "policy": self.policy.to_dict(),
            "source": self.source,
            "provenance": {
                name: {"source": item.source, "detail": item.detail}
                for name, item in self.provenance.items()
            },
        }


def plan_hosted_execution(
    configuration: ProductConfiguration | None = None,
    *,
    cli_provider: str | None = None,
    cli_model: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> HostedPlan:
    """Resolve and validate the hosted plan from the trusted configuration.

    The complete backend/model/endpoint/credential-reference/routing
    combination is validated here, after the two optional overrides are applied,
    so a caller never selects a runner for one backend and then executes
    another.
    """

    configuration = (
        configuration if configuration is not None else default_configuration()
    )
    resolved = resolve_inference(
        configuration,
        cli_provider=cli_provider,
        cli_model=cli_model,
        environ=environ,
    )
    if resolved.backend == "fixture":
        raise ConfigurationError(
            "backend 'fixture' is a local test backend and cannot be selected "
            f"for hosted execution; set inference.backend in "
            f"{configuration.source or '.reviewsensei.yml'} to one of "
            "local-ollama, cloud-ollama, openrouter, or openai-compatible",
            source=configuration.source,
        )
    policy = _hosted_policy(configuration)
    return _plan_from(resolved, policy, source=configuration.source)


def _hosted_policy(configuration: ProductConfiguration) -> HostedPolicy:
    github = configuration.github
    return HostedPolicy(
        automatic_reviews=github.automatic_reviews,
        writes=github.writes,
        reviews=github.reviews,
        mentions=github.mentions,
        learning=github.learning,
        artifacts=github.artifacts,
    )


def _plan_from(
    resolved: ResolvedInference,
    policy: HostedPolicy,
    *,
    source: str | None,
) -> HostedPlan:
    return HostedPlan(
        backend=resolved.backend,
        model=resolved.model,
        base_url=resolved.base_url,
        credential_env=resolved.credential_env,
        credential_required=resolved.credential_required,
        credential_present=resolved.credential_present,
        runner_kind=resolved.runner_kind,
        inference_location=resolved.inference_location,
        upstream_provider=resolved.upstream_provider,
        timeout_seconds=resolved.timeout_seconds,
        policy=policy,
        source=source,
        provenance=resolved.provenance,
    )


def hosted_plan_outputs(plan: HostedPlan) -> tuple[tuple[str, str], ...]:
    """Return the bounded plan as workflow-consumable output pairs.

    Only non-secret values are exported. A credential is named by its
    environment variable, never by its value.
    """

    def flag(value: bool) -> str:
        return "true" if value else "false"

    outputs = (
        ("backend", plan.backend),
        ("model", plan.model),
        ("base_url", plan.base_url),
        ("runner_kind", plan.runner_kind),
        ("credential_env", plan.credential_env or ""),
        ("credential_required", flag(plan.credential_required)),
        ("inference_location", plan.inference_location),
        ("upstream_provider", plan.upstream_provider or ""),
        ("automatic_reviews", flag(plan.policy.automatic_reviews)),
        ("writes", flag(plan.policy.writes)),
        ("reviews", plan.policy.reviews),
        ("mentions", flag(plan.policy.mentions)),
        ("learning", plan.policy.learning),
        ("artifacts", plan.policy.artifacts),
        ("configuration_source", plan.source or ""),
    )
    return outputs


def render_github_outputs(outputs: Sequence[tuple[str, str]]) -> str:
    """Render output pairs in the Actions environment-file format.

    Every value uses a heredoc delimiter so a bounded value that happens to be
    empty, or to contain ``=``, is never reinterpreted. The workflow appends
    the result to its own ``$GITHUB_OUTPUT``; this module never reads that
    variable itself.
    """

    lines: list[str] = []
    for name, value in outputs:
        if _OUTPUT_NAME.fullmatch(name) is None:
            raise ConfigurationError(f"output name '{name}' is not supported")
        if "\r" in value:
            raise ConfigurationError(f"output '{name}' contains a carriage return")
        delimiter = f"RS_{name.upper()}"
        while delimiter in value:
            delimiter += "_EOF"
        lines.extend((f"{name}<<{delimiter}", value, delimiter))
    return "\n".join(lines) + "\n"


def write_github_outputs(path: Path, outputs: Sequence[tuple[str, str]]) -> None:
    """Append the rendered outputs to one explicit, caller-provided path.

    Environment files are append-only by contract: a step that truncates
    ``$GITHUB_OUTPUT`` would discard the outputs other steps already wrote.
    """

    with path.open("a", encoding="utf-8") as stream:
        stream.write(render_github_outputs(outputs))


def render_hosted_plan(plan: HostedPlan, *, as_json: bool) -> str:
    """Render the plan for a human reader or as one JSON document."""

    if as_json:
        return json.dumps(plan.to_dict(), indent=2) + "\n"
    credential = plan.credential_env or "none"
    if plan.credential_env:
        credential += (
            ", required" if plan.credential_required else ", optional"
        ) + (", present" if plan.credential_present else ", absent")
    lines = [
        f"backend: {plan.backend} ({plan.source_of('backend').detail})",
        f"model: {plan.model} ({plan.source_of('model').detail})",
        f"endpoint: {plan.base_url} ({plan.source_of('base_url').detail})",
        f"runner requirement: {plan.runner_kind}",
        f"credential: {credential}",
    ]
    if plan.upstream_provider is not None:
        lines.append(f"upstream provider: {plan.upstream_provider}")
    if plan.timeout_seconds is not None:
        lines.append(f"timeout seconds: {plan.timeout_seconds:g}")
    lines.append(
        "github policy: "
        f"automatic_reviews={_flag(plan.policy.automatic_reviews)} "
        f"writes={_flag(plan.policy.writes)} reviews={plan.policy.reviews} "
        f"mentions={_flag(plan.policy.mentions)} learning={plan.policy.learning} "
        f"artifacts={plan.policy.artifacts}"
    )
    lines.append(f"configuration: {plan.source or 'none (packaged defaults)'}")
    return "\n".join(lines) + "\n"


def _flag(value: bool) -> str:
    return "true" if value else "false"


def load_and_plan_hosted(
    path: Path | None = None,
    *,
    cli_provider: str | None = None,
    cli_model: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> HostedPlan:
    """Load one configuration file and resolve the hosted plan from it."""

    return plan_hosted_execution(
        load_configuration(path),
        cli_provider=cli_provider,
        cli_model=cli_model,
        environ=environ,
    )
