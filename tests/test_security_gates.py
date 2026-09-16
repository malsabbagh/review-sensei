import importlib.util
import json
import re
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock
from urllib.parse import quote

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

    def test_default_baseline_flags_warning_finding(self):
        module = _load_script("check_codeql_findings.py")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = ROOT / "security" / "codeql-baseline.json"
            self._sarif(root, level="warning")
            violations = module.evaluate(root, baseline, expected_reports=1)
            self.assertEqual(len(violations), 1)
            self.assertIn("warning finding", violations[0])

    def test_missing_or_malformed_report_fails_closed(self):
        module = _load_script("check_codeql_findings.py")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = ROOT / "security" / "codeql-baseline.json"
            self.assertTrue(module.evaluate(root, baseline))
            (root / "broken.sarif").write_text("{}", encoding="utf-8")
            self.assertTrue(module.evaluate(root, baseline))

    def test_sarif_scan_rejects_too_many_non_sarif_entries(self):
        module = _load_script("check_codeql_findings.py")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            filenames = [
                f"ignored-{index}.txt"
                for index in range(module.MAX_SARIF_SCAN_ENTRIES + 1)
            ]
            with mock.patch.object(
                module.os, "walk", return_value=[(root, [], filenames)]
            ):
                with self.assertRaisesRegex(ValueError, "too many entries"):
                    module._sarif_paths(root)

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

    def test_partial_fingerprint_baseline_remains_scoped_to_location(self):
        module = _load_script("check_codeql_findings.py")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = root / "baseline.json"
            old_result = {
                "ruleId": "py/test",
                "level": "warning",
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
            moved_result = {
                **old_result,
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": "src/new.py"},
                            "region": {"startLine": 99},
                        }
                    }
                ],
            }
            baseline.write_text(
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
                                "fingerprint": module.finding_fingerprint(old_result),
                                "rule": "py/test",
                                "location": "src/old.py",
                                "rationale": "test",
                                "owner": "test",
                                "expires_on": (
                                    date.today() + timedelta(days=1)
                                ).isoformat(),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            self._sarif(
                root,
                name="python.sarif",
                language="python",
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
                        "results": [moved_result],
                    }
                ],
            )
            violations = module.evaluate(
                root,
                baseline,
                expected_reports=1,
                expected_languages=("python",),
            )
            self.assertEqual(len(violations), 1)
            self.assertIn("unreviewed", violations[0])

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
            self.assertTrue(any("missing" in violation for violation in duplicate))

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

    def test_expected_languages_allow_multiple_reports_and_runs_per_language(self):
        module = _load_script("check_codeql_findings.py")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = ROOT / "security" / "codeql-baseline.json"
            javascript_run = {
                "tool": {
                    "driver": {
                        "name": "CodeQL",
                        "organization": "GitHub",
                        "rules": [],
                    }
                },
                "properties": {"language": "javascript-typescript"},
                "automationDetails": {
                    "id": "review-sensei/language:javascript-typescript"
                },
                "results": [],
            }
            # CodeQL can split JavaScript and TypeScript work into multiple
            # runs and can emit more than one SARIF file for a language.
            self._sarif(
                root,
                name="javascript-part-1.sarif",
                language=None,
                runs=[javascript_run, {**javascript_run}],
            )
            self._sarif(
                root,
                name="javascript-part-2.sarif",
                language="javascript-typescript",
            )
            self._sarif(root, name="python.sarif", language="python")
            self.assertEqual(
                module.evaluate(
                    root,
                    baseline,
                    expected_reports=0,
                    expected_languages=("python", "javascript-typescript"),
                ),
                [],
            )

    def test_expected_languages_reject_empty_cli_entries(self):
        module = _load_script("check_codeql_findings.py")
        with self.assertRaises(SystemExit) as raised:
            module.main(
                [
                    "--sarif-dir",
                    "sarif",
                    "--baseline",
                    "baseline.json",
                    "--expected-languages",
                    "python,",
                ]
            )
        self.assertEqual(raised.exception.code, 2)

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
            module._normalize_location("src/file.ts:123", strip_line_suffix=False),
            "src/file.ts:123",
        )
        self.assertEqual(module._normalize_location("src/file.ts:123"), "src/file.ts")
        self.assertEqual(
            module._normalize_location("file:///etc/passwd"),
            "/etc/passwd",
        )
        self.assertFalse(
            module._is_safe_location(module._normalize_location("docs://foo/bar"))
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
        self.assertFalse(
            module._is_safe_location(
                module._normalize_location("src%2F..%2Foutside.py")
            )
        )
        with self.assertRaises(ValueError):
            module._normalize_location("src%252F..%252Foutside.py")
        self.assertEqual(
            module._normalize_location("src%252Ffile.py"),
            "src%2Ffile.py",
        )
        self.assertTrue(
            module._is_safe_location(module._normalize_location("src%252Ffile.py"))
        )
        deeply_encoded = "src/../outside.py"
        for _ in range(9):
            deeply_encoded = quote(deeply_encoded, safe="")
        with self.assertRaises(ValueError):
            module._normalize_location(deeply_encoded)
        self.assertTrue(
            module._is_safe_location(module._normalize_location("src%2Ffile.py"))
        )

    def test_security_severity_promotes_reported_level(self):
        module = _load_script("check_codeql_findings.py")
        score, level = module._result_severity(
            {"ruleId": "py/test", "level": "warning"},
            {"py/test": {"properties": {"security-severity": "7.0"}}},
        )
        self.assertEqual((score, level), (3, "error"))
        with self.assertRaises(ValueError):
            module._result_severity(
                {"ruleId": "py/test", "level": "warning"},
                {"py/test": {"properties": {"security-severity": True}}},
            )

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
            self.assertTrue(any("must equal" in violation for violation in violations))

    def test_minimum_score_must_match_declared_fail_levels(self):
        module = _load_script("check_codeql_findings.py")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = root / "baseline.json"
            baseline.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "policy": {
                            "fail_on_levels": ["error"],
                            "minimum_score": 2,
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
            self.assertTrue(any("must equal" in violation for violation in violations))

    def test_baseline_rejects_unknown_policy_fields(self):
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
                            "minimum_score": 2,
                            "exception_expiry_days": 30,
                            "fail_on_levelz": ["error"],
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
                any("unknown field" in violation for violation in violations)
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

    def test_bypass_actor_count_and_ruleset_target_are_bounded(self):
        module = _load_script("check_protection_policy.py")
        policy = module.load_object(ROOT / ".github" / "protection-policy.json")
        for value in (None, 0, 2, True):
            with self.subTest(max_bypass_actors=value):
                drifted = json.loads(json.dumps(policy))
                if value is None:
                    del drifted["max_bypass_actors"]
                else:
                    drifted["max_bypass_actors"] = value
                errors = module.validate_policy(drifted)
                self.assertTrue(any("max_bypass_actors" in error for error in errors))

        drifted = json.loads(json.dumps(policy))
        drifted["bypass_actors"].append(
            {
                "name": "second actor",
                "actor_id": 6,
                "actor_type": "RepositoryRole",
                "reason": "test",
                "bypass_mode": "pull_request",
            }
        )
        errors = module.validate_policy(drifted)
        self.assertTrue(any("must not exceed" in error for error in errors))

        drifted = json.loads(json.dumps(policy))
        del drifted["ruleset"]["target"]
        errors = module.validate_policy(drifted)
        self.assertTrue(any("ruleset.target" in error for error in errors))

    def test_bypass_actor_identity_is_concrete_and_unique(self):
        module = _load_script("check_protection_policy.py")
        policy = module.load_object(ROOT / ".github" / "protection-policy.json")
        actor = policy["bypass_actors"][0]
        self.assertEqual(actor["name"], "malsabbagh")
        self.assertEqual(actor["actor_id"], 13791232)
        self.assertEqual(actor["actor_type"], "User")

        duplicate = json.loads(json.dumps(policy))
        duplicate["bypass_actors"].append(dict(actor))
        errors = module.validate_policy(duplicate)
        self.assertTrue(any("unique actor identities" in error for error in errors))

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

    def test_malformed_rules_still_validate_bypass_actors(self):
        module = _load_script("check_protection_policy.py")
        policy = module.load_object(ROOT / ".github" / "protection-policy.json")
        readback = {
            "enforcement": "active",
            "target": "branch",
            "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
            "rules": {},
            "bypass_actors": [
                {"actor_id": 1, "actor_type": "Integration", "bypass_mode": "always"}
            ],
        }
        errors = module.compare_readback(policy, readback)
        self.assertTrue(any("rules must be an array" in error for error in errors))
        self.assertTrue(any("blanket" in error for error in errors))

    def test_missing_readback_target_is_explicitly_rejected(self):
        module = _load_script("check_protection_policy.py")
        policy = module.load_object(ROOT / ".github" / "protection-policy.json")
        readback = {
            "enforcement": "active",
            "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
            "rules": [],
            "bypass_actors": [],
        }
        errors = module.compare_readback(policy, readback)
        self.assertTrue(any("target is missing" in error for error in errors))

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
                    "actor_id": 13791232,
                    "actor_type": "User",
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
                    "actor_id": 13791232,
                    "actor_type": "User",
                    "bypass_mode": "pull_request",
                }
            ],
        }
        errors = module.compare_readback(policy, readback)
        self.assertTrue(any("duplicate pull_request" in error for error in errors))


class CommittedCodeQLFixtureTests(unittest.TestCase):
    _FIXTURES = ROOT / "tests" / "fixtures" / "codeql"
    _BASELINE = ROOT / "security" / "codeql-baseline.json"

    def test_committed_clean_reports_pass_the_gate(self):
        module = _load_script("check_codeql_findings.py")
        self.assertEqual(
            module.evaluate(
                self._FIXTURES / "clean",
                self._BASELINE,
                expected_reports=0,
                expected_languages=("python", "javascript-typescript"),
            ),
            [],
        )
        self.assertEqual(
            module.main(
                [
                    "--sarif-dir",
                    str(self._FIXTURES / "clean"),
                    "--baseline",
                    str(self._BASELINE),
                    "--expected-reports",
                    "0",
                    "--expected-languages",
                    "python,javascript-typescript",
                ]
            ),
            0,
        )

    def test_committed_seeded_warning_fails_the_gate(self):
        module = _load_script("check_codeql_findings.py")
        violations = module.evaluate(
            self._FIXTURES / "seeded-warning",
            self._BASELINE,
            expected_reports=0,
            expected_languages=("python",),
        )
        self.assertEqual(len(violations), 1)
        self.assertIn("warning finding", violations[0])
        self.assertIn("py/test", violations[0])
        self.assertEqual(
            module.main(
                [
                    "--sarif-dir",
                    str(self._FIXTURES / "seeded-warning"),
                    "--baseline",
                    str(self._BASELINE),
                    "--expected-reports",
                    "0",
                    "--expected-languages",
                    "python",
                ]
            ),
            1,
        )

    def test_committed_malformed_report_fails_closed(self):
        module = _load_script("check_codeql_findings.py")
        violations = module.evaluate(
            self._FIXTURES / "malformed",
            self._BASELINE,
            expected_reports=1,
        )
        self.assertTrue(violations)
        self.assertEqual(
            module.main(
                [
                    "--sarif-dir",
                    str(self._FIXTURES / "malformed"),
                    "--baseline",
                    str(self._BASELINE),
                    "--expected-reports",
                    "1",
                ]
            ),
            1,
        )
