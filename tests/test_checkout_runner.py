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

    def test_discovery_error_empty_suite_and_missing_worker_test_fail_closed(self):
        for source, manifest in (
            ("raise ImportError('synthetic discovery failure')", None),
            ("import unittest", None),
            ("import unittest", ["test_example.Missing.test_case"]),
        ):
            with self.subTest(source=source, manifest=manifest):
                result, _ = self.run_fixture(source, manifest=manifest)
                self.assertEqual(result.returncode, 1)

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
