"""Reduced durable baselines cannot restore active findings or native holds."""

from __future__ import annotations

import tempfile
import unittest
from copy import copy
from dataclasses import replace

from review_sensei_github import ReviewPublisher
from review_sensei_github.approval import approval_eligibility_from_result

from review_sensei.active_finding_window import baseline_from_active_window
from review_sensei.baseline import (
    ReviewBaseline,
    baseline_from_history_document,
    baseline_history_document,
)
from review_sensei.context import ReviewContextCacheKey
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.disposition import FindingDisposition
from review_sensei.models import FindingLifecycleRecord
from review_sensei.outcomes import PUBLIC_DIAGNOSTICS, RunOutcome
from review_sensei.schemas import validate_public_document
from tests import test_active_window_lifecycle_g10 as fixtures
from tests.test_active_window_lifecycle_g10 import (
    BASE_SHA,
    HEAD_1,
    HEAD_2,
    ORDINARY_DIFF,
    _comment,
    _coverage,
    _result,
    _sha,
    _WindowGitHubHost,
)

DIAGNOSTIC = "durable_finding_inventory_recovery_required"


class FindingInventoryReconstructionGuardTests(unittest.TestCase):
    def _baseline(self) -> ReviewBaseline:
        key = ReviewContextCacheKey(
            repository="owner/repo",
            pull_request=1,
            base_sha=BASE_SHA,
            head_sha=HEAD_1,
            engine="fixture",
            model="review-v1",
            profile="default",
            stage_digest=_sha("stage"),
            context_digest=_sha("context"),
            learning_digest=_sha("learning"),
        )
        return ReviewBaseline(
            cache_key=key,
            policy_digest=ReviewConvergencePolicy(mode="strict").digest(),
            complete=True,
            reviewed_paths=("src/app.py", "src/db.py"),
        )

    def _publish(self, publisher, **overrides):
        arguments = dict(
            token="fixture-token",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha=HEAD_2,
            base_branch="main",
            base_sha=BASE_SHA,
            result=_result(coverage=_coverage("src/app.py", "src/db.py")),
            diff=ORDINARY_DIFF,
            app_slug="reviewsensei[bot]",
            convergence_policy=ReviewConvergencePolicy(mode="strict"),
        )
        arguments.update(overrides)
        return publisher.publish(**arguments)

    def test_cold_publisher_refuses_reduced_baseline_before_any_native_effect(self):
        for route in ({"max_active_findings": 1}, {"enable_drift_tracking": True}):
            with self.subTest(route=route):
                host = _WindowGitHubHost(head_sha=HEAD_2)
                publisher = ReviewPublisher(http=host.http)
                baseline = self._baseline()
                before = baseline_history_document(baseline)
                restored = baseline_from_history_document(before)
                outcome = self._publish(publisher, baseline=restored, **route)
                self.assertEqual(outcome.status, "handoff")
                self.assertEqual(outcome.diagnostic, DIAGNOSTIC)
                self.assertEqual(host.calls, [])
                self.assertEqual(baseline_history_document(restored), before)
                self.assertIsNone(
                    publisher.active_window_state(repository_id=1, pull_request=1)
                )
                self.assertIsNone(
                    publisher.drift_state(repository_id=1, pull_request=1)
                )

    def test_cold_incremental_and_fallback_cannot_omit_baseline_to_restart(self):
        for mode in ("incremental", "fallback-full"):
            for route in ({"max_active_findings": 1}, {"enable_drift_tracking": True}):
                with self.subTest(mode=mode, route=route):
                    host = _WindowGitHubHost(head_sha=HEAD_2)
                    outcome = self._publish(
                        ReviewPublisher(http=host.http),
                        result=replace(
                            _result(coverage=_coverage("src/app.py")),
                            coverage_mode=mode,
                        ),
                        **route,
                    )
                    self.assertEqual(
                        (outcome.status, outcome.diagnostic), ("handoff", DIAGNOSTIC)
                    )
                    self.assertEqual(host.calls, [])

    def test_deleted_native_projections_do_not_erase_retained_obligations(self):
        helper = fixtures.OrdinaryPublisherActiveWindowTests()
        with tempfile.TemporaryDirectory() as directory:
            host = _WindowGitHubHost(head_sha=HEAD_1)
            app, _, publisher, options = helper._make_app(host, directory, cap=1)
            result = _result(
                _comment(
                    "src/app.py", line=5, symbol="auth", body="Required auth check."
                ),
                _comment(
                    "src/db.py",
                    line=5,
                    symbol="conn",
                    body="Required connection cleanup.",
                ),
                coverage=_coverage("src/app.py", "src/db.py"),
            )
            first = app.publish_review(
                options=options,
                oidc_token="fixture-oidc",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha=HEAD_1,
                base_branch="main",
                base_sha=BASE_SHA,
                result=result,
                diff=ORDINARY_DIFF,
                app_slug="reviewsensei[bot]",
                convergence_policy=ReviewConvergencePolicy(mode="strict"),
                blocker_candidates=helper._blockers(result),
            )
            self.assertEqual(first.status, "published")
            state = publisher.active_window_state(repository_id=1, pull_request=1)
            self.assertIsNotNone(state)
            self.assertEqual(len(state.authoritative_decision_inventory.obligations), 2)
            self.assertEqual(len(host.threads), 1)
            baseline = baseline_from_active_window(
                state, baseline_template=self._baseline()
            )
            document = baseline_history_document(baseline)
            restored = baseline_from_history_document(document)
            original_criteria = tuple(
                item.resolution_criterion for item in restored.findings
            )
            host.head_sha = HEAD_2
            host.reviews.clear()
            host.threads.clear()
            host.check_runs.clear()
            host.calls.clear()
            fresh_publisher = ReviewPublisher(http=host.http)
            fresh_app = copy(app)
            fresh_app.reviewer = fresh_publisher
            outcome = fresh_app.publish_review(
                options=options,
                oidc_token="fixture-oidc",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha=HEAD_2,
                base_branch="main",
                base_sha=BASE_SHA,
                result=_result(coverage=_coverage("src/app.py", "src/db.py")),
                diff=ORDINARY_DIFF,
                app_slug="reviewsensei[bot]",
                convergence_policy=ReviewConvergencePolicy(mode="strict"),
                baseline=restored,
                current_key=replace(restored.cache_key, head_sha=HEAD_2),
            )
            self.assertEqual(
                (outcome.status, outcome.diagnostic), ("handoff", DIAGNOSTIC)
            )
            self.assertFalse(any(method != "GET" for method, _, _ in host.calls))
            self.assertEqual(host.reviews, [])
            self.assertEqual(host.threads, [])
            self.assertEqual(host.check_runs, [])
            self.assertEqual(baseline_history_document(restored), document)
            self.assertEqual(
                tuple(item.resolution_criterion for item in restored.findings),
                original_criteria,
            )
            self.assertIsNone(
                fresh_publisher.active_window_state(repository_id=1, pull_request=1)
            )

    def test_initial_full_review_still_publishes_without_prior_inventory(self):
        for route in ({"max_active_findings": 1}, {"enable_drift_tracking": True}):
            with self.subTest(route=route):
                host = _WindowGitHubHost(head_sha=HEAD_2)
                outcome = self._publish(ReviewPublisher(http=host.http), **route)
                self.assertEqual(outcome.status, "published")
                self.assertTrue(host.reviews)

    def test_same_process_baseline_keeps_criterion_and_omission_hold(self):
        host = _WindowGitHubHost(head_sha=HEAD_1)
        publisher = ReviewPublisher(http=host.http)
        result = _result(
            _comment("src/app.py", line=5),
            coverage=_coverage("src/app.py", "src/db.py"),
        )
        first = self._publish(
            publisher,
            head_sha=HEAD_1,
            result=result,
            max_active_findings=1,
            blocker_candidates=fixtures.OrdinaryPublisherActiveWindowTests._blockers(
                result
            ),
        )
        self.assertEqual(first.status, "published")
        state = publisher.active_window_state(repository_id=1, pull_request=1)
        baseline = baseline_from_active_window(
            state, baseline_template=self._baseline()
        )
        original = state.authoritative_decision_inventory.obligations[0]
        host.head_sha = HEAD_2
        outcome = self._publish(
            publisher,
            baseline=baseline,
            current_key=replace(baseline.cache_key, head_sha=HEAD_2),
            max_active_findings=1,
        )
        self.assertEqual(outcome.status, "published")
        current = publisher.active_window_state(repository_id=1, pull_request=1)
        obligation = current.authoritative_decision_inventory.obligations[0]
        self.assertEqual(obligation.resolution_criterion, original.resolution_criterion)
        self.assertEqual(obligation.obligation_id, original.obligation_id)
        self.assertEqual(obligation.lifecycle_state, "omitted-uncertain")
        self.assertFalse(current.approval_eligibility.approved)
        self.assertFalse(any(item["state"] == "APPROVED" for item in host.reviews))

    def test_switch_to_drift_cannot_forget_existing_window_inventory(self):
        host = _WindowGitHubHost(head_sha=HEAD_1)
        publisher = ReviewPublisher(http=host.http)
        first = self._publish(publisher, head_sha=HEAD_1, max_active_findings=1)
        self.assertEqual(first.status, "published")
        previous = publisher.active_window_state(repository_id=1, pull_request=1)
        host.head_sha = HEAD_2
        host.calls.clear()
        outcome = self._publish(publisher, enable_drift_tracking=True)
        self.assertEqual((outcome.status, outcome.diagnostic), ("handoff", DIAGNOSTIC))
        self.assertEqual(host.calls, [])
        self.assertEqual(
            publisher.active_window_state(repository_id=1, pull_request=1), previous
        )
        self.assertIsNone(publisher.drift_state(repository_id=1, pull_request=1))

    def test_reanalysis_flag_cannot_supply_missing_inventory_authority(self):
        host = _WindowGitHubHost(head_sha=HEAD_2)
        outcome = self._publish(
            ReviewPublisher(http=host.http),
            baseline=self._baseline(),
            max_active_findings=1,
            explicit_reanalysis=True,
            host_resolved_threads=frozenset({"PRRT_deleted"}),
        )
        self.assertEqual((outcome.status, outcome.diagnostic), ("handoff", DIAGNOSTIC))
        self.assertEqual(host.calls, [])

    def test_prior_native_thread_flags_cannot_supply_missing_inventory(self):
        for flags in (
            {"host_resolved_threads": frozenset({"PRRT_prior"})},
            {"host_outdated_threads": frozenset({"PRRT_prior"})},
        ):
            with self.subTest(flags=flags):
                host = _WindowGitHubHost(head_sha=HEAD_2)
                outcome = self._publish(
                    ReviewPublisher(http=host.http),
                    check_token="fixture-check-token",
                    **flags,
                )
                self.assertEqual(
                    (outcome.status, outcome.diagnostic), ("handoff", DIAGNOSTIC)
                )
                self.assertEqual(host.calls, [])

    def test_explicit_dispositions_and_concern_evidence_cannot_restart_history(self):
        disposition = FindingDisposition(
            fingerprint=_sha("prior-concern"),
            action="accept-risk",
            reason="Tracked exception for the named concern.",
            actor="fixture-maintainer",
            head_sha=HEAD_2,
        )
        prior = approval_eligibility_from_result(
            _result(_comment(needs_human=True), coverage=_coverage("src/app.py")),
            head_sha=HEAD_2,
            base_sha=BASE_SHA,
            enabled=True,
            app_authored=True,
        )
        for context in (
            {"authorized_dispositions": (disposition,)},
            {"evidence_confirmed_concerns": (_sha("prior-concern"),)},
            {
                "retained_eligibility": prior,
                "continuation_digest": _sha("continuation"),
                "source_guard": lambda: True,
            },
            {
                "result": replace(
                    _result(),
                    finding_lifecycles=(
                        FindingLifecycleRecord(_sha("prior-concern"), "uncertain"),
                    ),
                )
            },
        ):
            for route in ({"max_active_findings": 1}, {"enable_drift_tracking": True}):
                with self.subTest(context=tuple(context), route=route):
                    host = _WindowGitHubHost(head_sha=HEAD_2)
                    outcome = self._publish(
                        ReviewPublisher(http=host.http),
                        check_token="fixture-check-token",
                        **route,
                        **context,
                    )
                    self.assertEqual(
                        (outcome.status, outcome.diagnostic), ("handoff", DIAGNOSTIC)
                    )
                    self.assertEqual(host.calls, [])

    def test_recovery_diagnostic_is_public_and_bounded(self):
        self.assertIn(DIAGNOSTIC, PUBLIC_DIAGNOSTICS)
        validate_public_document(
            RunOutcome("action_required", diagnostic=DIAGNOSTIC).to_dict(),
            "run-outcome",
        )
