from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from review_sensei.baseline import (
    MAX_VERIFICATION_CONCERNS,
    BaselineFinding,
    LaterFindingClassification,
    ReviewBaseline,
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
from review_sensei.coverage import CoverageManifest, FileCoverage
from review_sensei.diagnostics import (
    _normalize_session_record,
    _verification_scope_from_session,
    build_plan,
    run_doctor,
)
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewComment, ReviewResult
from review_sensei.planning import MAX_RELATED_PATHS, related_paths_for_change
from review_sensei.session import LOAD_STATUSES, LocalSessionLedger, SessionIdentity

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

_DEFAULT_COVERAGE = CoverageManifest(
    files=(
        FileCoverage("src/app.py", "reviewed"),
        FileCoverage("src/helper.py", "reviewed"),
    ),
    enumerated_paths=("src/app.py", "src/helper.py"),
)


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


def _result(
    *comments: ReviewComment,
    status: str = "complete",
    coverage: CoverageManifest | None = _DEFAULT_COVERAGE,
) -> ReviewResult:
    return ReviewResult(
        summary="baseline review",
        comments=comments,
        provider="fixture",
        review_status=status,
        coverage=coverage,
    )


class RelatedPathTests(unittest.TestCase):
    def test_same_directory_siblings_are_related(self) -> None:
        related = related_paths_for_change(("src/app.py", "src/helper.py", "docs/a.md"))
        self.assertEqual(related, ("src/helper.py", "src/app.py"))
        self.assertNotIn("docs/b.md", related)

    def test_related_path_overflow_fails_closed(self) -> None:
        changed = tuple(
            f"src/file-{index}.py" for index in range(MAX_RELATED_PATHS + 2)
        )
        with self.assertRaisesRegex(ReviewInputError, "MAX_RELATED_PATHS"):
            related_paths_for_change(changed)


class PreviewScopeTests(unittest.TestCase):
    def test_legacy_stays_unscoped(self) -> None:
        scope = preview_verification_scope(
            policy=ReviewConvergencePolicy(mode="legacy"),
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

    def test_operator_with_completed_initial_still_requires_compatible_baseline(
        self,
    ) -> None:
        scope = preview_verification_scope(
            policy=ReviewConvergencePolicy(mode="merge-focused"),
            completed_initial_reviews=1,
            session_status="ok",
            changed_paths=("src/app.py",),
        )
        self.assertEqual(scope.status, "baseline-required")
        self.assertFalse(scope.late_admission_required)
        self.assertEqual(scope.round_kind, "verification")

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

    def test_incomplete_context_falls_back_without_late_admission(self) -> None:
        policy = ReviewConvergencePolicy(mode="merge-focused")
        baseline = baseline_from_review(
            _result(_comment()), cache_key=_key(), policy=policy
        )
        with patch(
            "review_sensei.baseline.related_paths_for_change",
            side_effect=AssertionError("derived related paths must be skipped"),
        ):
            scope = plan_verification_scope(
                policy=policy,
                baseline=baseline,
                current_key=_key(head_sha=SHA_C),
                changed_paths=("src/app.py", "src/helper.py"),
                context_complete=False,
            )
        self.assertEqual(scope.status, "incomplete-baseline")
        self.assertEqual(scope.invalidation_reason, "coverage-incomplete")
        self.assertEqual(scope.coverage_mode, "fallback-full")
        self.assertIsNone(scope.incremental)
        self.assertFalse(scope.late_admission_required)

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

    def test_legacy_result_with_explicit_reviewed_scope_stays_incomplete(self) -> None:
        policy = ReviewConvergencePolicy(mode="merge-focused")
        baseline = baseline_from_review(
            _result(_comment(), coverage=None),
            cache_key=_key(),
            policy=policy,
            reviewed_paths=("src/app.py",),
        )
        self.assertFalse(baseline.complete)
        self.assertFalse(baseline.coverage_complete)
        scope = plan_verification_scope(
            policy=policy,
            baseline=baseline,
            current_key=_key(head_sha=SHA_C),
            changed_paths=("src/app.py",),
        )
        self.assertEqual(scope.status, "incomplete-baseline")
        self.assertEqual(scope.coverage_mode, "fallback-full")

    def test_baseline_required_scope_reports_changed_paths(self) -> None:
        policy = ReviewConvergencePolicy(mode="merge-focused")
        scope = plan_verification_scope(
            policy=policy,
            baseline=None,
            changed_paths=("src/app.py", "src/helper.py"),
        )
        self.assertEqual(scope.status, "baseline-required")
        self.assertEqual(scope.changed_paths, ("src/app.py", "src/helper.py"))

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

    def test_invalidated_baseline_ignores_related_overflow(self) -> None:
        policy = ReviewConvergencePolicy(mode="merge-focused")
        baseline = baseline_from_review(
            _result(_comment()), cache_key=_key(), policy=policy
        )
        scope = plan_verification_scope(
            policy=policy,
            baseline=baseline,
            current_key=_key(base_sha=SHA_D, head_sha=SHA_C),
            changed_paths=("src/app.py",),
            related_paths=tuple(
                f"src/related-{index}.py" for index in range(MAX_RELATED_PATHS + 1)
            ),
        )
        self.assertEqual(scope.status, "incompatible")
        self.assertEqual(scope.related_paths, ())

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

    def test_coverage_enumerated_paths_never_truncate_reviewed_scope(self) -> None:
        policy = ReviewConvergencePolicy(mode="merge-focused")
        paths = tuple(
            f"src/enumerated-{index}.py"
            for index in range(MAX_CACHE_METADATA_ITEMS + 1)
        )
        coverage = CoverageManifest(
            files=tuple(FileCoverage(path, "reviewed") for path in paths),
            enumerated_paths=paths,
        )
        with self.assertRaisesRegex(ReviewInputError, "MAX_CACHE_METADATA_ITEMS"):
            baseline_from_review(
                _result(_comment(), coverage=coverage),
                cache_key=_key(),
                policy=policy,
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
        with self.assertRaisesRegex(ReviewInputError, "incremental coverage"):
            VerificationScope(
                status="verify",
                round_kind="verification",
                late_admission_required=True,
                coverage_mode="fallback-full",
            )
        with self.assertRaisesRegex(ReviewInputError, "incremental plan"):
            VerificationScope(
                status="verify",
                round_kind="verification",
                late_admission_required=True,
                coverage_mode="incremental",
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
        with self.assertRaises(ReviewInputError):
            VerificationScope(
                status="baseline-required",
                round_kind="initial",
                late_admission_required=False,
                coverage_mode="full",
                existing_concerns=MAX_VERIFICATION_CONCERNS + 1,
            )

    def test_complete_clean_baseline_requires_reviewed_scope(self) -> None:
        policy = ReviewConvergencePolicy(mode="merge-focused")
        baseline = baseline_from_review(
            _result(coverage=None), cache_key=_key(), policy=policy
        )
        self.assertFalse(baseline.complete)

    def test_legacy_result_without_coverage_stays_incomplete(self) -> None:
        policy = ReviewConvergencePolicy(mode="merge-focused")
        baseline = baseline_from_review(
            _result(_comment(), coverage=None), cache_key=_key(), policy=policy
        )
        self.assertFalse(baseline.complete)
        self.assertFalse(baseline.coverage_complete)
        self.assertEqual(
            evaluate_baseline_compatibility(
                baseline,
                current_key=_key(head_sha=SHA_C),
                policy=policy,
            ),
            "coverage-incomplete",
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

    def test_reworded_symbol_uses_original_criterion_for_confirmation(self) -> None:
        policy = ReviewConvergencePolicy(mode="merge-focused")
        original = _comment(body="original wording", symbol="old_run")
        baseline = baseline_from_review(
            _result(original), cache_key=_key(), policy=policy
        )
        moved = _comment(
            body="reworded and moved wording", symbol="new_run", evidence_id="ev-2"
        )
        admitted = admit_review_result(
            _result(moved),
            policy,
            baseline=baseline,
            current_key=_key(head_sha=SHA_C),
            changed_paths=("src/app.py",),
            evidence_confirmed_concerns=(
                finding_lifecycle_for_comment(original).concern,
            ),
        )
        self.assertFalse(admitted.comments[0].effective_blocking)
        self.assertFalse(admitted.comments[0].needs_human)

    def test_path_kind_fallback_requires_a_specific_kind(self) -> None:
        policy = ReviewConvergencePolicy(mode="merge-focused")
        baseline = baseline_from_review(
            _result(_comment(symbol="old_run", defect_kind="unknown")),
            cache_key=_key(),
            policy=policy,
        )
        scope = plan_verification_scope(
            policy=policy,
            baseline=baseline,
            current_key=_key(head_sha=SHA_C),
            changed_paths=("src/app.py",),
        )
        classification = classify_later_finding(
            _comment(symbol="new_run", defect_kind="unknown", body="new concern"),
            baseline=baseline,
            scope=scope,
            changed_paths=("src/app.py",),
            on_changed_path=True,
        )
        self.assertEqual(classification.classification, "needs-human")
        self.assertFalse(classification.is_duplicate)
        self.assertEqual(classification.late_reason, "human-adjudication")

    def test_distinct_same_path_kind_symbols_are_not_collapsed(self) -> None:
        policy = ReviewConvergencePolicy(mode="merge-focused")
        baseline = baseline_from_review(
            _result(
                _comment(symbol="first", body="first", evidence_id="ev-1"),
                _comment(symbol="second", body="second", evidence_id="ev-2"),
            ),
            cache_key=_key(),
            policy=policy,
        )
        scope = plan_verification_scope(
            policy=policy,
            baseline=baseline,
            current_key=_key(head_sha=SHA_C),
            changed_paths=("src/app.py",),
        )
        classification = classify_later_finding(
            _comment(symbol="third", body="third concern", evidence_id="ev-3"),
            baseline=baseline,
            scope=scope,
            changed_paths=("src/app.py",),
            on_changed_path=True,
        )
        self.assertEqual(classification.classification, "needs-human")
        self.assertFalse(classification.is_duplicate)
        self.assertEqual(classification.lineage_reason, "ambiguous-identity")

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
        self.assertEqual(classification.late_reason, "human-adjudication")

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
            finding,
            scope=scope,
            evidence_confirmed=True,
            evidence_criterion=finding.resolution_criterion,
        )
        self.assertEqual(confirmed.classification, "verified-fixed")
        with self.assertRaises(ReviewInputError):
            classify_omitted_finding(
                finding,
                scope=scope,
                evidence_confirmed=True,
                evidence_criterion=DIGEST_A,
            )
        unread = classify_omitted_finding(finding, scope=scope, path_reviewed=False)
        self.assertEqual(unread.classification, "continuing-concern")

    def test_omission_stays_uncertain_on_fallback_scopes(self) -> None:
        policy = ReviewConvergencePolicy(mode="merge-focused")
        complete = baseline_from_review(
            _result(_comment()), cache_key=_key(), policy=policy
        )
        incomplete = baseline_from_review(
            _result(_comment(), status="partial"),
            cache_key=_key(),
            policy=policy,
        )
        scopes = (
            plan_verification_scope(
                policy=policy,
                baseline=complete,
                current_key=_key(base_sha=SHA_D, head_sha=SHA_C),
                changed_paths=("src/app.py",),
            ),
            plan_verification_scope(
                policy=policy,
                baseline=incomplete,
                current_key=_key(head_sha=SHA_C),
                changed_paths=("src/app.py",),
            ),
        )
        finding = complete.findings[0]
        for scope in scopes:
            with self.subTest(status=scope.status):
                self.assertIn(scope.status, {"incompatible", "incomplete-baseline"})
                omitted = classify_omitted_finding(finding, scope=scope)
                self.assertEqual(omitted.classification, "omitted-uncertain")
                self.assertNotEqual(omitted.classification, "verified-fixed")

    def test_criterion_evidence_verifies_identity_without_concern_digest(self) -> None:
        policy = ReviewConvergencePolicy(mode="merge-focused")
        baseline = ReviewBaseline(
            cache_key=_key(),
            policy_digest=policy.digest(),
            complete=True,
            findings=(
                BaselineFinding(
                    fingerprint=DIGEST_A,
                    resolution_criterion=DIGEST_B,
                    concern=None,
                    path="src/app.py",
                    symbol="run",
                    defect_kind="authz",
                ),
            ),
            reviewed_paths=("src/app.py",),
        )
        scope = plan_verification_scope(
            policy=policy,
            baseline=baseline,
            current_key=_key(head_sha=SHA_C),
            changed_paths=("src/app.py",),
        )
        classification = classify_later_finding(
            _comment(),
            baseline=baseline,
            scope=scope,
            evidence_confirmed=True,
            evidence_criterion=DIGEST_B,
        )
        self.assertEqual(classification.classification, "verified-fixed")

    def test_evidence_confirmation_without_a_match_fails_closed(self) -> None:
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
        with self.assertRaisesRegex(ReviewInputError, "matched baseline"):
            classify_later_finding(
                _comment(defect_kind="data-loss", body="unmatched"),
                baseline=baseline,
                scope=scope,
                evidence_confirmed=True,
                evidence_criterion=DIGEST_B,
            )

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

    def test_nonblocking_baseline_does_not_create_causal_regression(self) -> None:
        policy = ReviewConvergencePolicy(mode="merge-focused")
        baseline = baseline_from_review(
            _result(_comment(blocking=False)), cache_key=_key(), policy=policy
        )
        scope = plan_verification_scope(
            policy=policy,
            baseline=baseline,
            current_key=_key(head_sha=SHA_C),
            changed_paths=("src/app.py",),
        )
        classification = classify_later_finding(
            _comment(defect_kind="data-loss", evidence_id="new-evidence"),
            baseline=baseline,
            scope=scope,
            changed_paths=("src/app.py",),
            on_changed_path=True,
        )
        self.assertEqual(classification.classification, "needs-human")
        self.assertEqual(classification.late_reason, "human-adjudication")
        self.assertIsNone(classification.causal_parent)

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
        self.assertEqual(classification.classification, "needs-human")
        self.assertIsNone(classification.causal_parent)
        self.assertEqual(classification.lineage_reason, "none")
        self.assertEqual(classification.attribution, "pr-change")
        self.assertEqual(classification.late_reason, "human-adjudication")
        candidate = candidate_from_later_finding(
            unrelated,
            classification,
            on_changed_path=True,
            evidence_locations_validated=True,
            has_failure_condition=True,
            has_actionable_remedy=True,
            has_specific_violation=True,
        )
        self.assertTrue(candidate.needs_human)
        self.assertFalse(candidate.has_contradictory_evidence)
        admitted = admit_review_result(
            _result(unrelated), policy, candidates=(candidate,)
        )
        self.assertFalse(admitted.comments[0].effective_blocking)
        self.assertTrue(admitted.comments[0].needs_human)

    def test_clean_baseline_path_is_a_missed_defect(self) -> None:
        policy = ReviewConvergencePolicy(mode="merge-focused")
        baseline = baseline_from_review(
            _result(),
            cache_key=_key(),
            policy=policy,
            reviewed_paths=("src/app.py",),
        )
        scope = plan_verification_scope(
            policy=policy,
            baseline=baseline,
            current_key=_key(head_sha=SHA_C),
            changed_paths=("src/other.py",),
        )
        classification = classify_later_finding(
            _comment(path="src/app.py", symbol="new", defect_kind="injection"),
            baseline=baseline,
            scope=scope,
            changed_paths=("src/other.py",),
        )
        self.assertEqual(classification.classification, "substantiated-missed-defect")
        self.assertEqual(classification.late_reason, "substantiated-missed-defect")

    def test_invalidated_scope_requires_human_adjudication(self) -> None:
        policy = ReviewConvergencePolicy(mode="merge-focused")
        baseline = baseline_from_review(
            _result(_comment()), cache_key=_key(), policy=policy
        )
        scope = plan_verification_scope(
            policy=policy,
            baseline=baseline,
            current_key=_key(base_sha=SHA_D, head_sha=SHA_C),
            changed_paths=("src/app.py",),
        )
        classification = classify_later_finding(
            _comment(body="new after rebase"),
            baseline=baseline,
            scope=scope,
            changed_paths=("src/app.py",),
            on_changed_path=True,
        )
        self.assertEqual(classification.classification, "needs-human")
        self.assertEqual(classification.late_reason, "human-adjudication")
        self.assertEqual(classification.lineage_reason, "ambiguous-identity")

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
            self.assertEqual(plan["verification"]["status"], "baseline-required")
            self.assertFalse(plan["verification"]["late_admission_required"])

    def test_missing_session_status_is_baseline_required(self) -> None:
        scope = _verification_scope_from_session(
            policy=ReviewConvergencePolicy(mode="merge-focused"),
            session_record={"completed_initial_reviews": 1},
        )
        assert scope is not None
        self.assertEqual(scope.status, "baseline-required")

    def test_invalid_session_counter_is_untrusted(self) -> None:
        scope = _verification_scope_from_session(
            policy=ReviewConvergencePolicy(mode="merge-focused"),
            session_record={
                "status": "ok",
                "completed_initial_reviews": "1",
            },
        )
        assert scope is not None
        self.assertEqual(scope.status, "ledger-untrusted")

    def test_session_record_status_is_normalized_for_callers(self) -> None:
        self.assertEqual(
            _normalize_session_record({"completed_initial_reviews": 1}),
            {"completed_initial_reviews": 1, "status": "missing"},
        )
        self.assertEqual(
            _normalize_session_record({"status": "future-status"}),
            {"status": "invalid"},
        )
        self.assertNotIn("invalid", LOAD_STATUSES)


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

    def test_explicit_empty_related_paths_are_not_derived(self) -> None:
        policy = ReviewConvergencePolicy(mode="merge-focused")
        baseline = baseline_from_review(
            _result(_comment()), cache_key=_key(), policy=policy
        )
        with patch(
            "review_sensei.baseline.plan_verification_scope",
            wraps=plan_verification_scope,
        ) as planner:
            admit_review_result(
                _result(_comment()),
                policy,
                baseline=baseline,
                current_key=_key(head_sha=SHA_C),
                changed_paths=("src/app.py", "src/helper.py"),
                related_paths=(),
            )
        self.assertEqual(planner.call_args.kwargs["related_paths"], ())

    def test_baseline_admission_related_overflow_fails_closed(self) -> None:
        policy = ReviewConvergencePolicy(mode="merge-focused")
        baseline = baseline_from_review(
            _result(_comment()), cache_key=_key(), policy=policy
        )
        changed = tuple(
            f"src/file-{index}.py" for index in range(MAX_RELATED_PATHS + 1)
        )
        with self.assertRaisesRegex(ReviewInputError, "MAX_RELATED_PATHS"):
            admit_review_result(
                _result(_comment()),
                policy,
                baseline=baseline,
                current_key=_key(head_sha=SHA_C),
                changed_paths=changed,
                related_paths=None,  # type: ignore[arg-type]
            )

    def test_explicit_candidates_precede_baseline_planning(self) -> None:
        policy = ReviewConvergencePolicy(mode="merge-focused")
        comment = _comment()
        baseline = baseline_from_review(
            _result(comment), cache_key=_key(), policy=policy
        )
        candidate = derive_blocker_candidate(
            comment,
            on_changed_path=True,
            evidence_locations_validated=True,
            has_failure_condition=True,
            has_actionable_remedy=True,
            has_specific_violation=True,
        )
        with patch("review_sensei.baseline.plan_verification_scope") as planner:
            admitted = admit_review_result(
                _result(comment),
                policy,
                baseline=baseline,
                candidates=(candidate,),
            )
        planner.assert_not_called()
        self.assertTrue(admitted.comments[0].effective_blocking)

    def test_invalidated_baseline_admission_requires_human_adjudication(self) -> None:
        policy = ReviewConvergencePolicy(mode="merge-focused")
        comment = _comment()
        baseline = baseline_from_review(
            _result(comment), cache_key=_key(), policy=policy
        )
        with patch(
            "review_sensei.baseline.candidate_from_later_finding",
            wraps=candidate_from_later_finding,
        ) as mapper:
            admitted = admit_review_result(
                _result(comment),
                policy,
                baseline=baseline,
                current_key=_key(base_sha=SHA_D, head_sha=SHA_C),
                changed_lines={"src/app.py": frozenset({2})},
            )
        classification = mapper.call_args.args[1]
        self.assertEqual(classification.classification, "needs-human")
        self.assertEqual(classification.late_reason, "human-adjudication")
        self.assertFalse(admitted.comments[0].effective_blocking)
        self.assertTrue(admitted.comments[0].needs_human)
