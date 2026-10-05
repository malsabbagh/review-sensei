"""Validated partial findings retain durable identity without completing a pass."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from review_sensei.cli import main
from review_sensei.coverage import CoverageManifest, FileCoverage
from review_sensei.errors import ReviewInputError
from review_sensei.hosting.github import (
    GitHubApplication,
    GitHubWriteOptions,
    ReviewPublisher,
)
from review_sensei.hosting.github.publication import outcome_from_publication
from review_sensei.models import ReviewComment, ReviewResult
from review_sensei.outcomes import RecoveryArtifact, RunOutcome
from review_sensei.service import ReviewRun
from review_sensei.session import (
    InMemorySessionLedger,
    LocalSessionLedger,
    checkpoint_review_analysis,
    complete_review_publication,
    load_review_transaction_for_publication,
    prepare_review_transaction,
    session_reservation_id,
)

try:
    import test_gate_and_default_approval as gates
    import test_review_transaction as transactions
except ModuleNotFoundError:
    from tests import test_gate_and_default_approval as gates
    from tests import test_review_transaction as transactions


def partial_result(outcome="excluded-by-policy", reason="generated"):
    return ReviewResult(
        summary="Validated source findings; other work remains uncovered.",
        comments=(
            ReviewComment(
                path="src/app.py", line=2, body="Retained validated finding."
            ),
        ),
        provider="fixture",
        model="fixture-model",
        review_status="partial",
        coverage=CoverageManifest(
            files=(
                FileCoverage(path="src/app.py", outcome="reviewed"),
                FileCoverage(path="other.bin", outcome=outcome, reason=reason),
            ),
            enumerated_paths=("src/app.py", "other.bin"),
        ),
    )


def prepare(ledger, *, head=transactions.HEAD_SHA):
    return prepare_review_transaction(
        ledger,
        transactions.IDENTITY,
        transactions.POLICY,
        reservation_id=session_reservation_id(
            repository=transactions.IDENTITY.repository,
            pull_request=transactions.IDENTITY.pull_request,
            head_sha=head,
            kind="publish",
        ),
        base_sha=transactions.BASE_SHA,
        head_sha=head,
        configuration_digest=transactions.CONFIGURATION_DIGEST,
        evidence_digest=transactions.EVIDENCE_DIGEST,
    )


def load(ledger, result, *, head=transactions.HEAD_SHA):
    return load_review_transaction_for_publication(
        ledger,
        transactions.IDENTITY,
        result,
        base_sha=transactions.BASE_SHA,
        head_sha=head,
        policy_digest=transactions.POLICY.digest(),
        configuration_digest=transactions.CONFIGURATION_DIGEST,
        evidence_digest=transactions.EVIDENCE_DIGEST,
    )


class PartialCheckpointTests(unittest.TestCase):
    def test_covered_partial_variants_checkpoint_and_retry_once(self):
        for outcome, reason in (
            ("excluded-by-policy", "generated"),
            ("unsupported", "binary"),
            ("budget-exhausted", "provider-call-budget"),
            ("partially-reviewed", "chunk-failed"),
        ):
            with self.subTest(outcome=outcome):
                ledger = InMemorySessionLedger()
                prepared = prepare(ledger)
                result = partial_result(outcome, reason)
                bound = checkpoint_review_analysis(
                    ledger, transactions.IDENTITY, prepared, result
                )
                self.assertEqual(bound.coverage, result.coverage)
                self.assertEqual(bound.comments, result.comments)
                record = load(ledger, bound)
                self.assertEqual(record.transaction.phase, "publication_pending")
                self.assertEqual(record.completed_initial_reviews, 0)
                self.assertEqual(record.completed_verification_rounds, 0)
                self.assertEqual(record.failed_attempts, 1)
                self.assertIsNone(record.convergence_history)
                complete_review_publication(
                    ledger, transactions.IDENTITY, record.transaction, published=False
                )
                recovered = load(ledger, bound)
                self.assertEqual(recovered.transaction.phase, "publication_failed")
                complete_review_publication(
                    ledger, transactions.IDENTITY, recovered.transaction, published=True
                )
                replay = load(ledger, bound)
                self.assertEqual(replay.transaction.phase, "publication_succeeded")
                self.assertEqual(replay.failed_attempts, 1)
                self.assertEqual(replay.completed_initial_reviews, 0)

    def test_unknown_or_total_failed_coverage_cannot_checkpoint_or_recover(self):
        total_failure = CoverageManifest(
            files=(
                FileCoverage(
                    path="src/app.py",
                    outcome="partially-reviewed",
                    reason="chunk-failed",
                ),
            ),
            enumerated_paths=("src/app.py",),
        )
        for result in (
            replace(partial_result(), coverage=None),
            replace(
                partial_result(),
                coverage=replace(partial_result().coverage, enumeration_complete=False),
            ),
            replace(partial_result(), coverage=total_failure),
            replace(partial_result(), review_status="incomplete"),
            replace(partial_result(), review_status="summary-only"),
            replace(partial_result(), review_status="complete"),
        ):
            with self.subTest(result=result.review_status, coverage=result.coverage):
                ledger = InMemorySessionLedger()
                prepared = prepare(ledger)
                with self.assertRaisesRegex(ReviewInputError, "checkpointable"):
                    checkpoint_review_analysis(
                        ledger, transactions.IDENTITY, prepared, result
                    )
                with self.assertRaisesRegex(ReviewInputError, "checkpointable"):
                    load(ledger, replace(result, transaction=prepared.transaction))
                self.assertEqual(
                    ledger.load(transactions.IDENTITY).record.failed_attempts, 0
                )

    def test_validated_cross_file_partial_work_can_checkpoint_without_reviewed_file(
        self,
    ):
        result = replace(
            partial_result(),
            comments=(),
            coverage=CoverageManifest(
                files=(
                    FileCoverage(
                        path="src/app.py",
                        outcome="partially-reviewed",
                        reason="cross-file-relationship",
                    ),
                ),
                enumerated_paths=("src/app.py",),
            ),
        )
        ledger = InMemorySessionLedger()
        bound = checkpoint_review_analysis(
            ledger, transactions.IDENTITY, prepare(ledger), result
        )
        self.assertEqual(load(ledger, bound).completed_initial_reviews, 0)

    def test_partial_crash_recovery_keeps_identity_and_requires_same_result(self):
        ledger = InMemorySessionLedger()
        prepared = prepare(ledger)
        result = replace(partial_result(), transaction=prepared.transaction)
        record = load(ledger, result)
        self.assertEqual(record.failed_attempts, 1)
        bound = replace(result, transaction=record.transaction)
        with self.assertRaisesRegex(ReviewInputError, "identity"):
            load(ledger, bound, head="c" * 40)
        with self.assertRaisesRegex(ReviewInputError, "digest"):
            load(ledger, replace(bound, summary="Altered result."))
        self.assertEqual(load(ledger, bound).failed_attempts, 1)

    def test_partial_keeps_last_complete_baseline_and_next_head_admission(self):
        ledger = InMemorySessionLedger()
        complete = transactions._checkpoint_with_baseline(ledger)
        complete_review_publication(
            ledger, transactions.IDENTITY, complete.transaction, published=True
        )
        before = ledger.load(transactions.IDENTITY).record
        new_head = "c" * 40
        bound = checkpoint_review_analysis(
            ledger,
            transactions.IDENTITY,
            prepare(ledger, head=new_head),
            partial_result(),
        )
        after = load(ledger, bound, head=new_head)
        self.assertEqual(after.convergence_history, before.convergence_history)
        self.assertEqual(after.completed_verification_rounds, 0)
        next_round = prepare(ledger, head="d" * 40)
        self.assertTrue(next_round.decision.admit)
        self.assertEqual(next_round.record.failed_attempts, 0)
        with self.assertRaisesRegex(ReviewInputError, "cannot checkpoint a baseline"):
            checkpoint_review_analysis(
                ledger,
                transactions.IDENTITY,
                next_round,
                partial_result(),
                baseline=transactions._baseline(next_round.record.generation + 1),
            )


class PartialPublicationTests(unittest.TestCase):
    def test_application_recovery_publishes_partial_without_complete_progress(self):
        ledger = InMemorySessionLedger()
        result = checkpoint_review_analysis(
            ledger, transactions.IDENTITY, prepare(ledger), partial_result()
        )
        reviewer = transactions._Reviewer()
        application = GitHubApplication(
            broker=transactions._Broker(),
            http=None,
            reviewer=reviewer,
            learner=transactions._Noop(),
            replier=transactions._Noop(),
            session_ledger=ledger,
        )
        arguments = dict(
            options=GitHubWriteOptions(auto_review=True, github_writes=True),
            oidc_token="oidc",
            repository=transactions.IDENTITY.repository,
            repository_id=transactions.IDENTITY.repository_id,
            pull_request=transactions.IDENTITY.pull_request,
            head_sha=transactions.HEAD_SHA,
            base_branch="main",
            base_sha=transactions.BASE_SHA,
            result=result,
            diff=gates.DIFF,
            app_slug=gates.APP,
            convergence_policy=transactions.POLICY,
            configuration_context=transactions.CONFIGURATION,
            evidence_context=transactions.EVIDENCE,
        )
        publication = application.publish_review(**arguments)
        replay = application.publish_review(**arguments)
        self.assertEqual(publication.status, "published")
        self.assertEqual(replay.status, "already_published")
        self.assertEqual(reviewer.calls, 1)
        self.assertIsNone(ledger.load(transactions.IDENTITY).record.convergence_history)
        for outcome in (publication, replay):
            self.assertEqual(
                outcome_from_publication(outcome, review_result=result).status,
                "partial",
            )

    def test_identity_bound_partial_reaches_the_real_adapter(self):
        ledger = InMemorySessionLedger()
        result = checkpoint_review_analysis(
            ledger,
            transactions.IDENTITY,
            prepare(ledger),
            replace(
                partial_result(),
                comments=(
                    replace(partial_result().comments[0], line=None, side="FILE"),
                ),
            ),
        )
        checks = gates.FakeCheckRuns()
        payload = gates.pr_payload(head_sha=transactions.HEAD_SHA)
        payload["base"]["sha"] = transactions.BASE_SHA
        payload["base"]["repo"]["id"] = transactions.IDENTITY.repository_id
        http, calls = gates.make_http(
            [
                gates.json_response(payload),
                gates.json_response([]),
                gates.json_response(payload),
                gates.json_response({"id": 5}),
            ],
            routes=((checks.matches, checks.respond),),
        )
        application = GitHubApplication(
            broker=transactions._Broker(),
            http=http,
            reviewer=ReviewPublisher(http=http),
            learner=transactions._Noop(),
            replier=transactions._Noop(),
            session_ledger=ledger,
        )
        publication = application.publish_review(
            options=GitHubWriteOptions(auto_review=True, github_writes=True),
            oidc_token="oidc",
            repository=transactions.IDENTITY.repository,
            repository_id=transactions.IDENTITY.repository_id,
            pull_request=transactions.IDENTITY.pull_request,
            head_sha=transactions.HEAD_SHA,
            base_branch="main",
            base_sha=transactions.BASE_SHA,
            result=result,
            diff=gates.DIFF,
            app_slug=gates.APP,
            convergence_policy=transactions.POLICY,
            configuration_context=transactions.CONFIGURATION,
            evidence_context=transactions.EVIDENCE,
        )
        self.assertEqual(publication.status, "published")
        published_reviews = [
            json.loads(body)
            for method, url, body in calls
            if method == "POST" and url.endswith("/reviews") and body
        ]
        self.assertEqual([body["event"] for body in published_reviews], ["COMMENT"])
        self.assertEqual(checks.conclusions, [None, "action_required"])
        self.assertIn("Retained validated finding.", published_reviews[0]["body"])
        record = load(ledger, result)
        self.assertEqual(record.transaction.phase, "publication_succeeded")
        self.assertEqual(record.completed_initial_reviews, 0)
        self.assertIsNone(record.convergence_history)

    def test_later_partial_retry_preserves_required_admission_context(self):
        ledger = InMemorySessionLedger()
        initial = transactions._checkpoint_with_baseline(ledger)
        complete_review_publication(
            ledger, transactions.IDENTITY, initial.transaction, published=True
        )
        verification_head = "c" * 40
        verification = prepare(ledger, head=verification_head)
        prior_baseline = replace(
            transactions._baseline(verification.record.generation + 1),
            cache_key=replace(
                transactions._baseline(verification.record.generation + 1).cache_key,
                head_sha=verification_head,
            ),
        )
        complete = checkpoint_review_analysis(
            ledger,
            transactions.IDENTITY,
            verification,
            transactions._result(),
            baseline=prior_baseline,
        )
        complete_review_publication(
            ledger, transactions.IDENTITY, complete.transaction, published=True
        )
        prior_history = ledger.load(transactions.IDENTITY).record.convergence_history
        partial_head = "d" * 40
        result = checkpoint_review_analysis(
            ledger,
            transactions.IDENTITY,
            prepare(ledger, head=partial_head),
            partial_result(),
        )
        reviewer = transactions._Reviewer()
        application = GitHubApplication(
            broker=transactions._Broker(),
            http=None,
            reviewer=reviewer,
            learner=transactions._Noop(),
            replier=transactions._Noop(),
            session_ledger=ledger,
        )
        arguments = dict(
            options=GitHubWriteOptions(auto_review=True, github_writes=True),
            oidc_token="oidc",
            repository=transactions.IDENTITY.repository,
            repository_id=transactions.IDENTITY.repository_id,
            pull_request=transactions.IDENTITY.pull_request,
            head_sha=partial_head,
            base_branch="main",
            base_sha=transactions.BASE_SHA,
            result=result,
            diff=gates.DIFF,
            app_slug=gates.APP,
            convergence_policy=transactions.POLICY,
            configuration_context=transactions.CONFIGURATION,
            evidence_context=transactions.EVIDENCE,
        )
        missing = application.publish_review(**arguments)
        self.assertEqual(missing.status, "handoff")
        self.assertEqual(missing.diagnostic, "durable_baseline_recovery_required")
        self.assertEqual(reviewer.calls, 0)
        publication = application.publish_review(
            **arguments,
            baseline=prior_baseline,
            current_key=replace(prior_baseline.cache_key, head_sha=partial_head),
        )
        self.assertEqual(publication.status, "published")
        record = load(ledger, result, head=partial_head)
        self.assertEqual(record.convergence_history, prior_history)
        self.assertEqual(record.completed_initial_reviews, 1)
        self.assertEqual(record.completed_verification_rounds, 1)
        self.assertEqual(record.failed_attempts, 1)

    def test_recovery_artifact_partial_requires_known_validated_coverage(self):
        for result in (partial_result(), replace(partial_result(), coverage=None)):
            with self.subTest(coverage=result.coverage):
                reviewer = transactions._Reviewer()
                application = GitHubApplication(
                    broker=transactions._Broker(),
                    http=None,
                    reviewer=reviewer,
                    learner=transactions._Noop(),
                    replier=transactions._Noop(),
                )
                artifact = RecoveryArtifact.create(
                    repository=transactions.IDENTITY.repository,
                    pull_request_number=transactions.IDENTITY.pull_request,
                    base_sha=transactions.BASE_SHA,
                    head_sha=transactions.HEAD_SHA,
                    result=result.to_dict(),
                    expires_at=(
                        datetime.now(timezone.utc) + timedelta(hours=1)
                    ).isoformat(),
                )
                arguments = dict(
                    options=GitHubWriteOptions(auto_review=True, github_writes=True),
                    oidc_token="oidc",
                    repository=transactions.IDENTITY.repository,
                    repository_id=transactions.IDENTITY.repository_id,
                    pull_request=transactions.IDENTITY.pull_request,
                    head_sha=transactions.HEAD_SHA,
                    base_branch="main",
                    base_sha=transactions.BASE_SHA,
                    artifact=artifact,
                    diff=gates.DIFF,
                    app_slug=gates.APP,
                )
                if result.coverage is None:
                    with self.assertRaisesRegex(ReviewInputError, "incomplete"):
                        application.recover_review(**arguments)
                    self.assertEqual(reviewer.calls, 0)
                else:
                    publication = application.recover_review(**arguments)
                    self.assertEqual(publication.status, "published")
                    self.assertEqual(reviewer.calls, 1)
                    self.assertEqual(
                        outcome_from_publication(
                            publication, review_result=result
                        ).status,
                        "partial",
                    )

    def test_partial_adapter_posts_findings_with_action_required_and_no_approval(self):
        checks = gates.FakeCheckRuns()
        case = gates.GateAcceptanceCase()
        result = replace(
            partial_result(),
            comments=(replace(partial_result().comments[0], line=None, side="FILE"),),
        )
        outcome, calls = case.publish(
            [
                gates.json_response(gates.pr_payload(head_sha=gates.HEAD)),
                gates.json_response([]),
                gates.json_response(gates.pr_payload(head_sha=gates.HEAD)),
                gates.json_response({"id": 5}),
            ],
            checks=checks,
            result=result,
        )
        self.assertEqual(gates.posted_events(calls), ["COMMENT"])
        self.assertEqual(checks.conclusions, [None, "action_required"])
        body = gates.review_payloads(calls)[0]["body"]
        self.assertIn("Review incomplete", body)
        self.assertIn("Retained validated finding.", body)
        self.assertIn("excluded-by-policy", body)
        projected = outcome_from_publication(outcome, review_result=result)
        self.assertEqual(projected.status, "partial")
        self.assertEqual(projected.diagnostic, "review_incomplete")

    def test_cli_partial_artifacts_preserve_coverage_and_exit_semantics(self):
        result = partial_result()
        run = ReviewRun(
            RunOutcome("partial", diagnostic="partial_coverage", provider_calls=1),
            result=result,
        )
        for semantics, expected in (("operational", 0), ("review", 2)):
            with (
                self.subTest(semantics=semantics),
                tempfile.TemporaryDirectory() as tmp,
            ):
                root = Path(tmp)
                diff_path = root / "review.patch"
                response_path = root / "response.json"
                diff_path.write_text(gates.DIFF, encoding="utf-8")
                response_path.write_text(
                    '{"summary":"ok","comments":[]}', encoding="utf-8"
                )
                argv = [
                    "--diff",
                    str(diff_path),
                    "--provider",
                    "fixture",
                    "--fixture-response",
                    str(response_path),
                    "--model",
                    "fixture-model",
                    "--repository",
                    transactions.IDENTITY.repository,
                    "--pull-request",
                    str(transactions.IDENTITY.pull_request),
                    "--base-sha",
                    transactions.BASE_SHA,
                    "--head-sha",
                    transactions.HEAD_SHA,
                    "--session-ledger",
                    str(root / "ledger"),
                    "--configuration-context-output",
                    str(root / "configuration.json"),
                    "--admission-context-output",
                    str(root / "admission.json"),
                    "--format",
                    "json",
                    "--output",
                    str(root / "review.json"),
                    "--outcome",
                    str(root / "outcome.json"),
                    "--exit-semantics",
                    semantics,
                    "--no-learning-proposals",
                ]
                with (
                    patch("review_sensei.cli.ReviewService.run", return_value=run),
                    redirect_stderr(io.StringIO()),
                ):
                    self.assertEqual(main(argv), expected)
                rendered = ReviewResult.from_dict(
                    json.loads((root / "review.json").read_text())
                )
                self.assertEqual(rendered.review_status, "partial")
                self.assertEqual(rendered.coverage, result.coverage)
                self.assertIsNotNone(rendered.transaction)
                self.assertTrue((root / "configuration.json").exists())
                self.assertTrue((root / "admission.json").exists())
                self.assertEqual(
                    json.loads((root / "outcome.json").read_text())["status"], "partial"
                )
                record = (
                    LocalSessionLedger(root / "ledger")
                    .load(transactions.IDENTITY)
                    .record
                )
                self.assertEqual(record.completed_initial_reviews, 0)
                self.assertEqual(record.failed_attempts, 1)
                self.assertIsNone(record.convergence_history)
