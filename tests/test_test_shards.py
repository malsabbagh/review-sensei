"""Sharded runners must execute one complete inventory exactly once."""

import importlib.util
import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKOUT = ROOT / "scripts" / "run_checkout_tests.py"
INSTALLED = ROOT / "scripts" / "run_installed_test_suites.py"
VERIFIER = ROOT / "scripts" / "verify_test_shards.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


RUNNER = _load("checkout_runner_for_shards", CHECKOUT)
VERIFY = _load("shard_verifier_for_tests", VERIFIER)

SUITE = {
    "test_alpha.py": """\
        import os, unittest
        class Heavy(unittest.TestCase):
            def test_1(self): pass
            def test_2(self): pass
            def test_3(self): pass
            def test_4(self): pass
            def test_5(self):
                self.assertNotEqual(os.environ.get("SHARD_TEST_FAIL"), "1")
            def test_6(self): pass
        class Light(unittest.TestCase):
            def test_a(self): pass
        """,
    "test_beta.py": """\
        import unittest
        class Fixtured(unittest.TestCase):
            @classmethod
            def setUpClass(cls): cls.ready = True
            def test_x(self): self.assertTrue(self.ready)
            def test_y(self): self.assertTrue(self.ready)
            def test_z(self): self.assertTrue(self.ready)
        """,
    "test_gamma.py": """\
        import unittest
        def setUpModule(): pass
        class ModuleFixture(unittest.TestCase):
            def test_m1(self): pass
            def test_m2(self): pass
        """,
    "test_hooked.py": """\
        import unittest
        INSTALLED_SUITE_HOOK_CONTRACT = ("reproducible-module-local-v1", ())
        def load_tests(loader, tests, pattern):
            return tests
        class Hooked(unittest.TestCase):
            def test_h1(self): pass
            def test_h2(self): pass
        """,
}
COSTS = {
    "test_alpha.Heavy": 60.0,
    "test_alpha.Light": 1.0,
    "test_beta.Fixtured": 30.0,
    "test_gamma.ModuleFixture": 30.0,
}


class ShardPlanTests(unittest.TestCase):
    def test_shard_argument_is_bounded(self):
        self.assertEqual(RUNNER.parse_shard("2/3"), (2, 3))
        for value in ("0/2", "3/2", "1/17", "1/0", "a/b", "1/2/3", "01/2", " 1/2"):
            with self.subTest(value=value), self.assertRaises(Exception):
                RUNNER.parse_shard(value)

    def test_only_fixture_free_classes_are_splittable(self):
        with tempfile.TemporaryDirectory() as scratch:
            directory = Path(scratch)
            for name, source in SUITE.items():
                (directory / name).write_text(textwrap.dedent(source), "utf-8")
            sys.path.insert(0, str(directory))
            try:
                suite = unittest.TestLoader().discover(str(directory))
                splittable = RUNNER.fixture_free_classes(suite)
            finally:
                sys.path.remove(str(directory))
                for name in SUITE:
                    sys.modules.pop(name.removesuffix(".py"), None)
        self.assertIn("test_alpha.Heavy", splittable)
        self.assertIn("test_alpha.Light", splittable)
        self.assertNotIn("test_beta.Fixtured", splittable)
        self.assertNotIn("test_gamma.ModuleFixture", splittable)

    def test_method_splitting_preserves_inventory_and_fixture_classes(self):
        ids = [
            *(f"test_alpha.Heavy.test_{n}" for n in range(1, 7)),
            "test_alpha.Light.test_a",
            *(f"test_beta.Fixtured.test_{n}" for n in "xyz"),
            *(f"test_gamma.ModuleFixture.test_m{n}" for n in (1, 2)),
        ]
        intact = RUNNER.partition(ids, 4, COSTS)
        split = RUNNER.partition(
            ids,
            4,
            COSTS,
            splittable=frozenset({"test_alpha.Heavy", "test_alpha.Light"}),
        )
        for groups in (intact, split):
            self.assertEqual(sorted(sum(groups, [])), sorted(ids))
            for prefix in ("test_beta.Fixtured.", "test_gamma.ModuleFixture."):
                owners = [g for g in groups if any(i.startswith(prefix) for i in g)]
                self.assertEqual(len(owners), 1, prefix)
        heavy = [g for g in split if any("Heavy" in i for i in g)]
        self.assertGreater(len(heavy), 1)
        self.assertEqual(
            split,
            RUNNER.partition(
                ids,
                4,
                COSTS,
                splittable=frozenset({"test_alpha.Heavy", "test_alpha.Light"}),
            ),
        )
        with self.assertRaises(ValueError):
            RUNNER.partition(ids, 17, COSTS)
        self.assertEqual(
            sorted(sum(RUNNER.partition(ids, 17, COSTS, limit=32), [])), sorted(ids)
        )


class ShardExecutionTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.directory = Path(self.scratch.name)
        self.suite = self.directory / "suite"
        self.suite.mkdir()
        for name, source in SUITE.items():
            (self.suite / name).write_text(textwrap.dedent(source), "utf-8")
        self.costs = self.directory / "costs.json"
        self.costs.write_text(json.dumps(COSTS), "utf-8")

    def run_checkout(self, index, count, *, environment=None, reviewed=True):
        report = self.directory / f"checkout-{index}-{count}.json"
        command = [
            sys.executable,
            str(CHECKOUT),
            "--workers",
            "2",
            *(["--controller-module", "test_hooked"] if reviewed else []),
            "--start-directory",
            str(self.suite),
            "--costs",
            str(self.costs),
            "--shard",
            f"{index}/{count}",
            "--report",
            str(report),
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=self.directory,
            env={**_environment(), **(environment or {})},
        )
        return result, report

    def verify(self, reports, count):
        merged = self.directory / f"merged-{count}.json"
        result = subprocess.run(
            [
                sys.executable,
                str(VERIFIER),
                "--shards",
                str(count),
                "--report",
                str(merged),
                *map(str, reports),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return result, merged

    def test_checkout_shards_cover_the_inventory_once_with_one_controller(self):
        reports = []
        for index in (1, 2, 3):
            result, report = self.run_checkout(index, 3)
            self.assertEqual(result.returncode, 0, result.stderr)
            reports.append(report)
        data = [json.loads(path.read_text("utf-8")) for path in reports]
        self.assertEqual(len({item["shard"]["plan_sha256"] for item in data}), 1)
        self.assertIsNotNone(data[0]["controller"])
        self.assertTrue(all(item["controller"] is None for item in data[1:]))
        self.assertEqual(data[0]["controller"]["assigned"], 2)
        executed = [
            identity
            for item in data
            for worker in item["workers"]
            for identity in worker.get("identities", [])
        ]
        self.assertEqual(len(executed), len(set(executed)))
        result, merged = self.verify(reports, 3)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(merged.read_text("utf-8"))
        self.assertEqual(receipt["discovered"], 14)
        self.assertTrue(receipt["complete"])
        self.assertTrue(receipt["execution_complete"])
        self.assertEqual(receipt["effective_worker_count"], len(receipt["workers"]))
        self.assertIn(
            "14 discovered tests accounted for across 3 shards", result.stdout
        )

        refresh = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "refresh_checkout_costs.py"),
                str(merged),
                "--classes",
                str(self._classes()),
                "--output",
                str(self.directory / "refreshed.json"),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=ROOT,
        )
        self.assertEqual(refresh.returncode, 0, refresh.stderr)
        refreshed = json.loads((self.directory / "refreshed.json").read_text("utf-8"))
        self.assertEqual(set(refreshed), set(COSTS) | {"test_hooked.Hooked"})

        for subset, message in (
            (reports[:2], "expected shards 1..3"),
            ([reports[0], reports[0], reports[2]], "expected shards 1..3"),
        ):
            with self.subTest(message=message):
                result, _merged = self.verify(subset, 3)
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)
        result, _merged = self.verify(reports, 2)
        self.assertEqual(result.returncode, 1)

    def test_shards_from_different_plans_or_failures_fail_the_lane(self):
        failing = {"SHARD_TEST_FAIL": "1"}
        first, report_1 = self.run_checkout(1, 2, environment=failing)
        second, report_2 = self.run_checkout(2, 2, environment=failing)
        self.assertEqual(sorted([first.returncode, second.returncode]), [0, 1])
        result, merged = self.verify([report_1, report_2], 2)
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAILED", result.stdout)
        # Failing tests keep complete accounting; only the outcome fails.
        self.assertTrue(json.loads(merged.read_text("utf-8"))["complete"])

        self.costs.write_text(json.dumps({**COSTS, "test_alpha.Light": 500.0}), "utf-8")
        _result, other_plan = self.run_checkout(2, 2)
        result, _merged = self.verify([report_1, other_plan], 2)
        self.assertEqual(result.returncode, 1)
        self.assertIn("disagree on plan", result.stderr)

        damaged = json.loads(report_1.read_text("utf-8"))
        damaged["shard"]["assigned"] += 1
        report_1.write_text(json.dumps(damaged), "utf-8")
        result, _merged = self.verify([report_1, report_2], 2)
        self.assertEqual(result.returncode, 1)

    def test_unreviewed_hooks_run_the_original_root_in_shard_one_only(self):
        reports = []
        for index in (1, 2, 3):
            result, report = self.run_checkout(index, 3, reviewed=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            reports.append(report)
        data = [json.loads(path.read_text("utf-8")) for path in reports]
        self.assertEqual({item["mode"] for item in data}, {"original-root"})
        self.assertEqual([item["shard"]["assigned"] for item in data], [14, 0, 0])
        self.assertEqual([len(item["workers"]) for item in data], [1, 0, 0])
        result, _merged = self.verify(reports, 3)
        self.assertEqual(result.returncode, 0, result.stderr)

    def run_installed(self, index, count, *, reviewed=True):
        report = self.directory / f"installed-{index}-{count}.json"
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                str(INSTALLED),
                "--start-directory",
                str(self.suite),
                "--workers",
                "2",
                "--split-module",
                "test_alpha.py",
                *(["--reviewed-local-hook", "test_hooked"] if reviewed else []),
                "--shard",
                f"{index}/{count}",
                "--report",
                str(report),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=self.directory,
        )
        return result, report

    def test_installed_shards_cover_the_inventory_once(self):
        reports = []
        for index in (1, 2):
            result, report = self.run_installed(index, 2)
            self.assertEqual(result.returncode, 0, result.stderr)
            reports.append(report)
        data = [json.loads(path.read_text("utf-8")) for path in reports]
        self.assertEqual({item["hook_mode"] for item in data}, {"reviewed-local"})
        self.assertEqual(sum(item["shard"]["assigned"] for item in data), 14)
        result, merged = self.verify(reports, 2)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(merged.read_text("utf-8"))
        self.assertEqual(receipt["discovered"], 14)
        self.assertTrue(receipt["complete"])

        reports = []
        for index in (1, 2, 3):
            result, report = self.run_installed(index, 3, reviewed=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            reports.append(report)
        self.assertIn("shard 1 runs the original root", result.stderr)
        data = [json.loads(path.read_text("utf-8")) for path in reports]
        self.assertEqual({item["hook_mode"] for item in data}, {"original-root"})
        self.assertEqual([item["shard"]["assigned"] for item in data], [14, 0, 0])
        result, _merged = self.verify(reports, 3)
        self.assertEqual(result.returncode, 0, result.stderr)

    def _classes(self):
        path = self.directory / "classes.json"
        path.write_text(json.dumps([*COSTS, "test_hooked.Hooked"]), "utf-8")
        return path


def _environment():
    import os

    environment = dict(os.environ)
    environment.pop("SHARD_TEST_FAIL", None)
    return environment


if __name__ == "__main__":
    unittest.main()
