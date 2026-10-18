"""Separate local engine conformance from live model quality evidence."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any

from .errors import ReviewInputError

CONTROL_PROTOCOL = "fixture-conformance-and-live-quality-v1"


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 1.0


def summarize_cases(
    cases: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, float], dict[str, float]]:
    """Apply the existing numeric metrics without provider identity matching."""

    quality = [case for case in cases if case["kind"] == "quality"]
    expected = sum(case["expected_matches"] for case in quality)
    actual = sum(case["actual_matches"] for case in quality)
    false_positives = sum(case["false_positives"] for case in quality)
    categories = {case["category"] for case in quality if case["status"] == "passed"}
    required = {"correctness", "security", "architecture", "tests"}
    contracts = [case for case in cases if case["kind"] == "contract-rejection"]
    deterministic = {
        "exact_fixture_result_rate": _ratio(
            sum(case["status"] in {"passed", "rejected-expected"} for case in quality),
            len(quality),
        ),
        "expected_contract_rejection_rate": _ratio(
            sum(case["status"] == "rejected-expected" for case in contracts),
            len(contracts),
        ),
    }
    return deterministic, {
        "actionable_precision": _ratio(actual, actual + false_positives),
        "false_positive_rate": false_positives / max(1, len(quality)),
        "expected_finding_recall": _ratio(actual, expected),
        "location_validity": _ratio(
            sum(case["location_valid"] for case in quality), len(quality)
        ),
        "category_coverage": len(categories & required) / len(required),
    }


def threshold_failures(
    deterministic: Mapping[str, float],
    quality: Mapping[str, float],
    thresholds: Mapping[str, Mapping[str, float]],
) -> list[str]:
    """Use every original corpus threshold, with its original direction/name."""

    failures = [
        key
        for key, threshold in thresholds["deterministic"].items()
        if deterministic[key] < threshold
    ]
    for key, threshold in thresholds["quality"].items():
        if key.endswith("_minimum"):
            failed = quality[key.removesuffix("_minimum")] < threshold
        else:
            failed = quality[key.removesuffix("_maximum")] > threshold
        if failed:
            failures.append(key)
    return failures


def fixture_control(
    fixture_report: Mapping[str, Any], corpus_document: Mapping[str, Any]
) -> dict[str, Any]:
    run = fixture_report["run"]
    if (run["mode"], run["provider"], run["model"]) != (
        "fixture",
        "fixture",
        "fixture-v1",
    ):
        raise ReviewInputError("evaluation control requires the local fixture identity")
    return {
        "protocol": CONTROL_PROTOCOL,
        "fixture": {
            "mode": run["mode"],
            "provider": run["provider"],
            "model": run["model"],
            "engine_digest": run["engine_digest"],
            "prompt_digest": run["prompt_digest"],
            "configuration_digest": run["configuration_digest"],
            "corpus_digest": fixture_report["corpus"]["sha256"],
            "review_configuration_id": fixture_report["configuration"][
                "review_configuration_id"
            ],
            "cases": copy.deepcopy(fixture_report["cases"]),
            "deterministic": dict(fixture_report["deterministic"]),
            "quality": dict(fixture_report["quality"]),
            "threshold_failures": list(fixture_report["threshold_failures"]),
            "passed": fixture_report["passed"],
        },
        "thresholds": {
            "deterministic": dict(corpus_document["deterministic_thresholds"]),
            "quality": dict(corpus_document["quality_thresholds"]),
        },
    }


def live_inventory_complete(cases: Sequence[Mapping[str, Any]]) -> bool:
    quality = [case for case in cases if case["kind"] == "quality"]
    return bool(quality) and all(
        case.get("execution_complete") is True and case["provider_calls"] > 0
        for case in quality
    )


def validate_live_control(report: Mapping[str, Any]) -> bool:
    """Cross-check a schema-valid composite report before support is inferred.

    Historical reports still parse but cannot authorize the new protocol.
    This validates consistency, not cryptographic provenance of unsigned report
    producers. Retained byte provenance and independent expected identities
    remain the qualification consumer's separate responsibilities.
    """

    control = report.get("evaluation_control")
    if control is None:
        return False
    if report["run"]["mode"] != "live":
        raise ReviewInputError("evaluation control requires a live report")
    fixture = control["fixture"]
    if any(
        fixture[key] != report["run"][key] for key in ("engine_digest", "prompt_digest")
    ) or (
        fixture["corpus_digest"] != report["corpus"]["sha256"]
        or fixture["review_configuration_id"]
        != report["configuration"]["review_configuration_id"]
    ):
        raise ReviewInputError(
            "evaluation control identity does not match the live run"
        )
    if fixture["configuration_digest"] == report["run"]["configuration_digest"]:
        raise ReviewInputError(
            "fixture and live configuration identities must be distinct"
        )
    cases = report["cases"]
    fixture_cases = fixture["cases"]

    def inventory(values: Sequence[Mapping[str, Any]]) -> list[tuple[str, str, str]]:
        return [(item["id"], item["kind"], item["category"]) for item in values]

    if (
        inventory(cases) != inventory(fixture_cases)
        or len({case["id"] for case in cases}) != len(cases)
        or any(case["status"] == "skipped" for case in fixture_cases)
    ):
        raise ReviewInputError("evaluation control case inventory is incomplete")
    if any(
        case["kind"] == "quality"
        and case["expected_matches"] != expected["expected_matches"]
        for case, expected in zip(cases, fixture_cases)
    ):
        raise ReviewInputError(
            "live expectation inventory does not match fixture control"
        )
    if any(
        case["kind"] == "contract-rejection"
        and (case["status"] != "skipped" or case["provider_calls"] != 0)
        for case in cases
    ):
        raise ReviewInputError("fixture rejection work cannot be counted as live work")
    deterministic, fixture_quality = summarize_cases(fixture_cases)
    _, quality = summarize_cases(cases)
    fixture_failures = threshold_failures(
        deterministic, fixture_quality, control["thresholds"]
    )
    if (
        fixture["deterministic"] != deterministic
        or fixture["quality"] != fixture_quality
        or set(fixture["threshold_failures"]) != set(fixture_failures)
        or fixture["passed"] != (not fixture_failures)
        or report["deterministic"] != deterministic
        or report["quality"] != quality
        or report["metrics"]["provider_calls"]
        != sum(case["provider_calls"] for case in cases)
    ):
        raise ReviewInputError("evaluation control metrics disagree with case evidence")
    complete = live_inventory_complete(cases)
    failures = threshold_failures(deterministic, quality, control["thresholds"])
    if fixture_failures:
        failures.append("fixture_conformance")
    if not complete:
        failures.append("live_quality_inventory")
    if set(report["threshold_failures"]) != set(failures) or report["passed"] != (
        not failures
    ):
        raise ReviewInputError(
            "evaluation control verdict disagrees with case evidence"
        )
    return complete and not failures
