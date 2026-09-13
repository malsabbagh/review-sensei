"""Fail closed when CodeQL SARIF findings exceed the reviewed policy.

This checker is intentionally deterministic and does not upload results.  The
CodeQL action writes SARIF to a temporary directory; this script validates the
envelope, applies the repository policy, and compares findings with the
reviewed baseline.  A missing, malformed, or incomplete report is an error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

_SARIF_VERSION = "2.1.0"
_LEVELS = {"none": 0, "note": 1, "warning": 2, "error": 3}


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to read JSON file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON document {path} must be an object")
    return value


def finding_fingerprint(result: dict[str, Any]) -> str:
    """Return a stable identity for a SARIF result."""

    rule = str(result.get("ruleId", ""))
    locations = result.get("locations")
    first = locations[0] if isinstance(locations, list) and locations else {}
    physical = first.get("physicalLocation", {}) if isinstance(first, dict) else {}
    artifact = physical.get("artifactLocation", {})
    region = physical.get("region", {})
    payload = {
        "rule": rule,
        "uri": artifact.get("uri", "") if isinstance(artifact, dict) else "",
        "line": region.get("startLine", 0) if isinstance(region, dict) else 0,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _result_severity(
    result: dict[str, Any], rule_map: dict[str, dict[str, Any]]
) -> tuple[int, str]:
    level = str(result.get("level", "warning")).lower()
    score = _LEVELS.get(level, -1)
    if score < 0:
        raise ValueError(f"unsupported SARIF result level: {level}")
    rule = rule_map.get(str(result.get("ruleId", "")), {})
    properties = rule.get("properties", {}) if isinstance(rule, dict) else {}
    security = (
        properties.get("security-severity") if isinstance(properties, dict) else None
    )
    try:
        security_score = float(security) if security is not None else 0.0
    except (TypeError, ValueError) as exc:
        raise ValueError("SARIF security-severity must be numeric") from exc
    # Security severity 7+ is high/critical even when CodeQL emits a warning.
    if security_score >= 7.0:
        score = max(score, _LEVELS["error"])
    return score, level


def collect_findings(
    sarif_dir: Path, *, expected_reports: int = 1
) -> list[dict[str, Any]]:
    if not sarif_dir.is_dir():
        raise ValueError(f"SARIF directory does not exist: {sarif_dir}")
    paths = sorted(path for path in sarif_dir.rglob("*.sarif") if path.is_file())
    if len(paths) < expected_reports:
        raise ValueError(
            f"expected at least {expected_reports} SARIF files in {sarif_dir}, found {len(paths)}"
        )
    findings: list[dict[str, Any]] = []
    for path in paths:
        document = _load_json(path)
        if document.get("version") != _SARIF_VERSION:
            raise ValueError(f"{path}: SARIF version must be {_SARIF_VERSION}")
        runs = document.get("runs")
        if not isinstance(runs, list) or not runs:
            raise ValueError(f"{path}: SARIF runs must be a non-empty array")
        for run in runs:
            if not isinstance(run, dict) or not isinstance(run.get("tool"), dict):
                raise ValueError(f"{path}: SARIF run is incomplete")
            driver = run["tool"].get("driver")
            if not isinstance(driver, dict):
                raise ValueError(f"{path}: SARIF tool.driver is incomplete")
            rules = driver.get("rules", [])
            rule_map = {
                str(rule.get("id")): rule
                for rule in rules
                if isinstance(rule, dict) and rule.get("id")
            }
            results = run.get("results")
            if not isinstance(results, list):
                raise ValueError(f"{path}: SARIF results must be an array")
            for result in results:
                if not isinstance(result, dict) or not result.get("ruleId"):
                    raise ValueError(f"{path}: SARIF result is incomplete")
                locations = result.get("locations")
                if not isinstance(locations, list) or not locations:
                    raise ValueError(f"{path}: SARIF result has no location")
                score, level = _result_severity(result, rule_map)
                findings.append(
                    {
                        "fingerprint": finding_fingerprint(result),
                        "rule": str(result["ruleId"]),
                        "level": level,
                        "score": score,
                        "file": path.name,
                    }
                )
    return findings


def evaluate(
    sarif_dir: Path, baseline_path: Path, *, expected_reports: int = 1
) -> list[str]:
    try:
        policy = _load_json(baseline_path)
    except ValueError as exc:
        return [str(exc)]
    if policy.get("version") != 1:
        return ["baseline version must be 1"]
    settings = policy.get("policy")
    if not isinstance(settings, dict):
        return ["baseline policy must be an object"]
    fail_levels = settings.get("fail_on_levels")
    if (
        not isinstance(fail_levels, list)
        or not fail_levels
        or any(level not in _LEVELS for level in fail_levels)
    ):
        return ["baseline policy.fail_on_levels must list known severity levels"]
    try:
        minimum_score = int(
            settings.get("minimum_score", max(_LEVELS[level] for level in fail_levels))
        )
    except (TypeError, ValueError):
        return ["baseline policy.minimum_score must be an integer"]
    accepted = policy.get("findings", [])
    if not isinstance(accepted, list):
        return ["baseline findings must be an array"]
    accepted_ids: set[str] = set()
    for item in accepted:
        if not isinstance(item, dict) or not all(
            isinstance(item.get(key), str) and item.get(key)
            for key in (
                "fingerprint",
                "rule",
                "location",
                "rationale",
                "owner",
                "expires_on",
            )
        ):
            return [
                "each baseline finding requires fingerprint, rule, location, rationale, owner, and expires_on"
            ]
        accepted_ids.add(item["fingerprint"])
    try:
        findings = collect_findings(sarif_dir, expected_reports=expected_reports)
    except ValueError as exc:
        return [str(exc)]
    violations = []
    for finding in findings:
        if (
            finding["score"] >= minimum_score
            and finding["fingerprint"] not in accepted_ids
        ):
            violations.append(
                f"unreviewed {finding['level']} finding {finding['rule']} "
                f"({finding['fingerprint']}) in {finding['file']}"
            )
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sarif-dir", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument(
        "--expected-reports",
        type=int,
        default=1,
        help="minimum SARIF files expected (CI uses one report per analyzed language)",
    )
    args = parser.parse_args(argv)
    if args.expected_reports < 1:
        parser.error("--expected-reports must be positive")
    violations = evaluate(
        args.sarif_dir, args.baseline, expected_reports=args.expected_reports
    )
    if violations:
        print("\n".join(violations), file=sys.stderr)
        return 1
    print("CodeQL findings gate passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
