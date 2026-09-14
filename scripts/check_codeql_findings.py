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
import math
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import unquote, urlsplit

_SARIF_VERSION = "2.1.0"
_LEVELS = {"none": 0, "note": 1, "warning": 2, "error": 3}
_SHA256 = set("0123456789abcdef")
MAX_SARIF_FILES = 32
MAX_FINDINGS = 4096
MAX_SARIF_FILE_BYTES = 8 * 1024 * 1024
MAX_METADATA_TEXT = 512

# CodeQL's SARIF output does not have a required top-level language field.  The
# action nevertheless emits an analysis category in ``automationDetails.id``
# (and our fixture/producer contract also permits an explicit run property).
# Keep the accepted identities intentionally small and explicit: report names
# are only provenance and never establish language coverage by themselves.
_CODEQL_LANGUAGES = {"python", "javascript-typescript"}
_CODEQL_DRIVER_NAMES = {"CodeQL", "CodeQL command-line toolchain"}
_CODEQL_DRIVER_ORGANIZATION = "GitHub"
_LANGUAGE_CATEGORY_RE = re.compile(
    r"(?:^|/)language:(?P<language>[a-z0-9-]+)(?:/|$)", re.IGNORECASE
)
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_URI_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_RULE_LANGUAGE_PREFIXES = {
    "py": "python",
    "python": "python",
    "js": "javascript-typescript",
    "javascript": "javascript-typescript",
    "javascript-typescript": "javascript-typescript",
    "ts": "javascript-typescript",
    "typescript": "javascript-typescript",
}


def _load_json(path: Path) -> dict[str, Any]:
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(MAX_SARIF_FILE_BYTES + 1)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to read JSON file {path}: {exc}") from exc
    if len(raw) > MAX_SARIF_FILE_BYTES:
        raise ValueError(f"SARIF file {path} exceeds the configured size limit")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to read JSON file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON document {path} must be an object")
    return value


def _canonical_language(value: str) -> str:
    """Return the language name used by this gate's metadata contract."""

    normalized = value.strip().casefold()
    aliases = {
        "javascript": "javascript-typescript",
        "typescript": "javascript-typescript",
        "js": "javascript-typescript",
        "ts": "javascript-typescript",
        "py": "python",
    }
    return aliases.get(normalized, normalized)


def _normalize_expected_languages(expected_languages: Sequence[str]) -> tuple[str, ...]:
    if isinstance(expected_languages, (str, bytes)):
        raise ValueError("expected languages must be a sequence, not a string")
    try:
        values = tuple(expected_languages)
    except TypeError as exc:
        raise ValueError("expected languages must be a sequence") from exc
    languages: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("expected languages must be non-empty strings")
        language = _canonical_language(value)
        if language not in _CODEQL_LANGUAGES:
            raise ValueError(f"unsupported expected CodeQL language: {language}")
        languages.append(language)
    if len(languages) != len(set(languages)):
        raise ValueError("expected languages must be unique")
    return tuple(languages)


def _validate_driver(driver: dict[str, Any], path: Path) -> None:
    """Validate the producer identity before trusting language metadata."""

    name = driver.get("name")
    organization = driver.get("organization")
    if name not in _CODEQL_DRIVER_NAMES or organization != _CODEQL_DRIVER_ORGANIZATION:
        raise ValueError(
            f"{path}: SARIF tool.driver must identify the GitHub CodeQL producer"
        )


def _metadata_languages(run: dict[str, Any], path: Path) -> tuple[str, ...]:
    """Extract one language from the explicit run metadata contract.

    ``run.properties.language``/``languages`` are accepted for producers that
    preserve an explicit language field.  CodeQL Action's documented category
    is represented by ``automationDetails.id`` and uses a ``/language:<id>``
    segment.  Both are evidence, not filename hints; conflicting evidence is
    rejected by :func:`_run_language`.
    """

    evidence: list[str] = []
    properties = run.get("properties")
    if properties is not None:
        if not isinstance(properties, dict):
            raise ValueError(f"{path}: SARIF run.properties must be an object")
        for key in ("language", "languages"):
            if key not in properties:
                continue
            raw = properties[key]
            if key == "language":
                if not isinstance(raw, str) or not raw.strip():
                    raise ValueError(
                        f"{path}: SARIF run.properties.language must be a non-empty string"
                    )
                values = (raw,)
            else:
                if not isinstance(raw, list) or len(raw) != 1:
                    raise ValueError(
                        f"{path}: SARIF run.properties.languages must contain exactly one language"
                    )
                if not isinstance(raw[0], str) or not raw[0].strip():
                    raise ValueError(
                        f"{path}: SARIF run.properties.languages must contain a non-empty string"
                    )
                values = (raw[0],)
            language = _canonical_language(values[0])
            if language not in _CODEQL_LANGUAGES:
                raise ValueError(
                    f"{path}: unsupported SARIF CodeQL language {language}"
                )
            evidence.append(language)

    automation = run.get("automationDetails")
    if automation is not None:
        if not isinstance(automation, dict):
            raise ValueError(f"{path}: SARIF automationDetails must be an object")
        identifier = automation.get("id")
        if not isinstance(identifier, str) or not identifier.strip():
            raise ValueError(
                f"{path}: SARIF automationDetails.id must be a non-empty string"
            )
        if len(identifier) > MAX_METADATA_TEXT:
            raise ValueError(f"{path}: SARIF automationDetails.id is too long")
        matches = list(_LANGUAGE_CATEGORY_RE.finditer(identifier))
        if len(matches) > 1:
            raise ValueError(
                f"{path}: SARIF automationDetails.id has ambiguous language metadata"
            )
        if matches:
            language = _canonical_language(matches[0].group("language"))
            if language not in _CODEQL_LANGUAGES:
                raise ValueError(
                    f"{path}: unsupported SARIF CodeQL language {language}"
                )
            evidence.append(language)

    # Return only explicit run-level evidence. Rule IDs identify individual
    # queries, not the analysis run, and therefore cannot establish coverage.
    return tuple(evidence)


def _run_language(run: dict[str, Any], path: Path) -> str:
    """Resolve one unambiguous CodeQL language for a SARIF run."""

    evidence: list[str] = list(_metadata_languages(run, path))

    results = run.get("results")
    if not isinstance(results, list):
        raise ValueError(f"{path}: SARIF results must be an array")
    rule_languages: set[str] = set()
    for result in results:
        if not isinstance(result, dict):
            continue
        rule_id = result.get("ruleId")
        if not isinstance(rule_id, str) or "/" not in rule_id:
            continue
        prefix = rule_id.split("/", 1)[0].strip().casefold()
        language = _RULE_LANGUAGE_PREFIXES.get(prefix)
        if language:
            rule_languages.add(language)
    if len(rule_languages) > 1:
        raise ValueError(f"{path}: SARIF rule IDs identify multiple CodeQL languages")
    if not evidence:
        raise ValueError(f"{path}: SARIF report lacks trusted CodeQL language metadata")
    languages = set(evidence)
    if len(languages) != 1:
        raise ValueError(f"{path}: SARIF report has conflicting language metadata")
    if rule_languages and rule_languages != languages:
        raise ValueError(f"{path}: SARIF rule IDs conflict with language metadata")
    return next(iter(languages))


def finding_fingerprint(result: dict[str, Any]) -> str:
    """Return a stable identity for a SARIF result."""

    rule_value = result.get("ruleId", "")
    if not isinstance(rule_value, str):
        raise ValueError("SARIF finding ruleId must be a string")
    rule = rule_value
    locations = result.get("locations")
    first = locations[0] if isinstance(locations, list) and locations else {}
    physical = first.get("physicalLocation", {}) if isinstance(first, dict) else {}
    artifact = physical.get("artifactLocation", {})
    region = physical.get("region", {})
    uri_value = artifact.get("uri", "") if isinstance(artifact, dict) else ""
    if not isinstance(uri_value, str):
        raise ValueError("SARIF finding artifactLocation.uri must be a string")
    normalized_uri = _normalize_location(uri_value) if uri_value.strip() else ""
    payload = {
        "rule": rule,
        "uri": normalized_uri,
        "line": region.get("startLine", 0) if isinstance(region, dict) else 0,
    }
    partial = result.get("partialFingerprints")
    if partial is not None:
        if not isinstance(partial, dict) or not partial or len(partial) > 32:
            raise ValueError(
                "SARIF partialFingerprints must be a non-empty bounded object"
            )
        normalized: dict[str, str] = {}
        for key, value in partial.items():
            if (
                not isinstance(key, str)
                or not key.strip()
                or len(key) > 128
                or not isinstance(value, str)
                or not value.strip()
                or len(value) > 1024
            ):
                raise ValueError(
                    "SARIF partialFingerprints keys and values must be bounded strings"
                )
            normalized[key.strip()] = value.strip()
        if not normalized:
            raise ValueError("SARIF partialFingerprints must contain a non-empty value")
        # CodeQL's partial fingerprints are designed to survive harmless line
        # movement.  The repository-relative location is still matched
        # separately by ``evaluate`` so a fingerprint cannot whitelist a
        # finding from another path.
        payload = {"rule": rule, "partialFingerprints": normalized}
    if not payload["rule"] and not payload["uri"]:
        raise ValueError("SARIF finding must include a rule or artifact location")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _normalize_location(uri: str) -> str:
    """Normalize a SARIF/baseline artifact location to a repository path."""

    value = unquote(uri.strip())
    # A repository filename may legitimately contain a colon (for example
    # ``docs/file:example.md``).  Only strip a URI scheme when the URI has the
    # unambiguous ``scheme://`` form; ``urlsplit`` alone would misclassify such
    # filenames as schemes and silently rewrite their identity.
    if _URI_SCHEME_RE.match(value):
        parsed = urlsplit(value)
        if parsed.path:
            value = parsed.path
    value = value.removeprefix("./")
    # Baseline entries may carry the historical ``path:line`` spelling.  Keep
    # only the artifact identity so partial fingerprints can survive line
    # movement while still requiring the same file.
    value = re.sub(r":\d+$", "", value)
    return value


def _result_location(result: dict[str, Any]) -> str:
    """Return a bounded repository-relative location identity for a result."""

    locations = result.get("locations")
    first = locations[0] if isinstance(locations, list) and locations else None
    physical = first.get("physicalLocation") if isinstance(first, dict) else None
    artifact = physical.get("artifactLocation") if isinstance(physical, dict) else None
    uri = artifact.get("uri") if isinstance(artifact, dict) else None
    if not isinstance(uri, str) or not uri.strip():
        return ""
    return _normalize_location(uri)


def _is_safe_location(location: str) -> bool:
    """Return whether a normalized SARIF path is repository-relative."""

    return bool(
        location
        and len(location) <= 1024
        and all(character.isprintable() for character in location)
        and not location.startswith("/")
        and "\\" not in location
        and not _WINDOWS_DRIVE_RE.match(location)
        and all(segment not in {"", ".", ".."} for segment in location.split("/"))
    )


def _result_severity(
    result: dict[str, Any], rule_map: dict[str, dict[str, Any]]
) -> tuple[int, str]:
    raw_level = result.get("level", "warning")
    if not isinstance(raw_level, str):
        raise ValueError("SARIF result level must be a string")
    level = raw_level.lower()
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
    if (
        isinstance(security, bool)
        or not math.isfinite(security_score)
        or not 0 <= security_score <= 10
    ):
        raise ValueError("SARIF security-severity must be finite and between 0 and 10")
    # Security severity 7+ is high/critical even when CodeQL emits a warning.
    if security_score >= 7.0:
        score = max(score, _LEVELS["error"])
        level = "error"
    return score, level


def _sarif_paths(sarif_dir: Path) -> list[Path]:
    """Collect regular SARIF files without following symlink directories."""

    if sarif_dir.is_symlink() or not sarif_dir.is_dir():
        raise ValueError(f"SARIF directory does not exist: {sarif_dir}")
    paths: list[Path] = []
    for directory, directories, filenames in os.walk(
        sarif_dir, topdown=True, followlinks=False
    ):
        directories[:] = sorted(
            name for name in directories if not (Path(directory) / name).is_symlink()
        )
        for name in sorted(filenames):
            path = Path(directory) / name
            if (
                path.suffix.lower() != ".sarif"
                or path.is_symlink()
                or not path.is_file()
            ):
                continue
            paths.append(path)
            if len(paths) > MAX_SARIF_FILES:
                raise ValueError("SARIF directory contains too many reports")
    return sorted(paths, key=lambda path: path.relative_to(sarif_dir).as_posix())


def collect_findings(
    sarif_dir: Path,
    *,
    expected_reports: int = 1,
    expected_languages: Sequence[str] = (),
) -> list[dict[str, Any]]:
    if expected_reports < 1:
        raise ValueError("expected_reports must be positive")
    paths = _sarif_paths(sarif_dir)
    if len(paths) < expected_reports:
        raise ValueError(
            f"expected at least {expected_reports} SARIF files in {sarif_dir}, found {len(paths)}"
        )
    languages = _normalize_expected_languages(expected_languages)
    report_languages: dict[str, Path] = {}
    if languages:
        # In strict language mode every report must be accounted for exactly
        # once.  A minimum file count alone permits duplicate Python reports to
        # masquerade as complete Python + JavaScript/TypeScript coverage.
        if expected_reports > len(languages):
            raise ValueError(
                "expected_reports cannot exceed the number of expected languages"
            )
        if len(paths) != len(languages):
            raise ValueError(
                "expected exactly one SARIF report per expected CodeQL language"
            )
    findings: list[dict[str, Any]] = []
    for path in paths:
        document = _load_json(path)
        if document.get("version") != _SARIF_VERSION:
            raise ValueError(f"{path}: SARIF version must be {_SARIF_VERSION}")
        runs = document.get("runs")
        if not isinstance(runs, list) or not runs:
            raise ValueError(f"{path}: SARIF runs must be a non-empty array")
        report_language: str | None = None
        if languages:
            # CodeQL CLI documents one run per language.  Multiple runs are
            # ambiguous for this gate even when they happen to advertise the
            # same language.
            if len(runs) != 1:
                raise ValueError(
                    f"{path}: SARIF report must contain exactly one CodeQL run"
                )
            if not isinstance(runs[0], dict):
                raise ValueError(f"{path}: SARIF run is incomplete")
            report_language = _run_language(runs[0], path)
            if report_language not in languages:
                raise ValueError(
                    f"{path}: unexpected CodeQL language report {report_language}"
                )
            if report_language in report_languages:
                raise ValueError(
                    f"{path}: duplicate CodeQL language report {report_language}"
                )
            report_languages[report_language] = path
            # A recognizable filename is useful provenance, but it never
            # establishes identity.  Contradictory provenance is rejected so a
            # copied report cannot silently hide a language mismatch.
            stem_language = _canonical_language(path.stem)
            if stem_language in _CODEQL_LANGUAGES and stem_language != report_language:
                raise ValueError(
                    f"{path}: filename language conflicts with SARIF metadata"
                )
        for run in runs:
            if not isinstance(run, dict) or not isinstance(run.get("tool"), dict):
                raise ValueError(f"{path}: SARIF run is incomplete")
            driver = run["tool"].get("driver")
            if not isinstance(driver, dict):
                raise ValueError(f"{path}: SARIF tool.driver is incomplete")
            _validate_driver(driver, path)
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
                if (
                    not isinstance(result, dict)
                    or not isinstance(result.get("ruleId"), str)
                    or not result.get("ruleId")
                ):
                    raise ValueError(f"{path}: SARIF result is incomplete")
                locations = result.get("locations")
                if not isinstance(locations, list) or not locations:
                    raise ValueError(f"{path}: SARIF result has no location")
                location = _result_location(result)
                if not _is_safe_location(location):
                    raise ValueError(
                        f"{path}: SARIF finding location must be a canonical repository path"
                    )
                score, level = _result_severity(result, rule_map)
                findings.append(
                    {
                        "fingerprint": finding_fingerprint(result),
                        "rule": str(result["ruleId"]),
                        "level": level,
                        "score": score,
                        "location": location,
                        "file": path.name,
                        "language": report_language,
                    }
                )
                if len(findings) > MAX_FINDINGS:
                    raise ValueError("SARIF reports contain too many findings")
    if languages and set(report_languages) != set(languages):
        missing = sorted(set(languages) - set(report_languages))
        raise ValueError(
            "missing expected CodeQL language report(s): " + ", ".join(missing)
        )
    return sorted(
        findings,
        key=lambda finding: (
            finding["fingerprint"],
            finding["rule"],
            finding["file"],
            finding["level"],
        ),
    )


def evaluate(
    sarif_dir: Path,
    baseline_path: Path,
    *,
    expected_reports: int = 1,
    expected_languages: Sequence[str] = (),
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
        or any(
            not isinstance(level, str) or level not in _LEVELS for level in fail_levels
        )
    ):
        return ["baseline policy.fail_on_levels must list known severity levels"]
    exception_expiry_days = settings.get("exception_expiry_days")
    if (
        isinstance(exception_expiry_days, bool)
        or not isinstance(exception_expiry_days, int)
        or exception_expiry_days < 1
        or exception_expiry_days > 365
    ):
        return [
            "baseline policy.exception_expiry_days must be an integer between 1 and 365"
        ]
    maximum_expiry = date.today() + timedelta(days=exception_expiry_days)
    try:
        raw_minimum_score = settings.get(
            "minimum_score", max(_LEVELS[level] for level in fail_levels)
        )
        if isinstance(raw_minimum_score, bool) or not isinstance(
            raw_minimum_score, int
        ):
            raise ValueError
        minimum_score = int(raw_minimum_score)
    except (TypeError, ValueError):
        return ["baseline policy.minimum_score must be an integer"]
    if minimum_score < 0 or minimum_score > max(_LEVELS.values()):
        return ["baseline policy.minimum_score must be between 0 and 3"]
    least_fail_score = min(_LEVELS[level] for level in fail_levels)
    if minimum_score > least_fail_score:
        return [
            "baseline policy.minimum_score must not exceed the least severe "
            "baseline policy.fail_on_levels entry"
        ]
    accepted = policy.get("findings", [])
    if not isinstance(accepted, list):
        return ["baseline findings must be an array"]
    accepted_ids: set[tuple[str, str, str]] = set()
    expired_ids: set[tuple[str, str, str]] = set()
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
        fingerprint = item["fingerprint"]
        if len(fingerprint) != 64 or any(
            character not in _SHA256 for character in fingerprint
        ):
            return ["baseline finding fingerprint must be a lowercase SHA-256 digest"]
        try:
            expires_on = date.fromisoformat(item["expires_on"])
        except ValueError:
            return ["baseline finding expires_on must be ISO-8601 date"]
        location = _normalize_location(item["location"])
        if not _is_safe_location(location):
            return ["baseline finding location must be a canonical repository path"]
        key = (fingerprint, item["rule"], location)
        if key in accepted_ids or key in expired_ids:
            return ["baseline findings must not contain duplicate exceptions"]
        if expires_on <= date.today():
            # Keep expired identities so a matching finding can report the
            # actionable reason instead of looking like an unrelated failure.
            expired_ids.add(key)
            continue
        if expires_on > maximum_expiry:
            return [
                "baseline finding expires_on exceeds the configured exception horizon"
            ]
        accepted_ids.add(key)
    try:
        findings = collect_findings(
            sarif_dir,
            expected_reports=expected_reports,
            expected_languages=expected_languages,
        )
    except ValueError as exc:
        return [str(exc)]
    violations = []
    for finding in findings:
        if finding["score"] < minimum_score:
            continue
        key = (finding["fingerprint"], finding["rule"], finding["location"])
        if key in accepted_ids:
            continue
        reason = "expired baseline exception" if key in expired_ids else "unreviewed"
        violations.append(
            f"{reason} {finding['level']} finding {finding['rule']} "
            f"({finding['fingerprint']}) in {finding['location'] or finding['file']}"
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
    parser.add_argument(
        "--expected-languages",
        default="",
        help=(
            "comma-separated CodeQL language identities expected in SARIF "
            "metadata (filenames are not trusted)"
        ),
    )
    args = parser.parse_args(argv)
    if args.expected_reports < 1:
        parser.error("--expected-reports must be positive")
    violations = evaluate(
        args.sarif_dir,
        args.baseline,
        expected_reports=args.expected_reports,
        expected_languages=tuple(
            language.strip()
            for language in args.expected_languages.split(",")
            if language.strip()
        ),
    )
    if violations:
        print("\n".join(violations), file=sys.stderr)
        return 1
    print("CodeQL findings gate passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
