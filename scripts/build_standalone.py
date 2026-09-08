#!/usr/bin/env python3
"""Build one native ReviewSensei PyInstaller folder on a matching host."""

from __future__ import annotations

import argparse
import platform as host_platform
import re
import subprocess
import sys
import tomllib
from pathlib import Path

PYINSTALLER_REQUIREMENTS = Path("packaging/standalone/requirements.txt")
PYINSTALLER_SPEC = Path("packaging/standalone/review-sensei.spec")
ENTRYPOINT = Path("packaging/standalone/entrypoint.py")
PYINSTALLER_VERSION = "6.16.0"

TARGETS: dict[str, tuple[str, str]] = {
    "darwin-arm64": ("darwin", "arm64"),
    "darwin-x64": ("darwin", "x64"),
    "linux-arm64-gnu": ("linux", "arm64"),
    "linux-x64-gnu": ("linux", "x64"),
    "win32-x64": ("win32", "x64"),
}


class StandaloneBuildError(ValueError):
    """Raised when a native build cannot be performed safely."""


def _under(path: Path, parents: tuple[Path, ...]) -> bool:
    resolved = path.resolve()
    return any(resolved == parent or parent in resolved.parents for parent in parents)


def staging_path(path: Path, root: Path) -> Path:
    """Resolve *path* and ensure it is below ignored build/dist staging."""

    resolved_root = root.resolve()
    resolved = path if path.is_absolute() else resolved_root / path
    resolved = resolved.resolve()
    allowed = (resolved_root / "build", resolved_root / "dist")
    if not _under(resolved, allowed):
        raise StandaloneBuildError("standalone output must be below build/ or dist/")
    if resolved == resolved_root:
        raise StandaloneBuildError("standalone output must not be the repository root")
    return resolved


def pinned_pyinstaller_version(requirements: Path) -> str:
    try:
        lines = requirements.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise StandaloneBuildError(
            "PyInstaller requirements could not be read"
        ) from exc
    pins = [
        match.group(1)
        for line in lines
        if (match := re.fullmatch(r"\s*pyinstaller==(\d+\.\d+\.\d+)\s*", line, re.I))
    ]
    if pins != [PYINSTALLER_VERSION]:
        raise StandaloneBuildError("PyInstaller must have the pinned exact version")
    return pins[0]


def source_check(root: Path) -> None:
    requirements = root / PYINSTALLER_REQUIREMENTS
    pinned_pyinstaller_version(requirements)
    for relative in (PYINSTALLER_SPEC, ENTRYPOINT):
        path = root / relative
        if not path.is_file():
            raise StandaloneBuildError(f"standalone source file is missing: {relative}")
    spec = (root / PYINSTALLER_SPEC).read_text(encoding="utf-8")
    if (
        'copy_metadata("review-sensei")' not in spec
        and "copy_metadata('review-sensei')" not in spec
    ):
        raise StandaloneBuildError(
            "PyInstaller spec does not collect review-sensei metadata"
        )
    if "collect_data_files" not in spec or 'name="review-sensei"' not in spec:
        raise StandaloneBuildError(
            "PyInstaller spec does not describe the ReviewSensei bundle"
        )


def _host_platform() -> str:
    value = sys.platform
    if value.startswith("darwin"):
        return "darwin"
    if value.startswith("linux"):
        return "linux"
    if value.startswith("win32") or value.startswith("cygwin"):
        return "win32"
    return value


def _host_arch() -> str:
    value = host_platform.machine().lower()
    if value in {"x86_64", "amd64", "x64"}:
        return "x64"
    if value in {"aarch64", "arm64"}:
        return "arm64"
    return value


def validate_host_target(target_id: str) -> tuple[str, str]:
    try:
        target_platform, target_arch = TARGETS[target_id]
    except KeyError as exc:
        raise StandaloneBuildError(
            f"unsupported standalone target: {target_id}"
        ) from exc
    if _host_platform() != target_platform or _host_arch() != target_arch:
        raise StandaloneBuildError(
            f"native build requires matching host for {target_id}; cross-compilation is disabled"
        )
    if target_platform == "linux":
        libc_name, _version = host_platform.libc_ver()
        if libc_name.lower() != "glibc":
            raise StandaloneBuildError("Linux standalone builds require a glibc host")
    return target_platform, target_arch


def project_version(root: Path) -> str:
    try:
        with (root / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle).get("project", {})
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise StandaloneBuildError("project metadata could not be read") from exc
    version = project.get("version") if isinstance(project, dict) else None
    if not isinstance(version, str) or not version:
        raise StandaloneBuildError("project version is missing")
    return version


def build_standalone(
    root: Path,
    target_id: str,
    output: Path,
    *,
    python_executable: str | None = None,
    run=subprocess.run,
) -> Path:
    """Build and return the one-folder output path for a native target."""

    source_check(root)
    validate_host_target(target_id)
    output_path = staging_path(output, root)
    output_path.mkdir(parents=True, exist_ok=True)
    work_path = staging_path(root / "build" / "pyinstaller" / target_id, root)
    work_path.mkdir(parents=True, exist_ok=True)
    python_bin = python_executable or sys.executable
    command = [
        python_bin,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(output_path),
        "--workpath",
        str(work_path),
        str(root / PYINSTALLER_SPEC),
    ]
    try:
        run(command, cwd=root, check=True, shell=False)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise StandaloneBuildError("pinned PyInstaller build failed") from exc
    bundle = output_path / "review-sensei"
    if _host_platform() == "win32":
        executable = bundle / "review-sensei.exe"
    else:
        executable = bundle / "review-sensei"
    if not executable.is_file():
        raise StandaloneBuildError(
            "PyInstaller did not produce the expected executable"
        )
    return bundle


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--target", choices=sorted(TARGETS))
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate the pinned spec and staging rules without requiring PyInstaller",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.root.resolve()
    try:
        source_check(root)
        if args.check:
            if args.output:
                staging_path(args.output, root)
            if args.target:
                # --check is intentionally host-independent when no target is
                # requested; an explicit target still gets host validation.
                validate_host_target(args.target)
            print(f"standalone source check passed (PyInstaller {PYINSTALLER_VERSION})")
            return 0
        if not args.target or not args.output:
            raise StandaloneBuildError(
                "--target and --output are required unless --check is used"
            )
        build_standalone(root, args.target, args.output)
    except StandaloneBuildError as exc:
        print(f"standalone build check failed: {exc}", file=sys.stderr)
        return 1
    print(f"standalone build prepared for {args.target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
