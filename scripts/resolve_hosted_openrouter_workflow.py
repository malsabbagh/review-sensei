#!/usr/bin/env python3
"""Resolve and validate hosted OpenRouter model/upstream for workflow steps."""

from __future__ import annotations

import argparse

from review_sensei.hosting.openrouter_workflow import emit_hosted_openrouter_resolution


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
    return emit_hosted_openrouter_resolution(args.action)


if __name__ == "__main__":
    raise SystemExit(main())
