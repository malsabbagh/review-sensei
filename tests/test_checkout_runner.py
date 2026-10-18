"""The parallel checkout lane must retain exhaustive discovery and failures."""

import importlib.util
import json
import os
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
                self.assertTrue(
                    all(worker["timing_error"] for worker in data["workers"])
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

        for fault in ("missing-coverage", "missing-timing", "zero-execution"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as scratch:
                root = Path(scratch)
                (root / "test_example.py").write_text(
                    "import unittest\nclass Case(unittest.TestCase):\n    def test_ok(self): pass\n"
                )
                report = root / "report.json"

                def damaged_worker(command, **kwargs):
                    if fault != "missing-timing":
                        Path(command[command.index("--worker-report") + 1]).write_text(
                            json.dumps(
                                {
                                    "executed": 0 if fault == "zero-execution" else 1,
                                    "class_seconds": {},
                                }
                            )
                        )
                    if fault != "missing-coverage":
                        Path(command[command.index("--data-file") + 1]).write_bytes(
                            b"unused because execution is incomplete"
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
                self.assertEqual(data["workers"][0]["exit"], 0)
                if fault == "missing-timing":
                    self.assertIsNone(data["workers"][0]["executed"])
                if fault == "zero-execution":
                    self.assertEqual(data["workers"][0]["executed"], 0)
