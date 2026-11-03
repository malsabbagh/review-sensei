"""Run the complete discovered checkout suite in bounded isolated processes.

Optional branch coverage is collected per worker and combined. Workers execute
disjoint test IDs, preserve class/module fixtures, and all finish on failure.
Reviewed, fixture-disjoint hooks execute their original subtree before workers.
Other hook-bearing suites retain one original-root execution in the controller.
This is the checkout lane: coverage traces the editable source packages.
Installed-wheel suites run directly with unittest and their own import guards.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import dis
import hashlib
import json
import os
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
from types import ModuleType


class FixtureDispatchTracker:
    """Credit skips only at the default runner's completed fixture dispatch.

    Original handler locals identify the fixture; the outer dispatch return
    supplies its final flags. This preserves forwarding handlers without
    trusting a later manual handler call from a test body. Forwarding handlers
    must preserve the selected test's effective fixture class; replacing the
    allocated concrete TestCase type fails accounting.
    """

    def __init__(self, cases, identity):
        self.identities = {id(case): identity(case) for case in cases}
        self.case_types = {id(case): type(case) for case in cases}
        self.skipped = Counter()
        self.episodes = {}
        self.dispatches = {}
        self.module_dispatch = None
        self.codes = {
            unittest.TestSuite._handleModuleFixture.__code__: "Module",
            unittest.TestSuite._handleClassSetUp.__code__: "Class",
        }
        self.run_code = unittest.TestSuite.run.__code__
        names = {"_handleModuleFixture": "Module", "_handleClassSetUp": "Class"}
        self.call_lines = {
            instruction.positions.lineno: names[instruction.argval]
            for instruction in dis.get_instructions(unittest.TestSuite.run)
            if instruction.opname in {"LOAD_ATTR", "LOAD_METHOD"}
            and instruction.argval in names
        }

    def __enter__(self):
        self.previous = sys.getprofile()
        self.active = False
        return self

    def __exit__(self, *exc):
        if self.active and sys.getprofile() == self.observe:
            sys.setprofile(self.previous)
        self.active = False

    def dispatch_phase(self, caller):
        if (
            caller is not None
            and caller.f_code is self.run_code
            and caller.f_locals.get("result") is self.result
        ):
            return self.call_lines.get(caller.f_lineno)
        return None

    def boundary(self, frame, phase):
        selected = frame.f_locals.get("test")
        current = frame
        while current.f_back is not None:
            caller = current.f_back
            if caller.f_code is self.run_code:
                if (
                    self.dispatch_phase(caller) == phase
                    and caller.f_locals.get("test") is selected
                ):
                    return current
                return None
            current = caller
        return None

    def register(self, frame, phase):
        return self.dispatches.setdefault(
            id(frame),
            {
                "phase": phase,
                "test": frame.f_back.f_locals["test"],
                "skip_scopes": [],
                "locals": None,
            },
        )

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
                boundary = self.boundary(frame, phase)
                if (
                    actual_skip
                    and name.startswith(f"setUp{phase} (")
                    and frame.f_locals.get("result") is self.result
                    and boundary is not None
                ):
                    self.register(boundary, phase)["skip_scopes"].append(
                        frame.f_locals[
                            "currentModule" if phase == "Module" else "currentClass"
                        ]
                    )
                    if not self.active:
                        self.previous = sys.getprofile()
                        self.active = True
                        sys.setprofile(self.observe)
                return
            frame = frame.f_back

    def observe(self, frame, event, arg):
        if self.previous is not None:
            self.previous(frame, event, arg)
        if event not in {"call", "return"}:
            return
        original_phase = self.codes.get(frame.f_code)
        caller = frame.f_back
        possible_boundary = caller is not None and caller.f_code is self.run_code
        if (
            original_phase is None
            and not possible_boundary
            and id(frame) not in self.dispatches
        ):
            return
        phase = self.dispatch_phase(caller) if possible_boundary else None
        if event == "call":
            if phase is not None:
                self.register(frame, phase)
            return
        if original_phase is not None and frame.f_locals.get("result") is self.result:
            boundary = self.boundary(frame, original_phase)
            if boundary is not None:
                self.register(boundary, original_phase)["locals"] = dict(frame.f_locals)
        dispatch = self.dispatches.pop(id(frame), None)
        if dispatch is None:
            return
        # The same frame may be both original handler and outer boundary;
        # capture its locals before examining the completed dispatch.
        if phase == dispatch["phase"]:
            self.complete_dispatch(dispatch)
        if (
            dispatch["phase"] == "Class"
            and not self.dispatches
            and all(value is None for value in self.episodes.values())
        ):
            self.active = False
            sys.setprofile(self.previous)

    def complete_dispatch(self, dispatch):
        local = dispatch["locals"]
        if local is None:
            return  # A complete private reimplementation cannot supply proof.
        result = self.result
        selected = dispatch["test"]
        if type(selected) is not self.case_types.get(id(selected)):
            return  # A replacement TestCase type is not the allocated fixture.
        phase = dispatch["phase"]
        if phase == "Module":
            current = local["currentModule"]
            failed = getattr(result, "_moduleSetUpFailed", False)
            self.module_dispatch = (selected, current)
            had_skip = current in dispatch["skip_scopes"]
            if current != local["previousModule"] or not failed or had_skip:
                self.episodes["Module"] = current if had_skip and failed else None
            return
        cls = local["currentClass"]
        failed = getattr(cls, "_classSetupFailed", False)
        had_skip = any(scope is cls for scope in dispatch["skip_scopes"])
        if cls != local["previousClass"] or not failed or had_skip:
            self.episodes["Class"] = cls if had_skip and failed else None
        module_blocked = (
            getattr(result, "_moduleSetUpFailed", False)
            and self.module_dispatch is not None
            and self.module_dispatch[0] is selected
            and self.episodes.get("Module") is not None
            and self.episodes.get("Module") == self.module_dispatch[1]
        )
        class_blocked = failed and self.episodes.get("Class") is cls
        if module_blocked or class_blocked:
            identity = self.identities.get(id(selected))
            if identity is not None:
                self.skipped[identity] += 1


class HookAwareLoader(unittest.TestLoader):
    def __init__(self):
        super().__init__()
        self.hook_seen = False
        self.hooks = []

    def loadTestsFromModule(self, module, *args, **kwargs):
        hook = getattr(module, "load_tests", None) is not None
        if hook:
            self.hook_seen = True
        suite = super().loadTestsFromModule(module, *args, **kwargs)
        if hook:
            self.hooks.append((module.__name__, suite))
        return suite


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


def execution_plan(suite, loader, identities, groups, controller_modules):
    """Admit a reviewed, fixture-disjoint hook phase before isolated workers."""
    names = [name for name, _suite in loader.hooks]
    requested = set(controller_modules)
    if len(requested) != len(controller_modules):
        raise ValueError("controller modules must be distinct")
    if requested - set(names):
        raise ValueError("controller module must name a discovered load_tests hook")

    def original(reason):
        return {
            "mode": "original-root",
            "reason": reason,
            "controller": None,
            "controller_ids": [],
            "workers": [(0, identities)],
        }

    if not loader.hook_seen:
        return {
            "mode": "workers",
            "reason": None,
            "controller": None,
            "controller_ids": [],
            "workers": list(enumerate(groups)),
        }
    if not requested or requested != set(names):
        return original("discovery hook has no reviewed controller opt-in")
    if len(names) != len(set(names)):
        return original("hook was invoked more than once during discovery")
    if len(groups) == 1:
        return original("one worker preserves original-root execution")

    seen = set()

    def plain_tree(item):
        if not isinstance(item, unittest.TestSuite):
            return isinstance(item, unittest.TestCase)
        if id(item) in seen or not _unmodified_builtin_suite(item):
            return False
        seen.add(id(item))
        return all(plain_tree(child) for child in item)

    if not plain_tree(suite):
        return original("custom, modified or repeated suite ownership")
    children = list(suite)
    targets = {id(child) for _name, child in loader.hooks}
    if len(targets) != len(names) or not targets <= {id(child) for child in children}:
        return original("hook output is not a distinct top-level subtree")
    selected = [child for child in children if id(child) in targets]
    controller = selected[0] if len(selected) == 1 else unittest.TestSuite(selected)
    cases = test_cases(suite)
    if len({id(case) for case in cases}) != len(cases):
        return original("a test object occurs more than once")
    if [case.id() for case in cases] != identities:
        return original("discovery identities changed before execution")
    owned_cases = test_cases(controller)
    owned = {id(case) for case in owned_cases}
    identity = {id(case): name for case, name in zip(cases, identities)}

    scopes = []
    for case in cases:
        cls = type(case)
        if cls.__getattribute__ is not object.__getattribute__ or any(
            "__class__" in base.__dict__ for base in cls.__mro__ if base is not object
        ):
            return original("effective fixture scope is not statically stable")
        if id(case) not in owned:
            method = case.id
            if (
                cls.id is not unittest.TestCase.id
                or getattr(method, "__func__", None) is not unittest.TestCase.id
                or getattr(method, "__self__", None) is not case
            ):
                return original("worker identities require original-root execution")
        module = cls.__module__
        if not isinstance(module, str) or not module:
            return original("effective fixture module is not stable")
        if id(case) not in owned:
            resolved = sys.modules.get(module)
            for part in cls.__qualname__.split("."):
                if not isinstance(resolved, (ModuleType, type)):
                    resolved = None
                    break
                resolved = vars(resolved).get(part)
            if resolved is not cls:
                return original("worker class cannot be resolved without factories")
        scopes.append((id(case) in owned, id(cls), module))
    classes = {cls for inside, cls, _module in scopes if inside}
    modules = {module for inside, _cls, module in scopes if inside}
    if any(
        not inside and (cls in classes or module in modules)
        for inside, cls, module in scopes
    ):
        return original("controller and workers share a fixture scope")
    controller_ids = [identity[id(case)] for case in owned_cases]
    owned_ids = set(controller_ids)
    workers = [
        (bucket, [name for name in group if name not in owned_ids])
        for bucket, group in enumerate(groups)
    ]
    workers = [(bucket, members) for bucket, members in workers if members]
    assigned = controller_ids + [
        name for _bucket, members in workers for name in members
    ]
    if Counter(assigned) != Counter(identities):
        raise ValueError("controller plan does not preserve the exact inventory")
    return {
        "mode": "controller-then-workers",
        "reason": None,
        "controller": controller,
        "controller_ids": controller_ids,
        "workers": workers,
    }


def inventory_digest(identities):
    return hashlib.sha256("\n".join(sorted(identities)).encode()).hexdigest()


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
    parser.add_argument(
        "--controller-module",
        action="append",
        default=[],
        help="Reviewed hook module to run once before isolated worker launches",
    )
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
        plan = execution_plan(
            suite, loader, all_identities, groups, args.controller_module
        )
    except (ValueError, OSError) as exc:
        print(exc, file=sys.stderr)
        return 1
    fallback = plan["mode"] == "original-root"
    controller_suite = plan["controller"]
    controller_ids = plan["controller_ids"]
    bucket_ids = [bucket for bucket, _members in plan["workers"]]
    groups = [members for _bucket, members in plan["workers"]]
    if plan["mode"] == "workers" and fallback_coverage is not None:
        fallback_coverage.stop()
    phase_label = (
        f"controller phase {len(controller_ids)} tests; "
        if controller_suite is not None
        else ""
    )
    print(
        f"Discovered {len(all_identities)} tests; {phase_label}running {len(groups)} disjoint workers",
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
        worker_environment = os.environ.copy()
        worker_cwd = str(Path.cwd())
        launch_worker = subprocess.call

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
                code = launch_worker(
                    commands[index],
                    stdout=stdout,
                    stderr=stderr,
                    env=worker_environment,
                    cwd=worker_cwd,
                )
            elapsed[index] = time.monotonic() - start
            return code

        controller_code = None
        controller_elapsed = None
        if controller_suite is not None:
            # Hook tests may patch process-global environment/registries. Finish
            # their original subtree and fixture cleanup before any Popen call.
            phase_start = time.monotonic()
            with (
                (Path(scratch) / "controller.stdout").open(
                    "w", encoding="utf-8"
                ) as stdout,
                (Path(scratch) / "controller.stderr").open(
                    "w", encoding="utf-8"
                ) as stderr,
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                controller_code = execute_suite(
                    controller_suite,
                    controller_ids,
                    Path(scratch) / "timing-controller.json",
                )
            if fallback_coverage is not None:
                fallback_coverage.stop()
                fallback_coverage.save()
                shutil.copyfile(
                    Path(coverage_scratch.name) / "coverage",
                    Path(scratch) / ".coverage.controller",
                )
            controller_elapsed = time.monotonic() - phase_start

        # Await all workers before replaying separate logs in stable worker
        # order. Keep manifests/logs alive until execution and reporting finish.
        if fallback:
            results = [run_worker(0)]
        elif groups:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(groups)) as pool:
                results = list(pool.map(run_worker, range(len(groups))))
        else:
            results = []
        if controller_suite is not None:
            for suffix, stream in (("stdout", sys.stdout), ("stderr", sys.stderr)):
                print(
                    f"[checkout controller: {len(controller_ids)} tests, exit {controller_code}]",
                    file=stream,
                    flush=True,
                )
                with (Path(scratch) / f"controller.{suffix}").open(
                    encoding="utf-8", errors="replace"
                ) as output:
                    shutil.copyfileobj(output, stream)
                stream.flush()
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
                    "worker": bucket_ids[index],
                    "execution_ordinal": index,
                    "assigned": len(groups[index]),
                    "inventory_sha256": inventory_digest(groups[index]),
                    "exit": code,
                    "elapsed_seconds": round(elapsed[index], 3),
                    "timing_error": timing_error,
                    **data,
                }
            )
        controller = None
        if controller_suite is not None:
            data, timing_error = read_worker_report(
                Path(scratch) / "timing-controller.json"
            )
            execution_complete &= (
                timing_error is None
                and data["inventory_complete"] is True
                and data["executed"] + data["fixture_skipped"] == len(controller_ids)
            )
            if timing_error:
                print(f"Controller: {timing_error}", file=sys.stderr)
            controller = {
                "assigned": len(controller_ids),
                "exit": controller_code,
                "elapsed_seconds": round(controller_elapsed, 3),
                "inventory_sha256": inventory_digest(controller_ids),
                "timing_error": timing_error,
                **data,
            }
        coverage_error = None
        coverage_ok = None
        if args.coverage:
            from coverage import Coverage
            from coverage.exceptions import CoverageException

            paths = [
                Path(scratch) / f".coverage.worker{index}"
                for index in range(len(groups))
            ]
            if controller_suite is not None:
                paths.insert(0, Path(scratch) / ".coverage.controller")
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
                        "inventory_sha256": inventory_digest(all_identities),
                        "mode": plan["mode"],
                        "fallback_reason": plan["reason"],
                        "requested_workers": args.workers,
                        "effective_worker_count": len(workers),
                        "controller": controller,
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
    return (
        0
        if complete
        and controller_code in (None, 0)
        and all(code == 0 for code in results)
        else 1
    )


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
