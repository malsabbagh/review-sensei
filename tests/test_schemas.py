from __future__ import annotations

import json
import unittest
from pathlib import Path

from review_sensei.baseline import (
    FINDING_CLASSIFICATIONS,
    LINEAGE_REASONS,
    MAX_HISTORY_READ_FINDINGS,
)
from review_sensei.context import MAX_CACHE_METADATA_ITEMS
from review_sensei.convergence import ATTRIBUTIONS, LATE_REASONS
from review_sensei.errors import (
    ContextLoadError,
    LearningLoadError,
    ProviderError,
    ReviewFormatError,
    ReviewInputError,
    ReviewSenseiError,
)
from review_sensei.models import TRANSACTION_PHASES, ReviewComment, ReviewResult
from review_sensei.planning import MAX_RELATED_PATHS
from review_sensei.schemas import SCHEMA_DIR, validate_public_document
from review_sensei.session import (
    MAX_CONVERGENCE_PROGRESS_ENTRIES,
    MAX_SESSION_RECORD_BYTES,
    MAX_STORED_CONTINUATION_GRANTS,
    SESSION_SHA256_PATTERN,
)

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
    "session-record",
    "verification-scope",
    "later-finding-classification",
    "convergence-sequence-report",
    "review-transaction",
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

    def test_verification_scope_schema_limits_match_runtime_bounds(self) -> None:
        schema = json.loads(
            (SCHEMA_DIR / "verification-scope.schema.json").read_text(encoding="utf-8")
        )
        properties = schema["properties"]
        self.assertEqual(
            properties["reviewed_paths"]["maxItems"], MAX_CACHE_METADATA_ITEMS
        )
        self.assertEqual(
            properties["changed_paths"]["maxItems"], MAX_CACHE_METADATA_ITEMS
        )
        self.assertEqual(
            properties["existing_concerns"]["maximum"], MAX_CACHE_METADATA_ITEMS
        )
        self.assertEqual(properties["related_paths"]["maxItems"], MAX_RELATED_PATHS)

    def test_transaction_schema_and_history_bounds_match_runtime_contract(self) -> None:
        schema = json.loads(
            (SCHEMA_DIR / "review-transaction.schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            set(schema["properties"]["phase"]["enum"]), set(TRANSACTION_PHASES)
        )
        session_schema = json.loads(
            (SCHEMA_DIR / "session-record.schema.json").read_text(encoding="utf-8")
        )
        history = session_schema["properties"]["convergence_history"]["properties"]
        self.assertEqual(
            history["progress"]["maxItems"], MAX_CONVERGENCE_PROGRESS_ENTRIES
        )
        progress_properties = history["progress"]["items"]["properties"]
        self.assertEqual(
            progress_properties["blocker_count"]["maximum"], MAX_SESSION_RECORD_BYTES
        )
        self.assertEqual(
            progress_properties["blocker_set_sha256"]["pattern"],
            SESSION_SHA256_PATTERN,
        )

    def test_later_finding_schema_enums_match_runtime_contract(self) -> None:
        schema = json.loads(
            (SCHEMA_DIR / "later-finding-classification.schema.json").read_text(
                encoding="utf-8"
            )
        )
        properties = schema["properties"]
        self.assertEqual(
            set(properties["classification"]["enum"]), set(FINDING_CLASSIFICATIONS)
        )
        self.assertEqual(
            {item for item in properties["late_reason"]["enum"] if item is not None},
            set(LATE_REASONS),
        )
        self.assertEqual(
            set(properties["lineage_reason"]["enum"]), set(LINEAGE_REASONS)
        )
        self.assertEqual(set(properties["attribution"]["enum"]), set(ATTRIBUTIONS))

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

    def test_session_record_history_limits_match_runtime_bounds(self) -> None:
        schema = json.loads(
            (SCHEMA_DIR / "session-record.schema.json").read_text(encoding="utf-8")
        )
        baseline = schema["properties"]["convergence_history"]["properties"][
            "baseline"
        ]["properties"]
        # A Python-written envelope must never exceed these caps, or the record
        # fails schema validation on its next read.
        self.assertEqual(baseline["findings"]["maxItems"], MAX_HISTORY_READ_FINDINGS)
        self.assertEqual(
            baseline["reviewed_paths"]["maxItems"], MAX_CACHE_METADATA_ITEMS
        )
        self.assertEqual(baseline["related_paths"]["maxItems"], MAX_RELATED_PATHS)
        self.assertEqual(
            schema["properties"]["continuation_grants"]["maxItems"],
            MAX_STORED_CONTINUATION_GRANTS,
        )

    def test_session_record_schema_enforces_history_baseline_presence(self) -> None:
        golden = json.loads(
            (ROOT / "tests/fixtures/schemas/golden/session-record.json").read_text(
                encoding="utf-8"
            )
        )
        baseline = {
            "cache_key": {
                "repository": "owner/repo",
                "pull_request": 136,
                "base_sha": "a" * 40,
                "head_sha": "b" * 40,
                "engine": "fixture",
                "model": "fixture-model",
                "profile": "default",
                "stage_digest": "1" * 64,
                "context_digest": "2" * 64,
                "learning_digest": "3" * 64,
            },
            "policy_digest": "4" * 64,
            "complete": True,
            "coverage_complete": True,
            "generation": 1,
            "findings": [
                {
                    "fingerprint": "5" * 64,
                    "resolution_criterion": "6" * 64,
                    "concern": None,
                    "path": "src/app.py",
                    "symbol": None,
                    "defect_kind": "bug",
                    "generation": 1,
                    "blocking": True,
                }
            ],
            "reviewed_paths": ["src/app.py"],
            "related_paths": [],
        }
        history = {
            "state": "completed",
            "baseline": baseline,
            "progress": [{"event": "completed", "generation": 1}],
            "provenance": {"ledger_digest": "0" * 64},
        }
        # A completed history must carry its baseline, and a non-completed one
        # must not, so a constructor that skipped the in-process validator
        # still cannot publish a contradictory envelope.
        validate_public_document(
            dict(golden, convergence_history=history), "session-record"
        )
        invalidated = json.loads(json.dumps(history))
        invalidated["state"] = "invalidated"
        del invalidated["baseline"]
        invalidated["progress"] = [{"event": "invalidated", "generation": 1}]
        validate_public_document(
            dict(golden, convergence_history=invalidated), "session-record"
        )
        completed_without_baseline = json.loads(json.dumps(history))
        del completed_without_baseline["baseline"]
        with self.assertRaises(ReviewInputError):
            validate_public_document(
                dict(golden, convergence_history=completed_without_baseline),
                "session-record",
            )
        invalidated_with_baseline = json.loads(json.dumps(invalidated))
        invalidated_with_baseline["baseline"] = baseline
        with self.assertRaises(ReviewInputError):
            validate_public_document(
                dict(golden, convergence_history=invalidated_with_baseline),
                "session-record",
            )

    def test_session_record_schema_pairs_continuation_consumption_state(self) -> None:
        golden = json.loads(
            (ROOT / "tests/fixtures/schemas/golden/session-record.json").read_text(
                encoding="utf-8"
            )
        )
        grant = {
            "command_id": "comment-1",
            "actor": "alice",
            "head_sha": "a" * 40,
            "policy_digest": "b" * 64,
            "issued_at": "2026-09-19T12:00:00Z",
            "expires_at": "2026-09-19T13:00:00Z",
            "consumed_reservation_id": None,
            "consumed_generation": None,
        }
        validate_public_document(
            dict(golden, continuation_grants=[grant]), "session-record"
        )
        for field, value in (
            ("consumed_reservation_id", "abcd1234"),
            ("consumed_generation", 1),
        ):
            invalid = dict(grant)
            invalid[field] = value
            with self.subTest(field=field):
                with self.assertRaises(ReviewInputError):
                    validate_public_document(
                        dict(golden, continuation_grants=[invalid]),
                        "session-record",
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
