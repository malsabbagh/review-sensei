"""Run the complete discovered checkout suite in bounded isolated processes.

Optional branch coverage is collected per worker and combined. Workers execute
disjoint test IDs, preserve class/module fixtures, and all finish on failure.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


def test_ids(suite: unittest.TestSuite) -> list[str]:
    identities = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            identities.extend(test_ids(item))
        else:
            identities.append(item.id())
    return identities


def partition(
    identities: list[str], workers: int, costs: dict[str, float] | None = None
) -> list[list[str]]:
    if workers < 1 or workers > 16:
        raise ValueError("workers must be between 1 and 16")
    if not identities or len(set(identities)) != len(identities):
        raise ValueError("discovery must produce distinct nonempty test IDs")
    measured = costs is not None
    costs = costs or {}
    if any(not math.isfinite(value) or value <= 0 for value in costs.values()):
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
        if loader.errors or sorted(test_ids(suite)) != sorted(identities):
            print(
                "Worker failed to load its exact assigned test inventory",
                file=sys.stderr,
            )
            return 1

        class TimedResult(unittest.TextTestResult):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.class_seconds: dict[str, float] = {}

            def startTest(self, test):
                self.started = time.monotonic()
                super().startTest(test)

            def stopTest(self, test):
                name = test.id().rpartition(".")[0]
                self.class_seconds[name] = (
                    self.class_seconds.get(name, 0) + time.monotonic() - self.started
                )
                super().stopTest(test)

        result = unittest.TextTestRunner(verbosity=2, resultclass=TimedResult).run(
            suite
        )
        if args.worker_report is not None:
            args.worker_report.write_text(
                json.dumps(
                    {"class_seconds": result.class_seconds, "executed": result.testsRun}
                ),
                encoding="utf-8",
            )
        return 0 if result.wasSuccessful() else 1
    suite = loader.discover(str(directory))
    if loader.errors:
        for error in loader.errors:
            print(error, file=sys.stderr)
        return 1
    try:
        costs = (
            json.loads(args.costs.read_text(encoding="utf-8")) if args.costs else None
        )
        groups = partition(test_ids(suite), args.workers, costs)
    except ValueError as exc:
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
        complete = True
        workers = []
        for index, code in enumerate(results):
            timing = Path(scratch) / f"timing-{index}.json"
            data = json.loads(timing.read_text()) if timing.exists() else {}
            complete &= data.get("executed") == len(groups[index])
            workers.append(
                {
                    "worker": index,
                    "assigned": len(groups[index]),
                    "exit": code,
                    "elapsed_seconds": round(elapsed[index], 3),
                    **data,
                }
            )
        coverage_error = None
        if args.coverage:
            from coverage import Coverage
            from coverage.exceptions import CoverageException

            paths = [
                Path(scratch) / f".coverage.worker{index}"
                for index in range(len(groups))
            ]
            if not complete or not all(path.is_file() for path in paths):
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
                complete = False
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
