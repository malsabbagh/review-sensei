from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from review_sensei.baseline import ReviewBaseline
from review_sensei.cli import main
from review_sensei.context import ReviewContextCacheKey
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.diagnostics import run_doctor
from review_sensei.hosting.github import GitHubApplication, GitHubWriteOptions
from review_sensei.hosting.github.publication import PublicationResult
from review_sensei.models import ReviewResult
from review_sensei.outcomes import PUBLIC_DIAGNOSTICS
from review_sensei.session import (
    InMemorySessionLedger,
    LocalSessionLedger,
    SessionIdentity,
    SessionRecord,
    admission_diagnostic,
    complete_session_round,
    prepare_session_round,
    record_session_failed_attempt,
    session_reservation_id,
    should_skip_automation,
)

DIFF = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1,2 @@
 keep
+change
"""

FIXED_NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
IDENTITY = SessionIdentity("owner/repo", 136, repository_id=99)
HEAD = "a" * 40


def _reservation(*, kind: str = "publish") -> str:
    return session_reservation_id(
        repository=IDENTITY.repository,
        pull_request=IDENTITY.pull_request,
        head_sha=HEAD,
        kind=kind,
    )


class RecordingBroker:
    def request_oidc_token(self):
        return "oidc-token"

    def exchange(self, token, *, capability=None):
        return f"capability-{capability}"


class RecordingReviewer:
    def __init__(self):
        self.calls = []

    def publish(self, **kwargs):
        self.calls.append(kwargs)
        return PublicationResult(status="published", review_id=1)


class AutomationAdmissionTests(unittest.TestCase):
    def test_exhausted_round_is_not_reserved_and_skips_publication(self):
        ledger = InMemorySessionLedger()
        record = SessionRecord.create(
            IDENTITY,
            now=FIXED_NOW,
            completed_initial_reviews=1,
            completed_verification_rounds=5,
        )
        ledger._records[(IDENTITY.repository, IDENTITY.pull_request)] = record
        policy = ReviewConvergencePolicy(mode="merge-focused")
        prepared = prepare_session_round(
            ledger,
            IDENTITY,
            policy,
            reservation_id=_reservation(),
            now=FIXED_NOW,
            latest_head_reviewed=True,
            coverage_complete=True,
        )
        self.assertFalse(prepared.decision.admit)
        self.assertIsNone(prepared.reservation_id)
        self.assertTrue(should_skip_automation(prepared.decision, inference=True))
        self.assertTrue(should_skip_automation(prepared.decision, inference=False))
        loaded = ledger.load(IDENTITY, now=FIXED_NOW)
        self.assertIsNone(loaded.record.reservation_id)
        self.assertEqual(loaded.record.completed_verification_rounds, 5)
        self.assertEqual(loaded.record.failed_attempts, 0)
        self.assertEqual(loaded.record.generation, record.generation)
        self.assertEqual(
            admission_diagnostic(prepared.decision), "round-budget-exhausted"
        )

    def test_in_flight_reservation_pauses_other_jobs(self):
        ledger = InMemorySessionLedger()
        policy = ReviewConvergencePolicy(mode="strict")
        first = prepare_session_round(
            ledger, IDENTITY, policy, reservation_id=_reservation(), now=FIXED_NOW
        )
        self.assertTrue(first.decision.admit)
        second = prepare_session_round(
            ledger,
            IDENTITY,
            policy,
            reservation_id="bbbbbbbb",
            now=FIXED_NOW,
        )
        self.assertFalse(second.decision.admit)
        self.assertEqual(second.decision.handoff_reason, "paused")
        self.assertIsNone(second.reservation_id)

    def test_exhausted_unreviewed_head_reports_unreviewed_diagnostic(self):
        ledger = InMemorySessionLedger()
        record = SessionRecord.create(
            IDENTITY,
            now=FIXED_NOW,
            completed_initial_reviews=1,
            completed_verification_rounds=5,
        )
        ledger._records[(IDENTITY.repository, IDENTITY.pull_request)] = record
        prepared = prepare_session_round(
            ledger,
            IDENTITY,
            ReviewConvergencePolicy(mode="merge-focused"),
            reservation_id=_reservation(),
            now=FIXED_NOW,
            coverage_complete=True,
        )
        self.assertFalse(prepared.decision.admit)
        self.assertEqual(admission_diagnostic(prepared.decision), "unreviewed-head")

    def test_same_reservation_after_commit_is_duplicate_not_a_new_round(self):
        ledger = InMemorySessionLedger()
        policy = ReviewConvergencePolicy(mode="merge-focused")
        reservation = _reservation()
        prepared = prepare_session_round(
            ledger, IDENTITY, policy, reservation_id=reservation, now=FIXED_NOW
        )
        complete_session_round(
            ledger, IDENTITY, prepared, published=True, now=FIXED_NOW
        )
        replay = prepare_session_round(
            ledger, IDENTITY, policy, reservation_id=reservation, now=FIXED_NOW
        )
        self.assertFalse(replay.decision.admit)
        self.assertFalse(replay.decision.handoff)
        self.assertFalse(should_skip_automation(replay.decision, inference=False))
        self.assertTrue(should_skip_automation(replay.decision, inference=True))
        loaded = ledger.load(IDENTITY, now=FIXED_NOW)
        self.assertEqual(loaded.record.completed_initial_reviews, 1)
        self.assertEqual(admission_diagnostic(replay.decision), "already_published")

    def test_same_in_flight_reservation_pauses_retry(self):
        ledger = InMemorySessionLedger()
        policy = ReviewConvergencePolicy(mode="merge-focused")
        reservation = _reservation()
        first = prepare_session_round(
            ledger, IDENTITY, policy, reservation_id=reservation, now=FIXED_NOW
        )
        self.assertTrue(first.decision.admit)
        retry = prepare_session_round(
            ledger, IDENTITY, policy, reservation_id=reservation, now=FIXED_NOW
        )
        self.assertFalse(retry.decision.admit)
        self.assertEqual(retry.decision.handoff_reason, "paused")
        self.assertIsNone(retry.reservation_id)

    def test_continuation_reserves_one_extra_verification_round(self):
        ledger = InMemorySessionLedger()
        record = SessionRecord.create(
            IDENTITY,
            now=FIXED_NOW,
            completed_initial_reviews=1,
            completed_verification_rounds=5,
        )
        ledger._records[(IDENTITY.repository, IDENTITY.pull_request)] = record
        policy = ReviewConvergencePolicy(mode="merge-focused")
        prepared = prepare_session_round(
            ledger,
            IDENTITY,
            policy,
            reservation_id=_reservation(),
            now=FIXED_NOW,
            continuation_rounds=1,
            coverage_complete=True,
            latest_head_reviewed=True,
        )
        self.assertTrue(prepared.decision.admit)
        self.assertEqual(prepared.decision.round_kind, "verification")
        self.assertIsNotNone(prepared.reservation_id)

    def test_failed_attempt_charges_separately(self):
        ledger = InMemorySessionLedger()
        policy = ReviewConvergencePolicy(mode="strict")
        reservation = _reservation()
        prepare_session_round(
            ledger, IDENTITY, policy, reservation_id=reservation, now=FIXED_NOW
        )
        record_session_failed_attempt(
            ledger, IDENTITY, reservation_id=reservation, now=FIXED_NOW
        )
        loaded = ledger.load(IDENTITY, now=FIXED_NOW)
        self.assertEqual(loaded.record.failed_attempts, 1)
        self.assertEqual(loaded.record.completed_initial_reviews, 0)
        self.assertIsNone(loaded.record.reservation_id)


class GitHubHandoffPublicationTests(unittest.TestCase):
    def test_exhausted_session_does_not_publish(self):
        ledger = InMemorySessionLedger()
        record = SessionRecord.create(
            IDENTITY,
            now=FIXED_NOW,
            completed_initial_reviews=1,
            completed_verification_rounds=5,
        )
        ledger._records[(IDENTITY.repository, IDENTITY.pull_request)] = record
        reviewer = RecordingReviewer()
        application = GitHubApplication(
            broker=RecordingBroker(),
            http=None,
            reviewer=reviewer,
            learner=object(),
            replier=object(),
            session_ledger=ledger,
        )
        result = application.publish_review(
            options=GitHubWriteOptions(
                auto_review=True, github_writes=True, github_session_ledger=True
            ),
            oidc_token="oidc",
            repository=IDENTITY.repository,
            repository_id=99,
            pull_request=IDENTITY.pull_request,
            head_sha=HEAD,
            base_branch="main",
            base_sha="b" * 40,
            result=ReviewResult(summary="ok", comments=(), provider="fixture"),
            diff="diff",
            app_slug="reviewsensei[bot]",
            convergence_policy=ReviewConvergencePolicy(mode="merge-focused"),
        )
        self.assertEqual(result.status, "handoff")
        self.assertEqual(result.diagnostic, "round-budget-exhausted")
        self.assertEqual(reviewer.calls, [])

    def test_prior_round_without_durable_baseline_requires_recovery(self):
        ledger = InMemorySessionLedger()
        record = SessionRecord.create(
            IDENTITY,
            now=FIXED_NOW,
            completed_initial_reviews=1,
            completed_verification_rounds=5,
        )
        ledger._records[(IDENTITY.repository, IDENTITY.pull_request)] = record
        reviewer = RecordingReviewer()
        application = GitHubApplication(
            broker=RecordingBroker(),
            http=None,
            reviewer=reviewer,
            learner=object(),
            replier=object(),
            session_ledger=ledger,
        )
        result = application.publish_review(
            options=GitHubWriteOptions(
                auto_review=True, github_writes=True, github_session_ledger=True
            ),
            oidc_token="oidc",
            repository=IDENTITY.repository,
            repository_id=99,
            pull_request=IDENTITY.pull_request,
            head_sha=HEAD,
            base_branch="main",
            base_sha="b" * 40,
            result=ReviewResult(
                summary="ok",
                comments=(),
                provider="fixture",
                review_status="complete",
            ),
            diff="diff",
            app_slug="reviewsensei[bot]",
            convergence_policy=ReviewConvergencePolicy(mode="merge-focused"),
        )
        self.assertEqual(result.status, "handoff")
        self.assertEqual(result.diagnostic, "durable_baseline_recovery_required")
        self.assertEqual(reviewer.calls, [])

    def test_provider_failure_records_failed_attempt(self):
        class FailingReviewer:
            def publish(self, **kwargs):
                raise RuntimeError("github down")

        ledger = InMemorySessionLedger()
        application = GitHubApplication(
            broker=RecordingBroker(),
            http=None,
            reviewer=FailingReviewer(),
            learner=object(),
            replier=object(),
            session_ledger=ledger,
        )
        policy = ReviewConvergencePolicy(mode="merge-focused")
        baseline = ReviewBaseline(
            cache_key=ReviewContextCacheKey(
                repository=IDENTITY.repository,
                pull_request=IDENTITY.pull_request,
                base_sha="b" * 40,
                head_sha=HEAD,
                engine="fixture",
                model="fixture-model",
                profile="default",
                stage_digest="1" * 64,
                context_digest="2" * 64,
                learning_digest="3" * 64,
            ),
            policy_digest=policy.digest(),
            complete=True,
            coverage_complete=True,
            generation=1,
        )
        with self.assertRaises(RuntimeError):
            application.publish_review(
                options=GitHubWriteOptions(auto_review=True, github_writes=True),
                oidc_token="oidc",
                repository=IDENTITY.repository,
                repository_id=99,
                pull_request=IDENTITY.pull_request,
                head_sha=HEAD,
                base_branch="main",
                base_sha="b" * 40,
                result=ReviewResult(summary="ok", comments=(), provider="fixture"),
                diff=DIFF,
                app_slug="reviewsensei[bot]",
                convergence_policy=policy,
                baseline=baseline,
                current_key=baseline.cache_key,
            )
        loaded = ledger.load(IDENTITY)
        self.assertEqual(loaded.record.failed_attempts, 1)
        self.assertEqual(loaded.record.completed_initial_reviews, 0)


class DiagnosticAutomationTests(unittest.TestCase):
    def test_doctor_requires_ledger_in_operator_mode(self):
        doctor = run_doctor(review_mode="merge-focused")
        check = next(
            item for item in doctor["checks"] if item["name"] == "automation-admission"
        )
        self.assertEqual(check["status"], "action")
        self.assertIn("session ledger", check["detail"])

    def test_doctor_reports_exhausted_admission(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            ledger = LocalSessionLedger(root)
            record = SessionRecord.create(
                IDENTITY,
                now=FIXED_NOW,
                completed_initial_reviews=1,
                completed_verification_rounds=5,
            )
            ledger._write(IDENTITY, record)
            doctor = run_doctor(
                session_ledger=root,
                repository="owner/repo",
                pull_request=136,
                review_mode="merge-focused",
            )
            check = next(
                item
                for item in doctor["checks"]
                if item["name"] == "automation-admission"
            )
            self.assertEqual(check["status"], "action")
            self.assertIn("admit=False", check["detail"])
            self.assertIn("diagnostic=unreviewed-head", check["detail"])


class CliInferenceSkipTests(unittest.TestCase):
    def test_exhausted_operator_mode_makes_zero_provider_calls(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            ledger = LocalSessionLedger(root)
            record = SessionRecord.create(
                IDENTITY,
                now=FIXED_NOW,
                completed_initial_reviews=1,
                completed_verification_rounds=5,
            )
            ledger._write(IDENTITY, record)
            diff_path = root / "review.patch"
            diff_path.write_text(DIFF, encoding="utf-8")
            response_path = root / "response.json"
            response_path.write_text('{"summary":"ok","comments":[]}', encoding="utf-8")
            outcome_path = root / "outcome.json"

            class Registry:
                def __init__(self):
                    self.created = []

                def create(self, settings):
                    self.created.append(settings)
                    raise AssertionError("provider must not be constructed")

            from review_sensei.session import prepare_session_round as real_prepare

            def prepare_reviewed_head(ledger, identity, policy, **kwargs):
                kwargs["latest_head_reviewed"] = True
                kwargs["coverage_complete"] = True
                return real_prepare(ledger, identity, policy, **kwargs)

            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch(
                    "review_sensei.session.prepare_session_round",
                    side_effect=prepare_reviewed_head,
                ),
                patch("review_sensei.cli.default_registry", return_value=Registry()),
            ):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    status = main(
                        [
                            "--diff",
                            str(diff_path),
                            "--provider",
                            "fixture",
                            "--fixture-response",
                            str(response_path),
                            "--review-mode",
                            "merge-focused",
                            "--session-ledger",
                            str(root),
                            "--repository",
                            "owner/repo",
                            "--pull-request",
                            "136",
                            "--head-sha",
                            HEAD,
                            "--outcome",
                            str(outcome_path),
                        ]
                    )
            self.assertEqual(status, 1)
            self.assertIn("action_required", stdout.getvalue())
            payload = json.loads(outcome_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["diagnostic"], "round-budget-exhausted")
            self.assertIn(payload["diagnostic"], PUBLIC_DIAGNOSTICS)
            self.assertEqual(payload["provider_calls"], 0)


if __name__ == "__main__":
    unittest.main()
