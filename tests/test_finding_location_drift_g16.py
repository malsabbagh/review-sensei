"""Qualification tests for cross-file finding location and thread drift (G16)."""

from __future__ import annotations

import hashlib
import unittest
from dataclasses import replace

from review_sensei.active_finding_window import (
    AuthorizedCoverageAcceptance,
    AuthorizedHumanOverride,
    ConfirmedFixEvidence,
)
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.coverage import CoverageManifest, FileCoverage
from review_sensei.errors import ReviewInputError
from review_sensei.finding_drift import (
    DriftInventory,
    DriftTrackedObligation,
    FileDriftDescriptor,
    NativeThreadBinding,
    OriginalFindingCriterion,
    PresentLocationAnchor,
    SyntheticNativeDriftPublisher,
    drift_obligation_from_comment,
    evaluate_drift_round,
    extract_drift_descriptors_from_diff,
)
from review_sensei.models import ReviewComment, ReviewResult, ReviewTransaction
from review_sensei.placement import (
    BODY_PLACEMENT,
    BODY_PLACEMENT_NOTE,
    BODY_REASON_UNANCHORED,
    CONVERSATION_RESOLUTION_REQUIRED,
    INLINE_PLACEMENT,
    HostPlacementFacts,
)
from review_sensei.rescan import AuthorizedRescan

BASE_SHA = "a" * 40
HEAD_1 = "1" * 40
HEAD_2 = "2" * 40
HEAD_3 = "3" * 40
HEAD_4 = "4" * 40
HEAD_5 = "5" * 40


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _coverage(*paths: str, partial_path: str | None = None) -> CoverageManifest:
    all_paths = list(paths)
    entries = [FileCoverage(p, "reviewed") for p in paths]
    if partial_path is not None:
        all_paths.append(partial_path)
        entries.append(
            FileCoverage(
                partial_path, "partially-reviewed", reason="provider-call-budget"
            )
        )
    return CoverageManifest(
        files=tuple(entries),
        enumerated_paths=tuple(all_paths),
        enumeration_complete=True,
    )


def _comment(
    path: str = "src/legacy_auth.py",
    *,
    line: int | None = 12,
    side: str = "RIGHT",
    body: str = "Signature check skipped when key_id is empty.",
    blocking: bool = True,
    needs_human: bool = False,
    severity: str = "critical",
    symbol: str | None = "verify_token",
    defect_kind: str | None = "auth_bypass",
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
    paths: tuple[str, ...] = ("src/legacy_auth.py",),
    status: str = "complete",
    coverage: CoverageManifest | None = None,
) -> ReviewResult:
    cov = coverage if coverage is not None else _coverage(*paths)
    return ReviewResult(
        summary="Synthetic drift qualification review.",
        comments=comments,
        provider="fixture",
        review_status=status,
        coverage=cov,
    )


def _published_transaction(head_sha: str) -> ReviewTransaction:
    cfg = _sha("cfg")
    pol = _sha("pol")
    ev = _sha("ev")
    resv = _sha("resv")
    return ReviewTransaction(
        transaction_id=ReviewTransaction.derive_id(
            repository="owner/repo",
            pull_request=55,
            base_sha=BASE_SHA,
            head_sha=head_sha,
            policy_digest=pol,
            configuration_digest=cfg,
            evidence_digest=ev,
            reservation_id=resv,
        ),
        repository="owner/repo",
        pull_request=55,
        base_sha=BASE_SHA,
        head_sha=head_sha,
        policy_digest=pol,
        configuration_digest=cfg,
        evidence_digest=ev,
        reservation_id=resv,
        generation=1,
        phase="publication_succeeded",
        result_sha256=_sha(f"res-{head_sha}"),
    )


class FindingLocationDriftG16Tests(unittest.TestCase):
    def test_file_rename_reuses_thread_and_recovers_to_approve(self) -> None:
        publisher = SyntheticNativeDriftPublisher()
        c_crit = _comment(
            "src/legacy_auth.py",
            line=12,
            severity="critical",
            symbol="verify_token",
            defect_kind="auth_bypass",
        )
        c_med = _comment(
            "src/session.py",
            line=30,
            body="Session TTL does not clamp negative values.",
            severity="medium",
            symbol="compute_ttl",
            defect_kind="bounds_check",
        )
        # Round 1: 2 obligations, active cap = 1 -> only critical published inline.
        r1_eval = evaluate_drift_round(
            _result(c_crit, c_med, paths=("src/legacy_auth.py", "src/session.py")),
            head_sha=HEAD_1,
            base_sha=BASE_SHA,
            max_active_findings=1,
            round_number=1,
            placement_facts=HostPlacementFacts(CONVERSATION_RESOLUTION_REQUIRED),
        )
        r1, rec1 = publisher.publish_and_finalize(r1_eval)
        self.assertEqual(rec1.review_event, "COMMENT")
        self.assertEqual(rec1.created_threads, 1)
        self.assertEqual(rec1.active_thread_ids, ("gh-drift-thread-1",))
        crit_ob = next(
            o
            for o in r1.inventory.obligations
            if o.criterion.origin_symbol == "verify_token"
        )
        med_ob = next(
            o
            for o in r1.inventory.obligations
            if o.criterion.origin_symbol == "compute_ttl"
        )

        # Round 2: git rename from src/legacy_auth.py -> src/security/auth.py + line shift to 44!
        rename_diff = (
            "diff --git a/src/legacy_auth.py b/src/security/auth.py\n"
            "similarity index 88%\n"
            "rename from src/legacy_auth.py\n"
            "rename to src/security/auth.py\n"
            "--- a/src/legacy_auth.py\n"
            "+++ b/src/security/auth.py\n"
            "@@ -10,2 +42,2 @@\n"
            " def verify_token(token, key_id):\n"
            "-    if not key_id: return True\n"
            "+    if key_id == '': return True\n"
        )
        renamed_comment = _comment(
            "src/security/auth.py",
            line=44,
            body="Empty key_id still bypasses signature check after rename.",
            severity="critical",
            symbol="verify_token",
            defect_kind="auth_bypass",
        )
        r2_eval = evaluate_drift_round(
            _result(
                renamed_comment,
                c_med,
                paths=("src/security/auth.py", "src/session.py"),
            ),
            head_sha=HEAD_2,
            base_sha=BASE_SHA,
            max_active_findings=1,
            round_number=2,
            coverage_mode="incremental",
            prior_inventory=r1.inventory,
            diff_text=rename_diff,
        )
        r2, rec2 = publisher.publish_and_finalize(r2_eval)
        # Same obligation_id AND same thread reused in-place (zero new threads)!
        self.assertEqual(rec2.created_threads, 0)
        self.assertEqual(rec2.updated_threads, 1)
        self.assertEqual(rec2.active_thread_ids, ("gh-drift-thread-1",))
        self.assertEqual(r2.active_entries[0].obligation_id, crit_ob.obligation_id)
        self.assertEqual(r2.active_entries[0].drift_kind, "file_rename")
        self.assertEqual(r2.active_entries[0].origin_path, "src/legacy_auth.py")
        self.assertEqual(r2.active_entries[0].present_path, "src/security/auth.py")
        self.assertEqual(len(r2.inventory.obligations), 2)

        # Round 3: fix verify_token in src/security/auth.py -> resolves gh-drift-thread-1
        # and promotes compute_ttl into gh-drift-thread-2!
        fix_crit = ConfirmedFixEvidence(
            obligation_id=crit_ob.obligation_id,
            resolution_criterion=crit_ob.criterion.resolution_criterion,
            head_sha=HEAD_3,
            evidence_sha256=_sha("fix-renamed-auth"),
            verified_paths=("src/security/auth.py",),
        )
        r3_eval = evaluate_drift_round(
            _result(c_med, paths=("src/security/auth.py", "src/session.py")),
            head_sha=HEAD_3,
            base_sha=BASE_SHA,
            max_active_findings=1,
            round_number=3,
            coverage_mode="incremental",
            prior_inventory=r2.inventory,
            confirmed_fixes=(fix_crit,),
        )
        r3, rec3 = publisher.publish_and_finalize(r3_eval)
        self.assertEqual(rec3.resolved_threads, 1)
        self.assertEqual(rec3.created_threads, 1)
        self.assertEqual(rec3.active_thread_ids, ("gh-drift-thread-2",))
        self.assertEqual(rec3.resolved_thread_ids, ("gh-drift-thread-1",))
        self.assertEqual(r3.active_entries[0].obligation_id, med_ob.obligation_id)

        # Round 4: fix compute_ttl -> native finalization emits APPROVE!
        fix_med = ConfirmedFixEvidence(
            obligation_id=med_ob.obligation_id,
            resolution_criterion=med_ob.criterion.resolution_criterion,
            head_sha=HEAD_4,
            evidence_sha256=_sha("fix-med-ttl"),
            verified_paths=("src/session.py",),
        )
        r4_eval = evaluate_drift_round(
            _result(paths=("src/security/auth.py", "src/session.py")),
            head_sha=HEAD_4,
            base_sha=BASE_SHA,
            max_active_findings=1,
            round_number=4,
            coverage_mode="incremental",
            prior_inventory=r3.inventory,
            confirmed_fixes=(fix_med,),
        )
        r4, rec4 = publisher.publish_and_finalize(r4_eval)
        self.assertTrue(r4.approved)
        self.assertEqual(rec4.review_event, "APPROVE")
        self.assertEqual(rec4.check_conclusion, "success")
        self.assertEqual(rec4.active_thread_ids, ())
        self.assertEqual(
            rec4.resolved_thread_ids, ("gh-drift-thread-1", "gh-drift-thread-2")
        )

    def test_file_copy_and_duplicate_code_hold_ambiguity_until_all_targets_resolved(
        self,
    ) -> None:
        publisher = SyntheticNativeDriftPublisher()
        c_orig = _comment(
            "src/handler_v1.py",
            line=8,
            body="Unescaped template interpolation in response writer.",
            symbol="render_page",
            defect_kind="xss",
        )
        r1, _ = publisher.publish_and_finalize(
            evaluate_drift_round(
                _result(c_orig, paths=("src/handler_v1.py",)),
                head_sha=HEAD_1,
                base_sha=BASE_SHA,
                max_active_findings=3,
                round_number=1,
            )
        )
        oid = r1.inventory.obligations[0].obligation_id
        crit = r1.inventory.obligations[0].criterion.resolution_criterion

        # Round 2: handler_v1.py is copied to handler_v2.py (both contain the XSS)!
        copy_diff = (
            "diff --git a/src/handler_v1.py b/src/handler_v2.py\n"
            "similarity index 95%\n"
            "copy from src/handler_v1.py\n"
            "copy to src/handler_v2.py\n"
            "--- a/src/handler_v1.py\n"
            "+++ b/src/handler_v2.py\n"
            "@@ -7,2 +7,2 @@\n"
            " def render_page(html):\n"
            "-    return f'<div>{html}</div>'\n"
            "+    return f'<section>{html}</section>'\n"
        )
        c_copy_1 = _comment(
            "src/handler_v1.py",
            line=8,
            body="Unescaped HTML in v1.",
            symbol="render_page",
            defect_kind="xss",
        )
        c_copy_2 = _comment(
            "src/handler_v2.py",
            line=8,
            body="Unescaped HTML in copied v2.",
            symbol="render_page",
            defect_kind="xss",
        )
        r2_eval = evaluate_drift_round(
            _result(
                c_copy_1,
                c_copy_2,
                paths=("src/handler_v1.py", "src/handler_v2.py"),
            ),
            head_sha=HEAD_2,
            base_sha=BASE_SHA,
            max_active_findings=3,
            round_number=2,
            coverage_mode="incremental",
            prior_inventory=r1.inventory,
            diff_text=copy_diff,
        )
        r2, rec2 = publisher.publish_and_finalize(r2_eval)
        self.assertFalse(r2.approved)
        self.assertEqual(rec2.review_event, "COMMENT")
        self.assertIn("ambiguous-drift-target", r2.withheld_reasons)
        # Existing thread is updated, NOT duplicated into 2 threads!
        self.assertEqual(rec2.created_threads, 0)
        self.assertEqual(rec2.updated_threads, 1)
        self.assertEqual(
            r2.inventory.obligations[0].lifecycle_state, "ambiguous-multi-target"
        )

        # Partial fix on ONLY handler_v1.py does NOT clear the multi-target copy!
        partial_fix = ConfirmedFixEvidence(
            obligation_id=oid,
            resolution_criterion=crit,
            head_sha=HEAD_3,
            evidence_sha256=_sha("partial-copy-fix"),
            verified_paths=("src/handler_v1.py",),
        )
        r3_partial = evaluate_drift_round(
            _result(paths=("src/handler_v1.py", "src/handler_v2.py")),
            head_sha=HEAD_3,
            base_sha=BASE_SHA,
            max_active_findings=3,
            round_number=3,
            coverage_mode="incremental",
            prior_inventory=r2.inventory,
            drift_descriptors=(
                FileDriftDescriptor(
                    drift_kind="file_copy",
                    source_path="src/handler_v1.py",
                    target_paths=("src/handler_v2.py",),
                ),
            ),
            confirmed_fixes=(partial_fix,),
        )
        self.assertFalse(r3_partial.approved)

        # Complete fix verifying BOTH source and copy paths resolves cleanly to APPROVE!
        full_copy_fix = ConfirmedFixEvidence(
            obligation_id=oid,
            resolution_criterion=crit,
            head_sha=HEAD_3,
            evidence_sha256=_sha("both-copy-paths-fixed"),
            verified_paths=("src/handler_v1.py", "src/handler_v2.py"),
        )
        r3_ok, rec3_ok = publisher.publish_and_finalize(
            evaluate_drift_round(
                _result(paths=("src/handler_v1.py", "src/handler_v2.py")),
                head_sha=HEAD_3,
                base_sha=BASE_SHA,
                max_active_findings=3,
                round_number=3,
                coverage_mode="incremental",
                prior_inventory=r2.inventory,
                drift_descriptors=(
                    FileDriftDescriptor(
                        drift_kind="file_copy",
                        source_path="src/handler_v1.py",
                        target_paths=("src/handler_v2.py",),
                    ),
                ),
                confirmed_fixes=(full_copy_fix,),
            )
        )
        self.assertTrue(r3_ok.approved)
        self.assertEqual(rec3_ok.review_event, "APPROVE")

        # Brand-new same-round duplicate_code introduction also withholds approval
        # and recovers via AuthorizedHumanOverride!
        dup_new_a = _comment(
            "src/util_a.py",
            line=5,
            body="Weak MD5 token hash in util_a.",
            symbol="hash_secret",
            defect_kind="weak_crypto",
        )
        dup_new_b = _comment(
            "src/util_b.py",
            line=9,
            body="Weak MD5 token hash duplicated in util_b.",
            symbol="hash_secret",
            defect_kind="weak_crypto",
        )
        r_dup = evaluate_drift_round(
            _result(dup_new_a, dup_new_b, paths=("src/util_a.py", "src/util_b.py")),
            head_sha=HEAD_4,
            base_sha=BASE_SHA,
            max_active_findings=3,
            round_number=1,
        )
        self.assertFalse(r_dup.approved)
        self.assertIn("ambiguous-drift-target", r_dup.withheld_reasons)
        dup_oid = r_dup.inventory.obligations[0].obligation_id
        r_dup_recovered = evaluate_drift_round(
            _result(paths=("src/util_a.py", "src/util_b.py")),
            head_sha=HEAD_5,
            base_sha=BASE_SHA,
            max_active_findings=3,
            round_number=2,
            prior_inventory=r_dup.inventory,
            human_overrides=(
                AuthorizedHumanOverride(
                    obligation_id=dup_oid,
                    action="accept-risk",
                    actor="sec-lead",
                    association="OWNER",
                    head_sha=HEAD_5,
                    reason="MD5 used strictly for non-security cache shard key.",
                    proof_sha256=_sha("dup-override"),
                ),
            ),
        )
        self.assertTrue(r_dup_recovered.approved)

    def test_cross_file_move_unique_and_split_ambiguity(self) -> None:
        publisher = SyntheticNativeDriftPublisher()
        c_mono = _comment(
            "src/monolith.py",
            line=88,
            body="Double-charge race when retrying invoice capture.",
            symbol="capture_invoice",
            defect_kind="idempotency",
        )
        r1, rec1 = publisher.publish_and_finalize(
            evaluate_drift_round(
                _result(c_mono, paths=("src/monolith.py",)),
                head_sha=HEAD_1,
                base_sha=BASE_SHA,
                max_active_findings=2,
                round_number=1,
            )
        )
        self.assertEqual(rec1.active_thread_ids, ("gh-drift-thread-1",))
        oid = r1.inventory.obligations[0].obligation_id

        # Unique cross-file move of capture_invoice -> src/billing/capture.py
        c_moved = _comment(
            "src/billing/capture.py",
            line=19,
            body="Invoice retry still lacks idempotency key after extraction.",
            symbol="capture_invoice",
            defect_kind="idempotency",
        )
        r2, rec2 = publisher.publish_and_finalize(
            evaluate_drift_round(
                _result(c_moved, paths=("src/monolith.py", "src/billing/capture.py")),
                head_sha=HEAD_2,
                base_sha=BASE_SHA,
                max_active_findings=2,
                round_number=2,
                coverage_mode="incremental",
                prior_inventory=r1.inventory,
                drift_descriptors=(
                    FileDriftDescriptor(
                        drift_kind="cross_file_move",
                        source_path="src/monolith.py",
                        target_paths=("src/billing/capture.py",),
                        moved_symbols=("capture_invoice",),
                    ),
                ),
            )
        )
        self.assertEqual(rec2.created_threads, 0)
        self.assertEqual(rec2.updated_threads, 1)
        self.assertEqual(r2.active_entries[0].obligation_id, oid)
        self.assertEqual(r2.active_entries[0].drift_kind, "cross_file_move")
        self.assertEqual(r2.active_entries[0].present_path, "src/billing/capture.py")

        # Now split capture_invoice across two files -> ambiguous-multi-target,
        # then recover cleanly via AuthorizedRescan(mode="full")!
        r3, rec3 = publisher.publish_and_finalize(
            evaluate_drift_round(
                _result(
                    paths=(
                        "src/billing/capture.py",
                        "src/billing/stripe_capture.py",
                        "src/billing/ach_capture.py",
                    )
                ),
                head_sha=HEAD_3,
                base_sha=BASE_SHA,
                max_active_findings=2,
                round_number=3,
                coverage_mode="incremental",
                prior_inventory=r2.inventory,
                drift_descriptors=(
                    FileDriftDescriptor(
                        drift_kind="cross_file_move",
                        source_path="src/billing/capture.py",
                        target_paths=(
                            "src/billing/stripe_capture.py",
                            "src/billing/ach_capture.py",
                        ),
                        moved_symbols=("capture_invoice",),
                    ),
                ),
            )
        )
        self.assertFalse(r3.approved)
        self.assertEqual(rec3.review_event, "COMMENT")
        self.assertIn("ambiguous-drift-target", r3.withheld_reasons)

        r4, rec4 = publisher.publish_and_finalize(
            evaluate_drift_round(
                _result(
                    paths=(
                        "src/billing/stripe_capture.py",
                        "src/billing/ach_capture.py",
                    )
                ),
                head_sha=HEAD_4,
                base_sha=BASE_SHA,
                max_active_findings=2,
                round_number=4,
                coverage_mode="full",
                prior_inventory=r3.inventory,
                authorized_rescan=AuthorizedRescan(
                    kind="issue_comment",
                    request_id=404,
                    source=_published_transaction(HEAD_4),
                    review_id=505,
                    mode="full",
                ),
            )
        )
        self.assertTrue(r4.approved)
        self.assertEqual(rec4.review_event, "APPROVE")

    def test_line_deletion_outdated_anchor_and_human_thread_resolve_never_free_obligation(
        self,
    ) -> None:
        publisher = SyntheticNativeDriftPublisher()
        c1 = _comment(
            "src/legacy_endpoint.py",
            line=15,
            body="Endpoint leaks internal stack trace to unauthenticated caller.",
            symbol="handle_error",
            defect_kind="info_leak",
        )
        r1, rec1 = publisher.publish_and_finalize(
            evaluate_drift_round(
                _result(c1, paths=("src/legacy_endpoint.py",)),
                head_sha=HEAD_1,
                base_sha=BASE_SHA,
                max_active_findings=2,
                round_number=1,
            )
        )
        self.assertEqual(rec1.active_thread_ids, ("gh-drift-thread-1",))
        ob = r1.inventory.obligations[0]

        # Simulate:
        # 1) File deleted in diff,
        # 2) GitHub marks thread outdated, AND
        # 3) PR author clicks "Resolve conversation" in GitHub UI!
        publisher.mark_host_outdated("gh-drift-thread-1")
        publisher.mark_host_resolved_by_human("gh-drift-thread-1")
        delete_diff = (
            "diff --git a/src/legacy_endpoint.py b/src/legacy_endpoint.py\n"
            "deleted file mode 100644\n"
            "--- a/src/legacy_endpoint.py\n"
            "+++ /dev/null\n"
            "@@ -1,3 +0,0 @@\n"
            "-def handle_error(exc):\n"
            "-    return str(exc)\n"
            "-    # end\n"
        )
        r2_eval = evaluate_drift_round(
            _result(paths=("src/router.py",)),
            head_sha=HEAD_2,
            base_sha=BASE_SHA,
            max_active_findings=2,
            round_number=2,
            coverage_mode="incremental",
            prior_inventory=r1.inventory,
            diff_text=delete_diff,
            host_outdated_threads=frozenset({"gh-drift-thread-1"}),
            host_resolved_threads=frozenset({"gh-drift-thread-1"}),
        )
        r2, rec2 = publisher.publish_and_finalize(r2_eval)
        # MUST NOT approve! Must reopen the prematurely resolved thread and project
        # unanchored explanation + BODY_PLACEMENT_NOTE into summary body!
        self.assertFalse(r2.approved)
        self.assertEqual(rec2.review_event, "COMMENT")
        self.assertEqual(rec2.reopened_threads, 1)
        self.assertEqual(rec2.active_thread_ids, ("gh-drift-thread-1",))
        self.assertEqual(r2.active_entries[0].placement, BODY_PLACEMENT)
        self.assertEqual(r2.active_entries[0].body_reason, BODY_REASON_UNANCHORED)
        self.assertIn(BODY_PLACEMENT_NOTE, rec2.summary_body)
        self.assertIn("uncertain-finding-lifecycle", r2.withheld_reasons)

        # Now supply positive ConfirmedFixEvidence proving removal fixed the leak:
        fix_deleted = ConfirmedFixEvidence(
            obligation_id=ob.obligation_id,
            resolution_criterion=ob.criterion.resolution_criterion,
            head_sha=HEAD_3,
            evidence_sha256=_sha("verified-endpoint-removed-from-router"),
            verified_paths=("src/router.py",),
        )
        r3, rec3 = publisher.publish_and_finalize(
            evaluate_drift_round(
                _result(paths=("src/router.py",)),
                head_sha=HEAD_3,
                base_sha=BASE_SHA,
                max_active_findings=2,
                round_number=3,
                coverage_mode="incremental",
                prior_inventory=r2.inventory,
                diff_text=delete_diff,
                confirmed_fixes=(fix_deleted,),
            )
        )
        self.assertTrue(r3.approved)
        self.assertEqual(rec3.review_event, "APPROVE")
        self.assertEqual(rec3.resolved_threads, 1)

    def test_whole_file_rewrite_preserves_criterion_and_regression_reopens_thread(
        self,
    ) -> None:
        publisher = SyntheticNativeDriftPublisher()
        c_orig = _comment(
            "src/parser.py",
            line=3,
            body="Unbounded recursion on nested brackets.",
            symbol="parse_expr",
            defect_kind="dos_recursion",
        )
        r1, _ = publisher.publish_and_finalize(
            evaluate_drift_round(
                _result(c_orig, paths=("src/parser.py",)),
                head_sha=HEAD_1,
                base_sha=BASE_SHA,
                max_active_findings=2,
                round_number=1,
            )
        )
        ob = r1.inventory.obligations[0]

        # Whole-file rewrite replacing all 4 lines with 4 new lines, zero context!
        rewrite_diff = (
            "diff --git a/src/parser.py b/src/parser.py\n"
            "--- a/src/parser.py\n"
            "+++ b/src/parser.py\n"
            "@@ -1,4 +1,4 @@\n"
            "-def parse_expr(s):\n"
            "-    if s.startswith('['):\n"
            "-        return parse_expr(s[1:-1])\n"
            "-    return s\n"
            "+class IterativeParser:\n"
            "+    def run(self, text: str) -> str:\n"
            "+        stack = []\n"
            "+        return text.strip('[]')\n"
        )
        extracted = extract_drift_descriptors_from_diff(rewrite_diff)
        self.assertEqual(extracted[0].drift_kind, "whole_file_rewrite")

        # Complete diff coverage on rewritten file WITHOUT ConfirmedFixEvidence
        # must still withhold approval ("omitted-uncertain")!
        r2_omitted, rec2 = publisher.publish_and_finalize(
            evaluate_drift_round(
                _result(paths=("src/parser.py",)),
                head_sha=HEAD_2,
                base_sha=BASE_SHA,
                max_active_findings=2,
                round_number=2,
                coverage_mode="incremental",
                prior_inventory=r1.inventory,
                diff_text=rewrite_diff,
            )
        )
        self.assertFalse(r2_omitted.approved)
        self.assertEqual(rec2.review_event, "COMMENT")
        self.assertEqual(
            r2_omitted.inventory.obligations[0].present_anchor.drift_kind,
            "whole_file_rewrite",
        )
        self.assertEqual(
            r2_omitted.inventory.obligations[0].lifecycle_state, "omitted-uncertain"
        )

        # Positive ConfirmedFixEvidence resolves the rewritten file to APPROVE!
        r3, rec3 = publisher.publish_and_finalize(
            evaluate_drift_round(
                _result(paths=("src/parser.py",)),
                head_sha=HEAD_3,
                base_sha=BASE_SHA,
                max_active_findings=2,
                round_number=3,
                coverage_mode="incremental",
                prior_inventory=r2_omitted.inventory,
                confirmed_fixes=(
                    ConfirmedFixEvidence(
                        obligation_id=ob.obligation_id,
                        resolution_criterion=ob.criterion.resolution_criterion,
                        head_sha=HEAD_3,
                        evidence_sha256=_sha("iterative-parser-fix"),
                        verified_paths=("src/parser.py",),
                    ),
                ),
            )
        )
        self.assertTrue(r3.approved)
        self.assertEqual(rec3.review_event, "APPROVE")

        # Round 4: regression re-introduces the defect -> reopens gh-drift-thread-1!
        r4, rec4 = publisher.publish_and_finalize(
            evaluate_drift_round(
                _result(
                    _comment(
                        "src/parser.py",
                        line=2,
                        body="Recursive helper reintroduced unbounded recursion.",
                        symbol="parse_expr",
                        defect_kind="dos_recursion",
                    ),
                    paths=("src/parser.py",),
                ),
                head_sha=HEAD_4,
                base_sha=BASE_SHA,
                max_active_findings=2,
                round_number=4,
                coverage_mode="incremental",
                prior_inventory=r3.inventory,
            )
        )
        self.assertFalse(r4.approved)
        self.assertEqual(rec4.reopened_threads, 1)
        self.assertEqual(rec4.active_thread_ids, ("gh-drift-thread-1",))
        bf = r4.inventory.obligations[0].to_baseline_finding()
        self.assertEqual(bf.resolution_criterion, ob.criterion.resolution_criterion)
        self.assertIn("src/parser.py", r4.inventory.obligations[0].implicated_paths)

    def test_drift_validation_and_guardrail_edges(self) -> None:
        # Advisory / partial coverage / validation guardrails:
        c_opt = _comment(
            "src/app.py",
            line=4,
            body="Optional style note.",
            blocking=False,
            severity="low",
        )
        ob_opt = drift_obligation_from_comment(c_opt, head_sha=HEAD_1)
        self.assertFalse(ob_opt.criterion.required)
        self.assertFalse(ob_opt.blocks_approval_now)

        partial_cov = _coverage("src/app.py", partial_path="src/other.py")
        r_partial = evaluate_drift_round(
            _result(c_opt, status="partial", coverage=partial_cov),
            head_sha=HEAD_1,
            base_sha=BASE_SHA,
            max_active_findings=2,
            advisory=True,
            convergence_policy=ReviewConvergencePolicy(mode="advisory"),
        )
        self.assertFalse(r_partial.approved)
        self.assertIn("coverage-partial", r_partial.withheld_reasons)
        self.assertEqual(r_partial.active_entries[0].placement, BODY_PLACEMENT)

        # Partial coverage accepted by maintainer with no blocking obligations -> approved!
        r_accepted = evaluate_drift_round(
            _result(c_opt, status="partial", coverage=partial_cov),
            head_sha=HEAD_1,
            base_sha=BASE_SHA,
            max_active_findings=2,
            coverage_acceptance=AuthorizedCoverageAcceptance(
                actor="maintainer",
                association="OWNER",
                head_sha=HEAD_1,
                reason="src/other.py is generated lockfile metadata.",
                proof_sha256=_sha("accept-partial"),
            ),
        )
        self.assertTrue(r_accepted.approved)

        # Dataclass error branches:
        with self.assertRaises(ReviewInputError):
            FileDriftDescriptor(drift_kind="unchanged", source_path="src/app.py")
        with self.assertRaises(ReviewInputError):
            FileDriftDescriptor(drift_kind="file_rename", source_path="src/app.py")
        with self.assertRaises(ReviewInputError):
            FileDriftDescriptor(
                drift_kind="file_rename",
                source_path="src/app.py",
                target_paths=("src/a.py", "src/b.py"),
            )
        with self.assertRaises(ReviewInputError):
            PresentLocationAnchor(
                head_sha=HEAD_1,
                path=None,
                line=None,
                side="RIGHT",
                symbol=None,
                anchor_location="right",
            )
        with self.assertRaises(ReviewInputError):
            NativeThreadBinding(
                thread_id="t1",
                comment_id=1,
                placement=INLINE_PLACEMENT,
                body_reason="unexpected",
                slot_state="active",
                proof_sha256=_sha("p"),
            )
        with self.assertRaises(ReviewInputError):
            NativeThreadBinding(
                thread_id="t1",
                comment_id=1,
                placement=BODY_PLACEMENT,
                body_reason=None,
                slot_state="active",
                proof_sha256=_sha("p"),
            )
        with self.assertRaises(ReviewInputError):
            DriftInventory(obligations=(ob_opt, ob_opt))
        with self.assertRaises(ReviewInputError):
            replace(ob_opt.criterion, origin_side="FILE")
        with self.assertRaises(ReviewInputError):
            replace(ob_opt, resolution_proof_sha256=_sha("unexpected"))
        self.assertIsInstance(ob_opt.criterion, OriginalFindingCriterion)
        self.assertIsInstance(ob_opt, DriftTrackedObligation)

    def test_remaining_drift_descriptors_and_validation_branches(self) -> None:
        # 1) Explicit duplicate_code descriptor + incomplete coverage + uncertain slot:
        ob1 = drift_obligation_from_comment(
            _comment("src/a.py", line=3, symbol="fn_x", defect_kind="race"),
            head_sha=HEAD_1,
            obligation_id=_sha("ob1"),
        )
        ob1_uncertain = replace(
            ob1,
            latest_comment=None,
            thread_binding=NativeThreadBinding(
                thread_id="uncertain-t1",
                comment_id=101,
                placement=INLINE_PLACEMENT,
                body_reason=None,
                slot_state="uncertain",
                proof_sha256=_sha("u"),
            ),
        )
        self.assertIsNotNone(ob1_uncertain.synthetic_comment_for_placement())
        r_unc = evaluate_drift_round(
            ReviewResult(
                summary="Incomplete.",
                comments=(),
                provider="fixture",
                review_status="incomplete",
                coverage=None,
            ),
            head_sha=HEAD_2,
            base_sha=BASE_SHA,
            max_active_findings=2,
            round_number=2,
            prior_inventory=DriftInventory(obligations=(ob1_uncertain,)),
            drift_descriptors=(
                FileDriftDescriptor(
                    drift_kind="duplicate_code",
                    source_path="src/a.py",
                    target_paths=("src/b.py",),
                ),
            ),
        )
        self.assertFalse(r_unc.approved)
        self.assertIn("coverage-incomplete", r_unc.withheld_reasons)
        self.assertIn("uncertain-native-slot", r_unc.withheld_reasons)
        self.assertIn("ambiguous-drift-target", r_unc.withheld_reasons)
        pub = r_unc.to_public_status()
        self.assertFalse(pub["approval_eligible"])

        # Cannot release uncertain thread slot via override or fix:
        with self.assertRaisesRegex(ReviewInputError, "uncertain thread slot"):
            evaluate_drift_round(
                _result(paths=("src/a.py",)),
                head_sha=HEAD_2,
                base_sha=BASE_SHA,
                max_active_findings=2,
                round_number=2,
                prior_inventory=DriftInventory(obligations=(ob1_uncertain,)),
                human_overrides=(
                    AuthorizedHumanOverride(
                        obligation_id=ob1.obligation_id,
                        action="override",
                        actor="admin",
                        association="OWNER",
                        head_sha=HEAD_2,
                        reason="Testing uncertain slot guard.",
                        proof_sha256=_sha("o"),
                    ),
                ),
            )
        with self.assertRaisesRegex(ReviewInputError, "uncertain thread slot"):
            evaluate_drift_round(
                _result(paths=("src/a.py",)),
                head_sha=HEAD_2,
                base_sha=BASE_SHA,
                max_active_findings=2,
                round_number=2,
                prior_inventory=DriftInventory(obligations=(ob1_uncertain,)),
                confirmed_fixes=(
                    ConfirmedFixEvidence(
                        obligation_id=ob1.obligation_id,
                        resolution_criterion=ob1.criterion.resolution_criterion,
                        head_sha=HEAD_2,
                        evidence_sha256=_sha("f"),
                        verified_paths=("src/a.py",),
                    ),
                ),
            )

        # 2) Multi-prior ambiguity + file-level comment (line=None, side="FILE") + rescan:
        ob2 = drift_obligation_from_comment(
            _comment(
                "src/a.py",
                line=None,
                side="FILE",
                body="File-level race condition.",
                symbol="fn_x",
                defect_kind="race",
            ),
            head_sha=HEAD_1,
            obligation_id=_sha("ob2"),
        )
        r_multi = evaluate_drift_round(
            _result(
                _comment("src/a.py", line=9, symbol="fn_x", defect_kind="race"),
                paths=("src/a.py",),
            ),
            head_sha=HEAD_2,
            base_sha=BASE_SHA,
            max_active_findings=3,
            round_number=2,
            prior_inventory=DriftInventory(obligations=(ob1, ob2)),
        )
        self.assertTrue(r_multi.inventory.has_ambiguous_drift)

        # Full rescan supersedes non-ambiguous omitted prior cleanly:
        r_rescan = evaluate_drift_round(
            _result(paths=("src/a.py",)),
            head_sha=HEAD_3,
            base_sha=BASE_SHA,
            max_active_findings=2,
            round_number=3,
            coverage_mode="full",
            prior_inventory=DriftInventory(obligations=(ob1,)),
            authorized_rescan=AuthorizedRescan(
                kind="workflow_dispatch",
                request_id=9,
                source=_published_transaction(HEAD_3),
                review_id=10,
                mode="full",
            ),
        )
        self.assertTrue(r_rescan.approved)

        # 3) Remaining validation guards:
        for bad_crit in (
            {"origin_line": 0},
            {"origin_symbol": "  "},
            {"defect_kind": ""},
            {"blocking": "nope"},
            {"impact": "invalid"},
            {"evidence": "invalid"},
            {"evidence": "advisory", "blocking": True},
        ):
            with self.assertRaises(ReviewInputError):
                replace(ob1.criterion, **bad_crit)

        for bad_anchor in (
            {"line": 0},
            {"side": "BOTTOM"},
            {"symbol": " "},
            {"anchor_location": "nowhere"},
            {"drift_kind": "teleport"},
        ):
            with self.assertRaises(ReviewInputError):
                replace(ob1.present_anchor, **bad_anchor)

        for bad_ob in (
            {"lifecycle_state": "bad"},
            {"generation": -1},
            {"proposal_rank": 100},
            {"thread_binding": "not-a-binding"},
        ):
            with self.assertRaises(ReviewInputError):
                replace(ob1, **bad_ob)

        with self.assertRaises(ReviewInputError):
            FileDriftDescriptor(
                drift_kind="cross_file_move",
                source_path="src/a.py",
                target_paths=("src/b.py",),
                moved_symbols=("",),
            )
        with self.assertRaises(ReviewInputError):
            evaluate_drift_round(
                _result(paths=("src/a.py",)),
                head_sha=HEAD_2,
                base_sha=BASE_SHA,
                max_active_findings=0,
            )
        with self.assertRaises(ReviewInputError):
            evaluate_drift_round(
                _result(paths=("src/a.py",)),
                head_sha=HEAD_2,
                base_sha=BASE_SHA,
                max_active_findings=2,
                round_number=0,
            )
        with self.assertRaises(ReviewInputError):
            evaluate_drift_round(
                _result(paths=("src/a.py",)),
                head_sha=HEAD_2,
                base_sha=BASE_SHA,
                max_active_findings=2,
                prior_inventory=DriftInventory(obligations=(ob1,)),
                confirmed_fixes=(
                    ConfirmedFixEvidence(
                        obligation_id=ob1.obligation_id,
                        resolution_criterion=_sha("mismatch"),
                        head_sha=HEAD_2,
                        evidence_sha256=_sha("f"),
                        verified_paths=("src/a.py",),
                    ),
                ),
            )

    def test_drift_review_regressions_concern_scope_coverage_reopen_and_finalize(
        self,
    ) -> None:
        # 1) RS-03B9CC & RS-73DC43: additive whole-file rewrite on existing file
        #    and deduplication of copy headers against rename headers:
        additive_rewrite_diff = (
            "diff --git a/src/regenerated.py b/src/regenerated.py\n"
            "--- a/src/regenerated.py\n"
            "+++ b/src/regenerated.py\n"
            "@@ -0,0 +1,6 @@\n"
            "+def step_1(): pass\n"
            "+def step_2(): pass\n"
            "+def step_3(): pass\n"
            "+def step_4(): pass\n"
            "+def step_5(): pass\n"
            "+def step_6(): pass\n"
        )
        extracted_add = extract_drift_descriptors_from_diff(additive_rewrite_diff)
        self.assertEqual(len(extracted_add), 1)
        self.assertEqual(extracted_add[0].drift_kind, "whole_file_rewrite")

        rename_with_copy_lines = (
            "diff --git a/src/old_mod.py b/src/new_mod.py\n"
            "similarity index 90%\n"
            "rename from src/old_mod.py\n"
            "rename to src/new_mod.py\n"
            "copy from src/old_mod.py\n"
            "copy to src/new_mod.py\n"
            "--- a/src/old_mod.py\n"
            "+++ b/src/new_mod.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-x = 1\n"
            "+x = 2\n"
        )
        deduped = extract_drift_descriptors_from_diff(rename_with_copy_lines)
        self.assertEqual([d.drift_kind for d in deduped], ["file_rename"])

        # 2) RS-4DAC38: identical prose concern on an unrelated file/symbol must NOT
        #    collapse into the existing obligation!
        c_prior = _comment(
            "src/auth_a.py",
            line=10,
            body="Unsafe input handling allows unvalidated parameter injection.",
            symbol="parse_a",
            defect_kind="input_validation",
        )
        r_prior = evaluate_drift_round(
            _result(c_prior, paths=("src/auth_a.py",)),
            head_sha=HEAD_1,
            base_sha=BASE_SHA,
            max_active_findings=4,
        )
        c_unrelated = _comment(
            "src/unrelated_b.py",
            line=10,
            body="Unsafe input handling allows unvalidated parameter injection.",
            symbol="parse_b",
            defect_kind="input_validation",
        )
        r_distinct = evaluate_drift_round(
            _result(c_unrelated, paths=("src/unrelated_b.py",)),
            head_sha=HEAD_2,
            base_sha=BASE_SHA,
            max_active_findings=4,
            round_number=2,
            coverage_mode="incremental",
            prior_inventory=r_prior.inventory,
        )
        self.assertEqual(len(r_distinct.inventory.obligations), 2)
        states_by_path = {
            o.present_anchor.path: o.lifecycle_state
            for o in r_distinct.inventory.obligations
        }
        # RS-226427: src/auth_a.py was NOT in reviewed_set, so it stays active-unresolved!
        self.assertEqual(states_by_path["src/auth_a.py"], "active-unresolved")
        self.assertEqual(states_by_path["src/unrelated_b.py"], "active-unresolved")

        # RS-226427: fallback-full with partial coverage on unreviewed file ALSO stays
        # active-unresolved rather than omitted-uncertain!
        partial_fallback = evaluate_drift_round(
            _result(
                status="partial",
                coverage=_coverage("src/other.py", partial_path="src/auth_a.py"),
            ),
            head_sha=HEAD_2,
            base_sha=BASE_SHA,
            max_active_findings=4,
            round_number=2,
            coverage_mode="fallback-full",
            prior_inventory=r_prior.inventory,
        )
        self.assertEqual(
            partial_fallback.inventory.obligations[0].lifecycle_state,
            "active-unresolved",
        )

        # 3) RS-44D4B2 & RS-CD7439: AuthorizedRescan(mode="full") must NOT supersede
        #    an obligation whose target path was absent from reviewed_set, and must
        #    reject uncertain thread slots!
        r_unreviewed_rescan = evaluate_drift_round(
            _result(paths=("src/other_only.py",)),
            head_sha=HEAD_2,
            base_sha=BASE_SHA,
            max_active_findings=4,
            round_number=2,
            coverage_mode="full",
            prior_inventory=r_prior.inventory,
            authorized_rescan=AuthorizedRescan(
                kind="issue_comment",
                request_id=700,
                source=_published_transaction(HEAD_2),
                review_id=701,
                mode="full",
            ),
        )
        self.assertFalse(r_unreviewed_rescan.approved)
        self.assertEqual(
            r_unreviewed_rescan.inventory.obligations[0].lifecycle_state,
            "active-unresolved",
        )

        uncertain_ob = replace(
            r_prior.inventory.obligations[0],
            thread_binding=NativeThreadBinding(
                thread_id="uncertain-rescan-t",
                comment_id=999,
                placement=INLINE_PLACEMENT,
                body_reason=None,
                slot_state="uncertain",
                proof_sha256=_sha("unc"),
            ),
        )
        with self.assertRaisesRegex(
            ReviewInputError, "uncertain thread slot must reconcile before rescan"
        ):
            evaluate_drift_round(
                _result(paths=("src/auth_a.py",)),
                head_sha=HEAD_2,
                base_sha=BASE_SHA,
                max_active_findings=4,
                round_number=2,
                coverage_mode="full",
                prior_inventory=DriftInventory(obligations=(uncertain_ob,)),
                authorized_rescan=AuthorizedRescan(
                    kind="issue_comment",
                    request_id=702,
                    source=_published_transaction(HEAD_2),
                    review_id=703,
                    mode="full",
                ),
            )

        # 4) RS-827D99, RS-949993, RS-B6E862, RS-E3E9F4:
        #    Reopen parity, stale fix proof replacement, released challenger reopen,
        #    and host-thread resolve gated strictly on not item.unresolved!
        publisher = SyntheticNativeDriftPublisher()
        r_pub1, _ = publisher.publish_and_finalize(r_prior)
        ob_fixed_target = r_pub1.inventory.obligations[0]
        fix_proof = _sha("fix-auth-a")
        r_fixed, rec_fixed = publisher.publish_and_finalize(
            evaluate_drift_round(
                _result(paths=("src/auth_a.py",)),
                head_sha=HEAD_2,
                base_sha=BASE_SHA,
                max_active_findings=2,
                round_number=2,
                prior_inventory=r_pub1.inventory,
                confirmed_fixes=(
                    ConfirmedFixEvidence(
                        obligation_id=ob_fixed_target.obligation_id,
                        resolution_criterion=ob_fixed_target.criterion.resolution_criterion,
                        head_sha=HEAD_2,
                        evidence_sha256=fix_proof,
                        verified_paths=("src/auth_a.py",),
                    ),
                ),
            )
        )
        self.assertTrue(r_fixed.approved)
        self.assertEqual(rec_fixed.resolved_threads, 1)

        # Advisory comment cannot reopen a blocking verified-fixed obligation:
        advisory_reg = _comment(
            "src/auth_a.py",
            line=10,
            body="Minor advisory cleanup suggestion.",
            blocking=False,
            severity="low",
            symbol="parse_a",
            defect_kind="input_validation",
        )
        r_no_reopen = evaluate_drift_round(
            _result(advisory_reg, paths=("src/auth_a.py",)),
            head_sha=HEAD_3,
            base_sha=BASE_SHA,
            max_active_findings=2,
            round_number=3,
            prior_inventory=r_fixed.inventory,
        )
        self.assertEqual(
            r_no_reopen.inventory.obligations[0].lifecycle_state, "verified-fixed"
        )

        # Blocking regression reopens with fresh proof (replacing stale fix_proof),
        # emits thread_action == "reopen", rejects conflicting fix, and honors override!
        blocking_reg = _comment(
            "src/auth_a.py",
            line=14,
            body="Parameter injection reintroduced in parse_a.",
            blocking=True,
            severity="high",
            symbol="parse_a",
            defect_kind="input_validation",
        )
        with self.assertRaisesRegex(ReviewInputError, "regressed on current head"):
            evaluate_drift_round(
                _result(blocking_reg, paths=("src/auth_a.py",)),
                head_sha=HEAD_3,
                base_sha=BASE_SHA,
                max_active_findings=2,
                round_number=3,
                prior_inventory=r_fixed.inventory,
                confirmed_fixes=(
                    ConfirmedFixEvidence(
                        obligation_id=ob_fixed_target.obligation_id,
                        resolution_criterion=ob_fixed_target.criterion.resolution_criterion,
                        head_sha=HEAD_3,
                        evidence_sha256=_sha("contradictory-fix"),
                        verified_paths=("src/auth_a.py",),
                    ),
                ),
            )

        r_reopened = evaluate_drift_round(
            _result(blocking_reg, paths=("src/auth_a.py",)),
            head_sha=HEAD_3,
            base_sha=BASE_SHA,
            max_active_findings=2,
            round_number=3,
            prior_inventory=r_fixed.inventory,
        )
        reopened_ob = r_reopened.inventory.obligations[0]
        assert reopened_ob.thread_binding is not None
        self.assertNotEqual(reopened_ob.thread_binding.proof_sha256, fix_proof)
        self.assertEqual(r_reopened.active_entries[0].thread_action, "reopen")

        r_reopen_ovr = evaluate_drift_round(
            _result(blocking_reg, paths=("src/auth_a.py",)),
            head_sha=HEAD_3,
            base_sha=BASE_SHA,
            max_active_findings=2,
            round_number=3,
            prior_inventory=r_fixed.inventory,
            human_overrides=(
                AuthorizedHumanOverride(
                    obligation_id=ob_fixed_target.obligation_id,
                    action="accept-risk",
                    actor="sec-lead",
                    association="OWNER",
                    head_sha=HEAD_3,
                    reason="Validated upstream at API gateway.",
                    proof_sha256=_sha("reopen-ovr"),
                ),
            ),
        )
        self.assertTrue(r_reopen_ovr.approved)

        # RS-E3E9F4: If an unresolved obligation has slot_state == "released" and
        # does not fit in max_active_findings (entry is None), publish_and_finalize
        # must NEVER resolve its open host thread!
        incumbent_crit = _comment(
            "src/incumbent.py",
            line=2,
            body="Critical incumbent occupying the sole active slot.",
            severity="critical",
            symbol="crit_fn",
            defect_kind="rce",
        )
        r_cap1, _ = publisher.publish_and_finalize(
            evaluate_drift_round(
                _result(incumbent_crit, paths=("src/incumbent.py",)),
                head_sha=HEAD_3,
                base_sha=BASE_SHA,
                max_active_findings=1,
                round_number=3,
            )
        )
        # Force gh-drift-thread-1 open on host while reopened_ob is deferred (entry is None):
        publisher.threads["gh-drift-thread-1"] = replace(
            publisher.threads["gh-drift-thread-1"], resolved=False
        )
        combined_prior = DriftInventory(
            obligations=(
                r_cap1.inventory.obligations[0],
                r_fixed.inventory.obligations[0],
            )
        )
        r_deferred_reopen = evaluate_drift_round(
            _result(
                incumbent_crit,
                blocking_reg,
                paths=("src/incumbent.py", "src/auth_a.py"),
            ),
            head_sha=HEAD_4,
            base_sha=BASE_SHA,
            max_active_findings=1,
            round_number=4,
            prior_inventory=combined_prior,
        )
        _, rec_deferred = publisher.publish_and_finalize(r_deferred_reopen)
        self.assertEqual(rec_deferred.resolved_threads, 0)
        self.assertFalse(publisher.threads["gh-drift-thread-1"].resolved)


if __name__ == "__main__":
    unittest.main()
