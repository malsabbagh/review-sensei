"""Regression tests for Gap 8 (originalCommit drift readback) and Gap 9 (Tier 4 same-file collision)."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from review_sensei_github import ReviewPublisher
from review_sensei_github.finding_lifecycle import (
    FindingLifecycleJournal,
    restore_finding_lifecycle,
)
from review_sensei_github.finding_lifecycle_reconciliation import (
    reconcile_finding_lifecycle,
)
from review_sensei_github.publication import (
    _REVIEW_THREADS_QUERY,
    GitHubPublicationError,
    NativeFindingReceipt,
    _native_finding_receipts_from_nodes,
    _root_matches_native_head,
)

from review_sensei.active_finding_window import ConfirmedFixEvidence
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.coverage import CoverageManifest, FileCoverage
from review_sensei.errors import ReviewInputError
from review_sensei.finding_drift import (
    DriftInventory,
    FileDriftDescriptor,
    NativeThreadBinding,
    evaluate_drift_round,
    project_drift_review_result,
)
from review_sensei.models import ReviewComment, ReviewResult
from review_sensei.session import LocalSessionLedger
from tests import test_durable_finding_lifecycle_restart as restart
from tests.test_active_window_lifecycle_g10 import (
    BASE_SHA,
    HEAD_1,
    HEAD_2,
    HEAD_3,
    ORDINARY_DIFF,
    _WindowGitHubHost,
)
from tests.test_native_finding_receipts import _native_fixture

HEAD_4 = "4" * 40


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _coverage(*paths: str) -> CoverageManifest:
    return CoverageManifest(
        files=tuple(FileCoverage(p, "reviewed") for p in paths),
        enumerated_paths=tuple(paths),
        enumeration_complete=True,
    )


def _optional_comment(
    path: str = "src/app.py",
    *,
    line: int | None = 10,
    body: str = "Consider simplifying this expression.",
    symbol: str | None = None,
    defect_kind: str | None = "style",
    evidence_id: str | None = None,
    blocking: bool = False,
    needs_human: bool = False,
) -> ReviewComment:
    return ReviewComment(
        path=path,
        line=line,
        side="RIGHT",
        body=body,
        blocking=blocking,
        needs_human=needs_human,
        effective_blocking=blocking,
        severity="low" if not blocking else "high",
        symbol=symbol,
        defect_kind=defect_kind,
        evidence_id=evidence_id,
    )


def _review_result(
    *comments: ReviewComment,
    paths: tuple[str, ...] = ("src/app.py",),
) -> ReviewResult:
    return ReviewResult(
        summary="Regression test review.",
        comments=comments,
        provider="fixture",
        review_status="complete",
        coverage=_coverage(*paths),
    )


class OriginalCommitDriftReadbackTests(unittest.TestCase):
    def test_review_threads_query_selects_original_commit(self) -> None:
        self.assertIn("commit { oid }", _REVIEW_THREADS_QUERY)
        self.assertIn("originalCommit { oid }", _REVIEW_THREADS_QUERY)

    def test_root_matches_native_head_handles_original_commit_and_legacy_fallback(
        self,
    ) -> None:
        self.assertTrue(
            _root_matches_native_head(
                {
                    "commit": {"oid": HEAD_2},
                    "originalCommit": {"oid": HEAD_1},
                },
                HEAD_1,
            )
        )
        self.assertTrue(
            _root_matches_native_head(
                {
                    "commit": None,
                    "originalCommit": {"oid": HEAD_1},
                },
                HEAD_1,
            )
        )
        self.assertFalse(
            _root_matches_native_head(
                {
                    "commit": {"oid": HEAD_1},
                    "originalCommit": {"oid": HEAD_2},
                },
                HEAD_1,
            )
        )
        self.assertFalse(
            _root_matches_native_head(
                {
                    "commit": {"oid": "not-a-sha"},
                    "originalCommit": {"oid": HEAD_1},
                },
                HEAD_1,
            )
        )
        self.assertFalse(
            _root_matches_native_head(
                {
                    "commit": {"oid": HEAD_2, "extra": True},
                    "originalCommit": {"oid": HEAD_1},
                },
                HEAD_1,
            )
        )
        self.assertFalse(
            _root_matches_native_head(
                {
                    "commit": {"oid": HEAD_1},
                    "originalCommit": {"oid": HEAD_1, "extra": True},
                },
                HEAD_1,
            )
        )
        # Legacy fixtures omitting originalCommit fall back to commit.oid.
        self.assertTrue(_root_matches_native_head({"commit": {"oid": HEAD_1}}, HEAD_1))
        self.assertFalse(_root_matches_native_head({"commit": {"oid": HEAD_2}}, HEAD_1))
        self.assertFalse(_root_matches_native_head({"commit": {"oid": HEAD_1}}, None))

    def test_prepare_existing_drift_updates_accepts_auto_advanced_commit(self) -> None:
        host = _WindowGitHubHost(head_sha=HEAD_1)
        publisher = ReviewPublisher(http=host.http)
        comment_1 = _optional_comment(
            "src/app.py",
            line=5,
            body="Missing guard check on input.",
            symbol="process",
            defect_kind="missing_validation",
            blocking=True,
        )
        result_1 = _review_result(comment_1)
        publisher.publish(
            token="test",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha=HEAD_1,
            base_branch="main",
            base_sha=BASE_SHA,
            result=result_1,
            diff=ORDINARY_DIFF,
            app_slug=host.app_slug,
            max_active_findings=1,
            enable_drift_tracking=True,
            convergence_policy=ReviewConvergencePolicy(mode="strict"),
        )
        prior_drift = publisher._drift_states[(1, 1)]
        ob = prior_drift.inventory.obligations[0]
        assert ob.thread_binding is not None
        moved_comment = replace(comment_1, path="src/moved.py", line=12)
        result_2 = _review_result(moved_comment, paths=("src/moved.py",))
        planned_2 = evaluate_drift_round(
            result_2,
            head_sha=HEAD_2,
            base_sha=BASE_SHA,
            max_active_findings=1,
            round_number=2,
            coverage_mode="full",
            prior_inventory=prior_drift.inventory,
            drift_descriptors=(
                FileDriftDescriptor(
                    drift_kind="file_rename",
                    source_path="src/app.py",
                    target_paths=("src/moved.py",),
                    moved_symbols=("process",),
                ),
            ),
        )
        ob2 = planned_2.inventory.obligations[0]
        rendered_body = (
            "Moved finding body\n"
            f"<!-- reviewsensei:finding:v2 repo=1 pr=1 head={HEAD_2} "
            f"base={BASE_SHA} fingerprint={ob2.criterion.original_fingerprint} "
            "state=still-present blocking=true human=false -->"
        )
        prepared = [(ob2.synthetic_comment_for_placement(), "", rendered_body, "")]

        publisher._native_finding_context = {
            "repository": "owner/repo",
            "repository_id": 1,
            "pull_request": 1,
            "producer_id": 9001,
            "app_slug": host.app_slug,
        }
        node = deepcopy(host.threads[0])
        node["comments"]["nodes"][0]["commit"] = {"oid": HEAD_2}
        node["comments"]["nodes"][0]["originalCommit"] = {"oid": HEAD_1}
        publisher._native_binding_nodes = (node,)

        updates = publisher._prepare_existing_drift_updates(
            planned=planned_2,
            prepared=prepared,
        )
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0][2]["native_head_sha"], HEAD_1)

        tampered = deepcopy(node)
        tampered["comments"]["nodes"][0]["originalCommit"] = {"oid": HEAD_2}
        publisher._native_binding_nodes = (tampered,)
        with self.assertRaisesRegex(
            GitHubPublicationError, "retained drift root changed outside its receipt"
        ):
            publisher._prepare_existing_drift_updates(
                planned=planned_2,
                prepared=prepared,
            )

    def test_native_finding_receipts_accepts_auto_advanced_reused_root(self) -> None:
        node, instance, scope = _native_fixture()
        root = node["comments"]["nodes"][0]
        original_head = "e" * 40
        root["commit"] = {"oid": HEAD_2}
        root["originalCommit"] = {"oid": original_head}
        scope["existing_bindings"] = {
            instance: {
                "thread_id": node["id"],
                "comment_id": root["databaseId"],
                "review_id": root["pullRequestReview"]["databaseId"],
                "native_head_sha": original_head,
            }
        }
        receipt = _native_finding_receipts_from_nodes([node], **scope)[instance]
        self.assertEqual(receipt.native_head_sha, original_head)

        mismatched = deepcopy(node)
        mismatched["comments"]["nodes"][0]["originalCommit"] = {"oid": HEAD_2}
        self.assertNotIn(
            instance, _native_finding_receipts_from_nodes([mismatched], **scope)
        )

    def test_reconciliation_and_thread_readback_accept_original_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            restart.DurableFindingRestartTests().run_phase(root, "drift", "initial")
            service = restart.PersistentNativeService(root / "service.json", HEAD_1)
            ledger = LocalSessionLedger(
                root / "ledger", enable_finding_lifecycle_writes=True
            )
            reviewer = ReviewPublisher(http=service.http)
            restore_finding_lifecycle(reviewer, ledger, restart.IDENTITY)

            raw = reviewer.native_finding_receipts(repository_id=1, pull_request=1)[0]
            receipt = NativeFindingReceipt(
                **{k: v for k, v in raw.items() if k != "schema_version"}
            )
            for comment in service.threads[0]["comments"]["nodes"]:
                comment["commit"] = {"oid": HEAD_2}
                comment["originalCommit"] = {"oid": HEAD_1}
            service.save()

            reconciled = reconcile_finding_lifecycle(
                reviewer,
                ledger,
                restart.IDENTITY,
                mode="drift",
                target=receipt.target,
                resolved_state="active",
                proof_sha256=receipt.proof_sha256,
                token="fixture-token",
                app_slug="reviewsensei[bot]",
            )
            self.assertIsNotNone(reconciled)

            # Verify FindingLifecycleJournal._reconcile_thread_mutation readback path.
            reviewer._native_binding_nodes = tuple(deepcopy(service.threads))
            with patch.object(
                reviewer, "_published_finding_suppression", return_value=None
            ):
                journal = FindingLifecycleJournal(
                    reviewer,
                    ledger,
                    restart.IDENTITY,
                    operation_id="1" * 64,
                    base_sha=BASE_SHA,
                    head_sha=HEAD_2,
                    policy_digest="2" * 64,
                    configuration_digest="3" * 64,
                    app_slug="reviewsensei[bot]",
                )
                effect = {
                    "status": "pending",
                    "method": "POST",
                    "path": "/graphql",
                    "request": {
                        "query": "mutation ResolveFindingThread { resolveReviewThread(input: {}) { thread { id isResolved } } }",
                        "variables": {"threadId": receipt.thread_id},
                    },
                }
                service.threads[0]["isResolved"] = True
                reviewer._native_binding_nodes = tuple(deepcopy(service.threads))
                matched = journal._readback_thread(effect, token="fixture-token")
                self.assertIsNotNone(matched)
                self.assertEqual(matched[0], 200)

                tampered = deepcopy(service.threads)
                tampered[0]["comments"]["nodes"][0]["originalCommit"] = {"oid": HEAD_2}
                reviewer._native_binding_nodes = tuple(tampered)
                self.assertIsNone(
                    journal._readback_thread(effect, token="fixture-token")
                )

            # Mismatched originalCommit in reconciliation fails closed.
            service.threads[0]["isResolved"] = False
            service.threads[0]["comments"]["nodes"][0]["originalCommit"] = {
                "oid": HEAD_2
            }
            service.save()
            with self.assertRaisesRegex(
                ReviewInputError, "finding lifecycle native receipt readback conflicts"
            ):
                reconcile_finding_lifecycle(
                    reviewer,
                    ledger,
                    restart.IDENTITY,
                    mode="drift",
                    target=receipt.target,
                    resolved_state="active",
                    proof_sha256=receipt.proof_sha256,
                    token="fixture-token",
                    app_slug="reviewsensei[bot]",
                )


class Tier4UnchangedSameFileOptionalEscalationTests(unittest.TestCase):
    def test_multiple_optional_unsymbolled_findings_on_unchanged_file_stay_non_blocking(
        self,
    ) -> None:
        r1_comments = (
            _optional_comment(
                "src/app.py",
                line=10,
                body="Optional note 1 about naming.",
                evidence_id="ev-r1-1",
            ),
            _optional_comment(
                "src/app.py",
                line=25,
                body="Optional note 2 about formatting.",
                evidence_id="ev-r1-2",
            ),
            _optional_comment(
                "src/app.py",
                line=40,
                body="Optional note 3 about docstrings.",
                evidence_id="ev-r1-3",
            ),
        )
        r1 = evaluate_drift_round(
            _review_result(*r1_comments),
            head_sha=HEAD_1,
            base_sha=BASE_SHA,
            max_active_findings=8,
            round_number=1,
        )
        self.assertTrue(r1.approved)
        # Simulate priors whose concern is None (Tier 4 coarse path fallback).
        r1_no_concern = DriftInventory(
            obligations=tuple(
                replace(ob, criterion=replace(ob.criterion, concern=None))
                for ob in r1.inventory.obligations
            )
        )

        r2_comments = (
            _optional_comment(
                "src/app.py",
                line=12,
                body="Optional note 1 revised wording.",
                evidence_id="ev-r2-1",
            ),
            _optional_comment(
                "src/app.py",
                line=27,
                body="Optional note 2 revised wording.",
                evidence_id="ev-r2-2",
            ),
            _optional_comment(
                "src/app.py",
                line=42,
                body="Optional note 3 revised wording.",
                evidence_id="ev-r2-3",
            ),
        )
        r2_result = _review_result(*r2_comments)
        r2 = evaluate_drift_round(
            r2_result,
            head_sha=HEAD_2,
            base_sha=BASE_SHA,
            max_active_findings=8,
            round_number=2,
            prior_inventory=r1_no_concern,
        )
        self.assertTrue(r2.approved)
        self.assertFalse(r2.inventory.has_ambiguous_drift)
        self.assertFalse(r2.inventory.has_omitted_uncertainty)
        for ob in r2.inventory.obligations:
            self.assertNotEqual(ob.lifecycle_state, "ambiguous-multi-target")
            self.assertFalse(ob.criterion.blocking)
            self.assertFalse(ob.criterion.needs_human)
            self.assertFalse(ob.blocks_approval_now)
            self.assertFalse(ob.effective_blocking_now)
        projected = project_drift_review_result(r2_result, r2)
        for comment in projected.comments:
            self.assertFalse(comment.blocking)
            self.assertFalse(comment.needs_human)
            self.assertFalse(comment.effective_blocking)

    def test_one_to_one_optional_unsymbolled_tier4_match_still_tracks_single_finding(
        self,
    ) -> None:
        r1 = evaluate_drift_round(
            _review_result(
                _optional_comment(
                    "src/app.py", line=10, body="Note A.", evidence_id="ev-r1"
                )
            ),
            head_sha=HEAD_1,
            base_sha=BASE_SHA,
            max_active_findings=8,
            round_number=1,
        )
        r1_no_concern = DriftInventory(
            obligations=(
                replace(
                    r1.inventory.obligations[0],
                    criterion=replace(
                        r1.inventory.obligations[0].criterion, concern=None
                    ),
                ),
            )
        )
        r2 = evaluate_drift_round(
            _review_result(
                _optional_comment(
                    "src/app.py", line=14, body="Note A reworded.", evidence_id="ev-r2"
                )
            ),
            head_sha=HEAD_2,
            base_sha=BASE_SHA,
            max_active_findings=8,
            round_number=2,
            prior_inventory=r1_no_concern,
        )
        self.assertEqual(len(r2.inventory.obligations), 1)
        ob = r2.inventory.obligations[0]
        self.assertEqual(ob.lifecycle_state, "active-unresolved")
        self.assertEqual(ob.present_anchor.line, 14)
        self.assertTrue(r2.approved)

    def test_reopen_with_multiple_unsymbolled_findings_on_unchanged_file_does_not_hold(
        self,
    ) -> None:
        r1 = evaluate_drift_round(
            _review_result(
                _optional_comment(
                    "src/app.py", line=10, body="Note A.", evidence_id="ev-r1"
                )
            ),
            head_sha=HEAD_1,
            base_sha=BASE_SHA,
            max_active_findings=8,
            round_number=1,
        )
        prior_ob = r1.inventory.obligations[0]
        bound_inv = DriftInventory(
            obligations=(
                replace(
                    prior_ob,
                    criterion=replace(prior_ob.criterion, concern=None),
                    thread_binding=NativeThreadBinding(
                        thread_id="PRRT_thread_1",
                        comment_id=101,
                        placement="inline",
                        body_reason=None,
                        slot_state="active",
                        proof_sha256=_sha("proof-1"),
                    ),
                ),
            )
        )
        r2 = evaluate_drift_round(
            _review_result(),
            head_sha=HEAD_2,
            base_sha=BASE_SHA,
            max_active_findings=8,
            round_number=2,
            prior_inventory=bound_inv,
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

        r3 = evaluate_drift_round(
            _review_result(
                _optional_comment(
                    "src/app.py",
                    line=15,
                    body="New optional note 1.",
                    evidence_id="ev-r3-1",
                ),
                _optional_comment(
                    "src/app.py",
                    line=30,
                    body="New optional note 2.",
                    evidence_id="ev-r3-2",
                ),
            ),
            head_sha=HEAD_3,
            base_sha=BASE_SHA,
            max_active_findings=8,
            round_number=3,
            prior_inventory=r2.inventory,
        )
        self.assertTrue(r3.approved)
        self.assertFalse(r3.inventory.has_ambiguous_drift)
        by_id = {ob.obligation_id: ob for ob in r3.inventory.obligations}
        self.assertEqual(
            by_id[prior_ob.obligation_id].lifecycle_state, "verified-fixed"
        )

    def test_adr0134_file_copy_tier3_symbol_and_blocking_holds_remain_enforced(
        self,
    ) -> None:
        # 1) Genuine file_copy with symbol=None still enters ambiguous-multi-target.
        r1 = evaluate_drift_round(
            _review_result(_optional_comment("src/app.py", line=10, body="Note A.")),
            head_sha=HEAD_1,
            base_sha=BASE_SHA,
            max_active_findings=8,
            round_number=1,
        )
        r2_copy = evaluate_drift_round(
            _review_result(
                _optional_comment("src/app.py", line=10, body="Note A."),
                _optional_comment("src/copy.py", line=10, body="Copied note."),
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
        self.assertFalse(r2_copy.approved)
        self.assertEqual(
            r2_copy.inventory.obligations[0].lifecycle_state, "ambiguous-multi-target"
        )

        # 2) Tier 3 same-symbol multi-match on unchanged file still holds.
        r1_sym = evaluate_drift_round(
            _review_result(
                _optional_comment(
                    "src/app.py", line=10, body="Check A.", symbol="run_query"
                )
            ),
            head_sha=HEAD_1,
            base_sha=BASE_SHA,
            max_active_findings=8,
            round_number=1,
        )
        r2_sym = evaluate_drift_round(
            _review_result(
                _optional_comment(
                    "src/app.py", line=12, body="Check A1.", symbol="run_query"
                ),
                _optional_comment(
                    "src/app.py", line=22, body="Check A2.", symbol="run_query"
                ),
            ),
            head_sha=HEAD_2,
            base_sha=BASE_SHA,
            max_active_findings=8,
            round_number=2,
            prior_inventory=r1_sym.inventory,
        )
        self.assertFalse(r2_sym.approved)
        self.assertEqual(
            r2_sym.inventory.obligations[0].lifecycle_state, "ambiguous-multi-target"
        )

        # 3) Blocking finding omitted on unchanged file holds fail-closed.
        r1_blk = evaluate_drift_round(
            _review_result(
                _optional_comment(
                    "src/app.py",
                    line=10,
                    body="Validate input.",
                    blocking=True,
                )
            ),
            head_sha=HEAD_1,
            base_sha=BASE_SHA,
            max_active_findings=8,
            round_number=1,
        )
        r2_blk = evaluate_drift_round(
            _review_result(),
            head_sha=HEAD_4,
            base_sha=BASE_SHA,
            max_active_findings=8,
            round_number=2,
            prior_inventory=r1_blk.inventory,
        )
        self.assertFalse(r2_blk.approved)
        self.assertEqual(
            r2_blk.inventory.obligations[0].lifecycle_state, "omitted-uncertain"
        )
        self.assertTrue(r2_blk.inventory.obligations[0].effective_blocking_now)


if __name__ == "__main__":
    unittest.main()
