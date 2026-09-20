from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from review_sensei.cli import _parser, _transaction_provider_identity, main
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.errors import ReviewInputError
from review_sensei.hosting.github import (
    GitHubApplication,
    GitHubWriteOptions,
    PublicationResult,
)
from review_sensei.hosting.github.errors import GitHubPublicationError
from review_sensei.models import ProviderResponse, ReviewResult, ReviewTransaction
from review_sensei.outcomes import RunOutcome
from review_sensei.providers import ProviderSettings
from review_sensei.schemas import validate_public_document
from review_sensei.service import ReviewRun
from review_sensei.session import (
    InMemorySessionLedger,
    LocalSessionLedger,
    SessionIdentity,
    checkpoint_review_analysis,
    complete_review_publication,
    load_review_transaction_for_publication,
    prepare_review_transaction,
    reclaim_abandoned_review_transaction,
    session_reservation_id,
)

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
IDENTITY = SessionIdentity("owner/repo", 146, repository_id=99)
BASE_SHA = "b" * 40
HEAD_SHA = "a" * 40
POLICY = ReviewConvergencePolicy(mode="merge-focused")
CONFIGURATION = {
    "provider": {
        "name": "fixture",
        "profile": None,
        "base_url": None,
        "timeout_seconds": 900.0,
        "max_output_tokens": None,
        "allow_custom_endpoint": False,
        "openrouter_policy": None,
    },
    "model": "fixture-model",
    "stages": [],
    "category_policy": [],
    "orchestration": {"enabled": False, "continue_rounds": 0},
    "publication_mode": "merge-focused",
}
CONFIGURATION_DIGEST = ReviewTransaction.compute_configuration_digest(CONFIGURATION)
EVIDENCE = {"evidence_policy": "legacy", "snapshot_sha256": None}
EVIDENCE_DIGEST = ReviewTransaction.compute_evidence_digest(EVIDENCE)


def _result() -> ReviewResult:
    return ReviewResult(
        summary="Complete.",
        comments=(),
        provider="fixture",
        model="fixture-model",
        review_status="complete",
    )


def _checkpoint(ledger: InMemorySessionLedger) -> ReviewResult:
    reservation = session_reservation_id(
        repository=IDENTITY.repository,
        pull_request=IDENTITY.pull_request,
        head_sha=HEAD_SHA,
        kind="publish",
    )
    prepared = prepare_review_transaction(
        ledger,
        IDENTITY,
        POLICY,
        reservation_id=reservation,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
        configuration_digest=CONFIGURATION_DIGEST,
        evidence_digest=EVIDENCE_DIGEST,
        now=NOW,
    )
    return checkpoint_review_analysis(ledger, IDENTITY, prepared, _result(), now=NOW)


class ReviewTransactionTests(unittest.TestCase):
    def test_transaction_model_uses_named_profile_default(self):
        args = _parser().parse_args(
            ["--provider", "ollama", "--profile", "deep-verification"]
        )
        settings = ProviderSettings(
            name=args.provider,
            profile=args.profile,
        )
        provider_identity, model = _transaction_provider_identity(settings)
        self.assertEqual(
            provider_identity["profile"],
            "deep-verification",
        )
        self.assertEqual(
            provider_identity["base_url"],
            "https://ollama.com/api",
        )
        self.assertEqual(
            model,
            "deepseek-v4.1-flash:cloud",
        )

    def test_configuration_digest_is_canonical_across_mapping_order(self):
        reordered = {
            "publication_mode": CONFIGURATION["publication_mode"],
            "orchestration": CONFIGURATION["orchestration"],
            "category_policy": CONFIGURATION["category_policy"],
            "stages": CONFIGURATION["stages"],
            "model": CONFIGURATION["model"],
            "provider": CONFIGURATION["provider"],
        }
        self.assertEqual(
            CONFIGURATION_DIGEST,
            ReviewTransaction.compute_configuration_digest(reordered),
        )
        changed_provider = {
            **CONFIGURATION,
            "provider": {
                **CONFIGURATION["provider"],
                "base_url": "https://alternate.example/api",
            },
        }
        self.assertNotEqual(
            CONFIGURATION_DIGEST,
            ReviewTransaction.compute_configuration_digest(changed_provider),
        )

    def test_configuration_digest_rejects_untrusted_nested_shapes(self):
        malformed_values = (
            {
                **CONFIGURATION,
                "provider": {
                    **CONFIGURATION["provider"],
                    "api_key_preview": "sk-live-secret",
                },
            },
            {**CONFIGURATION, "stages": {}},
            {
                **CONFIGURATION,
                "provider": {
                    **CONFIGURATION["provider"],
                    "base_url": "https://user:password@example.test/api",
                },
            },
        )
        for malformed in malformed_values:
            with self.subTest(malformed=malformed), self.assertRaises(ReviewInputError):
                ReviewTransaction.compute_configuration_digest(malformed)

    def test_content_digest_ignores_runtime_only_serialized_fields(self):
        result = _result()
        baseline = result.content_digest()
        serialized = result.to_dict()
        serialized["runtime_only"] = "not part of the v1 result identity"
        with patch.object(ReviewResult, "to_dict", return_value=serialized):
            self.assertEqual(result.content_digest(), baseline)

    def test_content_digest_rejects_missing_required_projection_fields(self):
        result = _result()
        serialized = result.to_dict()
        serialized.pop("summary")
        with patch.object(ReviewResult, "to_dict", return_value=serialized):
            with self.assertRaisesRegex(ReviewInputError, "summary"):
                result.content_digest()

    def test_transaction_schema_matches_phase_digest_rules(self):
        transaction = _checkpoint(InMemorySessionLedger()).transaction
        analysis_with_digest = {
            **transaction.to_dict(),
            "phase": "analysis",
        }
        with self.assertRaises(ReviewInputError):
            validate_public_document(analysis_with_digest, "review-transaction")

    def test_result_checkpoint_counts_once_and_replays(self):
        ledger = InMemorySessionLedger()
        result = _checkpoint(ledger)
        record = ledger.load(IDENTITY).record
        self.assertEqual(record.completed_initial_reviews, 1)
        self.assertEqual(record.transaction.phase, "publication_pending")
        replay = load_review_transaction_for_publication(
            ledger,
            IDENTITY,
            result,
            base_sha=BASE_SHA,
            head_sha=HEAD_SHA,
            policy_digest=POLICY.digest(),
            configuration_digest=CONFIGURATION_DIGEST,
            evidence_digest=EVIDENCE_DIGEST,
            now=NOW,
        )
        self.assertEqual(replay.completed_initial_reviews, 1)
        self.assertEqual(replay.transaction.result_sha256, result.content_digest())

    def test_publication_failure_retry_does_not_infer_or_count(self):
        ledger = InMemorySessionLedger()
        result = _checkpoint(ledger)
        failed = complete_review_publication(
            ledger, IDENTITY, result.transaction, published=False, now=NOW
        )
        self.assertEqual(failed.transaction.phase, "publication_failed")
        self.assertEqual(failed.completed_initial_reviews, 1)
        succeeded = complete_review_publication(
            ledger, IDENTITY, failed.transaction, published=True, now=NOW
        )
        self.assertEqual(succeeded.transaction.phase, "publication_succeeded")
        self.assertEqual(succeeded.completed_initial_reviews, 1)

    def test_publication_failure_observed_after_success_is_idempotent(self):
        ledger = InMemorySessionLedger()
        result = _checkpoint(ledger)
        succeeded = complete_review_publication(
            ledger, IDENTITY, result.transaction, published=True, now=NOW
        )
        replay = complete_review_publication(
            ledger, IDENTITY, succeeded.transaction, published=False, now=NOW
        )
        self.assertEqual(replay.transaction.phase, "publication_succeeded")
        self.assertEqual(replay.generation, succeeded.generation)
        self.assertEqual(replay.completed_initial_reviews, 1)

    def test_trusted_context_mismatch_fails_closed(self):
        ledger = InMemorySessionLedger()
        result = _checkpoint(ledger)
        with self.assertRaisesRegex(ReviewInputError, "trusted context"):
            load_review_transaction_for_publication(
                ledger,
                IDENTITY,
                result,
                base_sha=BASE_SHA,
                head_sha=HEAD_SHA,
                policy_digest=POLICY.digest(),
                configuration_digest="f" * 64,
                evidence_digest=EVIDENCE_DIGEST,
                now=NOW,
            )

    def test_durable_result_digest_mismatch_fails_closed(self):
        ledger = InMemorySessionLedger()
        result = _checkpoint(ledger)
        tampered_payload = {**result.to_dict(), "summary": "Tampered."}
        tampered_payload.pop("transaction")
        tampered_unbound = ReviewResult.from_dict(tampered_payload)
        tampered_payload["transaction"] = {
            **result.transaction.to_dict(),
            "result_sha256": tampered_unbound.content_digest(),
        }
        tampered = ReviewResult.from_dict(tampered_payload)
        with self.assertRaisesRegex(ReviewInputError, "durable result digest"):
            load_review_transaction_for_publication(
                ledger,
                IDENTITY,
                tampered,
                base_sha=BASE_SHA,
                head_sha=HEAD_SHA,
                policy_digest=POLICY.digest(),
                configuration_digest=CONFIGURATION_DIGEST,
                evidence_digest=EVIDENCE_DIGEST,
                now=NOW,
            )

    def test_generation_and_reservation_mismatch_fail_closed(self):
        ledger = InMemorySessionLedger()
        result = _checkpoint(ledger)
        tampered = ReviewResult.from_dict(
            {
                **result.to_dict(),
                "transaction": {
                    **result.transaction.to_dict(),
                    "generation": result.transaction.generation + 1,
                },
            }
        )
        with self.assertRaisesRegex(ReviewInputError, "generation"):
            load_review_transaction_for_publication(
                ledger,
                IDENTITY,
                tampered,
                base_sha=BASE_SHA,
                head_sha=HEAD_SHA,
                policy_digest=POLICY.digest(),
                configuration_digest=CONFIGURATION_DIGEST,
                evidence_digest=EVIDENCE_DIGEST,
                now=NOW,
            )

    def test_failed_attempt_cleanup_cannot_charge_checkpointed_transaction(self):
        ledger = InMemorySessionLedger()
        result = _checkpoint(ledger)
        with self.assertRaisesRegex(ReviewInputError, "ownership"):
            from review_sensei.session import record_session_failed_attempt

            record_session_failed_attempt(
                ledger,
                IDENTITY,
                reservation_id=result.transaction.reservation_id,
                expected_generation=result.transaction.generation - 1,
                now=NOW,
            )
        record = ledger.load(IDENTITY).record
        self.assertEqual(record.completed_initial_reviews, 1)
        self.assertEqual(record.failed_attempts, 0)

    def test_abandoned_reclaim_requires_owner_and_generation(self):
        ledger = InMemorySessionLedger()
        reservation = session_reservation_id(
            repository=IDENTITY.repository,
            pull_request=IDENTITY.pull_request,
            head_sha=HEAD_SHA,
            kind="publish",
        )
        prepared = prepare_review_transaction(
            ledger,
            IDENTITY,
            POLICY,
            reservation_id=reservation,
            base_sha=BASE_SHA,
            head_sha=HEAD_SHA,
            configuration_digest=CONFIGURATION_DIGEST,
            evidence_digest=EVIDENCE_DIGEST,
            now=NOW,
        )
        with self.assertRaisesRegex(ReviewInputError, "ownership"):
            reclaim_abandoned_review_transaction(
                ledger,
                IDENTITY,
                reservation_id=reservation,
                expected_generation=prepared.record.generation - 1,
                now=NOW,
            )
        reclaimed = reclaim_abandoned_review_transaction(
            ledger,
            IDENTITY,
            reservation_id=reservation,
            expected_generation=prepared.record.generation,
            now=NOW,
        )
        self.assertIsNone(reclaimed.reservation_id)
        self.assertIsNone(reclaimed.transaction)

    def test_invalid_transaction_context_does_not_reserve(self):
        ledger = InMemorySessionLedger()
        reservation = session_reservation_id(
            repository=IDENTITY.repository,
            pull_request=IDENTITY.pull_request,
            head_sha=HEAD_SHA,
            kind="publish",
        )
        with self.assertRaisesRegex(ReviewInputError, "configuration digest"):
            prepare_review_transaction(
                ledger,
                IDENTITY,
                POLICY,
                reservation_id=reservation,
                base_sha=BASE_SHA,
                head_sha=HEAD_SHA,
                configuration_digest="not-a-digest",
                evidence_digest=EVIDENCE_DIGEST,
                now=NOW,
            )
        self.assertEqual(ledger.load(IDENTITY).status, "missing")

    def test_result_and_session_documents_validate_transaction_schema(self):
        ledger = InMemorySessionLedger()
        result = _checkpoint(ledger)
        restored = ReviewResult.from_dict(result.to_dict())
        self.assertEqual(
            restored.transaction.transaction_id, result.transaction.transaction_id
        )
        record = ledger.load(IDENTITY).record
        self.assertEqual(
            record.transaction.transaction_id, result.transaction.transaction_id
        )

    def test_cli_checkpoint_uses_one_provider_call_and_one_reservation(self):
        class RecordingProvider:
            name = "fixture"
            model = "fixture-v1"

            def __init__(self):
                self.calls = 0

            def complete(self, request):
                self.calls += 1
                return ProviderResponse(
                    text=json.dumps({"summary": "ok", "comments": []}),
                    provider=self.name,
                    model=self.model,
                )

        class RecordingRegistry:
            def __init__(self, provider):
                self.provider = provider

            def create(self, settings):
                return self.provider

        diff = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1,2 @@
 keep
+change
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            diff_path = root / "review.patch"
            response_path = root / "response.json"
            output_path = root / "review.json"
            configuration_path = root / "configuration.json"
            ledger_path = root / "ledger"
            diff_path.write_text(diff, encoding="utf-8")
            response_path.write_text(
                json.dumps({"summary": "ok", "comments": []}), encoding="utf-8"
            )
            provider = RecordingProvider()
            argv = [
                "--diff",
                str(diff_path),
                "--provider",
                "fixture",
                "--fixture-response",
                str(response_path),
                "--model",
                "fixture-v1",
                "--repository",
                IDENTITY.repository,
                "--pull-request",
                str(IDENTITY.pull_request),
                "--base-sha",
                BASE_SHA,
                "--head-sha",
                HEAD_SHA,
                "--review-mode",
                "merge-focused",
                "--session-ledger",
                str(ledger_path),
                "--output",
                str(output_path),
                "--configuration-context-output",
                str(configuration_path),
                "--no-learning-proposals",
            ]
            with patch(
                "review_sensei.cli.default_registry",
                return_value=RecordingRegistry(provider),
            ):
                self.assertEqual(main(argv), 0)

            rendered = json.loads(output_path.read_text(encoding="utf-8"))
            configuration = json.loads(configuration_path.read_text(encoding="utf-8"))
            self.assertEqual(provider.calls, 1)
            self.assertEqual(rendered["transaction"]["phase"], "publication_pending")
            self.assertEqual(configuration["provider"]["name"], "fixture")
            self.assertEqual(configuration["model"], "fixture-v1")
            record = LocalSessionLedger(ledger_path).load(IDENTITY).record
            self.assertEqual(record.completed_initial_reviews, 1)
            self.assertIsNone(record.reservation_id)
            self.assertEqual(
                record.transaction.result_sha256,
                ReviewResult.from_dict(rendered).content_digest(),
            )

    def test_cli_ledger_without_context_output_keeps_non_transaction_path(self):
        class RecordingProvider:
            name = "fixture"
            model = "fixture-v1"

            def complete(self, request):
                return ProviderResponse(
                    text=json.dumps({"summary": "ok", "comments": []}),
                    provider=self.name,
                    model=self.model,
                )

        class RecordingRegistry:
            def create(self, settings):
                return RecordingProvider()

        diff = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1,2 @@
 keep
+change
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            diff_path = root / "review.patch"
            response_path = root / "response.json"
            output_path = root / "review.json"
            ledger_path = root / "ledger"
            diff_path.write_text(diff, encoding="utf-8")
            response_path.write_text(
                json.dumps({"summary": "ok", "comments": []}), encoding="utf-8"
            )
            argv = [
                "--diff",
                str(diff_path),
                "--provider",
                "fixture",
                "--fixture-response",
                str(response_path),
                "--model",
                "fixture-v1",
                "--repository",
                IDENTITY.repository,
                "--pull-request",
                str(IDENTITY.pull_request),
                "--base-sha",
                BASE_SHA,
                "--head-sha",
                HEAD_SHA,
                "--review-mode",
                "merge-focused",
                "--session-ledger",
                str(ledger_path),
                "--output",
                str(output_path),
                "--no-learning-proposals",
            ]
            with patch(
                "review_sensei.cli.default_registry",
                return_value=RecordingRegistry(),
            ):
                self.assertEqual(main(argv), 0)

            rendered = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertNotIn("transaction", rendered)
            record = LocalSessionLedger(ledger_path).load(IDENTITY).record
            self.assertIsNone(record.transaction)
            self.assertIsNotNone(record.reservation_id)

    def test_cli_context_write_failure_does_not_persist_transaction(self):
        class RecordingProvider:
            name = "fixture"
            model = "fixture-v1"

            def complete(self, request):
                return ProviderResponse(
                    text=json.dumps({"summary": "ok", "comments": []}),
                    provider=self.name,
                    model=self.model,
                )

        class RecordingRegistry:
            def create(self, settings):
                return RecordingProvider()

        diff = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1,2 @@
 keep
+change
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            diff_path = root / "review.patch"
            response_path = root / "response.json"
            output_path = root / "review.json"
            configuration_path = root / "missing" / "configuration.json"
            ledger_path = root / "ledger"
            diff_path.write_text(diff, encoding="utf-8")
            response_path.write_text(
                json.dumps({"summary": "ok", "comments": []}), encoding="utf-8"
            )
            argv = [
                "--diff",
                str(diff_path),
                "--provider",
                "fixture",
                "--fixture-response",
                str(response_path),
                "--model",
                "fixture-v1",
                "--repository",
                IDENTITY.repository,
                "--pull-request",
                str(IDENTITY.pull_request),
                "--base-sha",
                BASE_SHA,
                "--head-sha",
                HEAD_SHA,
                "--review-mode",
                "merge-focused",
                "--session-ledger",
                str(ledger_path),
                "--output",
                str(output_path),
                "--configuration-context-output",
                str(configuration_path),
                "--no-learning-proposals",
            ]
            with patch(
                "review_sensei.cli.default_registry",
                return_value=RecordingRegistry(),
            ):
                with redirect_stderr(io.StringIO()):
                    self.assertEqual(main(argv), 1)

            record = LocalSessionLedger(ledger_path).load(IDENTITY).record
            self.assertIsNone(record.transaction)
            self.assertIsNone(record.reservation_id)
            self.assertEqual(record.failed_attempts, 1)

    def test_cli_partial_transaction_result_is_returned_and_bounded(self):
        class RecordingProvider:
            name = "fixture"
            model = "fixture-v1"

            def complete(self, request):
                return ProviderResponse(
                    text=json.dumps({"summary": "ok", "comments": []}),
                    provider=self.name,
                    model=self.model,
                )

        class RecordingRegistry:
            def create(self, settings):
                return RecordingProvider()

        diff = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1,2 @@
 keep
+change
"""
        partial = ReviewResult(
            summary="Partial.",
            comments=(),
            provider="fixture",
            model="fixture-v1",
            review_status="partial",
        )
        run = ReviewRun(
            RunOutcome(
                "partial",
                repository=IDENTITY.repository,
                pull_request_number=IDENTITY.pull_request,
                diagnostic="partial_coverage",
                provider_calls=1,
            ),
            result=partial,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            diff_path = root / "review.patch"
            response_path = root / "response.json"
            output_path = root / "review.json"
            configuration_path = root / "configuration.json"
            ledger_path = root / "ledger"
            diff_path.write_text(diff, encoding="utf-8")
            response_path.write_text(
                json.dumps({"summary": "ok", "comments": []}), encoding="utf-8"
            )
            argv = [
                "--diff",
                str(diff_path),
                "--provider",
                "fixture",
                "--fixture-response",
                str(response_path),
                "--model",
                "fixture-v1",
                "--repository",
                IDENTITY.repository,
                "--pull-request",
                str(IDENTITY.pull_request),
                "--base-sha",
                BASE_SHA,
                "--head-sha",
                HEAD_SHA,
                "--review-mode",
                "merge-focused",
                "--session-ledger",
                str(ledger_path),
                "--output",
                str(output_path),
                "--configuration-context-output",
                str(configuration_path),
                "--no-learning-proposals",
            ]
            with (
                patch(
                    "review_sensei.cli.default_registry",
                    return_value=RecordingRegistry(),
                ),
                patch("review_sensei.cli.ReviewService.run", return_value=run),
            ):
                self.assertEqual(main(argv), 0)

            rendered = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(rendered["review_status"], "partial")
            self.assertNotIn("transaction", rendered)
            self.assertFalse(configuration_path.exists())
            record = LocalSessionLedger(ledger_path).load(IDENTITY).record
            self.assertIsNone(record.transaction)
            self.assertIsNone(record.reservation_id)
            self.assertEqual(record.failed_attempts, 1)

    def test_cli_analysis_failure_cleans_transaction_reservation(self):
        class RecordingProvider:
            name = "fixture"
            model = "fixture-v1"

            def complete(self, request):
                return ProviderResponse(
                    text=json.dumps({"summary": "ok", "comments": []}),
                    provider=self.name,
                    model=self.model,
                )

        class RecordingRegistry:
            def create(self, settings):
                return RecordingProvider()

        diff = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1,2 @@
 keep
+change
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            diff_path = root / "review.patch"
            response_path = root / "response.json"
            configuration_path = root / "configuration.json"
            ledger_path = root / "ledger"
            diff_path.write_text(diff, encoding="utf-8")
            response_path.write_text(
                json.dumps({"summary": "ok", "comments": []}), encoding="utf-8"
            )
            argv = [
                "--diff",
                str(diff_path),
                "--provider",
                "fixture",
                "--fixture-response",
                str(response_path),
                "--model",
                "fixture-v1",
                "--repository",
                IDENTITY.repository,
                "--pull-request",
                str(IDENTITY.pull_request),
                "--base-sha",
                BASE_SHA,
                "--head-sha",
                HEAD_SHA,
                "--review-mode",
                "merge-focused",
                "--session-ledger",
                str(ledger_path),
                "--configuration-context-output",
                str(configuration_path),
                "--no-learning-proposals",
            ]
            with (
                patch(
                    "review_sensei.cli.default_registry",
                    return_value=RecordingRegistry(),
                ),
                patch(
                    "review_sensei.cli.ReviewService.run",
                    side_effect=RuntimeError("analysis failed"),
                ),
                redirect_stderr(io.StringIO()),
            ):
                with self.assertRaisesRegex(RuntimeError, "analysis failed"):
                    main(argv)

            record = LocalSessionLedger(ledger_path).load(IDENTITY).record
            self.assertIsNone(record.transaction)
            self.assertIsNone(record.reservation_id)
            self.assertEqual(record.failed_attempts, 1)

    def test_cli_preserves_checkpoint_when_replace_reports_after_commit(self):
        class RecordingProvider:
            name = "fixture"
            model = "fixture-v1"

            def complete(self, request):
                return ProviderResponse(
                    text=json.dumps({"summary": "ok", "comments": []}),
                    provider=self.name,
                    model=self.model,
                )

        class RecordingRegistry:
            def create(self, settings):
                return RecordingProvider()

        diff = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1,2 @@
 keep
+change
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            diff_path = root / "review.patch"
            response_path = root / "response.json"
            configuration_path = root / "configuration.json"
            ledger_path = root / "ledger"
            diff_path.write_text(diff, encoding="utf-8")
            response_path.write_text(
                json.dumps({"summary": "ok", "comments": []}), encoding="utf-8"
            )
            argv = [
                "--diff",
                str(diff_path),
                "--provider",
                "fixture",
                "--fixture-response",
                str(response_path),
                "--model",
                "fixture-v1",
                "--repository",
                IDENTITY.repository,
                "--pull-request",
                str(IDENTITY.pull_request),
                "--base-sha",
                BASE_SHA,
                "--head-sha",
                HEAD_SHA,
                "--review-mode",
                "merge-focused",
                "--session-ledger",
                str(ledger_path),
                "--configuration-context-output",
                str(configuration_path),
                "--no-learning-proposals",
            ]

            from review_sensei.session import checkpoint_review_analysis as checkpoint

            def commit_then_raise(*args, **kwargs):
                checkpoint(*args, **kwargs)
                raise RuntimeError("checkpoint response lost after commit")

            with (
                patch(
                    "review_sensei.cli.default_registry",
                    return_value=RecordingRegistry(),
                ),
                patch(
                    "review_sensei.session.checkpoint_review_analysis",
                    side_effect=commit_then_raise,
                ),
                redirect_stderr(io.StringIO()),
            ):
                with self.assertRaisesRegex(RuntimeError, "response lost") as caught:
                    main(argv)

            self.assertFalse(getattr(caught.exception, "__notes__", ()))
            record = LocalSessionLedger(ledger_path).load(IDENTITY).record
            self.assertEqual(record.completed_initial_reviews, 1)
            self.assertEqual(record.failed_attempts, 0)
            self.assertIsNone(record.reservation_id)
            self.assertEqual(record.transaction.phase, "publication_pending")

    def test_cli_keyboard_interrupt_aborts_transaction_without_failed_attempt(self):
        class RecordingProvider:
            name = "fixture"
            model = "fixture-v1"

            def complete(self, request):
                return ProviderResponse(
                    text=json.dumps({"summary": "ok", "comments": []}),
                    provider=self.name,
                    model=self.model,
                )

        class RecordingRegistry:
            def create(self, settings):
                return RecordingProvider()

        diff = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1,2 @@
 keep
+change
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            diff_path = root / "review.patch"
            response_path = root / "response.json"
            configuration_path = root / "configuration.json"
            ledger_path = root / "ledger"
            diff_path.write_text(diff, encoding="utf-8")
            response_path.write_text(
                json.dumps({"summary": "ok", "comments": []}), encoding="utf-8"
            )
            argv = [
                "--diff",
                str(diff_path),
                "--provider",
                "fixture",
                "--fixture-response",
                str(response_path),
                "--model",
                "fixture-v1",
                "--repository",
                IDENTITY.repository,
                "--pull-request",
                str(IDENTITY.pull_request),
                "--base-sha",
                BASE_SHA,
                "--head-sha",
                HEAD_SHA,
                "--review-mode",
                "merge-focused",
                "--session-ledger",
                str(ledger_path),
                "--configuration-context-output",
                str(configuration_path),
                "--no-learning-proposals",
            ]
            with (
                patch(
                    "review_sensei.cli.default_registry",
                    return_value=RecordingRegistry(),
                ),
                patch(
                    "review_sensei.cli.ReviewService.run",
                    side_effect=KeyboardInterrupt(),
                ),
                redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    main(argv)

            record = LocalSessionLedger(ledger_path).load(IDENTITY).record
            self.assertIsNone(record.transaction)
            self.assertIsNone(record.reservation_id)
            self.assertEqual(record.failed_attempts, 0)


class _Broker:
    def __init__(self):
        self.exchanges = 0

    def request_oidc_token(self):
        return "oidc"

    def exchange(self, token, *, capability=None):
        self.exchanges += 1
        return "capability"


class _Reviewer:
    def __init__(self):
        self.calls = 0

    def publish(self, **kwargs):
        self.calls += 1
        return PublicationResult(status="published", review_id=1)


class _Noop:
    pass


class PublicationTransactionTests(unittest.TestCase):
    def test_application_rejects_bound_result_before_broker_without_ledger(self):
        result = _checkpoint(InMemorySessionLedger())
        broker = _Broker()
        application = GitHubApplication(
            broker=broker,
            http=None,
            reviewer=_Reviewer(),
            learner=_Noop(),
            replier=_Noop(),
            session_ledger=None,
        )
        with self.assertRaisesRegex(
            GitHubPublicationError, "requires a session ledger"
        ):
            application.publish_review(
                options=GitHubWriteOptions(auto_review=True, github_writes=True),
                oidc_token="oidc",
                repository=IDENTITY.repository,
                repository_id=IDENTITY.repository_id,
                pull_request=IDENTITY.pull_request,
                head_sha=HEAD_SHA,
                base_branch="main",
                base_sha=BASE_SHA,
                result=result,
                diff="diff",
                app_slug="review-sensei[bot]",
                convergence_policy=POLICY,
                configuration_context=CONFIGURATION,
                evidence_context=EVIDENCE,
            )
        self.assertEqual(broker.exchanges, 0)

    def test_application_reuses_checkpoint_without_reservation_or_inference(self):
        ledger = InMemorySessionLedger()
        result = _checkpoint(ledger)
        broker = _Broker()
        reviewer = _Reviewer()
        application = GitHubApplication(
            broker=broker,
            http=None,
            reviewer=reviewer,
            learner=_Noop(),
            replier=_Noop(),
            session_ledger=ledger,
        )
        outcome = application.publish_review(
            options=GitHubWriteOptions(auto_review=True, github_writes=True),
            oidc_token="oidc",
            repository=IDENTITY.repository,
            repository_id=IDENTITY.repository_id,
            pull_request=IDENTITY.pull_request,
            head_sha=HEAD_SHA,
            base_branch="main",
            base_sha=BASE_SHA,
            result=result,
            diff="diff",
            app_slug="review-sensei[bot]",
            convergence_policy=POLICY,
            configuration_context=CONFIGURATION,
            evidence_context=EVIDENCE,
        )
        self.assertEqual(outcome.status, "published")
        self.assertEqual(reviewer.calls, 1)
        self.assertEqual(broker.exchanges, 1)
        record = ledger.load(IDENTITY).record
        self.assertEqual(record.completed_initial_reviews, 1)
        self.assertEqual(record.transaction.phase, "publication_succeeded")

    def test_application_keyboard_interrupt_leaves_transaction_pending(self):
        ledger = InMemorySessionLedger()
        result = _checkpoint(ledger)
        broker = _Broker()
        reviewer = _Reviewer()
        application = GitHubApplication(
            broker=broker,
            http=None,
            reviewer=reviewer,
            learner=_Noop(),
            replier=_Noop(),
            session_ledger=ledger,
        )
        with patch.object(reviewer, "publish", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                application.publish_review(
                    options=GitHubWriteOptions(auto_review=True, github_writes=True),
                    oidc_token="oidc",
                    repository=IDENTITY.repository,
                    repository_id=IDENTITY.repository_id,
                    pull_request=IDENTITY.pull_request,
                    head_sha=HEAD_SHA,
                    base_branch="main",
                    base_sha=BASE_SHA,
                    result=result,
                    diff="diff",
                    app_slug="review-sensei[bot]",
                    convergence_policy=POLICY,
                    configuration_context=CONFIGURATION,
                    evidence_context=EVIDENCE,
                )
        record = ledger.load(IDENTITY).record
        self.assertEqual(record.transaction.phase, "publication_pending")
        self.assertIsNone(record.reservation_id)
        self.assertEqual(record.completed_initial_reviews, 1)

    def test_application_replays_prepublication_result_after_success_idempotently(
        self,
    ):
        ledger = InMemorySessionLedger()
        result = _checkpoint(ledger)
        complete_review_publication(
            ledger, IDENTITY, result.transaction, published=True, now=NOW
        )
        broker = _Broker()
        reviewer = _Reviewer()
        application = GitHubApplication(
            broker=broker,
            http=None,
            reviewer=reviewer,
            learner=_Noop(),
            replier=_Noop(),
            session_ledger=ledger,
        )

        outcome = application.publish_review(
            options=GitHubWriteOptions(auto_review=True, github_writes=True),
            oidc_token="oidc",
            repository=IDENTITY.repository,
            repository_id=IDENTITY.repository_id,
            pull_request=IDENTITY.pull_request,
            head_sha=HEAD_SHA,
            base_branch="main",
            base_sha=BASE_SHA,
            result=result,
            diff="diff",
            app_slug="review-sensei[bot]",
            convergence_policy=POLICY,
            configuration_context=CONFIGURATION,
            evidence_context=EVIDENCE,
        )

        self.assertEqual(outcome.status, "already_published")
        self.assertEqual(broker.exchanges, 0)
        self.assertEqual(reviewer.calls, 0)
        self.assertEqual(
            ledger.load(IDENTITY).record.transaction.phase,
            "publication_succeeded",
        )

    def test_application_requires_trusted_configuration_before_broker(self):
        ledger = InMemorySessionLedger()
        result = _checkpoint(ledger)
        broker = _Broker()
        reviewer = _Reviewer()
        application = GitHubApplication(
            broker=broker,
            http=None,
            reviewer=reviewer,
            learner=_Noop(),
            replier=_Noop(),
            session_ledger=ledger,
        )
        with self.assertRaisesRegex(GitHubPublicationError, "trusted configuration"):
            application.publish_review(
                options=GitHubWriteOptions(auto_review=True, github_writes=True),
                oidc_token="oidc",
                repository=IDENTITY.repository,
                repository_id=IDENTITY.repository_id,
                pull_request=IDENTITY.pull_request,
                head_sha=HEAD_SHA,
                base_branch="main",
                base_sha=BASE_SHA,
                result=result,
                diff="diff",
                app_slug="review-sensei[bot]",
                convergence_policy=POLICY,
            )
        self.assertEqual(broker.exchanges, 0)
        self.assertEqual(reviewer.calls, 0)

    def test_application_rejects_mismatched_configuration_before_broker(self):
        ledger = InMemorySessionLedger()
        result = _checkpoint(ledger)
        broker = _Broker()
        reviewer = _Reviewer()
        application = GitHubApplication(
            broker=broker,
            http=None,
            reviewer=reviewer,
            learner=_Noop(),
            replier=_Noop(),
            session_ledger=ledger,
        )
        mismatched_configuration = {**CONFIGURATION, "model": "different-model"}
        with self.assertRaisesRegex(
            GitHubPublicationError, "transaction validation failed"
        ):
            application.publish_review(
                options=GitHubWriteOptions(auto_review=True, github_writes=True),
                oidc_token="oidc",
                repository=IDENTITY.repository,
                repository_id=IDENTITY.repository_id,
                pull_request=IDENTITY.pull_request,
                head_sha=HEAD_SHA,
                base_branch="main",
                base_sha=BASE_SHA,
                result=result,
                diff="diff",
                app_slug="review-sensei[bot]",
                convergence_policy=POLICY,
                configuration_context=mismatched_configuration,
                evidence_context=EVIDENCE,
            )
        self.assertEqual(broker.exchanges, 0)
        self.assertEqual(reviewer.calls, 0)

    def test_application_classifies_malformed_configuration_before_broker(self):
        ledger = InMemorySessionLedger()
        result = _checkpoint(ledger)
        broker = _Broker()
        application = GitHubApplication(
            broker=broker,
            http=None,
            reviewer=_Reviewer(),
            learner=_Noop(),
            replier=_Noop(),
            session_ledger=ledger,
        )
        malformed_configuration = {
            **CONFIGURATION,
            "provider": {
                **CONFIGURATION["provider"],
                "api_key_preview": "sk-live-secret",
            },
        }
        with self.assertRaisesRegex(
            GitHubPublicationError, "configuration validation failed"
        ):
            application.publish_review(
                options=GitHubWriteOptions(auto_review=True, github_writes=True),
                oidc_token="oidc",
                repository=IDENTITY.repository,
                repository_id=IDENTITY.repository_id,
                pull_request=IDENTITY.pull_request,
                head_sha=HEAD_SHA,
                base_branch="main",
                base_sha=BASE_SHA,
                result=result,
                diff="diff",
                app_slug="review-sensei[bot]",
                convergence_policy=POLICY,
                configuration_context=malformed_configuration,
                evidence_context=EVIDENCE,
            )
        self.assertEqual(broker.exchanges, 0)

    def test_application_rebinds_to_durable_phase_on_retry(self):
        ledger = InMemorySessionLedger()
        result = _checkpoint(ledger)
        complete_review_publication(
            ledger, IDENTITY, result.transaction, published=False, now=NOW
        )
        broker = _Broker()
        reviewer = _Reviewer()
        application = GitHubApplication(
            broker=broker,
            http=None,
            reviewer=reviewer,
            learner=_Noop(),
            replier=_Noop(),
            session_ledger=ledger,
        )
        outcome = application.publish_review(
            options=GitHubWriteOptions(auto_review=True, github_writes=True),
            oidc_token="oidc",
            repository=IDENTITY.repository,
            repository_id=IDENTITY.repository_id,
            pull_request=IDENTITY.pull_request,
            head_sha=HEAD_SHA,
            base_branch="main",
            base_sha=BASE_SHA,
            result=result,
            diff="diff",
            app_slug="review-sensei[bot]",
            convergence_policy=POLICY,
            configuration_context=CONFIGURATION,
            evidence_context=EVIDENCE,
        )
        self.assertEqual(outcome.status, "published")
        self.assertEqual(reviewer.calls, 1)
        self.assertEqual(
            ledger.load(IDENTITY).record.transaction.phase, "publication_succeeded"
        )

    def test_application_revalidates_a_token_bound_ledger_after_exchange(self):
        local_ledger = InMemorySessionLedger()
        remote_ledger = InMemorySessionLedger()
        local_result = _checkpoint(local_ledger)
        _checkpoint(remote_ledger)
        complete_review_publication(
            local_ledger, IDENTITY, local_result.transaction, published=True, now=NOW
        )
        broker = _Broker()
        reviewer = _Reviewer()
        application = GitHubApplication(
            broker=broker,
            http=None,
            reviewer=reviewer,
            learner=_Noop(),
            replier=_Noop(),
            session_ledger=local_ledger,
        )
        with patch.object(
            application, "_session_ledger_for_token", return_value=remote_ledger
        ):
            outcome = application.publish_review(
                options=GitHubWriteOptions(
                    auto_review=True,
                    github_writes=True,
                    github_session_ledger=True,
                ),
                oidc_token="oidc",
                repository=IDENTITY.repository,
                repository_id=IDENTITY.repository_id,
                pull_request=IDENTITY.pull_request,
                head_sha=HEAD_SHA,
                base_branch="main",
                base_sha=BASE_SHA,
                result=local_result,
                diff="diff",
                app_slug="review-sensei[bot]",
                convergence_policy=POLICY,
                configuration_context=CONFIGURATION,
                evidence_context=EVIDENCE,
            )

        self.assertEqual(outcome.status, "published")
        self.assertEqual(broker.exchanges, 1)
        self.assertEqual(reviewer.calls, 1)
        self.assertEqual(
            local_ledger.load(IDENTITY).record.transaction.phase,
            "publication_succeeded",
        )
        self.assertEqual(
            remote_ledger.load(IDENTITY).record.transaction.phase,
            "publication_succeeded",
        )

    def test_application_rejects_mismatched_token_bound_ledger(self):
        local_ledger = InMemorySessionLedger()
        remote_ledger = InMemorySessionLedger()
        local_result = _checkpoint(local_ledger)
        reservation = session_reservation_id(
            repository=IDENTITY.repository,
            pull_request=IDENTITY.pull_request,
            head_sha=HEAD_SHA,
            kind="publish",
        )
        remote_prepared = prepare_review_transaction(
            remote_ledger,
            IDENTITY,
            POLICY,
            reservation_id=reservation,
            base_sha=BASE_SHA,
            head_sha=HEAD_SHA,
            configuration_digest="c" * 64,
            evidence_digest=EVIDENCE_DIGEST,
            now=NOW,
        )
        checkpoint_review_analysis(
            remote_ledger, IDENTITY, remote_prepared, _result(), now=NOW
        )
        complete_review_publication(
            local_ledger, IDENTITY, local_result.transaction, published=True, now=NOW
        )
        broker = _Broker()
        reviewer = _Reviewer()
        application = GitHubApplication(
            broker=broker,
            http=None,
            reviewer=reviewer,
            learner=_Noop(),
            replier=_Noop(),
            session_ledger=local_ledger,
        )
        with patch.object(
            application, "_session_ledger_for_token", return_value=remote_ledger
        ):
            with self.assertRaisesRegex(
                GitHubPublicationError, "transaction validation failed"
            ):
                application.publish_review(
                    options=GitHubWriteOptions(
                        auto_review=True,
                        github_writes=True,
                        github_session_ledger=True,
                    ),
                    oidc_token="oidc",
                    repository=IDENTITY.repository,
                    repository_id=IDENTITY.repository_id,
                    pull_request=IDENTITY.pull_request,
                    head_sha=HEAD_SHA,
                    base_branch="main",
                    base_sha=BASE_SHA,
                    result=local_result,
                    diff="diff",
                    app_slug="review-sensei[bot]",
                    convergence_policy=POLICY,
                    configuration_context=CONFIGURATION,
                    evidence_context=EVIDENCE,
                )

        self.assertEqual(broker.exchanges, 1)
        self.assertEqual(reviewer.calls, 0)


if __name__ == "__main__":
    unittest.main()
