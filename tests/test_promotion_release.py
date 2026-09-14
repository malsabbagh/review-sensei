import unittest

from review_sensei.errors import ReviewInputError
from review_sensei.evaluation import PromotionRecord
from review_sensei.release_manifest import validate_compatibility_manifest

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
