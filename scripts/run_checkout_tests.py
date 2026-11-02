"""Run the complete discovered checkout suite in bounded isolated processes.

Optional branch coverage is collected per worker and combined. Workers execute
disjoint test IDs, preserve class/module fixtures, and all finish on failure.
This is the checkout lane: coverage traces the editable source packages.
Installed-wheel suites run directly with unittest and their own import guards.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from collections import Counter
from pathlib import Path


def test_ids(suite: unittest.TestSuite) -> list[str]:
    identities = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            identities.extend(test_ids(item))
        else:
            identities.append(item.id())
    return identities


def test_cases(suite: unittest.TestSuite) -> list[unittest.TestCase]:
    cases = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            cases.extend(test_cases(item))
        else:
            cases.append(item)
    return cases


def count_fixture_skips(remaining, scopes, skipped_events) -> int:
    """Consume each matching identity once; unknown scopes remain unaccounted."""
    fixture_skipped = 0
    for skipped, _reason in skipped_events:
        if isinstance(skipped, unittest.TestCase):
            continue
        match = re.fullmatch(r"setUp(Class|Module) \((.+)\)", skipped.id())
        if not match:
            continue
        scope_index = 0 if match.group(1) == "Class" else 1
        for identity in list(remaining):
            scope = scopes.get(identity)
            if scope is not None and scope[scope_index] == match.group(2):
                fixture_skipped += remaining.pop(identity)
    return fixture_skipped


def partition(
    identities: list[str], workers: int, costs: dict[str, float] | None = None
) -> list[list[str]]:
    """Deterministic, order-independent class assignment to nonempty workers."""
    if workers < 1 or workers > 16:
        raise ValueError("workers must be between 1 and 16")
    if not identities or len(set(identities)) != len(identities):
        raise ValueError("discovery must produce distinct nonempty test IDs")
    if costs is not None and not isinstance(costs, dict):
        raise ValueError("class costs must be a JSON object of positive seconds")
    measured = costs is not None
    costs = costs or {}
    if any(
        not isinstance(name, str)
        or not name
        or type(value) not in (int, float)
        or not 0 < value <= sys.float_info.max
        for name, value in costs.items()
    ):
        raise ValueError("class costs must be finite positive seconds")
    classes: dict[str, list[str]] = {}
    for identity in sorted(identities):
        classes.setdefault(identity.rpartition(".")[0] or identity, []).append(identity)
    groups: list[list[str]] = [[] for _ in range(min(workers, len(classes)))]
    loads = [0.0] * len(groups)

    def cost(name: str, members: list[str]) -> float:
        return costs.get(name, len(members) * (0.02 if measured else 1.0))

    # Keep class fixtures and any class-local state together. Splitting methods
    # repeats expensive setup and amplifies Windows filesystem contention.
    for _name, members in sorted(
        classes.items(), key=lambda item: (-cost(*item), item[0])
    ):
        index = min(range(len(groups)), key=lambda index: (loads[index], index))
        groups[index].extend(members)
        loads[index] += cost(_name, members)
    return [sorted(group) for group in groups]


def read_worker_report(path: Path) -> tuple[dict[str, object], str | None]:
    """Read only the worker's timing fields; damaged evidence is not success."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        return {
            "executed": None,
            "fixture_skipped": None,
            "inventory_complete": False,
            "class_seconds": {},
        }, ("Worker timing report unreadable: " + type(error).__name__)
    if (
        not isinstance(data, dict)
        or set(data)
        != {"executed", "fixture_skipped", "inventory_complete", "class_seconds"}
        or type(data["executed"]) is not int
        or data["executed"] < 0
        or type(data["fixture_skipped"]) is not int
        or data["fixture_skipped"] < 0
        or type(data["inventory_complete"]) is not bool
        or not isinstance(data["class_seconds"], dict)
        or any(
            not isinstance(name, str)
            or type(seconds) not in (int, float)
            or not 0 <= seconds <= sys.float_info.max
            for name, seconds in data["class_seconds"].items()
        )
    ):
        return {
            "executed": None,
            "fixture_skipped": None,
            "inventory_complete": False,
            "class_seconds": {},
        }, ("Worker timing report has invalid fields")
    return data, None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--start-directory", type=Path, default=Path("tests"))
    parser.add_argument(
        "--costs", type=Path, help="Measured class seconds; scheduling only"
    )
    parser.add_argument(
        "--report", type=Path, help="Write inventory and worker timing evidence"
    )
    parser.add_argument(
        "--coverage",
        action="store_true",
        help="Combine isolated worker branch coverage",
    )
    parser.add_argument("--worker-report", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--worker-manifest", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    directory = args.start_directory.resolve()
    sys.path.insert(0, str(Path.cwd()))
    sys.path.insert(0, str(directory))
    loader = unittest.TestLoader()
    if args.worker_manifest is not None:
        identities = json.loads(args.worker_manifest.read_text(encoding="utf-8"))
        suite = loader.loadTestsFromNames(identities)
        cases = test_cases(suite)
        loaded = [
            (
                case.id(),
                f"{type(case).__module__}.{type(case).__qualname__}",
                type(case).__module__,
            )
            for case in cases
        ]
        if loader.errors or sorted(item[0] for item in loaded) != sorted(identities):
            print(
                "Worker failed to load its exact assigned test inventory",
                file=sys.stderr,
            )
            return 1

        scopes = {
            identity: (class_name, module) for identity, class_name, module in loaded
        }

        class TimedResult(unittest.TextTestResult):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.class_seconds: dict[str, float] = {}
                self.started: float | None = None
                self.started_ids: list[str] = []
                self.stopped_ids: list[str] = []

            def startTest(self, test):
                self.started_ids.append(test.id())
                self.started = time.monotonic()
                super().startTest(test)

            def stopTest(self, test):
                self.stopped_ids.append(test.id())
                if self.started is not None:
                    name = test.id().rpartition(".")[0]
                    self.class_seconds[name] = (
                        self.class_seconds.get(name, 0)
                        + time.monotonic()
                        - self.started
                    )
                self.started = None
                super().stopTest(test)

        result = unittest.TextTestRunner(verbosity=2, resultclass=TimedResult).run(
            suite
        )
        expected = Counter(identities)
        observed = Counter(result.started_ids)
        remaining = expected - observed
        fixture_skipped = count_fixture_skips(remaining, scopes, result.skipped)
        inventory_complete = (
            not (observed - expected)
            and not remaining
            and result.testsRun == len(result.started_ids)
            and Counter(result.stopped_ids) == observed
        )
        if not inventory_complete:
            print(
                "Worker did not account for its exact assigned test inventory",
                file=sys.stderr,
            )
        if args.worker_report is not None:
            args.worker_report.write_text(
                json.dumps(
                    {
                        "class_seconds": result.class_seconds,
                        "executed": result.testsRun,
                        "fixture_skipped": fixture_skipped,
                        "inventory_complete": inventory_complete,
                    }
                ),
                encoding="utf-8",
            )
        return 0 if result.wasSuccessful() and inventory_complete else 1
    suite = loader.discover(str(directory))
    if loader.errors:
        for error in loader.errors:
            print(error, file=sys.stderr)
        return 1
    try:
        costs = (
            json.loads(args.costs.read_text(encoding="utf-8")) if args.costs else None
        )
        if args.costs and not isinstance(costs, dict):
            raise ValueError("class costs must be a JSON object of positive seconds")
        groups = partition(test_ids(suite), args.workers, costs)
    except (ValueError, OSError) as exc:
        print(exc, file=sys.stderr)
        return 1
    print(
        f"Discovered {suite.countTestCases()} tests; running {len(groups)} disjoint workers",
        flush=True,
    )
    # Use manifests rather than argv test lists: Windows has a 32 KiB command
    # limit, which a full checkout inventory can exceed.
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="review-sensei-tests-") as scratch:
        commands = []
        for index, identities in enumerate(groups):
            manifest = Path(scratch) / f"worker-{index}.json"
            manifest.write_text(json.dumps(identities), encoding="utf-8")
            prefix = [sys.executable]
            if args.coverage:
                prefix += [
                    "-m",
                    "coverage",
                    "run",
                    "--branch",
                    "--source=review_sensei,review_sensei_github",
                    "--data-file",
                    str(Path(scratch) / f".coverage.worker{index}"),
                ]
            commands.append(
                prefix
                + [
                    str(Path(__file__).resolve()),
                    "--start-directory",
                    str(directory),
                    "--worker-manifest",
                    str(manifest),
                    "--worker-report",
                    str(Path(scratch) / f"timing-{index}.json"),
                ]
            )

        elapsed = [0.0] * len(groups)

        def run_worker(index: int) -> int:
            start = time.monotonic()
            with (
                (Path(scratch) / f"worker-{index}.stdout").open("wb") as stdout,
                (Path(scratch) / f"worker-{index}.stderr").open("wb") as stderr,
            ):
                code = subprocess.call(commands[index], stdout=stdout, stderr=stderr)
            elapsed[index] = time.monotonic() - start
            return code

        # Await all workers before replaying separate logs in stable worker
        # order. Keep manifests/logs alive until execution and reporting finish.
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(groups)) as pool:
            results = list(pool.map(run_worker, range(len(groups))))
        for index, code in enumerate(results):
            label = (
                f"[checkout worker {index + 1}/{len(groups)}: "
                f"{len(groups[index])} tests, exit {code}]"
            )
            for suffix, stream in (("stdout", sys.stdout), ("stderr", sys.stderr)):
                print(label, file=stream, flush=True)
                with (Path(scratch) / f"worker-{index}.{suffix}").open(
                    encoding="utf-8", errors="replace"
                ) as output:
                    shutil.copyfileobj(output, stream)
                stream.flush()
        execution_complete = True
        workers = []
        for index, code in enumerate(results):
            timing = Path(scratch) / f"timing-{index}.json"
            data, timing_error = read_worker_report(timing)
            execution_complete &= (
                timing_error is None
                and data["inventory_complete"] is True
                and data["executed"] + data["fixture_skipped"] == len(groups[index])
            )
            if timing_error:
                print(f"Worker {index + 1}: {timing_error}", file=sys.stderr)
            workers.append(
                {
                    "worker": index,
                    "assigned": len(groups[index]),
                    "exit": code,
                    "elapsed_seconds": round(elapsed[index], 3),
                    "timing_error": timing_error,
                    **data,
                }
            )
        coverage_error = None
        coverage_ok = None
        if args.coverage:
            from coverage import Coverage
            from coverage.exceptions import CoverageException

            paths = [
                Path(scratch) / f".coverage.worker{index}"
                for index in range(len(groups))
            ]
            if not execution_complete or not all(path.is_file() for path in paths):
                coverage_error = "Missing worker coverage or incomplete execution"
            else:
                try:
                    coverage = Coverage()
                    coverage.combine(
                        data_paths=[str(path) for path in paths], strict=True
                    )
                    coverage.save()
                except (CoverageException, OSError) as error:
                    coverage_error = (
                        "Worker coverage combination failed: " + type(error).__name__
                    )
            if coverage_error:
                print(coverage_error, file=sys.stderr)
            coverage_ok = coverage_error is None
        complete = execution_complete and coverage_ok is not False
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(
                json.dumps(
                    {
                        "inventory_sha256": hashlib.sha256(
                            "\n".join(sorted(test_ids(suite))).encode()
                        ).hexdigest(),
                        "discovered": suite.countTestCases(),
                        "complete": complete,
                        "execution_complete": execution_complete,
                        "coverage_ok": coverage_ok,
                        "coverage_error": coverage_error,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        "workers": workers,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
    return 0 if complete and all(code == 0 for code in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
