import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CodeQLFindingsGateTests(unittest.TestCase):
    def _sarif(
        self, root: Path, *, level: str = "none", name: str = "python.sarif"
    ) -> Path:
        path = root / name
        result = []
        if level != "none":
            result = [
                {
                    "ruleId": "py/test",
                    "level": level,
                    "message": {"text": "fixture finding"},
                    "locations": [
                        {
                            "physicalLocation": {
                                "artifactLocation": {"uri": "src/example.py"},
                                "region": {"startLine": 3},
                            }
                        }
                    ],
                }
            ]
        path.write_text(
            json.dumps(
                {
                    "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
                    "version": "2.1.0",
                    "runs": [
                        {
                            "tool": {"driver": {"name": "CodeQL", "rules": []}},
                            "results": result,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_clean_report_passes_and_finding_fails(self):
        module = _load_script("check_codeql_findings.py")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = ROOT / "security" / "codeql-baseline.json"
            self._sarif(root)
            self._sarif(root, name="javascript-typescript.sarif")
            self.assertEqual(module.evaluate(root, baseline, expected_reports=2), [])
            self._sarif(root, level="warning")
            self.assertEqual(
                len(module.evaluate(root, baseline, expected_reports=2)), 1
            )

    def test_missing_or_malformed_report_fails_closed(self):
        module = _load_script("check_codeql_findings.py")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = ROOT / "security" / "codeql-baseline.json"
            self.assertTrue(module.evaluate(root, baseline))
            (root / "broken.sarif").write_text("{}", encoding="utf-8")
            self.assertTrue(module.evaluate(root, baseline))


class ProtectionPolicyTests(unittest.TestCase):
    def test_checked_in_policy_is_valid(self):
        module = _load_script("check_protection_policy.py")
        policy = module.load_object(ROOT / ".github" / "protection-policy.json")
        self.assertEqual(module.validate_policy(policy), [])

    def test_drifted_readback_is_rejected(self):
        module = _load_script("check_protection_policy.py")
        policy = module.load_object(ROOT / ".github" / "protection-policy.json")
        readback = {
            "rules": [],
            "bypass_actors": [{"bypass_mode": "always"}],
        }
        errors = module.compare_readback(policy, readback)
        self.assertTrue(any("Required checks" in error for error in errors))
        self.assertTrue(any("blanket" in error for error in errors))
