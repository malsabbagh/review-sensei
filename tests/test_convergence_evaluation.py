from __future__ import annotations

import inspect
import io
import json
import os
import unittest
from contextlib import redirect_stderr
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from review_sensei.baseline import (
    BaselineFinding,
    ReviewBaseline,
    baseline_history_document,
)
from review_sensei.cli import main
from review_sensei.context import ReviewContextCacheKey
from review_sensei.convergence import (
    DEFAULT_REVIEW_MODE,
    REVIEW_MODE_ENV,
    REVIEW_SHADOW_ENV,
    ReviewConvergencePolicy,
    RoundSessionState,
    observe_shadow_admission,
    resolve_review_mode,
    resolve_shadow_review_mode,
)
from review_sensei.diagnostics import build_plan, render_diagnostic, run_doctor
from review_sensei.errors import ReviewInputError
from review_sensei.hosting.github import GitHubApplication, GitHubWriteOptions
from review_sensei.hosting.github.observed import (
    _admitted_material_ids,
    _ObservedHTTPResponse,
    _ObservedProvider,
    observed_cutover_gaps,
    observed_publication_configuration,
    restored_compatible_baseline,
    run_observed_review_sequence,
)
from review_sensei.hosting.github.publication import PublicationResult
from review_sensei.models import (
    ReviewResult,
    ReviewTransaction,
    build_transaction_configuration_context,
    transaction_stage_identity,
)
from review_sensei.schemas import validate_public_document
from review_sensei.sequence import (
    UNAVAILABLE_EVIDENCE_IDENTITY,
    ObservedExecutionMetrics,
    ObservedSequenceEvent,
    ObservedSequenceReport,
    SequenceStep,
    compare_sequence_policies,
    replay_review_sequence,
)
from review_sensei.service import ReviewService
from review_sensei.session import (
    InMemorySessionLedger,
    LocalSessionLedger,
    SessionIdentity,
    SessionRecord,
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


def _policy(mode: str = "merge-focused") -> ReviewConvergencePolicy:
    return ReviewConvergencePolicy(mode=mode)


class DefaultIsMergeFocusedTests(unittest.TestCase):
    def test_installed_default_is_merge_focused(self):
        self.assertEqual(DEFAULT_REVIEW_MODE, "merge-focused")
        self.assertEqual(resolve_review_mode(), "merge-focused")
        self.assertEqual(ReviewConvergencePolicy().mode, "merge-focused")
        self.assertEqual(run_doctor()["review_convergence"]["mode"], "merge-focused")
        self.assertNotIn("shadow_review_convergence", run_doctor())


class SequenceReplayTests(unittest.TestCase):
    def test_fixing_ab_with_only_optional_leftovers_can_converge(self):
        report = replay_review_sequence(
            (
                SequenceStep(
                    head_sha="a" * 40,
                    blocking_identities=("defect-a", "defect-b"),
                    label="initial-ab",
                ),
                SequenceStep(
                    head_sha="b" * 40,
                    blocking_identities=(),
                    independently_approval_eligible=True,
                    label="optional-cd",
                ),
            ),
            _policy(),
        )
        self.assertTrue(report.steps[0].admit)
        self.assertEqual(report.steps[0].round_kind, "initial")
        self.assertTrue(report.steps[1].admit)
        self.assertEqual(report.steps[1].round_kind, "verification")
        self.assertTrue(report.steps[1].may_emit_approve)
        self.assertEqual(report.completed_initial_reviews, 1)
        self.assertEqual(report.completed_verification_rounds, 1)
        self.assertEqual(report.handoffs, 0)
        self.assertFalse(report.cap_created_approval)
        payload = report.to_dict()
        self.assertEqual(payload["mode"], "merge-focused")
        self.assertIn("not a claim of zero missed defects", payload["limitations"][0])

    def test_regression_is_detected_and_cap_does_not_approve(self):
        report = replay_review_sequence(
            (
                SequenceStep(
                    head_sha="a" * 40,
                    blocking_identities=("defect-a",),
                    label="initial",
                ),
                SequenceStep(
                    head_sha="b" * 40,
                    blocking_identities=("defect-c",),
                    label="regression",
                ),
                SequenceStep(
                    head_sha="c" * 40,
                    blocking_identities=("defect-e",),
                    label="still-open",
                ),
                SequenceStep(
                    head_sha="d" * 40,
                    blocking_identities=("defect-f",),
                    independently_approval_eligible=False,
                    label="at-cap",
                ),
            ),
            _policy(),
        )
        self.assertTrue(report.steps[1].admit)
        self.assertEqual(report.steps[1].round_kind, "verification")
        self.assertFalse(report.steps[3].admit)
        self.assertTrue(report.steps[3].handoff)
        self.assertEqual(report.steps[3].handoff_reason, "round-budget-exhausted")
        self.assertFalse(report.steps[3].may_emit_approve)
        self.assertFalse(report.cap_created_approval)

    def test_aba_oscillation_is_no_progress(self):
        report = replay_review_sequence(
            (
                SequenceStep(
                    head_sha="a" * 40,
                    blocking_identities=("defect-a",),
                    label="first-a",
                ),
                SequenceStep(
                    head_sha="b" * 40,
                    blocking_identities=("defect-b",),
                    label="then-b",
                ),
                SequenceStep(
                    head_sha="c" * 40,
                    blocking_identities=("defect-a",),
                    label="again-a",
                ),
            ),
            _policy(),
        )
        self.assertTrue(report.steps[2].no_progress)
        self.assertFalse(report.steps[2].admit)
        self.assertEqual(report.steps[2].handoff_reason, "no-progress")
        self.assertEqual(report.no_progress_events, 1)
        self.assertFalse(report.cap_created_approval)

    def test_comparison_uses_merge_focused_default_and_never_mints_cap_approval(self):
        steps = (
            SequenceStep(
                head_sha="a" * 40,
                blocking_identities=("defect-a", "defect-b"),
                label="initial",
            ),
            SequenceStep(
                head_sha="b" * 40,
                blocking_identities=(),
                independently_approval_eligible=True,
                label="clean",
            ),
        )
        payload = compare_sequence_policies(steps)
        self.assertEqual(payload["publication_default"], "merge-focused")
        self.assertEqual(payload["current"]["mode"], "merge-focused")
        self.assertEqual(payload["proposed"]["mode"], "merge-focused")
        self.assertFalse(payload["cap_created_approval"])
        self.assertFalse(payload["current"]["cap_created_approval"])
        self.assertFalse(payload["proposed"]["cap_created_approval"])


class ObservedSequenceTests(unittest.TestCase):
    def test_observed_harness_records_real_service_and_publication_events(self):
        report = run_observed_review_sequence(
            (
                SequenceStep(
                    head_sha="a" * 40,
                    expected_material_finding_ids=("material-a",),
                    fixture_material_finding_ids=("material-a",),
                    label="initial-regression",
                ),
                SequenceStep(
                    head_sha="b" * 40,
                    independently_approval_eligible=True,
                    label="verification-fixed",
                ),
            ),
            _policy(),
        )
        self.assertEqual(report.mode, "merge-focused")
        self.assertEqual(len(report.events), 2)
        self.assertTrue(all(event.provider_calls == 1 for event in report.events))
        self.assertEqual(
            [event.baseline_loaded for event in report.events], [False, True]
        )
        self.assertEqual(
            [event.publication_status for event in report.events],
            ["published", "published"],
        )
        # The first review has a confirmed material blocker; the clean
        # verification is the only observed approval event.
        self.assertEqual(report.approval_events, 1)
        self.assertEqual(report.baseline_events, 2)
        self.assertEqual(report.command_events, ("pause:applied", "continue:applied"))
        self.assertEqual(report.execution_metrics.completed_rounds, 2)
        self.assertEqual(report.execution_metrics.provider_calls, 2)
        self.assertTrue(report.shadow_isolated)
        self.assertIsNone(report.cap_created_approval)
        self.assertEqual(report.cutover_status, "not_ready")
        self.assertIn(
            "the configured round cap was not observed with zero new approval events",
            report.unmet_criteria,
        )
        self.assertEqual(
            report.to_dict()["events"][0]["publication_status"], "published"
        )

    def test_observed_harness_stops_inference_at_the_round_cap(self):
        report = run_observed_review_sequence(
            (
                SequenceStep(
                    head_sha="a" * 40,
                    expected_material_finding_ids=("material-a",),
                    fixture_material_finding_ids=("material-a",),
                    label="initial-regression",
                ),
                SequenceStep(head_sha="b" * 40, label="verification-fixed"),
                SequenceStep(head_sha="c" * 40, label="clean-repeat"),
                SequenceStep(head_sha="d" * 40, label="over-cap"),
            ),
            _policy(),
        )
        self.assertEqual(
            [
                (event.publication_status, event.provider_calls)
                for event in report.events
            ],
            [
                ("published", 1),
                ("published", 1),
                # An empty admitted blocker set is progress, not a repeated
                # blocker set, so a second clean round still publishes.
                ("published", 1),
                ("handoff", 0),
            ],
        )
        self.assertEqual(report.execution_metrics.completed_rounds, 3)
        self.assertEqual(report.execution_metrics.provider_calls, 3)
        # Both in-budget clean rounds approve. The over-cap handoff adds none.
        self.assertEqual(
            [event.approval_events for event in report.events], [0, 1, 1, 0]
        )
        self.assertEqual(report.approval_events, 2)
        self.assertEqual(report.events[-1].approval_events, 0)
        self.assertEqual(report.events[-1].handoff_reason, "round-budget-exhausted")
        self.assertFalse(report.cap_created_approval)
        self.assertNotIn(
            "the configured round cap was not observed with zero new approval events",
            report.unmet_criteria,
        )
        self.assertNotIn(
            "an over-cap request did not prove zero new inference",
            report.unmet_criteria,
        )

    def test_observed_harness_compares_material_labels_to_fixture_output(self):
        report = run_observed_review_sequence(
            (
                SequenceStep(
                    head_sha="a" * 40,
                    expected_material_finding_ids=("material-a",),
                    fixture_material_finding_ids=("material-a",),
                    label="seeded-regression",
                ),
                SequenceStep(head_sha="b" * 40, label="verification"),
            ),
            _policy(),
        )
        metrics = report.finding_metrics
        self.assertEqual(metrics.expected_material_findings, 1)
        self.assertEqual(metrics.observed_material_findings, 1)
        self.assertEqual(metrics.matched_material_findings, 1)
        self.assertEqual(metrics.missed_material_findings, 0)
        self.assertEqual(metrics.unjustified_late_blockers, 0)
        self.assertEqual(metrics.blocker_precision, 1.0)
        self.assertEqual(metrics.seeded_material_regressions_detected, 1)
        self.assertEqual(metrics.duplicate_findings, 0)
        self.assertEqual(metrics.reopened_findings, 0)
        self.assertEqual(metrics.contradictions, 0)

    def test_observed_harness_measures_deduplication_reopens_and_contradictions(self):
        report = run_observed_review_sequence(
            (
                SequenceStep(
                    head_sha="a" * 40,
                    expected_material_finding_ids=("material-a",),
                    # The real service normalizes this duplicate before it can
                    # reach C2 or the publisher.
                    fixture_material_finding_ids=("material-a", "material-a"),
                    label="deduplicated",
                ),
                SequenceStep(head_sha="b" * 40, label="fixed"),
                SequenceStep(
                    head_sha="c" * 40,
                    fixture_material_finding_ids=("material-a",),
                    label="reopened",
                ),
                SequenceStep(
                    head_sha="d" * 40,
                    expected_non_material_finding_ids=("material-b",),
                    fixture_material_finding_ids=("material-b",),
                    label="contradiction",
                ),
            ),
            ReviewConvergencePolicy(
                mode="merge-focused",
                max_completed_verification_rounds=4,
            ),
        )
        metrics = report.finding_metrics
        self.assertEqual(metrics.duplicate_findings, 0)
        self.assertEqual(metrics.reopened_findings, 1)
        self.assertEqual(metrics.contradictions, 1)
        self.assertEqual(metrics.unjustified_late_blockers, 1)
        self.assertEqual(metrics.observed_material_findings, 3)
        # Precision uses distinct admitted ids. Occurrences stay in the count above.
        self.assertEqual(metrics.blocker_precision, 0.5)

    def test_observed_metrics_count_only_admitted_material_ids(self):
        report = run_observed_review_sequence(
            (
                SequenceStep(
                    head_sha="a" * 40,
                    expected_material_finding_ids=("material-a", "material-missed"),
                    fixture_material_finding_ids=("material-a",),
                    label="partial-admission",
                ),
            ),
            _policy(),
        )
        metrics = report.finding_metrics
        self.assertEqual(metrics.observed_material_findings, 1)
        self.assertEqual(metrics.matched_material_findings, 1)
        self.assertEqual(metrics.missed_material_findings, 1)
        self.assertIn(
            "a labelled material regression was missed", report.unmet_criteria
        )

    def test_completed_history_without_a_compatible_baseline_stays_unloaded(self):
        policy = _policy()
        key = ReviewContextCacheKey(
            repository="owner/repo",
            pull_request=136,
            base_sha="a" * 40,
            head_sha="b" * 40,
            engine="ollama",
            model="test",
            profile="default",
            stage_digest="c" * 64,
            context_digest="d" * 64,
            learning_digest="e" * 64,
        )
        baseline = ReviewBaseline(
            cache_key=key,
            policy_digest=policy.digest(),
            complete=True,
            coverage_complete=True,
            findings=(
                BaselineFinding(
                    fingerprint="1" * 64,
                    resolution_criterion="2" * 64,
                    concern="3" * 64,
                    path="src/observed.py",
                    symbol="run",
                    defect_kind="bug",
                    generation=1,
                    blocking=True,
                ),
            ),
            reviewed_paths=("src/observed.py",),
        )
        history = {
            "state": "completed",
            "baseline": baseline_history_document(baseline),
        }
        self.assertIsNotNone(
            restored_compatible_baseline(history, current_key=key, policy=policy)
        )
        self.assertIsNone(
            restored_compatible_baseline(
                {"state": "completed"},
                current_key=key,
                policy=policy,
            )
        )
        self.assertIsNone(
            restored_compatible_baseline(
                {"state": "completed", "baseline": {"tampered": True}},
                current_key=key,
                policy=policy,
            )
        )
        incompatible = ReviewBaseline(
            cache_key=key,
            policy_digest="f" * 64,
            complete=True,
            coverage_complete=True,
            findings=baseline.findings,
            reviewed_paths=baseline.reviewed_paths,
        )
        self.assertIsNone(
            restored_compatible_baseline(
                {
                    "state": "completed",
                    "baseline": baseline_history_document(incompatible),
                },
                current_key=key,
                policy=policy,
            )
        )

    def test_observed_http_read_advances(self):
        response = _ObservedHTTPResponse({"ok": True})
        first = response.read(2)
        rest = response.read(len(response.body))
        self.assertEqual(first + rest, response.body)
        self.assertEqual(response.read(8), b"")

    def test_unqualified_fixture_comments_are_rejected_by_admission(self):
        report = run_observed_review_sequence(
            (
                SequenceStep(
                    head_sha="a" * 40,
                    expected_material_finding_ids=("material-a",),
                    fixture_material_finding_ids=("material-a",),
                    fixture_unqualified_finding_ids=("material-weak",),
                    label="mixed-evidence",
                ),
            ),
            _policy(),
        )
        metrics = report.finding_metrics
        self.assertEqual(metrics.observed_material_findings, 1)
        self.assertEqual(metrics.matched_material_findings, 1)
        self.assertEqual(metrics.unjustified_late_blockers, 0)
        self.assertEqual(metrics.missed_material_findings, 0)

    def test_harness_configuration_matches_the_shared_transaction_builder(self):
        provider = _ObservedProvider()
        service = ReviewService(provider)
        policy = _policy()
        context = observed_publication_configuration(provider, service.stages, policy)
        stages, categories = transaction_stage_identity(service.stages)
        production = build_transaction_configuration_context(
            provider={
                "name": provider.name,
                "profile": None,
                "base_url": None,
                "timeout_seconds": None,
                "max_output_tokens": None,
                "allow_custom_endpoint": False,
                "openrouter_policy": None,
            },
            model=provider.model,
            stages=stages,
            category_policy=categories,
            publication_mode=policy.mode,
            orchestration_enabled=False,
            continue_rounds=0,
        )
        self.assertEqual(context, production)
        self.assertEqual(
            ReviewTransaction.compute_configuration_digest(context),
            ReviewTransaction.compute_configuration_digest(production),
        )

    def test_local_ledger_load_rereads_disk(self):
        identity = SessionIdentity("owner/repo", 136, repository_id=136)
        with TemporaryDirectory() as root:
            writer = LocalSessionLedger(Path(root))
            created = writer.initialize(identity, now=FIXED_NOW)
            reader = LocalSessionLedger(Path(root))
            loaded = reader.load(identity, now=FIXED_NOW)
        self.assertIsNotNone(loaded.record)
        assert loaded.record is not None
        self.assertEqual(loaded.record.record_sha256, created.record_sha256)
        self.assertIsNot(reader, writer)

    def test_shadow_factory_failure_does_not_replace_ledger_classes(self):
        original_memory = InMemorySessionLedger.__init__
        original_local = LocalSessionLedger.__init__
        original_application = GitHubApplication.__init__

        def fail_factory() -> InMemorySessionLedger:
            raise ReviewInputError("shadow factory failed")

        with self.assertRaisesRegex(ReviewInputError, "shadow factory failed"):
            compare_sequence_policies(
                (SequenceStep(head_sha="a" * 40, label="only"),),
                ledger_factory=fail_factory,
            )
        self.assertIs(InMemorySessionLedger.__init__, original_memory)
        self.assertIs(LocalSessionLedger.__init__, original_local)
        self.assertIs(GitHubApplication.__init__, original_application)
        self.assertIsInstance(InMemorySessionLedger(), InMemorySessionLedger)

    def test_null_finding_counters_raise_review_input_error(self):
        with self.assertRaisesRegex(ReviewInputError, "duplicate findings"):
            replace(report_metrics(), duplicate_findings=None)

    def test_finding_metrics_reject_negative_and_imprecise_values(self):
        with self.assertRaisesRegex(ReviewInputError, "expected material findings"):
            replace(report_metrics(), expected_material_findings=-1)
        with self.assertRaisesRegex(ReviewInputError, "blocker precision"):
            replace(report_metrics(), blocker_precision=1.5)

    def test_report_rejects_an_unknown_cutover_status(self):
        with self.assertRaisesRegex(ReviewInputError, "cutover status"):
            ObservedSequenceReport(
                mode="merge-focused",
                events=(
                    ObservedSequenceEvent(
                        label="step",
                        provider_calls=0,
                        baseline_loaded=False,
                        publication_status="handoff",
                    ),
                ),
                baseline_events=0,
                command_events=("pause:applied", "continue:applied"),
                finding_metrics=report_metrics(),
                execution_metrics=report_execution(),
                shadow_isolated=False,
                evidence_identity=report_identity(),
                approval_events=0,
                cap_created_approval=None,
                cutover_status="maybe",
                unmet_criteria=("not ready",),
            )

    def test_shared_replay_ledger_fails_closed(self):
        shared = InMemorySessionLedger()
        with self.assertRaisesRegex(ReviewInputError, "distinct"):
            compare_sequence_policies(
                (SequenceStep(head_sha="a" * 40, label="only"),),
                ledger_factory=lambda: shared,
            )

    def test_observed_sequence_rejects_more_steps_than_the_schema(self):
        steps = tuple(
            SequenceStep(head_sha=f"{index:040x}", label=f"step-{index}")
            for index in range(33)
        )
        with self.assertRaisesRegex(ReviewInputError, "event limit"):
            run_observed_review_sequence(steps, _policy())

    def test_material_labels_match_across_the_sequence(self):
        report = run_observed_review_sequence(
            (
                SequenceStep(
                    head_sha="a" * 40,
                    label="early-expect",
                    expected_material_finding_ids=("material-late",),
                ),
                SequenceStep(
                    head_sha="b" * 40,
                    label="late-emit",
                    fixture_material_finding_ids=("material-late",),
                ),
            ),
            _policy(),
        )
        metrics = report.finding_metrics
        self.assertEqual(metrics.missed_material_findings, 0)
        self.assertEqual(metrics.matched_material_findings, 1)
        self.assertEqual(metrics.unjustified_late_blockers, 0)

    def test_admitted_unqualified_comment_is_not_a_material_match(self):
        comment = type(
            "Comment",
            (),
            {
                "body": "fixture-unqualified:material-weak",
                "effective_blocking": True,
            },
        )()
        self.assertEqual(
            _admitted_material_ids((comment,)),
            ("unqualified:material-weak",),
        )

    def test_event_rejects_an_unknown_publication_status(self):
        with self.assertRaisesRegex(ReviewInputError, "publication status"):
            ObservedSequenceEvent(
                label="step",
                provider_calls=0,
                baseline_loaded=False,
                publication_status="approved",
            )

    def test_report_rejects_an_empty_event_list(self):
        with self.assertRaisesRegex(ReviewInputError, "events"):
            ObservedSequenceReport(
                mode="merge-focused",
                events=(),
                baseline_events=0,
                command_events=("pause:applied", "continue:applied"),
                finding_metrics=report_metrics(),
                execution_metrics=report_execution(),
                shadow_isolated=False,
                evidence_identity=report_identity(),
                approval_events=0,
                cap_created_approval=None,
                cutover_status="not_ready",
                unmet_criteria=("not ready",),
            )

    def test_unresolved_blocking_thread_withholds_approval(self):
        step = (
            SequenceStep(
                head_sha="b" * 40,
                label="clean",
                expected_material_finding_ids=("material-a",),
            ),
        )
        closed = run_observed_review_sequence(step, _policy())
        opened = run_observed_review_sequence(
            step, _policy(), unresolved_blocking_thread=True
        )
        self.assertEqual(closed.events[0].approval_events, 1)
        self.assertEqual(opened.events[0].approval_events, 0)

    def test_empty_limitations_raise_review_input_error(self):
        with self.assertRaisesRegex(ReviewInputError, "limitations"):
            ObservedSequenceReport(
                mode="merge-focused",
                events=(
                    ObservedSequenceEvent(
                        label="step",
                        provider_calls=0,
                        baseline_loaded=False,
                        publication_status="handoff",
                    ),
                ),
                baseline_events=0,
                command_events=("pause:applied", "continue:applied"),
                finding_metrics=report_metrics(),
                execution_metrics=report_execution(),
                shadow_isolated=False,
                evidence_identity=report_identity(),
                approval_events=0,
                cap_created_approval=None,
                cutover_status="not_ready",
                unmet_criteria=("not ready",),
                limitations=(),
            )

    def test_each_cutover_gate_has_its_own_message(self):
        base = passing_cutover_inputs()
        self.assertEqual(observed_cutover_gaps(**base), ())
        published = ObservedSequenceEvent(
            label="published",
            provider_calls=1,
            baseline_loaded=True,
            publication_status="published",
            approval_events=0,
        )
        cases = {
            "cap_created_approval": (
                None,
                "the configured round cap was not observed with zero new approval events",
            ),
            "completed_rounds": (
                0,
                "the configured initial and verification round budget was not fully exercised",
            ),
            "events": (
                (published,),
                "an over-cap request did not prove zero new inference",
            ),
            "shadow_isolated": (
                False,
                "shadow comparison isolation was not observed",
            ),
            "command_events": (
                ("pause:ignored", "continue:applied"),
                "durable maintainer command evidence is incomplete",
            ),
            "expected_material_finding_ids": (
                set(),
                "no maintainer-labelled material regression was supplied",
            ),
            "observed_material_finding_ids": (
                set(),
                "a labelled material regression was missed",
            ),
            "duplicate_findings": (
                1,
                "duplicate, reopened, or contradictory finding evidence requires adjudication",
            ),
            "source_identity": (
                UNAVAILABLE_EVIDENCE_IDENTITY,
                "installed source, package, and workflow identities are unavailable",
            ),
        }
        no_baseline = tuple(
            ObservedSequenceEvent(
                label=event.label,
                provider_calls=event.provider_calls,
                baseline_loaded=False,
                publication_status=event.publication_status,
                handoff_reason=event.handoff_reason,
                approval_events=event.approval_events,
            )
            for event in base["events"]
        )
        cases_with_events = {
            "no fresh job loaded a durable completed baseline": no_baseline,
            "no successful application publication was observed": tuple(
                ObservedSequenceEvent(
                    label=event.label,
                    provider_calls=event.provider_calls,
                    baseline_loaded=event.baseline_loaded,
                    publication_status="handoff",
                    handoff_reason=event.handoff_reason or "round-budget-exhausted",
                    approval_events=0,
                )
                for event in base["events"]
            ),
            "an unjustified material blocker was observed": None,
        }
        for field, (value, message) in cases.items():
            kwargs = dict(base)
            kwargs[field] = value
            self.assertIn(message, observed_cutover_gaps(**kwargs), field)
        self.assertIn(
            "no fresh job loaded a durable completed baseline",
            observed_cutover_gaps(**{**base, "events": no_baseline}),
        )
        handoffs = cases_with_events[
            "no successful application publication was observed"
        ]
        self.assertIn(
            "no successful application publication was observed",
            observed_cutover_gaps(**{**base, "events": handoffs}),
        )
        self.assertIn(
            "an unjustified material blocker was observed",
            observed_cutover_gaps(
                **{**base, "observed_material_finding_ids": {"material-a", "extra"}}
            ),
        )
        followed = base["events"] + (
            ObservedSequenceEvent(
                label="after-cap",
                provider_calls=1,
                baseline_loaded=True,
                publication_status="published",
            ),
        )
        self.assertEqual(
            observed_cutover_gaps(**{**base, "events": followed}),
            (),
        )
        approved_after_cap = base["events"] + (
            ObservedSequenceEvent(
                label="after-cap-approval",
                provider_calls=1,
                baseline_loaded=True,
                publication_status="published",
                approval_events=1,
            ),
        )
        self.assertIn(
            "an over-cap request did not prove zero new inference",
            observed_cutover_gaps(**{**base, "events": approved_after_cap}),
        )

    def test_repeated_material_label_keeps_distinct_precision(self):
        report = run_observed_review_sequence(
            (
                SequenceStep(
                    head_sha="a" * 40,
                    expected_material_finding_ids=("material-a",),
                    fixture_material_finding_ids=("material-a",),
                ),
                SequenceStep(
                    head_sha="b" * 40,
                    expected_material_finding_ids=("material-a",),
                    fixture_material_finding_ids=("material-a",),
                ),
            ),
            _policy(),
        )
        self.assertEqual(report.finding_metrics.observed_material_findings, 2)
        self.assertEqual(report.finding_metrics.matched_material_findings, 1)
        self.assertEqual(report.finding_metrics.blocker_precision, 1.0)

    def test_passed_cutover_requires_an_exercised_cap(self):
        with self.assertRaisesRegex(ReviewInputError, "exercised cap"):
            ObservedSequenceReport(
                mode="merge-focused",
                events=passing_cutover_inputs()["events"],
                baseline_events=1,
                command_events=("pause:applied", "continue:applied"),
                finding_metrics=report_metrics(),
                execution_metrics=report_execution(),
                shadow_isolated=True,
                evidence_identity=report_identity(),
                approval_events=0,
                cap_created_approval=None,
                cutover_status="passed",
                unmet_criteria=(),
            )

    def test_execution_metrics_reject_too_many_handoffs(self):
        with self.assertRaisesRegex(ReviewInputError, "handoffs"):
            ObservedExecutionMetrics(
                completed_rounds=0,
                handoffs=33,
                provider_calls=0,
                failed_attempts=0,
            )

    def test_published_event_rejects_a_handoff_reason(self):
        with self.assertRaisesRegex(ReviewInputError, "handoff reason"):
            ObservedSequenceEvent(
                label="published",
                provider_calls=1,
                baseline_loaded=False,
                publication_status="published",
                handoff_reason="round-budget-exhausted",
            )
        document = json.loads(
            (
                Path(__file__).parent
                / "fixtures/schemas/golden/observed-convergence-report.json"
            ).read_text(encoding="utf-8")
        )
        document["events"][0]["handoff_reason"] = "round-budget-exhausted"
        with self.assertRaisesRegex(ReviewInputError, "schema validation"):
            validate_public_document(document, "observed-convergence-report")


def report_metrics():
    from review_sensei.sequence import ObservedFindingMetrics

    return ObservedFindingMetrics(
        expected_material_findings=0,
        observed_material_findings=0,
        matched_material_findings=0,
        missed_material_findings=0,
        unjustified_late_blockers=0,
        blocker_precision=None,
        seeded_material_regressions_detected=0,
    )


def report_execution():
    from review_sensei.sequence import ObservedExecutionMetrics

    return ObservedExecutionMetrics(
        completed_rounds=0,
        handoffs=0,
        provider_calls=0,
        failed_attempts=0,
    )


def report_identity():
    from review_sensei.sequence import ObservedEvidenceIdentity

    return ObservedEvidenceIdentity(
        source_identity="source",
        package_identity="package",
        workflow_identity="workflow",
        configuration_digest="a" * 64,
        fixture_identity="fixture",
        command="review-sensei evaluate-convergence --observed",
    )


def passing_cutover_inputs() -> dict[str, object]:
    events = (
        ObservedSequenceEvent(
            label="initial",
            provider_calls=1,
            baseline_loaded=False,
            publication_status="published",
        ),
        ObservedSequenceEvent(
            label="verification",
            provider_calls=1,
            baseline_loaded=True,
            publication_status="published",
            approval_events=1,
        ),
        ObservedSequenceEvent(
            label="repeat",
            provider_calls=1,
            baseline_loaded=True,
            publication_status="published",
            approval_events=1,
        ),
        ObservedSequenceEvent(
            label="over-cap",
            provider_calls=0,
            baseline_loaded=True,
            publication_status="handoff",
            handoff_reason="round-budget-exhausted",
        ),
    )
    return {
        "events": events,
        "cap_created_approval": False,
        "completed_rounds": 3,
        "required_rounds": 3,
        "provider_calls": 3,
        "shadow_isolated": True,
        "command_events": ("pause:applied", "continue:applied"),
        "expected_material_finding_ids": {"material-a"},
        "observed_material_finding_ids": {"material-a"},
        "duplicate_findings": 0,
        "reopened_findings": 0,
        "contradictions": 0,
        "source_identity": "source",
        "package_identity": "package",
        "workflow_identity": "workflow",
    }


class ShadowObservationTests(unittest.TestCase):
    def test_ambient_legacy_env_is_rejected_by_runtime_and_shadow_resolvers(self):
        with patch.dict(
            os.environ,
            {REVIEW_MODE_ENV: "legacy", REVIEW_SHADOW_ENV: "legacy"},
        ):
            with self.assertRaisesRegex(
                ReviewInputError, "legacy review mode is retired"
            ):
                resolve_review_mode()
            with self.assertRaisesRegex(ReviewInputError, "cannot be legacy"):
                resolve_shadow_review_mode()

    def test_shadow_resolution_ignores_the_ambient_review_mode_env(self):
        # The shadow resolver reads only REVIEW_SHADOW_ENV. An ambient legacy
        # REVIEWSENSEI_REVIEW_MODE must fail the runtime resolver without
        # leaking its value into the observation-only shadow mode.
        with patch.dict(
            os.environ,
            {REVIEW_MODE_ENV: "legacy", REVIEW_SHADOW_ENV: "merge-focused"},
        ):
            with self.assertRaisesRegex(
                ReviewInputError, "legacy review mode is retired"
            ):
                resolve_review_mode()
            self.assertEqual(resolve_shadow_review_mode(), "merge-focused")
        with patch.dict(
            os.environ,
            {REVIEW_MODE_ENV: "advisory", REVIEW_SHADOW_ENV: "merge-focused"},
        ):
            self.assertEqual(resolve_review_mode(), "advisory")
            self.assertEqual(resolve_shadow_review_mode(), "merge-focused")

    def test_shadow_rejects_legacy_and_is_observation_only(self):
        with self.assertRaisesRegex(ReviewInputError, "cannot be legacy"):
            resolve_shadow_review_mode("legacy")
        self.assertIsNone(resolve_shadow_review_mode())
        decision = observe_shadow_admission(
            RoundSessionState(
                completed_initial_reviews=1,
                completed_verification_rounds=2,
                latest_head_reviewed=True,
                coverage_complete=True,
            ),
            explicit="merge-focused",
        )
        self.assertIsNotNone(decision)
        self.assertFalse(decision.admit)
        self.assertTrue(decision.handoff)
        self.assertFalse(decision.may_emit_approve)

    def test_doctor_and_plan_display_shadow_without_changing_publication(self):
        with patch.dict("os.environ", {REVIEW_SHADOW_ENV: "merge-focused"}):
            doctor = run_doctor()
            plan = build_plan(diff=DIFF)
        check = next(
            item for item in doctor["checks"] if item["name"] == "review-shadow"
        )
        self.assertEqual(check["status"], "pass")
        self.assertIn("observation-only", check["detail"])
        self.assertEqual(doctor["review_convergence"]["mode"], "merge-focused")
        self.assertEqual(doctor["shadow_review_convergence"]["mode"], "merge-focused")
        self.assertEqual(plan["review_convergence"]["mode"], "merge-focused")
        self.assertEqual(plan["shadow_review_convergence"]["mode"], "merge-focused")
        self.assertFalse(plan["operations"]["publication"])
        rendered = render_diagnostic(doctor)
        self.assertIn("review_shadow: mode=merge-focused observation-only", rendered)
        with patch.dict("os.environ", {REVIEW_SHADOW_ENV: "legacy"}):
            invalid = run_doctor()
        shadow_check = next(
            item for item in invalid["checks"] if item["name"] == "review-shadow"
        )
        self.assertEqual(shadow_check["status"], "action")
        self.assertEqual(invalid["review_convergence"]["mode"], "merge-focused")

    def test_shadow_does_not_skip_legacy_publication(self):
        ledger = InMemorySessionLedger()
        record = SessionRecord.create(
            IDENTITY,
            now=FIXED_NOW,
            completed_initial_reviews=1,
            completed_verification_rounds=2,
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
        with patch.dict("os.environ", {REVIEW_SHADOW_ENV: "merge-focused"}):
            result = application.publish_review(
                options=GitHubWriteOptions(auto_review=True, github_writes=True),
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
                convergence_policy=ReviewConvergencePolicy(mode="legacy"),
            )
        self.assertEqual(result.status, "published")
        self.assertEqual(len(reviewer.calls), 1)
        self.assertIsNotNone(result.shadow)
        self.assertTrue(result.shadow["observation_only"])
        self.assertEqual(result.shadow["mode"], "merge-focused")
        self.assertFalse(result.shadow["admit"])
        self.assertTrue(result.shadow["handoff"])
        self.assertEqual(result.shadow["handoff_reason"], "round-budget-exhausted")


class EvaluateConvergenceCliTests(unittest.TestCase):
    def test_cli_emits_observed_real_component_evidence(self):
        stdout = io.StringIO()
        with redirect_stderr(io.StringIO()):
            with patch("sys.stdout", stdout):
                status = main(
                    [
                        "evaluate-convergence",
                        "--json",
                        "--observed",
                        "--source-identity",
                        "source-sha",
                        "--package-identity",
                        "review-sensei@0.5.0",
                        "--workflow-identity",
                        "workflow-sha",
                    ]
                )
        self.assertEqual(status, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["mode"], "merge-focused")
        self.assertTrue(payload["events"])
        self.assertEqual(payload["events"][0]["provider_calls"], 1)
        self.assertEqual(payload["cutover_status"], "passed")
        self.assertEqual(payload["unmet_criteria"], [])
        validate_public_document(payload, "observed-convergence-report")
        self.assertEqual(payload["events"][1]["label"], "verification-emits-nothing")
        self.assertEqual(payload["evidence_identity"]["source_identity"], "source-sha")
        self.assertEqual(
            payload["evidence_identity"]["workflow_identity"], "workflow-sha"
        )

    def test_cli_observed_default_identities_are_not_ready(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GITHUB_SHA", None)
            os.environ.pop("GITHUB_WORKFLOW_REF", None)
            with redirect_stderr(stderr):
                with patch("sys.stdout", stdout):
                    status = main(["evaluate-convergence", "--json", "--observed"])
        self.assertEqual(status, 1)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["cutover_status"], "not_ready")
        self.assertIn(
            "installed source, package, and workflow identities are unavailable",
            payload["unmet_criteria"],
        )
        self.assertIn("unavailable for source, workflow", stderr.getvalue())
        self.assertNotIn("package", stderr.getvalue())

    def test_cli_observed_blank_environment_identities_are_not_ready(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.dict(
            os.environ,
            {"GITHUB_SHA": "", "GITHUB_WORKFLOW_REF": ""},
            clear=False,
        ):
            with redirect_stderr(stderr):
                with patch("sys.stdout", stdout):
                    status = main(["evaluate-convergence", "--json", "--observed"])
        self.assertEqual(status, 1)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["cutover_status"], "not_ready")
        self.assertEqual(
            payload["evidence_identity"]["source_identity"],
            UNAVAILABLE_EVIDENCE_IDENTITY,
        )
        self.assertIn("unavailable for source, workflow", stderr.getvalue())

    def test_cli_observed_rejects_legacy_review_mode(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with patch("sys.stdout", stdout):
                status = main(
                    [
                        "evaluate-convergence",
                        "--json",
                        "--observed",
                        "--review-mode",
                        "legacy",
                        "--source-identity",
                        "source-sha",
                        "--package-identity",
                        "review-sensei@0.5.0",
                        "--workflow-identity",
                        "workflow-sha",
                    ]
                )
        self.assertEqual(status, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn(
            "legacy review mode is retired; migrate configuration to merge-focused",
            stderr.getvalue(),
        )

    def test_cli_observed_rejects_compare_default(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with patch("sys.stdout", io.StringIO()):
                status = main(
                    [
                        "evaluate-convergence",
                        "--observed",
                        "--compare-default",
                        "--source-identity",
                        "source-sha",
                        "--package-identity",
                        "review-sensei@0.5.0",
                        "--workflow-identity",
                        "workflow-sha",
                    ]
                )
        self.assertEqual(status, 1)
        self.assertIn("cannot run in one report", stderr.getvalue())

    def test_cli_observed_identity_may_contain_the_sentinel_word(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with patch("sys.stdout", stdout):
                status = main(
                    [
                        "evaluate-convergence",
                        "--json",
                        "--observed",
                        "--source-identity",
                        "source-identity: unavailable",
                        "--package-identity",
                        "review-sensei@0.5.0",
                        "--workflow-identity",
                        "workflow-sha",
                    ]
                )
        self.assertEqual(status, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["cutover_status"], "passed")
        self.assertNotIn(
            "installed source, package, and workflow identities are unavailable",
            payload["unmet_criteria"],
        )
        self.assertNotIn("unavailable for", stderr.getvalue())
        self.assertNotEqual(
            payload["evidence_identity"]["source_identity"],
            UNAVAILABLE_EVIDENCE_IDENTITY,
        )

    def test_cli_replays_sentinel_with_merge_focused_default(self):
        stdout = io.StringIO()
        with redirect_stderr(io.StringIO()):
            with patch("sys.stdout", stdout):
                status = main(["evaluate-convergence", "--json", "--compare-default"])
        self.assertEqual(status, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["publication_default"], "merge-focused")
        self.assertFalse(payload["cap_created_approval"])
        self.assertEqual(payload["proposed"]["mode"], "merge-focused")
        self.assertEqual(payload["current"]["mode"], "merge-focused")

    def test_cli_rejects_invalid_mode(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with patch("sys.stdout", stdout):
                status = main(["evaluate-convergence", "--review-mode", "soft"])
        self.assertEqual(status, 1)
        self.assertIn("unsupported", stderr.getvalue())


class PublicExportTests(unittest.TestCase):
    def test_sequence_exports_are_importable(self):
        import review_sensei
        import review_sensei.sequence as sequence

        self.assertIs(review_sensei.SequenceStep, SequenceStep)
        self.assertIs(review_sensei.replay_review_sequence, replay_review_sequence)
        self.assertFalse(hasattr(review_sensei, "run_observed_review_sequence"))
        self.assertFalse(hasattr(sequence, "run_observed_review_sequence"))
        self.assertNotIn("hosting.github", inspect.getsource(sequence))
        import review_sensei.hosting.github.observed as observed_harness

        self.assertIs(
            observed_harness.run_observed_review_sequence,
            run_observed_review_sequence,
        )
        self.assertIs(review_sensei.observe_shadow_admission, observe_shadow_admission)
        self.assertIs(
            review_sensei.resolve_shadow_review_mode, resolve_shadow_review_mode
        )
