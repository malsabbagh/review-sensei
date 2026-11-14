"""Refresh scheduling hints from complete checkout timing receipts, never test scope.

Test failures may have shorter paths than passing tests. Importing their timings
therefore requires an explicit flag and does not turn their receipts into a pass.
This offline helper neither imports tests nor starts workers or CI.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def _count(value: object) -> bool:
    return type(value) is int and value >= 0


def _seconds(value: object) -> bool:
    return type(value) in (int, float) and 0 <= value <= sys.float_info.max


def extract_costs(report: object, *, allow_failing_tests: bool = False) -> dict:
    """Require complete execution accounting before using advisory measurements."""
    if (
        not isinstance(report, dict)
        or report.get("execution_complete") is not True
        or report.get("complete") is not True
        or (report.get("coverage_ok") is not None and report["coverage_ok"] is not True)
        or report.get("coverage_error") is not None
        or not _count(report.get("discovered"))
        or report["discovered"] == 0
        or not isinstance(report.get("inventory_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", report["inventory_sha256"]) is None
        or not isinstance(report.get("workers"), list)
        or type(report.get("effective_worker_count")) is not int
        or report["effective_worker_count"] != len(report["workers"])
    ):
        raise ValueError("Expected a complete checkout execution receipt")
    entries = list(report["workers"])
    controller = report.get("controller")
    if controller is not None:
        entries.append(controller)
    if not entries:
        raise ValueError("Timing receipt has no executed phases")
    costs: dict[str, float] = {}
    assigned = 0
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or entry.get("timing_error") is not None
            or entry.get("inventory_complete") is not True
            or not all(
                _count(entry.get(key))
                for key in ("assigned", "executed", "fixture_skipped")
            )
            or entry["assigned"] != entry["executed"] + entry["fixture_skipped"]
            or type(entry.get("exit")) is not int
            or not _seconds(entry.get("elapsed_seconds"))
            or not isinstance(entry.get("class_seconds"), dict)
        ):
            raise ValueError("Timing phase has incomplete or malformed accounting")
        if entry["exit"] != 0 and not allow_failing_tests:
            raise ValueError(
                "Failed tests require --allow-failing-tests for timing use"
            )
        assigned += entry["assigned"]
        for name, seconds in entry["class_seconds"].items():
            if not isinstance(name, str) or not name or not _seconds(seconds):
                raise ValueError("Class timings must be finite nonnegative seconds")
            if name in costs:
                raise ValueError("Class timing occurs in multiple execution phases")
            # The runner requires positive costs. Do not round a fast test to zero.
            costs[name] = max(float(seconds), 0.000001)
    if assigned != report["discovered"]:
        raise ValueError("Timing phases do not account for the discovered inventory")
    if not costs:
        raise ValueError("Timing receipt contains no measured classes")
    return dict(sorted(costs.items()))


def select_costs(costs: dict, classes: object) -> tuple[dict, list[str], list[str]]:
    """Remove obsolete hints, not tests; unmeasured classes keep runner fallback."""
    if (
        not isinstance(classes, list)
        or not classes
        or any(not isinstance(name, str) or not name for name in classes)
        or len(classes) != len(set(classes))
    ):
        raise ValueError("Expected distinct nonempty discovered class names")
    selected = set(classes)
    return (
        {name: value for name, value in costs.items() if name in selected},
        sorted(set(costs) - selected),
        sorted(selected - set(costs)),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path)
    parser.add_argument("--classes", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-failing-tests", action="store_true")
    args = parser.parse_args()
    try:
        report = json.loads(args.receipt.read_text(encoding="utf-8"))
        costs = extract_costs(report, allow_failing_tests=args.allow_failing_tests)
        selected, obsolete, unmeasured = select_costs(
            costs, json.loads(args.classes.read_text(encoding="utf-8"))
        )
        if not selected:
            raise ValueError("No measured class exists in the selected inventory")
    except (OSError, ValueError) as error:
        parser.error(str(error))
    args.output.write_text(json.dumps(selected, indent=2) + "\n", encoding="utf-8")
    phases = [*report["workers"]]
    if report.get("controller") is not None:
        phases.append(report["controller"])
    print(
        json.dumps(
            {
                "inventory_sha256": report["inventory_sha256"],
                "tests_passed": all(phase["exit"] == 0 for phase in phases),
                "measured_classes": len(selected),
                "obsolete_hints": obsolete,
                "unmeasured_classes": unmeasured,
                "scheduling_only": True,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
