from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from review_sensei.baseline import (
    ReviewBaseline,
    baseline_from_history_document,
    baseline_history_document,
)
from review_sensei.cli import (
    _checkpoint_cache_request,
    _parser,
    _transaction_provider_identity,
    main,
)
from review_sensei.context import ReviewContextCacheKey, finding_lifecycle_for_comment
from review_sensei.convergence import ReviewConvergencePolicy, derive_blocker_candidate
from review_sensei.errors import ReviewInputError
from review_sensei.hosting.github import (
    GitHubApplication,
    GitHubWriteOptions,
    PublicationResult,
    ReviewPublisher,
)
from review_sensei.hosting.github.errors import GitHubPublicationError
from review_sensei.models import (
    ProviderResponse,
    ReviewComment,
    ReviewRequest,
    ReviewResult,
    ReviewTransaction,
)
from review_sensei.outcomes import RunOutcome, run_outcome_exit_code
from review_sensei.providers import ProviderSettings
from review_sensei.schemas import validate_public_document
from review_sensei.service import ReviewRun, ReviewService
from review_sensei.session import (
    InMemorySessionLedger,
    LocalSessionLedger,
    SessionIdentity,
    blocker_set_digest,
    checkpoint_review_analysis,
    complete_review_publication,
    convergence_progress_blocker_sets,
    load_review_transaction_for_publication,
    prepare_review_transaction,
    reclaim_abandoned_review_transaction,
    record_admitted_blocker_progress,
    session_reservation_id,
    suppress_review_publication,
)

try:
    from fake_github_http import json_response, make_http
except ImportError:
    from tests.fake_github_http import json_response, make_http

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


def _baseline(generation: int) -> ReviewBaseline:
    return ReviewBaseline(
        cache_key=ReviewContextCacheKey(
            repository=IDENTITY.repository,
            pull_request=IDENTITY.pull_request,
            base_sha=BASE_SHA,
            head_sha=HEAD_SHA,
            engine="fixture",
            model="fixture-model",
            profile="default",
            stage_digest="1" * 64,
            context_digest="2" * 64,
            learning_digest="3" * 64,
        ),
        policy_digest=POLICY.digest(),
        complete=True,
        coverage_complete=True,
        generation=generation,
    )


def _checkpoint_with_baseline(ledger: InMemorySessionLedger) -> ReviewResult:
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
    return checkpoint_review_analysis(
        ledger,
        IDENTITY,
        prepared,
        _result(),
        baseline=_baseline(prepared.record.generation + 1),
        now=NOW,
    )


class ReviewTransactionTests(unittest.TestCase):
    def test_checkpoint_persists_completed_baseline_for_a_fresh_ledger(self):
        reservation = session_reservation_id(
            repository=IDENTITY.repository,
            pull_request=IDENTITY.pull_request,
            head_sha=HEAD_SHA,
            kind="publish",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_root = Path(temp_dir)
            ledger = LocalSessionLedger(ledger_root)
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
            baseline = ReviewBaseline(
                cache_key=ReviewContextCacheKey(
                    repository=IDENTITY.repository,
                    pull_request=IDENTITY.pull_request,
                    base_sha=BASE_SHA,
                    head_sha=HEAD_SHA,
                    engine="fixture",
                    model="fixture-model",
                    profile="default",
                    stage_digest="1" * 64,
                    context_digest="2" * 64,
                    learning_digest="3" * 64,
                ),
                policy_digest=POLICY.digest(),
                complete=True,
                coverage_complete=True,
                generation=prepared.record.generation + 1,
            )
            checkpoint_review_analysis(
                ledger, IDENTITY, prepared, _result(), baseline=baseline, now=NOW
            )

            loaded = LocalSessionLedger(ledger_root).load(IDENTITY, now=NOW)
            self.assertEqual(loaded.status, "ok")
            self.assertIsNotNone(loaded.record)
            assert loaded.record is not None
            self.assertEqual(loaded.record.completed_initial_reviews, 1)
            self.assertEqual(loaded.record.convergence_history["state"], "completed")
            self.assertEqual(
                baseline_from_history_document(
                    loaded.record.convergence_history["baseline"]
                ),
                baseline,
            )

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

    def test_policy_and_evidence_digests_reject_untrusted_shapes(self):
        policy = POLICY.identity_fields()
        self.assertEqual(
            ReviewTransaction.compute_policy_digest(policy), POLICY.digest()
        )
        malformed_policy = {**policy, "caller_supplied": "secret"}
        with self.assertRaises(ReviewInputError):
            ReviewTransaction.compute_policy_digest(malformed_policy)

        self.assertEqual(
            ReviewTransaction.compute_evidence_digest(EVIDENCE), EVIDENCE_DIGEST
        )
        malformed_evidence = {**EVIDENCE, "caller_supplied": "unbounded"}
        with self.assertRaises(ReviewInputError):
            ReviewTransaction.compute_evidence_digest(malformed_evidence)
        with self.assertRaises(ReviewInputError):
            ReviewTransaction.compute_evidence_digest(
                {"evidence_policy": "confirmed", "snapshot_sha256": None}
            )

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

    def test_transaction_phase_transitions_accept_only_publication_suppression_path(
        self,
    ):
        ledger = InMemorySessionLedger()
        prepared = prepare_review_transaction(
            ledger,
            IDENTITY,
            POLICY,
            reservation_id=session_reservation_id(
                repository=IDENTITY.repository,
                pull_request=IDENTITY.pull_request,
                head_sha=HEAD_SHA,
                kind="phase-transition",
            ),
            base_sha=BASE_SHA,
            head_sha=HEAD_SHA,
            configuration_digest=CONFIGURATION_DIGEST,
            evidence_digest=EVIDENCE_DIGEST,
            now=NOW,
        )
        analysis = prepared.transaction
        with self.assertRaisesRegex(ReviewInputError, "transition"):
            analysis.with_phase("publication_suppressed")
        failed = analysis.with_result("c" * 64).with_phase("publication_failed")
        suppressed = failed.with_phase("publication_suppressed")
        self.assertEqual(suppressed.phase, "publication_suppressed")

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

    def test_publication_suppression_is_terminal_and_idempotent(self):
        ledger = InMemorySessionLedger()
        result = _checkpoint(ledger)
        suppressed = suppress_review_publication(
            ledger, IDENTITY, result.transaction, now=NOW
        )
        self.assertEqual(suppressed.transaction.phase, "publication_suppressed")
        self.assertEqual(suppressed.completed_initial_reviews, 1)
        replay = suppress_review_publication(
            ledger, IDENTITY, suppressed.transaction, now=NOW
        )
        self.assertEqual(replay.generation, suppressed.generation)
        cleanup = complete_review_publication(
            ledger, IDENTITY, suppressed.transaction, published=True, now=NOW
        )
        self.assertEqual(cleanup.generation, suppressed.generation)
        self.assertEqual(cleanup.transaction.phase, "publication_suppressed")

    def test_admitted_blocker_progress_and_suppression_are_one_terminal_transition(
        self,
    ):
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
        baseline = ReviewBaseline(
            cache_key=ReviewContextCacheKey(
                repository=IDENTITY.repository,
                pull_request=IDENTITY.pull_request,
                base_sha=BASE_SHA,
                head_sha=HEAD_SHA,
                engine="fixture",
                model="fixture-model",
                profile="default",
                stage_digest="1" * 64,
                context_digest="2" * 64,
                learning_digest="3" * 64,
            ),
            policy_digest=POLICY.digest(),
            complete=True,
            coverage_complete=True,
            generation=prepared.record.generation + 1,
        )
        result = checkpoint_review_analysis(
            ledger, IDENTITY, prepared, _result(), baseline=baseline, now=NOW
        )
        suppressed = record_admitted_blocker_progress(
            ledger,
            IDENTITY,
            result.transaction,
            blocker_set_sha256="a" * 64,
            blocker_count=1,
            suppress_publication=True,
            now=NOW,
        )
        self.assertEqual(suppressed.transaction.phase, "publication_suppressed")
        self.assertEqual(
            suppressed.convergence_history["progress"],
            [
                {
                    "event": "completed",
                    "generation": result.transaction.generation,
                    "blocker_set_sha256": "a" * 64,
                    "blocker_count": 1,
                    "transaction_id": result.transaction.transaction_id,
                }
            ],
        )
        replay = record_admitted_blocker_progress(
            ledger,
            IDENTITY,
            result.transaction,
            blocker_set_sha256="a" * 64,
            blocker_count=1,
            suppress_publication=True,
            now=NOW,
        )
        self.assertEqual(replay.generation, suppressed.generation)
        with self.assertRaisesRegex(ReviewInputError, "blocker progress"):
            record_admitted_blocker_progress(
                ledger,
                IDENTITY,
                result.transaction,
                blocker_set_sha256="b" * 64,
                blocker_count=1,
                suppress_publication=True,
                now=NOW,
            )

    def test_admitted_blocker_progress_replay_is_idempotent_before_publication(self):
        ledger = InMemorySessionLedger()
        result = _checkpoint(ledger)
        baseline = ReviewBaseline(
            cache_key=ReviewContextCacheKey(
                repository=IDENTITY.repository,
                pull_request=IDENTITY.pull_request,
                base_sha=BASE_SHA,
                head_sha=HEAD_SHA,
                engine="fixture",
                model="fixture-model",
                profile="default",
                stage_digest="1" * 64,
                context_digest="2" * 64,
                learning_digest="3" * 64,
            ),
            policy_digest=POLICY.digest(),
            complete=True,
            coverage_complete=True,
            reviewed_paths=("src/app.py",),
            generation=result.transaction.generation,
        )
        record = ledger.load(IDENTITY, now=NOW).record
        assert record is not None
        ledger.replace(
            IDENTITY,
            lambda current: current.evolve(
                convergence_history={
                    "state": "completed",
                    "baseline": baseline_history_document(baseline),
                    "progress": [
                        {
                            "event": "completed",
                            "generation": result.transaction.generation,
                        }
                    ],
                    "provenance": {"ledger_digest": record.record_sha256},
                },
                now=NOW,
            ),
            now=NOW,
        )
        pending = record_admitted_blocker_progress(
            ledger,
            IDENTITY,
            result.transaction,
            blocker_set_sha256="a" * 64,
            blocker_count=1,
            suppress_publication=False,
            now=NOW,
        )
        replay = record_admitted_blocker_progress(
            ledger,
            IDENTITY,
            result.transaction,
            blocker_set_sha256="a" * 64,
            blocker_count=1,
            suppress_publication=False,
            now=NOW,
        )
        self.assertEqual(replay.generation, pending.generation)
        failed = complete_review_publication(
            ledger, IDENTITY, result.transaction, published=False, now=NOW
        )
        failed_replay = record_admitted_blocker_progress(
            ledger,
            IDENTITY,
            failed.transaction,
            blocker_set_sha256="a" * 64,
            blocker_count=1,
            suppress_publication=False,
            now=NOW,
        )
        self.assertEqual(failed_replay.generation, failed.generation)
        succeeded = complete_review_publication(
            ledger, IDENTITY, result.transaction, published=True, now=NOW
        )
        self.assertLess(
            succeeded.convergence_history["progress"][-1]["generation"],
            succeeded.generation,
        )
        succeeded_replay = record_admitted_blocker_progress(
            ledger,
            IDENTITY,
            succeeded.transaction,
            blocker_set_sha256="a" * 64,
            blocker_count=1,
            suppress_publication=False,
            now=NOW,
        )
        self.assertEqual(succeeded_replay.generation, succeeded.generation)

    def test_terminal_replay_requires_transaction_owned_progress_marker(self):
        ledger = InMemorySessionLedger()
        result = _checkpoint_with_baseline(ledger)
        pending = record_admitted_blocker_progress(
            ledger,
            IDENTITY,
            result.transaction,
            blocker_set_sha256="a" * 64,
            blocker_count=1,
            suppress_publication=False,
            now=NOW,
        )
        failed = complete_review_publication(
            ledger, IDENTITY, pending.transaction, published=False, now=NOW
        )
        record = ledger.load(IDENTITY, now=NOW).record
        assert record is not None
        history = dict(record.convergence_history)
        progress = list(history["progress"])
        progress[-1] = {**progress[-1], "transaction_id": "f" * 64}
        history["progress"] = progress
        ledger.replace(
            IDENTITY,
            lambda current: current.evolve(
                convergence_history=history,
                now=NOW,
            ),
            now=NOW,
        )
        with self.assertRaisesRegex(ReviewInputError, "does not match transaction"):
            record_admitted_blocker_progress(
                ledger,
                IDENTITY,
                failed.transaction,
                blocker_set_sha256="a" * 64,
                blocker_count=1,
                suppress_publication=False,
                now=NOW,
            )

    def test_checkpoint_preserves_comparable_blocker_window(self):
        ledger = InMemorySessionLedger()
        first = _checkpoint_with_baseline(ledger)
        complete_review_publication(
            ledger, IDENTITY, first.transaction, published=True, now=NOW
        )
        record = ledger.load(IDENTITY, now=NOW).record
        assert record is not None
        history = dict(record.convergence_history)
        history["progress"] = [
            {
                "event": "completed",
                "generation": 1,
                "blocker_set_sha256": "a" * 64,
                "blocker_count": 1,
            },
            {
                "event": "completed",
                "generation": 2,
                "blocker_set_sha256": "b" * 64,
                "blocker_count": 1,
            },
            {"event": "completed", "generation": 3},
        ]
        ledger.replace(
            IDENTITY,
            lambda current: current.evolve(
                convergence_history=history,
                now=NOW,
            ),
            now=NOW,
        )
        prepared = prepare_review_transaction(
            ledger,
            IDENTITY,
            POLICY,
            reservation_id="f" * 64,
            base_sha=BASE_SHA,
            head_sha=HEAD_SHA,
            configuration_digest=CONFIGURATION_DIGEST,
            evidence_digest=EVIDENCE_DIGEST,
            now=NOW,
        )
        checkpoint_review_analysis(
            ledger,
            IDENTITY,
            prepared,
            _result(),
            baseline=_baseline(prepared.record.generation + 1),
            now=NOW,
        )
        self.assertEqual(
            convergence_progress_blocker_sets(
                ledger.load(IDENTITY, now=NOW).record.convergence_history
            ),
            (("a" * 64, 1), ("b" * 64, 1)),
        )

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

    def test_abandoned_reclaim_releases_reservation_only_record(self):
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
        ledger.replace(
            IDENTITY,
            lambda record: record.evolve(transaction=None),
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
diff --git a/src/helper.py b/src/helper.py
--- a/src/helper.py
+++ b/src/helper.py
@@ -1 +1,2 @@
 keep
+related change
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            diff_path = root / "review.patch"
            response_path = root / "response.json"
            output_path = root / "review.json"
            outcome_path = root / "outcome.json"
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
                "--outcome",
                str(outcome_path),
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
            self.assertIsNotNone(record.convergence_history)
            assert record.convergence_history is not None
            self.assertEqual(record.convergence_history["state"], "completed")
            initial_baseline = baseline_from_history_document(
                record.convergence_history["baseline"]
            )
            self.assertEqual(initial_baseline.cache_key.head_sha, HEAD_SHA)
            self.assertEqual(
                initial_baseline.related_paths,
                ("src/helper.py", "src/app.py"),
            )
            self.assertEqual(
                record.transaction.result_sha256,
                ReviewResult.from_dict(rendered).content_digest(),
            )
            initial_history = record.convergence_history
            malformed_argv = list(argv)
            malformed_argv[malformed_argv.index("--head-sha") + 1] = "c" * 40
            with (
                patch(
                    "review_sensei.cli.baseline_from_history_document",
                    side_effect=ReviewInputError("corrupt persisted baseline"),
                ),
                patch(
                    "review_sensei.cli.default_registry",
                    return_value=RecordingRegistry(provider),
                ),
            ):
                malformed_exit = main(malformed_argv)
            self.assertEqual(malformed_exit, run_outcome_exit_code("action_required"))
            self.assertNotEqual(malformed_exit, 0)
            malformed_outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
            malformed_record = LocalSessionLedger(ledger_path).load(IDENTITY).record
            self.assertEqual(malformed_outcome["status"], "action_required")
            self.assertEqual(
                malformed_outcome["diagnostic"],
                "durable_baseline_recovery_required",
            )
            self.assertEqual(malformed_outcome["base_sha"], BASE_SHA)
            self.assertEqual(malformed_outcome["head_sha"], "c" * 40)
            malformed_provider_calls = provider.calls
            self.assertEqual(malformed_provider_calls, 1)
            self.assertIsNone(malformed_record.reservation_id)
            self.assertEqual(malformed_record.convergence_history, initial_history)

            stage_mismatch_history = json.loads(json.dumps(initial_history))
            stage_mismatch_head = "c" * 40
            # Keep the prior head stable so this exercises the stage digest
            # mismatch rather than a head mismatch short-circuit.
            stage_mismatch_history["baseline"]["cache_key"]["head_sha"] = HEAD_SHA
            stage_mismatch_history["baseline"]["cache_key"]["stage_digest"] = "f" * 64
            stage_mismatch_argv = list(argv)
            stage_mismatch_argv[stage_mismatch_argv.index("--head-sha") + 1] = (
                stage_mismatch_head
            )
            LocalSessionLedger(ledger_path).replace(
                IDENTITY,
                lambda current: current.evolve(
                    convergence_history=stage_mismatch_history
                ),
            )
            stage_mismatch_provider_calls = provider.calls
            with patch(
                "review_sensei.cli.default_registry",
                return_value=RecordingRegistry(provider),
            ):
                self.assertEqual(
                    main(stage_mismatch_argv), run_outcome_exit_code("action_required")
                )
            self.assertEqual(provider.calls, stage_mismatch_provider_calls)
            self.assertEqual(provider.calls, 1)
            stage_mismatch_outcome = json.loads(
                outcome_path.read_text(encoding="utf-8")
            )
            stage_mismatch_record = (
                LocalSessionLedger(ledger_path).load(IDENTITY).record
            )
            self.assertEqual(stage_mismatch_outcome["status"], "action_required")
            self.assertEqual(
                stage_mismatch_outcome["diagnostic"],
                "durable_baseline_recovery_required",
            )
            self.assertEqual(stage_mismatch_outcome["base_sha"], BASE_SHA)
            self.assertEqual(stage_mismatch_outcome["head_sha"], stage_mismatch_head)
            self.assertEqual(provider.calls, malformed_provider_calls)
            self.assertIsNone(stage_mismatch_record.reservation_id)

            LocalSessionLedger(ledger_path).replace(
                IDENTITY,
                lambda current: current.evolve(convergence_history=initial_history),
            )
            LocalSessionLedger(ledger_path).replace(
                IDENTITY,
                lambda current: current.evolve(convergence_history=None),
            )
            second_argv = list(argv)
            second_argv[second_argv.index("--head-sha") + 1] = "c" * 40
            with patch(
                "review_sensei.cli.default_registry",
                return_value=RecordingRegistry(provider),
            ):
                self.assertNotEqual(main(second_argv), 0)
            recovery_outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
            recovery_record = LocalSessionLedger(ledger_path).load(IDENTITY).record
            self.assertEqual(provider.calls, malformed_provider_calls)
            self.assertEqual(recovery_outcome["status"], "action_required")
            self.assertIsNone(recovery_record.reservation_id)
            self.assertEqual(recovery_record.failed_attempts, 0)
            # ``recovery-required`` is the closed-schema equivalent of an
            # in-progress/non-completed history: it must take the same
            # fail-closed branch rather than admitting inference.
            recovery_history = {
                "state": "recovery-required",
                "progress": [{"event": "recovery-required", "generation": 1}],
                "provenance": {"ledger_digest": "0" * 64},
            }
            LocalSessionLedger(ledger_path).replace(
                IDENTITY,
                lambda current: current.evolve(convergence_history=recovery_history),
            )
            with patch(
                "review_sensei.cli.default_registry",
                return_value=RecordingRegistry(provider),
            ):
                self.assertNotEqual(main(second_argv), 0)
            recovery_outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
            recovery_record = LocalSessionLedger(ledger_path).load(IDENTITY).record
            self.assertEqual(provider.calls, malformed_provider_calls)
            self.assertEqual(recovery_outcome["status"], "action_required")
            self.assertIsNone(recovery_record.reservation_id)
            self.assertEqual(recovery_record.failed_attempts, 0)
            LocalSessionLedger(ledger_path).replace(
                IDENTITY,
                lambda current: current.evolve(convergence_history=initial_history),
            )
            with patch(
                "review_sensei.cli.default_registry",
                return_value=RecordingRegistry(provider),
            ):
                self.assertEqual(main(second_argv), 0)
            second_rendered = json.loads(output_path.read_text(encoding="utf-8"))
            restarted = LocalSessionLedger(ledger_path).load(IDENTITY).record
            self.assertEqual(provider.calls, 2)
            self.assertEqual(second_rendered["coverage_mode"], "incremental")
            self.assertEqual(restarted.completed_initial_reviews, 1)
            self.assertEqual(restarted.completed_verification_rounds, 1)
            self.assertEqual(
                baseline_from_history_document(
                    restarted.convergence_history["baseline"]
                ).cache_key.head_sha,
                "c" * 40,
            )
            incompatible_argv = list(second_argv)
            incompatible_argv[incompatible_argv.index("--head-sha") + 1] = "d" * 40
            incompatible_argv[incompatible_argv.index("--model") + 1] = "fixture-v2"
            with patch(
                "review_sensei.cli.default_registry",
                return_value=RecordingRegistry(provider),
            ):
                self.assertNotEqual(main(incompatible_argv), 0)
            rejected = LocalSessionLedger(ledger_path).load(IDENTITY).record
            self.assertEqual(provider.calls, 2)
            self.assertEqual(rejected.completed_initial_reviews, 1)
            self.assertEqual(rejected.completed_verification_rounds, 1)
            self.assertIsNone(rejected.reservation_id)

    def test_cli_checkpoint_preserves_named_profile_in_cache_identity(self):
        class RecordingProvider:
            name = "fixture"
            model = "deepseek-v4.1-flash:cloud"

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
            outcome_path = root / "outcome.json"
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
                "ollama",
                "--profile",
                "deep-verification",
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
                "--outcome",
                str(outcome_path),
                "--configuration-context-output",
                str(configuration_path),
                "--no-learning-proposals",
            ]
            observed_profiles: list[str] = []
            observed_incrementals = []
            original_run = ReviewService.run

            def recording_run(service, request, **kwargs):
                observed_profiles.append(kwargs.get("profile", "default"))
                observed_incrementals.append(kwargs.get("incremental"))
                return original_run(service, request, **kwargs)

            with (
                patch.dict("os.environ", {"OLLAMA_API_KEY": "test-key"}),
                patch(
                    "review_sensei.cli.default_registry",
                    return_value=RecordingRegistry(provider),
                ),
                patch("review_sensei.service.ReviewService.run", recording_run),
            ):
                self.assertEqual(main(argv), 0)

            record = LocalSessionLedger(ledger_path).load(IDENTITY).record
            self.assertIsNotNone(record.convergence_history)
            assert record.convergence_history is not None
            self.assertEqual(
                baseline_from_history_document(
                    record.convergence_history["baseline"]
                ).cache_key.profile,
                "deep-verification",
            )
            self.assertEqual(
                baseline_from_history_document(
                    record.convergence_history["baseline"]
                ).cache_key.model,
                provider.model,
            )

            second_argv = list(argv)
            second_argv[second_argv.index("--head-sha") + 1] = "c" * 40
            with (
                patch.dict("os.environ", {"OLLAMA_API_KEY": "test-key"}),
                patch(
                    "review_sensei.cli.default_registry",
                    return_value=RecordingRegistry(provider),
                ),
                patch("review_sensei.service.ReviewService.run", recording_run),
            ):
                self.assertEqual(main(second_argv), 0)

            self.assertEqual(
                observed_profiles,
                ["deep-verification", "deep-verification"],
            )
            self.assertIsNone(observed_incrementals[0])
            self.assertIsNotNone(observed_incrementals[1])
            assert observed_incrementals[1] is not None
            self.assertEqual(
                observed_incrementals[1].reviewed_paths,
                ("src/app.py",),
            )

    def test_cli_checkpoint_completes_without_a_cache_key(self):
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
            with (
                patch(
                    "review_sensei.cli.default_registry",
                    return_value=RecordingRegistry(provider),
                ),
                patch(
                    "review_sensei.cli.build_review_context_cache_key",
                    return_value=None,
                ),
            ):
                self.assertEqual(main(argv), 0)

            rendered = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(provider.calls, 1)
            # The durable checkpoint still runs when no cache key can be
            # derived; the transaction simply advances without a baseline
            # envelope instead of failing on the prepared round.
            self.assertEqual(rendered["transaction"]["phase"], "publication_pending")
            record = LocalSessionLedger(ledger_path).load(IDENTITY).record
            self.assertEqual(record.completed_initial_reviews, 1)
            self.assertIsNone(record.reservation_id)
            self.assertIsNone(record.convergence_history)

    def test_cli_transaction_checkpoint_binds_shas_off_the_live_request(self):
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
            live_requests: list[ReviewRequest] = []
            original_run = ReviewService.run

            def recording_run(self, request, **kwargs):
                live_requests.append(request)
                return original_run(self, request, **kwargs)

            with (
                patch(
                    "review_sensei.cli.default_registry",
                    return_value=RecordingRegistry(provider),
                ),
                patch("review_sensei.service.ReviewService.run", recording_run),
            ):
                self.assertEqual(main(argv), 0)

            # ADR 0053 keeps F2 off the live inference path: the request the
            # service receives carries no trusted SHAs, while the durable
            # transaction still records the identity the checkpoint bound.
            self.assertEqual(len(live_requests), 1)
            self.assertIsNone(live_requests[0].base_sha)
            self.assertIsNone(live_requests[0].head_sha)
            record = LocalSessionLedger(ledger_path).load(IDENTITY).record
            self.assertEqual(record.transaction.base_sha, BASE_SHA)
            self.assertEqual(record.transaction.head_sha, HEAD_SHA)

    def test_checkpoint_cache_request_binds_shas_without_touching_the_request(self):
        request = ReviewRequest(
            diff="diff",
            repository=IDENTITY.repository,
            pull_request_number=IDENTITY.pull_request,
        )
        bound = _checkpoint_cache_request(
            request,
            base_sha=f" {BASE_SHA.upper()}",
            head_sha=HEAD_SHA,
        )
        self.assertEqual(bound.base_sha, BASE_SHA)
        self.assertEqual(bound.head_sha, HEAD_SHA)
        # The live request stays unbound: the checkpoint copy is separate.
        self.assertIsNone(request.base_sha)
        self.assertIsNone(request.head_sha)

    def test_cli_expired_ledger_names_the_recovery_action(self):
        class UnconstructedProvider:
            name = "fixture"
            model = "fixture-v1"

        class RecordingRegistry:
            def __init__(self, provider):
                self.provider = provider

            def create(self, settings):
                raise AssertionError(
                    "an expired session ledger must fail before inference"
                )

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
            LocalSessionLedger(ledger_path).initialize(
                IDENTITY, now=NOW - timedelta(days=31)
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
            stderr = io.StringIO()
            with (
                patch(
                    "review_sensei.cli.default_registry",
                    return_value=RecordingRegistry(UnconstructedProvider()),
                ),
                redirect_stderr(stderr),
            ):
                self.assertEqual(main(argv), 1)

            message = stderr.getvalue()
            self.assertIn("review-sensei:", message)
            self.assertIn("expired", message)
            self.assertIn("recovery is required", message)
            self.assertIn("re-enroll", message)

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

    def test_cli_transaction_opt_in_does_not_require_context_file(self):
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
                "--transaction",
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
            self.assertEqual(rendered["transaction"]["phase"], "publication_pending")
            record = LocalSessionLedger(ledger_path).load(IDENTITY).record
            self.assertEqual(record.transaction.phase, "publication_pending")
            self.assertIsNone(record.reservation_id)

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
    def test_application_suppresses_repeated_admitted_blockers_before_publish(self):
        class Publisher(ReviewPublisher):
            def __init__(self) -> None:
                super().__init__(http=object())
                self.calls = 0

            def publish(self, **kwargs):
                self.calls += 1
                return PublicationResult(status="published", review_id=1)

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
        baseline = ReviewBaseline(
            cache_key=ReviewContextCacheKey(
                repository=IDENTITY.repository,
                pull_request=IDENTITY.pull_request,
                base_sha=BASE_SHA,
                head_sha=HEAD_SHA,
                engine="fixture",
                model="fixture-model",
                profile="default",
                stage_digest="1" * 64,
                context_digest="2" * 64,
                learning_digest="3" * 64,
            ),
            policy_digest=POLICY.digest(),
            complete=True,
            coverage_complete=True,
            generation=prepared.record.generation + 1,
        )
        comment = ReviewComment(
            path="src/app.py",
            line=2,
            body="The changed path needs a bounded input check.",
            blocking=False,
            severity="high",
            defect_kind="authz-failure",
            fix_effort="small",
        )
        result = checkpoint_review_analysis(
            ledger,
            IDENTITY,
            prepared,
            ReviewResult(
                summary="Complete.",
                comments=(comment,),
                provider="fixture",
                model="fixture-model",
                review_status="complete",
            ),
            baseline=baseline,
            now=NOW,
        )
        candidate = derive_blocker_candidate(
            comment,
            on_changed_path=True,
            evidence_locations_validated=True,
            has_failure_condition=True,
            has_actionable_remedy=True,
            has_specific_violation=True,
        )
        digest, count = blocker_set_digest(
            (finding_lifecycle_for_comment(comment).fingerprint,)
        )
        record = ledger.load(IDENTITY, now=NOW).record
        assert record is not None
        history = dict(record.convergence_history)
        history["progress"] = [
            {
                "event": "completed",
                "generation": 1,
                "blocker_set_sha256": digest,
                "blocker_count": count,
            },
            {
                "event": "completed",
                "generation": 2,
                "blocker_set_sha256": digest,
                "blocker_count": count,
            },
            {
                "event": "completed",
                "generation": result.transaction.generation,
            },
        ]
        ledger.replace(
            IDENTITY,
            lambda current: current.evolve(convergence_history=history, now=NOW),
            now=NOW,
        )
        publisher = Publisher()
        application = GitHubApplication(
            broker=_Broker(),
            http=None,
            reviewer=publisher,
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
            diff=(
                "diff --git a/src/app.py b/src/app.py\n"
                "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1,2 @@\n keep\n+change\n"
            ),
            app_slug="review-sensei[bot]",
            convergence_policy=POLICY,
            blocker_candidates=(candidate,),
            baseline=baseline,
            current_key=baseline.cache_key,
            configuration_context=CONFIGURATION,
            evidence_context=EVIDENCE,
        )
        self.assertEqual(outcome.status, "handoff")
        self.assertEqual(outcome.diagnostic, "no_progress")
        self.assertEqual(outcome.transaction_id, result.transaction.transaction_id)
        self.assertEqual(
            outcome.generation, ledger.load(IDENTITY, now=NOW).record.generation
        )
        self.assertEqual(publisher.calls, 0)
        self.assertEqual(
            ledger.load(IDENTITY, now=NOW).record.transaction.phase,
            "publication_suppressed",
        )

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

    def test_application_refuses_a_deleted_hosted_marker_before_recreation(self):
        result = _checkpoint(InMemorySessionLedger())

        class Broker:
            def open_session(self, exchange_input, **kwargs):
                return type(
                    "Session", (), {"token": "session-token", "state": "known"}
                )()

            def exchange(self, exchange_input, *, capability=None):
                return "publish-token"

        class Reviewer:
            def publish(self, **kwargs):
                raise AssertionError(
                    "a deleted session marker must stop before publish"
                )

        http, calls = make_http([json_response([])])
        application = GitHubApplication(
            broker=Broker(),
            http=http,
            reviewer=Reviewer(),
            learner=_Noop(),
            replier=_Noop(),
        )
        with self.assertRaisesRegex(
            GitHubPublicationError,
            "marker is missing; authenticated recovery is required",
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
                result=result,
                diff="diff",
                app_slug="review-sensei[bot]",
                convergence_policy=POLICY,
                configuration_context=CONFIGURATION,
                evidence_context=EVIDENCE,
            )
        # The identity-bound flow must fail closed on the read-only load
        # before any ledger call can re-create the deleted session comment.
        self.assertEqual([method for method, _url, _data in calls], ["GET"])

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

    def test_application_handoffs_later_transaction_without_prior_admission_inputs(
        self,
    ):
        ledger = InMemorySessionLedger()
        result = _checkpoint(ledger)
        ledger.replace(
            IDENTITY,
            lambda current: current.evolve(completed_verification_rounds=1),
            now=NOW,
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

        self.assertEqual(outcome.status, "handoff")
        self.assertEqual(outcome.diagnostic, "durable_baseline_recovery_required")
        self.assertEqual(reviewer.calls, 0)
        self.assertEqual(
            ledger.load(IDENTITY).record.transaction.phase,
            "publication_failed",
        )

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

    def test_application_classifies_malformed_evidence_before_broker(self):
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
        malformed_evidence = {**EVIDENCE, "caller_supplied": "unbounded"}
        with self.assertRaisesRegex(
            GitHubPublicationError, "evidence validation failed"
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
                evidence_context=malformed_evidence,
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
