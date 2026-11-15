"""Historical timing reuse is advisory, complete-accounted and failure-explicit."""

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.refresh_checkout_costs import extract_costs, select_costs
from scripts.run_checkout_tests import partition


def receipt():
    return {
        "inventory_sha256": "a" * 64,
        "discovered": 3,
        "execution_complete": True,
        "complete": True,
        "coverage_ok": None,
        "coverage_error": None,
        "effective_worker_count": 2,
        "controller": None,
        "workers": [
            {
                "assigned": count,
                "executed": count,
                "fixture_skipped": 0,
                "exit": 0,
                "elapsed_seconds": 4,
                "inventory_complete": True,
                "timing_error": None,
                "class_seconds": {name: seconds},
            }
            for name, seconds, count in (("m.Slow", 3.0, 2), ("m.Fast", 0.0, 1))
        ],
    }


class CheckoutTimingRefreshTests(unittest.TestCase):
    def test_complete_costs_preserve_all_ids_and_intact_classes(self):
        costs = extract_costs(receipt())
        self.assertEqual(costs, {"m.Fast": 0.000001, "m.Slow": 3.0})
        ids = ["m.Slow.test_a", "m.Slow.test_b", "m.Fast.test_c"]
        groups = partition(ids, 2, costs)
        self.assertEqual(sorted(sum(groups, [])), sorted(ids))
        self.assertIn(ids[:2], groups)

    def test_failed_complete_execution_requires_explicit_timing_opt_in(self):
        data = receipt()
        data["workers"][0]["exit"] = 1
        with self.assertRaisesRegex(ValueError, "allow-failing-tests"):
            extract_costs(data)
        self.assertEqual(
            extract_costs(data, allow_failing_tests=True), extract_costs(receipt())
        )
        self.assertEqual(data["workers"][0]["exit"], 1)

    def test_controller_and_fixture_skip_accounting_are_preserved(self):
        data = receipt()
        data["controller"] = data["workers"].pop()
        data["effective_worker_count"] = 1
        data["workers"][0].update(executed=1, fixture_skipped=1)
        self.assertEqual(extract_costs(data), extract_costs(receipt()))

    def test_incomplete_or_malformed_top_level_receipts_are_rejected(self):
        for key, value in (
            ("execution_complete", False),
            ("complete", False),
            ("coverage_ok", False),
            ("coverage_ok", 1),
            ("coverage_error", "missing"),
            ("discovered", 2),
            ("discovered", True),
            ("inventory_sha256", "not-a-digest"),
            ("effective_worker_count", 3),
            ("workers", []),
        ):
            with self.subTest(key=key, value=value):
                data = receipt()
                data[key] = value
                with self.assertRaises(ValueError):
                    extract_costs(data, allow_failing_tests=True)

    def test_incomplete_or_malformed_worker_receipts_are_rejected(self):
        for key, value in (
            ("inventory_complete", False),
            ("timing_error", "damaged"),
            ("assigned", 0),
            ("executed", None),
            ("executed", True),
            ("fixture_skipped", -1),
            ("exit", False),
            ("elapsed_seconds", float("inf")),
            ("elapsed_seconds", 10**1000),
            ("class_seconds", {"m.Slow": float("nan")}),
            ("class_seconds", {"m.Slow": -1}),
            ("class_seconds", {"m.Slow": True}),
            ("class_seconds", {"": 2}),
        ):
            with self.subTest(key=key, value=value):
                data = receipt()
                data["workers"][0][key] = value
                with self.assertRaises(ValueError):
                    extract_costs(data, allow_failing_tests=True)
        data = receipt()
        data["controller"] = data["workers"].pop()
        data["controller"]["class_seconds"] = {"m.Slow": 1}
        data["effective_worker_count"] = 1
        with self.assertRaisesRegex(ValueError, "multiple"):
            extract_costs(data)

    def test_a_class_split_across_shard_workers_costs_the_sum(self):
        data = receipt()
        data["workers"][1]["class_seconds"] = {"m.Slow": 1.5}
        self.assertEqual(extract_costs(data), {"m.Slow": 4.5})
        data["workers"][1]["class_seconds"] = {"m.Slow": sys.float_info.max}
        data["workers"][0]["class_seconds"] = {"m.Slow": sys.float_info.max}
        with self.assertRaisesRegex(ValueError, "finite"):
            extract_costs(data)

    def test_obsolete_hints_do_not_remove_unknown_tests(self):
        costs = extract_costs(receipt())
        selected, obsolete, unknown = select_costs(costs, ["m.Slow", "m.New"])
        self.assertEqual(selected, {"m.Slow": 3.0})
        self.assertEqual(obsolete, ["m.Fast"])
        self.assertEqual(unknown, ["m.New"])
        ids = ["m.Slow.test_a", "m.Slow.test_b", "m.New.test_c"]
        self.assertEqual(sorted(sum(partition(ids, 2, selected), [])), sorted(ids))
        self.assertEqual(costs, extract_costs(receipt()))
        for invalid in (None, [], [""], ["m.Slow", "m.Slow"], [1]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                select_costs(costs, invalid)

    def test_cli_rejects_failed_receipts_before_overwriting_then_reports_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = receipt()
            data["workers"][0]["exit"] = 1
            source, classes, output = (root / name for name in ("in", "classes", "out"))
            source.write_text(json.dumps(data), encoding="utf-8")
            classes.write_text(json.dumps(["m.Slow", "m.Fast"]), encoding="utf-8")
            output.write_text("retained", encoding="utf-8")
            script = (
                Path(__file__).resolve().parents[1]
                / "scripts/refresh_checkout_costs.py"
            )
            command = [
                sys.executable,
                str(script),
                str(source),
                "--classes",
                str(classes),
                "--output",
                str(output),
            ]
            refused = subprocess.run(
                command, capture_output=True, text=True, timeout=30
            )
            self.assertNotEqual(refused.returncode, 0)
            self.assertEqual(output.read_text(), "retained")
            accepted = subprocess.run(
                [*command, "--allow-failing-tests"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertFalse(json.loads(accepted.stdout)["tests_passed"])
            self.assertEqual(
                json.loads(output.read_text()),
                extract_costs(data, allow_failing_tests=True),
            )

    def test_inputs_are_not_mutated(self):
        data = receipt()
        original = copy.deepcopy(data)
        extract_costs(data)
        self.assertEqual(data, original)
