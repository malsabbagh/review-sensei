"""Capture a sanitized parity report while preserving the original smoke oracle."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import re
import ssl
import subprocess
import sys
from pathlib import Path

LIMIT = 4096


def scrub(text: str, environment: dict[str, str]) -> str:
    """Never record inherited credential values or dump the environment."""
    values = {
        value
        for name, value in environment.items()
        if value
        and any(
            word in name.upper()
            for word in ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "API_KEY")
        )
    }
    for value in sorted(values, key=len, reverse=True):
        text = text.replace(value, "<redacted>")
    return text


def stream_record(text: str, cwd: Path, environment: dict[str, str]) -> dict:
    normalized = text.replace(str(cwd.resolve()), "<repository>")
    sanitized = scrub(normalized, environment)
    return {
        "normalized_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        "normalized_utf8_bytes": len(normalized.encode("utf-8")),
        "sanitized_text": sanitized[:LIMIT],
        "truncated": len(sanitized) > LIMIT,
    }


def diagnose(repository: Path, executable: Path, expected_sha: str) -> dict:
    repository = repository.resolve()
    if re.fullmatch(r"[a-f0-9]{40}", expected_sha) is None:
        raise ValueError("expected source must be an immutable commit SHA")
    observed_sha = subprocess.check_output(
        ["git", "-C", str(repository), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed_sha != expected_sha:
        raise ValueError("diagnostic source does not match the released commit")
    spec = importlib.util.spec_from_file_location(
        "release_smoke", repository / "scripts/standalone_smoke.py"
    )
    if spec is None or spec.loader is None:
        raise ValueError("released smoke oracle is unavailable")
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    original_run = smoke._run
    comparisons = []

    def capture(command, cwd, env):
        completed = original_run(command, cwd, env)
        if any("missing-review-sensei-diff.patch" in item for item in command):
            comparisons.append(
                {
                    "role": "native" if len(comparisons) == 0 else "direct",
                    "returncode": completed.returncode,
                    "stdout": stream_record(completed.stdout, cwd, env),
                    "stderr": stream_record(completed.stderr, cwd, env),
                }
            )
        return completed

    smoke._run = capture
    versions = {}
    for name in (
        "review-sensei",
        "pyinstaller",
        "pyinstaller-hooks-contrib",
        "cryptography",
        "cffi",
        "jsonschema",
        "setuptools",
    ):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    report = {
        "source_sha": observed_sha,
        "python": sys.version,
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python_openssl": ssl.OPENSSL_VERSION,
        "github_actions": os.environ.get("GITHUB_ACTIONS") == "true",
        "versions": versions,
        "provider": "fixture",
        "publication_attempted": False,
    }
    try:
        report["cases"] = smoke.run_smoke(
            executable,
            repository=repository,
            diff=repository / "evaluation/v1/diffs/off-by-one.patch",
            fixture_response=repository / "evaluation/v1/responses/off-by-one.json",
            base_ref=subprocess.check_output(
                ["git", "-C", str(repository), "rev-parse", "HEAD^"], text=True
            ).strip(),
            head_ref=observed_sha,
            python_executable=sys.executable,
        )
        report["result"] = "passed"
    except smoke.SmokeError as exc:
        report["result"] = "failed"
        report["failure"] = scrub(str(exc), dict(os.environ))[:LIMIT]
    finally:
        smoke._run = original_run
    report["validation_failure_comparisons"] = comparisons
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = diagnose(args.repository, args.executable, args.expected_sha)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("Immutable-source diagnostic:", report["result"])
    return 0 if report["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
