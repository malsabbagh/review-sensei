"""Run installed tests with exact accounting and isolated import semantics.

Hook-bearing suites execute their original discovered root once in the -I
controller. Hook-free suites may use isolated workers and measured scheduling.
Fixture skips are recorded from actual unittest dispatch, never inferred from
all unstarted tests sharing a fixture name.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import fnmatch
import hashlib
import json
import math
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


class FixtureDispatchTracker:
    """Observe default unittest fixture dispatch without changing suite objects.

    Fixture holders do not identify the tests they blocked. The stdlib class
    setup handler is called for every selected leaf, including leaves blocked
    by module setup. Observe its return and the actual setup episode instead.
    """

    def __init__(self, cases, identity):
        self.identities = {id(case): identity(case) for case in cases}
        self.skipped = Counter()
        self.episodes = {}
        self.entries = {}
        self.module_dispatch = None
        self.codes = {
            unittest.TestSuite._handleModuleFixture.__code__: "Module",
            unittest.TestSuite._handleClassSetUp.__code__: "Class",
        }

    def __enter__(self):
        self.previous = sys.getprofile()
        self.active = False
        return self

    def __exit__(self, *exc):
        if self.active and sys.getprofile() == self.observe:
            sys.setprofile(self.previous)
        self.active = False

    def note_skip(self):
        frame = sys._getframe(1)
        actual_skip = False
        while frame is not None:
            if (
                frame.f_code
                is unittest.TestSuite._addClassOrModuleLevelException.__code__
            ):
                actual_skip = isinstance(
                    frame.f_locals.get("exception"), unittest.SkipTest
                )
                name = frame.f_locals.get("errorName", "")
            phase = self.codes.get(frame.f_code)
            if phase is not None:
                caller = frame.f_back
                if (
                    actual_skip
                    and name.startswith(f"setUp{phase} (")
                    and frame.f_locals.get("result") is self.result
                    and caller is not None
                    and caller.f_code is unittest.TestSuite.run.__code__
                    and caller.f_locals.get("test") is frame.f_locals.get("test")
                ):
                    self.entries[id(frame)] = True
                    if not self.active:
                        self.previous = sys.getprofile()
                        self.active = True
                        sys.setprofile(self.observe)
                return
            frame = frame.f_back

    def observe(self, frame, event, arg):
        if self.previous is not None:
            self.previous(frame, event, arg)
        phase = self.codes.get(frame.f_code)
        if phase is None or event not in {"call", "return"}:
            return
        result = frame.f_locals.get("result")
        if result is not getattr(self, "result", None) or result is None:
            return
        caller = frame.f_back
        if (
            caller is None
            or caller.f_code is not unittest.TestSuite.run.__code__
            or caller.f_locals.get("test") is not frame.f_locals.get("test")
        ):
            return
        if event == "call":
            self.entries[id(frame)] = False
            return
        skipped = self.entries.pop(id(frame), False)
        state = self.episodes.setdefault(id(result), [result, None, None])
        if phase == "Module":
            module = frame.f_locals["currentModule"]
            self.module_dispatch = (result, frame.f_locals["test"], module)
            if module != frame.f_locals["previousModule"]:
                state[1] = module if skipped else None
            return
        cls = frame.f_locals["currentClass"]
        previous = frame.f_locals["previousClass"]
        if cls != previous:
            state[2] = cls if skipped else None
        if (
            getattr(result, "_moduleSetUpFailed", False)
            and self.module_dispatch is not None
            and self.module_dispatch[0] is result
            and self.module_dispatch[1] is frame.f_locals["test"]
            and state[1] == self.module_dispatch[2]
        ) or (getattr(cls, "_classSetupFailed", False) and state[2] is cls):
            key = self.identities.get(id(frame.f_locals["test"]))
            if key is not None:
                self.skipped[key] += 1
        if state[1] is None and state[2] is None:
            self.active = False
            sys.setprofile(self.previous)


class HookAwareLoader(unittest.TestLoader):
    hook_seen = False

    def loadTestsFromModule(self, module, *args, **kwargs):
        if getattr(module, "load_tests", None) is not None:
            self.hook_seen = True
        return super().loadTestsFromModule(module, *args, **kwargs)


def describe(item, cases=None):
    """Describe suite nesting and each test's identity and fixture scopes."""
    if isinstance(item, unittest.TestSuite):
        return {
            "suite": f"{type(item).__module__}.{type(item).__qualname__}",
            "children": [describe(child, cases) for child in item],
        }
    if not isinstance(item, unittest.TestCase):
        raise ValueError("unsupported non-TestCase leaf in discovered suite")
    if cases is not None:
        cases.append(item)
    identity = item.id()
    if not isinstance(identity, str) or not identity:
        raise ValueError("discovery returned an invalid test identity")
    # unittest resolves class and module fixtures through test.__class__,
    # which a proxy TestCase can intentionally expose differently from type().
    cls = item.__class__
    return {
        "id": identity,
        "class": f"{cls.__module__}.{cls.__qualname__}",
        "module": cls.__module__,
    }


def leaves(description):
    if "suite" in description:
        return [leaf for child in description["children"] for leaf in leaves(child)]
    return [description]


def discover(directory: Path, pattern: str, cases=None, original=None):
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
    loader = HookAwareLoader()
    suite = loader.discover(
        str(directory), pattern=pattern, top_level_dir=str(directory)
    )
    if loader.errors:
        raise ValueError("test discovery reported an import error")
    if original is not None:
        original.extend([suite, loader.hook_seen])
    children = list(suite)
    if len(files) != len(children) or not files:
        raise ValueError("discovered modules cannot be mapped exactly to source files")
    descriptions = [describe(child, cases) for child in children]
    if loader.hook_seen:
        return files, descriptions, children
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


def plain_suite_tree(item):
    if isinstance(item, unittest.TestSuite):
        if (
            type(item) is not unittest.TestSuite
            or set(vars(item)) != {"_tests", "_removed_tests"}
            or type(item._tests) is not list
            or item._removed_tests != 0
        ):
            return False
        return all(plain_suite_tree(child) for child in item)
    return True


def build_units(files, descriptions, children, split_files):
    """Split opted-in plain suites only at complete module-fixture boundaries."""
    names = [path.name for path in files]
    if len(split_files) != len(set(split_files)) or not set(split_files) <= set(names):
        raise ValueError("requested split modules must be distinct discovered files")
    units = []
    for index, (path, description, suite) in enumerate(
        zip(files, descriptions, children)
    ):
        unit = {"key": path.name, "file": index, "children": None}
        if (
            path.name not in split_files
            or type(suite) is not unittest.TestSuite
            or not plain_suite_tree(suite)
        ):
            units.append(unit)
            continue
        parts = description["children"]
        part_leaves = [leaves(part) for part in parts]
        # Empty children may have custom run behavior; keep their parent whole.
        if not parts or any(not group for group in part_leaves):
            units.append(unit)
            continue
        scopes = [{leaf["module"] for leaf in group} for group in part_leaves]
        last = {
            scope: position for position, group in enumerate(scopes) for scope in group
        }
        start, end = 0, -1
        for position, group in enumerate(scopes):
            end = max([end, *(last[scope] for scope in group)])
            if position != end:
                continue
            members = list(range(start, position + 1))
            modules = sorted(set().union(*(scopes[i] for i in members)))
            units.append(
                {
                    "key": path.name + "::" + ",".join(modules),
                    "file": index,
                    "children": members,
                }
            )
            start = position + 1
        if start != len(parts):
            raise ValueError("could not preserve module fixture boundaries")
    if len({unit["key"] for unit in units}) != len(units):
        raise ValueError("discovery produced ambiguous scheduling units")
    return units


def unit_leaves(unit, descriptions):
    description = descriptions[unit["file"]]
    if unit["children"] is None:
        return leaves(description)
    return [
        leaf
        for index in unit["children"]
        for leaf in leaves(description["children"][index])
    ]


def partition_units(units, descriptions, workers, costs=None):
    if workers < 1 or workers > 16:
        raise ValueError("workers must be between 1 and 16")
    if not units:
        raise ValueError("discovery must contain test modules")
    if costs is not None and (
        not isinstance(costs, dict)
        or any(
            not isinstance(name, str)
            or not name
            or type(value) not in (int, float)
            or not math.isfinite(value)
            or value < 0
            for name, value in costs.items()
        )
    ):
        raise ValueError(
            "unit costs must be a JSON object of finite nonnegative weights"
        )
    costs = costs or {}
    groups = [[] for _ in range(min(workers, len(units)))]
    loads = [0.0] * len(groups)

    def cost(index):
        # Missing measurements still schedule every test with a small weight.
        return costs.get(
            units[index]["key"], len(unit_leaves(units[index], descriptions)) * 0.02
        )

    for index in sorted(range(len(units)), key=lambda i: (-cost(i), units[i]["key"])):
        worker = min(range(len(groups)), key=lambda i: (loads[i], i))
        groups[worker].append(index)
        loads[worker] += cost(index)
    return [sorted(group) for group in groups]


def identity(leaf):
    return (leaf["id"], leaf["class"], leaf["module"])


def worker(
    directory: Path, manifest_path: Path, report_path: Path, discovered=None
) -> int:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cases = []
    original_suite = None
    if discovered is None:
        files, descriptions, children = discover(directory, manifest["pattern"], cases)
    else:
        files, descriptions, children, cases, original_suite, prepared_units = (
            discovered
        )
    if [path.name for path in files] != manifest["files"] or descriptions != manifest[
        "descriptions"
    ]:
        raise ValueError("worker discovery differs from the original suite")
    units = (
        prepared_units
        if discovered is not None
        else build_units(files, descriptions, children, manifest["split_files"])
    )
    if units != manifest["units"]:
        raise ValueError("worker fixture-boundary units differ from discovery")
    indices = manifest["indices"]
    if not indices or any(
        type(index) is not int or not 0 <= index < len(units) for index in indices
    ):
        raise ValueError("worker manifest has invalid module indices")
    if indices != sorted(set(indices)):
        raise ValueError("worker manifest has duplicate or unordered module indices")

    # Select original suite objects. Split only a plain outer suite, keeping
    # each child wrapper and complete module-fixture scope together.
    selected = []
    expected = []
    owner = {}
    for index in indices:
        unit = units[index]
        suite = children[unit["file"]]
        if original_suite is not None:
            pass
        elif unit["children"] is None:
            selected.append(suite)
        else:
            parts = list(suite)
            selected.append(unittest.TestSuite(parts[i] for i in unit["children"]))
        for leaf in unit_leaves(unit, descriptions):
            key = identity(leaf)
            expected.append(key)
            owner[key] = (files[unit["file"]].name, unit["key"])

    if original_suite is not None:
        selected = [original_suite]

    class TimedResult(unittest.TextTestResult):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            tracking.result = getattr(tracking, "result", self)
            self.started = []
            self.stopped = []
            self.started_at = None
            self.module_seconds = {}
            self.unit_seconds = {}

        def addSkip(self, test, reason):
            tracking.note_skip()
            super().addSkip(test, reason)

        def startTest(self, test):
            self.started.append(identity(describe(test)))
            self.started_at = time.monotonic()
            super().startTest(test)

        def stopTest(self, test):
            key = identity(describe(test))
            self.stopped.append(key)
            if self.started_at is not None and key in owner:
                name, unit = owner[key]
                elapsed = time.monotonic() - self.started_at
                self.module_seconds[name] = self.module_seconds.get(name, 0.0) + elapsed
                self.unit_seconds[unit] = self.unit_seconds.get(unit, 0.0) + elapsed
            self.started_at = None
            super().stopTest(test)

    with FixtureDispatchTracker(
        cases, lambda case: identity(describe(case))
    ) as tracking:
        result = unittest.TextTestRunner(verbosity=2, resultclass=TimedResult).run(
            original_suite
            if original_suite is not None
            else unittest.TestSuite(selected)
        )
    expected_counts = Counter(expected)
    started_counts = Counter(result.started)
    fixture_skipped = sum(tracking.skipped.values())
    accounted = started_counts + tracking.skipped
    execution_complete = (
        accounted == expected_counts
        and result.testsRun == len(result.started)
        and Counter(result.stopped) == started_counts
    )
    report_path.write_text(
        json.dumps(
            {
                "assigned": len(expected),
                "started": len(result.started),
                "fixture_skipped": fixture_skipped,
                "skip_events": len(result.skipped),
                "execution_complete": execution_complete,
                "module_seconds": result.module_seconds,
                "unit_seconds": result.unit_seconds,
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
    parser.add_argument("--split-module", action="append", default=[])
    parser.add_argument("--costs", type=Path, help="Measured scheduling weights")
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
        cases = []
        original = []
        files, descriptions, children = discover(
            directory, args.pattern, cases, original
        )
        units = build_units(files, descriptions, children, args.split_module)
        costs = (
            json.loads(args.costs.read_text(encoding="utf-8")) if args.costs else None
        )
        groups = partition_units(units, descriptions, args.workers, costs)
        fallback = original[1]
        if fallback:
            groups = [list(range(len(units)))]
    except (ValueError, OSError, ImportError, json.JSONDecodeError) as error:
        print(f"Installed suite discovery failed: {error}", file=sys.stderr)
        return 1

    expected = [leaf for description in descriptions for leaf in leaves(description)]
    print(
        f"Discovered {len(expected)} tests in {len(files)} modules and {len(units)} fixture-safe units; running {len(groups)} isolated workers",
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
                        "split_files": args.split_module,
                        "units": units,
                        "indices": groups[index],
                    }
                ),
                encoding="utf-8",
            )
            if fallback:
                start = time.monotonic()
                with (
                    (root / f"worker-{index}.stdout").open(
                        "w", encoding="utf-8"
                    ) as stdout,
                    (root / f"worker-{index}.stderr").open(
                        "w", encoding="utf-8"
                    ) as stderr,
                ):
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        code = worker(
                            directory,
                            manifest,
                            report,
                            (files, descriptions, children, cases, original[0], units),
                        )
                elapsed[index] = time.monotonic() - start
                return code
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

        if fallback:
            results = [run_worker(0)]
        else:
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
                    "unit_seconds",
                }:
                    raise ValueError("invalid worker report")
                if data["assigned"] != sum(
                    len(unit_leaves(units[i], descriptions)) for i in groups[index]
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
                    files[units[i]["file"]].name for i in groups[index]
                }:
                    raise ValueError("worker report contains unexpected module timings")
                unit_seconds = data["unit_seconds"]
                if not isinstance(unit_seconds, dict) or not set(unit_seconds) <= {
                    units[i]["key"] for i in groups[index]
                }:
                    raise ValueError("worker report contains unexpected unit timings")
                if any(
                    type(value) not in (int, float)
                    or not math.isfinite(value)
                    or value < 0
                    for value in (*module_seconds.values(), *unit_seconds.values())
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
                    "unit_seconds": {},
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
                        "units": len(units),
                        "uncosted_units": sorted(
                            unit["key"]
                            for unit in units
                            if unit["key"] not in (costs or {})
                        )
                        if costs is not None
                        else [],
                        "unused_costs": sorted(
                            set(costs) - {unit["key"] for unit in units}
                        )
                        if costs is not None
                        else [],
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


def supervised_main():
    # Discovery and hooks occur only in the monitored child. A successful
    # process exit without the final receipt must never waive the test lane.
    if "--worker-manifest" in sys.argv:
        return main()
    marker = "--completion-receipt"
    if marker in sys.argv:
        index = sys.argv.index(marker)
        receipt = Path(sys.argv[index + 1])
        del sys.argv[index : index + 2]
        code = main()
        receipt.write_text(str(code), encoding="utf-8")
        return code
    with tempfile.TemporaryDirectory(prefix="unittest-completion-") as temporary:
        receipt = Path(temporary) / "complete"
        command = [sys.executable]
        if sys.flags.isolated:
            command.append("-I")
        command.extend(
            [str(Path(__file__).resolve()), marker, str(receipt), *sys.argv[1:]]
        )
        code = subprocess.call(command)
        try:
            recorded = receipt.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            recorded = None
        if recorded != str(code):
            print("Runner exited without its final completion receipt", file=sys.stderr)
            return 1
        return code


if __name__ == "__main__":
    raise SystemExit(supervised_main())
