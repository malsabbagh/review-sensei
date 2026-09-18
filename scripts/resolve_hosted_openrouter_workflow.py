#!/usr/bin/env python3
"""Resolve and validate hosted OpenRouter model/upstream for workflow steps."""

from __future__ import annotations

import argparse
import os
import sys

from review_sensei.errors import ReviewInputError
from review_sensei.provider_config import (
    hosted_openrouter_upstream,
    validate_resolved_hosted_job_model,
)


def resolve_model() -> str:
    return validate_resolved_hosted_job_model(
        provider_mode="openrouter",
        workflow_mode=os.environ.get("MODE", ""),
        caller_model=os.environ.get("CALLER_MODEL", ""),
        reviewsensei_model=os.environ.get("HOSTED_REVIEWSENSEI_MODEL", ""),
    )


def resolve_upstream(model: str) -> str:
    return hosted_openrouter_upstream(
        model,
        configured_upstream=os.environ.get("OPENROUTER_UPSTREAM_PROVIDER"),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Resolve hosted OpenRouter workflow model and upstream."
    )
    parser.add_argument(
        "action",
        choices=("model", "upstream", "both"),
        help="Emit resolved model, upstream, or both as a tab-separated line.",
    )
    args = parser.parse_args()
    try:
        model = resolve_model()
        upstream = resolve_upstream(model)
    except ReviewInputError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    if args.action == "model":
        print(model, end="")
        return 0
    if args.action == "upstream":
        print(upstream, end="")
        return 0
    print(f"{model}\t{upstream}", end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
