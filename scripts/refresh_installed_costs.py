"""Import advisory installed-unit timings without discovering or selecting tests.

Every reported unit must have a measurement and complete execution accounting.
Failed but fully accounted test runs require explicit opt-in; using their costs
does not turn them into passing qualification or predict current passing paths.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path

_REPORT_FIELDS = {
    "inventory_sha256",
    "discovered",
    "modules",
    "units",
    "uncosted_units",
    "unused_costs",
    "complete",
    "hook_mode",
    "fallback_reason",
    "elapsed_seconds",
    "workers",
}
_WORKER_FIELDS = {
    "worker",
    "exit",
    "elapsed_seconds",
    "assigned",
    "started",
    "fixture_skipped",
    "skip_events",
    "execution_complete",
    "module_seconds",
    "unit_seconds",
}
_UNIT = re.compile(
    r"[A-Za-z_][A-Za-z_0-9]*\.py(?:::[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*)?"
)


def _count(value):
    return type(value) is int and value >= 0


def _seconds(value):
    return type(value) in (int, float) and 0 <= value <= sys.float_info.max


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key in timing receipt")
        result[key] = value
    return result


def load_receipt(contents):
    """Do not let JSON's last-key-wins behavior erase conflicting measurements."""
    return json.loads(contents, object_pairs_hook=_unique_object)


def extract_costs(report, *, allow_failing_tests=False):
    if (
        type(report) is not dict
        or set(report) != _REPORT_FIELDS
        or type(report["inventory_sha256"]) is not str
        or re.fullmatch(r"[0-9a-f]{64}", report["inventory_sha256"]) is None
        or any(
            not _count(report[name]) or report[name] == 0
            for name in ("discovered", "modules", "units")
        )
        or not report["modules"] <= report["units"] <= report["discovered"]
        or type(report["complete"]) is not bool
        or type(report["workers"]) is not list
        or not 1 <= len(report["workers"]) <= 16
        or not _seconds(report["elapsed_seconds"])
        or report["hook_mode"] not in ("hook-free", "reviewed-local", "original-root")
        or (
            report["fallback_reason"] is not None
            and type(report["fallback_reason"]) is not str
        )
        or (
            report["hook_mode"] != "original-root"
            and report["fallback_reason"] is not None
        )
    ):
        raise ValueError("Expected a complete-accounted installed timing receipt")
    for field in ("uncosted_units", "unused_costs"):
        names = report[field]
        if (
            type(names) is not list
            or any(type(name) is not str or not name for name in names)
            or len(names) != len(set(names))
        ):
            raise ValueError("Malformed scheduling-hint metadata")
    costs, modules = {}, set()
    assigned = 0
    tests_passed = True
    for index, worker in enumerate(report["workers"]):
        if (
            type(worker) is not dict
            or set(worker) != _WORKER_FIELDS
            or type(worker["worker"]) is not int
            or worker["worker"] != index
            or type(worker["exit"]) is not int
            or worker["execution_complete"] is not True
            or any(
                not _count(worker[name])
                for name in ("assigned", "started", "fixture_skipped", "skip_events")
            )
            or worker["assigned"] == 0
            or worker["started"] + worker["fixture_skipped"] != worker["assigned"]
            or not _seconds(worker["elapsed_seconds"])
            or worker["elapsed_seconds"] > report["elapsed_seconds"] + 0.001
            or type(worker["module_seconds"]) is not dict
            or type(worker["unit_seconds"]) is not dict
            or len(worker["unit_seconds"]) > worker["started"]
        ):
            raise ValueError("Worker has incomplete or malformed execution accounting")
        tests_passed &= worker["exit"] == 0
        assigned += worker["assigned"]
        by_module = {}
        for name, seconds in worker["unit_seconds"].items():
            if (
                type(name) is not str
                or _UNIT.fullmatch(name) is None
                or not _seconds(seconds)
            ):
                raise ValueError(
                    "Unit timings must be finite nonnegative seconds with unambiguous names"
                )
            if name in costs:
                raise ValueError("Unit timing occurs in multiple workers")
            costs[name] = seconds
            module = name.split("::", 1)[0]
            by_module[module] = by_module.get(module, 0.0) + seconds
        if set(by_module) != set(worker["module_seconds"]):
            raise ValueError("Module and unit timing inventories differ")
        for name, seconds in worker["module_seconds"].items():
            if (
                not _seconds(seconds)
                or not _seconds(by_module[name])
                or not math.isclose(
                    seconds, by_module[name], rel_tol=1e-9, abs_tol=1e-6
                )
            ):
                raise ValueError("Module and unit timing totals differ")
        elapsed = sum(by_module.values())
        if not _seconds(elapsed) or elapsed > worker["elapsed_seconds"] + 0.001:
            raise ValueError("Unit timings exceed worker elapsed time")
        modules.update(by_module)
    if (
        assigned != report["discovered"]
        or len(costs) != report["units"]
        or len(modules) != report["modules"]
    ):
        raise ValueError(
            "Receipt does not account for every discovered test and measured unit"
        )
    if set(report["uncosted_units"]) - costs.keys():
        raise ValueError("Unknown unit in scheduling-hint metadata")
    if report["complete"] is not tests_passed:
        raise ValueError("Aggregate result disagrees with complete worker results")
    if not tests_passed and not allow_failing_tests:
        raise ValueError(
            "Failed tests require --allow-failing-tests for advisory timing use"
        )
    return dict(sorted(costs.items()))


def summary(report, contents):
    return {
        "timing_json_sha256": hashlib.sha256(contents).hexdigest(),
        "inventory_sha256": report["inventory_sha256"],
        "discovered": report["discovered"],
        "modules": report["modules"],
        "measured_units": report["units"],
        "all_workers_execution_complete": all(
            w["execution_complete"] for w in report["workers"]
        ),
        "worker_exits": [w["exit"] for w in report["workers"]],
        "complete": report["complete"],
        "tests_passed": all(w["exit"] == 0 for w in report["workers"]),
        "elapsed_seconds": report["elapsed_seconds"],
        "scheduling_only": True,
        "caveat": "Historical advisory costs only. Failed paths may understate passing runtime; no current qualification or wall-time improvement is implied.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-failing-tests", action="store_true")
    args = parser.parse_args()
    try:
        if args.output.resolve() == args.receipt.resolve() or (
            args.output.exists() and args.output.samefile(args.receipt)
        ):
            raise ValueError("Output must not overwrite the source receipt")
        contents = args.receipt.read_bytes()
        report = load_receipt(contents)
        costs = extract_costs(report, allow_failing_tests=args.allow_failing_tests)
        provenance = summary(report, contents)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    args.output.write_text(json.dumps(costs, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(provenance, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
