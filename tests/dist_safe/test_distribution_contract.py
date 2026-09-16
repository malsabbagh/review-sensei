from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))

from packaging_guard import (  # noqa: E402
    assert_distribution_import,
    is_loaded_from_checkout_src,
)

assert_distribution_import()

CONTRACT_PATH = _TESTS_ROOT / "fixtures" / "distribution-contract.json"


def load_contract() -> dict[str, object]:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


class DistributionContractTests(unittest.TestCase):
    def test_contract_records_supported_command_and_lanes(self) -> None:
        contract = load_contract()
        self.assertEqual(contract["version"], 1)
        self.assertEqual(contract["issue"], 34)
        identification = contract["archive_identification"]
        self.assertEqual(identification["algorithm"], "sha256")
        self.assertEqual(
            identification["command"], "sha256sum dist/*.tar.gz dist/*.whl"
        )
        command = contract["supported_command"]
        self.assertEqual(command["build"], "python -m build --outdir dist")
        self.assertEqual(command["install_wheel"], "python -m pip install dist/*.whl")
        self.assertEqual(
            command["run_dist_safe"],
            "python -I -m unittest discover -s tests/dist_safe -v",
        )
        self.assertEqual(
            command["run_downstream"],
            "python -I -m unittest discover -s tests/downstream -v",
        )
        lanes = contract["lanes"]
        self.assertTrue(lanes["dist_safe"]["packaged"])
        self.assertFalse(lanes["checkout_only"]["packaged"])
        self.assertEqual(
            lanes["dist_safe"]["discover"], ["tests/dist_safe", "tests/downstream"]
        )
        self.assertIn(
            "tests/fake_github_http.py",
            lanes["dist_safe"]["helpers"],
        )
        self.assertIn(
            "tests/test_ci_policy.py",
            lanes["checkout_only"]["modules"],
        )
        reproduction = contract["reproduction"]
        self.assertEqual(len(reproduction["failed_modules"]), 7)
        self.assertIn("unittest discover -s tests -v", reproduction["command"])
        self.assertIn("unavailable helpers/fixtures", reproduction["result"])
        self.assertIn("not engine assertion failures", reproduction["result"])
        canary = contract["canary"]
        self.assertFalse(canary["ci"])
        self.assertFalse(canary["secrets"])
        self.assertEqual(canary["environment"], "downstream-canary")

    def test_contract_file_is_packaged_with_the_dist_safe_helpers(self) -> None:
        self.assertTrue(CONTRACT_PATH.is_file())
        self.assertTrue((_TESTS_ROOT / "packaging_guard.py").is_file())
        self.assertTrue((_TESTS_ROOT / "fake_github_http.py").is_file())
        self.assertTrue((_TESTS_ROOT / "downstream").is_dir())
        if os.environ.get("REVIEWSENSEI_DIST_SAFE_LANE") == "1":
            self.assertFalse((_TESTS_ROOT / "test_ci_policy.py").exists())
            self.assertFalse((_TESTS_ROOT / "test_npm_distribution.py").exists())

    def test_import_guard_detects_checkout_src_layout(self) -> None:
        checkout = Path("/tmp/review-sensei-checkout")
        package = checkout / "src" / "review_sensei" / "__init__.py"
        self.assertTrue(is_loaded_from_checkout_src(package, checkout))
        installed = Path(
            "/tmp/venv/lib/python3.12/site-packages/review_sensei/__init__.py"
        )
        self.assertFalse(is_loaded_from_checkout_src(installed, checkout))

    def test_loaded_package_is_not_checkout_src_when_lane_is_armed(self) -> None:
        package_file = assert_distribution_import()
        self.assertTrue(package_file.is_file())
        if os.environ.get("REVIEWSENSEI_DIST_SAFE_LANE") == "1":
            prefix = Path(sys.prefix).resolve()
            self.assertIn(prefix, package_file.parents)
            checkout = os.environ.get("REVIEWSENSEI_CHECKOUT_ROOT")
            self.assertTrue(checkout)
            self.assertFalse(is_loaded_from_checkout_src(package_file, checkout))


if __name__ == "__main__":
    unittest.main()
