#!/usr/bin/env python3
"""Validate a release compatibility manifest before publication/execution."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from review_sensei.errors import ReviewInputError  # noqa: E402
from review_sensei.release_manifest import (  # noqa: E402
    manifest_digest,
    validate_compatibility_manifest,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args(argv)
    try:
        value = json.loads(args.manifest.read_text(encoding="utf-8"))
        manifest = validate_compatibility_manifest(value)
    except (OSError, UnicodeError, json.JSONDecodeError, ReviewInputError) as exc:
        print(f"compatibility manifest validation failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"compatibility manifest validation passed (digest {manifest_digest(manifest)})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
