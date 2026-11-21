"""Unit tests for finding-drift advisory transitions and the ADR 0134 guardrail."""

from __future__ import annotations

import hashlib
import unittest
from dataclasses import replace

from review_sensei.active_finding_window import ConfirmedFixEvidence
from review_sensei.coverage import CoverageManifest, FileCoverage
from review_sensei.finding_drift import (
    DriftInventory,
    FileDriftDescriptor,
    NativeThreadBinding,
    evaluate_drift_round,
    project_drift_review_result,
)
from review_sensei.models import ReviewComment, ReviewResult

BASE_SHA = "a" * 40
HEAD_1 = "1" * 40
HEAD_2 = "2" * 40
HEAD_3 = "3" * 40


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _coverage(*paths: str) -> CoverageManifest:
    return CoverageManifest(
        files=tuple(FileCoverage(p, "reviewed") for p in paths),
        enumerated_paths=tuple(paths),
        enumeration_complete=True,
    )


def _comment(
    path: str = "src/app.py",
    *,
    line: int | None = 12,
    side: str = "RIGHT",
    body: str = "Consider simplifying this helper.",
    blocking: bool = False,
    needs_human: bool = False,
    severity: str = "low",
    symbol: str | None = "verify_token",
    defect_kind: str | None = "missing_validation",
) -> ReviewComment:
    return ReviewComment(
        path=path,
        line=line,
        side=side,
        body=body,
        blocking=blocking,
        needs_human=needs_human,
        effective_blocking=blocking,
        severity=severity,
        symbol=symbol,
        defect_kind=defect_kind,
    )


def _result(
    *comments: ReviewComment,
    paths: tuple[str, ...] = ("src/app.py",),
) -> ReviewResult:
    return ReviewResult(
        summary="Drift transition test review.",
        comments=comments,
        provider="fixture",
        review_status="complete",
        coverage=_coverage(*paths),
    )


def _bound(
    obligation_id: str, *, thread_id: str = "PRRT_thread_1"
) -> NativeThreadBinding:
    _ = obligation_id
    return NativeThreadBinding(
        thread_id=thread_id,
        comment_id=101,
        placement="inline",
        body_reason=None,
        slot_state="active",
        proof_sha256=_sha("proof-" + thread_id),
    )


class FindingDriftAdvisoryTransitionTests(unittest.TestCase):
    def test_475_active_match_promotes_advisory_to_blocking_and_needs_human(self):
        for promo_blocking, promo_human, severity, expected_impact in (
            (True, False, "minor", "high"),
            (True, False, "critical", "critical"),
            (False, True, "low", "low"),
        ):
            with self.subTest(
                blocking=promo_blocking,
                needs_human=promo_human,
                severity=severity,
            ):
                r1 = evaluate_drift_round(
                    _result(_comment()),
                    head_sha=HEAD_1,
                    base_sha=BASE_SHA,
                    max_active_findings=8,
                    round_number=1,
                )
                prior = r1.inventory.obligations[0]
                self.assertEqual(prior.criterion.evidence, "advisory")
                self.assertIsNone(prior.criterion.impact)

                r2 = evaluate_drift_round(
                    _result(
                        _comment(
                            blocking=promo_blocking,
                            needs_human=promo_human,
                            severity=severity,
                        )
                    ),
                    head_sha=HEAD_2,
                    base_sha=BASE_SHA,
                    max_active_findings=8,
                    round_number=2,
                    prior_inventory=r1.inventory,
                )
                ob = r2.inventory.obligations[0]
                self.assertEqual(ob.lifecycle_state, "active-unresolved")
                self.assertEqual(ob.criterion.blocking, promo_blocking)
                self.assertEqual(ob.criterion.needs_human, promo_human)
                self.assertEqual(ob.criterion.evidence, "supported-source")
                self.assertEqual(ob.criterion.impact, expected_impact)
                self.assertTrue(ob.blocks_approval_now)
                self.assertFalse(r2.approved)
                projected = project_drift_review_result(
                    _result(
                        _comment(
                            blocking=promo_blocking,
                            needs_human=promo_human,
                            severity=severity,
                        )
                    ),
                    r2,
                )
                self.assertEqual(len(projected.comments), 1)

    def test_advisory_file_copy_ambiguity_preserves_criterion_and_holds_adr0134(self):
        r1 = evaluate_drift_round(
            _result(_comment()),
            head_sha=HEAD_1,
            base_sha=BASE_SHA,
            max_active_findings=8,
            round_number=1,
        )
        r2 = evaluate_drift_round(
            _result(
                _comment(path="src/app.py"),
                _comment(path="src/copy.py", body="Duplicated copy helper."),
                paths=("src/app.py", "src/copy.py"),
            ),
            head_sha=HEAD_2,
            base_sha=BASE_SHA,
            max_active_findings=8,
            round_number=2,
            prior_inventory=r1.inventory,
            drift_descriptors=(
                FileDriftDescriptor(
                    drift_kind="file_copy",
                    source_path="src/app.py",
                    target_paths=("src/app.py", "src/copy.py"),
                ),
            ),
        )
        self.assertEqual(len(r2.inventory.obligations), 1)
        ob = r2.inventory.obligations[0]
        self.assertEqual(ob.lifecycle_state, "ambiguous-multi-target")
        self.assertEqual(ob.criterion.evidence, "advisory")
        self.assertFalse(ob.criterion.blocking)
        self.assertFalse(ob.criterion.needs_human)
        self.assertIsNone(ob.criterion.impact)
        # ADR 0134 hold remains enforced separately via lifecycle_state.
        self.assertTrue(ob.blocks_approval_now)
        self.assertTrue(ob.effective_blocking_now)
        self.assertTrue(ob.to_baseline_finding().blocking)
        self.assertTrue(ob.synthetic_comment_for_placement().needs_human)
        self.assertFalse(ob.synthetic_comment_for_placement().blocking)
        self.assertFalse(r2.approved)

    def test_advisory_ambiguous_reopen_preserves_severity_or_promotes_only_when_required(
        self,
    ):
        r1 = evaluate_drift_round(
            _result(_comment()),
            head_sha=HEAD_1,
            base_sha=BASE_SHA,
            max_active_findings=8,
            round_number=1,
        )
        prior_ob = r1.inventory.obligations[0]
        r1_inv = DriftInventory(
            obligations=(
                replace(prior_ob, thread_binding=_bound(prior_ob.obligation_id)),
            )
        )
        r2 = evaluate_drift_round(
            _result(),
            head_sha=HEAD_2,
            base_sha=BASE_SHA,
            max_active_findings=8,
            round_number=2,
            prior_inventory=r1_inv,
            confirmed_fixes=(
                ConfirmedFixEvidence(
                    obligation_id=prior_ob.obligation_id,
                    resolution_criterion=prior_ob.criterion.resolution_criterion,
                    evidence_sha256=_sha("fix-1"),
                    head_sha=HEAD_2,
                    verified_paths=("src/app.py",),
                ),
            ),
        )
        self.assertEqual(r2.inventory.obligations[0].lifecycle_state, "verified-fixed")
        self.assertEqual(
            r2.inventory.obligations[0].thread_binding.slot_state, "released"
        )

        # Advisory-only ambiguous reopen keeps original criterion advisory while holding ADR 0134.
        r3_adv = evaluate_drift_round(
            _result(
                _comment(line=12, body="First advisory match."),
                _comment(line=24, body="Second advisory match."),
            ),
            head_sha=HEAD_3,
            base_sha=BASE_SHA,
            max_active_findings=8,
            round_number=3,
            prior_inventory=r2.inventory,
        )
        ob_adv = r3_adv.inventory.obligations[0]
        self.assertEqual(ob_adv.lifecycle_state, "ambiguous-multi-target")
        self.assertEqual(ob_adv.criterion.evidence, "advisory")
        self.assertFalse(ob_adv.criterion.blocking)
        self.assertFalse(ob_adv.criterion.needs_human)
        self.assertIsNone(ob_adv.criterion.impact)
        self.assertFalse(r3_adv.approved)

        # If one reopening comment is explicitly blocking, promote cleanly with ranked impact.
        r3_blk = evaluate_drift_round(
            _result(
                _comment(line=12, body="Advisory match."),
                _comment(
                    line=24,
                    body="Blocking regression.",
                    blocking=True,
                    severity="critical",
                ),
            ),
            head_sha=HEAD_3,
            base_sha=BASE_SHA,
            max_active_findings=8,
            round_number=3,
            prior_inventory=r2.inventory,
        )
        ob_blk = r3_blk.inventory.obligations[0]
        self.assertEqual(ob_blk.lifecycle_state, "ambiguous-multi-target")
        self.assertEqual(ob_blk.criterion.evidence, "supported-source")
        self.assertTrue(ob_blk.criterion.blocking)
        self.assertEqual(ob_blk.criterion.impact, "critical")
        self.assertFalse(ob_blk.criterion.needs_human)

    def test_brand_new_duplicate_advisory_comments_do_not_promote_criterion(self):
        r1 = evaluate_drift_round(
            _result(
                _comment(path="src/a.py", line=10, body="Optional note A."),
                _comment(path="src/b.py", line=20, body="Optional note B."),
                paths=("src/a.py", "src/b.py"),
            ),
            head_sha=HEAD_1,
            base_sha=BASE_SHA,
            max_active_findings=8,
            round_number=1,
        )
        self.assertEqual(len(r1.inventory.obligations), 1)
        ob = r1.inventory.obligations[0]
        self.assertEqual(ob.lifecycle_state, "ambiguous-multi-target")
        self.assertEqual(ob.criterion.evidence, "advisory")
        self.assertFalse(ob.criterion.blocking)
        self.assertFalse(ob.criterion.needs_human)
        self.assertIsNone(ob.criterion.impact)
        self.assertTrue(ob.blocks_approval_now)
        self.assertFalse(r1.approved)

        # Mixed brand-new duplicates (one advisory, one blocking) promote without crashing.
        r_mixed = evaluate_drift_round(
            _result(
                _comment(path="src/a.py", line=10, body="Optional note A."),
                _comment(
                    path="src/b.py",
                    line=20,
                    body="Required check B.",
                    blocking=True,
                    severity="medium",
                ),
                paths=("src/a.py", "src/b.py"),
            ),
            head_sha=HEAD_1,
            base_sha=BASE_SHA,
            max_active_findings=8,
            round_number=1,
        )
        ob_m = r_mixed.inventory.obligations[0]
        self.assertEqual(ob_m.lifecycle_state, "ambiguous-multi-target")
        self.assertEqual(ob_m.criterion.evidence, "supported-source")
        self.assertTrue(ob_m.criterion.blocking)
        self.assertEqual(ob_m.criterion.impact, "medium")

    def test_single_reopen_of_fixed_advisory_preserves_or_promotes_explicitly(self):
        r1 = evaluate_drift_round(
            _result(_comment()),
            head_sha=HEAD_1,
            base_sha=BASE_SHA,
            max_active_findings=8,
            round_number=1,
        )
        prior_ob = r1.inventory.obligations[0]
        r1_inv = DriftInventory(
            obligations=(
                replace(prior_ob, thread_binding=_bound(prior_ob.obligation_id)),
            )
        )
        r2 = evaluate_drift_round(
            _result(),
            head_sha=HEAD_2,
            base_sha=BASE_SHA,
            max_active_findings=8,
            round_number=2,
            prior_inventory=r1_inv,
            confirmed_fixes=(
                ConfirmedFixEvidence(
                    obligation_id=prior_ob.obligation_id,
                    resolution_criterion=prior_ob.criterion.resolution_criterion,
                    evidence_sha256=_sha("fix-single"),
                    head_sha=HEAD_2,
                    verified_paths=("src/app.py",),
                ),
            ),
        )
        # Reopening as advisory keeps criterion advisory.
        r3_adv = evaluate_drift_round(
            _result(_comment()),
            head_sha=HEAD_3,
            base_sha=BASE_SHA,
            max_active_findings=8,
            round_number=3,
            prior_inventory=r2.inventory,
        )
        ob_adv = r3_adv.inventory.obligations[0]
        self.assertEqual(ob_adv.lifecycle_state, "active-unresolved")
        self.assertEqual(ob_adv.criterion.evidence, "advisory")
        self.assertFalse(ob_adv.criterion.blocking)
        self.assertIsNone(ob_adv.criterion.impact)

        # Reopening as blocking promotes cleanly.
        r3_blk = evaluate_drift_round(
            _result(_comment(blocking=True, severity="high")),
            head_sha=HEAD_3,
            base_sha=BASE_SHA,
            max_active_findings=8,
            round_number=3,
            prior_inventory=r2.inventory,
        )
        ob_blk = r3_blk.inventory.obligations[0]
        self.assertEqual(ob_blk.lifecycle_state, "active-unresolved")
        self.assertEqual(ob_blk.criterion.evidence, "supported-source")
        self.assertTrue(ob_blk.criterion.blocking)
        self.assertEqual(ob_blk.criterion.impact, "high")

    def test_genuinely_required_finding_stays_required_under_advisory_ambiguity(self):
        r1 = evaluate_drift_round(
            _result(_comment(blocking=True, severity="high")),
            head_sha=HEAD_1,
            base_sha=BASE_SHA,
            max_active_findings=8,
            round_number=1,
        )
        with_thread = evaluate_drift_round(
            _result(),
            head_sha=HEAD_1,
            base_sha=BASE_SHA,
            max_active_findings=8,
            round_number=1,
            prior_inventory=r1.inventory,
        )
        ob0 = with_thread.inventory.obligations[0]
        r2 = evaluate_drift_round(
            _result(
                _comment(path="src/app.py", blocking=False, needs_human=False),
                _comment(
                    path="src/copy.py",
                    blocking=False,
                    needs_human=False,
                    body="Copy also omits check.",
                ),
                paths=("src/app.py", "src/copy.py"),
            ),
            head_sha=HEAD_2,
            base_sha=BASE_SHA,
            max_active_findings=8,
            round_number=2,
            prior_inventory=r1.inventory,
            drift_descriptors=(
                FileDriftDescriptor(
                    drift_kind="file_copy",
                    source_path="src/app.py",
                    target_paths=("src/app.py", "src/copy.py"),
                ),
            ),
            placement_facts=None,
        )
        _ = ob0
        _ = _bound
        ob = r2.inventory.obligations[0]
        self.assertEqual(ob.lifecycle_state, "ambiguous-multi-target")
        self.assertTrue(ob.criterion.blocking)
        self.assertEqual(ob.criterion.evidence, "supported-source")
        self.assertEqual(ob.criterion.impact, "high")
        self.assertTrue(ob.blocks_approval_now)
        self.assertFalse(r2.approved)


if __name__ == "__main__":
    unittest.main()
