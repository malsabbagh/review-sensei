#!/usr/bin/env python3
"""Independently validate ReviewSensei npm package directories and tarballs."""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import stat
import sys
import tarfile
from pathlib import Path
from typing import Mapping

LAUNCHER_NAME = "@reviewsensei/cli"
NPM_PUBLIC_REPOSITORY = {
    "type": "git",
    "url": "git+https://github.com/malsabbagh/review-sensei.git",
}
VERSIONED_TARGETS: dict[str, dict[str, object]] = {
    "@reviewsensei/cli-darwin-arm64": {
        "directory": "darwin-arm64",
        "os": ["darwin"],
        "cpu": ["arm64"],
        "payload": "bin/review-sensei",
    },
    "@reviewsensei/cli-darwin-x64": {
        "directory": "darwin-x64",
        "os": ["darwin"],
        "cpu": ["x64"],
        "payload": "bin/review-sensei",
    },
    "@reviewsensei/cli-linux-arm64-gnu": {
        "directory": "linux-arm64-gnu",
        "os": ["linux"],
        "cpu": ["arm64"],
        "payload": "bin/review-sensei",
    },
    "@reviewsensei/cli-linux-x64-gnu": {
        "directory": "linux-x64-gnu",
        "os": ["linux"],
        "cpu": ["x64"],
        "payload": "bin/review-sensei",
    },
    "@reviewsensei/cli-win32-x64": {
        "directory": "win32-x64",
        "os": ["win32"],
        "cpu": ["x64"],
        "payload": "bin/review-sensei.exe",
    },
}
PLATFORM_BY_DIRECTORY = {
    str(value["directory"]): (name, value) for name, value in VERSIONED_TARGETS.items()
}
LIFECYCLE_KEYS = {
    "preinstall",
    "install",
    "postinstall",
    "prepack",
    "postpack",
    "preversion",
    "version",
    "postversion",
    "preprepare",
    "postprepare",
    "dependencies",
    "prepare",
    "prepublish",
    "prepublishOnly",
    "publish",
    "postpublish",
}
PACKAGE_FILES = ("bin", "README.md", "LICENSE")
MAX_ARCHIVE_MEMBERS = 10_000
MAX_MEMBER_SIZE = 64 * 1024 * 1024
MAX_ARCHIVE_SIZE = 256 * 1024 * 1024
MAX_PACKAGE_FILES = 10_000
MAX_PACKAGE_FILE_SIZE = 64 * 1024 * 1024
MAX_PACKAGE_SIZE = 256 * 1024 * 1024
PACKAGE_SET_METADATA = {"checksums.json", "SHA256SUMS", "package-set.json"}
CERTIFI_CA_BUNDLE_SUFFIX = ("bin", "_internal", "certifi", "cacert.pem")


class NpmPackageValidationError(ValueError):
    """Raised for any unsafe or contract-invalid package input."""


# Short alias for callers that want a conventional validator exception name.
ValidationError = NpmPackageValidationError


def safe_archive_member_name(name: str) -> str:
    """Return a normalized archive path or fail closed."""

    if not isinstance(name, str) or not name or "\\" in name:
        raise NpmPackageValidationError("archive member has an unsafe path")
    # Tar directory members conventionally carry a trailing slash (npm pack
    # emits package/). Normalize that harmless marker before checking segments.
    candidate = name[:-1] if name.endswith("/") else name
    if (
        "\x00" in candidate
        or candidate.startswith("/")
        or candidate.startswith("~")
        or (len(candidate) >= 2 and candidate[1] == ":")
    ):
        raise NpmPackageValidationError("archive member has an unsafe path")
    normalized = posixpath.normpath(candidate)
    if normalized in {"", ".", ".."} or normalized != candidate:
        raise NpmPackageValidationError("archive member has an unsafe path")
    components = normalized.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise NpmPackageValidationError("archive member has an unsafe path")
    return normalized


def _forbidden_path(path: str) -> bool:
    lowered = path.casefold()
    parts = lowered.split("/")
    basename = parts[-1]
    is_certifi_ca_bundle = tuple(parts[-4:]) == CERTIFI_CA_BUNDLE_SUFFIX
    return (
        basename.endswith(".map")
        or basename == ".env"
        or basename.startswith(".env.")
        or basename == ".project-ai"
        or ".project-ai" in parts
        or any(part in {"credentials", "secrets", "private", ".ssh"} for part in parts)
        or (
            basename.endswith((".pem", ".key", ".pfx", ".p12"))
            and not is_certifi_ca_bundle
        )
        or basename.startswith(("id_rsa", "id_ed25519"))
    )


def _assert_no_lifecycle(manifest: Mapping[str, object]) -> None:
    scripts = manifest.get("scripts")
    if scripts is None:
        return
    if not isinstance(scripts, dict):
        raise NpmPackageValidationError("package scripts must be an object")
    found = sorted(LIFECYCLE_KEYS.intersection(scripts))
    if found:
        raise NpmPackageValidationError(
            f"npm lifecycle scripts are forbidden: {', '.join(found)}"
        )


def _assert_publishable_files(manifest: Mapping[str, object]) -> None:
    if manifest.get("files") != list(PACKAGE_FILES):
        raise NpmPackageValidationError(
            "npm package files metadata must be exactly bin, README.md, LICENSE"
        )


def _read_manifest(data: bytes) -> dict[str, object]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise NpmPackageValidationError("package.json is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise NpmPackageValidationError("package.json must contain an object")
    return value


def _validate_manifest(
    manifest: Mapping[str, object], expected_version: str | None = None
) -> tuple[str, str, str]:
    name = manifest.get("name")
    version = manifest.get("version")
    if (
        not isinstance(name, str)
        or not isinstance(version, str)
        or not name
        or not version
    ):
        raise NpmPackageValidationError("package name and version are required")
    if expected_version is not None and version != expected_version:
        raise NpmPackageValidationError(
            "package version does not match the release version"
        )
    _assert_no_lifecycle(manifest)
    _assert_publishable_files(manifest)
    if manifest.get("license") not in {None, "MIT"}:
        raise NpmPackageValidationError("package license must be MIT")
    if manifest.get("repository") != NPM_PUBLIC_REPOSITORY:
        raise NpmPackageValidationError(
            "package repository does not match the public source"
        )

    if name == LAUNCHER_NAME:
        if manifest.get("license") != "MIT":
            raise NpmPackageValidationError("launcher license must be MIT")
        if manifest.get("bin") != {"review-sensei": "bin/review-sensei.js"}:
            raise NpmPackageValidationError("launcher bin metadata is invalid")
        if manifest.get("engines") != {"node": ">=22"}:
            raise NpmPackageValidationError("launcher requires Node.js >=22")
        expected_optional = {target: version for target in VERSIONED_TARGETS}
        if manifest.get("optionalDependencies") != expected_optional:
            raise NpmPackageValidationError(
                "launcher optional dependencies are invalid"
            )
        if "dependencies" in manifest:
            raise NpmPackageValidationError("launcher dependencies are forbidden")
        publish = manifest.get("publishConfig")
        if publish != {"access": "public"}:
            raise NpmPackageValidationError(
                "launcher publishConfig.access must be public"
            )
        return "launcher", name, "bin/review-sensei.js"

    target = VERSIONED_TARGETS.get(name)
    if target is None:
        raise NpmPackageValidationError(
            "package name is not a supported ReviewSensei target"
        )
    if manifest.get("os") != target["os"] or manifest.get("cpu") != target["cpu"]:
        raise NpmPackageValidationError("platform package constraints are invalid")
    if "bin" in manifest:
        raise NpmPackageValidationError(
            "platform package must not expose a command bin"
        )
    if manifest.get("license") != "MIT":
        raise NpmPackageValidationError("platform package license must be MIT")
    if "optionalDependencies" in manifest or "dependencies" in manifest:
        raise NpmPackageValidationError("platform package dependencies are forbidden")
    return "platform", name, str(target["payload"])


def _check_license(data: bytes) -> None:
    if not data.startswith(b"MIT License"):
        raise NpmPackageValidationError("MIT LICENSE file is missing or invalid")


def _check_payload(
    data: bytes | None,
    member_mode: int,
    name: str,
    *,
    require_posix_mode: bool = True,
) -> None:
    if data is None:
        raise NpmPackageValidationError("executable payload is missing")
    # Tarball metadata must preserve POSIX execute bits. A Windows package
    # directory cannot represent those bits, so only that local check opts out.
    if (
        require_posix_mode
        and not name.endswith(".exe")
        and not (member_mode & stat.S_IXUSR)
    ):
        raise NpmPackageValidationError("POSIX executable payload is not executable")


def _validate_member_layout(
    names: set[str], role: str, payload: str, *, package_root: str | None = "package"
) -> None:
    """Enforce the closed npm files allowlist after pack expansion."""

    required = {"package.json", "LICENSE", "README.md", payload}
    if package_root is None:
        relative_names = names
    else:
        prefix = f"{package_root}/"
        relative_names = set()
        for name in names:
            if name == package_root:
                continue
            if not name.startswith(prefix):
                raise NpmPackageValidationError(
                    "npm tarball contains a member outside package/"
                )
            relative_names.add(name[len(prefix) :])
    for relative in relative_names:
        if relative in {"", "."}:
            continue
        if relative in {"package.json", "LICENSE", "README.md"}:
            continue
        if relative == "bin" or relative.startswith("bin/"):
            if role == "platform" or relative == payload:
                continue
        raise NpmPackageValidationError(
            "npm package contains a member outside the declared files allowlist"
        )
    if not required.issubset(relative_names):
        raise NpmPackageValidationError(
            "npm package is missing a required files-allowlist member"
        )


def _archive_members(archive: tarfile.TarFile) -> tuple[list[tarfile.TarInfo], int]:
    members = archive.getmembers()
    if len(members) > MAX_ARCHIVE_MEMBERS:
        raise NpmPackageValidationError("archive contains too many members")
    seen: set[str] = set()
    total = 0
    for member in members:
        name = safe_archive_member_name(member.name)
        if name in seen:
            raise NpmPackageValidationError("archive contains duplicate members")
        seen.add(name)
        if _forbidden_path(name):
            raise NpmPackageValidationError(
                "archive contains a prohibited private path"
            )
        if member.issym() or member.islnk():
            raise NpmPackageValidationError(
                "archive symlink and hardlink members are forbidden"
            )
        if not (member.isdir() or member.isreg()):
            raise NpmPackageValidationError("archive contains a special file member")
        if member.size < 0 or member.size > MAX_MEMBER_SIZE:
            raise NpmPackageValidationError(
                "archive member exceeds the expanded-size limit"
            )
        total += member.size
        if total > MAX_ARCHIVE_SIZE:
            raise NpmPackageValidationError("archive exceeds the expanded-size limit")
    return members, total


def validate_tarball(
    path: Path, expected_version: str | None = None
) -> dict[str, object]:
    """Validate an npm pack tarball without importing or executing package code."""

    try:
        archive = tarfile.open(path, mode="r:*")
    except (OSError, tarfile.TarError) as exc:
        raise NpmPackageValidationError("npm tarball could not be opened") from exc
    with archive:
        members, expanded_size = _archive_members(archive)
        named_members = {
            safe_archive_member_name(member.name): member for member in members
        }
        files = {
            name: member for name, member in named_members.items() if member.isreg()
        }
        package_json = [name for name in files if name.endswith("/package.json")]
        if len(package_json) != 1:
            raise NpmPackageValidationError(
                "tarball must contain exactly one package.json"
            )
        package_root = package_json[0].rsplit("/", 1)[0]
        if package_root != "package":
            raise NpmPackageValidationError("npm tarball package root must be package/")
        manifest = _read_manifest(archive.extractfile(files[package_json[0]]).read())  # type: ignore[union-attr]
        role, name, payload = _validate_manifest(manifest, expected_version)
        _validate_member_layout(set(named_members), role, payload)
        license_member = files.get(f"{package_root}/LICENSE")
        if license_member is None:
            raise NpmPackageValidationError("MIT LICENSE file is missing")
        license_data = archive.extractfile(license_member)
        _check_license(license_data.read(MAX_MEMBER_SIZE) if license_data else None)  # type: ignore[arg-type]
        payload_member = files.get(f"{package_root}/{payload}")
        payload_data = archive.extractfile(payload_member) if payload_member else None
        _check_payload(
            payload_data.read(MAX_MEMBER_SIZE) if payload_data else None,
            payload_member.mode if payload_member else 0,
            str(payload),
        )
        return {
            "path": str(path),
            "name": name,
            "role": role,
            "version": manifest["version"],
            "members": len(members),
            "expanded_size": expanded_size,
        }


def _directory_files(package: Path) -> list[Path]:
    paths = sorted(package.rglob("*"), key=lambda item: item.as_posix())
    if len(paths) > MAX_PACKAGE_FILES:
        raise NpmPackageValidationError("package contains too many members")
    result: list[Path] = []
    total = 0
    for path in paths:
        relative = path.relative_to(package).as_posix()
        safe_archive_member_name(relative)
        if _forbidden_path(relative):
            raise NpmPackageValidationError(
                "package contains a prohibited private path"
            )
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise NpmPackageValidationError(
                "package contains a symlink or special file"
            )
        if path.is_file():
            size = path.stat().st_size
            if size > MAX_PACKAGE_FILE_SIZE:
                raise NpmPackageValidationError(
                    "package file exceeds the expanded-size limit"
                )
            total += size
            if total > MAX_PACKAGE_SIZE:
                raise NpmPackageValidationError(
                    "package exceeds the expanded-size limit"
                )
            result.append(path)
    return result


def validate_package_directory(
    path: Path, expected_version: str | None = None
) -> dict[str, object]:
    # Check the caller-supplied path before resolving it: resolving first
    # would turn a symlinked package root into an apparently safe directory.
    if path.is_symlink():
        raise NpmPackageValidationError("package path must be a regular directory")
    package = path.resolve()
    if not package.is_dir():
        raise NpmPackageValidationError("package path must be a regular directory")
    files = _directory_files(package)
    by_name = {file.relative_to(package).as_posix(): file for file in files}
    manifest_path = package / "package.json"
    if "package.json" not in by_name:
        raise NpmPackageValidationError("package.json is missing")
    manifest = _read_manifest(manifest_path.read_bytes())
    role, name, payload = _validate_manifest(manifest, expected_version)
    _validate_member_layout(set(by_name), role, payload, package_root=None)
    license_path = package / "LICENSE"
    if not license_path.is_file():
        raise NpmPackageValidationError("MIT LICENSE file is missing")
    _check_license(license_path.read_bytes())
    payload_path = package / payload
    if not payload_path.is_file() or payload_path.is_symlink():
        raise NpmPackageValidationError("executable payload is missing")
    _check_payload(
        None if payload_path.stat().st_size == 0 else payload_path.read_bytes(),
        payload_path.stat().st_mode,
        str(payload),
        # NTFS does not preserve POSIX execute bits in a checked-out package
        # directory. Tarball validation above still enforces them from archive
        # metadata; this only relaxes the non-representable local check.
        require_posix_mode=os.name != "nt",
    )
    return {
        "path": str(package),
        "name": name,
        "role": role,
        "version": manifest["version"],
        "members": len(files),
        "expanded_size": sum(file.stat().st_size for file in files),
    }


def validate_package_set(
    path: Path, expected_version: str | None = None
) -> dict[str, object]:
    """Validate exactly the launcher plus five platform package directories."""

    if path.is_symlink():
        raise NpmPackageValidationError("package set must be a regular directory")
    root = path.resolve()
    if not root.is_dir():
        raise NpmPackageValidationError("package set must be a regular directory")
    expected_dirs = {"cli"} | {
        str(item["directory"]) for item in VERSIONED_TARGETS.values()
    }
    children = list(root.iterdir())
    actual_dirs = {child.name for child in children if child.is_dir()}
    if actual_dirs != expected_dirs:
        raise NpmPackageValidationError("package set must contain exactly six packages")
    for child in children:
        if child.name in expected_dirs:
            if child.is_symlink() or not child.is_dir():
                raise NpmPackageValidationError(
                    "package set package directories must be regular directories"
                )
        elif child.name in PACKAGE_SET_METADATA:
            if child.is_symlink() or not child.is_file():
                raise NpmPackageValidationError(
                    "package set metadata must be regular files"
                )
        else:
            raise NpmPackageValidationError("package set contains an unexpected member")
    package_reports = [
        validate_package_directory(root / directory, expected_version)
        for directory in sorted(expected_dirs)
    ]
    versions = {str(report["version"]) for report in package_reports}
    if len(versions) != 1:
        raise NpmPackageValidationError("package set contains multiple versions")
    expected_names = {LAUNCHER_NAME} | set(VERSIONED_TARGETS)
    if {str(report["name"]) for report in package_reports} != expected_names:
        raise NpmPackageValidationError("package set contains an unexpected package")
    return {
        "path": str(root),
        "version": next(iter(versions)),
        "packages": package_reports,
    }


def validate(path: Path, expected_version: str | None = None) -> dict[str, object]:
    if path.is_dir():
        if (path / "package.json").is_file():
            return validate_package_directory(path, expected_version)
        return validate_package_set(path, expected_version)
    return validate_tarball(path, expected_version)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path", type=Path, help="npm tarball, package directory, or six-package set"
    )
    parser.add_argument("--expected-version", help="require this exact package version")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = validate(args.path, args.expected_version)
    except NpmPackageValidationError as exc:
        print(f"npm package validation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
