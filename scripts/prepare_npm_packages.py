#!/usr/bin/env python3
"""Assemble the launcher and native platform packages in ignored staging."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import stat
import sys
import tomllib
from pathlib import Path
from typing import Mapping

TARGETS: dict[str, dict[str, object]] = {
    "darwin-arm64": {
        "package": "@reviewsensei/cli-darwin-arm64",
        "directory": "darwin-arm64",
        "os": ["darwin"],
        "cpu": ["arm64"],
        "payload": "bin/review-sensei",
    },
    "darwin-x64": {
        "package": "@reviewsensei/cli-darwin-x64",
        "directory": "darwin-x64",
        "os": ["darwin"],
        "cpu": ["x64"],
        "payload": "bin/review-sensei",
    },
    "linux-arm64-gnu": {
        "package": "@reviewsensei/cli-linux-arm64-gnu",
        "directory": "linux-arm64-gnu",
        "os": ["linux"],
        "cpu": ["arm64"],
        "payload": "bin/review-sensei",
    },
    "linux-x64-gnu": {
        "package": "@reviewsensei/cli-linux-x64-gnu",
        "directory": "linux-x64-gnu",
        "os": ["linux"],
        "cpu": ["x64"],
        "payload": "bin/review-sensei",
    },
    "win32-x64": {
        "package": "@reviewsensei/cli-win32-x64",
        "directory": "win32-x64",
        "os": ["win32"],
        "cpu": ["x64"],
        "payload": "bin/review-sensei.exe",
    },
}
LAUNCHER_NAME = "@reviewsensei/cli"
NPM_PUBLIC_REPOSITORY = {
    "type": "git",
    "url": "git+https://github.com/malsabbagh/review-sensei.git",
}
PACKAGE_DIR = Path("packages/npm")
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


class NpmAssemblyError(ValueError):
    """Raised when source manifests or native bundles are not publishable."""


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NpmAssemblyError(f"invalid package manifest: {path.name}") from exc
    if not isinstance(value, dict):
        raise NpmAssemblyError(f"package manifest must be an object: {path.name}")
    return value


def project_version(root: Path) -> str:
    try:
        with (root / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle).get("project", {})
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise NpmAssemblyError("project metadata could not be read") from exc
    version = project.get("version") if isinstance(project, dict) else None
    if not isinstance(version, str) or not version:
        raise NpmAssemblyError("project version is missing")
    return version


def _assert_no_lifecycle(manifest: Mapping[str, object]) -> None:
    scripts = manifest.get("scripts")
    if not isinstance(scripts, dict):
        return
    found = sorted(LIFECYCLE_KEYS.intersection(scripts))
    if found:
        raise NpmAssemblyError(
            f"npm lifecycle scripts are forbidden: {', '.join(found)}"
        )


def _assert_publishable_files(manifest: Mapping[str, object]) -> None:
    if manifest.get("files") != list(PACKAGE_FILES):
        raise NpmAssemblyError(
            "npm package files metadata must be exactly bin, README.md, LICENSE"
        )


def validate_source_manifests(root: Path, version: str) -> None:
    launcher_path = root / PACKAGE_DIR / "cli" / "package.json"
    launcher = _read_json(launcher_path)
    if launcher.get("name") != LAUNCHER_NAME or launcher.get("version") != version:
        raise NpmAssemblyError("launcher manifest name/version does not match project")
    if launcher.get("repository") != NPM_PUBLIC_REPOSITORY:
        raise NpmAssemblyError(
            "launcher manifest repository does not match the public source"
        )
    if launcher.get("engines") != {"node": ">=22"}:
        raise NpmAssemblyError("launcher manifest must require Node.js >=22")
    if launcher.get("bin") != {"review-sensei": "bin/review-sensei.js"}:
        raise NpmAssemblyError("launcher manifest has an invalid bin")
    if launcher.get("license") != "MIT":
        raise NpmAssemblyError("launcher manifest must use MIT")
    expected_optional = {item["package"]: version for item in TARGETS.values()}
    if launcher.get("optionalDependencies") != expected_optional:
        raise NpmAssemblyError(
            "launcher optional dependencies are not the exact target set"
        )
    if "dependencies" in launcher:
        raise NpmAssemblyError("launcher must not declare runtime dependencies")
    if launcher.get("publishConfig") != {"access": "public"}:
        raise NpmAssemblyError("launcher publishConfig.access must be public")
    _assert_no_lifecycle(launcher)
    _assert_publishable_files(launcher)

    platforms_root = root / PACKAGE_DIR / "platforms"
    actual = (
        {path.name for path in platforms_root.iterdir() if path.is_dir()}
        if platforms_root.is_dir()
        else set()
    )
    expected_dirs = {str(item["directory"]) for item in TARGETS.values()}
    if actual != expected_dirs:
        raise NpmAssemblyError(
            "platform manifest directories do not match the target set"
        )
    for target_id, target in TARGETS.items():
        manifest = _read_json(
            platforms_root / str(target["directory"]) / "package.json"
        )
        if (
            manifest.get("name") != target["package"]
            or manifest.get("version") != version
        ):
            raise NpmAssemblyError(
                f"platform manifest {target_id} name/version drifted"
            )
        if manifest.get("repository") != NPM_PUBLIC_REPOSITORY:
            raise NpmAssemblyError(
                f"platform manifest {target_id} repository does not match the public source"
            )
        if manifest.get("os") != target["os"] or manifest.get("cpu") != target["cpu"]:
            raise NpmAssemblyError(
                f"platform manifest {target_id} has invalid constraints"
            )
        if "bin" in manifest:
            raise NpmAssemblyError(
                f"platform manifest {target_id} must not expose a bin entry"
            )
        if manifest.get("license") != "MIT":
            raise NpmAssemblyError(f"platform manifest {target_id} must use MIT")
        _assert_no_lifecycle(manifest)
        _assert_publishable_files(manifest)


def _assert_staging(output: Path, root: Path) -> Path:
    root = root.resolve()
    resolved = (output if output.is_absolute() else root / output).resolve()
    allowed = (root / "build", root / "dist")
    if not any(resolved == base or base in resolved.parents for base in allowed):
        raise NpmAssemblyError("npm package staging must be below build/ or dist/")
    if resolved == root:
        raise NpmAssemblyError("npm package staging must not be the repository root")
    return resolved


def _copy_tree(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        raise NpmAssemblyError(
            f"package source is not a regular directory: {source.name}"
        )
    destination.mkdir(parents=True, exist_ok=True)
    for item in sorted(source.iterdir(), key=lambda child: child.name):
        target = destination / item.name
        if item.is_symlink():
            raise NpmAssemblyError(
                f"symlinked package source is forbidden: {item.name}"
            )
        if item.is_dir():
            _copy_tree(item, target)
        elif item.is_file():
            shutil.copy2(item, target)
        else:
            raise NpmAssemblyError(
                f"non-regular package source is forbidden: {item.name}"
            )


def _copy_package_metadata(source: Path, destination: Path) -> None:
    """Copy only the metadata files permitted by each package's files field."""

    destination.mkdir(parents=True, exist_ok=True)
    for name in ("package.json", "README.md", "LICENSE"):
        item = source / name
        if item.is_symlink() or not item.is_file():
            raise NpmAssemblyError(f"package metadata is not a regular file: {name}")
        shutil.copy2(item, destination / name)


def _bundle_payload(bundle: Path, target: Mapping[str, object]) -> tuple[Path, Path]:
    payload = Path(str(target["payload"]))
    filename = payload.name
    if bundle.is_symlink():
        raise NpmAssemblyError("symlinked native bundle is forbidden")
    if bundle.is_file():
        return bundle, Path("bin") / filename
    if not bundle.is_dir():
        raise NpmAssemblyError(f"native bundle is missing for {target['directory']}")
    direct = bundle / filename
    nested = bundle / "bin" / filename
    if direct.is_file():
        return direct, Path("bin") / filename
    if nested.is_file():
        return nested, Path("bin") / filename
    matches = [
        path
        for path in bundle.rglob(filename)
        if path.is_file() and not path.is_symlink()
    ]
    if len(matches) == 1:
        return matches[0], Path("bin") / filename
    raise NpmAssemblyError(f"native bundle has no unique {filename} payload")


def _copy_bundle(bundle: Path, destination: Path, target: Mapping[str, object]) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    # A one-folder PyInstaller bundle needs all sibling libraries/data, not
    # just the executable. Copy its contents into the package's bin/ folder.
    if bundle.is_symlink():
        raise NpmAssemblyError("symlinked native bundle is forbidden")
    if bundle.is_dir():
        source_root = bundle / "bin" if (bundle / "bin").is_dir() else bundle
        if source_root.is_symlink():
            raise NpmAssemblyError("symlinked native bundle members are forbidden")
        for item in sorted(source_root.iterdir(), key=lambda child: child.name):
            dest = destination / item.name
            if item.is_symlink():
                raise NpmAssemblyError("symlinked native bundle members are forbidden")
            if item.is_dir():
                _copy_tree(item, dest)
            elif item.is_file():
                shutil.copy2(item, dest)
            else:
                raise NpmAssemblyError("native bundle contains a non-regular member")
        expected = destination / Path(str(target["payload"])).name
    else:
        source, relative = _bundle_payload(bundle, target)
        expected = destination / relative.name
        shutil.copy2(source, expected)
    if not expected.is_file():
        raise NpmAssemblyError(
            f"native bundle payload missing for {target['directory']}"
        )
    if expected.suffix.lower() != ".exe":
        expected.chmod(
            expected.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        )


def _checksums(output: Path, package_dirs: list[Path]) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for package in sorted(package_dirs, key=lambda path: path.name):
        for path in sorted(package.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            entries.append({"path": str(path.relative_to(output)), "sha256": digest})
    return entries


def assemble_packages(
    root: Path,
    output: Path,
    bundles: Mapping[str, Path],
) -> Path:
    """Copy six validated package trees and native bundles into *output*."""

    root = root.resolve()
    version = project_version(root)
    validate_source_manifests(root, version)
    output = _assert_staging(output, root)
    output.mkdir(parents=True, exist_ok=True)
    # Do not silently retain a previous target, which could create a partial
    # package set after a failed native build.
    for child in output.iterdir():
        if child.is_dir() and (
            child.name == "cli"
            or child.name in {str(item["directory"]) for item in TARGETS.values()}
        ):
            shutil.rmtree(child)

    launcher_destination = output / "cli"
    _copy_package_metadata(root / PACKAGE_DIR / "cli", launcher_destination)
    _copy_tree(root / PACKAGE_DIR / "cli" / "bin", launcher_destination / "bin")
    package_dirs = [launcher_destination]
    for target_id, target in TARGETS.items():
        bundle = bundles.get(target_id)
        if bundle is None:
            raise NpmAssemblyError(f"native bundle is missing for {target_id}")
        destination = output / str(target["directory"])
        _copy_package_metadata(
            root / PACKAGE_DIR / "platforms" / str(target["directory"]), destination
        )
        _copy_bundle(bundle, destination / "bin", target)
        package_dirs.append(destination)

    checksums = _checksums(output, package_dirs)
    (output / "checksums.json").write_text(
        json.dumps({"version": version, "packages": checksums}, indent=2) + "\n",
        encoding="utf-8",
    )
    (output / "SHA256SUMS").write_text(
        "".join(f"{entry['sha256']}  {entry['path']}\n" for entry in checksums),
        encoding="utf-8",
    )
    (output / "package-set.json").write_text(
        json.dumps(
            {
                "version": version,
                "packages": [LAUNCHER_NAME]
                + [str(item["package"]) for item in TARGETS.values()],
                "targets": list(TARGETS),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return output


def _parse_bundles(values: list[str], bundles_root: Path | None) -> dict[str, Path]:
    bundles: dict[str, Path] = {}
    for value in values:
        if "=" not in value:
            raise NpmAssemblyError("--bundle values must be TARGET=PATH")
        target, path = value.split("=", 1)
        if target not in TARGETS or target in bundles:
            raise NpmAssemblyError(f"unknown or duplicate bundle target: {target}")
        bundles[target] = Path(path)
    if bundles_root is not None:
        for target in TARGETS:
            bundles.setdefault(target, bundles_root / target / "review-sensei")
    return bundles


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bundles-root", type=Path)
    parser.add_argument("--bundle", action="append", default=[], metavar="TARGET=PATH")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        bundles = _parse_bundles(args.bundle, args.bundles_root)
        assemble_packages(args.root.resolve(), args.output, bundles)
    except NpmAssemblyError as exc:
        print(f"npm package assembly failed: {exc}", file=sys.stderr)
        return 1
    print(f"npm package set prepared at {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
