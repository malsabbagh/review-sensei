"""Prove that sharded test runners executed one complete inventory exactly once.

Each shard of scripts/run_checkout_tests.py or scripts/run_installed_test_suites.py
computes the same complete plan and executes only its own buckets. This offline
check accepts the lane only when every shard reported the same inventory and
plan, the shards' buckets partition the plan, every assigned test is accounted
for, and every shard succeeded. A missing, duplicated or failed shard fails the
lane. The merged receipt keeps the single-runner report format for timing tools.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


class ShardError(ValueError):
    pass


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise ShardError(f"{path}: unreadable shard report ({type(error).__name__})")
    if not isinstance(data, dict) or not isinstance(data.get("shard"), dict):
        raise ShardError(f"{path}: report has no shard section")
    return data


def _count(value, label):
    if type(value) is not int or value < 0:
        raise ShardError(f"{label} is not a nonnegative integer")
    return value


def _shard_accounting(kind: str, data: dict, label: str) -> tuple[int, list[str]]:
    """Return the shard's accounted test count and any failure reasons."""
    failures = []
    workers = data.get("workers")
    if not isinstance(workers, list):
        raise ShardError(f"{label}: workers are missing")
    accounted = 0
    for worker in workers:
        if not isinstance(worker, dict):
            raise ShardError(f"{label}: worker entry is invalid")
        assigned = _count(worker.get("assigned"), f"{label} worker assigned")
        started_key = "executed" if kind == "checkout" else "started"
        started = worker.get(started_key)
        skipped = worker.get("fixture_skipped")
        if type(started) is not int or type(skipped) is not int:
            failures.append(f"worker {worker.get('worker')} has no complete accounting")
        elif started + skipped != assigned:
            failures.append(
                f"worker {worker.get('worker')} did not account for its tests"
            )
        complete_key = (
            "inventory_complete" if kind == "checkout" else "execution_complete"
        )
        if worker.get(complete_key) is not True:
            failures.append(f"worker {worker.get('worker')} inventory is incomplete")
        if worker.get("exit") != 0:
            failures.append(
                f"worker {worker.get('worker')} exited {worker.get('exit')}"
            )
        accounted += assigned
    if kind == "checkout":
        controller = data.get("controller")
        if controller is not None:
            if not isinstance(controller, dict):
                raise ShardError(f"{label}: controller entry is invalid")
            accounted += _count(
                controller.get("assigned"), f"{label} controller assigned"
            )
            if controller.get("exit") != 0:
                failures.append(f"controller exited {controller.get('exit')}")
            if controller.get("inventory_complete") is not True:
                failures.append("controller inventory is incomplete")
        if data.get("execution_complete") is not True:
            failures.append("execution is incomplete")
        if data.get("coverage_ok") is False:
            failures.append(f"coverage failed: {data.get('coverage_error')}")
    if data.get("complete") is not True:
        failures.append("shard did not report completion")
    return accounted, failures


def verify(paths: list[Path], shards: int) -> tuple[dict, list[str]]:
    if not 1 <= shards <= 16:
        raise ShardError("expected shard count must be within 1..16")
    reports = [(path, _load(path)) for path in paths]
    kinds = {"checkout" if "mode" in data else "installed" for _path, data in reports}
    if len(kinds) != 1:
        raise ShardError("shard reports mix checkout and installed runners")
    kind = kinds.pop()
    indices = sorted(data["shard"].get("index") for _path, data in reports)
    if indices != list(range(1, shards + 1)):
        raise ShardError(
            f"expected shards 1..{shards} exactly once, received {indices}"
        )
    first = reports[0][1]
    for key in ("inventory_sha256", "discovered"):
        if any(data.get(key) != first.get(key) for _path, data in reports):
            raise ShardError(f"shards disagree on {key}")
    for key in ("count", "plan_sha256", "plan_buckets"):
        if any(
            data["shard"].get(key) != first["shard"].get(key) for _path, data in reports
        ):
            raise ShardError(f"shards disagree on plan {key}")
    if first["shard"]["count"] != shards:
        raise ShardError("shard reports were produced for a different shard count")
    plan_buckets = first["shard"]["plan_buckets"]
    if not isinstance(plan_buckets, list) or len(set(plan_buckets)) != len(
        plan_buckets
    ):
        raise ShardError("plan buckets are invalid")
    owned = [
        bucket for _path, data in reports for bucket in data["shard"].get("buckets", [])
    ]
    if sorted(owned) != sorted(plan_buckets):
        raise ShardError("shard buckets do not partition the complete plan")
    discovered = _count(first.get("discovered"), "discovered")
    failures = []
    total = 0
    ordered = sorted(reports, key=lambda item: item[1]["shard"]["index"])
    for path, data in ordered:
        label = f"shard {data['shard']['index']}/{shards}"
        accounted, reasons = _shard_accounting(kind, data, label)
        if data["shard"].get("assigned") != accounted:
            raise ShardError(f"{label}: assigned count differs from worker accounting")
        total += accounted
        failures.extend(f"{label}: {reason}" for reason in reasons)
    if kind == "checkout":
        controllers = [
            data["shard"]["index"] for _path, data in ordered if data.get("controller")
        ]
        expected_controller = first["shard"].get("plan_controller_assigned")
        if expected_controller and controllers != [1]:
            raise ShardError("controller phase must run in shard 1 only")
        if not expected_controller and controllers:
            raise ShardError("a shard ran an unplanned controller phase")
    if total != discovered:
        raise ShardError(
            f"shards accounted for {total} tests, but {discovered} were discovered"
        )
    workers = []
    for _path, data in ordered:
        for worker in data["workers"]:
            workers.append({**worker, "shard": data["shard"]["index"]})
    merged = {
        key: value
        for key, value in first.items()
        if key not in ("shard", "workers", "controller", "elapsed_seconds")
    }
    merged.update(
        {
            "shards": [
                {
                    "index": data["shard"]["index"],
                    "buckets": data["shard"]["buckets"],
                    "assigned": data["shard"]["assigned"],
                    "elapsed_seconds": data.get("elapsed_seconds"),
                }
                for _path, data in ordered
            ],
            "plan_sha256": first["shard"]["plan_sha256"],
            "complete": all(data.get("complete") is True for _path, data in ordered),
            "elapsed_seconds": max(
                (data.get("elapsed_seconds") or 0) for _path, data in ordered
            ),
            "workers": workers,
        }
    )
    if kind == "checkout":
        merged["controller"] = ordered[0][1].get("controller")
        merged["effective_worker_count"] = len(workers)
        merged["execution_complete"] = all(
            data.get("execution_complete") is True for _path, data in ordered
        )
        merged.pop("coverage_ok", None)
        merged.pop("coverage_error", None)
    return merged, failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shards", type=int, required=True)
    parser.add_argument("--report", type=Path, help="Write the merged receipt")
    parser.add_argument("reports", type=Path, nargs="+")
    args = parser.parse_args()
    try:
        merged, failures = verify(args.reports, args.shards)
    except ShardError as error:
        print(f"Shard verification failed: {error}", file=sys.stderr)
        return 1
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    for shard in merged["shards"]:
        print(
            f"shard {shard['index']}/{args.shards}: {shard['assigned']} tests, "
            f"{shard['elapsed_seconds']}s"
        )
    for failure in failures:
        print(failure, file=sys.stderr)
    print(
        f"{merged['discovered']} discovered tests accounted for across "
        f"{args.shards} shards; {'all passed' if not failures else 'FAILED'}"
    )
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
