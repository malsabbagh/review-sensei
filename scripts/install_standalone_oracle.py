"""Install the native-build oracle, isolating Intel cryptography's OpenSSL."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from build_standalone import (
    TARGETS,
    StandaloneBuildError,
    source_check,
    validate_host_target,
)


class OracleInstallError(ValueError):
    """Raised when native dependencies cannot be installed safely."""


def install_oracle(
    root: Path,
    target: str,
    *,
    python_executable: str = sys.executable,
    environment: dict[str, str] | None = None,
    run=subprocess.run,
) -> None:
    root = root.resolve()
    source_check(root)
    validate_host_target(target)
    env = dict(os.environ if environment is None else environment)
    command = [
        python_executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
    ]
    try:
        if target == "darwin-x64":
            prefix = run(
                ["brew", "--prefix", "openssl@3"],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            ).stdout.strip()
            openssl = Path(prefix)
            if not openssl.is_absolute() or not all(
                (openssl / "lib" / name).is_file()
                for name in ("libssl.a", "libcrypto.a")
            ):
                raise OracleInstallError(
                    "Intel OpenSSL static archives are unavailable"
                )
            # x86_64 macOS wheels stopped at cryptography 48. Building against
            # Homebrew's shared OpenSSL lets PyInstaller's basename collection
            # collide with Python's older libssl. Static linkage avoids that
            # collision while preserving the declared dependency range.
            env.update(OPENSSL_STATIC="1", OPENSSL_DIR=str(openssl))
            command.extend(
                ["--no-cache-dir", "--no-binary", "cryptography", "--force-reinstall"]
            )
        command.append(".")
        run(command, cwd=root, env=env, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise OracleInstallError(
            "native oracle dependency installation failed"
        ) from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--target", choices=sorted(TARGETS), required=True)
    args = parser.parse_args()
    try:
        install_oracle(args.root, args.target)
    except (OracleInstallError, StandaloneBuildError) as exc:
        print(f"native oracle install failed: {exc}", file=sys.stderr)
        return 1
    print("Native oracle installed for", args.target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
