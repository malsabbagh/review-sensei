"""Hosted OpenRouter model/upstream resolution for reusable workflows."""

from __future__ import annotations

import os
import sys

from ..errors import ReviewInputError
from ..provider_config import (
    hosted_openrouter_upstream,
    validate_resolved_hosted_job_model,
)


def resolve_hosted_openrouter_model() -> str:
    return validate_resolved_hosted_job_model(
        provider_mode="openrouter",
        workflow_mode=os.environ.get("MODE", ""),
        caller_model=os.environ.get("CALLER_MODEL", ""),
        reviewsensei_model=os.environ.get("HOSTED_REVIEWSENSEI_MODEL", ""),
    )


def resolve_hosted_openrouter_upstream(model: str) -> str:
    return hosted_openrouter_upstream(
        model,
        configured_upstream=os.environ.get("OPENROUTER_UPSTREAM_PROVIDER"),
    )


def emit_hosted_openrouter_resolution(action: str) -> int:
    try:
        model = resolve_hosted_openrouter_model()
        upstream = resolve_hosted_openrouter_upstream(model)
    except ReviewInputError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    if action == "model":
        print(model, end="")
        return 0
    if action == "upstream":
        print(upstream, end="")
        return 0
    print(f"{model}\t{upstream}", end="")
    return 0
