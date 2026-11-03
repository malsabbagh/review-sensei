"""The parallel checkout lane must retain exhaustive discovery and failures."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from checkout_accounting_cases import CheckoutAccountingChecks

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/run_checkout_tests.py"
# Checkout-only: the sdist contract excludes this module from installed lanes.
SPEC = importlib.util.spec_from_file_location("checkout_runner", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


class CheckoutRunnerTests(CheckoutAccountingChecks, unittest.TestCase):
    def test_partition_covers_every_identity_once_and_stays_deterministic(self):
        identities = [
            f"module.Case_{index // 10:03d}.test_{index:04d}" for index in range(2700)
        ]
        for workers in (1, 2, 4, 16):
            with self.subTest(workers=workers):
                groups = RUNNER.partition(identities, workers)
                self.assertEqual(len(groups), workers)
                flat = [identity for group in groups for identity in group]
                self.assertEqual(sorted(flat), sorted(identities))
                self.assertEqual(len(set(flat)), len(flat))
                self.assertEqual(
                    groups, RUNNER.partition(list(reversed(identities)), workers)
                )
                self.assertLessEqual(max(map(len, groups)) - min(map(len, groups)), 10)
                # This count bound describes the uniform ten-method fixture,
                # not equal execution time for arbitrary test classes.
                placements = {}
                for index, group in enumerate(groups):
                    for identity in group:
                        placements.setdefault(identity.rpartition(".")[0], set()).add(
                            index
                        )
                self.assertTrue(
                    all(len(indices) == 1 for indices in placements.values())
                )
        self.assertEqual(RUNNER.partition(["one"], 4), [["one"]])
        self.assertEqual(RUNNER.partition(["one", "two"], 4), [["one"], ["two"]])
        for identities, workers in (([], 4), (["a", "a"], 4), (["a"], 0), (["a"], 17)):
            with self.assertRaises(ValueError):
                RUNNER.partition(identities, workers)

    def test_coverage_costs_change_checkpoint_assignment(self):
        loader = unittest.TestLoader()
        suite = loader.discover(
            str(ROOT / "tests"),
            pattern="test_quarantine_checkpoint.py",
            top_level_dir=str(ROOT / "tests"),
        )
        self.assertEqual(loader.errors, [])
        discovered = sorted(RUNNER.test_ids(suite))
        names = (
            "QuarantineCarrierPlanningTests",
            "QuarantineCheckpointTests",
            "QuarantineDecisionScopeFenceTests",
            "QuarantineRolloverTests",
        )
        selected = [
            next(identity for identity in discovered if f".{name}." in identity)
            for name in names
        ]
        fixture = ROOT / "tests/fixtures/checkout-class-seconds-coverage.json"
        costs = json.loads(fixture.read_text(encoding="utf-8"))
        keys = [identity.rpartition(".")[0] for identity in selected]
        for key in keys:
            self.assertIn(key, costs)
        checkpoint = next(
            key for key in keys if key.endswith("QuarantineCheckpointTests")
        )
        reduced = {key: value for key, value in costs.items() if key != checkpoint}
        full = RUNNER.partition(selected, 2, costs)
        partial = RUNNER.partition(selected, 2, reduced)
        for groups in (full, partial):
            self.assertEqual(sorted(sum(groups, [])), sorted(selected))
        self.assertNotEqual(full, partial)

    def run_fixture(self, source, *, manifest=None):
        with tempfile.TemporaryDirectory() as scratch:
            directory = Path(scratch)
            (directory / "test_example.py").write_text(source, encoding="utf-8")
            command = [
                sys.executable,
                str(SCRIPT),
                "--workers",
                "4",
                "--start-directory",
                str(directory),
            ]
            if manifest is not None:
                path = directory / "inventory.json"
                path.write_text(json.dumps(manifest), encoding="utf-8")
                command.extend(["--worker-manifest", str(path)])
            result = subprocess.run(command, capture_output=True, text=True, timeout=60)
            receipts = sorted(path.name for path in directory.glob("receipt-*"))
            return result, receipts

    def test_windows_costs_route_and_schedule_discovered_classes(self):
        import shlex

        import yaml

        jobs = yaml.safe_load(
            (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        )["jobs"]
        windows = next(
            step
            for step in jobs["compatibility"]["steps"]
            if step.get("if") == "runner.os == 'Windows'"
        )
        command = shlex.split(windows["run"])
        fixture_name = command[command.index("--costs") + 1]
        self.assertEqual(
            fixture_name, "tests/fixtures/checkout-class-seconds-windows.json"
        )
        self.assertEqual(command[command.index("--workers") + 1], "4")
        self.assertNotIn("--coverage", command)
        self.assertFalse(windows.get("continue-on-error", False))
        coverage = next(
            step
            for step in jobs["quality"]["steps"]
            if step.get("name") == "Branch coverage"
        )
        coverage_command = next(
            shlex.split(line)
            for line in coverage["run"].splitlines()
            if "scripts/run_checkout_tests.py" in line
        )
        self.assertNotEqual(
            coverage_command[coverage_command.index("--costs") + 1], fixture_name
        )

        loader = unittest.TestLoader()
        suite = loader.discover(
            str(ROOT / "tests"),
            pattern="test_quarantine_checkpoint.py",
            top_level_dir=str(ROOT / "tests"),
        )
        self.assertEqual(loader.errors, [])
        discovered = sorted(RUNNER.test_ids(suite))
        names = (
            "QuarantineCarrierPlanningTests",
            "QuarantineCheckpointTests",
            "QuarantineDecisionScopeFenceTests",
            "QuarantineRolloverTests",
        )
        selected = [
            next(identity for identity in discovered if f".{name}." in identity)
            for name in names
        ]
        costs = json.loads((ROOT / fixture_name).read_text(encoding="utf-8"))
        keys = [identity.rpartition(".")[0] for identity in selected]
        for key in keys:
            self.assertIn(key, costs)
        checkpoint = next(
            key for key in keys if key.endswith("QuarantineCheckpointTests")
        )
        reduced = {key: value for key, value in costs.items() if key != checkpoint}
        full = RUNNER.partition(selected, 2, costs)
        partial = RUNNER.partition(selected, 2, reduced)
        for groups in (full, partial):
            self.assertEqual(sorted(sum(groups, [])), sorted(selected))
        self.assertNotEqual(full, partial)

    def test_real_workers_finish_all_tests_and_preserve_fixtures_on_failure(self):
        source = """import pathlib, unittest
class Case(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ready = True
    def setUp(self):
        assert self.ready
    def tearDown(self):
        pathlib.Path(__file__).with_name("receipt-" + self._testMethodName).touch()
class CaseA(Case):
    def test_a(self): pass
class CaseB(Case):
    def test_b(self): self.fail("synthetic failure")
class CaseC(Case):
    def test_c(self): pass
class CaseD(Case):
    @unittest.skip("synthetic skip")
    def test_d(self): pass
"""
        result, receipts = self.run_fixture(source)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Discovered 4 tests", result.stdout)
        self.assertEqual(
            receipts, ["receipt-test_a", "receipt-test_b", "receipt-test_c"]
        )
        self.assertIn("synthetic failure", result.stderr)
        self.assertIn("synthetic skip", result.stderr)
        for index in range(1, 5):
            self.assertIn(f"[checkout worker {index}/4: 1 tests, exit ", result.stderr)
        failure = result.stderr.index("synthetic failure")
        self.assertLess(result.stderr.index("[checkout worker 2/4:"), failure)
        self.assertLess(failure, result.stderr.index("[checkout worker 3/4:"))

    def test_discovery_error_empty_suite_and_missing_worker_test_fail_closed(self):
        for source, manifest, diagnostic in (
            (
                "raise ImportError('synthetic discovery failure')",
                None,
                "synthetic discovery failure",
            ),
            (
                "import unittest",
                None,
                "discovery must produce distinct nonempty test IDs",
            ),
            (
                "import unittest",
                ["test_example.Missing.test_case"],
                "Worker failed to load its exact assigned test inventory",
            ),
        ):
            with self.subTest(source=source, manifest=manifest):
                result, _ = self.run_fixture(source, manifest=manifest)
                self.assertEqual(result.returncode, 1)
                self.assertIn(diagnostic, result.stderr)

    def test_fewer_classes_than_workers_reports_only_nonempty_workers(self):
        result, _ = self.run_fixture("""import unittest
class CaseA(unittest.TestCase):
    def test_a(self): pass
class CaseB(unittest.TestCase):
    def test_b(self): pass
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Discovered 2 tests; running 2 disjoint workers", result.stdout)
        self.assertIn("[checkout worker 1/2: 1 tests, exit 0]", result.stderr)
        self.assertIn("[checkout worker 2/2: 1 tests, exit 0]", result.stderr)

    def test_custom_suite_wrapper_fails_before_any_worker(self):
        subclass = """import pathlib, unittest
class GuardedSuite(unittest.TestSuite):
    def run(self, result, debug=False):
        raise AssertionError("custom wrapper must not be discarded")
def load_tests(loader, tests, pattern):
    return unittest.TestSuite([unittest.TestSuite([GuardedSuite(tests)])])
class Case(unittest.TestCase):
    def test_example(self):
        pathlib.Path(__file__).with_name("receipt-executed").touch()
"""
        instance = """import pathlib, unittest
def load_tests(loader, tests, pattern):
    inner = unittest.TestSuite([tests])
    def custom_run(result, debug=False):
        raise AssertionError("instance wrapper must not be discarded")
    inner.run = custom_run
    return unittest.TestSuite([inner])
class Case(unittest.TestCase):
    def test_example(self):
        pathlib.Path(__file__).with_name("receipt-executed").touch()
"""
        for source in (subclass, instance):
            with self.subTest(source=source):
                result, receipts = self.run_fixture(source)
                self.assertEqual(result.returncode, 1)
                # Hook suites execute their original root in the controller.
                # The wrapper's own failure must propagate without a body run.
                self.assertIn("wrapper must not be discarded", result.stderr)
                self.assertEqual(receipts, [])

        # A future interpreter may add an ordinary default field. Compare to
        # that interpreter's fresh suite while still rejecting modified state.
        probe = """\
import importlib.util, sys, unittest
original = unittest.TestSuite.__init__
def future_init(self, tests=()):
    original(self, tests)
    self.future_default_field = 0
unittest.TestSuite.__init__ = future_init
spec = importlib.util.spec_from_file_location('runner_under_test', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
test = unittest.FunctionTestCase(lambda: None)
suite = unittest.TestSuite([test])
assert module.test_ids(suite) == [test.id()]
suite.future_default_field = 1
try:
    module.test_ids(suite)
except ValueError:
    pass
else:
    raise AssertionError('modified future field was accepted')
"""
        result = subprocess.run(
            [sys.executable, "-c", probe, str(SCRIPT)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_macos_route_uses_complete_isolated_runner_with_seed_costs(self):
        import shlex

        import yaml

        jobs = yaml.safe_load(
            (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        )["jobs"]
        compatibility = jobs["compatibility"]
        self.assertIn(
            {"os": "macos-latest", "python-version": "3.13"},
            compatibility["strategy"]["matrix"]["include"],
        )
        macos = next(
            step
            for step in compatibility["steps"]
            if step.get("if") == "runner.os == 'macOS'"
        )
        command = shlex.split(macos["run"])
        self.assertIn("scripts/run_checkout_tests.py", command)
        self.assertEqual(command[command.index("--workers") + 1], "4")
        fixture_name = command[command.index("--costs") + 1]
        self.assertEqual(
            fixture_name, "tests/fixtures/checkout-class-seconds-macos.json"
        )
        self.assertIn("--report", command)
        self.assertNotIn("--coverage", command)
        self.assertFalse(macos.get("continue-on-error", False))
        upload = next(
            step
            for step in compatibility["steps"]
            if "upload-artifact@" in step.get("uses", "")
        )
        self.assertEqual(upload.get("if"), "always()")
        self.assertEqual(
            upload["with"]["path"], "${{ runner.temp }}/checkout-timings.json"
        )

        loader = unittest.TestLoader()
        suite = loader.discover(
            str(ROOT / "tests"),
            pattern="test_quarantine_checkpoint.py",
            top_level_dir=str(ROOT / "tests"),
        )
        self.assertEqual(loader.errors, [])
        discovered = sorted(RUNNER.test_ids(suite))
        names = (
            "QuarantineCarrierPlanningTests",
            "QuarantineCheckpointTests",
            "QuarantineDecisionScopeFenceTests",
            "QuarantineRolloverTests",
        )
        selected = [
            next(identity for identity in discovered if f".{name}." in identity)
            for name in names
        ]
        costs = json.loads((ROOT / fixture_name).read_text(encoding="utf-8"))
        keys = [identity.rpartition(".")[0] for identity in selected]
        for key in keys:
            self.assertIn(key, costs)
        checkpoint = next(
            key for key in keys if key.endswith("QuarantineCheckpointTests")
        )
        reduced = {key: value for key, value in costs.items() if key != checkpoint}
        full = RUNNER.partition(selected, 2, costs)
        partial = RUNNER.partition(selected, 2, reduced)
        for groups in (full, partial):
            self.assertEqual(sorted(sum(groups, [])), sorted(selected))
        self.assertNotEqual(full, partial)

        # Discover in a fresh process so this check does not import every test
        # module into the worker executing the checkout-runner tests.
        code = """\
import json, pathlib, sys, unittest
from scripts.run_checkout_tests import test_ids
root, output = map(pathlib.Path, sys.argv[1:])
loader = unittest.TestLoader()
suite = loader.discover(str(root / 'tests'), top_level_dir=str(root / 'tests'))
if loader.errors:
    raise SystemExit('\\n'.join(loader.errors))
classes = sorted({identity.rpartition('.')[0] for identity in test_ids(suite)})
output.write_text(json.dumps(classes), encoding='utf-8')
"""
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "classes.json"
            result = subprocess.run(
                [sys.executable, "-c", code, str(ROOT), str(output)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            classes = set(json.loads(output.read_text(encoding="utf-8")))
        self.assertFalse(set(costs) - classes, "macOS cost fixture has orphaned keys")

    def test_real_workers_report_success_for_the_complete_inventory(self):
        source = """import pathlib, unittest
class Case(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pathlib.Path(__file__).with_name("setup.once").touch(exist_ok=False)
    def test_a(self): pass
    def test_b(self): pass
"""
        result, _ = self.run_fixture(source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Discovered 2 tests; running 1 disjoint workers", result.stdout)
        with self.subTest(check="fixture skip accounting"):
            self._check_fixture_skips_account_only_for_the_matching_inventory()
        with self.subTest(check="fixture skip coverage"):
            self._check_fixture_skips_keep_coverage_and_the_accounting_distinct()
        with self.subTest(check="fixture module identity"):
            self._check_fixture_skip_uses_case_module_not_discovery_file()
        with self.subTest(check="unknown fixture scope"):
            self._check_unknown_fixture_scope_stays_unaccounted()
        for check in (
            self._check_original_suite_and_exact_accounting,
            self._check_observer_restores_existing_profiler_and_preserves_trace,
            self._check_early_success_exit_cannot_waive_inventory,
            self._check_hook_fallback_keeps_import_and_branch_coverage,
        ):
            with self.subTest(check=check.__name__):
                check()

    def _check_fixture_skips_account_only_for_the_matching_inventory(self):
        cases = (
            (
                "class",
                """import unittest
class Skipped(unittest.TestCase):
    @classmethod
    def setUpClass(cls): raise unittest.SkipTest('unavailable')
    def test_a(self): pass
    def test_b(self): pass
class Runs(unittest.TestCase):
    def test_c(self): pass
""",
                1,
                0,
                1,
                2,
            ),
            (
                "module",
                """import unittest
def setUpModule(): raise unittest.SkipTest('unavailable')
class First(unittest.TestCase):
    def test_a(self): pass
class Second(unittest.TestCase):
    def test_b(self): pass
""",
                2,
                0,
                0,
                2,
            ),
            (
                "two skipped classes",
                """import unittest
class First(unittest.TestCase):
    @classmethod
    def setUpClass(cls): raise unittest.SkipTest('first unavailable')
    def test_a(self): pass
    def test_b(self): pass
class Second(unittest.TestCase):
    @classmethod
    def setUpClass(cls): raise unittest.SkipTest('second unavailable')
    def test_c(self): pass
""",
                1,
                0,
                0,
                3,
            ),
            (
                "nested skipped class",
                """import unittest
class Outer:
    class Inner(unittest.TestCase):
        @classmethod
        def setUpClass(cls): raise unittest.SkipTest('unavailable')
        def test_a(self): pass
def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(Outer.Inner)
""",
                1,
                0,
                0,
                1,
            ),
            (
                "malformed skip holder",
                """import unittest
class InvalidHolder:
    def shortDescription(self): return None
    def __str__(self): return 'invalid holder'
class Malformed(unittest.TestCase):
    def run(self, result):
        result.addSkip(InvalidHolder(), 'invalid skip holder')
        return super().run(result)
    def test_a(self): pass
""",
                1,
                1,
                1,
                0,
            ),
            (
                "colliding class names with an omission",
                """import unittest
class A(unittest.TestCase):
    @classmethod
    def setUpClass(cls): raise unittest.SkipTest('unavailable')
    def id(self): return 'test_example.AliasA.test_a'
    def test_a(self): pass
class B(unittest.TestCase):
    def id(self): return 'test_example.AliasB.test_b'
    def run(self, result): return result
    def test_b(self): pass
A.__qualname__ = B.__qualname__ = 'Same'
AliasA, AliasB = A, B
del A, B
""",
                1,
                1,
                0,
                1,
            ),
            (
                "colliding class names with one completed",
                """import unittest
class A(unittest.TestCase):
    @classmethod
    def setUpClass(cls): raise unittest.SkipTest('unavailable')
    def id(self): return 'test_example.AliasA.test_a'
    def test_a(self): pass
class B(unittest.TestCase):
    def id(self): return 'test_example.AliasB.test_b'
    def test_b(self): pass
A.__qualname__ = B.__qualname__ = 'Same'
AliasA, AliasB = A, B
del A, B
""",
                1,
                0,
                1,
                1,
            ),
            (
                "colliding class names both skipped",
                """import unittest
class A(unittest.TestCase):
    @classmethod
    def setUpClass(cls): raise unittest.SkipTest('unavailable')
    def id(self): return 'test_example.AliasA.test_a'
    def test_a(self): pass
class B(unittest.TestCase):
    @classmethod
    def setUpClass(cls): raise unittest.SkipTest('unavailable')
    def id(self): return 'test_example.AliasB.test_b'
    def test_b(self): pass
A.__qualname__ = B.__qualname__ = 'Same'
AliasA, AliasB = A, B
del A, B
""",
                1,
                0,
                0,
                2,
            ),
            (
                "proxy fixture class",
                """import unittest
class Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls): raise unittest.SkipTest('unavailable')
class ProxyCase(unittest.TestCase):
    @property
    def __class__(self): return Fixture
    def id(self): return 'test_example.ProxyCase.test_a'
    def test_a(self): pass
""",
                1,
                0,
                0,
                1,
            ),
            (
                "omission",
                """import unittest
class Skipped(unittest.TestCase):
    @classmethod
    def setUpClass(cls): raise unittest.SkipTest('setUpModule (test_example)')
    def test_a(self): pass
class Omitted(unittest.TestCase):
    def run(self, result): return result
    def test_b(self): pass
""",
                1,
                1,
                0,
                1,
            ),
            (
                "duplicate start",
                """import unittest
class Duplicate(unittest.TestCase):
    def run(self, result):
        result.startTest(self); result.stopTest(self)
        result.startTest(self); result.stopTest(self)
        return result
    def test_a(self): pass
""",
                1,
                1,
                2,
                0,
            ),
            (
                "start without stop",
                """import unittest
class Unfinished(unittest.TestCase):
    def run(self, result):
        result.startTest(self)
        return result
    def test_a(self): pass
""",
                1,
                1,
                1,
                0,
            ),
        )
        for label, source, workers, expected_exit, executed, skipped in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as scratch:
                root = Path(scratch)
                (root / "test_example.py").write_text(source, encoding="utf-8")
                report = root / "report.json"
                proc = subprocess.run(
                    [
                        sys.executable,
                        str(SCRIPT),
                        "--workers",
                        str(workers),
                        "--start-directory",
                        str(root),
                        "--report",
                        str(report),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(proc.returncode, expected_exit, proc.stderr)
                data = json.loads(report.read_text())
                self.assertEqual(data["execution_complete"], expected_exit == 0)
                if expected_exit:
                    self.assertIn(
                        "did not account for its exact assigned test inventory",
                        proc.stderr,
                    )
                    self.assertFalse(data["workers"][0]["inventory_complete"])
                    self.assertIsNone(data["workers"][0]["timing_error"])
                self.assertEqual(
                    sum(worker["executed"] for worker in data["workers"]), executed
                )
                self.assertEqual(
                    sum(worker["fixture_skipped"] for worker in data["workers"]),
                    skipped,
                )

    def _check_fixture_skips_keep_coverage_and_the_accounting_distinct(self):
        from coverage import CoverageData

        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            for package in ("review_sensei", "review_sensei_github"):
                (root / package).mkdir()
                (root / package / "__init__.py").write_text("value = 1\n")
            (root / "test_example.py").write_text("""import unittest
class Skipped(unittest.TestCase):
    @classmethod
    def setUpClass(cls): raise unittest.SkipTest('not applicable')
    def test_a(self): pass
class Runs(unittest.TestCase):
    def test_b(self):
        import review_sensei, review_sensei_github
        self.assertEqual(review_sensei.value + review_sensei_github.value, 2)
""")
            report = root / "report.json"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--workers",
                    "1",
                    "--start-directory",
                    str(root),
                    "--coverage",
                    "--report",
                    str(report),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            data = json.loads(report.read_text())
            self.assertTrue(data["execution_complete"])
            self.assertTrue(data["coverage_ok"])
            self.assertEqual(data["workers"][0]["executed"], 1)
            self.assertEqual(data["workers"][0]["fixture_skipped"], 1)
            coverage = CoverageData(basename=str(root / ".coverage"))
            coverage.read()
            for package in ("review_sensei", "review_sensei_github"):
                source = str((root / package / "__init__.py").resolve())
                self.assertIn(source, coverage.measured_files())
                self.assertIn(1, coverage.lines(source))

    def _check_unknown_fixture_scope_stays_unaccounted(self):
        from collections import Counter
        from types import SimpleNamespace

        remaining = Counter({"unknown.Case.test_case": 1})
        event = SimpleNamespace(id=lambda: "setUpModule (unknown)")
        self.assertEqual(
            RUNNER.count_fixture_skips(remaining, {}, [(event, "skip")]), (0, False)
        )
        self.assertEqual(remaining, Counter({"unknown.Case.test_case": 1}))
        self.assertEqual(
            RUNNER.count_fixture_skips(remaining, {}, [("invalid", "skip")]),
            (0, True),
        )
        self.assertEqual(remaining, Counter({"unknown.Case.test_case": 1}))
        remaining = Counter({"m.A.test_1": 1, "m.A.test_2": 1, "m.B.test_3": 1})
        class_a, class_b = object(), object()
        scopes = {
            "m.A.test_1": ("m.A", "m", class_a),
            "m.A.test_2": ("m.A", "m", class_a),
            "m.B.test_3": ("m.B", "m", class_b),
        }
        first = SimpleNamespace(id=lambda: "setUpClass (m.A)")
        second = SimpleNamespace(id=lambda: "setUpClass (m.B)")
        events = [(first, "skip"), (first, "duplicate"), (second, "skip")]
        self.assertEqual(
            RUNNER.count_fixture_skips(remaining, scopes, events), (3, False)
        )
        self.assertFalse(remaining)
        remaining = Counter({"m.AliasA.test": 1, "m.AliasB.test": 1})
        scopes = {
            "m.AliasA.test": ("m.Same", "m", class_a),
            "m.AliasB.test": ("m.Same", "m", class_b),
        }
        ambiguous = SimpleNamespace(id=lambda: "setUpClass (m.Same)")
        self.assertEqual(
            RUNNER.count_fixture_skips(remaining, scopes, [(ambiguous, "skip")]),
            (0, False),
        )
        self.assertEqual(remaining.total(), 2)

    def _check_fixture_skip_uses_case_module_not_discovery_file(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            (root / "helper_suite.py").write_text("""import unittest
def setUpModule(): raise unittest.SkipTest('optional fixture unavailable')
class ForeignCase(unittest.TestCase):
    def test_a(self): pass
""")
            (root / "test_example.py").write_text("""import helper_suite
def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(helper_suite.ForeignCase)
""")
            report = root / "report.json"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--workers",
                    "1",
                    "--start-directory",
                    str(root),
                    "--report",
                    str(report),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("setUpModule (helper_suite)", proc.stderr)
            data = json.loads(report.read_text())
            self.assertTrue(data["execution_complete"])
            self.assertEqual(data["workers"][0]["executed"], 0)
            self.assertEqual(data["workers"][0]["fixture_skipped"], 1)


class MeasuredRunnerTests(unittest.TestCase):
    def test_measured_costs_schedule_expensive_classes_without_changing_inventory(self):
        identities = [f"module.Case{i}.test_it" for i in range(4)]
        costs = {
            "module.Case0": 10.0,
            "module.Case1": 9.0,
            "module.Case2": 1.0,
            "module.Case3": 1.0,
        }
        groups = RUNNER.partition(identities, 2, costs)
        self.assertNotEqual(
            next(i for i, g in enumerate(groups) if identities[0] in g),
            next(i for i, g in enumerate(groups) if identities[1] in g),
        )
        self.assertEqual(sorted(sum(groups, [])), identities)
        self.assertEqual(
            sorted(
                sum(costs[name.rpartition(".")[0]] for name in group)
                for group in groups
            ),
            [10.0, 11.0],
        )
        self.assertEqual(groups, RUNNER.partition(identities[::-1], 2, costs))
        for invalid in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                RUNNER.partition(identities, 2, {"module.Case0": invalid})

    def test_worker_coverage_combines_both_branches_and_keeps_failure_evidence(self):
        from coverage import CoverageData

        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            # --source accepts importable package names, including packages
            # outside cwd, rather than interpreting every name as a cwd path.
            installed = root / "installed"
            package = installed / "review_sensei"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("")
            probe = package / "probe.py"
            probe.write_text(
                "def choose(value):\n    if value:\n        return 1\n    return 0\n"
            )
            tests = root / "tests"
            tests.mkdir()
            (tests / "test_probe.py").write_text("""import unittest
from review_sensei.probe import choose
class Yes(unittest.TestCase):
    def test_yes(self): self.assertEqual(choose(True), 1)
class No(unittest.TestCase):
    def test_no(self):
        self.assertEqual(choose(False), 0)
        self.fail("intentional worker failure")
""")
            # The combined handoff overwrites previous-run data rather than
            # quietly accumulating unrelated coverage from cwd.
            old = CoverageData(basename=str(root / ".coverage"))
            old.add_arcs({str(root / "stale.py"): [(1, 2)]})
            old.write()
            report = root / "report.json"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--workers",
                    "2",
                    "--coverage",
                    "--report",
                    str(report),
                ],
                cwd=root,
                env={**os.environ, "PYTHONPATH": str(installed)},
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertIn("intentional worker failure", proc.stderr)
            data = json.loads(report.read_text())
            self.assertTrue(data["complete"])
            self.assertTrue(data["execution_complete"])
            self.assertTrue(data["coverage_ok"])
            self.assertEqual(data["discovered"], 2)
            self.assertEqual(
                sorted(worker["exit"] for worker in data["workers"]), [0, 1]
            )
            self.assertEqual(sum(worker["executed"] for worker in data["workers"]), 2)
            self.assertTrue(
                all(worker["elapsed_seconds"] > 0 for worker in data["workers"])
            )
            coverage = CoverageData(basename=str(root / ".coverage"))
            coverage.read()
            self.assertNotIn(str(root / "stale.py"), coverage.measured_files())
            self.assertTrue(coverage.has_arcs())
            self.assertTrue(
                {(2, 3), (2, 4)} <= set(coverage.arcs(str(probe.resolve())))
            )

    def test_corrupt_coverage_fails_closed_but_preserves_timing_report(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from unittest.mock import patch

        from coverage.exceptions import CoverageException

        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            (root / "test_example.py").write_text(
                "import unittest\nclass Case(unittest.TestCase):\n    def test_ok(self): pass\n"
            )
            report = root / "report.json"

            def corrupt_worker(command, **kwargs):
                assigned = json.loads(
                    Path(command[command.index("--worker-manifest") + 1]).read_text()
                )
                Path(command[command.index("--worker-report") + 1]).write_text(
                    json.dumps(
                        {
                            "executed": len(assigned),
                            "fixture_skipped": 0,
                            "inventory_complete": True,
                            "class_seconds": {},
                        }
                    )
                )
                Path(command[command.index("--data-file") + 1]).write_bytes(
                    b"corrupt SQLite data"
                )
                return 0

            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        str(SCRIPT),
                        "--workers",
                        "1",
                        "--start-directory",
                        str(root),
                        "--coverage",
                        "--report",
                        str(report),
                    ],
                ),
                patch.dict(sys.modules),
                patch.object(sys, "path", list(sys.path)),
                patch.object(RUNNER.subprocess, "call", side_effect=corrupt_worker),
                patch(
                    "coverage.Coverage.combine",
                    side_effect=CoverageException("synthetic corrupt coverage data"),
                ),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(RUNNER.main(), 1)
            data = json.loads(report.read_text())
            self.assertFalse(data["complete"])
            self.assertTrue(data["execution_complete"])
            self.assertFalse(data["coverage_ok"])
            self.assertIn("coverage combination failed", data["coverage_error"])
            self.assertEqual(data["workers"][0]["executed"], 1)
            self.assertEqual(data["workers"][0]["exit"], 0)

    def test_damaged_timing_reports_fail_closed_and_still_write_parent_evidence(self):
        payloads = (
            b"{",
            b"\xff",
            b"null",
            b"[]",
            b'{"executed":true,"class_seconds":{}}',
            b'{"executed":1,"class_seconds":{"Case":NaN}}',
            b'{"executed":1,"class_seconds":{},"exit":0}',
            b'{"executed":1,"fixture_skipped":true,"inventory_complete":true,"class_seconds":{}}',
            b'{"executed":1,"fixture_skipped":0,"inventory_complete":"yes","class_seconds":{}}',
        )
        for payload in payloads:
            with (
                self.subTest(payload=payload),
                tempfile.TemporaryDirectory() as scratch,
            ):
                root = Path(scratch)
                (root / "test_damaged.py").write_text(
                    f"""import atexit, pathlib, sys, unittest
if "--worker-report" in sys.argv:
    timing = pathlib.Path(sys.argv[sys.argv.index("--worker-report")+1])
    atexit.register(lambda: timing.write_bytes({payload!r}))
class First(unittest.TestCase):
    def test_first(self): pass
class Second(unittest.TestCase):
    def test_second(self): pass
""",
                    encoding="utf-8",
                )
                report = root / "report.json"
                proc = subprocess.run(
                    [
                        sys.executable,
                        str(SCRIPT),
                        "--workers",
                        "2",
                        "--start-directory",
                        str(root),
                        "--report",
                        str(report),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)
                data = json.loads(report.read_text())
                self.assertFalse(data["complete"])
                self.assertFalse(data["execution_complete"])
                self.assertIsNone(data["coverage_ok"])
                self.assertEqual([worker["exit"] for worker in data["workers"]], [0, 0])
                expected_error = (
                    "Worker timing report unreadable: JSONDecodeError"
                    if payload == b"{"
                    else "Worker timing report unreadable: UnicodeDecodeError"
                    if payload == b"\xff"
                    else "Worker timing report has invalid fields"
                )
                self.assertEqual(
                    [worker["timing_error"] for worker in data["workers"]],
                    [expected_error, expected_error],
                )
                self.assertEqual(data["discovered"], 2)
                self.assertTrue(
                    all(worker["executed"] is None for worker in data["workers"])
                )
                self.assertTrue(
                    all(worker["class_seconds"] == {} for worker in data["workers"])
                )

    def test_empty_coverage_cannot_pass_the_required_report_floor(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            installed = root / "installed" / "review_sensei"
            installed.mkdir(parents=True)
            (installed / "__init__.py").write_text("")
            probe = root / "unrelated.py"
            probe.write_text("pass\n")
            data = root / ".coverage"
            env = {**os.environ, "PYTHONPATH": str(installed.parent)}
            run = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "coverage",
                    "run",
                    "--branch",
                    "--source=review_sensei",
                    "--data-file",
                    str(data),
                    str(probe),
                ],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            report = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "coverage",
                    "report",
                    "--data-file",
                    str(data),
                    "--fail-under=80",
                ],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertNotEqual(report.returncode, 0)

    def test_missing_worker_evidence_and_zero_execution_cannot_pass_coverage(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from unittest.mock import patch

        for fault in (
            "missing-coverage",
            "missing-timing",
            "zero-execution",
            "failed-corrupt-timing",
        ):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as scratch:
                root = Path(scratch)
                (root / "test_example.py").write_text(
                    "import unittest\nclass Case(unittest.TestCase):\n    def test_ok(self): pass\n"
                )
                report = root / "report.json"

                def damaged_worker(command, **kwargs):
                    if fault == "failed-corrupt-timing":
                        Path(command[command.index("--worker-report") + 1]).write_bytes(
                            b"{"
                        )
                    elif fault != "missing-timing":
                        Path(command[command.index("--worker-report") + 1]).write_text(
                            json.dumps(
                                {
                                    "executed": 0 if fault == "zero-execution" else 1,
                                    "fixture_skipped": 0,
                                    "inventory_complete": fault != "zero-execution",
                                    "class_seconds": {},
                                }
                            )
                        )
                    if fault != "missing-coverage":
                        Path(command[command.index("--data-file") + 1]).write_bytes(
                            b"unused because execution is incomplete"
                        )
                    return 7 if fault == "failed-corrupt-timing" else 0

                with (
                    patch.object(
                        sys,
                        "argv",
                        [
                            str(SCRIPT),
                            "--workers",
                            "1",
                            "--start-directory",
                            str(root),
                            "--coverage",
                            "--report",
                            str(report),
                        ],
                    ),
                    patch.dict(sys.modules),
                    patch.object(sys, "path", list(sys.path)),
                    patch.object(RUNNER.subprocess, "call", side_effect=damaged_worker),
                    patch("coverage.Coverage.combine") as combine,
                    redirect_stdout(io.StringIO()),
                    redirect_stderr(io.StringIO()),
                ):
                    self.assertEqual(RUNNER.main(), 1)
                data = json.loads(report.read_text())
                combine.assert_not_called()
                self.assertFalse(data["complete"])
                self.assertFalse(data["coverage_ok"])
                self.assertEqual(
                    data["execution_complete"], fault == "missing-coverage"
                )
                self.assertEqual(
                    data["workers"][0]["exit"],
                    7 if fault == "failed-corrupt-timing" else 0,
                )
                if fault == "missing-timing":
                    self.assertIsNone(data["workers"][0]["executed"])
                if fault == "zero-execution":
                    self.assertEqual(data["workers"][0]["executed"], 0)

    def test_nonstandard_stop_without_start_retains_zero_execution_and_fails(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            (root / "test_protocol.py").write_text("""import unittest
class BrokenProtocol(unittest.TestCase):
    def run(self, result):
        result.stopTest(self)
        return result
    def test_never_started(self): pass
""")
            report = root / "report.json"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--start-directory",
                    str(root),
                    "--report",
                    str(report),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)
            data = json.loads(report.read_text())
            self.assertFalse(data["execution_complete"])
            self.assertEqual(data["workers"][0]["executed"], 0)
            self.assertEqual(data["workers"][0]["exit"], 1)
            self.assertIsNone(data["workers"][0]["timing_error"])

    def test_invalid_cost_files_fail_before_executing_any_workers(self):
        payloads = (
            b"null",
            b"[]",
            b"[1]",
            b"false",
            b"{",
            b'{"Case":"slow"}',
            b'{"Case":true}',
            b'{"Case":NaN}',
        )
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            (root / "test_costs.py").write_text("""import pathlib, unittest
class Case(unittest.TestCase):
    def test_should_not_run(self): pathlib.Path(__file__).with_name("executed").touch()
""")
            costs = root / "costs.json"
            for payload in (*payloads, None):
                with self.subTest(payload=payload):
                    if payload is None:
                        costs.unlink(missing_ok=True)
                    else:
                        costs.write_bytes(payload)
                    proc = subprocess.run(
                        [
                            sys.executable,
                            str(SCRIPT),
                            "--start-directory",
                            str(root),
                            "--costs",
                            str(costs),
                        ],
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    self.assertEqual(proc.returncode, 1, proc.stderr)
                    self.assertNotIn("Traceback", proc.stderr)
                    self.assertFalse((root / "executed").exists())
