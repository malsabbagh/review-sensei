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
