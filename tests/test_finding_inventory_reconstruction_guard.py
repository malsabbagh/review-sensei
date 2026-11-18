"""Reduced durable baselines cannot restore active findings or native holds."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from copy import copy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from review_sensei_github import GitHubPublicationError, ReviewPublisher
from review_sensei_github.approval import approval_eligibility_from_result

from review_sensei.active_finding_window import baseline_from_active_window
from review_sensei.baseline import (
    ReviewBaseline,
    baseline_from_history_document,
    baseline_from_review,
    baseline_history_document,
)
from review_sensei.context import ReviewContextCacheKey
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.disposition import FindingDisposition
from review_sensei.models import FindingLifecycleRecord, ReviewTransaction
from review_sensei.outcomes import PUBLIC_DIAGNOSTICS, RunOutcome
from review_sensei.schemas import validate_public_document
from review_sensei.session import (
    LocalSessionLedger,
    SessionIdentity,
    SessionLoadResult,
    checkpoint_review_analysis,
    prepare_review_transaction,
    session_reservation_id,
)
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


class EnrolledColdFindingGuardTests(unittest.TestCase):
    """An unbound full result cannot hide a selected ledger's existing lifecycle."""

    identity = SessionIdentity("owner/repo", 1, repository_id=1)
    _baseline = FindingInventoryReconstructionGuardTests._baseline

    def _produce(self, directory, *, drift=False):
        from tests.test_review_transaction import CONFIGURATION, EVIDENCE, POLICY

        helper = fixtures.OrdinaryPublisherActiveWindowTests()
        host = _WindowGitHubHost(head_sha=HEAD_1)
        _, app, publisher, options = helper._make_app(host, directory, cap=1)
        options = replace(options, enable_drift_tracking=drift)
        result = replace(
            _result(
                _comment("src/app.py", line=5, body="Required auth check."),
                _comment("src/db.py", line=5, symbol="conn", body="Required cleanup."),
            ),
            model="fixture-model",
        )
        ledger = app.session_ledger
        prepared = prepare_review_transaction(
            ledger,
            self.identity,
            POLICY,
            reservation_id=session_reservation_id(
                repository="owner/repo", pull_request=1, head_sha=HEAD_1, kind="publish"
            ),
            base_sha=BASE_SHA,
            head_sha=HEAD_1,
            configuration_digest=ReviewTransaction.compute_configuration_digest(
                CONFIGURATION
            ),
            evidence_digest=ReviewTransaction.compute_evidence_digest(EVIDENCE),
        )
        baseline = baseline_from_review(
            result,
            cache_key=self._baseline().cache_key,
            policy=POLICY,
            generation=prepared.record.generation + 1,
        )
        result = checkpoint_review_analysis(
            ledger, self.identity, prepared, result, baseline=baseline
        )
        arguments = dict(
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
            convergence_policy=POLICY,
            configuration_context=CONFIGURATION,
            evidence_context=EVIDENCE,
            blocker_candidates=helper._blockers(result),
        )
        outcome = app.publish_review(**arguments)
        self.assertEqual(outcome.status, "published")
        if drift:
            inventory = publisher.drift_state(repository_id=1, pull_request=1)
            self.assertEqual(len(inventory.inventory.obligations), 2)
            self.assertEqual(len(inventory.active_entries), 1)
            self.assertIsNone(
                publisher.active_window_state(repository_id=1, pull_request=1)
            )
        else:
            inventory = publisher.active_window_state(repository_id=1, pull_request=1)
            self.assertEqual(
                len(inventory.authoritative_decision_inventory.obligations), 2
            )
            self.assertEqual(len(inventory.display_active_findings.entries), 1)
        return app, host, arguments

    def test_real_process_restart_omitted_context_preserves_exact_ledger(self):
        # The child receives only the ordinary on-disk ledger, never a supplied
        # lifecycle/baseline/slot object or the producer publisher's dictionaries.
        child = r"""
import json, sys
from pathlib import Path
from dataclasses import replace
from review_sensei.session import LocalSessionLedger
from review_sensei_github import ReviewPublisher
from tests.test_active_window_lifecycle_g10 import (
    OrdinaryPublisherActiveWindowTests, _WindowGitHubHost, _result, BASE_SHA,
    HEAD_2, ORDINARY_DIFF,
)
host = _WindowGitHubHost(head_sha=HEAD_2)
_, app, _, options = OrdinaryPublisherActiveWindowTests()._make_app(host, sys.argv[2], cap=1)
app.session_ledger = LocalSessionLedger(Path(sys.argv[1]))
options = replace(options, max_active_findings=None, enable_drift_tracking=sys.argv[3]=='drift')
outcome = app.publish_review(
    options=options, oidc_token='fixture-oidc', repository='owner/repo',
    repository_id=1, pull_request=1, head_sha=HEAD_2, base_branch='main',
    base_sha=BASE_SHA, result=_result(), diff=ORDINARY_DIFF,
    app_slug='reviewsensei[bot]',
    **({'max_active_findings':1} if sys.argv[3]=='window' else {}),
)
print(json.dumps({'status':outcome.status, 'diagnostic':outcome.diagnostic,
                  'requests':[(m,p) for m,p,_ in host.calls]}))
"""
        with tempfile.TemporaryDirectory() as directory:
            app, _, _ = self._produce(directory)
            before = {
                p.relative_to(directory): p.read_bytes()
                for p in Path(directory).rglob("*")
                if p.is_file()
            }
            record = app.session_ledger.load(self.identity).record
            criteria = tuple(
                item.resolution_criterion
                for item in baseline_from_history_document(
                    record.convergence_history["baseline"]
                ).findings
            )
            self.assertEqual(len(criteria), 2)
            for route in ("window", "drift"):
                with self.subTest(route=route), tempfile.TemporaryDirectory() as empty:
                    process = subprocess.run(
                        [sys.executable, "-c", child, directory, empty, route],
                        cwd=Path(__file__).resolve().parents[1],
                        env={
                            **os.environ,
                            "PYTHONPATH": os.pathsep.join(
                                str(Path(__file__).resolve().parents[1] / relative)
                                for relative in (
                                    "src",
                                    "packages/python/review-sensei-github/src",
                                    ".",
                                )
                            ),
                        },
                        text=True,
                        capture_output=True,
                        check=False,
                        timeout=30,
                    )
                    self.assertEqual(process.returncode, 0, process.stderr)
                    observed = json.loads(process.stdout)
                    # This is the material regression: preceding source
                    # reserved and aborted (generation 4 -> 6) despite the
                    # eventual generic handoff. Check bytes before diagnostic.
                    self.assertEqual(
                        {
                            p.relative_to(directory): p.read_bytes()
                            for p in Path(directory).rglob("*")
                            if p.is_file()
                        },
                        before,
                    )
                    self.assertEqual(
                        LocalSessionLedger(Path(directory)).load(self.identity).record,
                        record,
                    )
                    self.assertEqual(
                        (observed["status"], observed["diagnostic"]),
                        ("handoff", DIAGNOSTIC),
                    )
                    self.assertEqual(observed["requests"], [])

    def test_bound_terminal_replay_keeps_existing_transaction_semantics(self):
        with tempfile.TemporaryDirectory() as directory:
            app, host, arguments = self._produce(directory)
            app.reviewer = ReviewPublisher(http=host.http)
            before = app.session_ledger.load(self.identity).record
            host.calls.clear()
            outcome = app.publish_review(**arguments)
            self.assertEqual(
                (outcome.status, outcome.diagnostic),
                ("already_published", "transaction-publication-complete"),
            )
            self.assertEqual(app.session_ledger.load(self.identity).record, before)
            self.assertEqual(host.calls, [])

    def test_restore_cleanup_preserves_nonweakref_ledger_error(self):
        from review_sensei_github.quarantine_lifecycle_transfer import (
            clear_namespace_restore,
        )

        from review_sensei.errors import ReviewInputError

        # Cleanup must retain a selected-reader error when no weak token could
        # ever have been registered for the object.
        with self.assertRaisesRegex(ReviewInputError, "original selected refusal"):
            try:
                raise ReviewInputError("original selected refusal")
            finally:
                clear_namespace_restore(SimpleNamespace())

    def test_bad_selected_ledger_cannot_be_treated_as_new_inventory(self):
        for status in ("conflict", "integrity-failed", "expired"):
            with (
                self.subTest(status=status),
                tempfile.TemporaryDirectory() as directory,
            ):
                host = _WindowGitHubHost(head_sha=HEAD_2)
                _, app, _, options = (
                    fixtures.OrdinaryPublisherActiveWindowTests()._make_app(
                        host, directory, cap=1
                    )
                )
                # No mutating ledger API exists on this double. Refusal must
                # happen at the selected read, not via reserve/abort cleanup.
                app.session_ledger = SimpleNamespace(
                    load=lambda identity, **kwargs: SessionLoadResult(status)
                )
                with self.assertRaisesRegex(
                    GitHubPublicationError, "finding inventory.*" + status
                ):
                    app.publish_review(
                        options=options,
                        oidc_token="fixture",
                        repository="owner/repo",
                        repository_id=1,
                        pull_request=1,
                        head_sha=HEAD_2,
                        base_branch="main",
                        base_sha=BASE_SHA,
                        result=_result(),
                        diff=ORDINARY_DIFF,
                        app_slug="reviewsensei[bot]",
                    )
                self.assertEqual(host.calls, [])

    def test_hosted_known_root_uses_one_bounded_read_without_mutations(self):
        from copy import deepcopy

        from review_sensei_github.session_ledger import render_session_comment

        from review_sensei.bounded_evidence import EvidenceReadBudget

        with tempfile.TemporaryDirectory() as directory:
            app, host, arguments = self._produce(directory)
            record = app.session_ledger.load(self.identity).record
            host.issue_comments = [
                {
                    "id": 801,
                    "body": render_session_comment(
                        repository_id=1, pull_request=1, record=record
                    ),
                    "user": {"id": 9001, "login": "reviewsensei[bot]", "type": "Bot"},
                }
            ]
            before = deepcopy(host.issue_comments)
            # Projection deletion cannot turn the enrolled root into a fresh
            # lifecycle or justify recreating threads/reviews/checks.
            host.reviews.clear()
            host.threads.clear()
            host.check_runs.clear()
            host.head_sha = HEAD_2
            host.calls.clear()
            app.session_ledger = None
            app.reviewer = ReviewPublisher(http=host.http)
            app.broker.open_session = lambda *args, **kwargs: SimpleNamespace(
                token="session-token", state="known"
            )
            budget = EvidenceReadBudget()
            outcome = app.publish_review(
                options=replace(arguments["options"], github_session_ledger=True),
                oidc_token="fixture",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha=HEAD_2,
                base_branch="main",
                base_sha=BASE_SHA,
                result=_result(),
                diff=ORDINARY_DIFF,
                app_slug="reviewsensei[bot]",
                publication_budget=budget,
            )
            self.assertEqual(
                (outcome.status, outcome.diagnostic), ("handoff", DIAGNOSTIC)
            )
            self.assertEqual(host.issue_comments, before)
            self.assertEqual(budget.calls, 1)
            self.assertEqual(
                [(m, p) for m, p, _ in host.calls],
                [("GET", "/repos/owner/repo/issues/1/comments")],
            )
            self.assertEqual(
                (host.reviews, host.threads, host.check_runs), ([], [], [])
            )

    def test_unbound_call_keeps_original_analysis_reservation_held(self):
        from tests.test_review_transaction import CONFIGURATION, EVIDENCE, POLICY

        with tempfile.TemporaryDirectory() as directory:
            host = _WindowGitHubHost(head_sha=HEAD_1)
            _, app, _, options = (
                fixtures.OrdinaryPublisherActiveWindowTests()._make_app(
                    host, directory, cap=1
                )
            )
            prepared = prepare_review_transaction(
                app.session_ledger,
                self.identity,
                POLICY,
                reservation_id=session_reservation_id(
                    repository="owner/repo",
                    pull_request=1,
                    head_sha=HEAD_1,
                    kind="publish",
                ),
                base_sha=BASE_SHA,
                head_sha=HEAD_1,
                configuration_digest=ReviewTransaction.compute_configuration_digest(
                    CONFIGURATION
                ),
                evidence_digest=ReviewTransaction.compute_evidence_digest(EVIDENCE),
            )
            before = app.session_ledger.load(self.identity).record
            outcome = app.publish_review(
                options=options,
                oidc_token="fixture",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha=HEAD_1,
                base_branch="main",
                base_sha=BASE_SHA,
                result=_result(),
                diff=ORDINARY_DIFF,
                app_slug="reviewsensei[bot]",
            )
            self.assertEqual(
                (outcome.status, outcome.diagnostic), ("handoff", DIAGNOSTIC)
            )
            after = app.session_ledger.load(self.identity).record
            self.assertEqual(after, before)
            self.assertEqual(after.reservation_id, prepared.reservation_id)
            self.assertEqual(after.transaction.phase, "analysis")
            self.assertEqual(host.calls, [])

    def test_pristine_selected_ledger_reaches_session_preparation(self):
        from review_sensei_github import application as application_module

        # A genuinely first unbound call is not diagnosed as lost inventory.
        # Its downstream identity-bound-transaction requirement is unchanged.
        for drift in (False, True):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as directory:
                host = _WindowGitHubHost(head_sha=HEAD_1)
                _, app, _, options = (
                    fixtures.OrdinaryPublisherActiveWindowTests()._make_app(
                        host, directory, cap=1
                    )
                )
                options = replace(options, enable_drift_tracking=drift)
                before = app.session_ledger.load(self.identity).record
                self.assertEqual(before.completed_initial_reviews, 0)
                self.assertIsNone(before.transaction)
                self.assertIsNone(before.convergence_history)
                with patch.object(
                    application_module,
                    "prepare_session_round",
                    wraps=application_module.prepare_session_round,
                ) as prepare:
                    outcome = app.publish_review(
                        options=options,
                        oidc_token="fixture",
                        repository="owner/repo",
                        repository_id=1,
                        pull_request=1,
                        head_sha=HEAD_1,
                        base_branch="main",
                        base_sha=BASE_SHA,
                        result=_result(),
                        diff=ORDINARY_DIFF,
                        app_slug="reviewsensei[bot]",
                    )
                prepare.assert_called_once()
                self.assertEqual(prepare.call_args.args[1], self.identity)
                self.assertEqual(
                    (outcome.status, outcome.diagnostic),
                    ("handoff", "identity-bound transaction required"),
                )
                self.assertEqual(host.calls, [])

    def test_present_drift_with_cap_does_not_require_unused_window_state(self):
        from review_sensei_github import application as application_module

        with tempfile.TemporaryDirectory() as directory:
            app, host, arguments = self._produce(directory, drift=True)
            self.assertIsNotNone(
                app.reviewer.drift_state(repository_id=1, pull_request=1)
            )
            self.assertIsNone(
                app.reviewer.active_window_state(repository_id=1, pull_request=1)
            )
            self.assertEqual(arguments["options"].max_active_findings, 1)
            host.calls.clear()
            with patch.object(
                application_module,
                "prepare_session_round",
                wraps=application_module.prepare_session_round,
            ) as prepare:
                outcome = app.publish_review(**{**arguments, "result": _result()})
            # Drift is the selected lifecycle; the cap configures its display
            # size, not a simultaneous independent active-window inventory.
            prepare.assert_called_once()
            self.assertNotEqual(outcome.diagnostic, DIAGNOSTIC)
            self.assertEqual(host.calls, [])
