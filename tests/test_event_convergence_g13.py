"""G13: Missed, delayed, out-of-order, and queued-replaced event convergence tests."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from review_sensei_github.event_routing_contract import (
    MAX_NO_PROGRESS_RECONCILIATION_ATTEMPTS,
    RECONCILIATION_OWNERS,
    AcceptedOperationRecord,
    ConvergenceDecision,
    LivePullRequestTarget,
    QueuedRunCandidate,
    inspect_caller_subscription_contract,
    reconcile_event_convergence,
    resolve_reconciliation_owner,
    simulate_actions_concurrency_queue,
)
from review_sensei_github.webhooks import WebhookVerifier

from review_sensei.hosting.github import trigger as hosting_trigger

BASE_SHA_1 = "1" * 40
BASE_SHA_2 = "2" * 40
HEAD_SHA_1 = "a" * 40
HEAD_SHA_2 = "b" * 40
HEAD_SHA_3 = "c" * 40


def _live_target(
    *,
    head_sha: str = HEAD_SHA_2,
    base_sha: str = BASE_SHA_1,
    base_ref: str = "main",
    head_ref: str = "feat/convergence",
) -> LivePullRequestTarget:
    return LivePullRequestTarget(
        repository_id=101,
        pull_request_number=42,
        base_sha=base_sha,
        base_ref=base_ref,
        head_sha=head_sha,
        head_ref=head_ref,
    )


def _pr_sync_payload(
    *,
    head_sha: str,
    base_sha: str = BASE_SHA_1,
    base_ref: str = "main",
    head_ref: str = "feat/convergence",
) -> dict[str, Any]:
    return {
        "action": "synchronize",
        "pull_request": {
            "number": 42,
            "state": "open",
            "draft": False,
            "head": {"sha": head_sha, "ref": head_ref},
            "base": {"sha": base_sha, "ref": base_ref},
        },
    }


def _issue_comment_payload(
    body: str,
    *,
    comment_id: int = 5001,
    association: str = "MEMBER",
) -> dict[str, Any]:
    return {
        "action": "created",
        "issue": {
            "number": 42,
            "pull_request": {"url": "https://api.github.com/repos/owner/repo/pulls/42"},
        },
        "comment": {
            "id": comment_id,
            "body": body,
            "updated_at": "2026-11-11T01:00:00Z",
            "author_association": association,
            "user": {"login": "maintainer", "type": "User"},
        },
    }


def _comment_dedup_key(comment_id: int, body: str) -> str:
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return f"issue:{comment_id}:{digest}"


class EventConvergenceG13Tests(unittest.TestCase):
    def test_out_of_order_synchronize_h2_before_delayed_h1_wastes_zero_inference(
        self,
    ) -> None:
        live_h2 = _live_target(head_sha=HEAD_SHA_2)

        # H2 arrives first and is scheduled once against live H2.
        h2_decision = reconcile_event_convergence(
            "pull_request",
            _pr_sync_payload(head_sha=HEAD_SHA_2),
            live_target=live_h2,
            delivery_id="deliv-h2",
        )
        self.assertEqual(h2_decision.status, "schedule_current_target_review")
        self.assertTrue(h2_decision.schedules_new_inference)
        self.assertEqual(h2_decision.target_head_sha, HEAD_SHA_2)

        completed_h2 = AcceptedOperationRecord(
            operation_id="op-review-h2",
            operation_kind="review",
            target_base_sha=BASE_SHA_1,
            target_head_sha=HEAD_SHA_2,
            lifecycle_phase="completed",
            source_dedup_key="delivery:deliv-h2",
            open_finding_ids=("rs-h2-open",),
        )

        # Delayed synchronize(H1) arrives after H2 is live and completed.
        delayed_h1 = reconcile_event_convergence(
            "pull_request",
            _pr_sync_payload(head_sha=HEAD_SHA_1),
            live_target=live_h2,
            records=(completed_h2,),
            delivery_id="deliv-h1-late",
        )
        self.assertEqual(delayed_h1.status, "ignored_stale_event")
        self.assertEqual(
            delayed_h1.outcome_code, "delayed_out_of_order_head_superseded"
        )
        self.assertFalse(delayed_h1.schedules_new_inference)
        self.assertEqual(delayed_h1.reused_operation_id, "op-review-h2")
        self.assertIsNone(delayed_h1.recovery_command)
        self.assertEqual(delayed_h1.preserved_open_finding_ids, ("rs-h2-open",))

        # Delayed stale base_sha event before H2 completes points to workflow_dispatch.
        delayed_stale_base = reconcile_event_convergence(
            "pull_request",
            _pr_sync_payload(head_sha=HEAD_SHA_2, base_sha=BASE_SHA_2),
            live_target=live_h2,
            records=(),
            delivery_id="deliv-stale-base",
        )
        self.assertEqual(delayed_stale_base.status, "ignored_stale_event")
        self.assertFalse(delayed_stale_base.schedules_new_inference)
        self.assertEqual(delayed_stale_base.recovery_command, "workflow_dispatch")

    def test_duplicate_deliveries_and_retained_pending_results_reuse_without_inference(
        self,
    ) -> None:
        live_h2 = _live_target(head_sha=HEAD_SHA_2)
        cmd_body = "@reviewsensei rescan"
        cmd_key = _comment_dedup_key(5001, cmd_body)

        completed_records = (
            AcceptedOperationRecord(
                operation_id="op-review-h2",
                operation_kind="review",
                target_base_sha=BASE_SHA_1,
                target_head_sha=HEAD_SHA_2,
                lifecycle_phase="completed",
                source_dedup_key="delivery:deliv-h2",
                open_finding_ids=("rs-f1",),
            ),
            AcceptedOperationRecord(
                operation_id="op-cmd-5001",
                operation_kind="review",
                target_base_sha=BASE_SHA_1,
                target_head_sha=HEAD_SHA_2,
                lifecycle_phase="completed",
                source_dedup_key=cmd_key,
                command_text=cmd_body,
                open_finding_ids=("rs-f1",),
            ),
        )

        dup_webhook = reconcile_event_convergence(
            "pull_request",
            _pr_sync_payload(head_sha=HEAD_SHA_2),
            live_target=live_h2,
            records=completed_records,
            delivery_id="deliv-h2",
        )
        self.assertEqual(dup_webhook.status, "converged_reused")
        self.assertEqual(dup_webhook.outcome_code, "duplicate_delivery_reused")
        self.assertFalse(dup_webhook.schedules_new_inference)
        self.assertEqual(dup_webhook.reused_operation_id, "op-review-h2")
        self.assertEqual(dup_webhook.preserved_open_finding_ids, ("rs-f1",))

        dup_comment = reconcile_event_convergence(
            "issue_comment",
            _issue_comment_payload(cmd_body, comment_id=5001),
            live_target=live_h2,
            records=completed_records,
        )
        self.assertEqual(dup_comment.status, "converged_reused")
        self.assertEqual(dup_comment.outcome_code, "duplicate_delivery_reused")
        self.assertFalse(dup_comment.schedules_new_inference)
        self.assertEqual(dup_comment.reused_operation_id, "op-cmd-5001")

        # Retained result waiting for native publication reuses result with zero inference.
        retained_pending = AcceptedOperationRecord(
            operation_id="op-retained-h2",
            operation_kind="review",
            target_base_sha=BASE_SHA_1,
            target_head_sha=HEAD_SHA_2,
            lifecycle_phase="result_retained_publication_pending",
            source_dedup_key="delivery:deliv-retained",
            open_finding_ids=("rs-f2",),
        )
        for label, ev_name, ev_payload, deliv in (
            (
                "same_delivery",
                "pull_request",
                _pr_sync_payload(head_sha=HEAD_SHA_2),
                "deliv-retained",
            ),
            (
                "manual_dispatch",
                "workflow_dispatch",
                {"inputs": {"pull_request_number": 42}},
                "deliv-manual",
            ),
            (
                "verify_command",
                "issue_comment",
                _issue_comment_payload("@reviewsensei verify", comment_id=5009),
                "deliv-verify",
            ),
        ):
            with self.subTest(retained_recovery=label):
                reconciled = reconcile_event_convergence(
                    ev_name,
                    ev_payload,
                    live_target=live_h2,
                    records=(retained_pending,),
                    delivery_id=deliv,
                )
                self.assertEqual(reconciled.status, "reconcile_retained_publication")
                self.assertEqual(
                    reconciled.outcome_code, "retained_result_reused_without_inference"
                )
                self.assertFalse(reconciled.schedules_new_inference)
                self.assertTrue(reconciled.schedules_native_reconciliation)
                self.assertEqual(reconciled.reused_operation_id, "op-retained-h2")
                self.assertEqual(reconciled.preserved_open_finding_ids, ("rs-f2",))

    def test_dropped_synchronize_recovers_via_workflow_dispatch_or_comment_command(
        self,
    ) -> None:
        # H1 completed with unresolved obligation; synchronize(H2) webhook was dropped.
        prior_h1 = AcceptedOperationRecord(
            operation_id="op-review-h1",
            operation_kind="review",
            target_base_sha=BASE_SHA_1,
            target_head_sha=HEAD_SHA_1,
            lifecycle_phase="completed",
            open_finding_ids=("rs-carried-1",),
        )
        live_h2 = _live_target(head_sha=HEAD_SHA_2)

        # `@reviewsensei verify` alone never triggers hidden model inference on unreviewed H2.
        verify_unreviewed = reconcile_event_convergence(
            "issue_comment",
            _issue_comment_payload("@reviewsensei verify", comment_id=6001),
            live_target=live_h2,
            records=(prior_h1,),
        )
        self.assertEqual(verify_unreviewed.status, "held_unreviewed_live_head")
        self.assertEqual(
            verify_unreviewed.outcome_code, "verify_requires_reviewed_live_snapshot"
        )
        self.assertFalse(verify_unreviewed.schedules_new_inference)
        self.assertEqual(verify_unreviewed.recovery_command, "workflow_dispatch")
        self.assertEqual(
            verify_unreviewed.preserved_open_finding_ids, ("rs-carried-1",)
        )

        # `workflow_dispatch` converges onto H2 in one bounded review while preserving obligations.
        manual_recovery = reconcile_event_convergence(
            "workflow_dispatch",
            {"inputs": {"pull_request_number": 42}},
            live_target=live_h2,
            records=(prior_h1,),
        )
        self.assertEqual(manual_recovery.status, "schedule_current_target_review")
        self.assertEqual(
            manual_recovery.outcome_code, "missed_or_advanced_head_converged"
        )
        self.assertTrue(manual_recovery.schedules_new_inference)
        self.assertEqual(manual_recovery.target_head_sha, HEAD_SHA_2)
        self.assertEqual(manual_recovery.preserved_open_finding_ids, ("rs-carried-1",))

        # `@reviewsensei rescan` comment command also converges onto live H2.
        rescan_recovery = reconcile_event_convergence(
            "issue_comment",
            _issue_comment_payload("@reviewsensei rescan", comment_id=6002),
            live_target=live_h2,
            records=(prior_h1,),
        )
        self.assertEqual(rescan_recovery.status, "schedule_current_target_review")
        self.assertEqual(
            rescan_recovery.outcome_code, "missed_or_advanced_head_converged"
        )
        self.assertTrue(rescan_recovery.schedules_new_inference)

        # Once H2 completes, verify reconciles natively with zero inference.
        completed_h2 = AcceptedOperationRecord(
            operation_id="op-review-h2",
            operation_kind="review",
            target_base_sha=BASE_SHA_1,
            target_head_sha=HEAD_SHA_2,
            lifecycle_phase="completed",
            open_finding_ids=("rs-carried-1",),
        )
        verify_reviewed = reconcile_event_convergence(
            "issue_comment",
            _issue_comment_payload("@reviewsensei verify", comment_id=6003),
            live_target=live_h2,
            records=(prior_h1, completed_h2),
        )
        self.assertEqual(verify_reviewed.status, "schedule_command_reconciliation")
        self.assertEqual(
            verify_reviewed.outcome_code, "command_reconciles_current_target"
        )
        self.assertFalse(verify_reviewed.schedules_new_inference)
        self.assertTrue(verify_reviewed.schedules_native_reconciliation)
        self.assertEqual(verify_reviewed.reused_operation_id, "op-review-h2")

        # Repeated workflow_dispatch on completed H2 reuses H2 with zero inference.
        second_dispatch = reconcile_event_convergence(
            "workflow_dispatch",
            {"inputs": {"pull_request_number": 42}},
            live_target=live_h2,
            records=(prior_h1, completed_h2),
        )
        self.assertEqual(second_dispatch.status, "converged_reused")
        self.assertFalse(second_dispatch.schedules_new_inference)
        self.assertEqual(second_dispatch.reused_operation_id, "op-review-h2")

    def test_uncertain_dispatch_and_active_writer_hold_without_duplicate_inference(
        self,
    ) -> None:
        live_h2 = _live_target(head_sha=HEAD_SHA_2)

        uncertain = AcceptedOperationRecord(
            operation_id="op-uncertain-1",
            operation_kind="review",
            target_base_sha=BASE_SHA_1,
            target_head_sha=HEAD_SHA_1,
            lifecycle_phase="uncertain_dispatch",
            open_finding_ids=("rs-f9",),
        )
        held_uncertain = reconcile_event_convergence(
            "workflow_dispatch",
            {"inputs": {"pull_request_number": 42}},
            live_target=live_h2,
            records=(uncertain,),
        )
        self.assertEqual(held_uncertain.status, "held_uncertain_operation")
        self.assertEqual(
            held_uncertain.outcome_code, "uncertain_dispatch_reconciliation_required"
        )
        self.assertFalse(held_uncertain.schedules_new_inference)
        self.assertEqual(held_uncertain.recovery_command, "@reviewsensei review status")
        self.assertEqual(held_uncertain.preserved_open_finding_ids, ("rs-f9",))

        active = AcceptedOperationRecord(
            operation_id="op-active-h2",
            operation_kind="review",
            target_base_sha=BASE_SHA_1,
            target_head_sha=HEAD_SHA_2,
            lifecycle_phase="execution_started",
            open_finding_ids=("rs-f9",),
        )
        held_active = reconcile_event_convergence(
            "pull_request",
            _pr_sync_payload(head_sha=HEAD_SHA_2),
            live_target=live_h2,
            records=(active,),
        )
        self.assertEqual(held_active.status, "held_active_writer")
        self.assertEqual(held_active.outcome_code, "active_operation_in_progress")
        self.assertFalse(held_active.schedules_new_inference)
        self.assertEqual(held_active.reused_operation_id, "op-active-h2")

    def test_queued_replaced_command_and_review_runs_expose_explicit_recovery(
        self,
    ) -> None:
        r1 = QueuedRunCandidate(
            run_id="run-1001",
            event_name="pull_request",
            operation_kind="review",
            target_head_sha=HEAD_SHA_1,
        )
        cmd_override = "@reviewsensei override rs-123456 acceptable risk"
        c1 = QueuedRunCandidate(
            run_id="run-1002",
            event_name="issue_comment",
            operation_kind="command",
            target_head_sha=HEAD_SHA_1,
            source_dedup_key=_comment_dedup_key(7001, cmd_override),
            command_text=cmd_override,
            accepted_before_queue=True,
        )
        r2 = QueuedRunCandidate(
            run_id="run-1003",
            event_name="pull_request",
            operation_kind="review",
            target_head_sha=HEAD_SHA_1,
        )
        r3 = QueuedRunCandidate(
            run_id="run-1004",
            event_name="pull_request",
            operation_kind="review",
            target_head_sha=HEAD_SHA_2,
        )
        rep1 = QueuedRunCandidate(
            run_id="run-1005",
            event_name="pull_request_review_comment",
            operation_kind="reply",
            target_head_sha=HEAD_SHA_2,
        )
        r4 = QueuedRunCandidate(
            run_id="run-1006",
            event_name="workflow_dispatch",
            operation_kind="review",
            target_head_sha=HEAD_SHA_2,
        )

        sim = simulate_actions_concurrency_queue(
            repository="owner/repo",
            pull_request_number=42,
            events=(
                ("enqueue", r1),
                ("enqueue", c1),
                ("enqueue", r2),  # replaces pending command c1
                ("enqueue", r3),  # replaces pending review r2 (different head)
                ("finish_active", None),  # r1 finishes, r3 becomes active
                ("enqueue", rep1),
                ("enqueue", r4),  # replaces pending reply rep1
                ("finish_active", None),  # r3 finishes, r4 becomes active
                ("finish_active", None),  # r4 finishes
            ),
        )
        self.assertEqual(
            sim.concurrency_group, "reviewsensei-provider-review-owner/repo-42"
        )
        self.assertEqual(sim.completed_run_ids, ("run-1001", "run-1004", "run-1006"))
        self.assertIsNone(sim.active_run)
        self.assertIsNone(sim.pending_run)
        self.assertEqual(len(sim.replaced_notices), 3)

        cmd_notice, rev_notice, rep_notice = sim.replaced_notices
        self.assertEqual(cmd_notice.replaced_run_id, "run-1002")
        self.assertEqual(cmd_notice.outcome_code, "queued_command_replaced")
        self.assertTrue(cmd_notice.accepted_before_queue)
        self.assertEqual(cmd_notice.recovery_command, cmd_override)

        self.assertEqual(rev_notice.replaced_run_id, "run-1003")
        self.assertEqual(rev_notice.outcome_code, "queued_review_superseded")
        self.assertEqual(rev_notice.recovery_command, "workflow_dispatch")

        self.assertEqual(rep_notice.replaced_run_id, "run-1005")
        self.assertEqual(rep_notice.outcome_code, "queued_reply_replaced")

        # Verify reconcile_event_convergence surfaces explicit queued-replaced state.
        live_h1 = _live_target(head_sha=HEAD_SHA_1)
        replaced_cmd_decision = reconcile_event_convergence(
            "issue_comment",
            _issue_comment_payload(cmd_override, comment_id=7001),
            live_target=live_h1,
            was_queued_replaced=True,
        )
        self.assertEqual(replaced_cmd_decision.status, "held_queued_replaced")
        self.assertEqual(replaced_cmd_decision.outcome_code, "queued_command_replaced")
        self.assertFalse(replaced_cmd_decision.schedules_new_inference)
        self.assertEqual(replaced_cmd_decision.recovery_command, cmd_override)

        replaced_rev_decision = reconcile_event_convergence(
            "pull_request",
            _pr_sync_payload(head_sha=HEAD_SHA_1),
            live_target=live_h1,
            was_queued_replaced=True,
        )
        self.assertEqual(replaced_rev_decision.status, "held_queued_replaced")
        self.assertEqual(
            replaced_rev_decision.outcome_code,
            "queued_run_superseded_before_admission",
        )
        self.assertEqual(replaced_rev_decision.recovery_command, "workflow_dispatch")

        # Unstarted accepted command intent resumes once on same head, or holds if head drifted.
        accepted_cmd_intent = AcceptedOperationRecord(
            operation_id="op-intent-7001",
            operation_kind="command",
            target_base_sha=BASE_SHA_1,
            target_head_sha=HEAD_SHA_1,
            lifecycle_phase="intent_accepted_unstarted",
            source_dedup_key=_comment_dedup_key(7001, cmd_override),
            command_text=cmd_override,
            open_finding_ids=("rs-123456",),
        )
        resumed_same_head = reconcile_event_convergence(
            "issue_comment",
            _issue_comment_payload(cmd_override, comment_id=7001),
            live_target=live_h1,
            records=(accepted_cmd_intent,),
        )
        self.assertEqual(resumed_same_head.status, "resume_accepted_intent")
        self.assertEqual(
            resumed_same_head.outcome_code, "unstarted_accepted_intent_resumed"
        )
        self.assertFalse(resumed_same_head.schedules_new_inference)
        self.assertTrue(resumed_same_head.schedules_native_reconciliation)
        self.assertEqual(resumed_same_head.reused_operation_id, "op-intent-7001")

        drifted_head = reconcile_event_convergence(
            "issue_comment",
            _issue_comment_payload(cmd_override, comment_id=7001),
            live_target=_live_target(head_sha=HEAD_SHA_3),
            records=(accepted_cmd_intent,),
        )
        self.assertEqual(drifted_head.status, "held_intent_superseded")
        self.assertEqual(drifted_head.outcome_code, "accepted_intent_head_drifted")
        self.assertFalse(drifted_head.schedules_new_inference)
        self.assertEqual(drifted_head.recovery_command, cmd_override)

    def test_reply_and_validation_guards_behave_deterministically(self) -> None:
        live_h2 = _live_target(head_sha=HEAD_SHA_2)
        reply_conv = reconcile_event_convergence(
            "issue_comment",
            _issue_comment_payload(
                "@reviewsensei why is this flagged?", comment_id=8001
            ),
            live_target=live_h2,
        )
        self.assertEqual(reply_conv.status, "schedule_reply")
        self.assertTrue(reply_conv.schedules_new_inference)

        not_admitted = reconcile_event_convergence(
            "merge_group",
            {"action": "checks_requested"},
            live_target=live_h2,
        )
        self.assertEqual(not_admitted.status, "not_admitted")
        self.assertEqual(not_admitted.outcome_code, "merge_group_unsupported")
        self.assertFalse(not_admitted.schedules_new_inference)
        self.assertIsNone(not_admitted.recovery_command)

        not_admitted_with_recovery = reconcile_event_convergence(
            "pull_request_review_thread",
            {
                "action": "resolved",
                "pull_request": {"number": 42},
                "sender": {"login": "alice", "type": "User"},
            },
            live_target=live_h2,
        )
        self.assertEqual(not_admitted_with_recovery.status, "not_admitted")
        self.assertEqual(
            not_admitted_with_recovery.outcome_code, "thread_event_manual_verify_only"
        )
        self.assertEqual(
            not_admitted_with_recovery.recovery_command, "@reviewsensei verify"
        )

        status_cmd_unreviewed = reconcile_event_convergence(
            "issue_comment",
            _issue_comment_payload("@reviewsensei review status", comment_id=8002),
            live_target=live_h2,
        )
        self.assertEqual(
            status_cmd_unreviewed.status, "schedule_command_reconciliation"
        )
        self.assertEqual(
            status_cmd_unreviewed.outcome_code, "command_reconciles_current_target"
        )
        self.assertFalse(status_cmd_unreviewed.schedules_new_inference)
        self.assertTrue(status_cmd_unreviewed.schedules_native_reconciliation)
        self.assertIsNone(status_cmd_unreviewed.reused_operation_id)

        unstarted_review_intent = AcceptedOperationRecord(
            operation_id="op-intent-rev-h2",
            operation_kind="review",
            target_base_sha=BASE_SHA_1,
            target_head_sha=HEAD_SHA_2,
            lifecycle_phase="intent_accepted_unstarted",
            source_dedup_key="delivery:deliv-unstarted-h2",
        )
        resumed_review = reconcile_event_convergence(
            "pull_request",
            _pr_sync_payload(head_sha=HEAD_SHA_2),
            live_target=live_h2,
            records=(unstarted_review_intent,),
            delivery_id="deliv-unstarted-h2",
        )
        self.assertEqual(resumed_review.status, "resume_accepted_intent")
        self.assertTrue(resumed_review.schedules_new_inference)
        self.assertFalse(resumed_review.schedules_native_reconciliation)
        self.assertEqual(resumed_review.reused_operation_id, "op-intent-rev-h2")

        with self.assertRaises(ValueError):
            LivePullRequestTarget(
                repository_id=0,
                pull_request_number=42,
                base_sha=BASE_SHA_1,
                base_ref="main",
                head_sha=HEAD_SHA_1,
                head_ref="feat/x",
            )
        with self.assertRaises(ValueError):
            AcceptedOperationRecord(
                operation_id="op-bad",
                operation_kind="review",
                target_base_sha=BASE_SHA_1,
                target_head_sha=HEAD_SHA_1,
                lifecycle_phase="invalid_phase",
            )
        with self.assertRaises(ValueError):
            simulate_actions_concurrency_queue(
                repository="owner/repo",
                pull_request_number=42,
                events=(("unknown_step", None),),
            )

    def test_finite_no_progress_stop_rule_and_reconciliation_ownership(self) -> None:
        live_h2 = _live_target(head_sha=HEAD_SHA_2)

        no_prog_1 = AcceptedOperationRecord(
            operation_id="op-noprog-1",
            operation_kind="command",
            target_base_sha=BASE_SHA_1,
            target_head_sha=HEAD_SHA_2,
            lifecycle_phase="completed",
            open_finding_ids=("rs-open-1",),
            state_fingerprint="fp-v1",
            made_progress=False,
        )
        # After 1 unchanged non-progressing attempt, reconciliation is still permitted once more.
        after_one = reconcile_event_convergence(
            "issue_comment",
            _issue_comment_payload("@reviewsensei review status", comment_id=9001),
            live_target=live_h2,
            records=(no_prog_1,),
            current_state_fingerprint="fp-v1",
        )
        self.assertEqual(after_one.unchanged_attempts, 1)
        self.assertFalse(after_one.stopped_no_progress)
        self.assertEqual(after_one.reconciliation_owner, "hosted_command_executor")
        self.assertEqual(after_one.status, "schedule_command_reconciliation")

        no_prog_2 = AcceptedOperationRecord(
            operation_id="op-noprog-2",
            operation_kind="command",
            target_base_sha=BASE_SHA_1,
            target_head_sha=HEAD_SHA_2,
            lifecycle_phase="completed",
            open_finding_ids=("rs-open-1",),
            state_fingerprint="fp-v1",
            made_progress=False,
        )
        self.assertEqual(MAX_NO_PROGRESS_RECONCILIATION_ATTEMPTS, 2)

        # At >= MAX_NO_PROGRESS_RECONCILIATION_ATTEMPTS on unchanged state, all admitted
        # reconciliation routes halt deterministically without paid retries or background loops.
        for label, ev_name, payload in (
            (
                "command_status",
                "issue_comment",
                _issue_comment_payload("@reviewsensei review status", comment_id=9002),
            ),
            (
                "rescan_comment",
                "issue_comment",
                _issue_comment_payload("@reviewsensei rescan", comment_id=9003),
            ),
            (
                "manual_dispatch",
                "workflow_dispatch",
                {"inputs": {"pull_request_number": 42}},
            ),
            (
                "reply_mention",
                "issue_comment",
                _issue_comment_payload("@reviewsensei explain this", comment_id=9004),
            ),
        ):
            with self.subTest(route=label):
                stopped = reconcile_event_convergence(
                    ev_name,
                    payload,
                    live_target=live_h2,
                    records=(no_prog_1, no_prog_2),
                    current_state_fingerprint="fp-v1",
                )
                self.assertEqual(stopped.status, "stopped_no_progress")
                self.assertEqual(stopped.outcome_code, "finite_no_progress_stop")
                self.assertTrue(stopped.stopped_no_progress)
                self.assertEqual(stopped.unchanged_attempts, 2)
                self.assertFalse(stopped.schedules_new_inference)
                self.assertFalse(stopped.schedules_native_reconciliation)
                self.assertEqual(stopped.reused_operation_id, "op-noprog-2")
                self.assertIsNone(stopped.recovery_command)
                self.assertEqual(
                    stopped.reconciliation_owner, "operator_manual_recovery"
                )
                self.assertIn(stopped.reconciliation_owner, RECONCILIATION_OWNERS)

        # Unsupported / non-admitted events remain owned by caller_workflow_preflight.
        unsubscribed = reconcile_event_convergence(
            "merge_group",
            {"action": "checks_requested"},
            live_target=live_h2,
            records=(no_prog_1, no_prog_2),
            current_state_fingerprint="fp-v1",
        )
        self.assertEqual(unsubscribed.status, "not_admitted")
        self.assertFalse(unsubscribed.stopped_no_progress)
        self.assertEqual(unsubscribed.reconciliation_owner, "caller_workflow_preflight")

        # Advancing state_fingerprint OR head_sha OR recording made_progress=True resets counter!
        advanced_fp = reconcile_event_convergence(
            "workflow_dispatch",
            {"inputs": {"pull_request_number": 42}},
            live_target=live_h2,
            records=(no_prog_1, no_prog_2),
            current_state_fingerprint="fp-v2",
        )
        self.assertEqual(advanced_fp.unchanged_attempts, 0)
        self.assertFalse(advanced_fp.stopped_no_progress)
        self.assertEqual(advanced_fp.status, "schedule_current_target_review")
        self.assertEqual(advanced_fp.reconciliation_owner, "hosted_review_admission")

        advanced_head = reconcile_event_convergence(
            "pull_request",
            _pr_sync_payload(head_sha=HEAD_SHA_3),
            live_target=_live_target(head_sha=HEAD_SHA_3),
            records=(no_prog_1, no_prog_2),
            current_state_fingerprint="fp-v1",
        )
        self.assertEqual(advanced_head.unchanged_attempts, 0)
        self.assertFalse(advanced_head.stopped_no_progress)
        self.assertEqual(advanced_head.status, "schedule_current_target_review")

        progressed_rec = AcceptedOperationRecord(
            operation_id="op-prog-3",
            operation_kind="review",
            target_base_sha=BASE_SHA_1,
            target_head_sha=HEAD_SHA_2,
            lifecycle_phase="completed",
            open_finding_ids=(),
            state_fingerprint="fp-v1",
            made_progress=True,
        )
        after_progress = reconcile_event_convergence(
            "issue_comment",
            _issue_comment_payload("@reviewsensei verify", comment_id=9005),
            live_target=live_h2,
            records=(no_prog_1, no_prog_2, progressed_rec),
            current_state_fingerprint="fp-v1",
        )
        self.assertEqual(after_progress.unchanged_attempts, 0)
        self.assertFalse(after_progress.stopped_no_progress)
        self.assertEqual(after_progress.status, "schedule_command_reconciliation")
        self.assertEqual(after_progress.reconciliation_owner, "hosted_command_executor")

        # Validation guards on new G13 fields
        with self.assertRaises(ValueError):
            AcceptedOperationRecord(
                operation_id="bad-bool",
                operation_kind="review",
                target_base_sha=BASE_SHA_1,
                target_head_sha=HEAD_SHA_2,
                lifecycle_phase="completed",
                made_progress=1,  # type: ignore[arg-type]
            )
        with self.assertRaises(ValueError):
            AcceptedOperationRecord(
                operation_id="bad-fp",
                operation_kind="review",
                target_base_sha=BASE_SHA_1,
                target_head_sha=HEAD_SHA_2,
                lifecycle_phase="completed",
                state_fingerprint="   ",
            )
        with self.assertRaises(ValueError):
            reconcile_event_convergence(
                "workflow_dispatch",
                {"inputs": {"pull_request_number": 42}},
                live_target=live_h2,
                max_no_progress_attempts=0,
            )
        with self.assertRaises(ValueError):
            reconcile_event_convergence(
                "workflow_dispatch",
                {"inputs": {"pull_request_number": 42}},
                live_target=live_h2,
                current_state_fingerprint="",
            )
        with self.assertRaises(ValueError):
            ConvergenceDecision(
                status="converged_reused",
                outcome_code="ok",
                target_base_sha=BASE_SHA_1,
                target_head_sha=HEAD_SHA_2,
                routing=after_progress.routing,
                schedules_new_inference=False,
                schedules_native_reconciliation=False,
                reused_operation_id=None,
                preserved_open_finding_ids=(),
                recovery_command=None,
                reconciliation_owner="unknown_owner",
            )
        self.assertEqual(
            resolve_reconciliation_owner(
                after_progress.routing, status="schedule_command_reconciliation"
            ),
            "hosted_command_executor",
        )


class _StaticSecret:
    def __init__(self, secret: bytes = b"g13-webhook-secret") -> None:
        self._secret = secret

    def load_webhook_secret(self) -> bytes:
        return self._secret


class SubscribedCallerToHandlerJourneyG13Tests(unittest.TestCase):
    """Exercise .github/workflows/review-sensei-review.yml + trigger.py -> webhooks.py / action_run.py."""

    def setUp(self) -> None:
        for target in ("socket.create_connection", "socket.socket.connect"):
            guard = patch(target, side_effect=AssertionError("unexpected network"))
            guard.start()
            self.addCleanup(guard.stop)

    def test_failed_convergence_does_not_consume_delivery(self):
        verifier = WebhookVerifier(app_id=123, secret_source=_StaticSecret())
        body, signature = self._sign(_pr_sync_payload(head_sha=HEAD_SHA_2))
        arguments = dict(
            body=body,
            signature=signature,
            event="pull_request",
            delivery_id="failed-convergence",
            live_target=_live_target(),
        )
        with patch(
            "review_sensei_github.event_routing_contract.reconcile_event_convergence",
            side_effect=RuntimeError("synthetic route failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "synthetic route failure"):
                verifier.verify_event_route(**arguments)
        self.assertFalse(
            verifier.ledger.is_known(app_id=123, delivery_id="failed-convergence")
        )
        retried = verifier.verify_event_route(**arguments)
        self.assertFalse(retried.duplicate_delivery_id)
        self.assertTrue(retried.convergence.schedules_new_inference)

    @staticmethod
    def _sign(
        payload: dict[str, Any], secret: bytes = b"g13-webhook-secret"
    ) -> tuple[bytes, str]:
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        sig = "sha256=" + hmac.new(secret, raw, hashlib.sha256).hexdigest()
        return raw, sig

    @staticmethod
    def _run_trigger_cli(
        directory: Path,
        *,
        event_name: str,
        pull: dict[str, Any],
        comment_body: str = "",
    ) -> dict[str, str]:
        pull_path = directory / f"pull-{event_name}.json"
        out_path = directory / f"output-{event_name}.txt"
        pull_path.write_text(json.dumps(pull), encoding="utf-8")
        out_path.write_text("", encoding="utf-8")
        with patch.dict(os.environ, {"GITHUB_OUTPUT": str(out_path)}, clear=False):
            rc = hosting_trigger.main(
                [
                    "--event",
                    event_name,
                    "--comment-body",
                    comment_body,
                    "--pull-json",
                    str(pull_path),
                ]
            )
        assert rc == 0
        parsed: dict[str, str] = {}
        for line in out_path.read_text(encoding="utf-8").splitlines():
            key, val = line.split("=", 1)
            parsed[key] = val
        return parsed

    def test_installed_workflow_and_trigger_to_webhook_journeys_across_delivery_anomalies(
        self,
    ) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        workflow_yaml = (
            repo_root / ".github" / "workflows" / "review-sensei-review.yml"
        ).read_text(encoding="utf-8")
        report = inspect_caller_subscription_contract(workflow_yaml)
        self.assertTrue(report.matches_approved_boundary)

        verifier = WebhookVerifier(app_id=77, secret_source=_StaticSecret())
        live_h2 = _live_target(head_sha=HEAD_SHA_2)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)

            # 1. Reordered + duplicate webhook delivery through trigger.py -> WebhookVerifier.verify_event_route
            h2_payload = {
                **_pr_sync_payload(head_sha=HEAD_SHA_2),
                "repository": {"id": 101, "full_name": "owner/repo"},
            }
            h2_cli = self._run_trigger_cli(
                tmp_dir,
                event_name="pull_request",
                pull=h2_payload["pull_request"],
            )
            self.assertEqual(h2_cli["operation"], "review")
            self.assertEqual(h2_cli["head_sha"], HEAD_SHA_2)

            raw_h2, sig_h2 = self._sign(h2_payload)
            first_h2 = verifier.verify_event_route(
                body=raw_h2,
                signature=sig_h2,
                event="pull_request",
                delivery_id="deliv-h2-live",
                live_target=live_h2,
            )
            self.assertFalse(first_h2.duplicate_delivery_id)
            assert first_h2.convergence is not None
            self.assertTrue(first_h2.convergence.schedules_new_inference)
            self.assertEqual(
                first_h2.convergence.reconciliation_owner, "hosted_review_admission"
            )

            # Model a caller interruption after receipt but before admission: no
            # accepted history exists, so redelivery must reconsider admission.
            dup_h2 = verifier.verify_event_route(
                body=raw_h2,
                signature=sig_h2,
                event="pull_request",
                delivery_id="deliv-h2-live",
                live_target=live_h2,
            )
            self.assertTrue(dup_h2.duplicate_delivery_id)
            assert dup_h2.convergence is not None
            self.assertTrue(dup_h2.convergence.schedules_new_inference)
            self.assertEqual(dup_h2.convergence, first_h2.convergence)

            uncertain_redelivery = verifier.verify_event_route(
                body=raw_h2,
                signature=sig_h2,
                event="pull_request",
                delivery_id="deliv-h2-live",
                live_target=live_h2,
                records=(
                    AcceptedOperationRecord(
                        operation_id="op-h2-uncertain",
                        operation_kind="review",
                        target_base_sha=BASE_SHA_1,
                        target_head_sha=HEAD_SHA_2,
                        lifecycle_phase="uncertain_dispatch",
                    ),
                ),
            )
            self.assertTrue(uncertain_redelivery.duplicate_delivery_id)
            self.assertEqual(
                uncertain_redelivery.convergence.status, "held_uncertain_operation"
            )
            self.assertFalse(uncertain_redelivery.convergence.schedules_new_inference)

            # Delayed out-of-order synchronize(H1) arriving when live PR is already at H2
            h1_payload = {
                **_pr_sync_payload(head_sha=HEAD_SHA_1),
                "repository": {"id": 101, "full_name": "owner/repo"},
            }
            raw_h1, sig_h1 = self._sign(h1_payload)
            late_h1 = verifier.verify_event_route(
                body=raw_h1,
                signature=sig_h1,
                event="pull_request",
                delivery_id="deliv-h1-delayed",
                live_target=live_h2,
            )
            assert late_h1.convergence is not None
            self.assertEqual(late_h1.convergence.status, "ignored_stale_event")
            self.assertFalse(late_h1.convergence.schedules_new_inference)

            # 2. Dropped synchronize(H3) recovered via workflow_dispatch and @reviewsensei rescan
            live_h3 = _live_target(head_sha=HEAD_SHA_3)
            completed_h2 = AcceptedOperationRecord(
                operation_id="op-h2-done",
                operation_kind="review",
                target_base_sha=BASE_SHA_1,
                target_head_sha=HEAD_SHA_2,
                lifecycle_phase="completed",
                open_finding_ids=("rs-carry-h2",),
            )
            accepted_redelivery = verifier.verify_event_route(
                body=raw_h2,
                signature=sig_h2,
                event="pull_request",
                delivery_id="deliv-h2-live",
                live_target=live_h2,
                records=(completed_h2,),
            )
            self.assertTrue(accepted_redelivery.duplicate_delivery_id)
            self.assertFalse(accepted_redelivery.convergence.schedules_new_inference)
            self.assertEqual(
                accepted_redelivery.convergence.reused_operation_id, "op-h2-done"
            )

            dispatch_cli = self._run_trigger_cli(
                tmp_dir,
                event_name="workflow_dispatch",
                pull=live_h3.to_pull_mapping(),
            )
            self.assertEqual(dispatch_cli["operation"], "review")
            self.assertEqual(dispatch_cli["head_sha"], HEAD_SHA_3)

            raw_disp, sig_disp = self._sign(
                {
                    "inputs": {"pull_request_number": 42},
                    "repository": {"id": 101, "full_name": "owner/repo"},
                }
            )
            disp_route = verifier.verify_event_route(
                body=raw_disp,
                signature=sig_disp,
                event="workflow_dispatch",
                delivery_id="deliv-manual-h3",
                live_target=live_h3,
                records=(completed_h2,),
            )
            assert disp_route.convergence is not None
            self.assertEqual(
                disp_route.convergence.status, "schedule_current_target_review"
            )
            self.assertEqual(
                disp_route.convergence.outcome_code, "missed_or_advanced_head_converged"
            )
            self.assertEqual(
                disp_route.convergence.preserved_open_finding_ids, ("rs-carry-h2",)
            )

            # 3. Queued-replaced command run through trigger.py -> WebhookVerifier -> action_run.py
            verify_cli = self._run_trigger_cli(
                tmp_dir,
                event_name="issue_comment",
                pull=live_h3.to_pull_mapping(),
                comment_body="@reviewsensei review status",
            )
            self.assertEqual(verify_cli["operation"], "command")

            raw_cmd, sig_cmd = self._sign(
                {
                    **_issue_comment_payload(
                        "@reviewsensei review status", comment_id=777
                    ),
                    "repository": {"id": 101, "full_name": "owner/repo"},
                }
            )
            replaced_route = verifier.verify_event_route(
                body=raw_cmd,
                signature=sig_cmd,
                event="issue_comment",
                delivery_id="deliv-cmd-replaced",
                live_target=live_h3,
                was_queued_replaced=True,
            )
            assert replaced_route.convergence is not None
            self.assertEqual(replaced_route.convergence.status, "held_queued_replaced")
            self.assertEqual(
                replaced_route.convergence.reconciliation_owner,
                "operator_manual_recovery",
            )

    def test_action_run_enforces_finite_stop_and_queued_replacement_before_execution(
        self,
    ) -> None:
        import io
        from contextlib import ExitStack, redirect_stderr, redirect_stdout

        from review_sensei_github import action_run, review_run
        from review_sensei_github.api_run_requests import ApiRunRequestTransport
        from review_sensei_github.broker_client import BrokerClient
        from review_sensei_github.http import GitHubHttp
        from test_action_run import CommandEntryFixture
        from test_review_run import fixture_process_environment

        def _invoke_action(
            fix: CommandEntryFixture,
            *,
            convergence_records: tuple[AcceptedOperationRecord, ...] = (),
            current_state_fingerprint: str | None = None,
            was_queued_replaced: bool = False,
        ) -> tuple[int, str]:
            fix.select_command("@reviewsensei review status")
            ev_path = fix.root / "g13-event.json"
            ev_path.write_text(
                json.dumps(
                    {
                        "action": "created",
                        "issue": {"number": 7, "pull_request": {}},
                        "comment": dict(fix.source),
                    }
                ),
                encoding="utf-8",
            )
            env = {
                **fixture_process_environment(),
                "GITHUB_RUN_ID": str(fix.run_id),
                "GITHUB_RUN_ATTEMPT": str(fix.attempt),
                "GITHUB_EVENT_NAME": "issue_comment",
                "GITHUB_EVENT_PATH": str(ev_path),
                "REVIEW_SENSEI_WORKFLOW_REF": "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@refs/tags/v5",
                "REVIEW_SENSEI_WORKFLOW_SHA": "a" * 40,
                "GITHUB_ACTOR": "alice",
                "GITHUB_TRIGGERING_ACTOR": "alice",
                "GITHUB_TOKEN": "synthetic-workflow-read",
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic",
                "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.github.test/token?fixture=1",
                "REVIEWSENSEI_APP_ID": "0",
            }

            def _caller_factory(**kw: Any) -> ApiRunRequestTransport:
                caller = ApiRunRequestTransport(**kw)
                fix.accounted = caller
                fix.transports.append(caller)
                return caller

            out, err = io.StringIO(), io.StringIO()
            prev = Path.cwd()
            try:
                os.chdir(fix.root)
                with ExitStack() as stack:
                    for ctx in (
                        patch.dict(os.environ, env, clear=True),
                        patch.object(review_run, "physical_opener", fix.external),
                        patch.object(
                            review_run,
                            "ApiRunRequestTransport",
                            side_effect=_caller_factory,
                        ),
                        patch.object(
                            review_run,
                            "BrokerClient",
                            side_effect=lambda **kw: BrokerClient(
                                broker_url="https://broker.github.test/api/github/token",
                                **kw,
                            ),
                        ),
                        patch.object(
                            review_run,
                            "GitHubHttp",
                            side_effect=lambda **kw: GitHubHttp(
                                api_url="https://api.github.test", **kw
                            ),
                        ),
                        redirect_stdout(out),
                        redirect_stderr(err),
                    ):
                        stack.enter_context(ctx)
                    rc = action_run.run_action(
                        [
                            "--policy-commit",
                            fix.policy,
                            "--run-directory",
                            str(fix.root),
                            "--",
                            "--repository",
                            "owner/repo",
                            "--repository-id",
                            "123",
                            "--pull-request",
                            "7",
                            "--head-sha",
                            fix.requested_head,
                            "--source-comment-id",
                            str(fix.SOURCE_ID),
                            "--comment-body",
                            fix.command_body,
                            "--actor",
                            "alice",
                            "--association",
                            "OWNER",
                            "--config",
                            str(fix.root / ".reviewsensei.yml"),
                            "--github-session-ledger",
                            "--allow-write",
                        ],
                        purpose="command",
                        convergence_records=convergence_records,
                        current_state_fingerprint=current_state_fingerprint,
                        was_queued_replaced=was_queued_replaced,
                    )
                    return rc, out.getvalue()
            except Exception as exc:
                return 1, str(exc)
            finally:
                os.chdir(prev)

        # 1. Queued-replaced action run halts before business execution
        with tempfile.TemporaryDirectory() as d1:
            f1 = CommandEntryFixture(d1)
            rc1, msg1 = _invoke_action(f1, was_queued_replaced=True)
            self.assertNotEqual(rc1, 0)
            self.assertIn("queued command run was replaced before execution", msg1)
            self.assertEqual(f1.business_writes, 0)

        # 2. Finite no-progress stop rule halts action_run after 2 unchanged attempts
        with tempfile.TemporaryDirectory() as d2:
            f2 = CommandEntryFixture(d2)
            base_sha = f2.live_base
            head_sha = f2.requested_head
            np_records = (
                AcceptedOperationRecord(
                    operation_id="op-np-1",
                    operation_kind="command",
                    target_base_sha=base_sha,
                    target_head_sha=head_sha,
                    lifecycle_phase="completed",
                    state_fingerprint="same-fp",
                    made_progress=False,
                ),
                AcceptedOperationRecord(
                    operation_id="op-np-2",
                    operation_kind="command",
                    target_base_sha=base_sha,
                    target_head_sha=head_sha,
                    lifecycle_phase="completed",
                    state_fingerprint="same-fp",
                    made_progress=False,
                ),
            )
            rc2, msg2 = _invoke_action(
                f2,
                convergence_records=np_records,
                current_state_fingerprint="same-fp",
            )
            self.assertNotEqual(rc2, 0)
            self.assertIn("halted by finite no-progress stop rule", msg2)
            self.assertEqual(f2.business_writes, 0)

        # 3. Advancing state fingerprint resets the stop rule and executes cleanly!
        with tempfile.TemporaryDirectory() as d3:
            f3 = CommandEntryFixture(d3)
            np_records_3 = (
                AcceptedOperationRecord(
                    operation_id="op-np-1",
                    operation_kind="command",
                    target_base_sha=f3.live_base,
                    target_head_sha=f3.requested_head,
                    lifecycle_phase="completed",
                    state_fingerprint="same-fp",
                    made_progress=False,
                ),
                AcceptedOperationRecord(
                    operation_id="op-np-2",
                    operation_kind="command",
                    target_base_sha=f3.live_base,
                    target_head_sha=f3.requested_head,
                    lifecycle_phase="completed",
                    state_fingerprint="same-fp",
                    made_progress=False,
                ),
            )
            rc3, out3 = _invoke_action(
                f3,
                convergence_records=np_records_3,
                current_state_fingerprint="advanced-fp",
            )
            self.assertEqual(rc3, 0, out3)

        # 4. Direct evaluate_action_convergence_progress checks: review_comment source,
        #    held_uncertain_operation refusal, repository_id=None refusal, and
        #    WebhookVerifier.verify_event_route(..., reject_merge_group=True)
        with tempfile.TemporaryDirectory() as d4:
            f4 = CommandEntryFixture(d4)
            self.assertEqual(
                f4.invoke_command("@reviewsensei review status"), 0, f4.error
            )
            assert f4.selected is not None
            rc_dec = action_run.evaluate_action_convergence_progress(
                f4.selected,
                purpose="reply",
                source={
                    "kind": "review_comment",
                    "id": 991,
                    "updated_at": "2026-10-26T00:00:00Z",
                },
                comment_body="@reviewsensei explain this",
            )
            self.assertEqual(rc_dec.reconciliation_owner, "hosted_reply_executor")

            uncertain_rec = AcceptedOperationRecord(
                operation_id="op-uncertain",
                operation_kind="command",
                target_base_sha=f4.live_base,
                target_head_sha=f4.requested_head,
                lifecycle_phase="uncertain_dispatch",
            )
            with self.assertRaisesRegex(
                action_run.ReviewInputError, "uncertain prior dispatch"
            ):
                action_run.evaluate_action_convergence_progress(
                    f4.selected,
                    purpose="command",
                    source=f4._source_scope(),
                    comment_body="@reviewsensei review status",
                    records=(uncertain_rec,),
                )

            from dataclasses import replace as dc_replace

            from review_sensei.session import SessionIdentity

            no_repo_run = dc_replace(
                f4.selected,
                identity=SessionIdentity("owner/repo", 7, repository_id=None),
            )
            with self.assertRaisesRegex(
                action_run.ReviewInputError, "positive integer repository_id"
            ):
                action_run.evaluate_action_convergence_progress(
                    no_repo_run,
                    purpose="command",
                    source=f4._source_scope(),
                    comment_body="@reviewsensei review status",
                )

        from review_sensei_github.errors import GitHubMergeGroupUnsupportedError

        verifier_mg = WebhookVerifier(app_id=77, secret_source=_StaticSecret())
        mg_raw, mg_sig = self._sign({"action": "checks_requested"})
        with self.assertRaises(GitHubMergeGroupUnsupportedError):
            verifier_mg.verify_event_route(
                body=mg_raw,
                signature=mg_sig,
                event="merge_group",
                delivery_id="deliv-mg-reject",
                reject_merge_group=True,
            )


if __name__ == "__main__":
    unittest.main()
