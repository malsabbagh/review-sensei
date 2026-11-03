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


SCRIPT = "run_checkout_tests.py"
ISOLATED = []
OTHER = 'import unittest\nclass Between(unittest.TestCase):\n    def id(self): return "test_example.Between.test_b"\n    def test_b(self): pass\n'
