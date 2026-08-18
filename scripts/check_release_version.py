#!/usr/bin/env python3
"""Check that a release tag agrees with project metadata and the changelog."""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

TAG_PATTERN = re.compile(r"^v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$")


class ReleaseVersionError(ValueError):
    """Raised when a release tag cannot be used for this project."""


def project_version(root: Path) -> str:
    try:
        with (root / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle).get("project", {})
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseVersionError("project metadata could not be read") from exc
    version = project.get("version") if isinstance(project, dict) else None
    if not isinstance(version, str) or not version:
        raise ReleaseVersionError("project version is missing")
    return version


def validate_release_tag(tag: str, root: Path = Path(".")) -> str:
    """Validate *tag* and return its unprefixed project version."""

    if not TAG_PATTERN.fullmatch(tag):
        raise ReleaseVersionError("release tag must match vX.Y.Z")
    expected_version = tag[1:]
    if project_version(root) != expected_version:
        raise ReleaseVersionError("release tag does not match project metadata")

    try:
        changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ReleaseVersionError("changelog could not be read") from exc
    heading = re.compile(
        rf"^##\s+{re.escape(expected_version)}\s+-\s+\d{{4}}-\d{{2}}-\d{{2}}\s*$",
        re.MULTILINE,
    )
    if not heading.search(changelog):
        raise ReleaseVersionError("matching dated changelog heading is missing")
    return expected_version


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tag", required=True, help="annotated release tag, for example v0.1.0"
    )
    parser.add_argument("--root", type=Path, default=Path("."), help="repository root")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        version = validate_release_tag(args.tag, args.root.resolve())
    except ReleaseVersionError as exc:
        print(f"release version check failed: {exc}", file=sys.stderr)
        return 1
    print(f"release version check passed: {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
