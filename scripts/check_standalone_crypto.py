"""Verify the actual frozen Intel cryptography binding and its private OpenSSL."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from build_standalone import StandaloneBuildError, validate_host_target

PROBE = r"""
import importlib
import json
import sys
from pathlib import Path

import cryptography.hazmat.bindings

binding = Path(sys.argv[1]).resolve()
name = "cryptography.hazmat.bindings._rust"
if name in sys.modules:
    raise RuntimeError("oracle Rust binding was loaded before the frozen probe")
cryptography.hazmat.bindings.__path__.insert(0, str(binding.parent))
module = importlib.import_module(name)

from cryptography.hazmat.backends.openssl.backend import backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

assert Path(module.__file__).resolve() == binding
key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
message = b"review-sensei frozen binding ABI regression"
signature = key.sign(message, padding.PKCS1v15(), hashes.SHA256())
key.public_key().verify(signature, message, padding.PKCS1v15(), hashes.SHA256())
print(json.dumps({"binding": str(binding), "rsa_verified": True,
                  "openssl": backend.openssl_version_text()}))
"""


class StandaloneCryptoError(ValueError):
    """Raised for a frozen binding that could recreate an OpenSSL collision."""


def safe_detail(text: str) -> str:
    for name, value in os.environ.items():
        if value and any(
            word in name.upper()
            for word in ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "API_KEY")
        ):
            text = text.replace(value, "<redacted>")
    return text[:4096]


def find_binding(bundle: Path) -> Path:
    directory = bundle.resolve() / "_internal/cryptography/hazmat/bindings"
    bindings = [path for path in directory.glob("_rust*.so") if path.is_file()]
    if len(bindings) != 1:
        raise StandaloneCryptoError("bundle must contain one frozen Rust binding")
    return bindings[0].resolve()


def shared_openssl_dependencies(output: str) -> list[str]:
    dependencies = []
    for line in output.splitlines()[1:]:
        library = line.strip().split(" (", 1)[0]
        if Path(library).name.startswith(("libssl.", "libcrypto.")):
            dependencies.append(library)
    return dependencies


def check_bundle(
    bundle: Path,
    *,
    python_executable: str = sys.executable,
    run=subprocess.run,
) -> dict:
    binding = find_binding(bundle)
    try:
        linked = run(
            ["otool", "-L", str(binding)],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        if shared_openssl_dependencies(linked.stdout):
            raise StandaloneCryptoError(
                "frozen Intel cryptography must not depend on shared OpenSSL"
            )
        # Load the extension FROM THE FROZEN FOLDER, not the installed oracle's
        # original extension. Its rewritten Mach-O dependencies are therefore
        # the dependencies used by the standalone executable. The oracle's
        # matching Python wrappers exercise real signing with a temporary key.
        checked = run(
            [python_executable, "-I", "-c", PROBE, str(binding)],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except subprocess.CalledProcessError as exc:
        detail = safe_detail(exc.stderr or "")
        raise StandaloneCryptoError(
            f"frozen cryptography import/signing failed: {detail}"
        ) from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StandaloneCryptoError(
            "frozen cryptography import/signing failed"
        ) from exc
    try:
        result = json.loads(checked.stdout)
        if (
            not isinstance(result, dict)
            or result.get("binding") != str(binding)
            or result.get("rsa_verified") is not True
            or not isinstance(result.get("openssl"), str)
        ):
            raise ValueError("invalid probe result")
    except (ValueError, TypeError) as exc:
        raise StandaloneCryptoError(
            "frozen cryptography probe result is invalid"
        ) from exc
    return {**result, "shared_openssl_dependencies": [], "check": "passed"}


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
        print(f"standalone crypto check failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
