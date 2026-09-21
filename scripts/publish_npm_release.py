#!/usr/bin/env python3
"""Publish ReviewSensei npm release bundles with registry readback retries."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

# Workflow steps invoke this file directly, so the checkout root must be importable.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.check_release_version import TAG_PATTERN  # noqa: E402

REGISTRY_USER_AGENT = "review-sensei-npm-publish/1.0"

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
BUNDLE_METADATA_FILENAME = "bundle-metadata.json"
BUNDLE_METADATA_MAX_BYTES = 4096
BUNDLE_TARBALL_MAX_BYTES = 256 * 1024 * 1024


class PublishError(RuntimeError):
    """Raised when npm publication or readback verification fails."""


class RegistryTransportError(PublishError):
    """Raised when the npm registry cannot be reached over the network."""


class IntegrityMismatchError(PublishError):
    """Raised when registry bytes do not match the attested release bundle."""


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


def bundle_metadata_path(bundle_dir: Path) -> Path:
    return bundle_dir / BUNDLE_METADATA_FILENAME


def _is_git_sha(value: str) -> bool:
    return len(value) == 40 and all(
        character in "0123456789abcdef" for character in value
    )


def _is_release_version(value: str) -> bool:
    return bool(TAG_PATTERN.fullmatch(f"v{value}"))


def load_bundle_metadata(bundle_dir: Path) -> dict[str, str] | None:
    path = bundle_metadata_path(bundle_dir)
    if not path.is_file():
        return None
    with path.open("rb") as handle:
        raw = handle.read(BUNDLE_METADATA_MAX_BYTES + 1)
    if len(raw) > BUNDLE_METADATA_MAX_BYTES:
        raise PublishError("release bundle bundle-metadata.json exceeds size limit")
    try:
        metadata = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublishError("release bundle has invalid bundle-metadata.json") from exc
    if not isinstance(metadata, dict):
        raise PublishError("release bundle has invalid bundle-metadata.json")
    allowed_keys = {"version", "source_sha"}
    if set(metadata) != allowed_keys:
        raise PublishError("release bundle bundle-metadata.json has unexpected fields")
    version = metadata.get("version")
    source_sha = metadata.get("source_sha")
    if not isinstance(version, str) or not version:
        raise PublishError("release bundle metadata is missing version")
    if not _is_release_version(version):
        raise PublishError(
            f"release bundle metadata has invalid version at {path}: {version!r}"
        )
    if not isinstance(source_sha, str) or not source_sha:
        raise PublishError("release bundle metadata is missing source_sha")
    if not _is_git_sha(source_sha):
        raise PublishError("release bundle metadata has invalid source_sha")
    return {"version": version, "source_sha": source_sha}


def _is_safe_bundle_member_name(filename: str) -> bool:
    if not filename or filename in {".", ".."}:
        return False
    if filename.startswith("-") or "/" in filename or "\\" in filename:
        return False
    return Path(filename).name == filename


def _parse_sha256sums_entry(line: str, *, sums_path: Path) -> tuple[str, str]:
    stripped = line.strip()
    if not stripped:
        raise PublishError(f"release bundle has empty SHA256SUMS entry in {sums_path}")
    parts = stripped.split(None, 1)
    if len(parts) != 2:
        raise PublishError(
            f"release bundle has invalid SHA256SUMS entry in {sums_path}: {line!r}"
        )
    digest, filename = parts
    if len(digest) != 64 or not all(char in "0123456789abcdef" for char in digest):
        raise PublishError(
            f"release bundle has invalid SHA256SUMS digest in {sums_path}: {line!r}"
        )
    if filename.startswith("*"):
        filename = filename[1:].strip()
    else:
        filename = filename.strip()
    if not _is_safe_bundle_member_name(filename):
        raise PublishError(
            f"release bundle SHA256SUMS entry has unsafe filename in {sums_path}: "
            f"{filename!r}"
        )
    return digest, filename


def _sha256_file(path: Path, *, max_bytes: int) -> str:
    hasher = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            total += len(chunk)
            if total > max_bytes:
                raise PublishError(
                    f"release bundle tarball {path.name!r} exceeds size limit"
                )
            hasher.update(chunk)
    return hasher.hexdigest()


def iter_sha256sum_entries(bundle_dir: Path) -> list[tuple[str, str]]:
    sums_path = bundle_dir / "SHA256SUMS"
    if not sums_path.is_file():
        raise PublishError(f"release bundle is missing SHA256SUMS at {sums_path}")
    entries: list[tuple[str, str]] = []
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entries.append(_parse_sha256sums_entry(line, sums_path=sums_path))
    if not entries:
        raise PublishError(f"release bundle SHA256SUMS is empty at {sums_path}")
    return entries


def list_sha256sum_subjects(bundle_dir: Path) -> list[str]:
    return [filename for _, filename in iter_sha256sum_entries(bundle_dir)]


def list_attestation_subjects(bundle_dir: Path) -> list[str]:
    return [
        filename
        for filename in list_sha256sum_subjects(bundle_dir)
        if filename.endswith(".tgz") or filename == BUNDLE_METADATA_FILENAME
    ]


def _checksum_subject_max_bytes(filename: str) -> int:
    if filename == BUNDLE_METADATA_FILENAME:
        return BUNDLE_METADATA_MAX_BYTES
    return BUNDLE_TARBALL_MAX_BYTES


def verify_bundle_checksums(bundle_dir: Path) -> None:
    for digest, filename in iter_sha256sum_entries(bundle_dir):
        subject_path = bundle_dir / filename
        if not subject_path.is_file():
            raise PublishError(
                f"release bundle is missing subject {filename!r} declared in SHA256SUMS"
            )
        actual_digest = _sha256_file(
            subject_path,
            max_bytes=_checksum_subject_max_bytes(filename),
        )
        if actual_digest != digest:
            raise PublishError(
                f"release bundle subject {filename!r} does not match SHA256SUMS"
            )


def verify_bundle_version(bundle_dir: Path, version: str) -> dict[str, dict[str, str]]:
    """Validate bundle version metadata when present, then load integrity records.

    Bundles without ``bundle-metadata.json`` still pass on the non-resume publish
    path; only ``integrity.jsonl`` is required there for backwards compatibility.
    """
    metadata = load_bundle_metadata(bundle_dir)
    if metadata is not None and metadata["version"] != version:
        raise PublishError(
            "release bundle version "
            f"{metadata['version']!r} does not match requested {version!r}"
        )
    return load_integrity_records(bundle_dir, version)


def verify_resumed_bundle(
    bundle_dir: Path,
    version: str,
    *,
    expected_source_sha: str,
) -> dict[str, dict[str, str]]:
    if not _is_git_sha(expected_source_sha):
        raise PublishError("expected source SHA is not a canonical git commit")
    metadata = load_bundle_metadata(bundle_dir)
    if metadata is None:
        raise PublishError(
            f"resumed release bundle at {bundle_dir} is missing bundle-metadata.json; "
            "only bundles produced after bundle metadata recording can be resumed"
        )
    if metadata["version"] != version:
        raise PublishError(
            "release bundle version "
            f"{metadata['version']!r} does not match requested {version!r}"
        )
    if metadata["source_sha"] != expected_source_sha:
        raise PublishError(
            "resumed release bundle source_sha "
            f"{metadata['source_sha']!r} does not match attested run "
            f"{expected_source_sha!r} in {bundle_dir}"
        )
    verify_bundle_checksums(bundle_dir)
    return load_integrity_records(bundle_dir, metadata["version"])


def load_integrity_records(bundle_dir: Path, version: str) -> dict[str, dict[str, str]]:
    integrity_path = bundle_dir / "integrity.jsonl"
    if not integrity_path.is_file():
        raise PublishError(
            f"release bundle is missing integrity.jsonl at {integrity_path}"
        )
    records = [
        json.loads(line)
        for line in integrity_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    expected = {
        item["name"]: item for item in records if item.get("version") == version
    }
    if set(expected) != set(ALL_PACKAGES) or len(expected) != len(records):
        raise PublishError(
            f"release bundle has an invalid package integrity set in {bundle_dir}"
        )
    return expected


def publish_state_path(bundle_dir: Path) -> Path:
    return bundle_dir / "publish-state.json"


def fetch_package_metadata(package: str) -> dict[str, Any]:
    url = f"https://registry.npmjs.org/{quote(package, safe='')}"
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": REGISTRY_USER_AGENT,
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
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


class PackumentProbeState(str, Enum):
    VERSION_INDEXED = "version_indexed"
    VERSION_ABSENT = "version_absent"
    PACKUMENT_MISSING = "packument_missing"
    INCONCLUSIVE = "inconclusive"


def probe_packument_version_state(package: str, version: str) -> PackumentProbeState:
    try:
        metadata = fetch_package_metadata(package)
    except HTTPError as exc:
        if exc.code == 404:
            return PackumentProbeState.PACKUMENT_MISSING
        if is_retryable_registry_http_error(exc.code):
            return PackumentProbeState.INCONCLUSIVE
        raise
    except RegistryTransportError:
        return PackumentProbeState.INCONCLUSIVE
    versions = metadata.get("versions")
    if isinstance(versions, dict) and version in versions:
        return PackumentProbeState.VERSION_INDEXED
    return PackumentProbeState.VERSION_ABSENT


def package_version_indexed(package: str, version: str) -> bool | None:
    probe = probe_packument_version_state(package, version)
    if probe == PackumentProbeState.VERSION_INDEXED:
        return True
    if probe == PackumentProbeState.INCONCLUSIVE:
        return None
    return False


def fetch_registry_package(package: str, version: str) -> dict[str, Any]:
    url = (
        "https://registry.npmjs.org/"
        f"{quote(package, safe='')}/{quote(version, safe='')}"
    )
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": REGISTRY_USER_AGENT,
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
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
    resuming: bool = False,
) -> str:
    delay = initial_delay_seconds
    last_error = "unknown registry preflight failure"
    visibility_attempts = 0
    packument_seen = False

    def continue_after_ambiguous_preflight(
        final_probe: PackumentProbeState,
        *,
        attempt: int,
        reason: str,
    ) -> str | None:
        nonlocal delay
        if (
            final_probe == PackumentProbeState.PACKUMENT_MISSING
            and not packument_seen
            and (
                attempt >= max_attempts or visibility_attempts >= preflight_404_attempts
            )
        ):
            return "publish"
        # A resumed publish can be racing the previous attempt's replication;
        # a fresh preflight has published nothing, so absence is conclusive.
        if (
            final_probe == PackumentProbeState.VERSION_ABSENT
            and packument_seen
            and (not resuming or attempt >= max_attempts)
        ):
            return "publish"
        if attempt >= max_attempts:
            raise PublishError(
                f"{package}@{version} registry preflight remained "
                f"ambiguous after {max_attempts} attempts ({reason})"
            )
        print(
            f"{package}@{version} registry preflight still "
            f"ambiguous after 404 probes ({reason}); continuing to poll",
            file=sys.stderr,
        )
        time.sleep(min(delay, max_delay_seconds))
        delay = min(delay * 1.5, max_delay_seconds)
        return None

    for attempt in range(1, max_attempts + 1):
        try:
            remote = fetch_registry_package(package, version)
        except HTTPError as exc:
            if exc.code == 404:
                probe = probe_packument_version_state(package, version)
                if probe in {
                    PackumentProbeState.VERSION_INDEXED,
                    PackumentProbeState.VERSION_ABSENT,
                }:
                    packument_seen = True
                if probe == PackumentProbeState.VERSION_INDEXED:
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
                if probe == PackumentProbeState.INCONCLUSIVE:
                    last_error = (
                        f"{package}@{version} registry preflight packument "
                        f"probe inconclusive (attempt {attempt}/{max_attempts})"
                    )
                    if attempt >= max_attempts:
                        raise PublishError(
                            f"{package}@{version} registry preflight packument "
                            f"probe remained inconclusive after {max_attempts} attempts"
                        )
                    print(last_error, file=sys.stderr)
                    time.sleep(min(delay, max_delay_seconds))
                    delay = min(delay * 1.5, max_delay_seconds)
                    continue
                if probe == PackumentProbeState.PACKUMENT_MISSING and packument_seen:
                    last_error = (
                        f"{package}@{version} registry preflight packument "
                        f"transiently missing (attempt {attempt}/{max_attempts})"
                    )
                    if attempt >= max_attempts:
                        raise PublishError(
                            f"{package}@{version} registry preflight packument "
                            f"probe remained inconclusive after {max_attempts} attempts"
                        )
                    print(last_error, file=sys.stderr)
                    time.sleep(min(delay, max_delay_seconds))
                    delay = min(delay * 1.5, max_delay_seconds)
                    continue
                visibility_attempts += 1
                last_error = (
                    f"{package}@{version} registry preflight not visible yet "
                    f"(HTTP 404, probe {visibility_attempts}/{preflight_404_attempts})"
                )
                if (
                    probe == PackumentProbeState.PACKUMENT_MISSING
                    and not packument_seen
                    and attempt >= max_attempts
                ):
                    return "publish"
                if (
                    probe == PackumentProbeState.VERSION_ABSENT
                    and packument_seen
                    and attempt >= max_attempts
                ):
                    return "publish"
                if visibility_attempts >= preflight_404_attempts:
                    final_probe = probe_packument_version_state(package, version)
                    if final_probe in {
                        PackumentProbeState.VERSION_INDEXED,
                        PackumentProbeState.VERSION_ABSENT,
                    }:
                        packument_seen = True
                    action = continue_after_ambiguous_preflight(
                        final_probe,
                        attempt=attempt,
                        reason=final_probe.value,
                    )
                    if action is not None:
                        return action
                    continue
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
            raise IntegrityMismatchError(
                "published npm bytes do not match the attested release bundle: "
                f"{package}@{version}. Reuse the previously attested release "
                "bundle for this version instead of rebuilding."
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
    file_name = record.get("file")
    if not isinstance(file_name, str) or not file_name.endswith(".tgz"):
        raise PublishError(f"invalid tarball name for {package}")
    tarball = (bundle_dir / file_name).resolve()
    bundle_root = bundle_dir.resolve()
    try:
        tarball.relative_to(bundle_root)
    except ValueError as exc:
        raise PublishError(
            f"tarball path escapes bundle directory for {package}"
        ) from exc
    if not tarball.is_file():
        raise PublishError(f"missing release tarball for {package}: {tarball.name}")
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
            raise IntegrityMismatchError(
                "published npm bytes do not match the attested release bundle: "
                f"{package}@{version}. Reuse the previously attested release "
                "bundle for this version instead of rebuilding."
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
        except PublishError as readback_exc:
            if isinstance(readback_exc, IntegrityMismatchError):
                raise readback_exc from exc
            raise exc from readback_exc
        print(
            f"Publish failure recovered via registry readback: {exc}", file=sys.stderr
        )
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
    expected_source_sha: str | None = None,
) -> None:
    if expected_source_sha is not None:
        records = verify_resumed_bundle(
            bundle_dir,
            version,
            expected_source_sha=expected_source_sha,
        )
    else:
        records = verify_bundle_version(bundle_dir, version)
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
            resuming=expected_source_sha is not None,
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
        choices=(
            "preflight",
            "publish-platforms",
            "publish-launcher",
            "verify-bundle",
            "list-sha256sum-subjects",
            "list-attestation-subjects",
        ),
    )
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument(
        "--expected-source-sha",
        help=(
            "When resuming a partial publish, require bundle-metadata.json "
            "source_sha to match this commit."
        ),
    )
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
        if args.command == "verify-bundle":
            if not args.expected_source_sha:
                raise PublishError("verify-bundle requires --expected-source-sha")
            verify_resumed_bundle(
                bundle_dir,
                args.version,
                expected_source_sha=args.expected_source_sha,
            )
        elif args.command == "list-sha256sum-subjects":
            for filename in list_sha256sum_subjects(bundle_dir):
                print(filename)
        elif args.command == "list-attestation-subjects":
            for filename in list_attestation_subjects(bundle_dir):
                print(filename)
        elif args.command == "preflight":
            preflight(
                bundle_dir,
                args.version,
                preflight_404_attempts=args.preflight_404_attempts,
                expected_source_sha=args.expected_source_sha,
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
