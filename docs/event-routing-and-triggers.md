# GitHub event routing, convergence, and recovery boundaries

This guide documents ReviewSensei's caller workflow subscriptions, runtime
admission guards, current-state convergence rules, merge-queue unsupported
boundary, manual thread reconciliation, and human pull-request review
independence (`G01`, `G07`, `G13`, `G26`, `G28`; see
[ADR 0136](adr/0136-event-routing-convergence-and-queue-boundaries.md)).

Source changes alone do not update an installed caller workflow or deployed tag.
Routing-only shell preflight selects whether to invoke the reusable workflow; it
never authenticates live pull-request title, head SHA, or draft state (`G22`),
which are validated from live GitHub API state inside the authenticated runtime.

## Caller subscriptions versus runtime admission (`G01`)

ReviewSensei intentionally subscribes caller workflows only to:

- `pull_request`: `opened`, `reopened`, `synchronize`, `ready_for_review`
- `issue_comment`: `created` (non-Bot `OWNER`, `MEMBER`, or `COLLABORATOR` on PR threads)
- `pull_request_review_comment`: `created` (non-Bot `OWNER`, `MEMBER`, or `COLLABORATOR`)
- `workflow_dispatch`: manual operator dispatch (`pull_request_number`, optional `review_remaining`)

Caller workflows retain read-only repository permissions (`contents: read`,
`pull-requests: read`, `issues: read`, `id-token: write`) and run zero
background polling loops. `map_installed_caller_subscription` binds each
approved trigger in `.github/workflows/review-sensei-review.yml` through
`hosting/github/trigger.py` and `WebhookVerifier.verify_event_route` to its
exact reusable-workflow runtime entry (`review_run.run_review`,
`action_run.run_action(purpose='command')`, or
`action_run.run_action(purpose='reply')`). Every non-subscribed GitHub
transition is governed by a read-time guard on the next admitted operation and
a bounded operator recovery command.

### Complete event and recovery command decision table

| Rule ID | Event / action | Subtype | Caller subscribed | Runtime disposition | Model inference | Read-time guard | Operator recovery command |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `pull_request.opened` | `pull_request`: `opened` | `initial_open` | Yes | `review` | Yes | `live_pr_open_and_exact_boolean_draft_false` | `workflow_dispatch` |
| `pull_request.reopened` | `pull_request`: `reopened` | `reopen` | Yes | `review_or_reuse_retained` | Yes (0 on exact reuse) | `live_pr_open_exact_boolean_draft_false_and_snapshot_reuse_check` | `workflow_dispatch` |
| `pull_request.synchronize` | `pull_request`: `synchronize` | `head_advance_or_push` | Yes | `review_or_reuse_retained` | Yes (0 on exact reuse) | `live_pr_open_exact_boolean_draft_false_and_snapshot_binding` | `workflow_dispatch` |
| `pull_request.ready_for_review` | `pull_request`: `ready_for_review` | `draft_exit` | Yes | `review_or_reuse_retained` | Yes (0 on exact reuse) | `live_pr_open_exact_boolean_draft_false_and_snapshot_reuse_check` | `workflow_dispatch` |
| `pull_request.edited.metadata` | `pull_request`: `edited` | `title_or_body` | No | `explicit_noop` | No | `shared_bootstrap_title_byte_limit_on_next_admitted_run` | `@reviewsensei verify` |
| `pull_request.edited.base` | `pull_request`: `edited` | `base_retarget` | No | `read_time_invalidation` | No | `exact_base_sha_and_base_ref_snapshot_binding` | `workflow_dispatch` |
| `pull_request.converted_to_draft` | `pull_request`: `converted_to_draft` | `draft_enter` | No | `explicit_noop` | No | `exact_boolean_draft_gate_blocks_new_publication` | `workflow_dispatch` |
| `pull_request.closed` | `pull_request`: `closed` | `close_or_merge` | No | `explicit_noop` | No | `live_pr_state_open_gate_blocks_new_publication` | `@reviewsensei review status` |
| `branch.recreation` | `create` / `delete` | `branch_ref_delete_or_recreate` | No | `read_time_invalidation` | No | `exact_pr_number_and_head_sha_binding_rejects_ref_aliasing` | `workflow_dispatch` |
| `pull_request_review_thread.manual` | `pull_request_review_thread`: `resolved`, `unresolved` | `native_thread_toggle` | No | `no_inference_manual_reconcile_only` | No | `live_graphql_thread_scan_separated_from_durable_finding_inventory` | `@reviewsensei verify` |
| `comment.created` | `issue_comment` / `pull_request_review_comment`: `created` | `comment_created` | Yes | `command_or_review_or_reply` | Command: No; Review/Reply: Yes | `live_source_comment_digest_non_bot_maintainer_and_pr_head_binding` | `@reviewsensei verify` |
| `comment.edited_or_deleted` | `issue_comment` / `pull_request_review_comment`: `edited`, `deleted` | `comment_mutation` | No | `read_time_invalidation` | No | `immutable_source_comment_id_updated_at_and_body_sha256_fence` | `@reviewsensei verify` |
| `pull_request_review.human` | `pull_request_review`: `submitted`, `edited`, `dismissed` | `native_human_review` | No | `explicit_noop` | No | `app_slug_author_isolation_and_independent_ledger_accounting` | `@reviewsensei verify` |
| `merge_group.queue` | `merge_group`: `checks_requested`, `destroyed` | `merge_queue` | No | `unsupported` | No | `merge_group_unsupported_and_exact_pr_base_head_sha_certification` | None (`merge_group` unsupported) |
| `workflow_dispatch.manual` | `workflow_dispatch`: `manual` | `operator_dispatch` | Yes | `manual_review_or_reuse_retained` | Yes (0 on exact reuse) | `live_pr_fetch_policy_freeze_and_retained_snapshot_reuse` | `workflow_dispatch` |

### Command precedence and source identity

When `comment.created` mentions `@reviewsensei` (or `@sensei`), admission
resolves deterministic precedence tiers while preserving immutable source
identity (`(source_kind, source_comment_id, source_updated_at,
source_body_sha256)`):

1. `rescan_or_remaining_review` (`@reviewsensei rescan`, `@reviewsensei review remaining`) — bounded review analysis window on the live PR snapshot.
2. `maintainer_command` (`@reviewsensei verify`, `@reviewsensei review status`, `@reviewsensei override`, `@reviewsensei accept-risk`, `@reviewsensei review continue`, etc.) — deterministic command execution with zero model inference (note that commands such as `@reviewsensei verify` still perform bounded native GitHub API/GraphQL effects under authenticated write authority).
3. `mention_reply` — bounded conversational reply bound to prepared base/head context (`G33`).

Ordinary comment and action callers (`webhooks.py`, `comment_intent.py`,
`action_run.py`) enforce source-comment edit/delete lifecycle fences (`G27`)
via `admit_event_comment_source`, `verify_live_comment_source`, and
`reconcile_action_source_lifecycle`. Unstarted actions stranded after source
mutation (`G32`) hold fail-closed by default and settle only when both
positive `NoBusinessDispatchEvidence` and qualified cessation or canonical
epoch fencing are verified (`UnstartedCallerRecoveryGate`).

## Missed, delayed, out-of-order, and queued-replaced convergence (`G13`)

GitHub webhooks do not provide durable FIFO ordering, and the per-PR Actions
concurrency group (`reviewsensei-provider-review-<owner>/<repo>-<pr>`,
`cancel-in-progress: false`) holds at most one active run and one pending run.

The bounded G13 reconciliation model evaluates authenticated current PR state
(`LivePullRequestTarget`) rather than arrival order. The outcomes below are
qualified for that model with supplied history, not as an end-to-end ordinary
runner guarantee. Ordinary review/command/reply runners do not yet durably produce
the accepted-operation and semantic-progress history or consume the model's
finite-stop verdict. G13 durable history and finite-stop integration remain open;
read-time source/session fences and original accounting still apply.

Model outcomes:

- **Duplicate delivery:** Re-delivering an already completed comment source key
  or an unchanged `(base_sha, head_sha)` review snapshot reuses the retained
  snapshot (`duplicate_delivery_reused` / `unchanged_snapshot_reused`) with zero
  model inference.
- **Retained result with pending publication:** Reconciles native publication
  from the retained result (`retained_result_reused_without_inference`) without
  re-running inference.
- **Out-of-order / delayed `synchronize`:** If `synchronize(H2)` has already
  advanced the live PR head to `H2`, a delayed `synchronize(H1)` payload is
  ignored (`delayed_out_of_order_head_superseded`) with zero inference and no
  rollback of `H2` state or open obligations.
- **Missed webhook or unsubscribed `edited` base retarget:** Dispatching
  `workflow_dispatch` (or `@reviewsensei rescan` / `@reviewsensei review remaining`)
  observes the live `(base_sha, head_sha)` target, invalidates incompatible
  base-SHA baselines while distinguishing ref-only base retargets, preserves
  prior open finding obligations, and schedules bounded review for the current
  snapshot.
- **Uncertain in-flight dispatch:** Holds new scheduling
  (`uncertain_dispatch_reconciliation_required`) and directs the operator to
  `@reviewsensei review status` rather than risking duplicate paid work.
- **Queued-replaced runs in Actions concurrency:** When a third arrival evicts
  the pending slot in `reviewsensei-provider-review-<repo>-<pr>`, replacement is
  classified explicitly (`queued_command_replaced`, `queued_reply_replaced`, or
  `queued_review_superseded`) with the exact recovery command (for example,
  re-issuing the displaced `@reviewsensei verify` command or `workflow_dispatch`).
- **Model ownership and finite no-progress stop rule:** Every
  reconciliation verdict assigns an explicit owner from `RECONCILIATION_OWNERS`
  (`caller_workflow_preflight`, `hosted_review_admission`,
  `hosted_command_executor`, `hosted_reply_executor`, or
  `operator_manual_recovery`). Repeated reconciliation attempts on an identical
  `(base_sha, head_sha, state_fingerprint, open_finding_ids)` tuple that make
  no progress halt deterministically at `MAX_NO_PROGRESS_RECONCILIATION_ATTEMPTS`
  (`2`) with `finite_no_progress_stop` (`stopped_no_progress`) in the model,
  without background reconcilers or paid retries; the model resets the count
  when PR state or progress advances. This is not a durable ordinary-runner
  stop guarantee.

## Merge queue (`merge_group`) unsupported boundary (`G07`)

GitHub Merge Queue (`merge_group` events `checks_requested` and `destroyed`, and
temporary `gh-readonly-queue/*` branches) is **explicitly unsupported**:

- Caller workflows omit `merge_group`.
- `WebhookVerifier.verify` (and `action_run.event_source` /
  `WebhookVerifier.verify_event_route(..., reject_merge_group=True)`) rejects
  `merge_group` immediately with `GitHubMergeGroupUnsupportedError`
  (`merge_group_unsupported`) before OIDC/token acquisition, provider calls,
  ledger writes, or check publications, and `hosting/github/trigger.py` exits
  nonzero on `merge_group`.
- `verify_snapshot_certification` binds every `ReviewCertificationReceipt`
  strictly to one pull request's exact `(repository_id, pull_request_number,
  reviewed_base_sha, reviewed_head_sha)`. An ordinary PR-head approval or
  passing `ReviewSensei` check run never certifies a synthetic merge-queue
  commit SHA, a `gh-readonly-queue/*` ref, an advanced queue base commit, or a
  multi-PR merge group.
- Do not configure `ReviewSensei` as a required check on `merge_group` rulesets
  until dedicated merge-queue support is separately specified and enabled. If a
  pull request is evicted from a merge queue (`merge_group: destroyed`), its
  underlying PR-head certification and open finding obligations remain intact on
  unchanged `(base_sha, head_sha)`.

## Manual thread resolve and reopen convergence (`G26`)

GitHub's native **Resolve conversation** / **Unresolve conversation** UI buttons
emit `pull_request_review_thread` (`resolved` / `unresolved`), which caller
workflows intentionally do not subscribe to:

- Clicking **Resolve conversation** on an unfixed, unoverridden blocking or
  human-adjudication thread never triggers hidden inference, never deletes the
  finding from ReviewSensei's durable obligation inventory, and never grants
  native approval (`manual_thread_resolve_withheld_unfixed_obligation`).
- Running `@reviewsensei verify` performs bounded, zero-inference reconciliation
  under finite GitHub API read/mutation budgets:
  - Observes threads already resolved on GitHub idempotently without redundant
    mutations when the finding's fix or authorized override is proven.
  - Resolves manually reopened threads via GraphQL `resolveReviewThread` when
    valid disposition markers match the live `(base_sha, head_sha)` snapshot.
  - Holds approval (`stale_or_unconfirmed_thread_mapping` or
    `verify_head_or_base_changed`) when thread mappings belong to a stale
    snapshot or live head/base drifted.
  - Converges cleanly to native approval (`verify_converged_to_approval`) once
    all blocking and human-adjudication obligations have proven fixes or
    authorized overrides and matching threads are resolved.

## Human pull-request review independence (`G28`)

Native human `pull_request_review` events (`submitted` with `approved`,
`changes_requested`, or `commented`; `edited`; and `dismissed`) are owned
exclusively by GitHub branch protection:

- Human reviews never mutate ReviewSensei's session-ledger generation, never
  clear `open_finding_ids`, and never trigger model inference.
- Forged `reviewsensei:resolve` markers inside a human review body are ignored
  because `resolution_records` accepts only reviews authored by the configured
  ReviewSensei GitHub App identity (`app_slug`).
- Human native approval never substitutes for incomplete/partial ReviewSensei
  coverage, open blocking or human-adjudication findings, or a stale
  `(base_sha, head_sha)` snapshot.
- Human `changes_requested` or `dismissed` reviews never block ReviewSensei's
  own recovery operations (`@reviewsensei verify`, `@reviewsensei rescan`,
  `@reviewsensei review remaining`, `workflow_dispatch`).
- Maintainer command admission validates both the actor's `author_association`
  (`OWNER`, `MEMBER`, `COLLABORATOR`, rejecting unprivileged comment authors via
  `unprivileged_comment_author_ignored`) and live repository collaborator
  permission (`lookup_collaborator_permission` in `admin`, `maintain`, `write`);
  if a former reviewer loses repository write privileges, subsequent
  `@reviewsensei` override commands apply zero dispositions or approvals
  (`explicit_noop`, `reason="no_change"`) while preserving prior ledger history.

### Source of write authority

The caller workflow's read-only `GITHUB_TOKEN` permissions are unchanged. Native
command writes use the hosted App installation capability obtained through the
OIDC-authenticated broker and its session grant, not broader workflow token
permissions. See [the architecture](architecture.md) and the
[approval recovery procedure](approval-recovery.md). Routing or convergence
classification alone cannot issue that capability or bypass live permission checks.

## Operator recovery quick reference

```text
@reviewsensei review status
@reviewsensei verify
@reviewsensei review remaining
@reviewsensei rescan
```

- Use `@reviewsensei review status` (zero inference) to inspect current coverage,
  open obligations, or held/uncertain operations, including on closed PRs.
- Use `@reviewsensei verify` (zero inference, finite GitHub API reads/mutations)
  after legitimate overrides or manual thread toggles on an already-reviewed live
  `(base_sha, head_sha)`.
- Use `@reviewsensei review remaining` or `workflow_dispatch` (`review_remaining:
  true`) to advance coverage on a published partial text review.
- Use `workflow_dispatch` or `@reviewsensei rescan` when a `synchronize` webhook
  was missed, a queued review run was superseded, or `pull_request: edited`
  retargeted the base branch.
