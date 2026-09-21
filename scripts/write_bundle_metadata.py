#!/usr/bin/env python3
"""Write bundle-metadata.json for attested npm release bundles."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Workflow steps invoke this file directly, so the checkout root must be importable.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.check_release_version import TAG_PATTERN  # noqa: E402

GIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
BUNDLE_METADATA_FILENAME = "bundle-metadata.json"
SHA256SUMS_FILENAME = "SHA256SUMS"


class BundleMetadataError(ValueError):
    """Raised when bundle metadata cannot be written."""


def normalize_release_version(
    *, version: str | None = None, tag: str | None = None
) -> str:
    if version is not None and tag is not None:
        raise BundleMetadataError("specify only one of version or tag")
    if version is not None:
        candidate = f"v{version}"
    elif tag is not None:
        candidate = tag
    else:
        raise BundleMetadataError("version or tag is required")
    if not TAG_PATTERN.fullmatch(candidate):
        raise BundleMetadataError(
            f"release version must match vX.Y.Z, not {candidate!r}"
        )
    return candidate[1:]


def canonical_git_sha(source_sha: str) -> str:
    if not GIT_SHA_PATTERN.fullmatch(source_sha):
        raise BundleMetadataError("source SHA is not a canonical git commit")
    return source_sha


def resolve_git_head_sha() -> str:
    try:
        head_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            text=True,
        ).strip()
    except subprocess.CalledProcessError as exc:
        raise BundleMetadataError(
            "unable to resolve checkout HEAD for bundle metadata"
        ) from exc
    return canonical_git_sha(head_sha)


def resolve_executed_source_sha(*, explicit: str | None) -> str:
    head_sha = resolve_git_head_sha()
    github_sha = os.environ.get("GITHUB_SHA")
    if github_sha:
        github_sha = canonical_git_sha(github_sha)
        if github_sha != head_sha:
            raise BundleMetadataError(
                "GITHUB_SHA does not match the current checkout HEAD"
            )
    executed_sha = head_sha
    if explicit is not None:
        explicit_sha = canonical_git_sha(explicit)
        if explicit_sha != executed_sha:
            raise BundleMetadataError("source SHA does not match the executed commit")
        return explicit_sha
    return executed_sha


def append_sha256sums_entry(bundle_dir: Path, subject: Path) -> None:
    sums_path = bundle_dir / SHA256SUMS_FILENAME
    if not sums_path.is_file():
        raise BundleMetadataError(
            f"release bundle is missing SHA256SUMS at {sums_path}"
        )
    if not subject.is_file():
        raise BundleMetadataError(f"release bundle subject is missing at {subject}")
    digest = hashlib.sha256(subject.read_bytes()).hexdigest()
    with sums_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{digest}  {subject.name}\n")


def write_bundle_metadata(
    bundle_dir: Path,
    *,
    version: str | None = None,
    tag: str | None = None,
    source_sha: str,
) -> Path:
    normalized_version = normalize_release_version(version=version, tag=tag)
    normalized_sha = canonical_git_sha(source_sha)
    metadata = {"version": normalized_version, "source_sha": normalized_sha}
    path = bundle_dir / BUNDLE_METADATA_FILENAME
    path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    append_sha256sums_entry(bundle_dir, path)
    return path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--version", help="Exact npm release version, for example 0.5.0"
    )
    source.add_argument("--tag", help="Annotated release tag, for example v0.5.0")
    parser.add_argument(
        "--source-sha",
        help="Git commit SHA for the attested bundle (defaults to HEAD)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        source_sha = resolve_executed_source_sha(explicit=args.source_sha)
        path = write_bundle_metadata(
            args.bundle_dir,
            version=args.version,
            tag=args.tag,
            source_sha=source_sha,
        )
    except BundleMetadataError as exc:
        print(f"bundle metadata write failed: {exc}", file=sys.stderr)
        return 1
    print(f"bundle metadata write passed: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
