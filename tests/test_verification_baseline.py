from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from review_sensei.baseline import (
    LaterFindingClassification,
    VerificationScope,
    baseline_from_review,
    candidate_from_later_finding,
    classify_later_finding,
    classify_omitted_finding,
    evaluate_baseline_compatibility,
    plan_verification_scope,
    preview_verification_scope,
)
from review_sensei.context import (
    MAX_CACHE_METADATA_ITEMS,
    ReviewContextCacheKey,
    finding_lifecycle_for_comment,
)
from review_sensei.convergence import (
    ReviewConvergencePolicy,
    admit_review_result,
    derive_blocker_candidate,
)
from review_sensei.diagnostics import (
    _verification_scope_from_session,
    build_plan,
    run_doctor,
)
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewComment, ReviewResult
from review_sensei.planning import MAX_RELATED_PATHS, related_paths_for_change
from review_sensei.session import LocalSessionLedger, SessionIdentity

DIFF = """diff --git a/src/app.py b/src/app.py
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
+impact
"""

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
SHA_D = "d" * 40


def _key(**overrides: object) -> ReviewContextCacheKey:
    values: dict[str, object] = {
        "repository": "owner/repo",
        "pull_request": 136,
        "base_sha": SHA_A,
        "head_sha": SHA_B,
        "engine": "fixture",
        "model": "fake-model",
        "profile": "default",
        "stage_digest": DIGEST_A,
        "context_digest": DIGEST_B,
        "learning_digest": DIGEST_C,
    }
    values.update(overrides)
    return ReviewContextCacheKey(**values)  # type: ignore[arg-type]


def _comment(**overrides: object) -> ReviewComment:
    values: dict[str, object] = {
        "path": "src/app.py",
        "line": 2,
        "body": "reachable auth failure",
        "blocking": True,
        "severity": "high",
        "fix_effort": "small",
        "symbol": "run",
        "defect_kind": "authz",
    }
    values.update(overrides)
    return ReviewComment(**values)  # type: ignore[arg-type]


def _result(*comments: ReviewComment, status: str = "complete") -> ReviewResult:
    return ReviewResult(
        summary="baseline review",
        comments=comments,
        provider="fixture",
        review_status=status,
    )


class RelatedPathTests(unittest.TestCase):
    def test_same_directory_siblings_are_related(self) -> None:
        related = related_paths_for_change(("src/app.py", "src/helper.py", "docs/a.md"))
        self.assertEqual(related, ("src/helper.py", "src/app.py"))


class PreviewScopeTests(unittest.TestCase):
    def test_legacy_stays_unscoped(self) -> None:
        scope = preview_verification_scope(
            policy=ReviewConvergencePolicy(),
            completed_initial_reviews=1,
            changed_paths=("src/app.py",),
        )
        self.assertEqual(scope.status, "legacy-unscoped")
        self.assertFalse(scope.late_admission_required)
        self.assertEqual(scope.to_dict()["coverage_mode"], "unscoped")

    def test_operator_without_baseline_is_initial(self) -> None:
        scope = preview_verification_scope(
            policy=ReviewConvergencePolicy(mode="merge-focused"),
            completed_initial_reviews=0,
            session_status="missing",
            changed_paths=("src/app.py", "src/helper.py"),
        )
        self.assertEqual(scope.status, "baseline-required")
        self.assertEqual(scope.round_kind, "initial")
        self.assertIn("src/helper.py", scope.related_paths)

    def test_operator_with_completed_initial_is_verification(self) -> None:
        scope = preview_verification_scope(
            policy=ReviewConvergencePolicy(mode="merge-focused"),
            completed_initial_reviews=1,
            session_status="ok",
            changed_paths=("src/app.py",),
        )
        self.assertEqual(scope.status, "verify")
        self.assertTrue(scope.late_admission_required)

    def test_tampered_ledger_is_untrusted(self) -> None:
        scope = preview_verification_scope(
            policy=ReviewConvergencePolicy(mode="strict"),
            completed_initial_reviews=1,
            session_status="integrity-failed",
        )
        self.assertEqual(scope.status, "ledger-untrusted")
        self.assertFalse(scope.late_admission_required)


class BaselinePlanTests(unittest.TestCase):
    def test_complete_baseline_builds_incremental_plan(self) -> None:
        policy = ReviewConvergencePolicy(mode="merge-focused")
        previous = _key()
        baseline = baseline_from_review(
            _result(_comment()),
            cache_key=previous,
            policy=policy,
            related_paths=("src/helper.py",),
        )
        self.assertTrue(baseline.complete)
        current = _key(head_sha=SHA_C)
        scope = plan_verification_scope(
            policy=policy,
            baseline=baseline,
            current_key=current,
            changed_paths=("src/app.py",),
            related_paths=("src/helper.py",),
        )
        self.assertEqual(scope.status, "verify")
        self.assertIsNotNone(scope.incremental)
        assert scope.incremental is not None
        self.assertEqual(scope.incremental.previous_key, previous)
        self.assertIn("src/app.py", scope.incremental.reviewed_paths)
        self.assertIn("src/helper.py", scope.incremental.related_paths)

    def test_incomplete_review_cannot_establish_baseline(self) -> None:
        policy = ReviewConvergencePolicy(mode="merge-focused")
        baseline = baseline_from_review(
            _result(_comment(), status="partial"),
            cache_key=_key(),
            policy=policy,
        )
        self.assertFalse(baseline.complete)
        scope = plan_verification_scope(
            policy=policy,
            baseline=baseline,
            current_key=_key(head_sha=SHA_C),
            changed_paths=("src/app.py",),
        )
        self.assertEqual(scope.status, "incomplete-baseline")
        self.assertEqual(scope.coverage_mode, "fallback-full")
        self.assertFalse(scope.late_admission_required)

    def test_rebase_invalidates_baseline(self) -> None:
        policy = ReviewConvergencePolicy(mode="merge-focused")
        baseline = baseline_from_review(
            _result(_comment()), cache_key=_key(), policy=policy
        )
        reason = evaluate_baseline_compatibility(
            baseline,
            current_key=_key(base_sha=SHA_D, head_sha=SHA_C),
            policy=policy,
        )
        self.assertEqual(reason, "rebase-or-base-change")
        scope = plan_verification_scope(
            policy=policy,
            baseline=baseline,
            current_key=_key(base_sha=SHA_D, head_sha=SHA_C),
            changed_paths=("src/app.py",),
        )
        self.assertEqual(scope.status, "incompatible")
        self.assertEqual(scope.invalidation_reason, "rebase-or-base-change")

    def test_model_change_invalidates_baseline(self) -> None:
        policy = ReviewConvergencePolicy(mode="merge-focused")
        baseline = baseline_from_review(
            _result(_comment()), cache_key=_key(), policy=policy
        )
        self.assertEqual(
            evaluate_baseline_compatibility(
                baseline,
                current_key=_key(model="other-model", head_sha=SHA_C),
                policy=policy,
            ),
            "model-change",
        )

    def test_policy_change_invalidates_baseline(self) -> None:
        baseline = baseline_from_review(
            _result(_comment()),
            cache_key=_key(),
            policy=ReviewConvergencePolicy(mode="merge-focused"),
        )
        self.assertEqual(
            evaluate_baseline_compatibility(
                baseline,
                current_key=_key(head_sha=SHA_C),
                policy=ReviewConvergencePolicy(mode="strict"),
            ),
            "policy-change",
        )

    def test_related_context_never_truncates_silently(self) -> None:
        policy = ReviewConvergencePolicy(mode="merge-focused")
        with self.assertRaises(ReviewInputError):
            baseline_from_review(
                _result(_comment()),
                cache_key=_key(),
                policy=policy,
                related_paths=tuple(
                    f"src/base-{index}.py" for index in range(MAX_RELATED_PATHS + 1)
                ),
            )
        baseline = baseline_from_review(
            _result(_comment()),
            cache_key=_key(),
            policy=policy,
            related_paths=tuple(
                f"src/base-{index}.py" for index in range(MAX_RELATED_PATHS)
            ),
        )
        with self.assertRaises(ReviewInputError):
            plan_verification_scope(
                policy=policy,
                baseline=baseline,
                current_key=_key(head_sha=SHA_C),
                changed_paths=("src/app.py",),
                related_paths=("src/overflow.py",),
            )

    def test_reviewed_context_never_truncates_silently(self) -> None:
        policy = ReviewConvergencePolicy(mode="merge-focused")
        baseline = baseline_from_review(
            _result(_comment()),
            cache_key=_key(),
            policy=policy,
            reviewed_paths=tuple(
                f"src/reviewed-{index}.py" for index in range(MAX_CACHE_METADATA_ITEMS)
            ),
        )
        with self.assertRaises(ReviewInputError):
            plan_verification_scope(
                policy=policy,
                baseline=baseline,
                current_key=_key(head_sha=SHA_C),
                changed_paths=("src/overflow.py",),
            )

    def test_invalidated_scopes_require_fallback_full_without_late_admission(
        self,
    ) -> None:
        for status in ("incompatible", "incomplete-baseline"):
            with self.assertRaises(ReviewInputError):
                VerificationScope(
                    status=status,
                    round_kind="initial",
                    late_admission_required=False,
                    coverage_mode="incremental",
                )
            with self.assertRaises(ReviewInputError):
                VerificationScope(
                    status=status,
                    round_kind="verification",
                    late_admission_required=True,
                    coverage_mode="fallback-full",
                )
        with self.assertRaises(ReviewInputError):
            VerificationScope(
                status="verify",
                round_kind="verification",
                late_admission_required=True,
                coverage_mode="unscoped",
            )
        with self.assertRaises(ReviewInputError):
            VerificationScope(
                status="legacy-unscoped",
                round_kind="initial",
                late_admission_required=False,
                coverage_mode="unscoped",
            )

    def test_boolean_fields_reject_integer_coercion(self) -> None:
        with self.assertRaises(ReviewInputError):
            LaterFindingClassification(
                fingerprint=DIGEST_A,
                classification="new-on-initial-pass",
                is_late_relative_to_baseline=1,  # type: ignore[arg-type]
                is_duplicate=False,
            )
        with self.assertRaises(ReviewInputError):
            VerificationScope(
                status="baseline-required",
                round_kind="initial",
                late_admission_required=1,  # type: ignore[arg-type]
                coverage_mode="full",
            )

    def test_schema_bound_tracks_cache_metadata_limit(self) -> None:
        scope = VerificationScope(
            status="baseline-required",
            round_kind="initial",
            late_admission_required=False,
            coverage_mode="full",
            reviewed_paths=tuple(
                f"src/file-{index}.py" for index in range(MAX_CACHE_METADATA_ITEMS)
            ),
        )
        self.assertEqual(
            len(scope.to_dict()["reviewed_paths"]), MAX_CACHE_METADATA_ITEMS
        )
        with self.assertRaises(ReviewInputError):
            VerificationScope(
                status="baseline-required",
                round_kind="initial",
                late_admission_required=False,
                coverage_mode="full",
                reviewed_paths=tuple(
                    f"src/file-{index}.py"
                    for index in range(MAX_CACHE_METADATA_ITEMS + 1)
                ),
            )
        with self.assertRaises(ReviewInputError):
            VerificationScope(
                status="baseline-required",
                round_kind="initial",
                late_admission_required=False,
                coverage_mode="full",
                reviewed_paths=("src/duplicate.py",) * (MAX_CACHE_METADATA_ITEMS + 1),
            )


class LaterFindingTests(unittest.TestCase):
    def test_reworded_same_identity_is_duplicate_not_late(self) -> None:
        policy = ReviewConvergencePolicy(mode="merge-focused")
        original = _comment(body="original wording")
        baseline = baseline_from_review(
            _result(original), cache_key=_key(), policy=policy
        )
        scope = plan_verification_scope(
            policy=policy,
            baseline=baseline,
            current_key=_key(head_sha=SHA_C),
            changed_paths=("src/app.py",),
        )
        moved = _comment(body="moved wording", evidence_id="ev-2")
        classification = classify_later_finding(
            moved,
            baseline=baseline,
            scope=scope,
            changed_paths=("src/app.py",),
            on_changed_path=True,
        )
        self.assertEqual(classification.classification, "reworded-or-moved")
        self.assertTrue(classification.is_duplicate)
        self.assertFalse(classification.is_late_relative_to_baseline)
        self.assertEqual(
            classification.to_dict()["lineage_reason"], "reworded-or-moved"
        )

    def test_ambiguous_identity_requires_human_adjudication(self) -> None:
        policy = ReviewConvergencePolicy(mode="merge-focused")
        first = _comment(body="first", evidence_id="ev-1")
        second = _comment(body="second", evidence_id="ev-2")
        baseline = baseline_from_review(
            _result(first, second), cache_key=_key(), policy=policy
        )
        scope = plan_verification_scope(
            policy=policy,
            baseline=baseline,
            current_key=_key(head_sha=SHA_C),
            changed_paths=("src/app.py",),
        )
        classification = classify_later_finding(
            _comment(body="first", evidence_id="ev-1"),
            baseline=baseline,
            scope=scope,
            changed_paths=("src/app.py",),
            on_changed_path=True,
        )
        self.assertEqual(classification.classification, "needs-human")
        self.assertEqual(classification.lineage_reason, "ambiguous-identity")

    def test_omission_is_not_fixed(self) -> None:
        policy = ReviewConvergencePolicy(mode="merge-focused")
        finding = baseline_from_review(
            _result(_comment()), cache_key=_key(), policy=policy
        ).findings[0]
        scope = preview_verification_scope(
            policy=policy,
            completed_initial_reviews=1,
            session_status="ok",
            changed_paths=("src/app.py",),
        )
        omitted = classify_omitted_finding(finding, scope=scope)
        self.assertEqual(omitted.classification, "omitted-uncertain")
        confirmed = classify_omitted_finding(
            finding, scope=scope, evidence_confirmed=True
        )
        self.assertEqual(confirmed.classification, "verified-fixed")
        unread = classify_omitted_finding(finding, scope=scope, path_reviewed=False)
        self.assertEqual(unread.classification, "continuing-concern")

    def test_cross_file_regression_records_causal_parent(self) -> None:
        policy = ReviewConvergencePolicy(mode="merge-focused")
        original = _comment()
        baseline = baseline_from_review(
            _result(original),
            cache_key=_key(),
            policy=policy,
            related_paths=("src/helper.py",),
        )
        scope = plan_verification_scope(
            policy=policy,
            baseline=baseline,
            current_key=_key(head_sha=SHA_C),
            changed_paths=("src/app.py",),
            related_paths=("src/helper.py",),
        )
        regression = _comment(
            path="src/helper.py",
            symbol="help",
            defect_kind="authz",
            body="fix broke helper auth",
        )
        classification = classify_later_finding(
            regression,
            baseline=baseline,
            scope=scope,
            changed_paths=("src/app.py",),
            related_paths=("src/helper.py",),
            on_changed_path=True,
        )
        self.assertEqual(classification.classification, "new-regression")
        self.assertEqual(classification.late_reason, "new-regression")
        self.assertEqual(classification.attribution, "fix-regression")
        self.assertEqual(
            classification.causal_parent,
            finding_lifecycle_for_comment(original).fingerprint,
        )
        self.assertEqual(
            classification.lineage_reason, "fix-introduced-on-related-path"
        )

    def test_distinct_defect_kinds_do_not_collapse(self) -> None:
        policy = ReviewConvergencePolicy(mode="merge-focused")
        baseline = baseline_from_review(
            _result(_comment(defect_kind="authz")),
            cache_key=_key(),
            policy=policy,
        )
        scope = plan_verification_scope(
            policy=policy,
            baseline=baseline,
            current_key=_key(head_sha=SHA_C),
            changed_paths=("src/app.py",),
        )
        other = _comment(defect_kind="data-loss", body="second defect")
        classification = classify_later_finding(
            other,
            baseline=baseline,
            scope=scope,
            changed_paths=("src/app.py",),
            on_changed_path=True,
        )
        self.assertEqual(classification.classification, "new-regression")
        self.assertFalse(classification.is_duplicate)

    def test_unrelated_changed_path_has_no_causal_parent(self) -> None:
        policy = ReviewConvergencePolicy(mode="merge-focused")
        baseline = baseline_from_review(
            _result(_comment(symbol="old", defect_kind="authz", blocking=False)),
            cache_key=_key(),
            policy=policy,
        )
        scope = plan_verification_scope(
            policy=policy,
            baseline=baseline,
            current_key=_key(head_sha=SHA_C),
            changed_paths=("src/app.py",),
        )
        unrelated = _comment(
            symbol="new",
            defect_kind="data-loss",
            body="unrelated changed-path defect",
        )
        classification = classify_later_finding(
            unrelated,
            baseline=baseline,
            scope=scope,
            changed_paths=("src/app.py",),
            on_changed_path=True,
        )
        self.assertEqual(classification.classification, "new-regression")
        self.assertIsNone(classification.causal_parent)
        self.assertEqual(classification.lineage_reason, "none")
        self.assertEqual(classification.attribution, "pr-change")

    def test_optional_on_already_reviewed_code_is_not_a_blocker(self) -> None:
        policy = ReviewConvergencePolicy(mode="merge-focused")
        baseline = baseline_from_review(
            _result(_comment()), cache_key=_key(), policy=policy
        )
        scope = plan_verification_scope(
            policy=policy,
            baseline=baseline,
            current_key=_key(head_sha=SHA_C),
            changed_paths=("src/app.py",),
        )
        polish = _comment(
            symbol="rename",
            defect_kind="naming",
            category="nit",
            severity="low",
            blocking=False,
            body="rename for style",
        )
        classification = classify_later_finding(
            polish,
            baseline=baseline,
            scope=scope,
            changed_paths=("src/app.py",),
            on_changed_path=True,
            is_preference_or_optional=True,
        )
        self.assertEqual(classification.classification, "already-reviewed-optional")
        admitted = admit_review_result(
            _result(polish),
            policy,
            baseline=baseline,
            changed_paths=("src/app.py",),
            current_key=_key(head_sha=SHA_C),
        )
        self.assertFalse(admitted.comments[0].effective_blocking)

    def test_missed_defect_on_reviewed_path_is_late(self) -> None:
        policy = ReviewConvergencePolicy(mode="merge-focused")
        baseline = baseline_from_review(
            _result(_comment()),
            cache_key=_key(),
            policy=policy,
            reviewed_paths=("src/app.py", "src/util.py"),
        )
        scope = plan_verification_scope(
            policy=policy,
            baseline=baseline,
            current_key=_key(head_sha=SHA_C),
            changed_paths=("src/app.py",),
        )
        missed = _comment(
            path="src/util.py",
            symbol="parse",
            defect_kind="injection",
            body="missed injection",
        )
        classification = classify_later_finding(
            missed,
            baseline=baseline,
            scope=scope,
            changed_paths=("src/app.py",),
            related_paths=(),
            on_changed_path=False,
        )
        self.assertEqual(classification.classification, "substantiated-missed-defect")
        self.assertEqual(classification.late_reason, "substantiated-missed-defect")

    def test_admitted_regression_keeps_late_reason(self) -> None:
        policy = ReviewConvergencePolicy(mode="merge-focused")
        original = _comment()
        baseline = baseline_from_review(
            _result(original), cache_key=_key(), policy=policy
        )
        scope = plan_verification_scope(
            policy=policy,
            baseline=baseline,
            current_key=_key(head_sha=SHA_C),
            changed_paths=("src/app.py",),
        )
        regression = _comment(
            symbol="run",
            defect_kind="authz-regression",
            body="fix introduced auth hole",
        )
        classification = classify_later_finding(
            regression,
            baseline=baseline,
            scope=scope,
            changed_paths=("src/app.py",),
            on_changed_path=True,
        )
        candidate = candidate_from_later_finding(
            regression,
            classification,
            on_changed_path=True,
            evidence_locations_validated=True,
            has_failure_condition=True,
            has_actionable_remedy=True,
            has_specific_violation=True,
        )
        self.assertEqual(candidate.late_reason, "new-regression")
        self.assertEqual(candidate.attribution, "fix-regression")
        admitted = admit_review_result(
            _result(regression),
            policy,
            candidates=(candidate,),
        )
        self.assertTrue(admitted.comments[0].effective_blocking)


class DiagnosticVerificationTests(unittest.TestCase):
    def test_unknown_session_status_is_untrusted(self) -> None:
        scope = _verification_scope_from_session(
            policy=ReviewConvergencePolicy(mode="merge-focused"),
            session_record={"status": "future-status"},
        )
        assert scope is not None
        self.assertEqual(scope.status, "ledger-untrusted")

    def test_doctor_and_plan_display_verification_scope(self) -> None:
        doctor = run_doctor(review_mode="merge-focused")
        check = next(
            item for item in doctor["checks"] if item["name"] == "verification-scope"
        )
        self.assertEqual(check["status"], "pass")
        self.assertEqual(doctor["verification"]["status"], "baseline-required")
        rendered = build_plan(diff=DIFF, review_mode="merge-focused")
        self.assertEqual(rendered["verification"]["round_kind"], "initial")
        self.assertIn("src/app.py", rendered["verification"]["changed_paths"])
        self.assertIn("src/helper.py", rendered["verification"]["related_paths"])

    def test_plan_uses_session_initial_count(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            identity = SessionIdentity("owner/repo", 136)
            ledger = LocalSessionLedger(root)
            initialized = ledger.initialize(identity)
            reserved = ledger.reserve(
                identity,
                slot="initial",
                reservation_id="a" * 8,
                expected_generation=initialized.generation,
            )
            ledger.commit(
                identity,
                reservation_id="a" * 8,
                expected_generation=reserved.generation,
            )
            plan = build_plan(
                diff=DIFF,
                repository="owner/repo",
                pull_request=136,
                review_mode="merge-focused",
                session_ledger=root,
            )
            self.assertEqual(plan["verification"]["status"], "verify")
            self.assertTrue(plan["verification"]["late_admission_required"])


class BaselineAdmissionTests(unittest.TestCase):
    def test_baseline_admission_is_fail_closed_without_explicit_candidate_facts(
        self,
    ) -> None:
        policy = ReviewConvergencePolicy(mode="merge-focused")
        comment = _comment()
        baseline = baseline_from_review(
            _result(comment), cache_key=_key(), policy=policy
        )
        result = admit_review_result(
            _result(comment),
            policy,
            baseline=baseline,
            current_key=_key(head_sha=SHA_C),
            changed_paths=("src/app.py",),
        )
        self.assertFalse(result.comments[0].effective_blocking)
        explicit = derive_blocker_candidate(
            comment,
            on_changed_path=True,
            evidence_locations_validated=True,
            has_failure_condition=True,
            has_actionable_remedy=True,
            has_specific_violation=True,
        )
        explicit_result = admit_review_result(
            _result(comment),
            policy,
            baseline=baseline,
            candidates=(explicit,),
        )
        self.assertTrue(explicit_result.comments[0].effective_blocking)

    def test_baseline_admission_includes_changed_deleted_and_related_paths(
        self,
    ) -> None:
        policy = ReviewConvergencePolicy(mode="merge-focused")
        baseline = baseline_from_review(
            _result(_comment()), cache_key=_key(), policy=policy
        )
        comment = _comment(path="src/legacy.py", side="LEFT")
        with patch(
            "review_sensei.baseline.plan_verification_scope",
            wraps=plan_verification_scope,
        ) as planner:
            admit_review_result(
                _result(comment),
                policy,
                baseline=baseline,
                changed_lines={"src/app.py": frozenset({2})},
                deleted_lines={"src/legacy.py": frozenset({2})},
                related_paths=("src/helper.py",),
                current_key=_key(head_sha=SHA_C),
            )
        self.assertEqual(
            planner.call_args.kwargs["changed_paths"],
            ("src/app.py", "src/legacy.py"),
        )
        self.assertEqual(planner.call_args.kwargs["related_paths"], ("src/helper.py",))
