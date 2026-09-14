import importlib.util
import json
import tempfile
import unittest
from datetime import date
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
        self,
        root: Path,
        *,
        level: str = "none",
        name: str = "python.sarif",
        language: str | None = None,
        properties: dict[str, object] | None = None,
        automation_id: str | None = None,
        driver_name: str = "CodeQL",
        organization: str = "GitHub",
        runs: list[dict[str, object]] | None = None,
    ) -> Path:
        path = root / name
        if language is None:
            language = {
                "javascript": "javascript-typescript",
                "typescript": "javascript-typescript",
                "javascript-typescript": "javascript-typescript",
                "python": "python",
            }.get(path.stem.casefold())
        result = []
        if level != "none":
            rule_id = "js/test" if language == "javascript-typescript" else "py/test"
            result = [
                {
                    "ruleId": rule_id,
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
        if runs is None:
            run: dict[str, object] = {
                "tool": {
                    "driver": {
                        "name": driver_name,
                        "organization": organization,
                        "rules": [],
                    }
                },
                "results": result,
            }
            if language is not None:
                run["properties"] = properties or {"language": language}
                run["automationDetails"] = {
                    "id": automation_id or f"/language:{language}"
                }
            runs = [run]
        path.write_text(
            json.dumps(
                {
                    "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
                    "version": "2.1.0",
                    "runs": runs,
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

    def test_partial_fingerprint_survives_location_changes(self):
        module = _load_script("check_codeql_findings.py")
        first = {
            "ruleId": "py/test",
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": "src/old.py"},
                        "region": {"startLine": 3},
                    }
                }
            ],
            "partialFingerprints": {"primaryLocationLineHash": "abc123"},
        }
        moved = {
            **first,
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": "src/new.py"},
                        "region": {"startLine": 99},
                    }
                }
            ],
        }
        self.assertEqual(
            module.finding_fingerprint(first), module.finding_fingerprint(moved)
        )
        self.assertNotEqual(
            module.finding_fingerprint(first),
            module.finding_fingerprint({**first, "ruleId": "js/test"}),
        )

    def test_codeql_rejects_invalid_severity_and_partial_fingerprints(self):
        module = _load_script("check_codeql_findings.py")
        with self.assertRaises(ValueError):
            module._result_severity(
                {"ruleId": "py/test", "level": "warning"},
                {"py/test": {"properties": {"security-severity": "NaN"}}},
            )
        with self.assertRaises(ValueError):
            module.finding_fingerprint(
                {
                    "ruleId": "py/test",
                    "locations": [],
                    "partialFingerprints": {"hash": ""},
                }
            )
        with self.assertRaises(ValueError):
            module.finding_fingerprint(
                {
                    "ruleId": "py/test",
                    "locations": [],
                    "partialFingerprints": {},
                }
            )

    def test_expected_language_reports_and_current_day_expiry_fail_closed(self):
        module = _load_script("check_codeql_findings.py")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = ROOT / "security" / "codeql-baseline.json"
            self._sarif(root, name="python.sarif")
            self._sarif(root, name="javascript-typescript.sarif")
            self.assertEqual(
                module.evaluate(
                    root,
                    baseline,
                    expected_reports=2,
                    expected_languages=("python", "javascript-typescript"),
                ),
                [],
            )
            self.assertTrue(
                module.evaluate(
                    root,
                    baseline,
                    expected_reports=2,
                    expected_languages=("python", "go"),
                )
            )
            fingerprint = module.finding_fingerprint(
                {
                    "ruleId": "py/test",
                    "locations": [
                        {
                            "physicalLocation": {
                                "artifactLocation": {"uri": "src/example.py"},
                                "region": {"startLine": 3},
                            }
                        }
                    ],
                }
            )
            expired_baseline = root / "baseline.json"
            expired_baseline.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "policy": {
                            "fail_on_levels": ["warning"],
                            "minimum_score": 2,
                            "exception_expiry_days": 30,
                        },
                        "findings": [
                            {
                                "fingerprint": fingerprint,
                                "rule": "py/test",
                                "location": "src/example.py:3",
                                "rationale": "test",
                                "owner": "test",
                                "expires_on": date.today().isoformat(),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            self._sarif(root, level="warning")
            violations = module.evaluate(root, expired_baseline, expected_reports=2)
            self.assertTrue(violations)
            self.assertTrue(
                any("expired" in violation.lower() for violation in violations)
            )

    def test_expected_language_coverage_uses_report_metadata_not_filenames(self):
        module = _load_script("check_codeql_findings.py")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = ROOT / "security" / "codeql-baseline.json"
            # Deliberately swap the names and metadata.  A filename-only check
            # would accept this pair; the language contract must reject it.
            self._sarif(
                root,
                name="python.sarif",
                language="javascript-typescript",
            )
            self._sarif(
                root,
                name="javascript-typescript.sarif",
                language="python",
            )
            violations = module.evaluate(
                root,
                baseline,
                expected_reports=2,
                expected_languages=("python", "javascript-typescript"),
            )
            self.assertTrue(violations)
            self.assertTrue(any("conflicts" in violation for violation in violations))

    def test_expected_language_reports_reject_duplicates_and_ambiguous_runs(self):
        module = _load_script("check_codeql_findings.py")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = ROOT / "security" / "codeql-baseline.json"
            self._sarif(root, name="python.sarif", language="python")
            self._sarif(root, name="python-copy.sarif", language="python")
            duplicate = module.evaluate(
                root,
                baseline,
                expected_reports=2,
                expected_languages=("python", "javascript-typescript"),
            )
            self.assertTrue(duplicate)
            self.assertTrue(any("duplicate" in violation for violation in duplicate))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = ROOT / "security" / "codeql-baseline.json"
            ambiguous_runs = [
                {
                    "tool": {
                        "driver": {
                            "name": "CodeQL",
                            "organization": "GitHub",
                            "rules": [],
                        }
                    },
                    "properties": {"languages": ["python"]},
                    "automationDetails": {"id": "/language:javascript-typescript"},
                    "results": [],
                }
            ]
            self._sarif(
                root,
                name="ambiguous.sarif",
                language=None,
                runs=ambiguous_runs,
            )
            ambiguous = module.evaluate(
                root,
                baseline,
                expected_reports=1,
                expected_languages=("python",),
            )
            self.assertTrue(ambiguous)
            self.assertTrue(any("conflicting" in violation for violation in ambiguous))

    def test_rule_prefix_can_identify_nonempty_language_reports(self):
        module = _load_script("check_codeql_findings.py")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = ROOT / "security" / "codeql-baseline.json"
            # No optional run metadata; a CodeQL rule prefix is the documented
            # fallback identity for a report that has findings.
            self._sarif(
                root,
                name="report.sarif",
                language=None,
                level="none",
                runs=[
                    {
                        "tool": {
                            "driver": {
                                "name": "CodeQL",
                                "organization": "GitHub",
                                "rules": [],
                            }
                        },
                        "results": [
                            {
                                "ruleId": "py/test",
                                "level": "none",
                                "locations": [
                                    {
                                        "physicalLocation": {
                                            "artifactLocation": {
                                                "uri": "src/example.py"
                                            },
                                            "region": {"startLine": 3},
                                        }
                                    }
                                ],
                            }
                        ],
                    }
                ],
            )
            self.assertEqual(
                module.evaluate(
                    root,
                    baseline,
                    expected_reports=1,
                    expected_languages=("python",),
                ),
                [],
            )

    def test_expected_language_without_metadata_fails_closed(self):
        module = _load_script("check_codeql_findings.py")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = ROOT / "security" / "codeql-baseline.json"
            # Filename is intentionally the only language hint and therefore
            # must not satisfy strict coverage.
            self._sarif(root, name="unlabelled.sarif", language=None)
            violations = module.evaluate(
                root,
                baseline,
                expected_reports=1,
                expected_languages=("python",),
            )
            self.assertTrue(violations)
            self.assertTrue(any("metadata" in violation for violation in violations))

    def test_codeql_rejects_unsafe_finding_locations(self):
        module = _load_script("check_codeql_findings.py")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = ROOT / "security" / "codeql-baseline.json"
            self._sarif(root, level="warning")
            report = root / "python.sarif"
            payload = json.loads(report.read_text(encoding="utf-8"))
            payload["runs"][0]["results"][0]["locations"][0]["physicalLocation"][
                "artifactLocation"
            ]["uri"] = "../outside.py"
            report.write_text(json.dumps(payload), encoding="utf-8")
            violations = module.evaluate(root, baseline, expected_reports=1)
            self.assertTrue(violations)
            self.assertTrue(
                any(
                    "canonical repository path" in violation for violation in violations
                )
            )


class ProtectionPolicyTests(unittest.TestCase):
    def test_checked_in_policy_is_valid(self):
        module = _load_script("check_protection_policy.py")
        policy = module.load_object(ROOT / ".github" / "protection-policy.json")
        self.assertEqual(module.validate_policy(policy), [])

    def test_drifted_readback_is_rejected(self):
        module = _load_script("check_protection_policy.py")
        policy = module.load_object(ROOT / ".github" / "protection-policy.json")
        readback = {
            "enforcement": "active",
            "conditions": {
                "ref_name": {
                    "include": ["refs/heads/main"],
                    "exclude": [],
                }
            },
            "rules": [],
            "bypass_actors": [
                {
                    "actor_id": 1,
                    "actor_type": "Integration",
                    "bypass_mode": "always",
                }
            ],
        }
        errors = module.compare_readback(policy, readback)
        self.assertTrue(any("Required checks" in error for error in errors))
        self.assertTrue(any("blanket" in error for error in errors))

    def test_complete_readback_matches_every_required_control(self):
        module = _load_script("check_protection_policy.py")
        policy = module.load_object(ROOT / ".github" / "protection-policy.json")
        readback = {
            "enforcement": "active",
            "target": "branch",
            "conditions": {
                "ref_name": {
                    "include": ["refs/heads/main"],
                    "exclude": [],
                }
            },
            "rules": [
                {
                    "type": "pull_request",
                    "parameters": {
                        "required_approving_review_count": 1,
                        "require_code_owner_review": True,
                        "dismiss_stale_reviews_on_push": True,
                        "require_last_push_approval": True,
                        "required_review_thread_resolution": True,
                    },
                },
                {
                    "type": "required_status_checks",
                    "parameters": {
                        "strict_required_status_checks_policy": True,
                        "required_status_checks": [{"context": "Required checks"}],
                    },
                },
            ],
            "bypass_actors": [
                {
                    "actor_id": 5,
                    "actor_type": "RepositoryRole",
                    "bypass_mode": "pull_request",
                }
            ],
        }
        self.assertEqual(module.compare_readback(policy, readback), [])

    def test_readback_parameter_or_bypass_drift_is_rejected(self):
        module = _load_script("check_protection_policy.py")
        policy = module.load_object(ROOT / ".github" / "protection-policy.json")
        readback = {
            "enforcement": "active",
            "target": "branch",
            "conditions": {
                "ref_name": {
                    "include": ["refs/heads/main"],
                    "exclude": [],
                }
            },
            "rules": [
                {
                    "type": "pull_request",
                    "parameters": {
                        "required_approving_review_count": 0,
                        "require_code_owner_review": False,
                        "dismiss_stale_reviews_on_push": True,
                        "require_last_push_approval": True,
                        "required_review_thread_resolution": True,
                    },
                },
                {
                    "type": "required_status_checks",
                    "parameters": {
                        "strict_required_status_checks_policy": False,
                        "required_status_checks": [{"context": "Other"}],
                    },
                },
            ],
            "bypass_actors": [
                {
                    "actor_id": 1,
                    "actor_type": "Integration",
                    "bypass_mode": "always",
                }
            ],
        }
        errors = module.compare_readback(policy, readback)
        self.assertTrue(
            any("required_approving_review_count" in error for error in errors)
        )
        self.assertTrue(
            any("strict_required_status_checks_policy" in error for error in errors)
        )
        self.assertTrue(any("bypass" in error for error in errors))
