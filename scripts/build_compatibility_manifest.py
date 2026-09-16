#!/usr/bin/env python3
"""Build a validated compatibility manifest from exact artifact files."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from review_sensei.errors import ReviewInputError  # noqa: E402
from review_sensei.release_manifest import (  # noqa: E402
    build_compatibility_manifest,
    manifest_digest,
)


def _named_path(value: str) -> tuple[str, Path]:
    name, separator, remainder = value.partition("=")
    if not separator or not name.strip() or not remainder:
        raise argparse.ArgumentTypeError("npm artifacts must be specified as name=path")
    return name, Path(remainder)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True)
    parser.add_argument("--workflow-commit", required=True)
    parser.add_argument("--workflow", type=Path, required=True)
    parser.add_argument("--workflow-name", default="review-sensei-run.yml")
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--python-name", default="review-sensei")
    parser.add_argument("--npm", type=_named_path, action="append", required=True)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--worker-name", default="review-sensei-worker")
    parser.add_argument("--schemas-version", required=True)
    parser.add_argument("--compatible-worker-range", required=True)
    parser.add_argument("--provenance", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        manifest = build_compatibility_manifest(
            release=args.release,
            workflow_path=args.workflow,
            workflow_name=args.workflow_name,
            workflow_commit=args.workflow_commit,
            python_path=args.python,
            python_name=args.python_name,
            npm_artifacts=tuple(args.npm),
            worker_path=args.worker,
            worker_name=args.worker_name,
            schemas_version=args.schemas_version,
            compatible_worker_range=args.compatible_worker_range,
            provenance=args.provenance,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n"
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=args.output.parent,
            delete=False,
        ) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        os.replace(temp_path, args.output)
    except (OSError, UnicodeError, ReviewInputError) as exc:
        print(f"compatibility manifest build failed: {exc}", file=sys.stderr)
        return 1
    print(f"compatibility manifest digest {manifest_digest(manifest)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
