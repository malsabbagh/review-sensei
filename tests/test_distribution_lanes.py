from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = json.loads(
    (ROOT / "tests" / "fixtures" / "distribution-contract.json").read_text(
        encoding="utf-8"
    )
)


class DistributionLaneTests(unittest.TestCase):
    def test_checkout_only_modules_exist_and_are_excluded_from_the_contract_lane(self):
        modules = CONTRACT["lanes"]["checkout_only"]["modules"]
        self.assertTrue(modules)
        for relative in modules:
            with self.subTest(relative=relative):
                self.assertTrue((ROOT / relative).is_file())
        for relative in CONTRACT["lanes"]["dist_safe"]["helpers"]:
            with self.subTest(helper=relative):
                self.assertTrue((ROOT / relative).is_file())
        for relative in CONTRACT["lanes"]["dist_safe"]["discover"]:
            with self.subTest(discover=relative):
                self.assertTrue((ROOT / relative).is_dir())

    def test_ci_and_manifest_match_the_recorded_distribution_contract(self):
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        command = CONTRACT["supported_command"]
        self.assertIn(command["build"], ci)
        self.assertIn(command["record_digests"], ci)
        self.assertIn("pip install dist/*.whl", ci)
        self.assertIn('unittest discover -s "$suite/dist_safe" -v', ci)
        self.assertIn('unittest discover -s "$suite/downstream" -v', ci)
        self.assertIn("prune tests", manifest)
        self.assertIn("graft tests/dist_safe", manifest)
        self.assertIn("graft tests/downstream", manifest)
        canary = ROOT / CONTRACT["canary"]["workflow"]
        self.assertTrue(canary.is_file())
        canary_text = canary.read_text(encoding="utf-8")
        self.assertIn(CONTRACT["canary"]["environment"], canary_text)


if __name__ == "__main__":
    unittest.main()
