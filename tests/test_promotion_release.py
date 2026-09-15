import unittest
from collections import UserDict

from review_sensei.errors import ReviewInputError
from review_sensei.evaluation import PromotionRecord
from review_sensei.release_manifest import (
    _constraint_parts,
    _range_contains,
    _version_parts,
    validate_compatibility_manifest,
)

SHA = "a" * 64


class PromotionAndReleaseTests(unittest.TestCase):
    def test_fixture_promotion_is_rejected(self) -> None:
        with self.assertRaises(ReviewInputError):
            PromotionRecord(
                SHA,
                SHA,
                SHA,
                SHA,
                "fixture",
                "fixture-v1",
                "r1",
                3,
                "2026-01-01",
                {},
            )

    def test_fixture_provider_class_alias_is_rejected_for_support(self) -> None:
        with self.assertRaises(ReviewInputError):
            PromotionRecord(
                SHA,
                SHA,
                SHA,
                SHA,
                "FixtureProvider",
                "fixture-v1",
                "r1",
                3,
                "2026-01-01",
                {"seed": "fixed"},
            )

    def test_supported_promotion_requires_reproducibility_settings(self) -> None:
        with self.assertRaises(ReviewInputError):
            PromotionRecord(
                SHA,
                SHA,
                SHA,
                SHA,
                "ollama",
                "model",
                "r1",
                3,
                "2026-01-01",
                {},
            )

    def test_mapping_reproducibility_is_copied_safely(self) -> None:
        record = PromotionRecord(
            SHA,
            SHA,
            SHA,
            SHA,
            "ollama",
            "model",
            "r1",
            3,
            "2026-01-01",
            UserDict({"seed": "fixed"}),
        )
        self.assertEqual(record.to_dict()["reproducibility"], {"seed": "fixed"})

    def test_invalid_reproducibility_shape_fails_with_review_input_error(self) -> None:
        with self.assertRaises(ReviewInputError):
            PromotionRecord(
                SHA,
                SHA,
                SHA,
                SHA,
                "ollama",
                "model",
                "r1",
                3,
                "2026-01-01",
                [],
            )

    def test_worker_range_wildcards_use_component_bounds(self) -> None:
        self.assertEqual(
            _constraint_parts("1.x"),
            [(">=", (1, 0, 0)), ("<", (2, 0, 0))],
        )
        self.assertEqual(
            _constraint_parts("1.2.x"),
            [(">=", (1, 2, 0)), ("<", (1, 3, 0))],
        )
        self.assertTrue(_range_contains("1.9.9", "1.x"))
        self.assertFalse(_range_contains("2.0.0", "1.x"))
        self.assertTrue(_range_contains("1.2.9", "1.2.x"))
        self.assertFalse(_range_contains("1.3.0", "1.2.x"))

    def test_worker_range_wildcard_at_zero_is_explicitly_unbounded(self) -> None:
        self.assertEqual(_constraint_parts("x"), [(">=", (0, 0, 0))])
        self.assertTrue(_range_contains("99.99.99", "x"))

    def test_worker_range_rejects_mixed_wildcard_components(self) -> None:
        with self.assertRaises(ValueError):
            _constraint_parts("1.x.0")
        with self.assertRaises(ValueError):
            _range_contains("1.0.0", "x.1")

    def test_worker_range_rejects_empty_or_consecutive_alternatives(self) -> None:
        for expression in (">=1.0.0 ||", ">=1.0.0 || || >=2.0.0"):
            with self.assertRaises(ValueError):
                _range_contains("1.0.0", expression)

    def test_worker_range_operator_boundaries(self) -> None:
        self.assertTrue(_range_contains("1.2.3", "^1.2.3"))
        self.assertTrue(_range_contains("1.9.9", "^1.2.3"))
        self.assertFalse(_range_contains("2.0.0", "^1.2.3"))
        self.assertTrue(_range_contains("1.2.3", "~1.2.3"))
        self.assertTrue(_range_contains("1.2.9", "~1.2.3"))
        self.assertFalse(_range_contains("1.3.0", "~1.2.3"))

    def test_worker_version_parser_rejects_wildcards_and_partial_versions(self) -> None:
        for version in ("x", "1.x", "1.2"):
            with self.assertRaises(ValueError):
                _version_parts(version)

    def test_manifest_rejects_mismatched_release_versions(self) -> None:
        artifact = {"name": "x", "version": "1.0.0", "sha256": SHA}
        value = {
            "schema_version": "1.0",
            "release": "1.0.0",
            "compatible_worker_range": ">=1",
            "provenance": "signed",
            "artifacts": {
                "workflow": artifact,
                "python": artifact,
                "npm": [artifact],
                "schemas_version": "1.0",
                "worker": {**artifact, "version": "2.0.0"},
            },
        }
        with self.assertRaises(ReviewInputError):
            validate_compatibility_manifest(value)

    def test_manifest_rejects_range_that_excludes_worker(self) -> None:
        artifact = {"name": "x", "version": "1.0.0", "sha256": SHA}
        value = {
            "schema_version": "1.0",
            "release": "1.0.0",
            "compatible_worker_range": ">=2.0.0",
            "provenance": "signed",
            "artifacts": {
                "workflow": artifact,
                "python": artifact,
                "npm": [artifact],
                "schemas_version": "1.0",
                "worker": artifact,
            },
        }
        with self.assertRaises(ReviewInputError):
            validate_compatibility_manifest(value)

    def test_manifest_rejects_malformed_range(self) -> None:
        artifact = {"name": "x", "version": "1.0.0", "sha256": SHA}
        value = {
            "schema_version": "1.0",
            "release": "1.0.0",
            "compatible_worker_range": "not-a-range",
            "provenance": "signed",
            "artifacts": {
                "workflow": artifact,
                "python": artifact,
                "npm": [artifact],
                "schemas_version": "1.0",
                "worker": artifact,
            },
        }
        with self.assertRaises(ReviewInputError):
            validate_compatibility_manifest(value)


if __name__ == "__main__":
    unittest.main()
