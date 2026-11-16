"""Reviewed installed hooks preserve original objects and fail closed on drift."""

import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[1] / "scripts/run_installed_test_suites.py"
DECLARATION = "INSTALLED_SUITE_HOOK_CONTRACT = ('reproducible-module-local-v1', ())\n"


class InstalledLocalHookTests(unittest.TestCase):
    def execute(self, sources, *, reviewed=(), split=()):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            suite = root / "suite"
            suite.mkdir()
            for name, source in sources.items():
                (suite / name).write_text(textwrap.dedent(source), encoding="utf-8")
            report = root / "report.json"
            command = [
                sys.executable,
                "-I",
                str(RUNNER),
                "--start-directory",
                str(suite),
                "--workers",
                "4",
                "--report",
                str(report),
            ]
            for module in reviewed:
                command.extend(("--reviewed-local-hook", module))
            for module in split:
                command.extend(("--split-module", module))
            result = subprocess.run(
                command, cwd=root, capture_output=True, text=True, timeout=30
            )
            data = json.loads(report.read_text()) if report.exists() else None
            count = (
                (suite / "count").read_text() if (suite / "count").exists() else None
            )
            return result, data, count

    def test_explicit_local_hook_preserves_configured_cases(self):
        source = (
            DECLARATION
            + """\
import unittest
class Case(unittest.TestCase):
    def test_configured(self): self.assertEqual(self.configuration, {'values': [42]})
def load_tests(loader, tests, pattern):
    case = Case('test_configured'); case.configuration = {'values': [42]}
    return unittest.TestSuite([case])
"""
        )
        result, data, _ = self.execute(
            {
                "test_a.py": source,
                "test_b.py": "import unittest\nclass Case(unittest.TestCase):\n    def test_b(self): pass\n",
            },
            reviewed=("test_a",),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(data["hook_mode"], "reviewed-local")
        self.assertEqual(len(data["workers"]), 2)
        self.assertEqual(sum(w["started"] for w in data["workers"]), 2)
        self.assertTrue(data["complete"])

    def test_custom_network_wrapper_and_teardown_remain_whole(self):
        source = """\
import socket, unittest
from unittest.mock import patch
INSTALLED_SUITE_HOOK_CONTRACT = ('reproducible-module-local-v1', ('Network',))
ready = False
active = False
class Network(unittest.TestSuite):
    def run(self, result, debug=False):
        global active
        with patch('socket.socket.connect', side_effect=RuntimeError('blocked')):
            active = True
            try: return super().run(result, debug)
            finally: active = False
def setUpModule():
    global ready
    ready = True
def tearDownModule():
    assert ready
    assert not active, 'outer root must finish teardown after wrapper returns'
class Case(unittest.TestCase):
    def test_one(self):
        self.assertTrue(ready and active)
        with socket.socket() as sock:
            with self.assertRaisesRegex(RuntimeError, 'blocked'):
                sock.connect(('127.0.0.1', 1))
    def test_two(self): self.assertTrue(ready and active)
def load_tests(loader, tests, pattern): return Network(tests)
"""
        result, data, _ = self.execute(
            {
                "test_a.py": source,
                "test_b.py": "import unittest\nclass Other(unittest.TestCase):\n    def test_other(self): pass\n",
            },
            reviewed=("test_a",),
            split=("test_a.py",),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(data["units"], 2)
        self.assertEqual(data["hook_mode"], "reviewed-local")
        self.assertEqual(sum(w["started"] for w in data["workers"]), 3)

    def test_unknown_stateful_hook_stays_one_discovery(self):
        source = """\
import pathlib, unittest
marker = pathlib.Path(__file__).with_name('count')
class Case(unittest.TestCase):
    def test_once(self): self.assertEqual(marker.read_text(), '1')
def load_tests(loader, tests, pattern):
    marker.write_text(str(int(marker.read_text()) + 1) if marker.exists() else '1')
    return tests
"""
        for reviewed in ((), ("test_a",)):
            with self.subTest(reviewed=reviewed):
                result, data, count = self.execute(
                    {"test_a.py": source}, reviewed=reviewed
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(count, "1")
                self.assertEqual(data["hook_mode"], "original-root")

    def test_unsupported_state_and_global_loader_keep_original_root(self):
        for setup, hook in (
            (
                "",
                "case=Case('test_ok'); case.opaque=object(); return unittest.TestSuite([case])",
            ),
            (
                "class Root(unittest.TestSuite): pass\n",
                "loader.suiteClass=Root; return tests",
            ),
        ):
            source = (
                DECLARATION
                + "import unittest\n"
                + setup
                + "class Case(unittest.TestCase):\n    def test_ok(self): pass\ndef load_tests(loader, tests, pattern):\n    "
                + hook
                + "\n"
            )
            with self.subTest(hook=hook):
                result, data, _ = self.execute(
                    {"test_a.py": source}, reviewed=("test_a",)
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(data["hook_mode"], "original-root")
                self.assertIsNotNone(data["fallback_reason"])

    def test_shared_fixture_scopes_keep_original_root(self):
        source = (
            DECLARATION
            + "import unittest, helper\ndef load_tests(loader, tests, pattern):\n    return loader.loadTestsFromTestCase(helper.Case)\n"
        )
        result, data, _ = self.execute(
            {
                "test_a.py": source,
                "test_b.py": source,
                "helper.py": "import unittest\nclass Case(unittest.TestCase):\n    def test_one(self): pass\n",
            },
            reviewed=("test_a", "test_b"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(data["hook_mode"], "original-root")
        self.assertIn("share a fixture", data["fallback_reason"])

    def test_unreviewed_wrapper_and_shared_mutable_state_fall_back(self):
        for setup, hook in (
            ("class Wrapper(unittest.TestSuite): pass\n", "return Wrapper(tests)"),
            (
                "",
                "a=Case('test_ok'); b=Case('test_ok'); a.shared=b.shared=[]; return unittest.TestSuite([a,b])",
            ),
            (
                "",
                "a=Case('test_ok'); a.cycle=[]; a.cycle.append(a.cycle); return unittest.TestSuite([a])",
            ),
        ):
            source = (
                DECLARATION
                + "import unittest\n"
                + setup
                + "class Case(unittest.TestCase):\n    def test_ok(self): pass\n"
                + "def load_tests(loader, tests, pattern):\n    "
                + hook
                + "\n"
            )
            with self.subTest(hook=hook):
                result, data, _ = self.execute(
                    {"test_a.py": source}, reviewed=("test_a",)
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(data["hook_mode"], "original-root")
                self.assertIsNotNone(data["fallback_reason"])

    def test_slot_backed_configuration_keeps_original_root(self):
        for declaration in ("('directory',)", "'directory'"):
            source = (
                DECLARATION
                + f"""\
import os, unittest
class Slotted:
    __slots__ = {declaration}
class Case(Slotted, unittest.TestCase):
    def test_original(self):
        self.assertEqual(self.directory, os.getcwd())
def load_tests(loader, tests, pattern):
    case=Case('test_original'); case.directory=os.getcwd()
    return unittest.TestSuite([case])
"""
            )
            with self.subTest(declaration=declaration):
                result, data, _ = self.execute(
                    {"test_a.py": source}, reviewed=("test_a",)
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(data["hook_mode"], "original-root")
                self.assertIn("slot-backed", data["fallback_reason"])
                self.assertEqual(len(data["workers"]), 1)

    def test_custom_inherited_slot_declaration_never_calls_equality(self):
        source = (
            DECLARATION
            + """\
import os, unittest
class Declaration:
    def __iter__(self): return iter(('directory',))
    def __eq__(self, other): raise AssertionError('SLOT EQUALITY MUST NOT RUN')
class Slotted:
    __slots__ = Declaration()
class Case(Slotted, unittest.TestCase):
    def test_original(self): self.assertEqual(self.directory, os.getcwd())
def load_tests(loader, tests, pattern):
    case=Case('test_original'); case.directory=os.getcwd()
    return unittest.TestSuite([case])
"""
        )
        result, data, _ = self.execute({"test_a.py": source}, reviewed=("test_a",))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(data["hook_mode"], "original-root")
        self.assertIn("slot-backed", data["fallback_reason"])
        self.assertNotIn("SLOT EQUALITY MUST NOT RUN", result.stderr)

    def test_worker_configuration_drift_fails_before_execution(self):
        source = (
            DECLARATION
            + """\
import os, unittest
class Case(unittest.TestCase):
    def test_never(self): raise AssertionError('BODY MUST NOT RUN')
def load_tests(loader, tests, pattern):
    case=Case('test_never'); case.directory=os.getcwd()
    return unittest.TestSuite([case])
"""
        )
        result, data, _ = self.execute({"test_a.py": source}, reviewed=("test_a",))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("configuration differs", result.stderr)
        self.assertNotIn("BODY MUST NOT RUN", result.stderr)
        self.assertFalse(data["complete"])


class QuarantineNetworkGuardDispatchTests(unittest.TestCase):
    """Run the real collector against synthetic modules, never quarantined work."""

    def execute(self, *, mode="success", workers=1, repeat=False, boundary="inside"):
        contract = (
            Path(__file__).resolve().parent
            / "dist_safe"
            / "test_quarantine_contract.py"
        ).read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            suite = root / "tests"
            lane = suite / "dist_safe"
            lane.mkdir(parents=True)
            (lane / "test_quarantine_contract.py").write_text(
                contract, encoding="utf-8"
            )
            (suite / "packaging_guard.py").write_text(
                "from pathlib import Path\n"
                "def assert_distribution_import():\n"
                "    with Path(__file__).with_name('import-guards').open('a') as stream:\n"
                "        stream.write('core\\n')\n"
                + (
                    "    raise ImportError('synthetic core boundary refused')\n"
                    if boundary == "core-refused"
                    else ""
                )
                + "def is_disarmed(): return False\n",
                encoding="utf-8",
            )
            (suite / "review_sensei_github.py").write_text(
                "import sys\nfrom pathlib import Path\n"
                + (
                    "__file__ = str(Path(sys.prefix) / 'synthetic-installed-hosted.py')\n"
                    if boundary != "hosted-outside"
                    else ""
                ),
                encoding="utf-8",
            )
            (suite / "guard_fixture.py").write_text(
                textwrap.dedent("""\
                    import socket
                    from pathlib import Path
                    EVENTS = Path(__file__).with_name('events')
                    def record(label):
                        with EVENTS.open('a') as stream: stream.write(label + '\\n')
                    def unsafe_connect(*args, **kwargs):
                        record('UNGUARDED CONNECT')
                        raise RuntimeError('synthetic unguarded socket; no real network')
                    socket.socket.connect = unsafe_connect
                    def blocked(label):
                        try:
                            socket.socket.connect(None, ('example.invalid', 1))
                        except AssertionError as error:
                            assert str(error) == 'live network forbidden'
                        else:
                            raise AssertionError('socket protection absent')
                        record(label)
                    def restored():
                        assert socket.socket.connect is unsafe_connect
                        record('restored')
                    """),
                encoding="utf-8",
            )
            for module, class_names in (("a", ("First", "Second")), ("b", ("Only",))):
                selected_mode = mode if module == "a" else "success"
                source = (
                    "import unittest\nfrom guard_fixture import blocked\n"
                    f"MODE = {selected_mode!r}\n"
                    "def cleanup(label, fail=False):\n"
                    "    blocked(label)\n"
                    "    if fail: raise RuntimeError('synthetic cleanup error')\n"
                    "def setUpModule():\n"
                    f"    blocked('{module}.setup')\n"
                    f"    unittest.addModuleCleanup(cleanup, '{module}.cleanup-first', MODE == 'cleanup-error')\n"
                    f"    unittest.addModuleCleanup(cleanup, '{module}.cleanup-second')\n"
                    "    if MODE == 'setup-skip': raise unittest.SkipTest('synthetic module skip')\n"
                    "    if MODE == 'setup-error': raise RuntimeError('synthetic setup error')\n"
                    "def tearDownModule():\n"
                    f"    blocked('{module}.teardown')\n"
                    "    if MODE == 'teardown-error': raise RuntimeError('synthetic teardown error')\n"
                )
                for cls in class_names:
                    label = module + "." + cls
                    source += (
                        f"class {cls}(unittest.TestCase):\n"
                        "    @classmethod\n"
                        "    def setUpClass(cls):\n"
                        f"        blocked('{label}.setup-class')\n"
                        f"        cls.addClassCleanup(blocked, '{label}.class-cleanup')\n"
                        "    @classmethod\n"
                        f"    def tearDownClass(cls): blocked('{label}.teardown-class')\n"
                        f"    def setUp(self): blocked('{label}.setup-test')\n"
                        f"    def tearDown(self): blocked('{label}.teardown-test')\n"
                        "    def test_guard(self):\n"
                        f"        blocked('{label}.test')\n"
                        "        if MODE == 'test-error': raise RuntimeError('synthetic test error')\n"
                    )
                (suite / f"test_quarantine_{module}.py").write_text(
                    source, encoding="utf-8"
                )
            # These existing publication-proof exclusions must stay untouched.
            for name in ("check_capacity", "journey", "publication"):
                (suite / f"test_quarantine_v3_{name}.py").write_text(
                    "raise AssertionError('excluded publication proof imported')\n",
                    encoding="utf-8",
                )
            (lane / "test_z_restore.py").write_text(
                "import unittest\nfrom guard_fixture import restored\n"
                "class Restore(unittest.TestCase):\n"
                "    def test_restored(self): restored()\n",
                encoding="utf-8",
            )
            report = root / "report.json"
            command = [sys.executable, "-I"]
            if repeat:
                # Discover twice without running any body; the subsequent real
                # runner must still see identical objects' fixture bindings.
                driver = textwrap.dedent("""\
                    import importlib.util, json, sys
                    from pathlib import Path
                    runner_path, directory, changed = sys.argv[1:4]
                    spec = importlib.util.spec_from_file_location('synthetic_runner', runner_path)
                    runner = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(runner)
                    first = runner.discover(Path(directory), 'test*.py')
                    collector = sys.modules['test_quarantine_contract']
                    names = ('test_quarantine_a', 'test_quarantine_b')
                    setups = {name: vars(sys.modules[name])['setUpModule'] for name in names}
                    if changed == 'changed':
                        sys.modules[names[0]].setUpModule = lambda: None
                    second = runner.discover(Path(directory), 'test*.py')
                    assert first[0] == second[0] and first[1] == second[1]
                    assert all(vars(sys.modules[name])['setUpModule'] is setup for name, setup in setups.items())
                    units = runner.build_units(*second, ['test_quarantine_contract.py'])
                    assert len(units) == 3
                    Path(directory).parent.joinpath('repeat-proof').write_text(json.dumps({'units': units, 'descriptions': second[1]}))
                    sys.argv = [runner_path, *sys.argv[4:]]
                    raise SystemExit(runner.main())
                    """)
                command.extend(
                    (
                        "-c",
                        driver,
                        str(RUNNER),
                        str(lane),
                        "changed" if mode == "changed-setup" else "unchanged",
                    )
                )
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
                    "--reviewed-local-hook",
                    "test_quarantine_contract",
                    "--report",
                    str(report),
                )
            )
            result = subprocess.run(
                command, cwd=root, capture_output=True, text=True, timeout=30
            )
            data = json.loads(report.read_text()) if report.exists() else None
            events = (
                (suite / "events").read_text().splitlines()
                if (suite / "events").exists()
                else []
            )
            proof = (
                json.loads((suite / "repeat-proof").read_text())
                if (suite / "repeat-proof").exists()
                else None
            )
            guards = (
                (suite / "import-guards").read_text().splitlines()
                if (suite / "import-guards").exists()
                else []
            )
            return result, data, events, proof, guards

    def assert_complete(self, result, data, events, *, exit_code=0):
        self.assertEqual(result.returncode, exit_code, result.stdout + result.stderr)
        self.assertEqual(data["hook_mode"], "reviewed-local")
        self.assertEqual(data["discovered"], 4)
        self.assertEqual(data["units"], 3)
        self.assertEqual(sum(worker["assigned"] for worker in data["workers"]), 4)
        self.assertTrue(all(worker["execution_complete"] for worker in data["workers"]))
        self.assertNotIn("UNGUARDED CONNECT", events)
        self.assertEqual(events.count("restored"), 1)

    def test_actual_imported_fixture_lifecycle_is_guarded_and_restored(self):
        result, data, events, _, guards = self.execute()
        self.assert_complete(result, data, events)
        expected = []
        for module, classes in (("a", ("First", "Second")), ("b", ("Only",))):
            expected.append(module + ".setup")
            for cls in classes:
                expected.extend(
                    f"{module}.{cls}.{stage}"
                    for stage in (
                        "setup-class",
                        "setup-test",
                        "test",
                        "teardown-test",
                        "teardown-class",
                        "class-cleanup",
                    )
                )
            expected.extend(
                f"{module}.{stage}"
                for stage in ("teardown", "cleanup-second", "cleanup-first")
            )
        self.assertEqual(events, [*expected, "restored"])
        self.assertTrue(guards)

    def test_distinct_modules_remain_parallel_and_same_module_classes_stay_whole(self):
        result, data, events, _, _ = self.execute(workers=4)
        self.assert_complete(result, data, events)
        self.assertEqual(len(data["workers"]), 3)
        self.assertEqual(
            sorted(worker["assigned"] for worker in data["workers"]), [1, 1, 2]
        )
        self.assertEqual(events.count("a.setup"), 1)
        self.assertEqual(events.count("b.setup"), 1)

    def test_repeated_discovery_preserves_guard_binding_and_exact_suite_shape(self):
        result, data, events, proof, _ = self.execute(repeat=True)
        self.assert_complete(result, data, events)
        self.assertEqual(len(proof["units"]), 3)
        self.assertEqual(events.count("a.setup"), 1)
        self.assertEqual(events.count("b.setup"), 1)

    def test_setup_skip_and_error_restore_after_all_guarded_cleanups(self):
        for mode in ("setup-skip", "setup-error"):
            with self.subTest(mode=mode):
                result, data, events, _, _ = self.execute(mode=mode)
                self.assertEqual(
                    events[:3], ["a.setup", "a.cleanup-second", "a.cleanup-first"]
                )
                self.assertNotIn("a.teardown", events)
                self.assertFalse(
                    any(
                        event.startswith("a.First.") or event.startswith("a.Second.")
                        for event in events
                    )
                )
                self.assertNotIn("UNGUARDED CONNECT", events)
                self.assertEqual(events[-1], "restored")
                if mode == "setup-skip":
                    self.assert_complete(result, data, events)
                    self.assertEqual(
                        sum(worker["fixture_skipped"] for worker in data["workers"]), 2
                    )
                    self.assertEqual(
                        sum(worker["started"] for worker in data["workers"]), 2
                    )
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(data["complete"])
                    self.assertIn(
                        "synthetic setup error", result.stdout + result.stderr
                    )

    def test_test_teardown_and_cleanup_errors_preserve_protection_and_restoration(self):
        for mode in ("test-error", "teardown-error", "cleanup-error"):
            with self.subTest(mode=mode):
                result, data, events, _, _ = self.execute(mode=mode)
                self.assert_complete(result, data, events, exit_code=1)
                self.assertIn("a.teardown", events)
                self.assertIn("a.cleanup-second", events)
                self.assertIn("a.cleanup-first", events)
                self.assertEqual(events[-1], "restored")
                self.assertIn(
                    "synthetic " + mode.replace("-", " "), result.stdout + result.stderr
                )

    def test_changed_guard_binding_fails_discovery_before_any_test(self):
        result, data, events, proof, _ = self.execute(mode="changed-setup", repeat=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(data)
        self.assertIsNone(proof)
        self.assertEqual(events, [])

    def test_original_import_boundaries_still_refuse_before_any_test(self):
        for boundary in ("core-refused", "hosted-outside"):
            with self.subTest(boundary=boundary):
                result, data, events, _, guards = self.execute(boundary=boundary)
                self.assertNotEqual(result.returncode, 0)
                self.assertIsNone(data)
                self.assertEqual(events, [])
                self.assertTrue(guards)
