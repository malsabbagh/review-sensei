"""Qualification tests for bounded active window and finding lifecycle (G10)."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from dataclasses import replace
from typing import Any

from review_sensei.active_finding_window import (
    PUBLIC_WINDOW_SCHEMA_VERSION,
    ActiveWindowApprovalEligibility,
    ActiveWindowEntry,
    ActiveWindowRecoveryStep,
    AuthoritativeDecisionInventory,
    AuthorizedCoverageAcceptance,
    AuthorizedHumanOverride,
    ChangedFileCoverageState,
    ConfirmedFixEvidence,
    DecisionProposal,
    DisplayActiveWindow,
    assert_no_deferred_leakage,
    baseline_from_active_window,
    commit_window_publication,
    evaluate_active_window_round,
    extract_decision_proposals,
    obligation_from_comment,
    reconcile_uncertain_slot,
    select_display_active_window,
)
from review_sensei.baseline import ReviewBaseline
from review_sensei.context import ReviewContextCacheKey, finding_lifecycle_for_comment
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.coverage import CoverageManifest, FileCoverage
from review_sensei.decision_configuration import DecisionConfiguration
from review_sensei.decision_provider import canonical
from review_sensei.errors import ReviewInputError
from review_sensei.models import (
    ProviderResponse,
    ReviewComment,
    ReviewRequest,
    ReviewResult,
    ReviewTransaction,
)
from review_sensei.outcomes import ResourceBudget, ResourceBudgetTracker
from review_sensei.publication_selection import PublishedSlot
from review_sensei.rescan import AuthorizedRescan
from review_sensei.review_decision import run_decisions
from review_sensei.review_decision_contract import (
    ReviewDecisionAnswer,
    ReviewDecisionResponse,
    strict_json,
)

BASE_SHA = "a" * 40
HEAD_1 = "1" * 40
HEAD_2 = "2" * 40
HEAD_3 = "3" * 40
HEAD_4 = "4" * 40
HEAD_5 = "5" * 40
HEAD_6 = "6" * 40


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _coverage(*paths: str, partial_path: str | None = None) -> CoverageManifest:
    all_paths = list(paths)
    files = [FileCoverage(path, "reviewed") for path in paths]
    if partial_path is not None:
        all_paths.append(partial_path)
        files.append(
            FileCoverage(
                partial_path, "partially-reviewed", reason="provider-call-budget"
            )
        )
    return CoverageManifest(
        files=tuple(files),
        enumerated_paths=tuple(all_paths),
        enumeration_complete=True,
    )


def _comment(
    path: str = "src/app.py",
    *,
    line: int = 10,
    body: str = "unchecked auth token",
    blocking: bool = True,
    needs_human: bool = False,
    severity: str = "high",
    symbol: str | None = "authenticate",
    defect_kind: str | None = "authz",
) -> ReviewComment:
    return ReviewComment(
        path=path,
        line=line,
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
    status: str = "complete",
    coverage: CoverageManifest | None = None,
) -> ReviewResult:
    cov = coverage if coverage is not None else _coverage("src/app.py", "src/db.py")
    return ReviewResult(
        summary="Synthetic bounded active-window review.",
        comments=comments,
        provider="fixture",
        review_status=status,
        coverage=cov,
    )


def _published_transaction(head_sha: str) -> ReviewTransaction:
    cfg = _sha("cfg")
    pol = _sha("policy")
    ev = _sha("evidence")
    resv = _sha("resv")
    return ReviewTransaction(
        transaction_id=ReviewTransaction.derive_id(
            repository="owner/repo",
            pull_request=42,
            base_sha=BASE_SHA,
            head_sha=head_sha,
            policy_digest=pol,
            configuration_digest=cfg,
            evidence_digest=ev,
            reservation_id=resv,
        ),
        reservation_id=resv,
        generation=1,
        phase="publication_succeeded",
        repository="owner/repo",
        pull_request=42,
        base_sha=BASE_SHA,
        head_sha=head_sha,
        configuration_digest=cfg,
        policy_digest=pol,
        evidence_digest=ev,
        result_sha256=_sha(f"res-{head_sha}"),
    )


class _StubReviewProvider:
    name = "fixture"
    model = "review-v1"

    def complete(self, request):
        packet = strict_json(request.prompt)
        assert isinstance(packet, dict)
        payload = {
            item["candidate_id"]: {
                "outcome": "supported",
                "evidence_sha256": [e["sha256"] for e in item["evidence"]],
            }
            for item in packet["candidates"]
        }
        return ProviderResponse(canonical(payload), self.name, self.model)


class _StubDecisionAdapter:
    name = "fixture"
    model = "decision-v1"

    def __init__(self, choice_map: dict[str, dict[str, str]]) -> None:
        self.choice_map = choice_map

    def evaluate(self, request, execution):
        raw_payload = canonical(request.to_dict())

        def send(_timeout: float, _max_bytes: int) -> str:
            out: dict[str, str] = {}
            for q in request.questions:
                cid = q.subject
                key = q.name.removeprefix(f"{cid}_")
                out[q.name] = self.choice_map[cid][key]
            return canonical(out)

        parsed = strict_json(execution.execute(raw_payload, send))
        assert isinstance(parsed, dict)
        response = ReviewDecisionResponse(
            request_sha256=request.digest(),
            provider=self.name,
            requested_model=self.model,
            actual_model=self.model,
            answers=tuple(
                ReviewDecisionAnswer(name=k, choice=v) for k, v in parsed.items()
            ),
        )
        response.validate(request)
        return response


class ActiveWindowLifecycleG10Tests(unittest.TestCase):
    def test_complete_coverage_with_omitted_prior_finding_withholds_approval(
        self,
    ) -> None:
        c1 = _comment(
            "src/app.py",
            line=10,
            body="SQL injection in query builder",
            severity="critical",
            symbol="build_query",
            defect_kind="sql_injection",
        )
        c2 = _comment(
            "src/db.py",
            line=20,
            body="Missing transaction rollback on timeout",
            severity="high",
            symbol="commit_tx",
            defect_kind="transaction",
        )
        round1 = evaluate_active_window_round(
            _result(c1, c2),
            head_sha=HEAD_1,
            base_sha=BASE_SHA,
            max_active_findings=1,
            round_number=1,
        )
        oid_1 = round1.display_active_findings.entries[0].obligation_id
        round1 = commit_window_publication(
            round1, created_targets={oid_1: "gh-thread-101"}
        )
        self.assertTrue(round1.changed_file_coverage.fully_reviewed)
        self.assertEqual(len(round1.authoritative_decision_inventory.obligations), 2)
        self.assertEqual(len(round1.display_active_findings.entries), 1)
        self.assertFalse(round1.approval_eligibility.approved)

        # Round 2: complete diff coverage on incremental pass, ZERO comments returned
        # by model, and NO ConfirmedFixEvidence supplied.
        round2_inc = evaluate_active_window_round(
            _result(),
            head_sha=HEAD_2,
            base_sha=BASE_SHA,
            max_active_findings=1,
            round_number=2,
            coverage_mode="incremental",
            prior_inventory=round1.authoritative_decision_inventory,
            slots=round1.display_active_findings.slots,
        )
        self.assertTrue(round2_inc.changed_file_coverage.fully_reviewed)
        self.assertEqual(round2_inc.changed_file_coverage.approval_state, "reviewed")
        self.assertTrue(
            round2_inc.authoritative_decision_inventory.has_omitted_uncertainty
        )
        self.assertFalse(round2_inc.approval_eligibility.approved)
        self.assertIn(
            "uncertain-finding-lifecycle",
            round2_inc.approval_eligibility.withheld_reasons,
        )
        # Omitted c1 retains its active slot; non-displayed c2 is NOT promoted.
        self.assertEqual(len(round2_inc.display_active_findings.entries), 1)
        self.assertEqual(
            round2_inc.display_active_findings.entries[0].obligation_id, oid_1
        )
        self.assertEqual(
            round2_inc.display_active_findings.entries[0].target_action, "reuse"
        )

        # Ordinary fallback-full WITHOUT AuthorizedRescan also preserves uncertainty!
        round2_fallback = evaluate_active_window_round(
            _result(),
            head_sha=HEAD_2,
            base_sha=BASE_SHA,
            max_active_findings=1,
            round_number=2,
            coverage_mode="fallback-full",
            prior_inventory=round1.authoritative_decision_inventory,
            slots=round1.display_active_findings.slots,
        )
        self.assertFalse(round2_fallback.approval_eligibility.approved)
        self.assertTrue(
            all(
                o.lifecycle_state == "omitted-uncertain"
                for o in round2_fallback.authoritative_decision_inventory.obligations
            )
        )

        # Prior obligation on an unreviewed path stays active-unresolved.
        partial_cov = _coverage("src/app.py")
        round2_unreviewed_path = evaluate_active_window_round(
            _result(coverage=partial_cov),
            head_sha=HEAD_2,
            base_sha=BASE_SHA,
            max_active_findings=1,
            round_number=2,
            coverage_mode="incremental",
            prior_inventory=round1.authoritative_decision_inventory,
            slots=round1.display_active_findings.slots,
        )
        by_path = {
            o.path: o.lifecycle_state
            for o in round2_unreviewed_path.authoritative_decision_inventory.obligations
        }
        self.assertEqual(by_path["src/app.py"], "omitted-uncertain")
        self.assertEqual(by_path["src/db.py"], "active-unresolved")

    def test_multi_round_slot_turnover_and_end_to_end_recovery_to_approval(
        self,
    ) -> None:
        comments = (
            _comment(
                "src/app.py",
                line=5,
                body="critical RCE in deserialization",
                severity="critical",
                symbol="loads_payload",
                defect_kind="rce",
            ),
            _comment(
                "src/app.py",
                line=15,
                body="high privilege escalation in role check",
                severity="high",
                symbol="check_role",
                defect_kind="authz",
            ),
            _comment(
                "src/db.py",
                line=25,
                body="medium connection leak on error",
                severity="medium",
                symbol="open_conn",
                defect_kind="resource_leak",
            ),
            _comment(
                "src/db.py",
                line=35,
                body="low unindexed lookup under contention",
                severity="low",
                symbol="lookup_item",
                defect_kind="perf",
            ),
        )
        cov = _coverage("src/app.py", "src/db.py")

        # Round 1: 4 obligations, max_active_findings=2 -> critical & high displayed.
        r1 = evaluate_active_window_round(
            _result(*comments, coverage=cov),
            head_sha=HEAD_1,
            base_sha=BASE_SHA,
            max_active_findings=2,
            round_number=1,
        )
        self.assertEqual(len(r1.display_active_findings.entries), 2)
        self.assertEqual(
            [e.impact for e in r1.display_active_findings.entries],
            ["critical", "high"],
        )
        by_symbol = {
            o.symbol: o for o in r1.authoritative_decision_inventory.obligations
        }
        r1 = commit_window_publication(
            r1,
            created_targets={
                by_symbol["loads_payload"].obligation_id: "thread-rce",
                by_symbol["check_role"].obligation_id: "thread-authz",
            },
        )

        # Verify public status NEVER leaks deferred count or deferred findings!
        pub1 = r1.to_public_status()
        self.assertEqual(pub1["schema_version"], PUBLIC_WINDOW_SCHEMA_VERSION)
        active_list = pub1["active_findings"]
        assert isinstance(active_list, list)
        self.assertEqual(len(active_list), 2)
        self.assertFalse(pub1["approval_eligible"])
        self.assertEqual(pub1["outcome"], "withheld")
        for forbidden in (
            "deferred",
            "deferred_count",
            "deferred_findings",
            "hidden_count",
            "remaining_count",
            "total_obligations",
        ):
            self.assertNotIn(forbidden, pub1)

        # Round 2: fix critical RCE -> frees thread-rce, reuses thread-authz,
        # and deterministically promotes medium (open_conn)!
        fix_rce = ConfirmedFixEvidence(
            obligation_id=by_symbol["loads_payload"].obligation_id,
            resolution_criterion=by_symbol["loads_payload"].resolution_criterion,
            head_sha=HEAD_2,
            evidence_sha256=_sha("fix-rce-proof"),
            verified_paths=("src/app.py",),
        )
        r2 = evaluate_active_window_round(
            _result(comments[1], comments[2], comments[3], coverage=cov),
            head_sha=HEAD_2,
            base_sha=BASE_SHA,
            max_active_findings=2,
            round_number=2,
            coverage_mode="incremental",
            prior_inventory=r1.authoritative_decision_inventory,
            slots=r1.display_active_findings.slots,
            confirmed_fixes=(fix_rce,),
        )
        self.assertEqual(len(r2.display_active_findings.entries), 2)
        self.assertEqual(
            [
                (e.native_target, e.target_action)
                for e in r2.display_active_findings.entries
            ],
            [("thread-authz", "reuse"), (None, "create")],
        )
        self.assertEqual(
            r2.display_active_findings.entries[1].obligation_id,
            by_symbol["open_conn"].obligation_id,
        )
        r2 = commit_window_publication(
            r2,
            created_targets={by_symbol["open_conn"].obligation_id: "thread-conn"},
        )

        # Round 3: override high authz with slot_released=False first (contingent):
        # obligation is overridden, but contingent slot holds physical occupancy!
        override_contingent = AuthorizedHumanOverride(
            obligation_id=by_symbol["check_role"].obligation_id,
            action="accept-risk",
            actor="lead-maintainer",
            association="OWNER",
            head_sha=HEAD_3,
            reason="Enforced upstream at gateway proxy layer.",
            proof_sha256=_sha("override-authz-contingent"),
            slot_released=False,
        )
        r3_contingent = evaluate_active_window_round(
            _result(comments[2], comments[3], coverage=cov),
            head_sha=HEAD_3,
            base_sha=BASE_SHA,
            max_active_findings=2,
            round_number=3,
            coverage_mode="incremental",
            prior_inventory=r2.authoritative_decision_inventory,
            slots=r2.display_active_findings.slots,
            human_overrides=(override_contingent,),
        )
        # Contingent slot prevents exceeding physical cap of 2 threads!
        self.assertEqual(len(r3_contingent.display_active_findings.entries), 1)
        self.assertFalse(r3_contingent.approval_eligibility.approved)

        # Now with confirmed native closure (slot_released=True), low promotes!
        override_released = replace(override_contingent, slot_released=True)
        r3 = evaluate_active_window_round(
            _result(comments[2], comments[3], coverage=cov),
            head_sha=HEAD_3,
            base_sha=BASE_SHA,
            max_active_findings=2,
            round_number=3,
            coverage_mode="incremental",
            prior_inventory=r2.authoritative_decision_inventory,
            slots=r2.display_active_findings.slots,
            human_overrides=(override_released,),
        )
        self.assertEqual(len(r3.display_active_findings.entries), 2)
        self.assertEqual(
            [
                (e.native_target, e.target_action)
                for e in r3.display_active_findings.entries
            ],
            [("thread-conn", "reuse"), (None, "create")],
        )
        self.assertEqual(
            r3.display_active_findings.entries[1].obligation_id,
            by_symbol["lookup_item"].obligation_id,
        )
        r3 = commit_window_publication(
            r3,
            created_targets={by_symbol["lookup_item"].obligation_id: "thread-perf"},
        )

        # Round 4: fix both remaining obligations (open_conn and lookup_item) -> APPROVED!
        fix_conn = ConfirmedFixEvidence(
            obligation_id=by_symbol["open_conn"].obligation_id,
            resolution_criterion=by_symbol["open_conn"].resolution_criterion,
            head_sha=HEAD_4,
            evidence_sha256=_sha("fix-conn"),
            verified_paths=("src/db.py",),
        )
        fix_perf = ConfirmedFixEvidence(
            obligation_id=by_symbol["lookup_item"].obligation_id,
            resolution_criterion=by_symbol["lookup_item"].resolution_criterion,
            head_sha=HEAD_4,
            evidence_sha256=_sha("fix-perf"),
            verified_paths=("src/db.py",),
        )
        r4 = evaluate_active_window_round(
            _result(coverage=cov),
            head_sha=HEAD_4,
            base_sha=BASE_SHA,
            max_active_findings=2,
            round_number=4,
            coverage_mode="incremental",
            prior_inventory=r3.authoritative_decision_inventory,
            slots=r3.display_active_findings.slots,
            confirmed_fixes=(fix_conn, fix_perf),
        )
        self.assertTrue(r4.approval_eligibility.approved)
        self.assertEqual(r4.approval_eligibility.outcome, "approved")
        self.assertEqual(r4.approval_eligibility.withheld_reasons, ())
        self.assertEqual(r4.display_active_findings.entries, ())
        # Full audit history of all 4 obligations is preserved!
        self.assertEqual(len(r4.authoritative_decision_inventory.obligations), 4)
        pub4 = r4.to_public_status()
        self.assertTrue(pub4["approval_eligible"])
        self.assertEqual(pub4["active_findings"], [])

        # Round 5: if RCE regresses later, it reopens thread-rce instead of minting new!
        r5 = evaluate_active_window_round(
            _result(comments[0], coverage=cov),
            head_sha=HEAD_5,
            base_sha=BASE_SHA,
            max_active_findings=2,
            round_number=5,
            coverage_mode="incremental",
            prior_inventory=r4.authoritative_decision_inventory,
            slots=r4.display_active_findings.slots,
        )
        self.assertFalse(r5.approval_eligibility.approved)
        self.assertEqual(len(r5.display_active_findings.entries), 1)
        self.assertEqual(
            (
                r5.display_active_findings.entries[0].native_target,
                r5.display_active_findings.entries[0].target_action,
            ),
            ("thread-rce", "reopen"),
        )
        r5_committed = commit_window_publication(r5)
        self.assertEqual(
            r5_committed.display_active_findings.entries[0].target_action, "reuse"
        )

    def test_duplicate_and_ambiguous_identities_hold_approval_and_recover(
        self,
    ) -> None:
        prior = _comment(
            "src/app.py",
            line=10,
            body="missing auth check on endpoint",
            symbol="handle_req",
            defect_kind="authz",
        )
        r1 = evaluate_active_window_round(
            _result(prior),
            head_sha=HEAD_1,
            base_sha=BASE_SHA,
            max_active_findings=3,
            round_number=1,
        )
        prior_id = r1.authoritative_decision_inventory.obligations[0].obligation_id

        # Two distinct comments in Round 2 both match the same prior (path, symbol, kind).
        dup_a = _comment(
            "src/app.py",
            line=12,
            body="auth check bypassed for admin flag",
            symbol="handle_req",
            defect_kind="authz",
        )
        dup_b = _comment(
            "src/app.py",
            line=28,
            body="auth check bypassed for guest token",
            symbol="handle_req",
            defect_kind="authz",
        )
        r2 = evaluate_active_window_round(
            _result(dup_a, dup_b),
            head_sha=HEAD_2,
            base_sha=BASE_SHA,
            max_active_findings=3,
            round_number=2,
            coverage_mode="incremental",
            prior_inventory=r1.authoritative_decision_inventory,
        )
        self.assertFalse(r2.approval_eligibility.approved)
        self.assertTrue(r2.authoritative_decision_inventory.has_ambiguous_identity)
        self.assertIn(
            "ambiguous-finding-identity",
            r2.approval_eligibility.withheld_reasons,
        )
        routes = {s.route for s in r2.approval_eligibility.recovery_steps}
        self.assertTrue({"override", "accept-risk", "rescan"}.issubset(routes))
        self.assertEqual(prior_id, r1.display_active_findings.entries[0].obligation_id)

        # Also test model attempting silent blocking->non-blocking downgrade!
        downgraded = _comment(
            "src/app.py",
            line=10,
            body="minor style note on auth check",
            blocking=False,
            severity="low",
            symbol="handle_req",
            defect_kind="authz",
        )
        r2_downgrade = evaluate_active_window_round(
            _result(downgraded),
            head_sha=HEAD_2,
            base_sha=BASE_SHA,
            max_active_findings=3,
            round_number=2,
            coverage_mode="incremental",
            prior_inventory=r1.authoritative_decision_inventory,
        )
        self.assertFalse(r2_downgrade.approval_eligibility.approved)
        preserved = r2_downgrade.authoritative_decision_inventory.obligations[0]
        self.assertTrue(preserved.blocking)
        self.assertEqual(preserved.lifecycle_state, "omitted-uncertain")

        # Recover from ambiguous state via explicit AuthorizedRescan(mode="full")!
        rescan = AuthorizedRescan(
            kind="issue_comment",
            request_id=9001,
            source=_published_transaction(HEAD_3),
            review_id=501,
            mode="full",
        )
        # Remaining-mode rescan does NOT clear omitted/ambiguous obligations:
        rescan_remaining = replace(rescan, mode="remaining")
        r3_rem = evaluate_active_window_round(
            _result(),
            head_sha=HEAD_3,
            base_sha=BASE_SHA,
            max_active_findings=3,
            round_number=3,
            coverage_mode="full",
            prior_inventory=r2.authoritative_decision_inventory,
            authorized_rescan=rescan_remaining,
        )
        self.assertFalse(r3_rem.approval_eligibility.approved)

        # Without AuthorizedRescan, full coverage + zero comments still withholds approval!
        r3_no_rescan = evaluate_active_window_round(
            _result(),
            head_sha=HEAD_3,
            base_sha=BASE_SHA,
            max_active_findings=3,
            round_number=3,
            coverage_mode="full",
            prior_inventory=r2.authoritative_decision_inventory,
        )
        self.assertFalse(r3_no_rescan.approval_eligibility.approved)

        # Full-mode rescan with complete coverage supersedes ambiguous/omitted obligations:
        r3_full = evaluate_active_window_round(
            _result(),
            head_sha=HEAD_3,
            base_sha=BASE_SHA,
            max_active_findings=3,
            round_number=3,
            coverage_mode="full",
            prior_inventory=r2.authoritative_decision_inventory,
            authorized_rescan=rescan,
        )
        self.assertTrue(r3_full.approval_eligibility.approved)
        self.assertTrue(
            all(
                o.lifecycle_state == "rescan-superseded"
                for o in r3_full.authoritative_decision_inventory.obligations
            )
        )

    def test_pr427_decision_proposals_never_drop_or_clear_obligations(self) -> None:
        diff_text = (
            "diff --git a/src/app.py b/src/app.py\n"
            "--- a/src/app.py\n+++ b/src/app.py\n"
            "@@ -1,1 +1,3 @@\n keep\n+line_two\n+line_three\n"
        )
        c_first = _comment(
            "src/app.py",
            line=2,
            body="blocking defect alpha",
            severity="high",
            symbol="alpha",
            defect_kind="logic",
        )
        c_second = _comment(
            "src/app.py",
            line=3,
            body="blocking defect beta",
            severity="high",
            symbol="beta",
            defect_kind="logic",
        )
        req = ReviewRequest(
            diff=diff_text,
            repository="owner/repo",
            pull_request_number=77,
            base_sha=BASE_SHA,
            head_sha=HEAD_1,
            model="review-v1",
        )
        raw_result = _result(c_first, c_second, coverage=_coverage("src/app.py"))
        # Decision adapter tries to mark c0000 as optional_improvement/human_review
        # and c0001 as supported behavioral_defect/continue!
        adapter = _StubDecisionAdapter(
            {
                "c0000": {
                    "kind": "optional_improvement",
                    "evidence": "contradicted",
                    "next": "continue",
                    "duplicate": "same_concern",
                },
                "c0001": {
                    "kind": "behavioral_defect",
                    "evidence": "supported",
                    "next": "verify_further",
                    "duplicate": "different_concern",
                },
            }
        )
        tracker = ResourceBudgetTracker(ResourceBudget.create(max_provider_calls=6))
        triaged = run_decisions(
            req,
            raw_result,
            provider=adapter,
            review_provider=_StubReviewProvider(),  # type: ignore[arg-type]
            settings=DecisionConfiguration(
                enabled=True,
                provider="fixture",
                model="decision-v1",
                base_url="http://127.0.0.1:8080/v1",
            ),
            tracker=tracker,
        )
        proposals = extract_decision_proposals(triaged)
        self.assertEqual(len(proposals), 2)
        self.assertLess(proposals[1].proposal_rank, proposals[0].proposal_rank)

        # Evaluate with max_active_findings=1:
        # 1) c_second wins intra-tier tiebreak (rank 10 < rank 30),
        # 2) c_first is NOT dropped or cleared despite decision model saying
        #    optional_improvement + contradicted!
        # 3) review_status became "partial" because c0000 required human_review,
        #    and BOTH obligations remain blocking in authoritative_decision_inventory!
        state = evaluate_active_window_round(
            triaged,
            head_sha=HEAD_1,
            base_sha=BASE_SHA,
            max_active_findings=1,
            round_number=1,
        )
        self.assertEqual(
            len(state.authoritative_decision_inventory.blocking_obligations), 2
        )
        self.assertEqual(len(state.display_active_findings.entries), 1)
        self.assertEqual(
            state.display_active_findings.entries[0].fingerprint,
            finding_lifecycle_for_comment(c_second).fingerprint,
        )
        self.assertFalse(state.approval_eligibility.approved)

        # Tampered decision_pass inventory_sha256 fails closed.
        assert isinstance(triaged.decision_pass, dict)
        tampered_receipt = copy.deepcopy(triaged.decision_pass)
        tampered_receipt["inventory_sha256"] = "f" * 64
        with self.assertRaisesRegex(ReviewInputError, "inventory_sha256"):
            extract_decision_proposals(replace(triaged, decision_pass=tampered_receipt))

    def test_uncertain_slots_partial_coverage_baseline_and_guardrails(self) -> None:
        c1 = _comment("src/app.py", line=4, body="race condition in cache")
        partial_cov = _coverage("src/app.py", partial_path="src/db.py")
        r1 = evaluate_active_window_round(
            _result(c1, status="partial", coverage=partial_cov),
            head_sha=HEAD_1,
            base_sha=BASE_SHA,
            max_active_findings=2,
            round_number=1,
        )
        self.assertIn("coverage-partial", r1.approval_eligibility.withheld_reasons)
        oid = r1.display_active_findings.entries[0].obligation_id

        # Simulate lost HTTP response during thread creation -> uncertain slot!
        r1_uncertain = commit_window_publication(
            r1, uncertain_targets={oid: "pending-thread-1"}
        )
        self.assertTrue(r1_uncertain.display_active_findings.has_uncertain_slots)
        self.assertIn(
            "uncertain-native-slot",
            r1_uncertain.approval_eligibility.withheld_reasons,
        )
        # Reconcile uncertain slot via owned readback to active:
        r1_reconciled = reconcile_uncertain_slot(
            r1_uncertain,
            target="pending-thread-1",
            resolved_state="active",
            proof_sha256=_sha("readback-ok"),
        )
        self.assertFalse(r1_reconciled.display_active_findings.has_uncertain_slots)
        self.assertEqual(
            r1_reconciled.display_active_findings.entries[0].target_action, "reuse"
        )

        # Override c1 + accept partial coverage uncertainty -> recovers to approved!
        override = AuthorizedHumanOverride(
            obligation_id=oid,
            action="override",
            actor="maintainer-1",
            association="MEMBER",
            head_sha=HEAD_2,
            reason="Cache is process-local and single-threaded.",
            proof_sha256=_sha("ovr-1"),
        )
        cov_accept = AuthorizedCoverageAcceptance(
            actor="maintainer-1",
            association="MEMBER",
            head_sha=HEAD_2,
            reason="src/db.py change is mechanical comment formatting.",
            proof_sha256=_sha("cov-accept-1"),
        )
        r2 = evaluate_active_window_round(
            _result(status="partial", coverage=partial_cov),
            head_sha=HEAD_2,
            base_sha=BASE_SHA,
            max_active_findings=2,
            round_number=2,
            coverage_mode="incremental",
            prior_inventory=r1_reconciled.authoritative_decision_inventory,
            slots=r1_reconciled.display_active_findings.slots,
            human_overrides=(override,),
            coverage_acceptance=cov_accept,
        )
        self.assertTrue(r2.approval_eligibility.approved)

        # Project round state back into a ReviewBaseline cleanly.
        key = ReviewContextCacheKey(
            repository="owner/repo",
            pull_request=42,
            base_sha=BASE_SHA,
            head_sha=HEAD_1,
            engine="fixture",
            model="review-v1",
            profile="default",
            stage_digest=_sha("stage"),
            context_digest=_sha("ctx"),
            learning_digest=_sha("learn"),
        )
        template = ReviewBaseline(
            cache_key=key,
            policy_digest=ReviewConvergencePolicy(mode="merge-focused").digest(),
            complete=True,
            reviewed_paths=("src/app.py",),
        )
        projected = baseline_from_active_window(
            r1_reconciled, baseline_template=template
        )
        self.assertEqual(len(projected.findings), 1)
        self.assertIsNotNone(r1_reconciled.authoritative_decision_inventory.digest())

        # Deferred leakage detector rejects forbidden keys anywhere in nested dict.
        bad_public = r2.to_public_status()
        bad_public["deferred_count"] = 3
        with self.assertRaisesRegex(ReviewInputError, "deferred"):
            assert_no_deferred_leakage(bad_public)

        # Unauthorized collaborator (WRITE/COLLABORATOR without maintain/admin) cannot override!
        with self.assertRaisesRegex(ReviewInputError, "maintainer or admin"):
            AuthorizedHumanOverride(
                obligation_id=oid,
                action="override",
                actor="outside-collab",
                association="COLLABORATOR",
                permission="write",
                head_sha=HEAD_2,
                reason="Attempting unauthorized override.",
                proof_sha256=_sha("unauth"),
            )

    def test_validation_error_boundaries(self) -> None:
        c_opt = _comment(
            "src/app.py",
            line=1,
            body="optional naming nit",
            blocking=False,
            severity="low",
        )
        ob = obligation_from_comment(c_opt)
        self.assertFalse(ob.required)
        self.assertFalse(ob.blocks_approval_now)

        with self.assertRaises(ReviewInputError):
            DecisionProposal(
                candidate_id="c0000",
                fingerprint="bad",
                proposed_action="continue",
                verification="supported",
                suspected_duplicate=False,
                proposal_rank=10,
            )
        with self.assertRaises(ReviewInputError):
            ChangedFileCoverageState(
                coverage_mode="full",
                approval_state="incomplete",
                enumeration_complete=False,
                fully_reviewed=False,
                enumerated_paths=(),
                reviewed_paths=(),
                coverage_uncertainty_accepted=True,
                acceptance_proof_sha256=_sha("p"),
            )
        with self.assertRaises(ReviewInputError):
            AuthoritativeDecisionInventory(obligations=(ob, ob))
        with self.assertRaises(ReviewInputError):
            ActiveWindowEntry(
                obligation_id=ob.obligation_id,
                fingerprint=ob.fingerprint,
                path="src/app.py",
                line=1,
                side="RIGHT",
                required=False,
                impact=None,
                lifecycle_state="active-unresolved",
                native_target="unexpected-target",
                target_action="create",
                consumes_new_slot=True,
            )
        with self.assertRaises(ReviewInputError):
            ActiveWindowApprovalEligibility(
                approved=True,
                outcome="approved",
                withheld_reasons=("coverage-incomplete",),
                recovery_steps=(),
            )
        with self.assertRaises(ReviewInputError):
            ActiveWindowRecoveryStep(
                route="unknown",
                reason_code="coverage-incomplete",
                target_scope="pull-request",
            )
        slot = PublishedSlot("t1", "active", _sha("s1"))
        with self.assertRaises(ReviewInputError):
            DisplayActiveWindow(
                max_active_findings=1,
                entries=(),
                slots=(slot, slot),
            )
        with self.assertRaises(ReviewInputError):
            select_display_active_window(
                AuthoritativeDecisionInventory(obligations=(ob,)),
                slots=(slot, PublishedSlot("t2", "active", _sha("s2"))),
                max_active_findings=1,
            )

    def test_matching_tiers_incomplete_coverage_and_publication_edges(self) -> None:
        # 1) Incomplete coverage (coverage=None) -> coverage-incomplete + rescan route
        no_cov = ReviewResult(
            summary="No coverage manifest.",
            comments=(),
            provider="fixture",
            review_status="incomplete",
            coverage=None,
        )
        r_inc = evaluate_active_window_round(
            no_cov,
            head_sha=HEAD_1,
            base_sha=BASE_SHA,
            max_active_findings=2,
        )
        self.assertFalse(r_inc.approval_eligibility.approved)
        self.assertIn(
            "coverage-incomplete", r_inc.approval_eligibility.withheld_reasons
        )
        self.assertIn("review-incomplete", r_inc.approval_eligibility.withheld_reasons)

        # 2) Multi-prior ambiguity (two prior obligations share exact (path, symbol, kind))
        c_base = _comment(
            "src/app.py",
            line=10,
            body="missing CSRF check on form A",
            symbol="submit_form",
            defect_kind="csrf",
        )
        ob_a = obligation_from_comment(c_base, obligation_id=_sha("ob-a"))
        ob_b = obligation_from_comment(
            _comment(
                "src/app.py",
                line=40,
                body="missing CSRF check on form B",
                symbol="submit_form",
                defect_kind="csrf",
            ),
            obligation_id=_sha("ob-b"),
        )
        prior_inv = AuthoritativeDecisionInventory(obligations=(ob_a, ob_b))
        r_multi = evaluate_active_window_round(
            _result(c_base),
            head_sha=HEAD_2,
            base_sha=BASE_SHA,
            max_active_findings=3,
            round_number=2,
            coverage_mode="incremental",
            prior_inventory=prior_inv,
        )
        self.assertTrue(r_multi.authoritative_decision_inventory.has_ambiguous_identity)
        self.assertFalse(r_multi.approval_eligibility.approved)

        # 3) Exact (path, symbol, kind) match when body/fingerprint changes, and
        # path+defect_kind fallback match when symbol is omitted on round 2:
        ob_sym = obligation_from_comment(
            _comment(
                "src/app.py",
                line=10,
                body="original wording for timeout bug",
                symbol="fetch_remote",
                defect_kind="timeout",
            )
        )
        ob_nosym = obligation_from_comment(
            _comment(
                "src/db.py",
                line=12,
                body="original wording for deadlock bug",
                symbol="lock_row",
                defect_kind="deadlock",
            )
        )
        r_tier = evaluate_active_window_round(
            _result(
                _comment(
                    "src/app.py",
                    line=14,
                    body="reworded description for timeout bug",
                    symbol="fetch_remote",
                    defect_kind="timeout",
                ),
                _comment(
                    "src/db.py",
                    line=18,
                    body="completely rewritten deadlock description",
                    symbol=None,
                    defect_kind="deadlock",
                ),
            ),
            head_sha=HEAD_2,
            base_sha=BASE_SHA,
            max_active_findings=2,
            round_number=2,
            coverage_mode="incremental",
            prior_inventory=AuthoritativeDecisionInventory(
                obligations=(ob_sym, ob_nosym)
            ),
        )
        self.assertEqual(len(r_tier.authoritative_decision_inventory.obligations), 2)
        self.assertEqual(
            {
                o.obligation_id
                for o in r_tier.authoritative_decision_inventory.obligations
            },
            {ob_sym.obligation_id, ob_nosym.obligation_id},
        )

        # 4) Commit publication + uncertain reconciliation to 'released' + rescan slot release:
        r_pub = commit_window_publication(
            r_tier,
            created_targets={ob_sym.obligation_id: "t-sym"},
            uncertain_targets={ob_nosym.obligation_id: "t-nosym"},
        )
        # Cannot release uncertain slot via fix or override before reconciling!
        with self.assertRaisesRegex(ReviewInputError, "uncertain native slot"):
            evaluate_active_window_round(
                _result(),
                head_sha=HEAD_3,
                base_sha=BASE_SHA,
                max_active_findings=2,
                round_number=3,
                prior_inventory=r_pub.authoritative_decision_inventory,
                slots=r_pub.display_active_findings.slots,
                confirmed_fixes=(
                    ConfirmedFixEvidence(
                        obligation_id=ob_nosym.obligation_id,
                        resolution_criterion=ob_nosym.resolution_criterion,
                        head_sha=HEAD_3,
                        evidence_sha256=_sha("fix-nosym"),
                        verified_paths=("src/db.py",),
                    ),
                ),
            )
        with self.assertRaisesRegex(ReviewInputError, "uncertain native slot"):
            evaluate_active_window_round(
                _result(),
                head_sha=HEAD_3,
                base_sha=BASE_SHA,
                max_active_findings=2,
                round_number=3,
                prior_inventory=r_pub.authoritative_decision_inventory,
                slots=r_pub.display_active_findings.slots,
                human_overrides=(
                    AuthorizedHumanOverride(
                        obligation_id=ob_nosym.obligation_id,
                        action="dismiss",
                        actor="admin-1",
                        association="OWNER",
                        head_sha=HEAD_3,
                        reason="Verified false positive after manual audit.",
                        proof_sha256=_sha("ovr-nosym"),
                    ),
                ),
            )
        # Reconcile uncertain slot as 'released' (creation never landed):
        r_rel = reconcile_uncertain_slot(
            r_pub,
            target="t-nosym",
            resolved_state="released",
            proof_sha256=_sha("no-thread-created"),
        )
        self.assertEqual(len(r_rel.display_active_findings.entries), 2)
        # Wrong fix criterion fails closed:
        with self.assertRaisesRegex(ReviewInputError, "criterion does not match"):
            evaluate_active_window_round(
                _result(),
                head_sha=HEAD_3,
                base_sha=BASE_SHA,
                max_active_findings=2,
                round_number=3,
                prior_inventory=r_rel.authoritative_decision_inventory,
                slots=r_rel.display_active_findings.slots,
                confirmed_fixes=(
                    ConfirmedFixEvidence(
                        obligation_id=ob_sym.obligation_id,
                        resolution_criterion=_sha("wrong-criterion"),
                        head_sha=HEAD_3,
                        evidence_sha256=_sha("bad-fix"),
                        verified_paths=("src/app.py",),
                    ),
                ),
            )
        # Full AuthorizedRescan releases active slot 't-sym' cleanly:
        r_rescan = evaluate_active_window_round(
            _result(),
            head_sha=HEAD_3,
            base_sha=BASE_SHA,
            max_active_findings=2,
            round_number=3,
            coverage_mode="full",
            prior_inventory=r_rel.authoritative_decision_inventory,
            slots=r_rel.display_active_findings.slots,
            authorized_rescan=AuthorizedRescan(
                kind="workflow_dispatch",
                request_id=77,
                source=_published_transaction(HEAD_3),
                review_id=88,
                mode="full",
            ),
        )
        self.assertTrue(r_rescan.approval_eligibility.approved)
        self.assertTrue(
            all(s.state == "released" for s in r_rescan.display_active_findings.slots)
        )

        # 5) Publication receipt error guardrails:
        with self.assertRaisesRegex(ReviewInputError, "both confirmed and uncertain"):
            commit_window_publication(
                r_tier,
                created_targets={ob_sym.obligation_id: "x"},
                uncertain_targets={ob_sym.obligation_id: "x"},
            )
        with self.assertRaisesRegex(ReviewInputError, "non-displayed"):
            commit_window_publication(r_tier, created_targets={_sha("unknown"): "x"})
        with self.assertRaisesRegex(ReviewInputError, "create actions"):
            commit_window_publication(
                r_rel, created_targets={ob_sym.obligation_id: "dup"}
            )
        with self.assertRaisesRegex(ReviewInputError, "collides"):
            commit_window_publication(
                r_rel, created_targets={ob_nosym.obligation_id: "t-sym"}
            )
        with self.assertRaisesRegex(ReviewInputError, "unselected"):
            assert_no_deferred_leakage({"nested": [{"unselected_items": 1}]})

    def test_remaining_contract_validation_branches(self) -> None:
        base_ob = obligation_from_comment(
            _comment("src/app.py", line=10, body="valid base obligation")
        )
        # ConfirmedFixEvidence / Override / CoverageAcceptance guardrails:
        with self.assertRaises(ReviewInputError):
            ConfirmedFixEvidence(
                obligation_id=base_ob.obligation_id,
                resolution_criterion=base_ob.resolution_criterion,
                head_sha="not-a-sha",
                evidence_sha256=_sha("e"),
                verified_paths=("src/app.py",),
            )
        with self.assertRaises(ReviewInputError):
            ConfirmedFixEvidence(
                obligation_id=base_ob.obligation_id,
                resolution_criterion=base_ob.resolution_criterion,
                head_sha=HEAD_1,
                evidence_sha256=_sha("e"),
                verified_paths=(),
            )
        with self.assertRaises(ReviewInputError):
            AuthorizedHumanOverride(
                obligation_id=base_ob.obligation_id,
                action="invalid",
                actor="admin",
                association="OWNER",
                head_sha=HEAD_1,
                reason="Valid reason text.",
                proof_sha256=_sha("p"),
            )
        with self.assertRaises(ReviewInputError):
            AuthorizedHumanOverride(
                obligation_id=base_ob.obligation_id,
                action="override",
                actor="  ",
                association="OWNER",
                head_sha=HEAD_1,
                reason="Valid reason text.",
                proof_sha256=_sha("p"),
            )
        with self.assertRaises(ReviewInputError):
            AuthorizedCoverageAcceptance(
                actor="",
                association="OWNER",
                head_sha=HEAD_1,
                reason="Valid reason text.",
                proof_sha256=_sha("p"),
            )
        with self.assertRaises(ReviewInputError):
            AuthorizedCoverageAcceptance(
                actor="collab",
                association="COLLABORATOR",
                permission="write",
                head_sha=HEAD_1,
                reason="Valid reason text.",
                proof_sha256=_sha("p"),
            )
        with self.assertRaises(ReviewInputError):
            DecisionProposal(
                candidate_id="c0000",
                fingerprint=base_ob.fingerprint,
                proposed_action="bad",
                verification="supported",
                suspected_duplicate=False,
                proposal_rank=10,
            )
        with self.assertRaises(ReviewInputError):
            DecisionProposal(
                candidate_id="c0000",
                fingerprint=base_ob.fingerprint,
                proposed_action="continue",
                verification="bad",
                suspected_duplicate=False,
                proposal_rank=10,
            )
        with self.assertRaises(ReviewInputError):
            DecisionProposal(
                candidate_id="c0000",
                fingerprint=base_ob.fingerprint,
                proposed_action="continue",
                verification="supported",
                suspected_duplicate=False,
                proposal_rank=101,
            )
        # ChangedFileCoverageState guardrails:
        with self.assertRaises(ReviewInputError):
            ChangedFileCoverageState(
                coverage_mode="invalid",
                approval_state="reviewed",
                enumeration_complete=True,
                fully_reviewed=True,
                enumerated_paths=(),
                reviewed_paths=(),
            )
        with self.assertRaises(ReviewInputError):
            ChangedFileCoverageState(
                coverage_mode="full",
                approval_state="invalid",
                enumeration_complete=True,
                fully_reviewed=False,
                enumerated_paths=(),
                reviewed_paths=(),
            )
        with self.assertRaises(ReviewInputError):
            ChangedFileCoverageState(
                coverage_mode="full",
                approval_state="partial",
                enumeration_complete=True,
                fully_reviewed=True,
                enumerated_paths=(),
                reviewed_paths=(),
            )
        with self.assertRaises(ReviewInputError):
            ChangedFileCoverageState(
                coverage_mode="full",
                approval_state="reviewed",
                enumeration_complete=True,
                fully_reviewed=True,
                enumerated_paths=(),
                reviewed_paths=(),
                acceptance_proof_sha256=_sha("unexpected"),
            )
        with self.assertRaises(ReviewInputError):
            ChangedFileCoverageState.from_manifest(
                _coverage("src/app.py"),
                head_sha=HEAD_1,
                coverage_acceptance=AuthorizedCoverageAcceptance(
                    actor="admin",
                    association="OWNER",
                    head_sha=HEAD_1,
                    reason="Not partial.",
                    proof_sha256=_sha("p"),
                ),
            )
        with self.assertRaises(ReviewInputError):
            ChangedFileCoverageState.from_manifest(
                _coverage("src/app.py", partial_path="src/db.py"),
                head_sha=HEAD_2,
                coverage_acceptance=AuthorizedCoverageAcceptance(
                    actor="admin",
                    association="OWNER",
                    head_sha=HEAD_1,
                    reason="Mismatched head.",
                    proof_sha256=_sha("p"),
                ),
            )
        # AuthoritativeObligation field guardrails:
        for bad_kwargs in (
            {"line": 0},
            {"side": "MIDDLE"},
            {"symbol": "   "},
            {"defect_kind": ""},
            {"blocking": "yes"},
            {"impact": "extreme"},
            {"evidence": "rumor"},
            {"evidence": "advisory", "blocking": True},
            {"lifecycle_state": "dropped"},
            {"generation": -1},
            {"proposal_rank": -1},
            {"native_target": ""},
            {
                "native_target": "t-1",
                "target_state": "bogus",
                "target_proof_sha256": _sha("p"),
            },
            {"target_state": "active"},
            {"resolution_proof_sha256": _sha("unexpected")},
        ):
            with self.assertRaises(ReviewInputError):
                replace(base_ob, **bad_kwargs)

        # Duplicate native_target across unresolved obligations fails closed:
        ob_t1 = replace(
            base_ob,
            native_target="shared-thread",
            target_state="active",
            target_proof_sha256=_sha("t"),
        )
        ob_t2 = replace(
            base_ob,
            obligation_id=_sha("other-id"),
            native_target="shared-thread",
            target_state="active",
            target_proof_sha256=_sha("t"),
        )
        with self.assertRaises(ReviewInputError):
            AuthoritativeDecisionInventory(obligations=(ob_t1, ob_t2))
        with self.assertRaises(ReviewInputError):
            select_display_active_window(
                AuthoritativeDecisionInventory(obligations=(ob_t1,)),
                slots=(),
                max_active_findings=2,
            )

        # evaluate_active_window_round stale/duplicate input guardrails:
        r1 = evaluate_active_window_round(
            _result(_comment("src/app.py", line=10)),
            head_sha=HEAD_1,
            base_sha=BASE_SHA,
            max_active_findings=2,
        )
        oid = r1.authoritative_decision_inventory.obligations[0].obligation_id
        crit = r1.authoritative_decision_inventory.obligations[0].resolution_criterion
        fix_ok = ConfirmedFixEvidence(
            obligation_id=oid,
            resolution_criterion=crit,
            head_sha=HEAD_2,
            evidence_sha256=_sha("f"),
            verified_paths=("src/app.py",),
        )
        with self.assertRaises(ReviewInputError):
            evaluate_active_window_round(
                _result(),
                head_sha=HEAD_2,
                base_sha=BASE_SHA,
                max_active_findings=2,
                prior_inventory=r1.authoritative_decision_inventory,
                confirmed_fixes=(fix_ok, fix_ok),
            )
        with self.assertRaises(ReviewInputError):
            evaluate_active_window_round(
                _result(),
                head_sha=HEAD_2,
                base_sha=BASE_SHA,
                max_active_findings=2,
                prior_inventory=r1.authoritative_decision_inventory,
                confirmed_fixes=(replace(fix_ok, head_sha=HEAD_1),),
            )
        with self.assertRaises(ReviewInputError):
            evaluate_active_window_round(
                _result(),
                head_sha=HEAD_2,
                base_sha=BASE_SHA,
                max_active_findings=2,
                confirmed_fixes=(fix_ok,),
            )
        with self.assertRaises(ReviewInputError):
            evaluate_active_window_round(
                _result(),
                head_sha=HEAD_2,
                base_sha=BASE_SHA,
                max_active_findings=2,
                authorized_rescan=AuthorizedRescan(
                    kind="issue_comment",
                    request_id=1,
                    source=_published_transaction(HEAD_1),
                    review_id=2,
                ),
            )
        with self.assertRaises(ReviewInputError):
            reconcile_uncertain_slot(
                r1,
                target="missing",
                resolved_state="active",
                proof_sha256=_sha("p"),
            )

    def test_reopen_parity_override_and_slot_uniqueness_guards(self) -> None:
        c_block = _comment(
            "src/app.py",
            line=10,
            body="Blocking token bypass in login handler.",
            blocking=True,
            severity="critical",
            symbol="login_user",
            defect_kind="authn",
        )
        r1 = commit_window_publication(
            evaluate_active_window_round(
                _result(c_block),
                head_sha=HEAD_1,
                base_sha=BASE_SHA,
                max_active_findings=2,
            ),
            created_targets={
                obligation_from_comment(c_block).obligation_id: "thread-login"
            },
        )
        ob1 = r1.authoritative_decision_inventory.obligations[0]
        fix_proof = _sha("fix-login-1")
        r2 = evaluate_active_window_round(
            _result(),
            head_sha=HEAD_2,
            base_sha=BASE_SHA,
            max_active_findings=2,
            round_number=2,
            prior_inventory=r1.authoritative_decision_inventory,
            slots=r1.display_active_findings.slots,
            confirmed_fixes=(
                ConfirmedFixEvidence(
                    obligation_id=ob1.obligation_id,
                    resolution_criterion=ob1.resolution_criterion,
                    head_sha=HEAD_2,
                    evidence_sha256=fix_proof,
                    verified_paths=("src/app.py",),
                ),
            ),
        )
        self.assertTrue(r2.approval_eligibility.approved)

        # 1) RS-827D99: Advisory comment cannot reopen a verified-fixed blocking obligation:
        c_advisory = _comment(
            "src/app.py",
            line=10,
            body="Optional docstring tweak on login_user.",
            blocking=False,
            severity="low",
            symbol="login_user",
            defect_kind="authn",
        )
        r3_adv = evaluate_active_window_round(
            _result(c_advisory),
            head_sha=HEAD_3,
            base_sha=BASE_SHA,
            max_active_findings=2,
            round_number=3,
            prior_inventory=r2.authoritative_decision_inventory,
            slots=r2.display_active_findings.slots,
        )
        self.assertEqual(
            r3_adv.authoritative_decision_inventory.obligations[0].lifecycle_state,
            "verified-fixed",
        )

        # 2) RS-827D99 & RS-949993: Blocking regression reopens with fresh proof,
        #    rejects contradictory ConfirmedFixEvidence, and honors AuthorizedHumanOverride!
        c_regressed = _comment(
            "src/app.py",
            line=12,
            body="Token bypass reintroduced in login_user.",
            blocking=True,
            severity="critical",
            symbol="login_user",
            defect_kind="authn",
        )
        with self.assertRaisesRegex(
            ReviewInputError, "conflicts with active regression"
        ):
            evaluate_active_window_round(
                _result(c_regressed),
                head_sha=HEAD_3,
                base_sha=BASE_SHA,
                max_active_findings=2,
                round_number=3,
                prior_inventory=r2.authoritative_decision_inventory,
                slots=r2.display_active_findings.slots,
                confirmed_fixes=(
                    ConfirmedFixEvidence(
                        obligation_id=ob1.obligation_id,
                        resolution_criterion=ob1.resolution_criterion,
                        head_sha=HEAD_3,
                        evidence_sha256=_sha("conflicting-fix"),
                        verified_paths=("src/app.py",),
                    ),
                ),
            )

        r3_reopen = evaluate_active_window_round(
            _result(c_regressed),
            head_sha=HEAD_3,
            base_sha=BASE_SHA,
            max_active_findings=2,
            round_number=3,
            prior_inventory=r2.authoritative_decision_inventory,
            slots=r2.display_active_findings.slots,
        )
        reopened = r3_reopen.authoritative_decision_inventory.obligations[0]
        self.assertEqual(reopened.lifecycle_state, "active-unresolved")
        self.assertNotEqual(reopened.target_proof_sha256, fix_proof)

        r3_ovr = evaluate_active_window_round(
            _result(c_regressed),
            head_sha=HEAD_3,
            base_sha=BASE_SHA,
            max_active_findings=2,
            round_number=3,
            prior_inventory=r2.authoritative_decision_inventory,
            slots=r2.display_active_findings.slots,
            human_overrides=(
                AuthorizedHumanOverride(
                    obligation_id=ob1.obligation_id,
                    action="accept-risk",
                    actor="sec-admin",
                    association="OWNER",
                    head_sha=HEAD_3,
                    reason="Enforced by upstream reverse-proxy auth layer.",
                    proof_sha256=_sha("reopen-override"),
                ),
            ),
        )
        self.assertTrue(r3_ovr.approval_eligibility.approved)

        # 3) RS-A87B22: Reopen target colliding with an active slot fails closed:
        other_active = replace(
            obligation_from_comment(
                _comment("src/db.py", line=8, symbol="query_x", defect_kind="sqli")
            ),
            native_target="thread-login",
            target_state="active",
            target_proof_sha256=_sha("claimed"),
        )
        colliding_inv = AuthoritativeDecisionInventory(
            obligations=(
                r2.authoritative_decision_inventory.obligations[0],
                other_active,
            )
        )
        with self.assertRaisesRegex(
            ReviewInputError, "multiple unresolved obligations share one native_target"
        ):
            evaluate_active_window_round(
                _result(c_regressed),
                head_sha=HEAD_3,
                base_sha=BASE_SHA,
                max_active_findings=2,
                round_number=3,
                prior_inventory=colliding_inv,
                slots=(PublishedSlot("thread-login", "active", _sha("claimed")),),
            )


ORDINARY_DIFF = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,1 +1,15 @@
-old_app
+line_1
+line_2
+line_3
+line_4
+line_5
+line_6
+line_7
+line_8
+line_9
+line_10
+line_11
+line_12
+line_13
+line_14
+line_15
diff --git a/src/db.py b/src/db.py
--- a/src/db.py
+++ b/src/db.py
@@ -1,1 +1,35 @@
-old_db
+db_1
+db_2
+db_3
+db_4
+db_5
+db_6
+db_7
+db_8
+db_9
+db_10
+db_11
+db_12
+db_13
+db_14
+db_15
+db_16
+db_17
+db_18
+db_19
+db_20
+db_21
+db_22
+db_23
+db_24
+db_25
+db_26
+db_27
+db_28
+db_29
+db_30
+db_31
+db_32
+db_33
+db_34
+db_35
"""


class _WindowFakeBroker:
    def request_oidc_token(self) -> str:
        return "oidc-window-test"

    def exchange(self, _oidc: str, *, capability: str) -> str:
        return f"tok-{capability}"


class _WindowGitHubHost:
    """Stateful fake GitHub host exercising real ReviewPublisher & GitHubApplication."""

    def __init__(
        self,
        *,
        head_sha: str = HEAD_1,
        base_sha: str = BASE_SHA,
        app_slug: str = "reviewsensei[bot]",
    ) -> None:
        from review_sensei_github import GitHubHttp

        self.head_sha = head_sha
        self.base_sha = base_sha
        self.app_slug = app_slug
        self.reviews: list[dict[str, Any]] = []
        self.threads: list[dict[str, Any]] = []
        self.check_runs: list[dict[str, Any]] = []
        self.issue_comments: list[dict[str, Any]] = []
        self.fail_next_review_post_transient = False
        self.calls: list[tuple[str, str, object]] = []
        self.http = GitHubHttp(api_url="https://api.github.test", opener=self._open)

    def _resp(self, payload: object, status: int = 200):
        from tests.fake_github_http import json_response

        return json_response(payload, status)

    def _open(self, request, timeout=None):
        from urllib.parse import urlparse

        from review_sensei_github.errors import GitHubHTTPTransientError

        parsed = urlparse(request.full_url)
        path = parsed.path
        body = json.loads(request.data.decode("utf-8")) if request.data else None
        self.calls.append((request.method, path, body))

        if request.method == "GET":
            if path.endswith("/pulls/1"):
                return self._resp(
                    {
                        "state": "open",
                        "draft": False,
                        "changed_files": 2,
                        "user": {"login": "dev-author", "type": "User"},
                        "head": {
                            "sha": self.head_sha,
                            "ref": "feature",
                            "repo": {"full_name": "owner/repo", "fork": False},
                        },
                        "base": {
                            "sha": self.base_sha,
                            "ref": "main",
                            "repo": {"id": 1, "full_name": "owner/repo", "fork": False},
                        },
                    }
                )
            if path.endswith("/pulls/1/reviews"):
                return self._resp(list(self.reviews))
            if "/reviews/" in path and path.endswith("/comments"):
                rid = int(path.split("/reviews/")[1].split("/")[0])
                matched = [
                    c
                    for t in self.threads
                    for c in t["comments"]["nodes"]
                    if c.get("pull_request_review_id") == rid
                ]
                return self._resp(matched)
            if path.endswith("/pulls/1/comments"):
                return self._resp([])
            if "/pulls/comments/" in path:
                cid = int(path.rsplit("/", 1)[-1])
                for t in self.threads:
                    for c in t["comments"]["nodes"]:
                        if c.get("databaseId") == cid:
                            return self._resp(
                                {
                                    "id": cid,
                                    "body": c["body"],
                                    "pull_request_url": "https://api.github.test/repos/owner/repo/pulls/1",
                                    "user": {
                                        "id": 9001,
                                        "login": self.app_slug,
                                        "type": "Bot",
                                    },
                                }
                            )
                return self._resp({"message": "Not Found"}, 404)
            if path.endswith("/issues/1/comments"):
                return self._resp(list(self.issue_comments))
            if path.endswith("/check-runs"):
                matching = [
                    r for r in self.check_runs if r.get("head_sha") == self.head_sha
                ]
                return self._resp({"check_runs": matching})
            if "/rules/branches/" in path:
                return self._resp([])
            if path.endswith("/protection"):
                return self._resp({"message": "Not Found"}, 404)
            if path == "/user" or path.startswith("/users/"):
                return self._resp({"id": 9001, "login": self.app_slug, "type": "Bot"})
            if "/collaborators/" in path and path.endswith("/permission"):
                return self._resp({"permission": "maintain"})

        if request.method == "POST":
            if path.endswith("/pulls/1/reviews"):
                if self.fail_next_review_post_transient:
                    self.fail_next_review_post_transient = False
                    raise GitHubHTTPTransientError("simulated review POST timeout")
                assert isinstance(body, dict)
                review_id = 100 + len(self.reviews)
                state_map = {
                    "APPROVE": "APPROVED",
                    "REQUEST_CHANGES": "CHANGES_REQUESTED",
                    "COMMENT": "COMMENTED",
                }
                ev = str(body.get("event", "COMMENT"))
                record = {
                    "id": review_id,
                    "commit_id": body["commit_id"],
                    "state": state_map.get(ev, "COMMENTED"),
                    "body": body.get("body", ""),
                    "pull_request_url": "https://api.github.test/repos/owner/repo/pulls/1",
                    "user": {"id": 9001, "login": self.app_slug, "type": "Bot"},
                }
                self.reviews.append(record)
                for idx, comment in enumerate(body.get("comments") or ()):
                    thread_id = f"PRRT_{review_id}_{idx}"
                    self.threads.append(
                        {
                            "id": thread_id,
                            "isResolved": False,
                            "path": comment["path"],
                            "line": comment["line"],
                            "comments": {
                                "nodes": [
                                    {
                                        "id": f"PRRC_{review_id}_{idx}",
                                        "databaseId": review_id * 100 + idx,
                                        "pull_request_review_id": review_id,
                                        "body": comment["body"],
                                        "author": {
                                            "login": self.app_slug,
                                            "__typename": "Bot",
                                        },
                                    }
                                ]
                            },
                        }
                    )
                return self._resp(record, 200)
            if path.endswith("/check-runs"):
                assert isinstance(body, dict)
                run_id = 500 + len(self.check_runs)
                run = {
                    "id": run_id,
                    "name": body["name"],
                    "head_sha": body["head_sha"],
                    "status": body.get("status"),
                    "conclusion": body.get("conclusion"),
                    "output": body.get("output", {}),
                    "app": {"slug": "reviewsensei"},
                }
                self.check_runs.append(run)
                return self._resp(run, 201)
            if path == "/graphql":
                assert isinstance(body, dict)
                query = str(body.get("query", ""))
                variables = body.get("variables") or {}
                if "resolveReviewThread" in query:
                    tid = (variables.get("input") or {}).get(
                        "threadId"
                    ) or variables.get("threadId")
                    for thread in self.threads:
                        if thread["id"] == tid:
                            thread["isResolved"] = True
                            return self._resp(
                                {
                                    "data": {
                                        "resolveReviewThread": {
                                            "thread": {"id": tid, "isResolved": True}
                                        }
                                    }
                                }
                            )
                    return self._resp(
                        {"errors": [{"message": "thread not found"}]}, 200
                    )
                if body.get("operationName") == "ResolutionReadback":
                    tid = variables.get("id")
                    for thread in self.threads:
                        if thread["id"] == tid:
                            return self._resp(
                                {
                                    "data": {
                                        "node": {
                                            "id": tid,
                                            "isResolved": thread["isResolved"],
                                        }
                                    }
                                }
                            )
                    return self._resp({"data": {"node": None}}, 200)
                return self._resp(
                    {
                        "data": {
                            "repository": {
                                "pullRequest": {
                                    "reviewThreads": {
                                        "nodes": list(self.threads),
                                        "pageInfo": {
                                            "hasNextPage": False,
                                            "endCursor": None,
                                        },
                                    }
                                }
                            }
                        }
                    }
                )
            if path.endswith("/issues/1/comments"):
                assert isinstance(body, dict)
                cid = 800 + len(self.issue_comments)
                item = {
                    "id": cid,
                    "body": body["body"],
                    "user": {"id": 9001, "login": self.app_slug, "type": "Bot"},
                }
                self.issue_comments.append(item)
                return self._resp(item, 201)

        if request.method == "PUT" and "/reviews/" in path:
            assert isinstance(body, dict)
            rid = int(path.rsplit("/", 1)[-1])
            for rev in self.reviews:
                if rev["id"] == rid:
                    rev["body"] = body["body"]
                    return self._resp(rev, 200)

        if request.method == "PATCH" and "/check-runs/" in path:
            assert isinstance(body, dict)
            cid = int(path.rsplit("/", 1)[-1])
            for run in self.check_runs:
                if run["id"] == cid:
                    run.update(body)
                    return self._resp(run, 200)

        raise AssertionError(f"Unhandled request: {request.method} {path}")


class OrdinaryPublisherActiveWindowTests(unittest.TestCase):
    """End-to-end ordinary ReviewPublisher & GitHubApplication G10 tests."""

    def _make_app(self, host: _WindowGitHubHost, ledger_dir: str, *, cap: int = 2):
        from pathlib import Path

        from review_sensei_github import (
            ConversationPublisher,
            GitHubApplication,
            GitHubWriteOptions,
            LearningPRPublisher,
            ReviewPublisher,
        )

        from review_sensei.session import LocalSessionLedger, SessionIdentity

        publisher = ReviewPublisher(http=host.http)
        ledger = LocalSessionLedger(Path(ledger_dir))
        ledger.initialize(
            SessionIdentity(repository="owner/repo", pull_request=1, repository_id=1)
        )
        app = GitHubApplication(
            http=host.http,
            broker=_WindowFakeBroker(),  # type: ignore[arg-type]
            reviewer=publisher,
            learner=LearningPRPublisher(http=host.http),
            replier=ConversationPublisher(http=host.http),
            session_ledger=None,
        )
        cmd_app = GitHubApplication(
            http=host.http,
            broker=_WindowFakeBroker(),  # type: ignore[arg-type]
            reviewer=publisher,
            learner=LearningPRPublisher(http=host.http),
            replier=ConversationPublisher(http=host.http),
            session_ledger=ledger,
        )
        options = GitHubWriteOptions(
            auto_review=True,
            auto_approve=True,
            github_writes=True,
            operation_entry="disabled",
            max_active_findings=cap,
        )
        return app, cmd_app, publisher, options

    @staticmethod
    def _blockers(res: ReviewResult):
        from review_sensei.convergence import derive_blocker_candidate

        return tuple(
            derive_blocker_candidate(
                c,
                on_changed_path=True,
                evidence_locations_validated=True,
                has_failure_condition=True,
                has_actionable_remedy=True,
                has_specific_violation=True,
                named_mandatory_rule="SEC-01",
                has_required_contract=True,
            )
            for c in res.comments
        )

    def _assert_zero_deferred_leakage_across_surfaces(
        self,
        *,
        host: _WindowGitHubHost,
        app,
        pub_result,
    ) -> None:
        if pub_result.shadow is not None:
            assert_no_deferred_leakage(pub_result.shadow)
        status_doc = app.active_window_public_status(repository_id=1, pull_request=1)
        self.assertIsNotNone(status_doc)
        assert_no_deferred_leakage(status_doc)
        forbidden_tokens = (
            "deferred_count",
            "deferred_findings",
            "hidden_count",
            "hidden_findings",
            "remaining_count",
            "total_obligations",
            "unselected_count",
        )
        for rev in host.reviews:
            text = str(rev.get("body", ""))
            for tok in forbidden_tokens:
                self.assertNotIn(tok, text)
        for thread in host.threads:
            for node in thread["comments"]["nodes"]:
                for tok in forbidden_tokens:
                    self.assertNotIn(tok, str(node.get("body", "")))
        for run in host.check_runs:
            out = json.dumps(run.get("output", {}))
            for tok in forbidden_tokens:
                self.assertNotIn(tok, out)

    def test_ordinary_publisher_multi_round_bounded_cap_turnover_and_convergence(
        self,
    ) -> None:
        import tempfile

        from review_sensei.convergence import ReviewConvergencePolicy

        c_crit = _comment(
            "src/app.py",
            line=5,
            body="Critical RCE in payload loader.",
            severity="critical",
            symbol="loads_payload",
            defect_kind="rce",
        )
        c_high = _comment(
            "src/app.py",
            line=15,
            body="High privilege escalation in role check.",
            severity="high",
            symbol="check_role",
            defect_kind="authz",
        )
        c_med = _comment(
            "src/db.py",
            line=25,
            body="Medium connection leak on exception path.",
            severity="medium",
            symbol="open_conn",
            defect_kind="resource_leak",
        )
        c_low = _comment(
            "src/db.py",
            line=35,
            body="Low unindexed query under contention.",
            severity="low",
            symbol="lookup_item",
            defect_kind="perf",
        )
        cov = _coverage("src/app.py", "src/db.py")
        policy = ReviewConvergencePolicy(mode="strict")

        with tempfile.TemporaryDirectory() as tmpdir:
            host = _WindowGitHubHost(head_sha=HEAD_1)
            app, _cmd_app, _publisher, options = self._make_app(host, tmpdir, cap=2)

            # Round 1: 4 blocking findings, cap=2 -> only critical & high published!
            res1 = _result(c_crit, c_high, c_med, c_low, coverage=cov)
            pub1 = app.publish_review(
                options=options,
                oidc_token="oidc-1",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha=HEAD_1,
                base_branch="main",
                base_sha=BASE_SHA,
                result=res1,
                diff=ORDINARY_DIFF,
                app_slug="reviewsensei[bot]",
                convergence_policy=policy,
                blocker_candidates=self._blockers(res1),
            )
            self.assertEqual(pub1.status, "published")
            self.assertEqual(pub1.diagnostic, "required_fixes_open")
            self.assertEqual(len(host.reviews), 1)
            self.assertEqual(host.reviews[0]["state"], "CHANGES_REQUESTED")
            self.assertEqual(len(host.threads), 2)
            thread_bodies_r1 = [t["comments"]["nodes"][0]["body"] for t in host.threads]
            self.assertTrue(any("Critical RCE" in b for b in thread_bodies_r1))
            self.assertTrue(any("High privilege" in b for b in thread_bodies_r1))
            self.assertFalse(any("Medium connection" in b for b in thread_bodies_r1))
            self.assertFalse(any("Low unindexed" in b for b in thread_bodies_r1))
            self.assertEqual(host.check_runs[-1]["conclusion"], "failure")

            state1 = app.active_window_state(repository_id=1, pull_request=1)
            assert state1 is not None
            self.assertEqual(
                len(state1.authoritative_decision_inventory.obligations), 4
            )
            self.assertEqual(len(state1.display_active_findings.entries), 2)
            self._assert_zero_deferred_leakage_across_surfaces(
                host=host, app=app, pub_result=pub1
            )

            by_symbol = {
                o.symbol: o for o in state1.authoritative_decision_inventory.obligations
            }
            high_slot_r1 = by_symbol["check_role"].native_target
            self.assertIsNotNone(high_slot_r1)

            # Round 2 (HEAD_2): proven fix for critical -> frees 1 slot, reuses high,
            # and deterministically promotes medium (open_conn)!
            host.head_sha = HEAD_2
            fix_crit = ConfirmedFixEvidence(
                obligation_id=by_symbol["loads_payload"].obligation_id,
                resolution_criterion=by_symbol["loads_payload"].resolution_criterion,
                head_sha=HEAD_2,
                evidence_sha256=_sha("fix-crit-r2"),
                verified_paths=("src/app.py",),
            )
            res2 = _result(c_high, c_med, c_low, coverage=cov)
            pub2 = app.publish_review(
                options=options,
                oidc_token="oidc-2",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha=HEAD_2,
                base_branch="main",
                base_sha=BASE_SHA,
                result=res2,
                diff=ORDINARY_DIFF,
                app_slug="reviewsensei[bot]",
                convergence_policy=policy,
                blocker_candidates=self._blockers(res2),
                confirmed_fixes=(fix_crit,),
            )
            self.assertEqual(pub2.status, "published")
            self.assertEqual(pub2.diagnostic, "required_fixes_open")
            state2 = app.active_window_state(repository_id=1, pull_request=1)
            assert state2 is not None
            self.assertEqual(
                [e.obligation_id for e in state2.display_active_findings.entries],
                [
                    by_symbol["check_role"].obligation_id,
                    by_symbol["open_conn"].obligation_id,
                ],
            )
            # Compatible identity/slot reuse on check_role:
            self.assertEqual(
                state2.display_active_findings.entries[0].native_target, high_slot_r1
            )
            self.assertEqual(
                state2.display_active_findings.entries[0].target_action, "reuse"
            )
            self._assert_zero_deferred_leakage_across_surfaces(
                host=host, app=app, pub_result=pub2
            )

            # Round 3 (HEAD_3): override high + fix medium -> promotes low!
            host.head_sha = HEAD_3
            ovr_high = AuthorizedHumanOverride(
                obligation_id=by_symbol["check_role"].obligation_id,
                action="accept-risk",
                actor="lead-maint",
                association="OWNER",
                head_sha=HEAD_3,
                reason="Gateway enforces role check upstream.",
                proof_sha256=_sha("ovr-high-r3"),
                slot_released=True,
            )
            fix_med = ConfirmedFixEvidence(
                obligation_id=by_symbol["open_conn"].obligation_id,
                resolution_criterion=by_symbol["open_conn"].resolution_criterion,
                head_sha=HEAD_3,
                evidence_sha256=_sha("fix-med-r3"),
                verified_paths=("src/db.py",),
            )
            res3 = _result(c_low, coverage=cov)
            pub3 = app.publish_review(
                options=options,
                oidc_token="oidc-3",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha=HEAD_3,
                base_branch="main",
                base_sha=BASE_SHA,
                result=res3,
                diff=ORDINARY_DIFF,
                app_slug="reviewsensei[bot]",
                convergence_policy=policy,
                blocker_candidates=self._blockers(res3),
                confirmed_fixes=(fix_med,),
                human_overrides=(ovr_high,),
            )
            self.assertEqual(pub3.status, "published")
            self.assertEqual(pub3.diagnostic, "required_fixes_open")
            state3 = app.active_window_state(repository_id=1, pull_request=1)
            assert state3 is not None
            self.assertEqual(
                [e.obligation_id for e in state3.display_active_findings.entries],
                [by_symbol["lookup_item"].obligation_id],
            )

            # Round 4 (HEAD_4): fix low -> full authoritative inventory resolved -> APPROVE!
            head_4 = "4" * 40
            host.head_sha = head_4
            # Resolve prior open threads on GitHub as superseded/cleaned before clean head
            for t in host.threads:
                t["isResolved"] = True
            fix_low = ConfirmedFixEvidence(
                obligation_id=by_symbol["lookup_item"].obligation_id,
                resolution_criterion=by_symbol["lookup_item"].resolution_criterion,
                head_sha=head_4,
                evidence_sha256=_sha("fix-low-r4"),
                verified_paths=("src/db.py",),
            )
            pub4 = app.publish_review(
                options=options,
                oidc_token="oidc-4",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha=head_4,
                base_branch="main",
                base_sha=BASE_SHA,
                result=_result(coverage=cov),
                diff=ORDINARY_DIFF,
                app_slug="reviewsensei[bot]",
                convergence_policy=policy,
                confirmed_fixes=(fix_low,),
            )
            self.assertEqual(pub4.status, "published")
            self.assertIsNone(pub4.diagnostic)
            self.assertEqual(host.check_runs[-1]["conclusion"], "success")
            self.assertTrue(
                any(
                    r["commit_id"] == head_4 and r["state"] == "APPROVED"
                    for r in host.reviews
                )
            )
            self._assert_zero_deferred_leakage_across_surfaces(
                host=host, app=app, pub_result=pub4
            )

    def test_negative_control_complete_diff_coverage_omission_withholds_approval_in_publisher(
        self,
    ) -> None:
        import tempfile

        from review_sensei.convergence import ReviewConvergencePolicy

        c1 = _comment(
            "src/app.py",
            line=5,
            body="SQL injection in query builder.",
            severity="critical",
            symbol="build_query",
            defect_kind="sqli",
        )
        cov = _coverage("src/app.py", "src/db.py")
        policy = ReviewConvergencePolicy()

        with tempfile.TemporaryDirectory() as tmpdir:
            host = _WindowGitHubHost(head_sha=HEAD_1)
            app, _cmd_app, _pub, options = self._make_app(host, tmpdir, cap=2)

            res_c1 = _result(c1, coverage=cov)
            app.publish_review(
                options=options,
                oidc_token="oidc-1",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha=HEAD_1,
                base_branch="main",
                base_sha=BASE_SHA,
                result=res_c1,
                diff=ORDINARY_DIFF,
                app_slug="reviewsensei[bot]",
                convergence_policy=policy,
                blocker_candidates=self._blockers(res_c1),
            )

            # Round 2 on HEAD_2: 100% diff coverage, 0 comments emitted by model,
            # NO ConfirmedFixEvidence / AuthorizedHumanOverride / AuthorizedRescan!
            host.head_sha = HEAD_2
            for t in host.threads:
                t["isResolved"] = True
            pub2 = app.publish_review(
                options=options,
                oidc_token="oidc-2",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha=HEAD_2,
                base_branch="main",
                base_sha=BASE_SHA,
                result=_result(coverage=cov),
                diff=ORDINARY_DIFF,
                app_slug="reviewsensei[bot]",
                convergence_policy=policy,
            )
            self.assertEqual(pub2.status, "published")
            self.assertEqual(pub2.diagnostic, "required_fixes_open")
            self.assertEqual(host.check_runs[-1]["conclusion"], "failure")
            self.assertFalse(any(r["state"] == "APPROVED" for r in host.reviews))
            state2 = app.active_window_state(repository_id=1, pull_request=1)
            assert state2 is not None
            self.assertEqual(
                state2.authoritative_decision_inventory.obligations[0].lifecycle_state,
                "omitted-uncertain",
            )
            self.assertFalse(state2.approval_eligibility.approved)

    def test_negative_control_maintainer_override_of_displayed_window_blocks_hidden_obligation(
        self,
    ) -> None:
        import tempfile

        from review_sensei.convergence import ReviewConvergencePolicy

        c_disp = _comment(
            "src/app.py",
            line=5,
            body="Critical auth bypass in token check.",
            severity="critical",
            symbol="verify_tok",
            defect_kind="authn",
        )
        c_hidden = _comment(
            "src/db.py",
            line=25,
            body="High data leak in export query.",
            severity="high",
            symbol="export_rows",
            defect_kind="leak",
        )
        cov = _coverage("src/app.py", "src/db.py")
        policy = ReviewConvergencePolicy()

        with tempfile.TemporaryDirectory() as tmpdir:
            host = _WindowGitHubHost(head_sha=HEAD_1)
            app, cmd_app, _pub, options = self._make_app(host, tmpdir, cap=1)

            res_pair = _result(c_disp, c_hidden, coverage=cov)
            pub1 = app.publish_review(
                options=options,
                oidc_token="oidc-1",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha=HEAD_1,
                base_branch="main",
                base_sha=BASE_SHA,
                result=res_pair,
                diff=ORDINARY_DIFF,
                app_slug="reviewsensei[bot]",
                convergence_policy=policy,
                blocker_candidates=self._blockers(res_pair),
            )
            self.assertEqual(pub1.diagnostic, "required_fixes_open")
            state1 = app.active_window_state(repository_id=1, pull_request=1)
            assert state1 is not None
            by_sym = {
                o.symbol: o for o in state1.authoritative_decision_inventory.obligations
            }
            fp_disp = by_sym["verify_tok"].fingerprint
            fp_hidden = by_sym["export_rows"].fingerprint

            # Maintainer overrides the ONLY displayed finding on HEAD_1 via apply_maintainer_command:
            cmd1 = cmd_app.apply_maintainer_command(
                options=options,
                oidc_token="oidc-cmd-1",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha=HEAD_1,
                app_slug="reviewsensei[bot]",
                actor_login="maint-lead",
                actor_type="User",
                association="OWNER",
                body=f"@reviewsensei accept-risk {fp_disp} --reason Handled at edge gateway",
            )
            self.assertTrue(cmd1.applied)
            # Hidden obligation MUST prevent APPROVE and keep check failing!
            self.assertFalse(any(r["state"] == "APPROVED" for r in host.reviews))
            self.assertEqual(host.check_runs[-1]["conclusion"], "failure")

            # Negative control: attempting to waive the non-displayed fingerprint directly
            # on HEAD_1 MUST NOT clear the hidden obligation or approve!
            cmd_app.apply_maintainer_command(
                options=options,
                oidc_token="oidc-cmd-2",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha=HEAD_1,
                app_slug="reviewsensei[bot]",
                actor_login="maint-lead",
                actor_type="User",
                association="OWNER",
                body=f"@reviewsensei accept-risk {fp_hidden} --reason Attempt blind waive",
            )
            state_after_cmd = app.active_window_state(repository_id=1, pull_request=1)
            assert state_after_cmd is not None
            by_sym_after = {
                o.symbol: o
                for o in state_after_cmd.authoritative_decision_inventory.obligations
            }
            self.assertEqual(
                by_sym_after["verify_tok"].lifecycle_state, "authorized-override"
            )
            self.assertEqual(
                by_sym_after["export_rows"].lifecycle_state, "active-unresolved"
            )
            self.assertFalse(any(r["state"] == "APPROVED" for r in host.reviews))

            # Round 2 on HEAD_2: freed slot promotes c_hidden into the active window!
            host.head_sha = HEAD_2
            res_hid = _result(c_hidden, coverage=cov)
            pub2 = app.publish_review(
                options=options,
                oidc_token="oidc-2",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha=HEAD_2,
                base_branch="main",
                base_sha=BASE_SHA,
                result=res_hid,
                diff=ORDINARY_DIFF,
                app_slug="reviewsensei[bot]",
                convergence_policy=policy,
                blocker_candidates=self._blockers(res_hid),
            )
            self.assertEqual(pub2.status, "published")
            state2 = app.active_window_state(repository_id=1, pull_request=1)
            assert state2 is not None
            self.assertEqual(
                [e.obligation_id for e in state2.display_active_findings.entries],
                [by_sym["export_rows"].obligation_id],
            )

    def test_ordinary_publisher_transient_post_holds_uncertain_slot_until_reconciled(
        self,
    ) -> None:
        import tempfile

        from review_sensei_github.errors import GitHubPublicationTransientError

        from review_sensei.convergence import ReviewConvergencePolicy

        c1 = _comment(
            "src/app.py",
            line=5,
            body="Critical auth bug.",
            severity="critical",
            symbol="auth_check",
            defect_kind="authn",
        )
        c2 = _comment(
            "src/db.py",
            line=25,
            body="High query bug.",
            severity="high",
            symbol="run_query",
            defect_kind="sqli",
        )
        cov = _coverage("src/app.py", "src/db.py")
        policy = ReviewConvergencePolicy()

        with tempfile.TemporaryDirectory() as tmpdir:
            host = _WindowGitHubHost(head_sha=HEAD_1)
            host.fail_next_review_post_transient = True
            app, _cmd_app, _pub, options = self._make_app(host, tmpdir, cap=1)

            with self.assertRaises(GitHubPublicationTransientError):
                app.publish_review(
                    options=options,
                    oidc_token="oidc-1",
                    repository="owner/repo",
                    repository_id=1,
                    pull_request=1,
                    head_sha=HEAD_1,
                    base_branch="main",
                    base_sha=BASE_SHA,
                    result=_result(c1, c2, coverage=cov),
                    diff=ORDINARY_DIFF,
                    app_slug="reviewsensei[bot]",
                    convergence_policy=policy,
                )

            uncertain_state = app.active_window_state(repository_id=1, pull_request=1)
            assert uncertain_state is not None
            self.assertTrue(uncertain_state.display_active_findings.has_uncertain_slots)
            self.assertIn(
                "uncertain-native-slot",
                uncertain_state.approval_eligibility.withheld_reasons,
            )
            uncertain_slot = uncertain_state.display_active_findings.slots[0]
            self.assertEqual(uncertain_slot.state, "uncertain")

            # Reconcile readback proving target was never created ("released"), then retry:
            app.reconcile_active_window_slot(
                repository_id=1,
                pull_request=1,
                target=uncertain_slot.target,
                resolved_state="released",
                proof_sha256=_sha("readback-absent"),
            )
            retry_pub = app.publish_review(
                options=options,
                oidc_token="oidc-1-retry",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha=HEAD_1,
                base_branch="main",
                base_sha=BASE_SHA,
                result=_result(c1, c2, coverage=cov),
                diff=ORDINARY_DIFF,
                app_slug="reviewsensei[bot]",
                convergence_policy=policy,
            )
            self.assertEqual(retry_pub.status, "published")
            reconciled_state = app.active_window_state(repository_id=1, pull_request=1)
            assert reconciled_state is not None
            self.assertFalse(
                reconciled_state.display_active_findings.has_uncertain_slots
            )
            self.assertEqual(len(reconciled_state.display_active_findings.entries), 1)

    def test_options_and_shadow_deferred_leakage_guards(self) -> None:
        from review_sensei_github import GitHubWriteOptions, PublicationResult

        with self.assertRaisesRegex(ReviewInputError, "max_active_findings"):
            GitHubWriteOptions(max_active_findings=0)
        with self.assertRaisesRegex(ReviewInputError, "max_active_findings"):
            GitHubWriteOptions(max_active_findings=True)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ReviewInputError, "never expose deferred finding"):
            PublicationResult(status="published", shadow={"deferred_count": 2})


if __name__ == "__main__":
    unittest.main()
