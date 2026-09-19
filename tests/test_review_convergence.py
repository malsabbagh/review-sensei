from __future__ import annotations

import unittest
from unittest.mock import patch

from review_sensei import (
    BlockerAdmissionDecision,
    BlockerCandidate,
    ReviewConvergencePolicy,
    RoundAdmissionDecision,
    RoundSessionState,
    evaluate_blocker_admission,
    evaluate_round_admission,
    resolve_review_convergence_policy,
)
from review_sensei.convergence import (
    REVIEW_MODE_ENV,
    policy_from_mapping,
    resolve_review_mode,
)
from review_sensei.diagnostics import build_plan, render_diagnostic, run_doctor
from review_sensei.errors import ReviewInputError
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

    def test_advisory_disables_github_review_events_and_inline_threads(self):
        policy = ReviewConvergencePolicy(mode="advisory")
        self.assertFalse(policy.automatic_github_review_events)
        self.assertFalse(policy.inline_advisory_threads)
        self.assertEqual(policy.compatibility, "explicit-opt-in")

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
        self.assertIs(BlockerAdmissionDecision, BlockerAdmissionDecision)
        self.assertIs(RoundSessionState, RoundSessionState)
        policy = resolve_review_convergence_policy(mode="strict")
        self.assertEqual(policy.mode, "strict")


if __name__ == "__main__":
    unittest.main()
