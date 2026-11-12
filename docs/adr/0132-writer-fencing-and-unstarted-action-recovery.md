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
- Codify the repository-wide writer and credential route inventory in `WRITER_CREDENTIAL_ROUTE_INVENTORY` (`actions_hosted`, `actions_local`, `actions_command`, `actions_reply`, `manual_cli_local_ledger`, `manual_cli_direct_github`, `broker_raw_token`, `pre_cancellation_inflight`) mapped onto contained route keys (`actions_hosted`, `actions_local`, `actions_command`, `manual_cli`) versus bypass route keys (`broker_raw_token`, `pre_cancellation_inflight`).
- Prove via `evaluate_side_coordinator_fencing_proof(...)` and `WriterContainmentPosture.require_exclusive_containment()` why placing a sidecar authority coordinator beside an unfenced token-issuing broker or raw `GitHubAppAuth` installation token cannot fence delayed direct `PATCH`/`POST` writes to `api.github.com`.
- `ContainedForwardingCoordinator` enforces monotonic per-PR epochs, process-issued `IssuedWriterToken` capabilities, one-use dispatch/reservation bindings, linearizable `frontier.sqlite` advancement before `authority.sqlite` commit, external epoch-floor regression detection (`verify_external_epoch_floor`), physical request conservation (`evaluate_physical_budget_guarantees`), and strict refusal of legacy envelopes (`LEGACY_ACCOUNTING_VERSION`) without explicit migration or reset.

### 2. Sibling-writer cessation and barrier-qualified concurrency (`G08`)

Tighten `ApiRunActionsEvidence.ceased_owner` and expose `inspect_concurrency_posture` in `review_sensei_github.api_run_owner` alongside `ApiRunRequestTransport.containment_and_budget_snapshot()` in `review_sensei_github.api_run_requests` so:

- Predecessor cessation requires completion of every recognized business-writer job (`ReviewSensei / hosted`, `ReviewSensei / local`, `ReviewSensei / command`) on the predecessor attempt under `.github/workflows/review-sensei-run.yml`'s `cancel-in-progress: false` PR provider concurrency group, regardless of non-FIFO numeric `run_id` ordering.
- Duplicate same-slot claims (`claim_review_startup`), conflicting or already-started `admit_action` calls, active review reservations/transactions, unrecognized historical executor workflow SHAs (`qualified_publication_cessation`), live `head_sha` drift (`ActionRunLedger.business_guard`), and unsupported `caller-requests-v1` legacy accounting overlap all refuse fail-closed.
- Uncertain HTTP outcomes (`PendingApiRequest`, `uncertain > 0`) permanently debit the finite caller request allowance and block terminal close and cross-run rollover until reconciled by authenticated readback.

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

## Scope

- **Applies to:** `src/review_sensei/writer_containment.py`, `packages/python/review-sensei-github/src/review_sensei_github/api_run_owner.py`, `packages/python/review-sensei-github/src/review_sensei_github/api_run_requests.py`, `packages/python/review-sensei-github/src/review_sensei_github/unstarted_action_recovery.py`, `packages/python/review-sensei-github/src/review_sensei_github/source_comment_lifecycle.py`, and their qualification suites.
- **Out of scope / non-goals:** Enabling live canonical-authority cutover (`LIVE_CANONICAL_CUTOVER_ENABLED` stays `False`), deploying an external hosted coordinator service, modifying `.github/workflows/review-sensei-run.yml` or broker Worker deployment, or relaxing request allowances.

## Consequences

- **Positive:** Every repository writer and credential route is inventoried and verified against real modules; supported `ApiRunOwner` and `CallerRunRequests` routes enforce sibling-job cessation, non-FIFO safety, duplicate-admission refusal, unknown-commit refusal, permanent uncertain-request accounting, and legacy-overlap refusal; narrow unstarted-action cancellation (`G32`) unblocks edited/deleted pre-execution comments without losing prior accounting.
- **Costs / residual boundaries:** While `LIVE_CANONICAL_CUTOVER_ENABLED = False` and the hosted broker returns bearer installation tokens to Actions jobs, pre-cutover writer exclusion relies on GitHub Actions `cancel-in-progress: false` job serialization, `ApiRunActionsEvidence` job-completion reads, and optimistic `record_sha256` checks rather than server-mediated capability forwarding.

## Alternatives considered

1. **Place a sidecar epoch coordinator beside raw GitHub installation tokens:** Rejected because `api.github.com` does not check sidecar epochs; any worker retaining a raw bearer installation token can still overwrite a successor comment on delayed network arrival.
2. **Enable `LIVE_CANONICAL_CUTOVER_ENABLED = True` in repository source without a deployed forwarding host:** Rejected because live cutover requires service-owned credential containment and linearizable frontier hosting.
3. **Reuse `controlled_restart` for unstarted edited/deleted comment actions:** Rejected because `controlled_restart` abandons whole-history lineage, whereas an unstarted pending action needs only narrow intent cancellation preserving existing ledger state.

## Validation

- `tests/test_stale_ledger_fencing_g02.py`: complete writer/credential route inventory verification, side-coordinator raw-token impossibility proof, external epoch-floor rollback guard, real `ApiRunRequestTransport`/`ApiRunSessionLedger`/`ApiRunActionsEvidence` delayed-writer fencing, uncertain-write permanent debit, `caller-requests-v1` legacy overlap refusal, unknown historical executor SHA refusal, plus contained coordinator crash/restore/fencing matrix.
- `tests/test_command_event_concurrency_g08.py`: deterministic barriers across review vs command, reply vs command, local vs hosted, cancelled worker with delayed write vs successor, non-FIFO delivery (`run_id` out-of-order completion), `cancel-in-progress: false` waiting-sibling refusal, duplicate startup-slot and `admit_action` refusal, live head-drift guard fencing, and two manual commands.
- `tests/test_unstarted_action_recovery_g32.py`: default fail-closed hold, same-source succession, in-run and cross-run `canceled_unstarted` recovery for edited and deleted sources, full end-to-end subsequent command + PR review recovery, and fail-closed counterexamples (`execution_started=True`, accepted result, uncertain dispatch, altered intent, foreign owner, forged witness, stale canonical epoch).
- `tests/test_source_comment_lifecycle_g27.py`: all five phases across edited text, deleted source, duplicate/late delivery, owner succession, and `edit_restored_body` restoration.

## Follow-up work

- Keep `G02` and `G08` live cutover gated (`LIVE_CANONICAL_CUTOVER_ENABLED = False`) until an operator-owned external forwarding service disables raw bearer write tokens on broker/Actions/manual CLI routes and passes canary verification.

## Rollout and rollback

- `LIVE_CANONICAL_CUTOVER_ENABLED` remains `False`; live runtime authority cutover continues to require the Section 8.3 service-host, credential-containment, frontier, and canary gates.
- `controlled_restarts.json` remains empty (`[]`), and no historical ledger or open PR state is mutated by these source modules.
- Reverting the additive schema enum `"canceled_unstarted"` and companion recovery modules restores unconditional pending-action holds without affecting legacy or completed session records.

### Independent-host cutover preflight invalidation

Cutover now revalidates the predecessor's exact frontier snapshot inside the same
`frontier.sqlite` `BEGIN IMMEDIATE` transaction that advances the active epoch.
A second host's durable `forwarded` intent therefore invalidates an earlier clean
preflight, even while its physical wire request remains delayed. If cutover wins
first, the predecessor's active-epoch authorization refuses forwarding instead.
The failed cutover preserves the predecessor epoch, dispatch intent, and original
held request liability; it does not refund, resend, or infer cessation.

The G02 regression uses a separate Python process and coordinator sharing the
local stores, with the wire held open across the cutover attempt. This qualifies
that interleaving, not distributed storage, simultaneous successor enrollment,
credential containment, or an authenticated external epoch witness. No new schema
or live host is introduced. `verify_external_epoch_floor` remains a diagnostic
comparison against a supplied integer, not an authenticated witness producer.
SQLite locking and durable nonrollback frontier hosting remain deployment gates;
unsupported or conflicting storage must refuse rather than fall back to an
in-process lock. `LIVE_CANONICAL_CUTOVER_ENABLED` remains `False`.
