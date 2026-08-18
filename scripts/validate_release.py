#!/usr/bin/env python3
"""Validate ReviewSensei source and wheel artifacts without importing them."""

from __future__ import annotations

import argparse
import re
import sys
import tarfile
import zipfile
from email.parser import Parser
from pathlib import Path
from typing import Iterable

PACKAGE_NAME = "review-sensei"
NORMALIZED_PACKAGE_NAME = "review_sensei"
DEFAULT_PACKAGE_FILES = (
    "default_categories/01-correctness.json",
    "default_categories/02-security.json",
    "default_categories/03-architecture.json",
    "default_categories/03-maintainability.json",
    "default_categories/04-tests.json",
    "default_stages/01-default-review.json",
)
SDIST_ROOT_FILES = ("pyproject.toml", "README.md", "LICENSE", "CHANGELOG.md")
WHEEL_NAME_PATTERN = re.compile(
    r"^(?P<name>[^-]+)-(?P<version>(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))-[^-]+-[^-]+-[^-]+\.whl$"
)


class ReleaseArtifactError(ValueError):
    """Raised when a release artifact is malformed or incomplete."""


def _safe_member(name: str) -> bool:
    """Return whether an archive member is a safe, relative POSIX path."""

    if not name or "\x00" in name or "\\" in name:
        return False
    candidate = name.rstrip("/")
    if not candidate or candidate.startswith("/") or re.match(r"^[A-Za-z]:", candidate):
        return False
    # Inspect the raw segments instead of a normalizing path class: archive
    # readers may collapse repeated separators or dot segments before callers
    # get a chance to apply their own extraction policy.
    parts = candidate.split("/")
    return all(part not in {"", ".", ".."} for part in parts)


def _unique_safe_members(names: Iterable[str], artifact: str) -> list[str]:
    values = list(names)
    if any(not _safe_member(name) for name in values):
        raise ReleaseArtifactError(f"{artifact} contains an unsafe archive path")
    normalized = [name.rstrip("/") for name in values]
    if len(set(normalized)) != len(normalized):
        raise ReleaseArtifactError(f"{artifact} contains duplicate archive paths")
    return normalized


def _read_wheel(path: Path, expected_version: str | None) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            for entry in entries:
                mode = (entry.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    raise ReleaseArtifactError("wheel contains a symbolic link")
            names = _unique_safe_members((item.filename for item in entries), "wheel")
            match = WHEEL_NAME_PATTERN.fullmatch(path.name)
            if (
                not match
                or match.group("name").lower().replace("-", "_")
                != NORMALIZED_PACKAGE_NAME
            ):
                raise ReleaseArtifactError("wheel filename is invalid")
            filename_version = match.group("version")
            if expected_version is not None and filename_version != expected_version:
                raise ReleaseArtifactError(
                    "wheel version does not match the requested release"
                )

            dist_info = [
                name.split("/", 1)[0]
                for name in names
                if name.endswith("/METADATA")
                and name.count("/") == 1
                and name.split("/", 1)[0].endswith(".dist-info")
            ]
            if len(set(dist_info)) != 1:
                raise ReleaseArtifactError(
                    "wheel dist-info metadata is missing or ambiguous"
                )
            dist_info_dir = dist_info[0]
            if (
                dist_info_dir
                != f"{NORMALIZED_PACKAGE_NAME}-{filename_version}.dist-info"
            ):
                raise ReleaseArtifactError("wheel dist-info directory is inconsistent")
            metadata_path = f"{dist_info_dir}/METADATA"
            wheel_path = f"{dist_info_dir}/WHEEL"
            record_path = f"{dist_info_dir}/RECORD"
            if not {metadata_path, wheel_path, record_path}.issubset(names):
                raise ReleaseArtifactError("wheel dist-info files are incomplete")
            try:
                metadata_text = archive.read(metadata_path).decode("utf-8")
            except (KeyError, UnicodeError):
                raise ReleaseArtifactError("wheel metadata is unreadable") from None
            metadata = Parser().parsestr(metadata_text)
            if metadata.get("Name") != PACKAGE_NAME:
                raise ReleaseArtifactError("wheel package name is incorrect")
            if metadata.get("Version") != filename_version:
                raise ReleaseArtifactError("wheel metadata version is inconsistent")
            if metadata.get("License-Expression") != "MIT":
                raise ReleaseArtifactError("wheel SPDX license expression is missing")
            if metadata.get("Requires-Python") != ">=3.11":
                raise ReleaseArtifactError("wheel Python requirement is inconsistent")
            if not any(
                name == "LICENSE" or name.endswith("/LICENSE") for name in names
            ):
                raise ReleaseArtifactError("wheel license file is missing")
            for relative in DEFAULT_PACKAGE_FILES:
                if f"review_sensei/{relative}" not in names:
                    raise ReleaseArtifactError(
                        "wheel packaged default assets are incomplete"
                    )
    except ReleaseArtifactError:
        raise
    except (OSError, zipfile.BadZipFile):
        raise ReleaseArtifactError("wheel could not be read") from None


def _read_sdist(path: Path, expected_version: str | None) -> None:
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            entries = archive.getmembers()
            if any(
                entry.issym() or entry.islnk() or not (entry.isfile() or entry.isdir())
                for entry in entries
            ):
                raise ReleaseArtifactError(
                    "source distribution contains an unsafe member type"
                )
            names = _unique_safe_members(
                (item.name for item in entries), "source distribution"
            )
            if not names:
                raise ReleaseArtifactError("source distribution is empty")
            roots = {name.split("/", 1)[0] for name in names}
            if len(roots) != 1:
                raise ReleaseArtifactError("source distribution has ambiguous roots")
            root = next(iter(roots))
            if not root.startswith(f"{NORMALIZED_PACKAGE_NAME}-"):
                raise ReleaseArtifactError("source distribution root is invalid")
            root_prefix = f"{root}/"
            if any(name != root and not name.startswith(root_prefix) for name in names):
                raise ReleaseArtifactError(
                    "source distribution contains a member outside its root"
                )
            version = root[len(NORMALIZED_PACKAGE_NAME) + 1 :]
            if expected_version is not None and version != expected_version:
                raise ReleaseArtifactError(
                    "source distribution version does not match the requested release"
                )
            relative = {
                name[len(root) + 1 :] for name in names if name.startswith(f"{root}/")
            }
            if not all(required in relative for required in SDIST_ROOT_FILES):
                raise ReleaseArtifactError(
                    "source distribution root files are incomplete"
                )
            if not any(
                name.startswith("docs/") and name.endswith(".md") for name in relative
            ):
                raise ReleaseArtifactError(
                    "source distribution documentation is missing"
                )
            if not any(name.startswith("examples/") for name in relative):
                raise ReleaseArtifactError("source distribution examples are missing")
            for required in DEFAULT_PACKAGE_FILES:
                if f"src/review_sensei/{required}" not in relative:
                    raise ReleaseArtifactError(
                        "source distribution packaged defaults are incomplete"
                    )
    except ReleaseArtifactError:
        raise
    except (OSError, tarfile.TarError):
        raise ReleaseArtifactError("source distribution could not be read") from None


def validate_release_directory(
    directory: Path, expected_version: str | None = None
) -> None:
    """Validate the one-wheel/one-sdist release bundle in *directory*."""

    if not directory.is_dir():
        raise ReleaseArtifactError("release artifact directory is missing")
    sdists = sorted(
        path
        for path in directory.glob("*.tar.gz")
        if path.is_file() and not path.is_symlink()
    )
    wheels = sorted(
        path
        for path in directory.glob("*.whl")
        if path.is_file() and not path.is_symlink()
    )
    if len(sdists) != 1 or len(wheels) != 1:
        raise ReleaseArtifactError(
            "release directory must contain exactly one sdist and one wheel"
        )
    _read_sdist(sdists[0], expected_version)
    _read_wheel(wheels[0], expected_version)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "directory", type=Path, help="directory containing one sdist and one wheel"
    )
    parser.add_argument("--version", help="expected package version")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        validate_release_directory(args.directory.resolve(), args.version)
    except ReleaseArtifactError as exc:
        print(f"release artifact validation failed: {exc}", file=sys.stderr)
        return 1
    print("release artifact validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
