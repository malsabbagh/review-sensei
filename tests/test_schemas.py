from __future__ import annotations

import json
import unittest
from pathlib import Path

from review_sensei.errors import (
    ContextLoadError,
    LearningLoadError,
    ProviderError,
    ReviewFormatError,
    ReviewInputError,
    ReviewSenseiError,
)
from review_sensei.models import ReviewComment, ReviewResult
from review_sensei.schemas import SCHEMA_DIR, validate_public_document

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_NAMES = (
    "conversation-reply",
    "review-result",
    "review-comment",
    "learning-entry",
    "learning-proposal",
    "review-category",
    "stage",
    "concurrency-plan",
    "evaluation-corpus",
    "evaluation-report",
)


class PublicSchemaTests(unittest.TestCase):
    def test_schema_files_exist_and_use_v1_ids(self) -> None:
        for name in SCHEMA_NAMES:
            with self.subTest(name=name):
                path = SCHEMA_DIR / f"{name}.schema.json"
                self.assertTrue(path.is_file(), path)
                value = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(
                    value["$schema"],
                    "https://json-schema.org/draft/2020-12/schema",
                )
                self.assertIn("/v1/", value["$id"])

    def test_packaged_defaults_and_examples_validate(self) -> None:
        cases = (
            (ROOT / "src/review_sensei/default_categories", "review-category"),
            (ROOT / "src/review_sensei/default_stages", "stage"),
            (ROOT / "examples/categories", "review-category"),
            (ROOT / "examples/stages", "stage"),
        )
        for directory, schema_name in cases:
            for path in sorted(directory.glob("*.json")):
                with self.subTest(path=path):
                    validate_public_document(
                        json.loads(path.read_text(encoding="utf-8")),
                        schema_name,
                    )

    def test_golden_fixtures_validate(self) -> None:
        for path in sorted((ROOT / "tests/fixtures/schemas/golden").glob("*.json")):
            with self.subTest(path=path):
                schema_name = path.name[: -len(".json")]
                validate_public_document(
                    json.loads(path.read_text(encoding="utf-8")),
                    schema_name,
                )

    def test_negative_fixtures_fail(self) -> None:
        for path in sorted((ROOT / "tests/fixtures/schemas/negative").glob("*.json")):
            with self.subTest(path=path):
                schema_name = path.name[: -len(".json")]
                with self.assertRaises(ReviewInputError):
                    validate_public_document(
                        json.loads(path.read_text(encoding="utf-8")),
                        schema_name,
                    )

    def test_review_result_to_dict_validates_against_review_result_schema(self) -> None:
        result = ReviewResult(
            summary="ok",
            comments=(
                ReviewComment(path="src/app.py", line=2, body="Use a constant."),
            ),
            provider="ollama",
        )
        validate_public_document(result.to_dict(), "review-result")

    def test_error_categories_are_stable(self) -> None:
        self.assertEqual(ReviewSenseiError.error_category, "unknown")
        self.assertEqual(ReviewInputError.error_category, "input")
        self.assertEqual(ReviewFormatError.error_category, "format")
        self.assertEqual(LearningLoadError.error_category, "learning")
        self.assertEqual(ContextLoadError.error_category, "context")
        self.assertEqual(ProviderError.error_category, "provider")

    def test_provider_error_transient_flag(self) -> None:
        self.assertFalse(ProviderError("ordinary").transient)
        self.assertTrue(ProviderError("timeout", transient=True).transient)
        self.assertEqual(str(ProviderError("timeout", transient=True)), "timeout")


if __name__ == "__main__":
    unittest.main()
