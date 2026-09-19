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
    "learning-feedback",
    "review-category",
    "stage",
    "concurrency-plan",
    "evaluation-corpus",
    "evaluation-report",
    "promotion-record",
    "openrouter-qualification",
    "run-outcome",
    "recovery-artifact",
    "candidate-finding",
    "verification-result",
    "coverage-manifest",
    "review-convergence-policy",
    "blocker-admission",
    "review-round-decision",
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

    def test_review_comment_schema_requires_line_or_file_side(self) -> None:
        with self.assertRaises(ReviewInputError):
            validate_public_document(
                {"path": "src/app.py", "body": "Missing both line and side."},
                "review-comment",
            )

    def test_review_comment_schema_requires_line_for_right_side(self) -> None:
        with self.assertRaises(ReviewInputError):
            validate_public_document(
                {"path": "src/app.py", "body": "Missing line.", "side": "RIGHT"},
                "review-comment",
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

    def test_review_result_source_context_coverage_is_optional(self) -> None:
        from review_sensei.context import ContextSnapshot, SourceContextCoverage

        result = ReviewResult(
            summary="ok",
            comments=(),
            provider="ollama",
            source_context_coverage=SourceContextCoverage(
                enabled=True,
                complete=False,
                snapshot=ContextSnapshot("a" * 40),
                outcomes=(("src/app.py", "unsupported-language"),),
                excerpt_count=1,
            ),
        )
        document = result.to_dict()
        validate_public_document(document, "review-result")
        restored = ReviewResult.from_dict(document)
        self.assertIsNotNone(restored.source_context_coverage)

    def test_review_result_source_context_round_trips_untrusted_head_sha(self) -> None:
        from review_sensei.context import ContextSnapshot, SourceContextCoverage

        result = ReviewResult(
            summary="ok",
            comments=(),
            provider="ollama",
            source_context_coverage=SourceContextCoverage(
                enabled=True,
                complete=True,
                snapshot=ContextSnapshot("a" * 40),
                outcomes=(("src/app.py", "reviewed"),),
                excerpt_count=2,
                untrusted_head_sha="b" * 40,
            ),
        )
        document = result.to_dict()
        validate_public_document(document, "review-result")
        self.assertEqual(document["source_context"]["untrusted_head_sha"], "b" * 40)
        restored = ReviewResult.from_dict(document)
        coverage = restored.source_context_coverage
        assert coverage is not None
        self.assertEqual(coverage.untrusted_head_sha, "b" * 40)
        self.assertEqual(coverage.snapshot.revision, "a" * 40)
        self.assertEqual(coverage.excerpt_count, 2)
        self.assertTrue(coverage.complete)
        # The field is schema-constrained, so a non-SHA value must not survive
        # the publisher-facing boundary.
        document["source_context"]["untrusted_head_sha"] = "not-a-sha"
        with self.assertRaises(ReviewInputError):
            ReviewResult.from_dict(document)

    def test_review_result_source_context_rejects_coerced_types(self) -> None:
        from review_sensei.context import ContextSnapshot, SourceContextCoverage

        result = ReviewResult(
            summary="ok",
            comments=(),
            provider="ollama",
            source_context_coverage=SourceContextCoverage(
                enabled=True,
                complete=False,
                snapshot=ContextSnapshot("a" * 40),
                outcomes=(("src/app.py", "reviewed"),),
                excerpt_count=1,
            ),
        )
        document = result.to_dict()
        validate_public_document(document, "review-result")
        for field, invalid in (
            ("enabled", 1),
            ("complete", "yes"),
            ("excerpt_count", "1"),
            ("excerpt_count", True),
            ("languages", ["python", 2]),
            ("outcomes", "src/app.py"),
        ):
            with self.subTest(field=field, invalid=invalid):
                broken = json.loads(json.dumps(document))
                broken["source_context"][field] = invalid
                with self.assertRaises(ReviewInputError):
                    ReviewResult.from_dict(broken)
        for missing in (
            "enabled",
            "complete",
            "excerpt_count",
            "languages",
            "outcomes",
            "snapshot",
        ):
            with self.subTest(missing=missing):
                broken = json.loads(json.dumps(document))
                del broken["source_context"][missing]
                with self.assertRaises(ReviewInputError):
                    ReviewResult.from_dict(broken)

    def test_conversation_reply_resolution_flag_is_optional_boolean(self) -> None:
        validate_public_document({"body": "ok"}, "conversation-reply")
        validate_public_document({"body": "ok", "resolve": True}, "conversation-reply")
        with self.assertRaises(ReviewInputError):
            validate_public_document(
                {"body": "ok", "resolve": "yes"}, "conversation-reply"
            )

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
