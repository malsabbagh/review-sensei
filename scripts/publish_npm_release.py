#!/usr/bin/env python3
"""Publish ReviewSensei npm release bundles with registry readback retries."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import urlopen

PLATFORM_PACKAGES = (
    "@reviewsensei/cli-darwin-arm64",
    "@reviewsensei/cli-darwin-x64",
    "@reviewsensei/cli-linux-arm64-gnu",
    "@reviewsensei/cli-linux-x64-gnu",
    "@reviewsensei/cli-win32-x64",
)
LAUNCHER_PACKAGE = "@reviewsensei/cli"
ALL_PACKAGES = PLATFORM_PACKAGES + (LAUNCHER_PACKAGE,)

DEFAULT_READBACK_ATTEMPTS = 30
DEFAULT_READBACK_INITIAL_DELAY_SECONDS = 2.0
DEFAULT_READBACK_MAX_DELAY_SECONDS = 30.0
DEFAULT_PREFLIGHT_404_ATTEMPTS = 3
DEFAULT_NPM_PUBLISH_TIMEOUT_SECONDS = 600


class PublishError(RuntimeError):
    """Raised when npm publication or readback verification fails."""


class RegistryTransportError(PublishError):
    """Raised when the npm registry cannot be reached over the network."""


def is_retryable_registry_http_error(code: int) -> bool:
    return code in {404, 429} or code >= 500


def retry_delay_seconds(
    exc: HTTPError,
    delay: float,
    max_delay_seconds: float,
) -> float:
    retry_after = exc.headers.get("Retry-After") if exc.headers else None
    if retry_after is not None and retry_after.isdigit():
        return min(float(retry_after), max_delay_seconds)
    return min(delay, max_delay_seconds)


def load_integrity_records(bundle_dir: Path, version: str) -> dict[str, dict[str, str]]:
    integrity_path = bundle_dir / "integrity.jsonl"
    records = [
        json.loads(line)
        for line in integrity_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    expected = {
        item["name"]: item for item in records if item.get("version") == version
    }
    if set(expected) != set(ALL_PACKAGES) or len(expected) != len(records):
        raise PublishError("release bundle has an invalid package integrity set")
    return expected


def publish_state_path(bundle_dir: Path) -> Path:
    return bundle_dir / "publish-state.json"


def fetch_package_metadata(package: str) -> dict[str, Any]:
    url = f"https://registry.npmjs.org/{quote(package, safe='')}"
    try:
        with urlopen(url, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError:
        raise
    except URLError as exc:
        raise RegistryTransportError(
            f"registry request failed for {package}: {exc.reason}"
        ) from exc
    except OSError as exc:
        raise RegistryTransportError(
            f"registry request failed for {package}: {exc}"
        ) from exc


def package_version_indexed(package: str, version: str) -> bool | None:
    try:
        metadata = fetch_package_metadata(package)
    except HTTPError as exc:
        if exc.code == 404:
            return False
        if is_retryable_registry_http_error(exc.code):
            return None
        raise
    except RegistryTransportError:
        return None
    versions = metadata.get("versions")
    return isinstance(versions, dict) and version in versions


def fetch_registry_package(package: str, version: str) -> dict[str, Any]:
    url = (
        "https://registry.npmjs.org/"
        f"{quote(package, safe='')}/{quote(version, safe='')}"
    )
    try:
        with urlopen(url, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError:
        raise
    except URLError as exc:
        raise RegistryTransportError(
            f"registry request failed for {package}: {exc.reason}"
        ) from exc
    except OSError as exc:
        raise RegistryTransportError(
            f"registry request failed for {package}: {exc}"
        ) from exc


def classify_registry_state(
    package: str,
    version: str,
    expected_integrity: str,
    *,
    max_attempts: int = DEFAULT_READBACK_ATTEMPTS,
    preflight_404_attempts: int = DEFAULT_PREFLIGHT_404_ATTEMPTS,
    initial_delay_seconds: float = DEFAULT_READBACK_INITIAL_DELAY_SECONDS,
    max_delay_seconds: float = DEFAULT_READBACK_MAX_DELAY_SECONDS,
) -> str:
    delay = initial_delay_seconds
    last_error = "unknown registry preflight failure"
    visibility_attempts = 0
    for attempt in range(1, max_attempts + 1):
        try:
            remote = fetch_registry_package(package, version)
        except HTTPError as exc:
            if exc.code == 404:
                indexed = package_version_indexed(package, version)
                if indexed is True:
                    last_error = (
                        f"{package}@{version} version document replicating "
                        f"(attempt {attempt}/{max_attempts})"
                    )
                    if attempt >= max_attempts:
                        break
                    print(last_error, file=sys.stderr)
                    time.sleep(min(delay, max_delay_seconds))
                    delay = min(delay * 1.5, max_delay_seconds)
                    continue
                if indexed is None:
                    last_error = (
                        f"{package}@{version} registry preflight packument "
                        f"probe inconclusive (attempt {attempt}/{max_attempts})"
                    )
                    if attempt >= max_attempts:
                        break
                    print(last_error, file=sys.stderr)
                    time.sleep(min(delay, max_delay_seconds))
                    delay = min(delay * 1.5, max_delay_seconds)
                    continue
                visibility_attempts += 1
                last_error = (
                    f"{package}@{version} registry preflight not visible yet "
                    f"(HTTP 404, probe {visibility_attempts}/{preflight_404_attempts})"
                )
                if visibility_attempts >= preflight_404_attempts:
                    final_indexed = package_version_indexed(package, version)
                    if final_indexed is not False:
                        if attempt >= max_attempts:
                            break
                        print(
                            f"{package}@{version} registry preflight still "
                            "ambiguous after 404 probes; continuing to poll",
                            file=sys.stderr,
                        )
                        time.sleep(min(delay, max_delay_seconds))
                        delay = min(delay * 1.5, max_delay_seconds)
                        continue
                    return "publish"
                print(last_error, file=sys.stderr)
                time.sleep(min(delay, max_delay_seconds))
                delay = min(delay * 1.5, max_delay_seconds)
                continue
            if not is_retryable_registry_http_error(exc.code):
                raise PublishError(
                    f"registry preflight failed for {package}: HTTP {exc.code}"
                ) from exc
            last_error = (
                f"{package}@{version} registry preflight not ready yet "
                f"(HTTP {exc.code}, attempt {attempt}/{max_attempts})"
            )
            if attempt >= max_attempts:
                break
            print(last_error, file=sys.stderr)
            time.sleep(retry_delay_seconds(exc, delay, max_delay_seconds))
            delay = min(delay * 1.5, max_delay_seconds)
            continue
        except RegistryTransportError as exc:
            last_error = (
                f"{package}@{version} registry preflight transport failure "
                f"(attempt {attempt}/{max_attempts}): {exc}"
            )
            if attempt >= max_attempts:
                break
            print(last_error, file=sys.stderr)
            time.sleep(min(delay, max_delay_seconds))
            delay = min(delay * 1.5, max_delay_seconds)
            continue

        dist = remote.get("dist")
        if (
            remote.get("name") != package
            or remote.get("version") != version
            or not isinstance(dist, dict)
            or dist.get("integrity") != expected_integrity
        ):
            raise PublishError(
                "published npm bytes do not match the attested release bundle: "
                f"{package}@{version}"
            )
        return "verified"

    raise PublishError(last_error)


def write_publish_state(
    bundle_dir: Path, version: str, packages: list[dict[str, str]]
) -> None:
    publish_state_path(bundle_dir).write_text(
        json.dumps({"version": version, "packages": packages}, indent=2) + "\n",
        encoding="utf-8",
    )


def load_publish_state(bundle_dir: Path) -> dict[str, Any]:
    return json.loads(publish_state_path(bundle_dir).read_text(encoding="utf-8"))


def package_action(bundle_dir: Path, package: str, version: str) -> str:
    state = load_publish_state(bundle_dir)
    if state.get("version") != version:
        raise PublishError(
            "publish-state.json version "
            f"{state.get('version')!r} does not match requested {version!r}"
        )
    matches = [
        item["action"] for item in state["packages"] if item.get("name") == package
    ]
    if len(matches) != 1 or matches[0] not in {"publish", "verified"}:
        raise PublishError(f"missing unique package registry state for {package}")
    return matches[0]


def tarball_for_package(
    bundle_dir: Path,
    package: str,
    version: str,
    records: dict[str, dict[str, str]],
) -> Path:
    record = records[package]
    if record.get("version") != version:
        raise PublishError(f"integrity record version mismatch for {package}")
    tarball = bundle_dir / record["file"]
    if not tarball.is_file():
        raise PublishError(f"missing release tarball for {package}: {tarball}")
    return tarball


def read_back_with_retry(
    package: str,
    version: str,
    expected_integrity: str,
    *,
    max_attempts: int = DEFAULT_READBACK_ATTEMPTS,
    initial_delay_seconds: float = DEFAULT_READBACK_INITIAL_DELAY_SECONDS,
    max_delay_seconds: float = DEFAULT_READBACK_MAX_DELAY_SECONDS,
) -> None:
    delay = initial_delay_seconds
    last_error = "unknown registry readback failure"
    for attempt in range(1, max_attempts + 1):
        try:
            remote = fetch_registry_package(package, version)
        except RegistryTransportError as exc:
            last_error = (
                f"{package}@{version} registry readback transport failure "
                f"(attempt {attempt}/{max_attempts}): {exc}"
            )
            if attempt >= max_attempts:
                break
            print(last_error, file=sys.stderr)
            time.sleep(min(delay, max_delay_seconds))
            delay = min(delay * 1.5, max_delay_seconds)
            continue
        except HTTPError as exc:
            if not is_retryable_registry_http_error(exc.code):
                raise PublishError(
                    f"registry readback failed for {package}: HTTP {exc.code}"
                ) from exc
            last_error = (
                f"{package}@{version} registry readback not ready yet "
                f"(HTTP {exc.code}, attempt {attempt}/{max_attempts})"
            )
            if attempt >= max_attempts:
                break
            print(last_error, file=sys.stderr)
            time.sleep(retry_delay_seconds(exc, delay, max_delay_seconds))
            delay = min(delay * 1.5, max_delay_seconds)
            continue

        dist = remote.get("dist")
        if (
            remote.get("name") != package
            or remote.get("version") != version
            or not isinstance(dist, dict)
            or dist.get("integrity") != expected_integrity
        ):
            raise PublishError(
                "published npm bytes do not match the attested release bundle: "
                f"{package}@{version}"
            )
        print(f"Verified registry readback for {package}@{version}")
        return

    raise PublishError(last_error)


def publish_tarball(
    tarball: Path,
    *,
    timeout_seconds: float = DEFAULT_NPM_PUBLISH_TIMEOUT_SECONDS,
) -> None:
    resolved_tarball = tarball.resolve()
    try:
        subprocess.run(
            [
                "npm",
                "publish",
                "--ignore-scripts",
                str(resolved_tarball),
                "--access",
                "public",
                "--provenance",
            ],
            check=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise PublishError(
            f"npm publish timed out after {timeout_seconds:.0f}s for {tarball.name}"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise PublishError(
            f"npm publish failed with exit code {exc.returncode} for {tarball.name}"
        ) from exc


def publish_package(
    bundle_dir: Path,
    package: str,
    version: str,
    records: dict[str, dict[str, str]],
    *,
    max_attempts: int,
    initial_delay_seconds: float,
    max_delay_seconds: float,
) -> None:
    action = package_action(bundle_dir, package, version)
    record = records.get(package)
    if record is None:
        raise PublishError(f"missing integrity record for {package}")
    expected_integrity = record["integrity"]
    if action == "verified":
        read_back_with_retry(
            package,
            version,
            expected_integrity,
            max_attempts=max_attempts,
            initial_delay_seconds=initial_delay_seconds,
            max_delay_seconds=max_delay_seconds,
        )
        print(f"Registry already contains the attested bytes for {package}@{version}")
        return

    tarball = tarball_for_package(bundle_dir, package, version, records)
    print(f"Publishing {package}@{version} from {tarball.name}")
    try:
        publish_tarball(tarball)
    except PublishError as exc:
        try:
            read_back_with_retry(
                package,
                version,
                expected_integrity,
                max_attempts=max_attempts,
                initial_delay_seconds=initial_delay_seconds,
                max_delay_seconds=max_delay_seconds,
            )
        except PublishError:
            raise exc
        print(
            "Registry already contained the attested bytes after a publish failure for "
            f"{package}@{version}"
        )
        return
    read_back_with_retry(
        package,
        version,
        expected_integrity,
        max_attempts=max_attempts,
        initial_delay_seconds=initial_delay_seconds,
        max_delay_seconds=max_delay_seconds,
    )


def preflight(
    bundle_dir: Path,
    version: str,
    *,
    max_attempts: int,
    preflight_404_attempts: int,
    initial_delay_seconds: float,
    max_delay_seconds: float,
) -> None:
    records = load_integrity_records(bundle_dir, version)
    retry_kwargs = {
        "max_attempts": max_attempts,
        "preflight_404_attempts": preflight_404_attempts,
        "initial_delay_seconds": initial_delay_seconds,
        "max_delay_seconds": max_delay_seconds,
    }
    state = []
    for package in ALL_PACKAGES:
        action = classify_registry_state(
            package,
            version,
            records[package]["integrity"],
            **retry_kwargs,
        )
        state.append({"name": package, "action": action})
        print(f"{package}: {action}")
    write_publish_state(bundle_dir, version, state)


def publish_platforms(
    bundle_dir: Path,
    version: str,
    *,
    max_attempts: int,
    initial_delay_seconds: float,
    max_delay_seconds: float,
) -> None:
    records = load_integrity_records(bundle_dir, version)
    for package in PLATFORM_PACKAGES:
        publish_package(
            bundle_dir,
            package,
            version,
            records,
            max_attempts=max_attempts,
            initial_delay_seconds=initial_delay_seconds,
            max_delay_seconds=max_delay_seconds,
        )


def publish_launcher(
    bundle_dir: Path,
    version: str,
    *,
    max_attempts: int,
    initial_delay_seconds: float,
    max_delay_seconds: float,
) -> None:
    records = load_integrity_records(bundle_dir, version)
    publish_package(
        bundle_dir,
        LAUNCHER_PACKAGE,
        version,
        records,
        max_attempts=max_attempts,
        initial_delay_seconds=initial_delay_seconds,
        max_delay_seconds=max_delay_seconds,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("preflight", "publish-platforms", "publish-launcher"),
    )
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument(
        "--readback-attempts",
        type=int,
        default=DEFAULT_READBACK_ATTEMPTS,
    )
    parser.add_argument(
        "--readback-initial-delay-seconds",
        type=float,
        default=DEFAULT_READBACK_INITIAL_DELAY_SECONDS,
    )
    parser.add_argument(
        "--readback-max-delay-seconds",
        type=float,
        default=DEFAULT_READBACK_MAX_DELAY_SECONDS,
    )
    parser.add_argument(
        "--preflight-404-attempts",
        type=int,
        default=DEFAULT_PREFLIGHT_404_ATTEMPTS,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    bundle_dir = args.bundle_dir
    retry_kwargs = {
        "max_attempts": args.readback_attempts,
        "initial_delay_seconds": args.readback_initial_delay_seconds,
        "max_delay_seconds": args.readback_max_delay_seconds,
    }

    try:
        if args.command == "preflight":
            preflight(
                bundle_dir,
                args.version,
                preflight_404_attempts=args.preflight_404_attempts,
                **retry_kwargs,
            )
        elif args.command == "publish-platforms":
            publish_platforms(bundle_dir, args.version, **retry_kwargs)
        else:
            publish_launcher(bundle_dir, args.version, **retry_kwargs)
    except PublishError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
