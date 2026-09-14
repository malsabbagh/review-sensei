import importlib.util
import json
import re
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

    def test_rule_prefix_without_run_metadata_fails_closed(self):
        module = _load_script("check_codeql_findings.py")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = ROOT / "security" / "codeql-baseline.json"
            # A query prefix identifies a finding family, not the analyzed run.
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
            violations = module.evaluate(
                root,
                baseline,
                expected_reports=1,
                expected_languages=("python",),
            )
            self.assertTrue(violations)
            self.assertTrue(any("metadata" in violation for violation in violations))

    def test_category_metadata_establishes_language_without_properties(self):
        module = _load_script("check_codeql_findings.py")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = ROOT / "security" / "codeql-baseline.json"
            self._sarif(
                root,
                name="category-only.sarif",
                language=None,
                runs=[
                    {
                        "tool": {
                            "driver": {
                                "name": "CodeQL",
                                "organization": "GitHub",
                                "rules": [],
                            }
                        },
                        "automationDetails": {"id": "review-sensei/language:python"},
                        "results": [],
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

    def test_rule_prefix_conflicting_with_explicit_metadata_fails_closed(self):
        module = _load_script("check_codeql_findings.py")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = ROOT / "security" / "codeql-baseline.json"
            self._sarif(
                root,
                name="conflicting-rule.sarif",
                language=None,
                runs=[
                    {
                        "tool": {
                            "driver": {
                                "name": "CodeQL",
                                "organization": "GitHub",
                                "rules": [],
                            }
                        },
                        "properties": {"language": "python"},
                        "automationDetails": {"id": "/language:python"},
                        "results": [
                            {
                                "ruleId": "js/test",
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
            violations = module.evaluate(
                root,
                baseline,
                expected_reports=1,
                expected_languages=("python",),
            )
            self.assertTrue(violations)
            self.assertTrue(any("conflict" in violation for violation in violations))

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

    def test_location_normalization_preserves_colons_and_rejects_controls(self):
        module = _load_script("check_codeql_findings.py")
        self.assertEqual(
            module._normalize_location("src/file:example.py"),
            "src/file:example.py",
        )
        self.assertEqual(
            module._normalize_location("file:///etc/passwd"),
            "/etc/passwd",
        )
        self.assertFalse(module._is_safe_location("src/file.py\x7f"))
        self.assertFalse(module._is_safe_location("src/file.py\x85"))
        self.assertFalse(module._is_safe_location("C:/workspace/file.py"))
        self.assertFalse(module._is_safe_location(r"C:\\workspace\\file.py"))
        self.assertFalse(module._is_safe_location("C:workspace/file.py"))
        self.assertFalse(
            module._is_safe_location(
                module._normalize_location("C%3A/workspace/file.py")
            )
        )

    def test_security_severity_promotes_reported_level(self):
        module = _load_script("check_codeql_findings.py")
        score, level = module._result_severity(
            {"ruleId": "py/test", "level": "warning"},
            {"py/test": {"properties": {"security-severity": "7.0"}}},
        )
        self.assertEqual((score, level), (3, "error"))

    def test_non_codeql_driver_is_rejected_even_without_language_filter(self):
        module = _load_script("check_codeql_findings.py")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = ROOT / "security" / "codeql-baseline.json"
            self._sarif(root, driver_name="semgrep", organization="Semgrep")
            violations = module.evaluate(root, baseline, expected_reports=1)
            self.assertTrue(violations)
            self.assertTrue(
                any("CodeQL producer" in violation for violation in violations)
            )

    def test_minimum_score_cannot_hide_declared_fail_level(self):
        module = _load_script("check_codeql_findings.py")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = root / "baseline.json"
            baseline.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "policy": {
                            "fail_on_levels": ["warning"],
                            "minimum_score": 3,
                            "exception_expiry_days": 30,
                        },
                        "findings": [],
                    }
                ),
                encoding="utf-8",
            )
            self._sarif(root)
            violations = module.evaluate(root, baseline, expected_reports=1)
            self.assertTrue(violations)
            self.assertTrue(
                any("must not exceed" in violation for violation in violations)
            )


class ProtectionPolicyTests(unittest.TestCase):
    def test_checked_in_policy_is_valid(self):
        module = _load_script("check_protection_policy.py")
        policy = module.load_object(ROOT / ".github" / "protection-policy.json")
        self.assertEqual(module.validate_policy(policy), [])

    def test_immutable_tag_pattern_is_anchored_semver_regex(self):
        module = _load_script("check_protection_policy.py")
        policy = module.load_object(ROOT / ".github" / "protection-policy.json")
        pattern = re.compile(policy["tags"]["immutable_pattern"])
        self.assertIsNotNone(pattern.fullmatch("v1.2.3"))
        for tag in ("v1x2x3", "v1.2.3-extra", "prefix-v1.2.3", "v1alpha.2.3"):
            with self.subTest(tag=tag):
                self.assertIsNone(pattern.fullmatch(tag))

    def test_required_tag_policy_fields_cannot_drift(self):
        module = _load_script("check_protection_policy.py")
        policy = module.load_object(ROOT / ".github" / "protection-policy.json")
        for field, value in (
            ("immutable_pattern", "v[0-9]*"),
            ("movable_channels", []),
            ("promotion_record", ""),
        ):
            with self.subTest(field=field):
                drifted = json.loads(json.dumps(policy))
                drifted["tags"][field] = value
                errors = module.validate_policy(drifted)
                self.assertTrue(any(f"tags.{field}" in error for error in errors))

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

    def test_duplicate_readback_rules_fail_closed(self):
        module = _load_script("check_protection_policy.py")
        policy = module.load_object(ROOT / ".github" / "protection-policy.json")
        pull_request = {
            "type": "pull_request",
            "parameters": {
                "required_approving_review_count": 1,
                "require_code_owner_review": True,
                "dismiss_stale_reviews_on_push": True,
                "require_last_push_approval": True,
                "required_review_thread_resolution": True,
            },
        }
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
                pull_request,
                dict(pull_request),
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
        errors = module.compare_readback(policy, readback)
        self.assertTrue(any("duplicate pull_request" in error for error in errors))
