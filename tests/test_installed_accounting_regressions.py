"""Real runner regressions for fixture dispatch and one-time discovery hooks."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CASES = [
    (
        "installed-collision-probe.py",
        'import unittest\n\nclass A(unittest.TestCase):\n    @classmethod\n    def setUpClass(cls): raise unittest.SkipTest("optional fixture unavailable")\n    def id(self): return "test_example.AliasA.test_a"\n    def test_a(self): pass\nclass B(unittest.TestCase):\n    def id(self): return "test_example.AliasB.test_b"\n    def run(self, result): return result\n    def test_b(self): pass\nA.__qualname__ = B.__qualname__ = "Same"\nAliasA, AliasB = A, B\ndel A, B\ndef load_tests(loader, tests, pattern):\n    return unittest.TestSuite([AliasA("test_a"), AliasB("test_b")])\n',
        False,
    ),
    (
        "installed-proxy-fixture-probe.py",
        'import unittest\n\nclass Fixture(unittest.TestCase):\n    @classmethod\n    def setUpClass(cls): raise unittest.SkipTest("optional fixture unavailable")\nclass ProxyCase(unittest.TestCase):\n    @property\n    def __class__(self): return Fixture\n    def test_a(self): pass\ndef load_tests(loader, tests, pattern):\n    return unittest.TestSuite([ProxyCase("test_a")])\n',
        True,
    ),
    (
        "installed-missing-stop-probe.py",
        'import unittest\n\nclass Unfinished(unittest.TestCase):\n    def run(self, result):\n        result.startTest(self)\n        return result\n    def test_a(self): self.fail("the test body must not be silently omitted")\n',
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
            "fixture-reentry-probe.py",
            'import unittest\n\nclass Shared(unittest.TestCase):\n    setup_calls = 0\n\n    @classmethod\n    def setUpClass(cls):\n        cls.setup_calls += 1\n        if cls.setup_calls == 2:\n            raise unittest.SkipTest("second setup")\n\n    def id(self):\n        alias = "AAlias" if self._testMethodName == "test_a" else "CAlias"\n        return f"test_example.{alias}.{self._testMethodName}"\n\n    def run(self, result):\n        if self._testMethodName == "test_a":\n            return result  # Deliberately omit an assigned test before fixture re-entry.\n        return super().run(result)\n\n    def test_a(self): pass\n    def test_c(self): pass\n\nAAlias = CAlias = Shared\n\nclass Between(unittest.TestCase):\n    def test_b(self): pass\n\ndef load_tests(loader, tests, pattern):\n    return unittest.TestSuite([Shared("test_a"), Between("test_b"), Shared("test_c")])\n',
            False,
        ),
        (
            "fixture-module-reentry-probe.py",
            'import unittest\nfrom other_fixture import Between\n\nsetup_calls = 0\ndef setUpModule():\n    global setup_calls\n    setup_calls += 1\n    if setup_calls == 2:\n        raise unittest.SkipTest("second module setup")\n\nclass Shared(unittest.TestCase):\n    def id(self):\n        alias = "AAlias" if self._testMethodName == "test_a" else "CAlias"\n        return f"test_example.{alias}.{self._testMethodName}"\n    def run(self, result):\n        if self._testMethodName == "test_a":\n            return result\n        return super().run(result)\n    def test_a(self): pass\n    def test_c(self): pass\n\nAAlias = CAlias = Shared\ndef load_tests(loader, tests, pattern):\n    return unittest.TestSuite([Shared("test_a"), Between("test_b"), Shared("test_c")])\n',
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


class RunnerAccountingRegressions(unittest.TestCase):
    def test_original_suite_and_exact_accounting(self):
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
                if "reentry" in label and not success:
                    self.assertIn("skipped=1", completed.stderr)
                elif "reentry" in label:
                    self.assertEqual(
                        sum(w["fixture_skipped"] for w in data["workers"]), 1
                    )

    def test_observer_restores_existing_profiler_and_preserves_trace(self):
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

    def test_no_hook_modules_keep_isolated_workers(self):
        runner = Path(__file__).resolve().parents[1] / "scripts" / SCRIPT
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("a", "b"):
                (root / f"test_{name}.py").write_text(
                    "import os, pathlib, sys, unittest\n"
                    "class Case(unittest.TestCase):\n"
                    "    def test_isolated(self):\n"
                    "        self.assertTrue(sys.flags.isolated)\n"
                    "        pathlib.Path(__file__).with_suffix('.pid').write_text(str(os.getpid()))\n",
                    encoding="utf-8",
                )
            costs = root / "costs.json"
            costs.write_text(json.dumps({"test_a.py": 2, "test_b.py": 1}))
            report = root / "report.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(runner),
                    "--start-directory",
                    str(root),
                    "--workers",
                    "2",
                    "--costs",
                    str(costs),
                    "--report",
                    str(report),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            data = json.loads(report.read_text())
            self.assertTrue(data["complete"])
            self.assertEqual(len(data["workers"]), 2)
            self.assertEqual(data["discovered"], 2)
            self.assertEqual(data["uncosted_units"], [])
            self.assertNotEqual(
                (root / "test_a.pid").read_text(), (root / "test_b.pid").read_text()
            )

    def test_early_success_exit_cannot_waive_inventory(self):
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


SCRIPT = "run_installed_test_suites.py"
ISOLATED = ["-I"]
OTHER = 'import unittest\nclass Between(unittest.TestCase):\n    def id(self): return "test_example.Between.test_b"\n    def test_b(self): pass\n'
