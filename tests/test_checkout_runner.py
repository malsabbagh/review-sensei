"""The parallel checkout lane must retain exhaustive discovery and failures."""

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/run_checkout_tests.py"
# Checkout-only: the sdist contract excludes this module from installed lanes.
SPEC = importlib.util.spec_from_file_location("checkout_runner", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


class CheckoutRunnerTests(unittest.TestCase):
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
        self.assertEqual(groups, RUNNER.partition(identities[::-1], 2, costs))
        for invalid in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                RUNNER.partition(identities, 2, {"module.Case0": invalid})

    def test_worker_coverage_combines_both_branches_and_keeps_failure_evidence(self):
        from coverage import CoverageData

        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            package = root / "review_sensei"
            package.mkdir()
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
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertIn("intentional worker failure", proc.stderr)
            data = json.loads(report.read_text())
            self.assertTrue(data["complete"])
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
                    json.dumps({"executed": len(assigned), "class_seconds": {}})
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
            self.assertIn("coverage combination failed", data["coverage_error"])
            self.assertEqual(data["workers"][0]["executed"], 1)
            self.assertEqual(data["workers"][0]["exit"], 0)
