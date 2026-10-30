"""Verify the frozen engine bundle excludes hosted runtime and cryptography.

The historical script entrypoint is retained for native qualification callers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from build_standalone import StandaloneBuildError, validate_host_target

PROBE = r"""
import json
import sys
import PyInstaller
from PyInstaller.archive.readers import CArchiveReader

archive = CArchiveReader(sys.argv[1])
modules = []
pyz_archives = 0
for name, entry in archive.toc.items():
    if entry[-1] == "z":
        pyz_archives += 1
        modules.extend(archive.open_embedded_archive(name).toc)
print(json.dumps({"schema_version": 1, "pyinstaller": PyInstaller.__version__,
                  "archive_entries": sorted(archive.toc),
                  "python_modules": sorted(modules), "pyz_archives": pyz_archives}))
"""


class StandaloneCryptoError(ValueError):
    """Raised when the actual engine bundle boundary cannot be verified."""


def safe_detail(text: str) -> str:
    for name, value in os.environ.items():
        if value and any(
            word in name.upper()
            for word in ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "API_KEY")
        ):
            text = text.replace(value, "<redacted>")
    return text[:4096]


def _forbidden_entry(name: str) -> bool:
    parts = name.replace("\\", "/").replace(".", "/").split("/")
    return any(part in {"cryptography", "review_sensei_github"} for part in parts)


def check_bundle(
    bundle: Path,
    *,
    python_executable: str = sys.executable,
    run=subprocess.run,
) -> dict:
    bundle = bundle.resolve()
    if not bundle.is_dir():
        raise StandaloneCryptoError("engine bundle is missing")
    executables = [
        path
        for name in ("review-sensei", "review-sensei.exe")
        if (path := bundle / name).is_file()
    ]
    if len(executables) != 1 or not os.access(executables[0], os.X_OK):
        raise StandaloneCryptoError("engine bundle executable is missing or ambiguous")
    collected = [
        path.relative_to(bundle).as_posix()
        for path in bundle.rglob("*")
        if path.is_file()
    ]
    if any(_forbidden_entry(name) for name in collected):
        raise StandaloneCryptoError(
            "engine bundle contains a forbidden hosted or cryptography package"
        )
    try:
        checked = run(
            [python_executable, "-I", "-c", PROBE, str(executables[0])],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except subprocess.CalledProcessError as exc:
        raise StandaloneCryptoError(
            f"engine bundle inventory failed: {safe_detail(exc.stderr or '')}"
        ) from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StandaloneCryptoError("engine bundle inventory failed") from exc
    try:
        inventory = json.loads(checked.stdout)
        if not isinstance(inventory, dict) or set(inventory) != {
            "schema_version",
            "pyinstaller",
            "archive_entries",
            "python_modules",
            "pyz_archives",
        }:
            raise ValueError("invalid inventory")
        if (
            inventory["schema_version"] != 1
            or not isinstance(inventory["pyinstaller"], str)
            or not inventory["pyinstaller"]
            or type(inventory["pyz_archives"]) is not int
            or inventory["pyz_archives"] < 1
        ):
            raise ValueError("invalid inventory")
        for field in ("archive_entries", "python_modules"):
            entries = inventory[field]
            if (
                not isinstance(entries, list)
                or not entries
                or any(not isinstance(name, str) or not name for name in entries)
            ):
                raise ValueError("invalid inventory")
        if not any(
            name == "review_sensei" or name.startswith("review_sensei.")
            for name in inventory["python_modules"]
        ):
            raise ValueError("missing engine modules")
    except (ValueError, TypeError) as exc:
        raise StandaloneCryptoError("engine bundle inventory is invalid") from exc
    if any(
        _forbidden_entry(name)
        for field in ("archive_entries", "python_modules")
        for name in inventory[field]
    ):
        raise StandaloneCryptoError(
            "engine bundle contains a forbidden hosted or cryptography package"
        )
    return {
        "check": "passed",
        "boundary": "engine-only",
        "executable": str(executables[0]),
        "collected_files": len(collected),
        "archive_entries": len(inventory["archive_entries"]),
        "python_modules": len(inventory["python_modules"]),
        "pyinstaller": inventory["pyinstaller"],
        "inventory_sha256": hashlib.sha256(
            json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        validate_host_target("darwin-x64")
        report = check_bundle(args.bundle)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(
                json.dumps(report, indent=2) + "\n", encoding="utf-8"
            )
    except (StandaloneCryptoError, StandaloneBuildError, OSError) as exc:
        if args.report:
            try:
                args.report.parent.mkdir(parents=True, exist_ok=True)
                args.report.write_text(
                    json.dumps({"check": "failed", "error": safe_detail(str(exc))})
                    + "\n",
                    encoding="utf-8",
                )
            except OSError:
                pass
        print(f"standalone engine boundary check failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
