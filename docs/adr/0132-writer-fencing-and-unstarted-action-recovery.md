# ADR 0132: Qualify writer containment, concurrency barriers, and narrow unstarted-action recovery

- Status: Proposed
- Date: 2026-11-11
- Decision owner/reviewers: ReviewSensei maintainers; independent source review
- Linked GitHub Issue: user-directed gap-closure Batch 2 (`G02`, `G08`, `G32`, `G27`)
- Linked PR: pending

## Context

Four coupled lifecycle boundaries govern session-ledger writer safety and comment-triggered action recovery:

1. **Stale ledger write races (`G02`):** GitHub's REST issue-comment `PATCH /repos/{owner}/{repo}/issues/comments/{comment_id}` endpoint applies last-arrival-wins semantics and offers no server-side conditional generation predicate. Pre-write discovery and post-write readback catch races observed immediately, but a delayed network write dispatched before cancellation or carried by an already-issued raw installation token can overwrite a successor root after readback.
2. **Command/event concurrency interleavings (`G08`):** Hosted reviews, local-runner workflows, maintainer commands, and mention replies share one per-PR session ledger and paid request allowance. Cancellation of a workflow run does not equal physical writer cessation, and sibling business jobs (`hosted`, `local`, `command`) within an incomplete run must all cease before any successor claims startup or rollover capacity.
3. **Unstarted actions stranded after source edit/deletion (`G32`):** Admitting an action persists `action.phase == "pending"` with `execution_started == False` before re-reading the live source comment at `begin_action_effect`. If the comment was edited or deleted in that window, effect entry refuses with zero business writes, leaving `action.pending` on the ledger. Previously, ordinary rollover also refused because `action.phase != "terminal"`, stranding subsequent PR reviews and commands without a narrow recovery route.
4. **Source-comment edit/delete lifecycle (`G27`):** Comment edits, restorations of earlier comment prose, deletions, duplicate webhook deliveries, and late stale deliveries have distinct safety meanings depending on whether an action is pre-admission, unstarted pending, started/uncertain, holding a published pending receipt, or terminal.

## Decision

### 1. Credential-contained writer fencing and default-disabled live cutover (`G02`)

Add `review_sensei.writer_containment` alongside `review_sensei.recovery_authority` (`ADR 0128`):

- Keep `LIVE_CANONICAL_CUTOVER_ENABLED = False`. No production runtime, CLI, workflow, or broker cutover is enabled by source qualification alone.
- Model all writer routes explicitly: contained routes (`actions_hosted`, `actions_local`, `actions_command`, `manual_cli`) versus bypass routes (`broker_raw_token`, `pre_cancellation_inflight`).
- `WriterContainmentPosture.require_exclusive_containment()` refuses exclusive native authority whenever any raw write token path or pre-cutover in-flight request remains uncontained, proving structurally that placing an authority coordinator beside an unfenced token-issuing broker cannot fence delayed writes.
- `ContainedForwardingCoordinator` enforces monotonic per-PR epochs, process-issued `IssuedWriterToken` capabilities, one-use dispatch/reservation bindings, linearizable frontier advancement before SQLite commit, database-restore detection, and strict refusal of legacy envelopes without explicit migration or reset.

### 2. Sibling-writer cessation and barrier-qualified concurrency (`G08`)

Tighten `ApiRunActionsEvidence.ceased_owner` in `review_sensei_github.api_run_owner` so predecessor cessation requires completion of every recognized business-writer job (`ReviewSensei / hosted`, `ReviewSensei / local`, `ReviewSensei / command`) on the predecessor attempt, not merely the single job label recorded in `owner.job`. Preserve unknown request liabilities across succession, treat workflow cancellation as non-cessation until physical job completion is observed, and verify deterministic convergence once cessation is established.

### 3. Narrow authenticated unstarted-action cancellation (`G32`)

Add `UNSTARTED_CANCELLATION_REASONS = {"source_edited", "source_deleted"}` to `ApiRunAccounting` and schema `api-run-accounting.schema.json` strictly when `action.phase == "terminal"` and `action.execution_started is False`, plus `review_sensei_github.unstarted_action_recovery`:

- Preserve default fail-closed holds when no witness is supplied (`test_source_edit_and_owner_takeover_preserve_pending_intent_without_effect` remains unchanged).
- Require a process-issued `UnstartedCancellationWitness` (`weakref.WeakSet`-tracked, non-serializable) produced by `authenticate_unstarted_cancellation(...)`, binding:
  1. Exact `SessionIdentity`, `target_run_id`, `target_root_sha256`, and canonical `intent_sha256` on a record with `action.phase == "pending"`, `execution_started is False`, `result is None`, `outcome is None`, `uncertain == 0`, and no active review reservation or unfinished review transaction.
  2. Metered live GitHub source observation proving HTTP `404` (`source_deleted`) or HTTP `200` with changed `(updated_at, body_sha256)` (`source_edited`). Unchanged sources refuse so ordinary same-source succession executes normally.
  3. Positive writer exclusion/cessation via `ApiRunActionsEvidence` (`same_owner_active`, `same_run_ceased_predecessor`, `cross_run_ceased_predecessor`) or an active `ContainedForwardingCoordinator` epoch (`canonical_epoch_fenced`) with zero unsettled dispatches.
- Support both in-run settlement (`ApiRunRequestTransport.cancel_unstarted_action`) and cross-run settlement + `caller-run-root-v1` archival (`ApiRunRequestTransport.rollover_unstarted_and_activate`), preserving all prior request charges, reservations, review transactions, dispositions, and ancestor action results.
- Never reuse `controlled_restart` whole-history abandonment and never populate `controlled_restarts.json`.

### 4. Five-phase source-comment mutation classification (`G27`)

Add `review_sensei_github.source_comment_lifecycle` to classify live comment state (`unchanged`, `source_edited`, `edit_restored_body`, `source_deleted`) and delivery freshness (`current_revision`, `duplicate_delivery`, `late_stale_delivery`, `mutated_revision`, `deleted_source`) across all five phases:

1. `before_admission`: admit only when live source matches the exact `(kind, id, updated_at, body_sha256)` tuple; refuse mutated, restored-body, deleted, or stale deliveries before any ledger write.
2. `unstarted_intent`: execute same-source succession when unchanged; route `source_edited`, `edit_restored_body`, and `source_deleted` to `G32` only when authenticated by an `UnstartedCancellationWitness` (holding fail-closed otherwise).
3. `started_work`: refuse `G32` cancellation; hold fail-closed with prior charges and unknown/accepted effects preserved.
4. `pending_receipt`: refuse silent settlement and refuse `G32` cancellation; hold durable pending receipts and original source attribution intact.
5. `terminal_completed`: authenticate the retained `caller-action-result-v1` manifest and preserve terminal receipts, dispositions, and approvals with zero replay.

Editing a comment and later restoring its exact original body bytes advances `updated_at` and classifies as `edit_restored_body` (`mutated_revision`), never as an unchanged replay of the original revision.

## Validation

- `tests/test_stale_ledger_fencing_g02.py`: deterministic stale-`PATCH` race proving advisory readback fails under delayed network writes while canonical epoch fencing rejects the stale `PATCH`, unfenced-broker negative control, frontier/SQLite crash ordering, database restore detection, process kill after wire effect, unknown usage preservation, duplicate idempotency, readback tail exhaustion, and legacy exclusion.
- `tests/test_command_event_concurrency_g08.py`: deterministic barriers across review vs command, reply vs command, local vs hosted, cancelled worker with delayed write vs successor, non-FIFO queued replacement without lost obligations, and two manual commands.
- `tests/test_unstarted_action_recovery_g32.py`: default fail-closed hold, same-source succession, in-run and cross-run `canceled_unstarted` recovery for edited and deleted sources, full end-to-end subsequent command + PR review recovery, and fail-closed counterexamples (`execution_started=True`, accepted result, uncertain dispatch, altered intent, foreign owner, forged witness, stale canonical epoch).
- `tests/test_source_comment_lifecycle_g27.py`: all five phases across edited text, deleted source, duplicate/late delivery, owner succession, and `edit_restored_body` restoration.

## Rollout and rollback

- `LIVE_CANONICAL_CUTOVER_ENABLED` remains `False`; live runtime authority cutover continues to require the Section 8.3 service-host, credential-containment, frontier, and canary gates.
- `controlled_restarts.json` remains empty (`[]`), and no historical ledger or open PR state is mutated by these source modules.
- Reverting the additive schema enum `"canceled_unstarted"` and companion recovery modules restores unconditional pending-action holds without affecting legacy or completed session records.
