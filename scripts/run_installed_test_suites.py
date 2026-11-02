"""Run installed-wheel test modules in isolated processes without flattening suites.

The unpacked sdist's dist-safe lane may return custom ``load_tests`` suites.
Workers rediscover the complete suite and run whole top-level module suites,
preserving their wrappers. Ambiguous discovery or fixture ownership fails before
workers start instead of silently omitting tests or suite behavior.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import fnmatch
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from collections import Counter
from pathlib import Path


def describe(item):
    """Describe suite nesting and each test's identity and fixture scopes."""
    if isinstance(item, unittest.TestSuite):
        return {
            "suite": f"{type(item).__module__}.{type(item).__qualname__}",
            "children": [describe(child) for child in item],
        }
    if not isinstance(item, unittest.TestCase):
        raise ValueError("unsupported non-TestCase leaf in discovered suite")
    identity = item.id()
    if not isinstance(identity, str) or not identity:
        raise ValueError("discovery returned an invalid test identity")
    cls = type(item)
    return {
        "id": identity,
        "class": f"{cls.__module__}.{cls.__qualname__}",
        "module": cls.__module__,
    }


def leaves(description):
    if "suite" in description:
        return [leaf for child in description["children"] for leaf in leaves(child)]
    return [description]


def discover(directory: Path, pattern: str):
    """Discover the original suite and bind each top-level child to its file."""
    if not directory.is_dir():
        raise ValueError("test directory does not exist")
    if any(
        path.is_dir() and (path / "__init__.py").is_file()
        for path in directory.iterdir()
    ):
        raise ValueError("nested test packages require wrapper-aware sharding")
    files = sorted(
        path
        for path in directory.iterdir()
        if path.is_file()
        and unittest.loader.VALID_MODULE_NAME.match(path.name)
        and fnmatch.fnmatch(path.name, pattern)
    )
    loader = unittest.TestLoader()
    suite = loader.discover(
        str(directory), pattern=pattern, top_level_dir=str(directory)
    )
    if loader.errors:
        raise ValueError("test discovery reported an import error")
    children = list(suite)
    if len(files) != len(children) or not files:
        raise ValueError("discovered modules cannot be mapped exactly to source files")
    descriptions = [describe(child) for child in children]
    owners = {"class": {}, "module": {}}
    for index, (path, description) in enumerate(zip(files, descriptions)):
        module = sys.modules.get(path.stem)
        if module is None or Path(module.__file__).resolve() != path.resolve():
            raise ValueError("discovery imported a module from an unexpected path")
        last_scope = {"class": None, "module": None}
        seen_scope = {"class": set(), "module": set()}
        for leaf in leaves(description):
            for scope in owners:
                previous = owners[scope].setdefault(leaf[scope], index)
                if previous != index:
                    raise ValueError(
                        "a fixture scope spans multiple discovered modules"
                    )
                if leaf[scope] != last_scope[scope]:
                    if leaf[scope] in seen_scope[scope]:
                        raise ValueError("a fixture scope occurs in multiple blocks")
                    seen_scope[scope].add(leaf[scope])
                    last_scope[scope] = leaf[scope]
    return files, descriptions, children


def partition_modules(descriptions, workers):
    if workers < 1 or workers > 16:
        raise ValueError("workers must be between 1 and 16")
    if not descriptions:
        raise ValueError("discovery must contain test modules")
    groups = [[] for _ in range(min(workers, len(descriptions)))]
    loads = [0] * len(groups)
    for index in sorted(
        range(len(descriptions)), key=lambda i: (-len(leaves(descriptions[i])), i)
    ):
        worker = min(range(len(groups)), key=lambda i: (loads[i], i))
        groups[worker].append(index)
        loads[worker] += len(leaves(descriptions[index]))
    return [sorted(group) for group in groups]


def identity(leaf):
    return (leaf["id"], leaf["class"], leaf["module"])


def worker(directory: Path, manifest_path: Path, report_path: Path) -> int:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files, descriptions, children = discover(directory, manifest["pattern"])
    if [path.name for path in files] != manifest["files"] or descriptions != manifest[
        "descriptions"
    ]:
        raise ValueError("worker discovery differs from the original suite")
    indices = manifest["indices"]
    if not indices or any(
        type(index) is not int or not 0 <= index < len(files) for index in indices
    ):
        raise ValueError("worker manifest has invalid module indices")
    if indices != sorted(set(indices)):
        raise ValueError("worker manifest has duplicate or unordered module indices")

    # Run the actual suites produced by discovery, without invoking load_tests
    # again in the worker or flattening custom wrappers into test names.
    selected = []
    expected = []
    owner = {}
    for index in indices:
        selected.append(children[index])
        for leaf in leaves(descriptions[index]):
            key = identity(leaf)
            expected.append(key)
            owner[key] = files[index].name

    class TimedResult(unittest.TextTestResult):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.started = []
            self.started_at = None
            self.module_seconds = {}

        def startTest(self, test):
            self.started.append(identity(describe(test)))
            self.started_at = time.monotonic()
            super().startTest(test)

        def stopTest(self, test):
            key = identity(describe(test))
            if self.started_at is not None and key in owner:
                name = owner[key]
                self.module_seconds[name] = (
                    self.module_seconds.get(name, 0.0)
                    + time.monotonic()
                    - self.started_at
                )
            self.started_at = None
            super().stopTest(test)

    result = unittest.TextTestRunner(verbosity=2, resultclass=TimedResult).run(
        unittest.TestSuite(selected)
    )
    expected_counts = Counter(expected)
    started_counts = Counter(result.started)
    unexpected = started_counts - expected_counts
    remaining = expected_counts - started_counts
    fixture_skipped = 0
    for item, _reason in result.skipped:
        match = re.fullmatch(r"setUp(Class|Module) \((.+)\)", item.id())
        if not match:
            continue
        scope = 1 if match.group(1) == "Class" else 2
        for key in list(remaining):
            if key[scope] == match.group(2):
                fixture_skipped += remaining.pop(key)
    execution_complete = not unexpected and not remaining
    report_path.write_text(
        json.dumps(
            {
                "assigned": len(expected),
                "started": len(result.started),
                "fixture_skipped": fixture_skipped,
                "skip_events": len(result.skipped),
                "execution_complete": execution_complete,
                "module_seconds": result.module_seconds,
            }
        ),
        encoding="utf-8",
    )
    if not execution_complete:
        print("Worker did not account for its exact test inventory", file=sys.stderr)
    return 0 if result.wasSuccessful() and execution_complete else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-directory", type=Path, required=True)
    parser.add_argument("--pattern", default="test*.py")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--worker-manifest", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--worker-report", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not sys.flags.isolated:
        print("installed suite runner requires Python -I", file=sys.stderr)
        return 1
    directory = args.start_directory.resolve()
    try:
        if args.worker_manifest:
            if not args.worker_report:
                raise ValueError("worker report is required")
            return worker(directory, args.worker_manifest, args.worker_report)
        files, descriptions, _children = discover(directory, args.pattern)
        groups = partition_modules(descriptions, args.workers)
    except (ValueError, OSError, ImportError, json.JSONDecodeError) as error:
        print(f"Installed suite discovery failed: {error}", file=sys.stderr)
        return 1

    expected = [leaf for description in descriptions for leaf in leaves(description)]
    print(
        f"Discovered {len(expected)} tests in {len(files)} modules; running {len(groups)} isolated workers",
        flush=True,
    )
    started = time.monotonic()
    with tempfile.TemporaryDirectory(
        prefix="review-sensei-installed-tests-"
    ) as scratch:
        root = Path(scratch)
        elapsed = [0.0] * len(groups)

        def run_worker(index):
            manifest = root / f"worker-{index}.json"
            report = root / f"worker-{index}-report.json"
            manifest.write_text(
                json.dumps(
                    {
                        "pattern": args.pattern,
                        "files": [path.name for path in files],
                        "descriptions": descriptions,
                        "indices": groups[index],
                    }
                ),
                encoding="utf-8",
            )
            worker_directory = root / f"worker-{index}"
            worker_directory.mkdir()
            command = [
                sys.executable,
                "-I",
                str(Path(__file__).resolve()),
                "--start-directory",
                str(directory),
                "--worker-manifest",
                str(manifest),
                "--worker-report",
                str(report),
            ]
            start = time.monotonic()
            with (
                (root / f"worker-{index}.stdout").open("wb") as stdout,
                (root / f"worker-{index}.stderr").open("wb") as stderr,
            ):
                try:
                    code = subprocess.call(
                        command, cwd=worker_directory, stdout=stdout, stderr=stderr
                    )
                except OSError as error:
                    stderr.write(f"Could not start worker: {error}\n".encode())
                    code = 1
            elapsed[index] = time.monotonic() - start
            return code

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(groups)) as pool:
            results = list(pool.map(run_worker, range(len(groups))))
        reports = []
        for index, code in enumerate(results):
            for suffix, stream in (("stdout", sys.stdout), ("stderr", sys.stderr)):
                print(
                    f"[installed worker {index + 1}/{len(groups)}: exit {code}]",
                    file=stream,
                    flush=True,
                )
                with (root / f"worker-{index}.{suffix}").open(
                    encoding="utf-8", errors="replace"
                ) as output:
                    shutil.copyfileobj(output, stream)
                stream.flush()
            try:
                data = json.loads(
                    (root / f"worker-{index}-report.json").read_text(encoding="utf-8")
                )
                if set(data) != {
                    "assigned",
                    "started",
                    "fixture_skipped",
                    "skip_events",
                    "execution_complete",
                    "module_seconds",
                }:
                    raise ValueError("invalid worker report")
                if data["assigned"] != sum(
                    len(leaves(descriptions[i])) for i in groups[index]
                ):
                    raise ValueError("worker assignment count differs")
                for key in ("assigned", "started", "fixture_skipped", "skip_events"):
                    if type(data[key]) is not int or data[key] < 0:
                        raise ValueError("worker report contains an invalid count")
                if data["started"] + data["fixture_skipped"] != data["assigned"]:
                    raise ValueError(
                        "worker report does not account for all assigned tests"
                    )
                module_seconds = data["module_seconds"]
                if not isinstance(module_seconds, dict) or not set(module_seconds) <= {
                    files[i].name for i in groups[index]
                }:
                    raise ValueError("worker report contains unexpected module timings")
                if any(
                    type(value) not in (int, float)
                    or not math.isfinite(value)
                    or value < 0
                    for value in module_seconds.values()
                ):
                    raise ValueError("worker report contains invalid timings")
                if data["execution_complete"] is not True:
                    raise ValueError("worker inventory was not fully accounted for")
            except (OSError, ValueError, json.JSONDecodeError, TypeError) as error:
                print(f"Worker {index + 1}: {error}", file=sys.stderr)
                data = {
                    "assigned": None,
                    "started": None,
                    "fixture_skipped": None,
                    "skip_events": None,
                    "execution_complete": False,
                    "module_seconds": {},
                }
            reports.append(
                {
                    "worker": index,
                    "exit": code,
                    "elapsed_seconds": round(elapsed[index], 3),
                    **data,
                }
            )
        complete = all(code == 0 for code in results) and all(
            item["execution_complete"] for item in reports
        )
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(
                json.dumps(
                    {
                        "inventory_sha256": hashlib.sha256(
                            "\n".join(sorted(leaf["id"] for leaf in expected)).encode()
                        ).hexdigest(),
                        "discovered": len(expected),
                        "modules": len(files),
                        "complete": complete,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        "workers": reports,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
