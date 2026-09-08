#!/usr/bin/env python3
"""Check that a release tag agrees with project metadata and the changelog."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

TAG_PATTERN = re.compile(r"^v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$")

NPM_LAUNCHER_NAME = "@reviewsensei/cli"
NPM_PUBLIC_REPOSITORY = {
    "type": "git",
    "url": "git+https://github.com/malsabbagh/review-sensei.git",
}
NPM_TARGETS = {
    "@reviewsensei/cli-darwin-arm64": ("darwin", "arm64", "bin/review-sensei"),
    "@reviewsensei/cli-darwin-x64": ("darwin", "x64", "bin/review-sensei"),
    "@reviewsensei/cli-linux-arm64-gnu": ("linux", "arm64", "bin/review-sensei"),
    "@reviewsensei/cli-linux-x64-gnu": ("linux", "x64", "bin/review-sensei"),
    "@reviewsensei/cli-win32-x64": ("win32", "x64", "bin/review-sensei.exe"),
}


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


def validate_npm_manifests(root: Path, expected_version: str) -> None:
    """Validate npm manifests when the optional npm source tree exists.

    Small temporary roots used by release-version unit tests intentionally do
    not contain packages/npm and continue to exercise only Python metadata.
    Once the tree is present, however, every target and exact dependency is
    required so a tag can never publish a partial or mixed-version set.
    """

    npm_root = root / "packages" / "npm"
    if not npm_root.exists():
        return
    launcher_path = npm_root / "cli" / "package.json"
    target_paths = {
        name: npm_root / "platforms" / name.split("/cli-", 1)[1] / "package.json"
        for name in NPM_TARGETS
    }
    paths = [launcher_path, *target_paths.values()]
    manifests: dict[str, dict[str, object]] = {}
    for path in paths:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ReleaseVersionError("npm package metadata could not be read") from exc
        if not isinstance(value, dict):
            raise ReleaseVersionError("npm package metadata is invalid")
        name = value.get("name")
        if not isinstance(name, str):
            raise ReleaseVersionError("npm package name is missing")
        manifests[name] = value

    launcher = manifests.get(NPM_LAUNCHER_NAME)
    if launcher is None or launcher.get("version") != expected_version:
        raise ReleaseVersionError(
            "npm launcher version does not match project metadata"
        )
    if launcher.get("optionalDependencies") != {
        name: expected_version for name in NPM_TARGETS
    }:
        raise ReleaseVersionError(
            "npm optional dependency versions do not match project metadata"
        )
    expected_names = set(NPM_TARGETS) | {NPM_LAUNCHER_NAME}
    if set(manifests) != expected_names:
        raise ReleaseVersionError(
            "npm package target set does not match project metadata"
        )
    for manifest in manifests.values():
        if manifest.get("repository") != NPM_PUBLIC_REPOSITORY:
            raise ReleaseVersionError(
                "npm package repository does not match the public source"
            )
    for name, (platform, arch, _payload) in NPM_TARGETS.items():
        manifest = manifests[name]
        if manifest.get("version") != expected_version:
            raise ReleaseVersionError(
                f"npm package {name} version does not match project metadata"
            )
        if manifest.get("os") != [platform] or manifest.get("cpu") != [arch]:
            raise ReleaseVersionError(
                f"npm package {name} platform metadata is invalid"
            )
        if "bin" in manifest:
            raise ReleaseVersionError(
                f"npm platform package {name} must not expose a bin entry"
            )


def validate_release_tag(tag: str, root: Path = Path(".")) -> str:
    """Validate *tag* and return its unprefixed project version."""

    if not TAG_PATTERN.fullmatch(tag):
        raise ReleaseVersionError("release tag must match vX.Y.Z")
    expected_version = tag[1:]
    if project_version(root) != expected_version:
        raise ReleaseVersionError("release tag does not match project metadata")
    validate_npm_manifests(root, expected_version)

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
