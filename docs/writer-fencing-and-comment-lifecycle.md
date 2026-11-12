# Writer Fencing, Concurrency Interleavings, and Source-Comment Lifecycle

This document specifies the qualified source contracts for session-ledger writer containment (`G02`), command/event concurrency barriers (`G08`), narrow unstarted-action recovery (`G32`), and five-phase source-comment mutation handling (`G27`). See [ADR 0128](adr/0128-unconnected-recovery-authority-candidate.md) and [ADR 0132](adr/0132-writer-fencing-and-unstarted-action-recovery.md) for architectural rationale.

## 1. Writer Containment and Stale-PATCH Fencing (`G02`)

GitHub issue-comment `PATCH` requests have no native conditional `If-Match` or generation check. Consequently:

- **Advisory generation/readback boundary:** `GitHubIssueCommentSessionLedger` performs pre-write discovery, live-head verification, `PATCH`, and immediate readback. This catches concurrent updates that land before readback, but cannot revoke an HTTP `PATCH` already in flight from a delayed old worker holding a valid installation token.
- **Credential-contained epoch fencing (`review_sensei.writer_containment`):**
  - Every writer and credential route across hosted Actions, local self-hosted runners, manual CLI, and token adapters is codified in `WRITER_CREDENTIAL_ROUTE_INVENTORY` and classified in `ALL_WRITER_ROUTES`:
    - Contained route keys (`CONTAINED_WRITER_ROUTES`): `actions_hosted` (including `review-run` and `reply-run` on `runner_kind == "hosted"`), `actions_local` (`review-run` and `reply-run` on `runner_kind == "local"`), `actions_command` (`command-run`), and `manual_cli` (`manual_cli_local_ledger` for local-filesystem-only CLI and `manual_cli_direct_github` for `--allow-write` OIDC publication).
    - Bypass route keys (`BYPASS_WRITER_ROUTES`): `broker_raw_token` (`BrokerClient` raw capability bearer tokens and `GitHubAppAuth` / `GitHubTokenTransport` RS256 installation tokens) and `pre_cancellation_inflight` (socket mutations already transmitted before SIGTERM or workflow cancellation).
  - **Why a side coordinator cannot fence an unrestricted old GitHub token:** `evaluate_side_coordinator_fencing_proof(...)` proves structurally that GitHub REST and GraphQL endpoints authenticate only the bearer `InstallationToken` (`Authorization: Bearer ...`) against repository installation permissions, never a ReviewSensei sidecar SQLite database or session-comment generation. Advancing an epoch in a sidecar coordinator while workers still hold raw GitHub bearer tokens leaves delayed `PATCH` and `POST` writes unfenced (`can_fence_delayed_writer=False`, `residual_clobber_possible=True`). `WriterContainmentPosture.require_exclusive_containment()` therefore refuses `native_exclusive=True` unless raw write tokens are disabled across broker, Actions, and manual CLI paths, all mutations traverse `ContainedForwardingCoordinator`, and pre-cutover in-flight writes have drained.
  - **Durable nonrollback frontier:** `ContainedForwardingCoordinator` persists active per-PR epochs and checkpoints to `frontier.sqlite` before committing `authority.sqlite`, detects database-restore regressions, and exposes `verify_external_epoch_floor(...)` so externally witnessed GitHub epochs/generations reject simultaneous rollback of local SQLite files.
  - **Physical caller-budget guarantees:** `evaluate_physical_budget_guarantees(...)` and `ApiRunRequestTransport.containment_and_budget_snapshot()` enforce exact conservation across finite `configured_limit <= 4,096` (`startup_liability + prior_nonstartup_liability + confirmed + uncertain + reserved_undispatched + remaining == configured_limit`), protect at least `WRITE_CONFIRMATION_TAIL = 3` requests (`1` write + `2` confirmation reads), permanently debit timed-out/5xx/429 uncertain mutations (`PendingApiRequest`), refuse HTTP redirects, and refuse `LEGACY_ACCOUNTING_VERSION` (`caller-requests-v1`) overlap on rollover.
  - **Live cutover remains disabled (`LIVE_CANONICAL_CUTOVER_ENABLED = False`):** Repository source keeps `live_cutover_enabled = False`; pre-cutover hosted execution relies on workflow concurrency serialization (`cancel-in-progress: false` on `reviewsensei-provider-review-{repo}-{pr}`), `ApiRunActionsEvidence` physical job-completion checks, and optimistic `record_sha256` readback, with raw-token fencing gated on external service-host cutover.

## 2. Command/Event Concurrency and Predecessor Cessation (`G08`)

Across concurrent review runs, mention replies, local workflows, and maintainer commands on the same PR:

1. **Single active business writer (`cancel-in-progress: false`):** Provider jobs (`hosted`, `local`, `command`) in `.github/workflows/review-sensei-run.yml` join `reviewsensei-provider-review-{repository}-{pull_request}` with `cancel-in-progress: false`, while `reply` runs use unique `github.run_id` workflow groups so replies never cancel sibling replies. At most one owner `(run_attempt, job, generation)` holds active caller reservation blocks on a `SessionRecord` at a time, and `claim_review_startup` atomically blocks duplicate same-slot process restarts.
2. **Cancellation is not cessation (`ApiRunActionsEvidence`):** A `cancelled` workflow or timed-out client with an in-flight `PATCH` remains an active writer hazard until physical job completion is observed across all `BUSINESS_WRITER_JOBS` (`hosted`, `local`, `command`) via `ApiRunActionsEvidence.ceased_owner` / `ceased_run_owner` / `inspect_concurrency_posture`, or fenced by canonical epoch advancement. Historical root-write non-entry (`qualified_publication_cessation`) additionally requires an exact recognized workflow executor commit (`QUALIFIED_EXECUTORS`) and exact `head_sha`, refusing unknown commits fail-closed.
3. **Duplicate admission and live head fencing (`CallerRunRequests`):** `ApiRunRequestTransport.admit_action` returns idempotently on unstarted same-owner retry, refuses conflicting `(purpose, source, head_sha, configuration_digest, operation_key)` tuples, refuses duplicate admission once `begin_action_effect` has entered execution, refuses admission when a review reservation or unfinished review transaction exists, and pairs with `ActionRunLedger.business_guard` (`VerifiedRunAction.fence`) to block mutations on live head or source drift.
4. **Preserved unknown liabilities:** Lost or HTTP `502`/`504`/`429` mutation responses remain recorded as `PendingApiRequest` entries (`uncertain > 0`), permanently charged against the run's finite request allowance, blocking `finish_review_run`, `settle_action_effect`, `close_analysis_abort`, and cross-run rollover until reconciled by authenticated readback.
5. **Non-FIFO delivery and replacement convergence:** When out-of-order Actions runs execute (for example run `250` completing before delayed run `249` starts) or queued runs are superseded before admission, `ApiRunActionsEvidence` authenticates completion regardless of numeric `run_id` order while `VerifiedRunAction` and `admit_action` enforce live `head_sha`/source freshness, keeping retained findings, dispositions, and open review obligations intact.

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
