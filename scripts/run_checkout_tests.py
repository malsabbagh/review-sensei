"""Run the complete discovered checkout suite in bounded isolated processes.

Coverage remains the serial quality lane's responsibility. Workers execute
disjoint test IDs, preserve class/module fixtures, and all finish on failure.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import shutil
import subprocess
import sys
import tempfile
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


def partition(identities: list[str], workers: int) -> list[list[str]]:
    if workers < 1 or workers > 16:
        raise ValueError("workers must be between 1 and 16")
    if not identities or len(set(identities)) != len(identities):
        raise ValueError("discovery must produce distinct nonempty test IDs")
    classes: dict[str, list[str]] = {}
    for identity in sorted(identities):
        classes.setdefault(identity.rpartition(".")[0] or identity, []).append(identity)
    groups: list[list[str]] = [[] for _ in range(min(workers, len(classes)))]
    # Keep class fixtures and any class-local state together. Splitting methods
    # repeats expensive setup and amplifies Windows filesystem contention.
    for _name, members in sorted(
        classes.items(), key=lambda item: (-len(item[1]), item[0])
    ):
        index = min(range(len(groups)), key=lambda index: (len(groups[index]), index))
        groups[index].extend(members)
    return [sorted(group) for group in groups]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--start-directory", type=Path, default=Path("tests"))
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
        return (
            0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
        )
    suite = loader.discover(str(directory))
    if loader.errors:
        for error in loader.errors:
            print(error, file=sys.stderr)
        return 1
    try:
        groups = partition(test_ids(suite), args.workers)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(
        f"Discovered {suite.countTestCases()} tests; running {len(groups)} disjoint workers",
        flush=True,
    )
    # Use manifests rather than argv test lists: Windows has a 32 KiB command
    # limit, which a full checkout inventory can exceed.
    with tempfile.TemporaryDirectory(prefix="review-sensei-tests-") as scratch:
        commands = []
        for index, identities in enumerate(groups):
            manifest = Path(scratch) / f"worker-{index}.json"
            manifest.write_text(json.dumps(identities), encoding="utf-8")
            commands.append(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--start-directory",
                    str(directory),
                    "--worker-manifest",
                    str(manifest),
                ]
            )

        def run_worker(index: int) -> int:
            with (
                (Path(scratch) / f"worker-{index}.stdout").open("wb") as stdout,
                (Path(scratch) / f"worker-{index}.stderr").open("wb") as stderr,
            ):
                return subprocess.call(commands[index], stdout=stdout, stderr=stderr)

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
    return 0 if all(code == 0 for code in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
