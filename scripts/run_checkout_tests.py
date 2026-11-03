"""Run the complete discovered checkout suite in bounded isolated processes.

Optional branch coverage is collected per worker and combined. Workers execute
disjoint test IDs, preserve class/module fixtures, and all finish on failure.
Hook-bearing suites execute the original discovered root once in the controller.
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


def _unmodified_builtin_suite(suite: unittest.TestSuite) -> bool:
    """Compare against this interpreter's fresh built-in suite, with the same tests."""
    if type(suite) is not unittest.TestSuite:
        return False
    try:
        reference = unittest.TestSuite(list(suite))
        actual_state, reference_state = vars(suite), vars(reference)
        return actual_state.keys() == reference_state.keys() and all(
            type(actual_state[key]) is type(value) and actual_state[key] == value
            for key, value in reference_state.items()
        )
    except (AttributeError, TypeError, ValueError):
        return False


def test_ids(suite: unittest.TestSuite, *, allow_custom=False) -> list[str]:
    # This check runs at every recursive level, including nested load_tests suites.
    if not allow_custom and not _unmodified_builtin_suite(suite):
        raise ValueError("custom unittest suite wrappers cannot be sharded safely")
    identities = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            identities.extend(test_ids(item, allow_custom=allow_custom))
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


def count_fixture_skips(remaining, scopes, skipped_events) -> tuple[int, bool]:
    """Consume matching identities once and report malformed skip holders."""
    fixture_skipped = 0
    malformed = False
    for skipped, _reason in skipped_events:
        if isinstance(skipped, unittest.TestCase):
            continue
        holder_id = getattr(skipped, "id", None)
        if not callable(holder_id):
            malformed = True
            continue
        try:
            descriptor = holder_id()
        except Exception:
            malformed = True
            continue
        if not isinstance(descriptor, str):
            malformed = True
            continue
        match = re.fullmatch(r"setUp(Class|Module) \((.+)\)", descriptor)
        if not match:
            continue
        scope_index = 0 if match.group(1) == "Class" else 1
        matches = [
            identity
            for identity in remaining
            if (scope := scopes.get(identity)) is not None
            and scope[scope_index] == match.group(2)
        ]
        # unittest's class skip holder only identifies the qualified class
        # name. Different class objects can share that name, so do not guess
        # which unstarted class was skipped when the holder is ambiguous.
        if (
            scope_index == 0
            and len({id(scopes[identity][2]) for identity in matches}) > 1
        ):
            continue
        for identity in matches:
            fixture_skipped += remaining.pop(identity)
    return fixture_skipped, malformed


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


def execute_suite(suite, identities, worker_report, load_errors=False):
    cases = test_cases(suite)
    loaded = []
    for case in cases:
        # unittest selects class/module fixtures via test.__class__.
        fixture_class = case.__class__
        loaded.append(
            (
                case.id(),
                f"{fixture_class.__module__}.{fixture_class.__qualname__}",
                fixture_class.__module__,
                fixture_class,
            )
        )
    if load_errors or sorted(item[0] for item in loaded) != sorted(identities):
        print(
            "Worker failed to load its exact assigned test inventory",
            file=sys.stderr,
        )
        return 1

    scopes = {
        identity: (class_name, module, fixture_class)
        for identity, class_name, module, fixture_class in loaded
    }

    class TimedResult(unittest.TextTestResult):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.class_seconds: dict[str, float] = {}
            self.started: float | None = None
            tracking.result = getattr(tracking, "result", self)
            self.started_ids: list[str] = []
            self.stopped_ids: list[str] = []

        def addSkip(self, test, reason):
            tracking.note_skip()
            super().addSkip(test, reason)

        def startTest(self, test):
            self.started_ids.append(test.id())
            self.started = time.monotonic()
            super().startTest(test)

        def stopTest(self, test):
            self.stopped_ids.append(test.id())
            if self.started is not None:
                name = test.id().rpartition(".")[0]
                self.class_seconds[name] = (
                    self.class_seconds.get(name, 0) + time.monotonic() - self.started
                )
            self.started = None
            super().stopTest(test)

    with FixtureDispatchTracker(cases, lambda case: case.id()) as tracking:
        result = unittest.TextTestRunner(verbosity=2, resultclass=TimedResult).run(
            suite
        )
    expected = Counter(identities)
    observed = Counter(result.started_ids)
    remaining = expected - observed
    _, malformed_skip = count_fixture_skips(Counter(), scopes, result.skipped)
    accounted = observed + tracking.skipped
    remaining = expected - accounted
    fixture_skipped = sum(tracking.skipped.values())
    inventory_complete = (
        not (accounted - expected)
        and not remaining
        and result.testsRun == len(result.started_ids)
        and Counter(result.stopped_ids) == observed
        and not malformed_skip
    )
    if not inventory_complete:
        print(
            "Worker did not account for its exact assigned test inventory",
            file=sys.stderr,
        )
    if worker_report is not None:
        worker_report.write_text(
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


def _main(coverage_owners) -> int:
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
    loader = HookAwareLoader()
    if args.worker_manifest is not None:
        identities = json.loads(args.worker_manifest.read_text(encoding="utf-8"))
        suite = loader.loadTestsFromNames(identities)
        return execute_suite(suite, identities, args.worker_report, loader.errors)
    fallback_coverage = None
    if args.coverage:
        from coverage import Coverage

        coverage_scratch = tempfile.TemporaryDirectory(
            prefix="checkout-parent-coverage-"
        )
        fallback_coverage = Coverage(
            data_file=str(Path(coverage_scratch.name) / "coverage"),
            branch=True,
            source=["review_sensei", "review_sensei_github"],
        )
        coverage_owners.append((fallback_coverage, coverage_scratch))
        fallback_coverage.start()
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
        all_identities = test_ids(suite, allow_custom=loader.hook_seen)
        groups = partition(all_identities, args.workers, costs)
    except (ValueError, OSError) as exc:
        print(exc, file=sys.stderr)
        return 1
    fallback = loader.hook_seen
    if fallback:
        groups = [all_identities]
    elif fallback_coverage is not None:
        fallback_coverage.stop()
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
            if fallback:
                with (
                    (Path(scratch) / f"worker-{index}.stdout").open(
                        "w", encoding="utf-8"
                    ) as stdout,
                    (Path(scratch) / f"worker-{index}.stderr").open(
                        "w", encoding="utf-8"
                    ) as stderr,
                ):
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        code = execute_suite(
                            suite,
                            all_identities,
                            Path(scratch) / f"timing-{index}.json",
                        )
                if fallback_coverage is not None:
                    fallback_coverage.stop()
                    fallback_coverage.save()
                    shutil.copyfile(
                        Path(coverage_scratch.name) / "coverage",
                        Path(scratch) / ".coverage.worker0",
                    )
                elapsed[index] = time.monotonic() - start
                return code
            with (
                (Path(scratch) / f"worker-{index}.stdout").open("wb") as stdout,
                (Path(scratch) / f"worker-{index}.stderr").open("wb") as stderr,
            ):
                code = subprocess.call(commands[index], stdout=stdout, stderr=stderr)
            elapsed[index] = time.monotonic() - start
            return code

        # Await all workers before replaying separate logs in stable worker
        # order. Keep manifests/logs alive until execution and reporting finish.
        if fallback:
            results = [run_worker(0)]
        else:
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
                            "\n".join(sorted(all_identities)).encode()
                        ).hexdigest(),
                        "discovered": len(all_identities),
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


def main() -> int:
    coverage_owners = []
    try:
        return _main(coverage_owners)
    finally:
        for coverage, temporary in coverage_owners:
            coverage.stop()
            temporary.cleanup()


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
