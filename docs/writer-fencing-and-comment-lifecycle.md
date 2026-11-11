# Writer Fencing, Concurrency Interleavings, and Source-Comment Lifecycle

This document specifies the qualified source contracts for session-ledger writer containment (`G02`), command/event concurrency barriers (`G08`), narrow unstarted-action recovery (`G32`), and five-phase source-comment mutation handling (`G27`). See [ADR 0128](adr/0128-unconnected-recovery-authority-candidate.md) and [ADR 0132](adr/0132-writer-fencing-and-unstarted-action-recovery.md) for architectural rationale.

## 1. Writer Containment and Stale-PATCH Fencing (`G02`)

GitHub issue-comment `PATCH` requests have no native conditional `If-Match` or generation check. Consequently:

- **Advisory generation/readback boundary:** `GitHubIssueCommentSessionLedger` performs pre-write discovery, live-head verification, `PATCH`, and immediate readback. This catches concurrent updates that land before readback, but cannot revoke an HTTP `PATCH` already in flight from a delayed old worker holding a valid installation token.
- **Credential-contained epoch fencing (`review_sensei.writer_containment`):**
  - Every writer route is classified in `ALL_WRITER_ROUTES`:
    - Contained routes: `actions_hosted`, `actions_local`, `actions_command`, `manual_cli`.
    - Bypass routes: `broker_raw_token`, `pre_cancellation_inflight`.
  - `WriterContainmentPosture.require_exclusive_containment()` refuses `native_exclusive=True` unless raw write tokens are disabled across broker, Actions, and manual CLI paths, all mutations traverse the authority forwarder, and pre-cutover in-flight writes have drained.
  - `ContainedForwardingCoordinator` issues process-bound `IssuedWriterToken` capabilities per `(base_store_id, repository_id, pull_request, epoch)`. Advancing to `epoch + 1` requires `predecessor_ceased=True` and causes the forwarder to reject any delayed `PATCH` presenting `epoch` before wire transmission.
  - `LIVE_CANONICAL_CUTOVER_ENABLED` is `False` by default; live cutover remains gated on hosted frontier deployment and full credential containment.

## 2. Command/Event Concurrency and Predecessor Cessation (`G08`)

Across concurrent review runs, mention replies, local workflows, and maintainer commands on the same PR:

1. **Single active business writer:** At most one owner `(run_attempt, job, generation)` holds active caller reservation blocks on a `SessionRecord` at a time.
2. **Cancellation is not cessation:** A `cancelled` workflow or timed-out client with an in-flight `PATCH` remains an active writer hazard until job completion is observed via `ApiRunActionsEvidence` (checking all `BUSINESS_WRITER_JOBS`: `hosted`, `local`, `command`) or fenced by canonical epoch advancement.
3. **Preserved unknown liabilities:** Lost mutation responses remain recorded as `PendingApiRequest` entries (`uncertain > 0`), permanently charged against the run's finite request allowance and carried into successor owners until reconciled by authenticated readback.
4. **Non-FIFO replacement convergence:** When queued runs are superseded before admission, retained findings, dispositions, and open review obligations on the session comment remain intact for the surviving successor run.

## 3. Narrow Unstarted-Action Recovery (`G32`)

When an action run persists `action.phase == "pending"` (`execution_started == False`) during `admit_action` and then refuses before `begin_action_effect` because the triggering comment was edited or deleted:

- **Default behavior (no witness):** Both same-run re-admission with changed source bytes and ordinary `rollover_and_activate` refuse fail-closed.
- **Authenticated recovery (`review_sensei_github.unstarted_action_recovery`):**
  - `authenticate_unstarted_cancellation(...)` verifies:
    - `action.phase == "pending"`, `execution_started is False`, `result is None`, `outcome is None`, `uncertain == 0`, no active review reservation, and no unfinished review transaction;
    - positive writer cessation/exclusion (`ApiRunActionsEvidence` or `ContainedForwardingCoordinator`); and
    - metered live GitHub comment observation returning HTTP `404` (`source_deleted`) or HTTP `200` with mutated `(updated_at, body_sha256)` (`source_edited`).
  - **In-run settlement:** `ApiRunRequestTransport.cancel_unstarted_action(ledger, identity, witness)` persists `action.phase = "terminal"`, `execution_started = False`, and `outcome = {"kind": "canceled_unstarted", "reason": "source_edited" | "source_deleted"}` with an authenticated inline `caller-action-result-v1` manifest.
  - **Cross-run rollover (after attempt exhaustion):** `ApiRunRequestTransport.rollover_unstarted_and_activate(...)` settles the stranded root on the native ledger, archives that exact committed root into an immutable `caller-run-root-v1` comment, and activates the new caller run so subsequent commands and PR reviews proceed normally.
  - **Separation from `controlled_restart`:** Neither path touches `controlled_restart` or `controlled_restarts.json`, and all prior request charges, review transactions, and dispositions remain preserved.

## 4. Source-Comment Edit/Delete Lifecycle Matrix (`G27`)

`review_sensei_github.source_comment_lifecycle.classify_source_comment_lifecycle(...)` evaluates live comment status and webhook delivery tuples across five phases:

| Lifecycle Phase | Durable Record State | Unchanged Source (`current_revision`) | Edited Text / Restored Body (`edit_restored_body`) | Deleted Source (`404`) | Duplicate / Late Stale Delivery |
|---|---|---|---|---|---|
| `before_admission` | `record is None` or `action is None` | `admit_current_revision` | `refuse_before_admission` (zero ledger writes) | `refuse_before_admission` (zero ledger writes) | `refuse_before_admission` |
| `unstarted_intent` | `action.pending`, `execution_started=False`, `uncertain=0` | `execute_same_source` | `g32_unstarted_cancellation` (with witness; holds without) | `g32_unstarted_cancellation` (with witness; holds without) | `execute_same_source` (exact duplicate) / `refuse_stale_delivery` (stale) |
| `started_work` | `action.pending`, `execution_started=True` or `uncertain>0` | `hold_started_work` | `hold_started_work` (`G32` refused; charges kept) | `hold_started_work` (`G32` refused; charges kept) | `hold_started_work` |
| `pending_receipt` | `action.pending` + durable pending receipt | `hold_pending_receipt` | `hold_pending_receipt` (no silent settlement) | `hold_pending_receipt` (no silent settlement) | `hold_pending_receipt` |
| `terminal_completed` | `action.terminal` + verified result manifest | `preserve_terminal_no_replay` | `preserve_terminal_no_replay` (immutable receipt) | `preserve_terminal_no_replay` (immutable receipt) | `preserve_terminal_no_replay` (zero replay) |

### Edit-Then-Restore Non-Replay Rule

Every comment revision binds `(kind, id, updated_at, body_sha256)`. If a user edits a comment at `T2` and later restores the exact original body text at `T3` (`T3 != T1`), `observe_live_source_revision` classifies the comment as `mutation_state = "edit_restored_body"` (`delivery_disposition = "mutated_revision"`). It is never treated as `unchanged` and never replays a prior action bound to `T1`; in `unstarted_intent`, it may be cleanly superseded via `G32` (`reason = "source_edited"`), after which the new `T3` revision can be admitted only as its own distinct event.
