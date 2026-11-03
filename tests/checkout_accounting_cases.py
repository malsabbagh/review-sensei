"""Real runner regressions for fixture dispatch and one-time discovery hooks."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CASES = [
    (
        "fixture-reentry-probe.py",
        'import unittest\n\nclass Shared(unittest.TestCase):\n    setup_calls = 0\n\n    @classmethod\n    def setUpClass(cls):\n        cls.setup_calls += 1\n        if cls.setup_calls == 2:\n            raise unittest.SkipTest("second setup")\n\n    def id(self):\n        alias = "AAlias" if self._testMethodName == "test_a" else "CAlias"\n        return f"test_example.{alias}.{self._testMethodName}"\n\n    def run(self, result):\n        if self._testMethodName == "test_a":\n            return result  # Deliberately omit an assigned test before fixture re-entry.\n        return super().run(result)\n\n    def test_a(self): pass\n    def test_c(self): pass\n\nAAlias = CAlias = Shared\n\nclass Between(unittest.TestCase):\n    def test_b(self): pass\n\ndef load_tests(loader, tests, pattern):\n    return unittest.TestSuite([Shared("test_a"), Between("test_b"), Shared("test_c")])\n',
        False,
    ),
    (
        "fixture-module-reentry-probe.py",
        'import unittest\nfrom other_fixture import Between\n\nsetup_calls = 0\ndef setUpModule():\n    global setup_calls\n    setup_calls += 1\n    if setup_calls == 2:\n        raise unittest.SkipTest("second module setup")\n\nclass Shared(unittest.TestCase):\n    def id(self):\n        alias = "AAlias" if self._testMethodName == "test_a" else "CAlias"\n        return f"test_example.{alias}.{self._testMethodName}"\n    def run(self, result):\n        if self._testMethodName == "test_a":\n            return result\n        return super().run(result)\n    def test_a(self): pass\n    def test_c(self): pass\n\nAAlias = CAlias = Shared\ndef load_tests(loader, tests, pattern):\n    return unittest.TestSuite([Shared("test_a"), Between("test_b"), Shared("test_c")])\n',
        False,
    ),
    (
        "plain-suite-mutation-probe.py",
        'import unittest\n\nsuite_ref = None\nclass First(unittest.TestCase):\n    def id(self): return "test_example.aa_factory"\n    def test_a(self):\n        self.assertNotIn("_handleClassSetUp", vars(suite_ref))\n        self.assertNotIn("_handleModuleFixture", vars(suite_ref))\nclass Inspect(unittest.TestCase):\n    def id(self): return "test_example.zz_check"\n    def test_check(self):\n        self.assertEqual(list(suite_ref), [None], "the original suite was not consumed")\ndef aa_factory():\n    global suite_ref\n    suite_ref = unittest.TestSuite([First("test_a")])\n    return suite_ref\ndef zz_check(): return Inspect("test_check")\ndef load_tests(loader, tests, pattern):\n    return unittest.TestSuite([aa_factory(), Inspect("test_check")])\n',
        True,
    ),
    (
        "custom-suite-skip-probe.py",
        'import unittest\n\nclass Wrapper(unittest.TestSuite):\n    pass\nclass Skipped(unittest.TestCase):\n    @classmethod\n    def setUpClass(cls): raise unittest.SkipTest("optional fixture unavailable")\n    def id(self): return "test_example.aa_factory"\n    def test_a(self): pass\ndef aa_factory(): return Wrapper([Skipped("test_a")])\ndef load_tests(loader, tests, pattern): return aa_factory()\n',
        True,
    ),
    (
        "fixture-slot-mutation-probe.py",
        'import unittest\n\nsuite_ref = None\nfirst_case = None\nclass Shared(unittest.TestCase):\n    setup_calls = 0\n    @classmethod\n    def setUpClass(cls):\n        cls.setup_calls += 1\n        if cls.setup_calls == 2: raise unittest.SkipTest("second setup")\n    def id(self):\n        return "test_example.aa_factory" if self._testMethodName == "test_a" else "test_example.cc_factory"\n    def run(self, result):\n        if self._testMethodName == "test_a": return result\n        return super().run(result)\n    def test_a(self): pass\n    def test_c(self): pass\nclass Between(unittest.TestCase):\n    def id(self): return "test_example.bb_factory"\n    def test_b(self): suite_ref._tests[0] = first_case\ndef aa_factory():\n    global suite_ref, first_case\n    first_case = Shared("test_a")\n    suite_ref = unittest.TestSuite([first_case])\n    return suite_ref\ndef bb_factory(): return Between("test_b")\ndef cc_factory(): return Shared("test_c")\ndef load_tests(loader, tests, pattern):\n    return unittest.TestSuite([aa_factory(), bb_factory(), cc_factory()])\n',
        False,
    ),
    (
        "hook state and one invocation",
        'import pathlib, unittest\nmarker = pathlib.Path(__file__).with_name("count")\nclass Sample(unittest.TestCase):\n    def test_state(self):\n        self.assertEqual(self.configured, 42)\n        self.assertEqual(marker.read_text(), "1")\ndef load_tests(loader, tests, pattern):\n    count = int(marker.read_text()) if marker.exists() else 0\n    marker.write_text(str(count+1))\n    case=Sample("test_state")\n    case.configured=42\n    return unittest.TestSuite([case])\n',
        True,
    ),
]

CASES.append(
    (
        "original root teardown",
        'import unittest\ncleanup_done = False\ndef tearDownModule():\n    global cleanup_done\n    cleanup_done = True\nclass Root(unittest.TestSuite):\n    def run(self, result, debug=False):\n        returned = super().run(result, debug)\n        assert cleanup_done, "root returned before module fixture cleanup"\n        return returned\nclass Sample(unittest.TestCase):\n    def test_case(self): pass\ndef load_tests(loader, tests, pattern):\n    loader.suiteClass = Root\n    return tests\n',
        True,
    )
)

CASES.extend(
    [
        (
            "two genuine same-name skips",
            'import unittest\nclass A(unittest.TestCase):\n    @classmethod\n    def setUpClass(cls): raise unittest.SkipTest("first")\n    def test_a(self): pass\nclass B(unittest.TestCase):\n    @classmethod\n    def setUpClass(cls): raise unittest.SkipTest("second")\n    def test_b(self): pass\nA.__qualname__=B.__qualname__="Same"\ndef load_tests(loader, tests, pattern): return unittest.TestSuite([A("test_a"),B("test_b")])\n',
            True,
        ),
        (
            "stale module episode",
            'import unittest, types, sys\nma=types.ModuleType("fixture_a")\nmb=types.ModuleType("fixture_b")\ndef setup(): raise unittest.SkipTest("actual skip")\nma.setUpModule=setup\nsys.modules["fixture_a"]=ma\nsys.modules["fixture_b"]=mb\nclass A(unittest.TestCase):\n    def test_a(self): pass\nclass B(unittest.TestCase):\n    def test_b(self): self.fail("must not be counted")\nA.__module__="fixture_a"\nB.__module__="fixture_b"\nclass Broken(unittest.TestSuite):\n    def _handleModuleFixture(self, test, result): result._moduleSetUpFailed=True\ndef load_tests(loader, tests, pattern): return unittest.TestSuite([unittest.TestSuite([A("test_a")]),Broken([B("test_b")])])\n',
            False,
        ),
    ]
)


CASES.extend(
    [
        (
            "valid-fixture-reentry-probe.py",
            'import unittest\n\nclass Shared(unittest.TestCase):\n    setup_calls = 0\n\n    @classmethod\n    def setUpClass(cls):\n        cls.setup_calls += 1\n        if cls.setup_calls == 2:\n            raise unittest.SkipTest("second setup")\n\n    def id(self):\n        alias = "AAlias" if self._testMethodName == "test_a" else "CAlias"\n        return f"test_example.{alias}.{self._testMethodName}"\n\n    def run(self, result):\n        if self._testMethodName == "test_a":\n            return super().run(result)\n        return super().run(result)\n\n    def test_a(self): pass\n    def test_c(self): pass\n\nAAlias = CAlias = Shared\n\nclass Between(unittest.TestCase):\n    def test_b(self): pass\n\ndef load_tests(loader, tests, pattern):\n    return unittest.TestSuite([Shared("test_a"), Between("test_b"), Shared("test_c")])\n',
            True,
        ),
        (
            "valid-fixture-module-reentry-probe.py",
            'import unittest\nfrom other_fixture import Between\n\nsetup_calls = 0\ndef setUpModule():\n    global setup_calls\n    setup_calls += 1\n    if setup_calls == 2:\n        raise unittest.SkipTest("second module setup")\n\nclass Shared(unittest.TestCase):\n    def id(self):\n        alias = "AAlias" if self._testMethodName == "test_a" else "CAlias"\n        return f"test_example.{alias}.{self._testMethodName}"\n    def run(self, result):\n        if self._testMethodName == "test_a":\n            return super().run(result)\n        return super().run(result)\n    def test_a(self): pass\n    def test_c(self): pass\n\nAAlias = CAlias = Shared\ndef load_tests(loader, tests, pattern):\n    return unittest.TestSuite([Shared("test_a"), Between("test_b"), Shared("test_c")])\n',
            True,
        ),
    ]
)


CASES.extend(
    [
        (
            "forwarded class fixture skip",
            'import unittest\nclass Forwarding(unittest.TestSuite):\n    def _handleModuleFixture(self, test, result):\n        return super()._handleModuleFixture(test, result)\n    def _handleClassSetUp(self, test, result):\n        return super()._handleClassSetUp(test, result)\nclass Skipped(unittest.TestCase):\n    @classmethod\n    def setUpClass(cls): raise unittest.SkipTest("actual forwarded class skip")\n    def test_a(self): pass\n    def test_b(self): pass\ndef load_tests(loader, tests, pattern):\n    return Forwarding([Skipped("test_a"), Skipped("test_b")])\n',
            True,
        ),
        (
            "forwarded module fixture skip",
            'import unittest\nclass Forwarding(unittest.TestSuite):\n    def _handleModuleFixture(self, test, result):\n        return super()._handleModuleFixture(test, result)\n    def _handleClassSetUp(self, test, result):\n        return super()._handleClassSetUp(test, result)\ndef setUpModule(): raise unittest.SkipTest("actual forwarded module skip")\nclass Skipped(unittest.TestCase):\n    def test_a(self): pass\n    def test_b(self): pass\ndef load_tests(loader, tests, pattern):\n    return Forwarding([Skipped("test_a"), Skipped("test_b")])\n',
            True,
        ),
    ]
)


CASES.extend(
    [
        (
            "manual fixture call after dispatch",
            "import unittest\nsuite_ref = None\nclass Omitted(unittest.TestCase):\n    calls = 0\n    @classmethod\n    def setUpClass(cls):\n        cls.calls += 1\n        if cls.calls == 2: raise unittest.SkipTest('manual later setup')\n    def run(self, result):\n        result._previousTestClass = None\n        suite_ref._handleClassSetUp(self, result)\n        return result\n    def test_body(self): self.fail('must execute')\ndef load_tests(loader, tests, pattern):\n    global suite_ref\n    suite_ref = unittest.TestSuite([Omitted('test_body')])\n    return suite_ref\n",
            False,
        ),
        (
            "repeated super recovers then omits",
            "import unittest\nclass Omitted(unittest.TestCase):\n    calls = 0\n    @classmethod\n    def setUpClass(cls):\n        cls.calls += 1\n        if cls.calls == 1: raise unittest.SkipTest('first setup only')\n    def run(self, result): return result\n    def test_body(self): self.fail('must execute')\nclass RetryingSuite(unittest.TestSuite):\n    def _handleClassSetUp(self, test, result):\n        super()._handleClassSetUp(test, result)\n        return super()._handleClassSetUp(test, result)\ndef load_tests(loader, tests, pattern):\n    return RetryingSuite([Omitted('test_body')])\n",
            False,
        ),
        (
            "changed allocated case type",
            "import unittest\nclass Replacement(unittest.TestCase):\n    def run(self, result): return result\n    def test_body(self): self.fail('must execute')\nclass Original(unittest.TestCase):\n    @classmethod\n    def setUpClass(cls): raise unittest.SkipTest('original fixture')\n    def test_body(self): self.fail('must execute')\nclass ChangingSuite(unittest.TestSuite):\n    def _handleClassSetUp(self, test, result):\n        super()._handleClassSetUp(test, result)\n        test.__class__ = Replacement\ndef load_tests(loader, tests, pattern):\n    return ChangingSuite([Original('test_body')])\n",
            False,
        ),
    ]
)


class CheckoutAccountingChecks:
    def _check_original_suite_and_exact_accounting(self):
        runner = Path(__file__).resolve().parents[1] / "scripts" / SCRIPT
        for label, source, success in CASES:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "test_example.py").write_text(source, encoding="utf-8")
                if label.endswith("fixture-module-reentry-probe.py"):
                    (root / "other_fixture.py").write_text(OTHER, encoding="utf-8")
                report = root / "report.json"
                completed = subprocess.run(
                    [
                        sys.executable,
                        *ISOLATED,
                        str(runner),
                        "--start-directory",
                        str(root),
                        "--workers",
                        "2",
                        "--report",
                        str(report),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(completed.returncode == 0, success, completed.stderr)
                data = json.loads(report.read_text())
                self.assertEqual(data["complete"], success)
                if "reentry" in label:
                    self.assertEqual(
                        sum(w["fixture_skipped"] for w in data["workers"]), 1
                    )

    def _check_observer_restores_existing_profiler_and_preserves_trace(self):
        import importlib.util
        import io
        from collections import Counter

        runner = Path(__file__).resolve().parents[1] / "scripts" / SCRIPT
        spec = importlib.util.spec_from_file_location("runner_under_test", runner)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        class Skipped(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                raise unittest.SkipTest("fixture unavailable")

            def test_case(self):
                self.fail("unreachable")

        class Ordinary(unittest.TestCase):
            def test_profile_is_restored(self):
                self.assertIs(sys.getprofile(), prior)

        case = Skipped("test_case")
        ordinary = Ordinary("test_profile_is_restored")
        tracker = module.FixtureDispatchTracker(
            [case, ordinary], lambda test: test.id()
        )

        class Result(unittest.TextTestResult):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                tracker.result = self

            def addSkip(self, test, reason):
                tracker.note_skip()
                super().addSkip(test, reason)

        events = []

        def prior(frame, event, arg):
            if frame.f_code is Skipped.setUpClass.__func__.__code__:
                events.append(event)

        original = sys.getprofile()
        trace = sys.gettrace()
        try:
            sys.setprofile(prior)
            with tracker:
                result = unittest.TextTestRunner(
                    stream=io.StringIO(), resultclass=Result
                ).run(unittest.TestSuite([case, ordinary]))
            self.assertIs(sys.getprofile(), prior)
            self.assertIs(sys.gettrace(), trace)
            self.assertTrue(result.wasSuccessful())
            self.assertEqual(tracker.skipped, Counter({case.id(): 1}))
            self.assertIn("call", events)
            with self.assertRaisesRegex(RuntimeError, "stop"):
                with module.FixtureDispatchTracker([], lambda test: test.id()):
                    raise RuntimeError("stop")
            self.assertIs(sys.getprofile(), prior)
            # The inactive observer must not undo intentional profiler changes
            # made by the suite it is observing.
            with module.FixtureDispatchTracker([], lambda test: test.id()):
                sys.setprofile(None)
            self.assertIsNone(sys.getprofile())

            class ChangesProfiler(unittest.TestCase):
                def test_change(self):
                    sys.setprofile(None)

            class AfterSkip(unittest.TestCase):
                def test_current_profiler(self):
                    self.assertIsNone(sys.getprofile())

            # A later genuine skip must restore the profiler current when that
            # episode begins, rather than the one captured on context entry.
            sys.setprofile(prior)
            cases = [
                ChangesProfiler("test_change"),
                Skipped("test_case"),
                AfterSkip("test_current_profiler"),
            ]
            tracker = module.FixtureDispatchTracker(cases, lambda test: test.id())
            with tracker:
                result = unittest.TextTestRunner(
                    stream=io.StringIO(), resultclass=Result
                ).run(unittest.TestSuite(cases))
            self.assertTrue(result.wasSuccessful())
            self.assertIsNone(sys.getprofile())
        finally:
            sys.setprofile(original)

    def _check_early_success_exit_cannot_waive_inventory(self):
        runner = Path(__file__).resolve().parents[1] / "scripts" / SCRIPT
        for exit_code in ("raise SystemExit(0)", "os._exit(0)"):
            with (
                self.subTest(exit_code=exit_code),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                (root / "test_exit.py").write_text(
                    "import os, unittest\nclass Suite(unittest.TestSuite):\n    def run(self, result, debug=False): "
                    + exit_code
                    + "\ndef load_tests(loader, tests, pattern): return Suite(tests)\nclass Case(unittest.TestCase):\n    def test_case(self): self.fail('unexecuted')\n",
                    encoding="utf-8",
                )
                completed = subprocess.run(
                    [
                        sys.executable,
                        *ISOLATED,
                        str(runner),
                        "--start-directory",
                        str(root),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertNotEqual(completed.returncode, 0, completed.stderr)
                self.assertIn("without its final completion receipt", completed.stderr)

    def _check_hook_fallback_keeps_import_and_branch_coverage(self):
        import os

        from coverage import CoverageData

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "review_sensei"
            package.mkdir()
            (package / "__init__.py").write_text("")
            source = package / "logic.py"
            source.write_text(
                "def choose(flag):\n    if flag:\n        return 1\n    return 0\n",
                encoding="utf-8",
            )
            (root / "test_hook.py").write_text(
                "import unittest\nfrom review_sensei.logic import choose\ndef load_tests(loader, tests, pattern): return tests\nclass Case(unittest.TestCase):\n    def test_both(self):\n        self.assertEqual(choose(True),1)\n        self.assertEqual(choose(False),0)\n",
                encoding="utf-8",
            )
            runner = Path(__file__).resolve().parents[1] / "scripts" / SCRIPT
            report = root / "report.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(runner),
                    "--start-directory",
                    str(root),
                    "--coverage",
                    "--report",
                    str(report),
                ],
                cwd=root,
                env=dict(os.environ),
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            data = json.loads(report.read_text())
            self.assertTrue(data["coverage_ok"])
            coverage = CoverageData(basename=str(root / ".coverage"))
            coverage.read()
            self.assertTrue({1, 2, 3, 4} <= set(coverage.lines(str(source.resolve()))))

    def _controller_runner(self, root, *extra):
        runner = Path(__file__).resolve().parents[1] / "scripts" / SCRIPT
        report = root / "report.json"
        result = subprocess.run(
            [
                sys.executable,
                str(runner),
                "--start-directory",
                str(root),
                "--workers",
                "4",
                "--report",
                str(report),
                *extra,
            ],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=40,
        )
        return result, json.loads(report.read_text()) if report.exists() else None

    def _check_controller_execution_and_boundaries(self):
        hook = """import os, pathlib, socket, unittest
from unittest.mock import Mock, patch
root = pathlib.Path(__file__).parent
network_guard = Mock(side_effect=AssertionError('controller socket guard'))
def setUpModule():
    global guard
    os.environ['CHECKOUT_CONTROLLER_ACTIVE'] = 'yes'
    guard = patch.object(socket.socket, 'connect', network_guard)
    guard.start()
def tearDownModule():
    guard.stop()
    os.environ.pop('CHECKOUT_CONTROLLER_ACTIVE', None)
    (root/'phase-done').touch()
class HookCase(unittest.TestCase):
    def test_selected(self):
        self.assertEqual(self.configured,42)
        self.assertEqual((root/'hook-count').read_text(),'1')
        self.assertIs(socket.socket.connect,network_guard)
        with socket.socket() as client:
            with self.assertRaisesRegex(AssertionError,'controller socket guard'):
                client.connect(('example.invalid',1))
        FAILURE
def load_tests(loader,tests,pattern):
    marker=root/'hook-count'
    count=int(marker.read_text()) if marker.exists() else 0
    marker.write_text(str(count+1))
    case=HookCase('test_selected')
    case.configured=42
    return unittest.TestSuite([case])
"""
        worker = """import os, pathlib, socket, time, unittest
from unittest.mock import Mock
root=pathlib.Path(__file__).parent
class Case(unittest.TestCase):
    def test_worker(self):
        self.assertTrue((root/'phase-done').exists())
        self.assertNotIn('CHECKOUT_CONTROLLER_ACTIVE',os.environ)
        self.assertEqual((root/'hook-count').read_text(),'1')
        self.assertNotIsInstance(socket.socket.connect,Mock)
        (root/('pid-'+__name__)).write_text(str(os.getpid()))
        (root/('active-'+__name__)).touch()
        deadline=time.monotonic()+10
        while len(list(root.glob('active-*'))) != 4:
            self.assertLess(time.monotonic(),deadline,'four worker processes did not run')
            time.sleep(.01)
"""
        for fail in (False, True):
            with (
                self.subTest(controller_failure=fail),
                tempfile.TemporaryDirectory() as tmp,
            ):
                root = Path(tmp)
                (root / "test_hook.py").write_text(
                    hook.replace(
                        "FAILURE", "self.fail('controller failure')" if fail else "pass"
                    )
                )
                for index in range(4):
                    (root / f"test_worker{index}.py").write_text(worker)
                result, data = self._controller_runner(
                    root, "--controller-module", "test_hook"
                )
                self.assertEqual(result.returncode, 1 if fail else 0, result.stderr)
                self.assertEqual(data["mode"], "controller-then-workers")
                self.assertTrue(data["complete"])
                self.assertEqual(data["discovered"], 5)
                self.assertEqual(data["controller"]["assigned"], 1)
                self.assertEqual(data["controller"]["executed"], 1)
                self.assertEqual(data["controller"]["exit"], 1 if fail else 0)
                self.assertEqual(len(data["workers"]), 4)
                self.assertTrue(
                    all(
                        w["exit"] == 0 and w["assigned"] == w["executed"] == 1
                        for w in data["workers"]
                    )
                )
                pids = {p.read_text() for p in root.glob("pid-*")}
                self.assertEqual(len(pids), 4)
        # Remove an all-controller original bucket without renumbering survivors.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "test_a_hook.py").write_text(
                "import unittest\nclass A(unittest.TestCase):\n    def test_a(self): pass\ndef load_tests(loader,tests,pattern): return tests\n"
            )
            for name in ("b", "c", "d"):
                (root / f"test_{name}.py").write_text(
                    "import unittest\nclass Case(unittest.TestCase):\n    def test_a(self): pass\n"
                )
            result, data = self._controller_runner(
                root, "--controller-module", "test_a_hook"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual([w["worker"] for w in data["workers"]], [1, 2, 3])
            self.assertEqual(
                [w["execution_ordinal"] for w in data["workers"]], [0, 1, 2]
            )
        # All tests in the original hook subtree: no empty worker is launched.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "test_hook.py").write_text(
                'import unittest\nclass A(unittest.TestCase):\n    @classmethod\n    def setUpClass(cls): raise unittest.SkipTest("fixture skip")\n    def test_a(self): pass\nclass B(unittest.TestCase):\n    def test_b(self): pass\ndef load_tests(loader,tests,pattern): return tests\n'
            )
            result, data = self._controller_runner(
                root, "--controller-module", "test_hook"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(data["workers"], [])
            self.assertEqual(data["controller"]["executed"], 1)
            self.assertEqual(data["controller"]["fixture_skipped"], 1)
            self.assertTrue(data["execution_complete"])
            result, data = self._controller_runner(
                root, "--controller-module", "test_hook", "--workers", "1"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(data["mode"], "original-root")

        # Standard TestCase.id is insufficient when a local class is only
        # exported under an alias: retain its original instance in whole-root mode.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "test_hook.py").write_text(
                "import unittest\nclass Hook(unittest.TestCase):\n    def test_a(self): pass\ndef load_tests(loader, tests, pattern): return tests\n"
            )
            (root / "test_worker.py").write_text(
                "import unittest\ndef create_case():\n    class LocalCase(unittest.TestCase):\n        def test_b(self): pass\n    return LocalCase\nExportedCase = create_case()\n"
            )
            result, data = self._controller_runner(
                root, "--controller-module", "test_hook"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(data["mode"], "original-root")
            self.assertIn("resolved", data["fallback_reason"])

    def _check_controller_conservative_admission(self):
        import importlib.util
        from types import ModuleType, SimpleNamespace
        from unittest.mock import patch

        runner = Path(__file__).resolve().parents[1] / "scripts" / SCRIPT
        spec = importlib.util.spec_from_file_location("controller_plan_test", runner)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        class A(unittest.TestCase):
            def test_a(self):
                pass

            def test_b(self):
                pass

        class B(unittest.TestCase):
            def test_b(self):
                pass

        A.__module__ = "controller_fixture"
        B.__module__ = "worker_fixture"
        A.__qualname__ = "A"
        B.__qualname__ = "B"
        controller_module = ModuleType("controller_fixture")
        controller_module.A, controller_module.B = A, B
        worker_module = ModuleType("worker_fixture")
        worker_module.B = B

        def plan(kind):
            left = unittest.TestSuite([A("test_a")])
            right = unittest.TestSuite([B("test_b")])
            root = unittest.TestSuite([left, right])
            hooks = [("test_hook", left)]
            if kind == "unknown":
                hooks.append(("test_unknown", right))
            if kind == "modified":
                right.annotation = "changed"
            if kind == "custom-id":
                B.id = lambda self: "worker_fixture.factory"
            if kind == "shared-module":
                B.__module__ = A.__module__
            if kind == "shared-class":
                right = unittest.TestSuite([A("test_b"), B("test_b")])
                root = unittest.TestSuite([left, right])
            if kind == "custom-root":

                class Root(unittest.TestSuite):
                    pass

                root = Root([left, right])
            ids = module.test_ids(root, allow_custom=True)
            with patch.dict(
                sys.modules,
                {
                    "controller_fixture": controller_module,
                    "worker_fixture": worker_module,
                },
            ):
                output = module.execution_plan(
                    root,
                    SimpleNamespace(hook_seen=True, hooks=hooks),
                    ids,
                    module.partition(ids, 2),
                    ["test_hook"],
                )
            B.__module__ = "worker_fixture"
            if "id" in B.__dict__:
                del B.id
            return output

        self.assertEqual(plan("safe")["mode"], "controller-then-workers")
        for kind in (
            "unknown",
            "modified",
            "custom-id",
            "shared-module",
            "shared-class",
            "custom-root",
        ):
            with self.subTest(kind=kind):
                output = plan(kind)
                self.assertEqual(output["mode"], "original-root")
                self.assertTrue(output["reason"])
                if kind in ("shared-module", "shared-class"):
                    self.assertIn("share a fixture scope", output["reason"])
        left = unittest.TestSuite([A("test_a")])
        root = unittest.TestSuite([left])
        ids = module.test_ids(root, allow_custom=True)
        loader = SimpleNamespace(hook_seen=True, hooks=[("test_hook", left)])
        for names in (["missing"], ["test_hook", "test_hook"]):
            with self.assertRaises(ValueError):
                module.execution_plan(
                    root, loader, ids, module.partition(ids, 2), names
                )

    def _check_controller_branch_coverage(self):
        from coverage import CoverageData

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "review_sensei"
            package.mkdir()
            (package / "__init__.py").write_text("")
            source = package / "logic.py"
            source.write_text(
                "def choose(flag):\n    if flag:\n        return 1\n    return 0\n"
            )
            hook = """import unittest
from review_sensei.logic import choose
class HookCase(unittest.TestCase):
    def test_yes(self):
        self.assertEqual(choose(True),1)
        FAILURE
def load_tests(loader,tests,pattern): return tests
"""
            (root / "test_worker.py").write_text(
                "import unittest\nfrom review_sensei.logic import choose\nclass Case(unittest.TestCase):\n    def test_no(self): self.assertEqual(choose(False),0)\n"
            )
            for failure in (False, True):
                with self.subTest(controller_failure=failure):
                    (root / "test_hook.py").write_text(
                        hook.replace(
                            "FAILURE",
                            "self.fail('controller failure')" if failure else "pass",
                        )
                    )
                    result, data = self._controller_runner(
                        root, "--controller-module", "test_hook", "--coverage"
                    )
                    self.assertEqual(
                        result.returncode, 1 if failure else 0, result.stderr
                    )
                    self.assertEqual(data["mode"], "controller-then-workers")
                    self.assertTrue(data["coverage_ok"])
                    self.assertTrue(data["execution_complete"])
                    self.assertEqual(data["controller"]["executed"], 1)
                    self.assertEqual(sum(w["executed"] for w in data["workers"]), 1)
                    cov = CoverageData(basename=str(root / ".coverage"))
                    cov.read()
                    self.assertTrue(
                        {1, 2, 3, 4} <= set(cov.lines(str(source.resolve())))
                    )
                    self.assertTrue(
                        {(2, 3), (2, 4)} <= set(cov.arcs(str(source.resolve())))
                    )

    def _check_real_controller_cost_fixture_dispatch(self):
        import shlex

        import yaml

        root = Path(__file__).resolve().parents[1]
        workflow = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())[
            "jobs"
        ]
        commands = [
            step["run"]
            for step in workflow["compatibility"]["steps"]
            if "scripts/run_checkout_tests.py" in step.get("run", "")
        ]
        commands += [
            line
            for step in workflow["quality"]["steps"]
            for line in step.get("run", "").splitlines()
            if "scripts/run_checkout_tests.py" in line
        ]
        self.assertEqual(len(commands), 3)
        for text in commands:
            command = shlex.split(text)
            self.assertEqual(
                command[command.index("--controller-module") + 1],
                "test_provider_common_flow",
            )
            self.assertEqual(command[command.index("--workers") + 1], "4")
        code = r"""import contextlib,io,json,pathlib,sys
from unittest.mock import patch
root=pathlib.Path.cwd()
sys.path[:0]=[str(root/'tests'),str(root),str(root/'src'),str(root/'packages/python/review-sensei-github/src')]
from scripts import run_checkout_tests as runner
costs_path,output=map(pathlib.Path,sys.argv[1:])
roots=[];hooks=[];manifests={};phase=[]
original_discover=runner.HookAwareLoader.discover
original_load=runner.HookAwareLoader.loadTestsFromModule
def discover(loader,*args,**kwargs):
    suite=original_discover(loader,*args,**kwargs);roots.append(suite);return suite
def load(loader,module,*args,**kwargs):
    suite=original_load(loader,module,*args,**kwargs)
    if module.__name__=='test_provider_common_flow':hooks.append(suite)
    return suite
def controller(suite,identities,report,load_errors=False):
    assert len(hooks)==1 and suite is hooks[0]
    assert runner.test_ids(suite,allow_custom=True)==identities
    assert 0<len(identities)<len(runner.test_ids(roots[0],allow_custom=True))
    phase.append(list(identities))
    report.write_text(json.dumps({'class_seconds':{},'executed':len(identities),'fixture_skipped':0,'inventory_complete':True}))
    return 0
def worker(command,**kwargs):
    assert len(phase)==1,'worker launched before controller completed'
    manifest=pathlib.Path(command[command.index('--worker-manifest')+1])
    identities=json.loads(manifest.read_text());manifests[manifest.name]=identities
    pathlib.Path(command[command.index('--worker-report')+1]).write_text(json.dumps({'class_seconds':{},'executed':len(identities),'fixture_skipped':0,'inventory_complete':True}))
    return 0
with patch.object(runner.HookAwareLoader,'discover',discover),patch.object(runner.HookAwareLoader,'loadTestsFromModule',load),patch.object(runner,'execute_suite',side_effect=controller),patch.object(runner.subprocess,'call',side_effect=worker),patch.object(sys,'argv',[str(root/'scripts/run_checkout_tests.py'),'--start-directory',str(root/'tests'),'--workers','4','--controller-module','test_provider_common_flow','--costs',str(costs_path),'--report',str(output)]),contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
    assert runner.main()==0
assert len(roots)==len(hooks)==len(phase)==1
ids=runner.test_ids(roots[0],allow_custom=True)
expected=runner.partition(ids,4,json.loads(costs_path.read_text()))
owned=set(phase[0]);data=json.loads(output.read_text())
assert data['mode']=='controller-then-workers' and data['discovered']==len(ids)
assert data['controller']['assigned']==len(owned)==len(runner.test_ids(hooks[0],allow_custom=True))
assert data['inventory_sha256']==runner.inventory_digest(ids)
assert len(data['workers'])==4
for entry in data['workers']:
    actual=manifests[f"worker-{entry['execution_ordinal']}.json"]
    assert actual==[name for name in expected[entry['worker']] if name not in owned]
    assert entry['inventory_sha256']==runner.inventory_digest(actual)
assert len(owned)+sum(entry['assigned'] for entry in data['workers'])==len(ids)
output.write_text(json.dumps({'inventory':data['inventory_sha256'],'controller':data['controller'],'workers':data['workers']}))
"""
        with tempfile.TemporaryDirectory() as tmp:
            reports = {}
            for fixture in sorted(
                (root / "tests/fixtures").glob("checkout-class-seconds*.json")
            ):
                output = Path(tmp) / (fixture.stem + ".report.json")
                result = subprocess.run(
                    [sys.executable, "-c", code, str(fixture), str(output)],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=90,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                reports[fixture.name] = json.loads(output.read_text())
            # A real discovered class's measured cost must affect ACTUAL main
            # dispatch manifests, not merely a disconnected partition helper.
            fixture = root / "tests/fixtures/checkout-class-seconds-coverage.json"
            costs = json.loads(fixture.read_text())
            key = next(
                name for name in costs if name.endswith(".QuarantineCheckpointTests")
            )
            costs.pop(key)
            reduced = Path(tmp) / "reduced.json"
            reduced.write_text(json.dumps(costs))
            output = Path(tmp) / "reduced.report.json"
            result = subprocess.run(
                [sys.executable, "-c", code, str(reduced), str(output)],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=90,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            changed = json.loads(output.read_text())
            full = reports[fixture.name]
            self.assertEqual(full["inventory"], changed["inventory"])
            self.assertNotEqual(
                [w["inventory_sha256"] for w in full["workers"]],
                [w["inventory_sha256"] for w in changed["workers"]],
            )


SCRIPT = "run_checkout_tests.py"
ISOLATED = []
OTHER = 'import unittest\nclass Between(unittest.TestCase):\n    def id(self): return "test_example.Between.test_b"\n    def test_b(self): pass\n'
