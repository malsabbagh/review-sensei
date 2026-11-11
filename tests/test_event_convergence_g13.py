"""G13: Missed, delayed, out-of-order, and queued-replaced event convergence tests."""

from __future__ import annotations

import hashlib
import unittest
from typing import Any

from review_sensei_github.event_routing_contract import (
    AcceptedOperationRecord,
    LivePullRequestTarget,
    QueuedRunCandidate,
    reconcile_event_convergence,
    simulate_actions_concurrency_queue,
)

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


if __name__ == "__main__":
    unittest.main()
