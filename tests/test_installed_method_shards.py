"""Independent-method scheduling uses synthetic bodies, never held fixtures."""

import ast
import hashlib
import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts/run_installed_test_suites.py"
COLLECTOR = ROOT / "tests/dist_safe/test_quarantine_contract.py"
MODULE = "test_quarantine_method_probe"
METHODS = (
    ("Alpha", "test_a", 3),
    ("Alpha", "test_b", 3),
    ("Beta", "test_a", 6),
    ("Beta", "test_b", 3),
    ("Beta", "test_c", 2),
    ("Delta", "test_a", 2),
    ("Gamma", "test_a", 9),
)
IDS = tuple(f"{MODULE}.{cls}.{method}" for cls, method, _ in METHODS)


def declaration(source):
    return next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "INSTALLED_METHOD_SHARD_CONTRACT"
            for target in node.targets
        )
    )


class InstalledIndependentMethodTests(unittest.TestCase):
    def execute(self, *, workers=4, mode="success", approved=True, repeat=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixtures = root / "tests"
            lane = fixtures / "dist_safe"
            lane.mkdir(parents=True)
            support = textwrap.dedent("""\
                import atexit, json, os, socket, time, unittest
                from pathlib import Path
                CASES = {}
                SHARED = []
                ROOT = Path(__file__).parent
                LOG = ROOT / ('events-' + str(os.getpid()) + '.jsonl')
                def record(kind, label=''):
                    with LOG.open('a') as stream:
                        stream.write(json.dumps([kind, label, time.monotonic()]) + '\\n')
                def unsafe_connect(*args, **kwargs):
                    record('UNGUARDED CONNECT')
                    raise RuntimeError('synthetic socket sentinel; no real network')
                socket.socket.connect = unsafe_connect
                def blocked(label):
                    try: socket.socket.connect(None, ('example.invalid', 1))
                    except AssertionError as error:
                        assert str(error) == 'live network forbidden', str(error)
                    else: raise AssertionError('socket guard absent')
                    record('guarded', label)
                def restored():
                    assert socket.socket.connect is unsafe_connect
                    record('exit-restored')
                atexit.register(restored)
                def configure(suite):
                    if isinstance(suite, unittest.TestSuite):
                        for child in suite: configure(child)
                    else:
                        suite.serial = len(CASES)
                        CASES[suite.serial] = suite
                        suite.configuration = {'values': [42]}
                def cleanup(label, fail=False):
                    blocked(label)
                    if fail: raise RuntimeError('synthetic cleanup failure')
                def original_case(case):
                    assert CASES[case.serial] is case, 'case was reconstructed'
                    assert case.configuration == {'values': [42]}
                """)
            if mode in {"guard-skip", "guard-error"}:
                support += (
                    "class SocketMeta(type):\n"
                    "    def __setattr__(cls, name, value):\n"
                    "        if name == 'connect' and value is not unsafe_connect and not isinstance(value, staticmethod):\n"
                    "            record('guard-unavailable')\n"
                    + (
                        "            raise unittest.SkipTest('synthetic socket setup skip')\n"
                        if mode == "guard-skip"
                        else "            raise RuntimeError('synthetic socket setup error')\n"
                    )
                    + "        super().__setattr__(name, value)\n"
                    "class SyntheticSocket(metaclass=SocketMeta):\n"
                    "    connect = staticmethod(unsafe_connect)\n"
                    "socket.socket = SyntheticSocket\n"
                )
            if mode == "noop-enter":
                support += "unittest.enterModuleContext = lambda cm: None\n"
            if mode == "enter-code":
                support += "unittest.enterModuleContext.__code__ = (lambda cm: None).__code__\n"
            if mode == "enter-link":
                support += "unittest.enterModuleContext.__globals__['_enter_context'] = lambda cm, cleanup: None\n"
            if mode == "cleanup-link":
                support += "unittest.enterModuleContext.__globals__['addModuleCleanup'] = lambda *args: None\n"
            if mode == "patch-code":
                support += "from unittest.mock import patch\npatch.__code__ = (lambda *args, **kwargs: None).__code__\n"
            (fixtures / "probe_support.py").write_text(support, encoding="utf-8")
            (fixtures / "packaging_guard.py").write_text(
                "from probe_support import record\n"
                "def assert_distribution_import(): record('core-import-guard')\n"
                "def is_disarmed(): return False\n",
                encoding="utf-8",
            )
            (fixtures / "review_sensei_github.py").write_text(
                "import sys\nfrom pathlib import Path\n"
                "__file__ = str(Path(sys.prefix) / 'synthetic-installed.py')\n",
                encoding="utf-8",
            )
            source = (
                "import unittest\n"
                "from probe_support import blocked, cleanup, original_case, record\n"
            )
            if mode in {"module-setup", "module-teardown", "module-hook"}:
                source += {
                    "module-setup": "def setUpModule(): blocked('original-setup')\n",
                    "module-teardown": "def tearDownModule(): blocked('original-teardown')\n",
                    "module-hook": "def load_tests(loader, tests, pattern): return tests\n",
                }[mode]
            previous_class = None
            for cls, method, count in METHODS:
                if mode == "missing-method" and cls == "Gamma":
                    continue
                if cls != previous_class:
                    source += f"class {cls}(unittest.TestCase):\n"
                    if mode == "class-setup" and cls == "Alpha":
                        source += (
                            "    @classmethod\n"
                            "    def setUpClass(cls): blocked('original-class-setup')\n"
                        )
                    if mode == "class-state" and cls == "Alpha":
                        source += "    shared = []\n"
                    if mode == "custom-dispatch" and cls == "Alpha":
                        source += (
                            "    def run(self, result): return super().run(result)\n"
                        )
                    previous_class = cls
                label = f"{MODULE}.{cls}.{method}"
                source += (
                    f"    def {method}(self):\n"
                    "        original_case(self)\n"
                    f"        record('body', {label!r})\n"
                    f"        self.addCleanup(blocked, {label + ':test-cleanup'!r})\n"
                    f"        self.addClassCleanup(cleanup, {label + ':class-cleanup'!r})\n"
                    f"        unittest.addModuleCleanup(cleanup, {label + ':module-cleanup'!r}, {mode == 'cleanup-error' and cls == 'Gamma'!r})\n"
                    f"        for number in range({count}):\n"
                    "            with self.subTest(number=number):\n"
                    f"                blocked({label!r} + ':' + str(number))\n"
                    f"                record('case', {label!r} + ':' + str(number))\n"
                )
                if mode == "subtest-failure" and cls == "Beta" and method == "test_b":
                    source += "                self.assertNotEqual(number, 1, 'synthetic fault')\n"
            if mode == "extra-method":
                source += "    def test_extra(self): blocked('extra-method')\n"
            if mode == "metadata-state":
                source += "Alpha.__static_attributes__ = []\n"
            if mode == "changed-definition":
                source += "Alpha.test_a.__code__ = Alpha.test_b.__code__\n"
            (fixtures / (MODULE + ".py")).write_text(source, encoding="utf-8")
            (fixtures / "test_quarantine_other.py").write_text(
                "import unittest\nfrom probe_support import blocked\n"
                "class First(unittest.TestCase):\n"
                "    def test_a(self): blocked('other-first')\n"
                "class Second(unittest.TestCase):\n"
                "    def test_b(self): blocked('other-second')\n",
                encoding="utf-8",
            )
            contract = (
                "independent-socket-methods-v1",
                (
                    (
                        MODULE,
                        IDS,
                        tuple(
                            (
                                name,
                                hashlib.sha256(
                                    (fixtures / name).read_bytes()
                                ).hexdigest(),
                            )
                            for name in (MODULE + ".py", "probe_support.py")
                        ),
                    ),
                ),
            )
            collector = COLLECTOR.read_text(encoding="utf-8")
            node = declaration(collector)
            lines = collector.splitlines(keepends=True)
            # Rebind only the reviewed test-data declaration to a synthetic
            # module/hash/IDs. The production checkpoint file is never imported.
            collector = (
                "".join(lines[: node.lineno - 1])
                + "INSTALLED_METHOD_SHARD_CONTRACT = "
                + repr(contract)
                + "\n"
                + "".join(lines[node.end_lineno :])
            )
            collector = collector.replace(
                "tests.addTests(loader.loadTestsFromModule(module))",
                "from probe_support import CASES, configure\n"
                f"        if path.stem == {MODULE!r}: CASES.clear()\n"
                "        loaded = loader.loadTestsFromModule(module)\n"
                f"        if path.stem == {MODULE!r}: configure(loaded)\n"
                "        tests.addTests(loaded)",
            )
            if mode == "custom-wrapper":
                collector = collector.replace(
                    "        tests.addTests(loaded)",
                    "        class Custom(unittest.TestSuite): pass\n"
                    f"        if path.stem == {MODULE!r}: loaded = unittest.TestSuite([Custom(loaded)])\n"
                    "        tests.addTests(loaded)",
                )
            if mode == "shared-configuration":
                collector = collector.replace(
                    "        tests.addTests(loaded)",
                    "        from probe_support import CASES, SHARED\n"
                    f"        if path.stem == {MODULE!r}:\n"
                    "            for case in CASES.values(): case.extra = SHARED\n"
                    "        tests.addTests(loaded)",
                )
            if mode == "duplicate-case":
                collector = collector.replace(
                    "        tests.addTests(loaded)",
                    f"        if path.stem == {MODULE!r}: loaded.addTest(CASES[0])\n"
                    "        tests.addTests(loaded)",
                )
            worker_drift = {
                "worker-declaration": (
                    "global INSTALLED_METHOD_SHARD_CONTRACT\n"
                    "            version, rows = INSTALLED_METHOD_SHARD_CONTRACT\n"
                    "            name, ids, sources = rows[0]\n"
                    "            INSTALLED_METHOD_SHARD_CONTRACT = (version, ((name, ids[::-1], sources),))"
                ),
                "worker-closure": "_guarded_setups[module].__closure__[0].cell_contents = lambda: None",
                "worker-guard-code": (
                    "function = _guarded_setups[module]\n"
                    "            function.__code__ = function.__code__.replace(co_consts=tuple('changed protection' if value == 'live network forbidden' else value for value in function.__code__.co_consts))"
                ),
                "worker-config": "CASES[0].configuration = {'values': [43]}",
                "worker-definition": "module.Alpha.test_a.__defaults__ = (None,)",
            }
            if mode in worker_drift:
                collector = collector.replace(
                    "        tests.addTests(loaded)",
                    f"        if path.stem == {MODULE!r} and Path.cwd().name.startswith('worker-'):\n"
                    f"            {worker_drift[mode]}\n"
                    "        tests.addTests(loaded)",
                )
            (lane / "test_quarantine_contract.py").write_text(
                collector, encoding="utf-8"
            )
            if mode == "dependency-drift":
                with (fixtures / "probe_support.py").open("a") as stream:
                    stream.write("# changed after independent-method review\n")
            if mode == "crlf":
                for name in (MODULE + ".py", "probe_support.py"):
                    path = fixtures / name
                    path.write_bytes(path.read_text().replace("\n", "\r\n").encode())
            report = root / "report.json"
            costs = root / "costs.json"
            costs.write_text(
                json.dumps(
                    {
                        "test_quarantine_contract.py::" + MODULE: 700,
                        "test_quarantine_contract.py::test_quarantine_other": 10,
                    }
                ),
                encoding="utf-8",
            )
            command = [sys.executable, "-I"]
            if repeat:
                driver = textwrap.dedent("""\
                    import importlib.util, sys
                    from pathlib import Path
                    path, directory = sys.argv[1:3]
                    spec = importlib.util.spec_from_file_location('synthetic_runner', path)
                    runner = importlib.util.module_from_spec(spec); spec.loader.exec_module(runner)
                    before = runner.discover(Path(directory), 'test*.py')
                    module = sys.modules['test_quarantine_method_probe']
                    setup = module.setUpModule
                    after = runner.discover(Path(directory), 'test*.py')
                    assert before[0] == after[0] and before[1] == after[1]
                    assert module.setUpModule is setup
                    sys.argv = [path, *sys.argv[3:]]
                    raise SystemExit(runner.main())
                    """)
                command.extend(("-c", driver, str(RUNNER), str(lane)))
            else:
                command.append(str(RUNNER))
            command.extend(
                (
                    "--start-directory",
                    str(lane),
                    "--workers",
                    str(workers),
                    "--split-module",
                    "test_quarantine_contract.py",
                    "--costs",
                    str(costs),
                    "--report",
                    str(report),
                )
            )
            if approved:
                command.extend(("--reviewed-local-hook", "test_quarantine_contract"))
            result = subprocess.run(
                command, cwd=root, capture_output=True, text=True, timeout=30
            )
            data = json.loads(report.read_text()) if report.exists() else None
            events = {
                path.stem: [json.loads(line) for line in path.read_text().splitlines()]
                for path in sorted(fixtures.glob("events-*.jsonl"))
            }
            return result, data, events

    def assert_success(self, result, data, events, *, workers=4, exit_code=0):
        self.assertEqual(result.returncode, exit_code, result.stdout + result.stderr)
        self.assertEqual(data["hook_mode"], "reviewed-local")
        self.assertEqual(data["discovered"], 9)
        self.assertEqual(data["units"], 8)
        self.assertEqual(len(data["workers"]), workers)
        self.assertEqual(sum(w["assigned"] for w in data["workers"]), 9)
        self.assertEqual(sum(w["started"] for w in data["workers"]), 9)
        self.assertTrue(all(w["execution_complete"] for w in data["workers"]))
        rows = [row for process in events.values() for row in process]
        self.assertEqual(sorted(row[1] for row in rows if row[0] == "body"), list(IDS))
        self.assertEqual(sum(row[0] == "case" for row in rows), 28)
        self.assertNotIn("UNGUARDED CONNECT", [row[0] for row in rows])
        for process in events.values():
            self.assertEqual(sum(row[0] == "exit-restored" for row in process), 1)
        self.assertEqual(len(data["uncosted_units"]), 7)
        self.assertEqual(data["unused_costs"], [])
        self.assertEqual(sum(len(w["unit_seconds"]) for w in data["workers"]), 8)

    def test_seven_original_methods_and_all_28_subtests_run_once_in_four_workers(self):
        result, data, events = self.execute()
        self.assert_success(result, data, events)
        executing = [
            rows for rows in events.values() if any(row[0] == "body" for row in rows)
        ]
        self.assertEqual(len(executing), 4)

    def test_single_worker_preserves_inventory_configured_objects_and_guarded_cleanups(
        self,
    ):
        result, data, events = self.execute(workers=1)
        self.assert_success(result, data, events, workers=1)
        rows = next(
            rows for rows in events.values() if any(row[0] == "body" for row in rows)
        )
        self.assertEqual([row[1] for row in rows if row[0] == "body"], list(IDS))
        cleanups = [
            row[1]
            for row in rows
            if row[0] == "guarded" and row[1].endswith("module-cleanup")
        ]
        self.assertEqual(cleanups, [name + ":module-cleanup" for name in reversed(IDS)])

    def test_repeated_discovery_preserves_original_guard_and_method_plan(self):
        self.assert_success(*self.execute(repeat=True))

    def test_crlf_checkout_sources_preserve_the_same_reviewed_method_plan(self):
        self.assert_success(*self.execute(mode="crlf"))

    def test_unapproved_hook_retains_original_root(self):
        result, data, events = self.execute(approved=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(data["hook_mode"], "original-root")
        self.assertEqual(len(data["workers"]), 1)
        self.assertEqual(data["discovered"], 9)
        self.assertEqual(
            sum(row[0] == "case" for rows in events.values() for row in rows), 28
        )

    def test_changed_fixture_inputs_and_shared_behavior_stay_atomic(self):
        for mode in (
            "module-setup",
            "module-teardown",
            "module-hook",
            "class-setup",
            "class-state",
            "metadata-state",
            "changed-definition",
            "custom-dispatch",
            "custom-wrapper",
            "shared-configuration",
            "dependency-drift",
        ):
            with self.subTest(mode=mode):
                result, data, events = self.execute(mode=mode)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(data["hook_mode"], "original-root")
                self.assertIsNotNone(data["fallback_reason"])
                self.assertEqual(len(data["workers"]), 1)
                self.assertEqual(data["discovered"], 9)
                self.assertEqual(
                    sum(row[0] == "case" for rows in events.values() for row in rows),
                    28,
                )

    def test_unknown_missing_and_duplicate_methods_are_never_filtered_out(self):
        for mode, discovered, cases in (
            ("extra-method", 10, 28),
            ("missing-method", 8, 19),
            ("duplicate-case", 10, 31),
        ):
            with self.subTest(mode=mode):
                result, data, events = self.execute(mode=mode)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(data["hook_mode"], "original-root")
                self.assertEqual(data["discovered"], discovered)
                self.assertEqual(data["workers"][0]["started"], discovered)
                self.assertEqual(
                    sum(row[0] == "case" for rows in events.values() for row in rows),
                    cases,
                )

    def test_worker_declaration_guard_definition_and_configuration_drift_fail_before_bodies(
        self,
    ):
        for mode in (
            "worker-declaration",
            "worker-closure",
            "worker-guard-code",
            "worker-config",
            "worker-definition",
        ):
            with self.subTest(mode=mode):
                result, data, events = self.execute(mode=mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(data["hook_mode"], "reviewed-local")
                self.assertFalse(data["complete"])
                self.assertFalse(
                    any(row[0] == "body" for rows in events.values() for row in rows)
                )

    def test_subtest_and_cleanup_failures_propagate_after_every_method(self):
        for mode in ("subtest-failure", "cleanup-error"):
            with self.subTest(mode=mode):
                result, data, events = self.execute(mode=mode)
                self.assert_success(result, data, events, exit_code=1)
                self.assertFalse(data["complete"])

    def test_guard_setup_skips_and_errors_keep_exact_dispatch_accounting(self):
        for mode in ("guard-skip", "guard-error"):
            with self.subTest(mode=mode):
                result, data, events = self.execute(mode=mode)
                self.assertEqual(
                    result.returncode,
                    0 if mode == "guard-skip" else 1,
                    result.stdout + result.stderr,
                )
                self.assertEqual(data["hook_mode"], "reviewed-local")
                self.assertEqual(data["units"], 8)
                self.assertFalse(
                    any(row[0] == "body" for rows in events.values() for row in rows)
                )
                if mode == "guard-skip":
                    self.assertTrue(data["complete"])
                    self.assertEqual(
                        sum(w["fixture_skipped"] for w in data["workers"]), 9
                    )
                    self.assertEqual(sum(w["started"] for w in data["workers"]), 0)
                else:
                    self.assertFalse(data["complete"])

    def test_changed_socket_collaborators_refuse_before_original_root_execution(self):
        for mode in (
            "noop-enter",
            "enter-code",
            "enter-link",
            "cleanup-link",
            "patch-code",
        ):
            with self.subTest(mode=mode):
                result, data, events = self.execute(mode=mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertIsNone(data)
                self.assertIn("socket fixture collaborator changed", result.stderr)
                self.assertFalse(
                    any(row[0] == "body" for rows in events.values() for row in rows)
                )
                self.assertFalse(
                    any(
                        row[0] == "UNGUARDED CONNECT"
                        for rows in events.values()
                        for row in rows
                    )
                )

    def test_production_declaration_pins_exact_source_inventory_without_importing_it(
        self,
    ):
        source = COLLECTOR.read_text(encoding="utf-8")
        version, rows = ast.literal_eval(declaration(source).value)
        self.assertEqual(version, "independent-socket-methods-v1")
        self.assertEqual(len(rows), 1)
        module, ids, dependencies = rows[0]
        self.assertEqual(module, "test_quarantine_checkpoint")
        target = ROOT / "tests" / (module + ".py")
        tree = ast.parse(target.read_text(encoding="utf-8"))
        actual = sorted(
            f"{module}.{cls.name}.{method.name}"
            for cls in tree.body
            if isinstance(cls, ast.ClassDef)
            and any(
                isinstance(base, ast.Attribute) and base.attr == "TestCase"
                for base in cls.bases
            )
            for method in cls.body
            if isinstance(method, ast.FunctionDef) and method.name.startswith("test_")
        )
        self.assertEqual(list(ids), actual)
        self.assertEqual(len(ids), 7)
        self.assertEqual(
            hashlib.sha256(
                target.read_text(encoding="utf-8").encode("utf-8")
            ).hexdigest(),
            "7bb95ea52f82b843049aea99dc319599a5782974bbde90f7f55ec4b28dc379a9",
        )
        for name, digest in dependencies:
            with self.subTest(source=name):
                self.assertEqual(
                    hashlib.sha256(
                        (target.parent / name)
                        .read_text(encoding="utf-8")
                        .encode("utf-8")
                    ).hexdigest(),
                    digest,
                )
        pending, inputs = [module + ".py"], set()
        while pending:
            name = pending.pop()
            if name in inputs:
                continue
            inputs.add(name)
            source = ast.parse((target.parent / name).read_text(encoding="utf-8"))
            for node in ast.walk(source):
                imports = (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module]
                    if isinstance(node, ast.ImportFrom) and node.module
                    else []
                )
                for imported in imports:
                    filename = imported.split(".")[0] + ".py"
                    if (target.parent / filename).is_file():
                        pending.append(filename)
        self.assertEqual({name for name, _digest in dependencies}, inputs)


if __name__ == "__main__":
    unittest.main()
