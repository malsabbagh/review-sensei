from __future__ import annotations

import unittest
from unittest.mock import patch

from review_sensei import (
    BlockerAdmissionDecision,
    BlockerCandidate,
    ReviewConvergencePolicy,
    RoundAdmissionDecision,
    RoundSessionState,
    admit_review_result,
    derive_blocker_candidate,
    detect_no_progress,
    evaluate_blocker_admission,
    evaluate_round_admission,
    resolve_review_convergence_policy,
)
from review_sensei.context import finding_lifecycle_for_comment
from review_sensei.convergence import (
    PREFERENCE_CATEGORIES,
    REQUIRED_CONTRACT_KINDS,
    REVIEW_MODE_ENV,
    comment_targets_pr_change,
    policy_from_mapping,
    resolve_review_mode,
)
from review_sensei.diagnostics import build_plan, render_diagnostic, run_doctor
from review_sensei.disposition import FindingDisposition
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewComment, ReviewResult
from review_sensei.schemas import validate_public_document

DIFF = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1,2 @@
 keep
+change
"""


def _admitted(**overrides: object) -> BlockerCandidate:
    values: dict[str, object] = {
        "proposed_blocking": True,
        "severity": "high",
        "has_specific_violation": True,
        "has_actionable_remedy": True,
        "evidence_locations_validated": True,
        "has_failure_condition": True,
        "attribution": "pr-change",
    }
    values.update(overrides)
    return BlockerCandidate(**values)  # type: ignore[arg-type]


class ReviewModeResolutionTests(unittest.TestCase):
    def test_default_is_legacy_and_empty_values_are_compatible(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(resolve_review_mode(), "legacy")
            self.assertEqual(resolve_review_mode(""), "legacy")
            self.assertEqual(resolve_review_mode("  "), "legacy")

    def test_cli_overrides_environment(self):
        with patch.dict("os.environ", {REVIEW_MODE_ENV: "strict"}):
            self.assertEqual(resolve_review_mode("advisory"), "advisory")
            self.assertEqual(resolve_review_mode(), "strict")

    def test_unsupported_mode_fails_closed(self):
        with self.assertRaisesRegex(ReviewInputError, "unsupported"):
            resolve_review_mode("soft")
        with self.assertRaisesRegex(ReviewInputError, "string"):
            resolve_review_mode(1)  # type: ignore[arg-type]


class PolicyContractTests(unittest.TestCase):
    def test_default_policy_is_display_only_legacy(self):
        policy = resolve_review_convergence_policy()
        document = policy.to_dict()
        self.assertEqual(document["mode"], "legacy")
        self.assertEqual(document["enforcement"], "display-only")
        self.assertEqual(document["max_completed_verification_rounds"], 2)
        self.assertTrue(document["automatic_github_review_events"])
        self.assertTrue(document["inline_advisory_threads"])
        self.assertEqual(document["policy_digest"], policy.digest())
        validate_public_document(document, "review-convergence-policy")
        restored = policy_from_mapping(document)
        self.assertEqual(restored.digest(), policy.digest())

    def test_operator_modes_resolve_publication_enforcement(self):
        policy = resolve_review_convergence_policy(mode="merge-focused")
        self.assertEqual(policy.enforcement, "publication")
        self.assertFalse(policy.inline_advisory_threads)
        advisory = resolve_review_convergence_policy(mode="advisory")
        self.assertEqual(advisory.enforcement, "publication")
        self.assertFalse(advisory.automatic_github_review_events)

    def test_advisory_disables_github_review_events_and_inline_threads(self):
        policy = ReviewConvergencePolicy(mode="advisory")
        self.assertFalse(policy.automatic_github_review_events)
        self.assertFalse(policy.inline_advisory_threads)
        self.assertEqual(policy.compatibility, "explicit-opt-in")
        self.assertEqual(policy.enforcement, "publication")

    def test_operator_mode_constructor_uses_publication_enforcement(self):
        for mode in ("advisory", "merge-focused", "strict"):
            with self.subTest(mode=mode):
                policy = ReviewConvergencePolicy(mode=mode)
                self.assertEqual(policy.enforcement, "publication")
                coerced = ReviewConvergencePolicy(mode=mode, enforcement="display-only")
                self.assertEqual(coerced.enforcement, "publication")

    def test_policy_rejects_unknown_fields_and_digest_mismatch(self):
        policy = ReviewConvergencePolicy(mode="merge-focused")
        payload = policy.to_dict()
        payload["policy_digest"] = "b" * 64
        with self.assertRaisesRegex(ReviewInputError, "policy_digest"):
            policy_from_mapping(payload)
        payload = policy.to_dict()
        payload["extra"] = True
        with self.assertRaisesRegex(ReviewInputError, "unknown fields"):
            policy_from_mapping(payload)
        with self.assertRaisesRegex(ReviewInputError, "integer"):
            ReviewConvergencePolicy(max_failed_attempts=True)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ReviewInputError, "out of range"):
            ReviewConvergencePolicy(max_completed_verification_rounds=9)


class BlockerAdmissionDecisionTableTests(unittest.TestCase):
    def test_legacy_trusts_explicit_blocking_without_evidence(self):
        policy = ReviewConvergencePolicy()
        blocked = evaluate_blocker_admission(
            BlockerCandidate(proposed_blocking=True, severity="low"),
            policy,
        )
        allowed = evaluate_blocker_admission(
            BlockerCandidate(proposed_blocking=False, severity="critical"),
            policy,
        )
        fallback = evaluate_blocker_admission(
            BlockerCandidate(severity="CRITICAL"),
            policy,
        )
        self.assertTrue(blocked.effective_blocking)
        self.assertEqual(blocked.severity_reason, "legacy-explicit-blocking")
        self.assertFalse(allowed.effective_blocking)
        self.assertTrue(fallback.effective_blocking)
        self.assertEqual(fallback.severity_reason, "legacy-severity-fallback")
        self.assertEqual(blocked.admission_reason, "legacy-classification")
        self.assertFalse(blocked.needs_human)

    def test_merge_focused_does_not_trust_model_blocking_boolean(self):
        policy = ReviewConvergencePolicy(mode="merge-focused")
        decision = evaluate_blocker_admission(
            BlockerCandidate(proposed_blocking=True, severity="critical"),
            policy,
        )
        self.assertFalse(decision.effective_blocking)
        self.assertEqual(decision.disposition, "advisory")
        self.assertEqual(decision.admission_reason, "not-admitted-advisory")

    def test_decision_table_for_operator_modes(self):
        merge = ReviewConvergencePolicy(mode="merge-focused")
        strict = ReviewConvergencePolicy(mode="strict")
        advisory = ReviewConvergencePolicy(mode="advisory")
        cases = (
            (
                "credential leakage blocks",
                _admitted(severity="critical"),
                merge,
                "block",
                True,
                "admitted-blocker",
            ),
            (
                "preference stays advisory",
                _admitted(is_preference_or_optional=True, severity="low"),
                merge,
                "advisory",
                False,
                "not-admitted-advisory",
            ),
            (
                "json validity is not proof",
                _admitted(evidence_is_json_or_line_validity_only=True),
                merge,
                "advisory",
                False,
                "not-admitted-advisory",
            ),
            (
                "model agreement is not proof",
                _admitted(evidence_is_model_confidence_or_agreement_only=True),
                merge,
                "advisory",
                False,
                "not-admitted-advisory",
            ),
            (
                "pre-existing target issue is advisory",
                _admitted(attribution="pre-existing"),
                merge,
                "advisory",
                False,
                "not-admitted-advisory",
            ),
            (
                "fix-introduced regression is attributed",
                _admitted(
                    attribution="fix-regression",
                    is_late_relative_to_baseline=True,
                    late_reason="new-regression",
                ),
                merge,
                "block",
                True,
                "new-regression",
            ),
            (
                "late substantiated miss can block",
                _admitted(
                    is_late_relative_to_baseline=True,
                    late_reason="substantiated-missed-defect",
                ),
                merge,
                "block",
                True,
                "substantiated-missed-defect",
            ),
            (
                "late preference on reviewed code is not a new blocker",
                _admitted(is_late_relative_to_baseline=True, late_reason=None),
                merge,
                "advisory",
                False,
                "not-admitted-advisory",
            ),
            (
                "duplicate is suppressed",
                _admitted(is_duplicate=True),
                merge,
                "suppressed-duplicate",
                False,
                "duplicate-of-existing",
            ),
            (
                "authorized disposition is honored",
                _admitted(has_authorized_disposition=True),
                merge,
                "honored-disposition",
                False,
                "authorized-disposition-still-valid",
            ),
            (
                "contradiction needs human, not a proven blocker",
                _admitted(has_contradictory_evidence=True),
                merge,
                "human-adjudication",
                False,
                "contradictory-evidence-needs-human",
            ),
            (
                "classification needs human without contradiction",
                _admitted(needs_human=True),
                merge,
                "human-adjudication",
                False,
                "classification-needs-human",
            ),
            (
                "weak high-impact needs human",
                _admitted(high_impact_weakly_supported=True),
                merge,
                "human-adjudication",
                False,
                "weak-high-impact-needs-human",
            ),
            (
                "independent analyzer artifact can prove evidence",
                _admitted(
                    evidence_locations_validated=False,
                    has_failure_condition=False,
                    has_independent_artifact=True,
                ),
                merge,
                "block",
                True,
                "admitted-blocker",
            ),
            (
                "required contract still needs high severity in merge-focused",
                _admitted(
                    severity="medium",
                    has_specific_violation=False,
                    has_required_contract=True,
                ),
                merge,
                "advisory",
                False,
                "not-admitted-advisory",
            ),
            (
                "required contract with high severity can block in merge-focused",
                _admitted(
                    severity="high",
                    has_specific_violation=False,
                    has_required_contract=True,
                ),
                merge,
                "block",
                True,
                "admitted-blocker",
            ),
            (
                "strict named rule can block at medium severity",
                _admitted(
                    severity="medium",
                    has_specific_violation=False,
                    named_mandatory_rule="api-compat",
                ),
                strict,
                "block",
                True,
                "admitted-blocker",
            ),
            (
                "merge-focused named rule without required contract stays advisory",
                _admitted(
                    severity="medium",
                    has_specific_violation=False,
                    named_mandatory_rule="style-rename",
                ),
                merge,
                "advisory",
                False,
                "not-admitted-advisory",
            ),
        )
        for label, candidate, policy, disposition, blocking, reason in cases:
            with self.subTest(label=label):
                decision = evaluate_blocker_admission(candidate, policy)
                self.assertEqual(decision.disposition, disposition)
                self.assertEqual(decision.effective_blocking, blocking)
                self.assertEqual(decision.admission_reason, reason)
                self.assertEqual(
                    decision.needs_human, disposition == "human-adjudication"
                )
                self.assertEqual(
                    decision.withholds_automatic_approval,
                    blocking or decision.needs_human,
                )
                if decision.needs_human:
                    self.assertFalse(decision.effective_blocking)
                validate_public_document(decision.to_dict(), "blocker-admission")

        advisory_block = evaluate_blocker_admission(_admitted(), advisory)
        self.assertTrue(advisory_block.effective_blocking)
        self.assertFalse(advisory_block.automatic_github_review_events)
        self.assertFalse(advisory_block.inline_advisory_threads)

    def test_evaluator_rejects_invalid_inputs(self):
        policy = ReviewConvergencePolicy(mode="merge-focused")
        with self.assertRaisesRegex(ReviewInputError, "boolean"):
            BlockerCandidate(has_specific_violation=1)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ReviewInputError, "invalid"):
            evaluate_blocker_admission("candidate", policy)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ReviewInputError, "invalid"):
            evaluate_blocker_admission(_admitted(), "policy")  # type: ignore[arg-type]
        with self.assertRaisesRegex(ReviewInputError, "non-empty"):
            BlockerCandidate(severity=" ")
        with self.assertRaisesRegex(ReviewInputError, "control character"):
            BlockerCandidate(severity="high\n")
        with self.assertRaisesRegex(ReviewInputError, "named_mandatory_rule"):
            BlockerCandidate(named_mandatory_rule=" ")
        with self.assertRaisesRegex(ReviewInputError, "late_reason"):
            BlockerCandidate(late_reason="maybe")
        with self.assertRaisesRegex(ReviewInputError, "enforcement"):
            ReviewConvergencePolicy(enforcement="silent")
        with self.assertRaisesRegex(ReviewInputError, "block disposition"):
            BlockerAdmissionDecision(
                mode="merge-focused",
                proposed_blocking=True,
                effective_blocking=True,
                needs_human=False,
                withholds_automatic_approval=True,
                automatic_github_review_events=True,
                inline_advisory_threads=False,
                disposition="advisory",
                severity_reason="material-high-critical",
                evidence_reason="validated-evidence-and-failure-condition",
                scope_reason="pr-attributed",
                admission_reason="admitted-blocker",
            )

        with self.assertRaisesRegex(ReviewInputError, "requires effective_blocking"):
            BlockerAdmissionDecision(
                mode="merge-focused",
                proposed_blocking=False,
                effective_blocking=False,
                needs_human=False,
                withholds_automatic_approval=False,
                automatic_github_review_events=True,
                inline_advisory_threads=False,
                disposition="block",
                severity_reason="material-high-critical",
                evidence_reason="validated-evidence-and-failure-condition",
                scope_reason="pr-attributed",
                admission_reason="admitted-blocker",
            )
        with self.assertRaisesRegex(ReviewInputError, "proven blocker"):
            BlockerAdmissionDecision(
                mode="merge-focused",
                proposed_blocking=True,
                effective_blocking=True,
                needs_human=True,
                withholds_automatic_approval=True,
                automatic_github_review_events=True,
                inline_advisory_threads=False,
                disposition="block",
                severity_reason="material-high-critical",
                evidence_reason="insufficient-evidence",
                scope_reason="pr-attributed",
                admission_reason="weak-high-impact-needs-human",
            )

    def test_classification_needs_human_precedes_contradictory_evidence(self):
        policy = ReviewConvergencePolicy(mode="merge-focused")
        candidate = _admitted(
            needs_human=True,
            has_contradictory_evidence=True,
        )
        decision = evaluate_blocker_admission(candidate, policy)
        self.assertEqual(decision.disposition, "human-adjudication")
        self.assertTrue(decision.needs_human)
        self.assertEqual(decision.admission_reason, "classification-needs-human")


class RoundAdmissionDecisionTableTests(unittest.TestCase):
    def test_legacy_does_not_cap_rounds(self):
        policy = ReviewConvergencePolicy()
        decision = evaluate_round_admission(
            RoundSessionState(
                completed_initial_reviews=1,
                completed_verification_rounds=20,
                independently_approval_eligible=True,
                coverage_complete=True,
                latest_head_reviewed=True,
            ),
            policy,
        )
        self.assertTrue(decision.admit)
        self.assertFalse(decision.handoff)
        self.assertTrue(decision.may_emit_approve)
        self.assertFalse(decision.cap_creates_approval)

    def test_decision_table_for_round_budget_and_handoff(self):
        policy = ReviewConvergencePolicy(mode="merge-focused")
        cases = (
            (
                "first review is initial",
                RoundSessionState(),
                True,
                "initial",
                False,
                None,
                False,
            ),
            (
                "verification after initial",
                RoundSessionState(completed_initial_reviews=1),
                True,
                "verification",
                False,
                None,
                False,
            ),
            (
                "second verification is still admitted",
                RoundSessionState(
                    completed_initial_reviews=1, completed_verification_rounds=1
                ),
                True,
                "verification",
                False,
                None,
                False,
            ),
            (
                "cap hands off without minting approval",
                RoundSessionState(
                    completed_initial_reviews=1,
                    completed_verification_rounds=2,
                    coverage_complete=True,
                    latest_head_reviewed=True,
                ),
                False,
                "none",
                True,
                "round-budget-exhausted",
                False,
            ),
            (
                "eligible last-round result may approve",
                RoundSessionState(
                    completed_initial_reviews=1,
                    completed_verification_rounds=2,
                    independently_approval_eligible=True,
                    coverage_complete=True,
                    latest_head_reviewed=True,
                ),
                False,
                "none",
                True,
                "round-budget-exhausted",
                True,
            ),
            (
                "incomplete coverage at cap cannot approve",
                RoundSessionState(
                    completed_initial_reviews=1,
                    completed_verification_rounds=2,
                    independently_approval_eligible=True,
                    latest_head_reviewed=True,
                ),
                False,
                "none",
                True,
                "incomplete-coverage",
                False,
            ),
            (
                "later unreviewed head stays unverified",
                RoundSessionState(
                    completed_initial_reviews=1,
                    completed_verification_rounds=2,
                    independently_approval_eligible=True,
                    coverage_complete=True,
                ),
                False,
                "none",
                True,
                "unreviewed-head",
                False,
            ),
            (
                "same-head duplicate does not count",
                RoundSessionState(same_head_duplicate=True),
                False,
                "none",
                False,
                None,
                False,
            ),
            (
                "publication recovery does not count",
                RoundSessionState(publication_recovery=True),
                False,
                "none",
                False,
                None,
                False,
            ),
            (
                "retry does not count as a round",
                RoundSessionState(transport_or_structural_retry=True),
                False,
                "none",
                False,
                None,
                False,
            ),
            (
                "eligible duplicate cannot approve",
                RoundSessionState(
                    same_head_duplicate=True,
                    independently_approval_eligible=True,
                    coverage_complete=True,
                    latest_head_reviewed=True,
                ),
                False,
                "none",
                False,
                None,
                False,
            ),
            (
                "duplicate with no-progress still hands off",
                RoundSessionState(
                    same_head_duplicate=True,
                    no_progress=True,
                    independently_approval_eligible=True,
                    coverage_complete=True,
                    latest_head_reviewed=True,
                ),
                False,
                "none",
                True,
                "no-progress",
                False,
            ),
            (
                "recovery with exhausted attempts still hands off",
                RoundSessionState(
                    publication_recovery=True,
                    failed_attempts=6,
                    independently_approval_eligible=True,
                    coverage_complete=True,
                    latest_head_reviewed=True,
                ),
                False,
                "none",
                True,
                "failed-attempt-budget-exhausted",
                False,
            ),
            (
                "retry with no-progress still hands off",
                RoundSessionState(
                    transport_or_structural_retry=True,
                    no_progress=True,
                    independently_approval_eligible=True,
                    coverage_complete=True,
                    latest_head_reviewed=True,
                ),
                False,
                "none",
                True,
                "no-progress",
                False,
            ),
            (
                "no-progress hands off early",
                RoundSessionState(completed_initial_reviews=1, no_progress=True),
                False,
                "none",
                True,
                "no-progress",
                False,
            ),
            (
                "failed attempts are bounded separately",
                RoundSessionState(failed_attempts=6),
                False,
                "none",
                True,
                "failed-attempt-budget-exhausted",
                False,
            ),
            (
                "paused in-flight reservation hands off",
                RoundSessionState(paused=True, completed_initial_reviews=1),
                False,
                "none",
                True,
                "paused",
                False,
            ),
        )
        for label, state, admit, kind, handoff, reason, may_approve in cases:
            with self.subTest(label=label):
                decision = evaluate_round_admission(state, policy)
                self.assertEqual(decision.admit, admit)
                self.assertEqual(decision.round_kind, kind)
                self.assertEqual(decision.handoff, handoff)
                self.assertEqual(decision.handoff_reason, reason)
                self.assertEqual(decision.may_emit_approve, may_approve)
                self.assertFalse(decision.cap_creates_approval)
                self.assertEqual(decision.count_as_completed_round, admit)
                validate_public_document(decision.to_dict(), "review-round-decision")
        last_round = evaluate_round_admission(
            RoundSessionState(
                completed_initial_reviews=1,
                completed_verification_rounds=2,
                independently_approval_eligible=True,
                coverage_complete=True,
                latest_head_reviewed=True,
            ),
            policy,
        )
        self.assertTrue(last_round.handoff)
        self.assertEqual(last_round.handoff_reason, "round-budget-exhausted")
        self.assertTrue(last_round.may_emit_approve)
        self.assertFalse(last_round.cap_creates_approval)

    def test_continuation_admits_one_extra_verification_round(self):
        policy = ReviewConvergencePolicy(mode="merge-focused")
        exhausted = RoundSessionState(
            completed_initial_reviews=1,
            completed_verification_rounds=2,
            coverage_complete=True,
            latest_head_reviewed=True,
        )
        refused = evaluate_round_admission(exhausted, policy)
        self.assertFalse(refused.admit)
        continued = evaluate_round_admission(exhausted, policy, continuation_rounds=1)
        self.assertTrue(continued.admit)
        self.assertEqual(continued.round_kind, "verification")
        self.assertTrue(continued.count_as_completed_round)
        with self.assertRaisesRegex(ReviewInputError, "continuation_rounds"):
            evaluate_round_admission(exhausted, policy, continuation_rounds=2)

    def test_continuation_does_not_admit_an_unreviewed_head(self):
        policy = ReviewConvergencePolicy(mode="merge-focused")
        decision = evaluate_round_admission(
            RoundSessionState(
                completed_initial_reviews=1,
                completed_verification_rounds=2,
                latest_head_reviewed=False,
            ),
            policy,
            continuation_rounds=1,
        )
        self.assertFalse(decision.admit)
        self.assertEqual(decision.handoff_reason, "unreviewed-head")

    def test_continuation_does_not_admit_incomplete_coverage(self):
        policy = ReviewConvergencePolicy(mode="merge-focused")
        decision = evaluate_round_admission(
            RoundSessionState(
                completed_initial_reviews=1,
                completed_verification_rounds=2,
                coverage_complete=False,
                independently_approval_eligible=True,
                latest_head_reviewed=True,
            ),
            policy,
            continuation_rounds=1,
        )
        self.assertFalse(decision.admit)
        self.assertEqual(decision.handoff_reason, "incomplete-coverage")
        self.assertFalse(decision.may_emit_approve)

    def test_detect_no_progress_repeats_and_oscillation(self):
        self.assertFalse(
            detect_no_progress(previous_blocking=("a",), current_blocking=())
        )
        self.assertTrue(
            detect_no_progress(previous_blocking=("a",), current_blocking=("a",))
        )
        self.assertTrue(
            detect_no_progress(
                earlier_blocking=("a",),
                previous_blocking=("b",),
                current_blocking=("a",),
            )
        )
        self.assertFalse(
            detect_no_progress(previous_blocking=("a",), current_blocking=("b",))
        )
        with self.assertRaisesRegex(ReviewInputError, "blocking identity"):
            detect_no_progress(current_blocking=("a", 1))  # type: ignore[arg-type]

    def test_round_invariants_reject_cap_created_approval(self):
        with self.assertRaisesRegex(ReviewInputError, "must not create approval"):
            RoundAdmissionDecision(
                mode="merge-focused",
                admit=False,
                count_as_completed_round=False,
                round_kind="none",
                remaining_initial_reviews=0,
                remaining_verification_rounds=0,
                handoff=True,
                handoff_reason="round-budget-exhausted",
                may_emit_approve=False,
                cap_creates_approval=True,
            )
        with self.assertRaisesRegex(ReviewInputError, "round kind"):
            RoundAdmissionDecision(
                mode="merge-focused",
                admit=True,
                count_as_completed_round=True,
                round_kind="none",
                remaining_initial_reviews=1,
                remaining_verification_rounds=2,
                handoff=False,
                handoff_reason=None,
                may_emit_approve=False,
            )
        with self.assertRaisesRegex(ReviewInputError, "completed round"):
            RoundAdmissionDecision(
                mode="merge-focused",
                admit=False,
                count_as_completed_round=True,
                round_kind="none",
                remaining_initial_reviews=0,
                remaining_verification_rounds=0,
                handoff=True,
                handoff_reason="round-budget-exhausted",
                may_emit_approve=False,
            )
        with self.assertRaisesRegex(ReviewInputError, "invalid"):
            evaluate_round_admission("state", ReviewConvergencePolicy())  # type: ignore[arg-type]
        with self.assertRaisesRegex(ReviewInputError, "invalid"):
            evaluate_round_admission(RoundSessionState(), "policy")  # type: ignore[arg-type]
        with self.assertRaisesRegex(ReviewInputError, "must be an object"):
            policy_from_mapping("legacy")  # type: ignore[arg-type]
        with self.assertRaisesRegex(ReviewInputError, "schema_version"):
            policy_from_mapping({"schema_version": "2.0", "mode": "legacy"})


class DoctorPlanDisplayTests(unittest.TestCase):
    def test_doctor_and_plan_display_legacy_policy_by_default(self):
        doctor = run_doctor()
        check = next(
            item for item in doctor["checks"] if item["name"] == "review-convergence"
        )
        self.assertEqual(check["status"], "pass")
        self.assertIn("mode=legacy", check["detail"])
        self.assertEqual(doctor["review_convergence"]["mode"], "legacy")
        self.assertEqual(doctor["review_convergence"]["enforcement"], "display-only")
        plan = build_plan(diff=DIFF)
        self.assertEqual(plan["review_convergence"]["mode"], "legacy")
        self.assertFalse(plan["operations"]["publication"])
        rendered = render_diagnostic(plan)
        self.assertIn("review_convergence: mode=legacy", rendered)

    def test_doctor_reports_opt_in_mode_and_invalid_mode(self):
        doctor = run_doctor(review_mode="merge-focused")
        self.assertEqual(doctor["review_convergence"]["mode"], "merge-focused")
        self.assertEqual(doctor["review_convergence"]["enforcement"], "publication")
        self.assertFalse(doctor["review_convergence"]["inline_advisory_threads"])
        invalid = run_doctor(review_mode="soft")
        check = next(
            item for item in invalid["checks"] if item["name"] == "review-convergence"
        )
        self.assertEqual(check["status"], "action")
        self.assertEqual(invalid["status"], "action")
        self.assertNotIn("review_convergence", invalid)
        with patch.dict("os.environ", {REVIEW_MODE_ENV: "advisory"}):
            from_env = run_doctor()
        self.assertEqual(from_env["review_convergence"]["mode"], "advisory")
        with self.assertRaisesRegex(ReviewInputError, "unsupported"):
            build_plan(review_mode="soft")

    def test_public_exports_are_importable(self):
        import review_sensei

        self.assertIs(review_sensei.BlockerAdmissionDecision, BlockerAdmissionDecision)
        self.assertIs(review_sensei.admit_review_result, admit_review_result)
        policy = resolve_review_convergence_policy(mode="strict")
        self.assertEqual(policy.mode, "strict")
        self.assertEqual(policy.enforcement, "publication")


class FindingAdmissionTests(unittest.TestCase):
    def test_left_side_comment_is_not_attributed_from_new_file_lines(self):
        comment = ReviewComment(
            path="src/app.py",
            line=2,
            body="finding",
            side="LEFT",
            blocking=True,
            severity="high",
            defect_kind="authz-failure",
            fix_effort="small",
        )
        changed = {"src/app.py": frozenset({2})}
        deleted = {"src/app.py": frozenset({2})}
        self.assertFalse(comment_targets_pr_change(comment, changed_lines=changed))
        self.assertTrue(comment_targets_pr_change(comment, deleted_lines=deleted))
        right = ReviewComment(
            path="src/app.py",
            line=2,
            body="finding",
            side="RIGHT",
        )
        self.assertTrue(comment_targets_pr_change(right, changed_lines=changed))
        file_comment = ReviewComment(
            path="src/app.py",
            line=None,
            body="finding",
            side="FILE",
        )
        self.assertFalse(
            comment_targets_pr_change(
                file_comment, changed_lines=changed, deleted_lines=deleted
            )
        )
        policy = ReviewConvergencePolicy(
            mode="merge-focused", enforcement="publication"
        )
        admitted = admit_review_result(
            ReviewResult(summary="Summary.", comments=(comment,), provider="fixture"),
            policy,
            changed_lines=changed,
        )
        self.assertFalse(admitted.comments[0].effective_blocking)
        self.assertTrue(admitted.comments[0].needs_human)

    def test_legacy_result_is_not_rewritten(self):
        comment = ReviewComment(
            path="src/app.py", line=2, body="finding", blocking=True, severity="low"
        )
        result = ReviewResult(
            summary="Summary.", comments=(comment,), provider="fixture"
        )
        admitted = admit_review_result(result, ReviewConvergencePolicy())
        self.assertIs(admitted, result)
        self.assertTrue(admitted.comments[0].blocks_approval)
        self.assertIsNone(admitted.comments[0].effective_blocking)

    def test_operator_mode_without_explicit_enforcement_still_admits(self):
        comment = ReviewComment(
            path="src/app.py",
            line=2,
            body="finding",
            blocking=True,
            severity="medium",
        )
        result = ReviewResult(
            summary="Summary.", comments=(comment,), provider="fixture"
        )
        admitted = admit_review_result(
            result, ReviewConvergencePolicy(mode="merge-focused")
        )
        finding = admitted.comments[0]
        self.assertTrue(finding.blocking)
        self.assertFalse(finding.effective_blocking)
        self.assertFalse(finding.blocks_approval)

    def test_persisted_head_bound_disposition_is_applied_during_admission(self):
        comment = ReviewComment(
            path="src/app.py",
            line=2,
            body="finding",
            blocking=True,
            severity="high",
            defect_kind="authz-failure",
            fix_effort="small",
        )
        fingerprint = finding_lifecycle_for_comment(comment).fingerprint
        disposition = FindingDisposition(
            fingerprint=fingerprint,
            action="accept-risk",
            reason="accepted launch exception",
            actor="alice",
            head_sha="a" * 40,
        )
        admitted = admit_review_result(
            ReviewResult(summary="Summary.", comments=(comment,), provider="fixture"),
            ReviewConvergencePolicy(mode="merge-focused"),
            changed_lines={"src/app.py": frozenset({2})},
            authorized_dispositions=(disposition,),
            current_head_sha="a" * 40,
        )
        self.assertFalse(admitted.comments[0].effective_blocking)
        self.assertFalse(admitted.comments[0].needs_human)

        stale = admit_review_result(
            ReviewResult(summary="Summary.", comments=(comment,), provider="fixture"),
            ReviewConvergencePolicy(mode="merge-focused"),
            changed_lines={"src/app.py": frozenset({2})},
            authorized_dispositions=(disposition,),
            current_head_sha="b" * 40,
        )
        self.assertFalse(stale.comments[0].effective_blocking)
        self.assertTrue(stale.comments[0].needs_human)

    def test_merge_focused_demotes_model_blocker_without_evidence(self):
        comment = ReviewComment(
            path="src/app.py",
            line=2,
            body="finding",
            blocking=True,
            severity="medium",
        )
        result = ReviewResult(
            summary="Summary.", comments=(comment,), provider="fixture"
        )
        policy = ReviewConvergencePolicy(
            mode="merge-focused", enforcement="publication"
        )
        admitted = admit_review_result(result, policy)
        finding = admitted.comments[0]
        self.assertTrue(finding.blocking)
        self.assertFalse(finding.effective_blocking)
        self.assertFalse(finding.blocks_approval)
        self.assertFalse(finding.needs_human)

    def test_merge_focused_high_impact_without_evidence_needs_human(self):
        comment = ReviewComment(
            path="src/app.py", line=2, body="finding", blocking=True, severity="high"
        )
        result = ReviewResult(
            summary="Summary.", comments=(comment,), provider="fixture"
        )
        policy = ReviewConvergencePolicy(
            mode="merge-focused", enforcement="publication"
        )
        admitted = admit_review_result(result, policy)
        finding = admitted.comments[0]
        self.assertTrue(finding.blocking)
        self.assertFalse(finding.effective_blocking)
        self.assertTrue(finding.needs_human)

    def test_verified_locations_alone_do_not_promote_proposed_non_blocking(self):
        comment = ReviewComment(
            path="src/app.py",
            line=2,
            body="finding",
            blocking=False,
            severity="high",
            defect_kind="authz-failure",
            fix_effort="small",
        )
        result = ReviewResult(
            summary="Summary.", comments=(comment,), provider="fixture"
        )
        policy = ReviewConvergencePolicy(
            mode="merge-focused", enforcement="publication"
        )
        facts = derive_blocker_candidate(
            comment,
            on_changed_path=True,
            evidence_locations_validated=True,
        )
        admitted = admit_review_result(result, policy, candidates=(facts,))
        finding = admitted.comments[0]
        self.assertFalse(finding.blocking)
        self.assertFalse(finding.effective_blocking)
        self.assertTrue(finding.needs_human)
        self.assertFalse(finding.blocks_approval)

    def test_explicit_facts_can_admit_despite_proposed_non_blocking(self):
        comment = ReviewComment(
            path="src/app.py",
            line=2,
            body="finding",
            blocking=False,
            severity="high",
            defect_kind="authz-failure",
            fix_effort="small",
        )
        result = ReviewResult(
            summary="Summary.", comments=(comment,), provider="fixture"
        )
        policy = ReviewConvergencePolicy(
            mode="merge-focused", enforcement="publication"
        )
        facts = derive_blocker_candidate(
            comment,
            on_changed_path=True,
            evidence_locations_validated=True,
            has_failure_condition=True,
            has_actionable_remedy=True,
            has_specific_violation=True,
        )
        admitted = admit_review_result(result, policy, candidates=(facts,))
        finding = admitted.comments[0]
        self.assertFalse(finding.blocking)
        self.assertTrue(finding.effective_blocking)
        self.assertTrue(finding.blocks_approval)

    def test_derivation_does_not_treat_defect_kind_as_named_mandatory_rule(self):
        comment = ReviewComment(
            path="src/app.py",
            line=2,
            body="finding",
            blocking=False,
            severity="medium",
            defect_kind="authz-failure",
            fix_effort="small",
        )
        facts = derive_blocker_candidate(
            comment,
            on_changed_path=True,
            evidence_locations_validated=True,
            has_failure_condition=True,
            has_actionable_remedy=True,
        )
        self.assertFalse(facts.has_specific_violation)
        self.assertFalse(facts.has_required_contract)
        self.assertIsNone(facts.named_mandatory_rule)
        policy = ReviewConvergencePolicy(mode="strict", enforcement="publication")
        admitted = admit_review_result(
            ReviewResult(summary="Summary.", comments=(comment,), provider="fixture"),
            policy,
            candidates=(facts,),
        )
        self.assertFalse(admitted.comments[0].effective_blocking)
        self.assertFalse(admitted.comments[0].needs_human)

    def test_required_contract_kinds_are_not_named_mandatory_rules(self):
        comment = ReviewComment(
            path="src/app.py",
            line=2,
            body="finding",
            blocking=True,
            severity="medium",
            defect_kind="api-contract",
            fix_effort="small",
        )
        facts = derive_blocker_candidate(
            comment,
            on_changed_path=True,
            evidence_locations_validated=True,
            has_failure_condition=True,
            has_actionable_remedy=True,
        )
        self.assertFalse(facts.has_specific_violation)
        self.assertFalse(facts.has_required_contract)
        self.assertIsNone(facts.named_mandatory_rule)
        self.assertFalse(facts.is_preference_or_optional)
        self.assertIn("api-contract", REQUIRED_CONTRACT_KINDS)
        policy = ReviewConvergencePolicy(mode="strict", enforcement="publication")
        admitted = admit_review_result(
            ReviewResult(summary="Summary.", comments=(comment,), provider="fixture"),
            policy,
            candidates=(facts,),
        )
        self.assertFalse(admitted.comments[0].effective_blocking)

    def test_explicit_required_contract_opts_into_contract_gate(self):
        comment = ReviewComment(
            path="src/app.py",
            line=2,
            body="finding",
            blocking=False,
            severity="high",
            defect_kind="api-contract",
            fix_effort="small",
        )
        facts = derive_blocker_candidate(
            comment,
            on_changed_path=True,
            evidence_locations_validated=True,
            has_failure_condition=True,
            has_actionable_remedy=True,
            has_required_contract=True,
        )
        self.assertTrue(facts.has_required_contract)
        self.assertFalse(facts.has_specific_violation)
        self.assertIsNone(facts.named_mandatory_rule)
        policy = ReviewConvergencePolicy(
            mode="merge-focused", enforcement="publication"
        )
        admitted = admit_review_result(
            ReviewResult(summary="Summary.", comments=(comment,), provider="fixture"),
            policy,
            candidates=(facts,),
        )
        self.assertTrue(admitted.comments[0].effective_blocking)

    def test_preference_categories_are_the_only_preference_signal(self):
        comment = ReviewComment(
            path="src/app.py",
            line=2,
            body="finding",
            blocking=False,
            severity="high",
            category="style",
            defect_kind="api-contract",
            fix_effort="small",
        )
        self.assertIn("style", PREFERENCE_CATEGORIES)
        facts = derive_blocker_candidate(
            comment,
            on_changed_path=True,
            evidence_locations_validated=True,
            has_failure_condition=True,
            has_actionable_remedy=True,
            has_required_contract=True,
        )
        self.assertTrue(facts.is_preference_or_optional)
        policy = ReviewConvergencePolicy(
            mode="merge-focused", enforcement="publication"
        )
        admitted = admit_review_result(
            ReviewResult(summary="Summary.", comments=(comment,), provider="fixture"),
            policy,
            candidates=(facts,),
        )
        self.assertFalse(admitted.comments[0].effective_blocking)

        renamed = ReviewComment(
            path="src/app.py",
            line=2,
            body="finding",
            blocking=False,
            severity="high",
            category="preference-lens",
            defect_kind="api-contract",
            fix_effort="small",
        )
        renamed_facts = derive_blocker_candidate(
            renamed,
            on_changed_path=True,
            evidence_locations_validated=True,
            has_failure_condition=True,
            has_actionable_remedy=True,
            has_required_contract=True,
        )
        self.assertFalse(renamed_facts.is_preference_or_optional)

    def test_explicit_named_mandatory_rule_opts_into_strict_path(self):
        comment = ReviewComment(
            path="src/app.py",
            line=2,
            body="finding",
            blocking=False,
            severity="medium",
            defect_kind="authz-failure",
            fix_effort="small",
        )
        facts = derive_blocker_candidate(
            comment,
            on_changed_path=True,
            evidence_locations_validated=True,
            has_failure_condition=True,
            has_actionable_remedy=True,
            named_mandatory_rule="authz-must-deny",
        )
        self.assertEqual(facts.named_mandatory_rule, "authz-must-deny")
        policy = ReviewConvergencePolicy(mode="strict", enforcement="publication")
        admitted = admit_review_result(
            ReviewResult(summary="Summary.", comments=(comment,), provider="fixture"),
            policy,
            candidates=(facts,),
        )
        self.assertTrue(admitted.comments[0].effective_blocking)
        self.assertFalse(admitted.comments[0].needs_human)

    def test_admitted_result_omits_runtime_fields_from_public_document(self):
        comment = ReviewComment(
            path="src/app.py",
            line=2,
            body="finding",
            blocking=False,
            severity="high",
            defect_kind="authz-failure",
            fix_effort="small",
        )
        result = ReviewResult(
            summary="Summary.", comments=(comment,), provider="fixture"
        )
        policy = ReviewConvergencePolicy(
            mode="merge-focused", enforcement="publication"
        )
        facts = derive_blocker_candidate(
            comment,
            on_changed_path=True,
            evidence_locations_validated=True,
            has_failure_condition=True,
            has_actionable_remedy=True,
            has_specific_violation=True,
        )
        admitted = admit_review_result(result, policy, candidates=(facts,))
        payload = admitted.to_dict()
        validate_public_document(payload, "review-result")
        serialized = payload["comments"][0]
        self.assertIsInstance(serialized, dict)
        self.assertNotIn("effective_blocking", serialized)
        self.assertNotIn("needs_human", serialized)
        restored = ReviewResult.from_dict(payload)
        self.assertIsNone(restored.comments[0].effective_blocking)
        self.assertFalse(restored.comments[0].needs_human)
        self.assertFalse(restored.comments[0].blocking)

    def test_free_form_defect_kind_is_not_a_specific_violation(self):
        comment = ReviewComment(
            path="src/app.py",
            line=2,
            body="finding",
            blocking=False,
            severity="high",
            defect_kind="authz-failure",
            fix_effort="small",
        )
        facts = derive_blocker_candidate(
            comment,
            on_changed_path=True,
            evidence_locations_validated=True,
            has_failure_condition=True,
            has_actionable_remedy=True,
        )
        self.assertFalse(facts.has_specific_violation)
        self.assertFalse(facts.has_required_contract)
        self.assertIsNone(facts.named_mandatory_rule)
        policy = ReviewConvergencePolicy(
            mode="merge-focused", enforcement="publication"
        )
        admitted = admit_review_result(
            ReviewResult(summary="Summary.", comments=(comment,), provider="fixture"),
            policy,
            candidates=(facts,),
        )
        self.assertFalse(admitted.comments[0].effective_blocking)

    def test_identity_mismatch_fails_closed(self):
        comment = ReviewComment(path="src/app.py", line=2, body="finding")
        other = ReviewComment(path="src/other.py", line=2, body="finding")
        facts = derive_blocker_candidate(comment, on_changed_path=True)
        policy = ReviewConvergencePolicy(
            mode="merge-focused", enforcement="publication"
        )
        with self.assertRaisesRegex(ReviewInputError, "must match review comment"):
            admit_review_result(
                ReviewResult(summary="Summary.", comments=(other,), provider="fixture"),
                policy,
                candidates=(facts,),
            )

    def test_location_less_candidates_fail_closed(self):
        comment = ReviewComment(path="src/app.py", line=2, body="finding")
        policy = ReviewConvergencePolicy(
            mode="merge-focused", enforcement="publication"
        )
        with self.assertRaisesRegex(ReviewInputError, "bind comment identity"):
            admit_review_result(
                ReviewResult(
                    summary="Summary.", comments=(comment,), provider="fixture"
                ),
                policy,
                candidates=(BlockerCandidate(has_specific_violation=True),),
            )

    def test_partial_identity_without_path_fails_closed(self):
        comment = ReviewComment(path="src/app.py", line=2, body="finding", side="RIGHT")
        policy = ReviewConvergencePolicy(
            mode="merge-focused", enforcement="publication"
        )
        with self.assertRaisesRegex(ReviewInputError, "bind comment identity"):
            admit_review_result(
                ReviewResult(
                    summary="Summary.", comments=(comment,), provider="fixture"
                ),
                policy,
                candidates=(
                    BlockerCandidate(has_specific_violation=True, line=2, side="RIGHT"),
                ),
            )

    def test_misaligned_candidates_fail_closed(self):
        result = ReviewResult(
            summary="Summary.",
            comments=(ReviewComment(path="src/app.py", line=2, body="finding"),),
            provider="fixture",
        )
        policy = ReviewConvergencePolicy(
            mode="merge-focused", enforcement="publication"
        )
        with self.assertRaisesRegex(ReviewInputError, "align"):
            admit_review_result(result, policy, candidates=())


if __name__ == "__main__":
    unittest.main()
