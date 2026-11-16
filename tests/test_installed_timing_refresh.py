"""Offline installed timing hints preserve complete accounting and failure facts."""

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.refresh_installed_costs import extract_costs, load_receipt, summary

ROOT = Path(__file__).resolve().parents[1]


def receipt():
    return {
        "inventory_sha256": "a" * 64,
        "discovered": 3,
        "modules": 2,
        "units": 3,
        "uncosted_units": ["b.py"],
        "unused_costs": ["old.py"],
        "complete": True,
        "hook_mode": "reviewed-local",
        "fallback_reason": None,
        "elapsed_seconds": 5.3,
        "workers": [
            {
                "worker": 0,
                "exit": 0,
                "elapsed_seconds": 5.2,
                "assigned": 2,
                "started": 2,
                "fixture_skipped": 0,
                "skip_events": 0,
                "execution_complete": True,
                "module_seconds": {"a.py": 5.0},
                "unit_seconds": {"a.py::first": 3.0, "a.py::second": 2.0},
            },
            {
                "worker": 1,
                "exit": 0,
                "elapsed_seconds": 4.2,
                "assigned": 1,
                "started": 1,
                "fixture_skipped": 0,
                "skip_events": 0,
                "execution_complete": True,
                "module_seconds": {"b.py": 4.0},
                "unit_seconds": {"b.py": 4.0},
            },
        ],
    }


class InstalledTimingRefreshTests(unittest.TestCase):
    def test_complete_receipt_keeps_every_unit_and_does_not_mutate_inputs(self):
        data = receipt()
        before = copy.deepcopy(data)
        self.assertEqual(
            extract_costs(data), {"a.py::first": 3.0, "a.py::second": 2.0, "b.py": 4.0}
        )
        self.assertEqual(data, before)

    def test_failed_complete_execution_requires_opt_in_and_preserves_failure(self):
        data = receipt()
        data["workers"][0]["exit"] = 1
        data["complete"] = False
        with self.assertRaisesRegex(ValueError, "allow-failing-tests"):
            extract_costs(data)
        self.assertEqual(
            extract_costs(data, allow_failing_tests=True), extract_costs(receipt())
        )
        facts = summary(data, b"exact archived bytes")
        self.assertFalse(facts["complete"])
        self.assertFalse(facts["tests_passed"])
        self.assertEqual(facts["worker_exits"], [1, 0])
        self.assertTrue(facts["all_workers_execution_complete"])
        self.assertTrue(facts["scheduling_only"])

    def test_fixture_skip_accounting_does_not_discard_measured_units(self):
        data = receipt()
        data["discovered"] = 4
        data["workers"][0].update(assigned=3, fixture_skipped=1, skip_events=1)
        self.assertEqual(extract_costs(data), extract_costs(receipt()))

    def test_started_test_skip_is_not_an_unstarted_fixture_skip(self):
        data = receipt()
        data["workers"][0]["skip_events"] = 1
        self.assertEqual(data["workers"][0]["started"], data["workers"][0]["assigned"])
        self.assertEqual(data["workers"][0]["fixture_skipped"], 0)
        self.assertEqual(extract_costs(data), extract_costs(receipt()))

    def test_multiple_subtest_skips_can_exceed_assigned_test_count(self):
        data = receipt()
        data["workers"][0]["skip_events"] = 5
        self.assertGreater(
            data["workers"][0]["skip_events"], data["workers"][0]["assigned"]
        )
        self.assertEqual(extract_costs(data), extract_costs(receipt()))

    def test_incomplete_or_malformed_top_level_data_is_rejected(self):
        for key, value in (
            ("inventory_sha256", "not a digest"),
            ("discovered", True),
            ("discovered", 4),
            ("modules", 3),
            ("units", 4),
            ("workers", []),
            ("elapsed_seconds", 1),
            ("elapsed_seconds", float("nan")),
            ("complete", False),
            ("complete", 1),
            ("hook_mode", "unknown"),
            ("fallback_reason", "unexpected"),
            ("uncosted_units", ["missing.py"]),
            ("unused_costs", ["old.py", "old.py"]),
            ("unused_costs", [True]),
        ):
            with self.subTest(key=key, value=value):
                data = receipt()
                data[key] = value
                with self.assertRaises(ValueError):
                    extract_costs(data, allow_failing_tests=True)
        data = receipt()
        data["unrecognized"] = "cannot silently ignore a changed receipt schema"
        with self.assertRaises(ValueError):
            extract_costs(data)

    def test_incomplete_or_malformed_worker_data_is_rejected(self):
        for key, value in (
            ("worker", 1),
            ("worker", False),
            ("exit", False),
            ("execution_complete", False),
            ("assigned", 3),
            ("started", 1),
            ("started", True),
            ("fixture_skipped", -1),
            ("skip_events", -1),
            ("skip_events", True),
            ("skip_events", 1.0),
            ("elapsed_seconds", float("inf")),
            ("elapsed_seconds", 10**1000),
            ("elapsed_seconds", 4),
            ("module_seconds", {"a.py": 5.1}),
            ("module_seconds", {"other.py": 5}),
            ("unit_seconds", {}),
        ):
            with self.subTest(key=key, value=value):
                data = receipt()
                data["workers"][0][key] = value
                with self.assertRaises(ValueError):
                    extract_costs(data, allow_failing_tests=True)
        data = receipt()
        del data["workers"][0]["started"]
        with self.assertRaises(ValueError):
            extract_costs(data)

    def test_invalid_costs_and_duplicate_or_partial_units_are_rejected(self):
        for value in (-1, True, float("nan"), float("inf"), 10**1000):
            with self.subTest(value=value):
                data = receipt()
                data["workers"][0]["unit_seconds"]["a.py::first"] = value
                with self.assertRaises(ValueError):
                    extract_costs(data)
        for name in ("", "a.py::", "a.py::one..two", "../a.py", "a.py::one::two"):
            with self.subTest(name=name):
                data = receipt()
                data["workers"][0]["unit_seconds"] = {name: 5}
                with self.assertRaises(ValueError):
                    extract_costs(data)
        data = receipt()
        data["workers"][1]["unit_seconds"] = {"a.py::first": 4}
        data["workers"][1]["module_seconds"] = {"a.py": 4}
        with self.assertRaisesRegex(ValueError, "multiple workers"):
            extract_costs(data)
        data = receipt()
        del data["workers"][0]["unit_seconds"]["a.py::second"]
        data["workers"][0]["module_seconds"]["a.py"] = 3
        with self.assertRaisesRegex(ValueError, "every discovered"):
            extract_costs(data)

    def test_zero_measurements_are_preserved_without_invented_precision(self):
        data = receipt()
        data["workers"][1]["unit_seconds"]["b.py"] = 0
        data["workers"][1]["module_seconds"]["b.py"] = 0
        self.assertEqual(extract_costs(data)["b.py"], 0)

    def test_duplicate_json_keys_cannot_hide_conflicting_facts(self):
        for document in (
            '{"complete": false, "complete": true}',
            '{"unit_seconds": {"a.py": 2, "a.py": 3}}',
        ):
            with (
                self.subTest(document=document),
                self.assertRaisesRegex(ValueError, "Duplicate"),
            ):
                load_receipt(document)

    def test_cli_validates_before_overwriting_and_reports_failure_explicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / "receipt.json", root / "costs.json"
            data = receipt()
            data["workers"][0]["exit"] = 1
            data["complete"] = False
            source.write_text(json.dumps(data), encoding="utf-8")
            output.write_text("retained", encoding="utf-8")
            command = [
                sys.executable,
                str(ROOT / "scripts/refresh_installed_costs.py"),
                str(source),
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
            self.assertEqual(
                json.loads(output.read_text()),
                extract_costs(data, allow_failing_tests=True),
            )
            facts = json.loads(accepted.stdout)
            self.assertFalse(facts["tests_passed"])
            self.assertEqual(
                facts["timing_json_sha256"],
                hashlib.sha256(source.read_bytes()).hexdigest(),
            )
            original = source.read_bytes()
            same = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/refresh_installed_costs.py"),
                    str(source),
                    "--output",
                    str(source),
                    "--allow-failing-tests",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertNotEqual(same.returncode, 0)
            self.assertEqual(source.read_bytes(), original)
            output.write_text("retained", encoding="utf-8")
            source.write_text('{"complete": true, "complete": false}', encoding="utf-8")
            duplicate = subprocess.run(
                [*command, "--allow-failing-tests"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertNotEqual(duplicate.returncode, 0)
            self.assertEqual(output.read_text(), "retained")

    def test_committed_hints_keep_historical_provenance_and_no_fake_method_times(self):
        fixture = ROOT / "tests/fixtures/installed-unit-seconds.json"
        costs = load_receipt(fixture.read_bytes())
        facts = json.loads(
            (ROOT / "tests/fixtures/installed-timing-source.json").read_text()
        )
        self.assertEqual(len(costs), 68)
        self.assertEqual(facts["measured_units"], 68)
        self.assertEqual(facts["discovered"], 1139)
        self.assertEqual(facts["artifact_id"], 14131367317)
        self.assertEqual(facts["worker_exits"], [1, 1, 1, 1])
        self.assertFalse(facts["complete"])
        self.assertFalse(facts["tests_passed"])
        self.assertEqual(
            facts["cost_fixture_sha256"],
            hashlib.sha256(
                fixture.read_text(encoding="utf-8").encode("utf-8")
            ).hexdigest(),
        )
        self.assertTrue(
            facts["cost_fixture_sha256_encoding"].startswith("Canonical-LF UTF-8")
        )
        self.assertEqual(
            costs["test_quarantine_contract.py::test_quarantine_checkpoint"],
            7478.680748065999,
        )
        self.assertFalse(any("test_quarantine_checkpoint." in key for key in costs))

    def test_crlf_checkout_preserves_costs_and_canonical_fixture_hash(self):
        fixture = ROOT / "tests/fixtures/installed-unit-seconds.json"
        canonical = fixture.read_text(encoding="utf-8").encode("utf-8")
        facts = json.loads(
            (ROOT / "tests/fixtures/installed-timing-source.json").read_text(
                encoding="utf-8"
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory) / "installed-unit-seconds.json"
            checkout.write_bytes(canonical.replace(b"\n", b"\r\n"))
            self.assertNotEqual(checkout.read_bytes(), canonical)
            self.assertEqual(
                load_receipt(checkout.read_bytes()), load_receipt(canonical)
            )
            self.assertEqual(
                hashlib.sha256(
                    checkout.read_text(encoding="utf-8").encode("utf-8")
                ).hexdigest(),
                facts["cost_fixture_sha256"],
            )
        self.assertEqual(
            facts["timing_json_sha256"],
            "0bab73ad0e5e83a8bdbaa7317337bfbc78f8992212c5df30a7fe7b73e2acc2eb",
        )


if __name__ == "__main__":
    unittest.main()
