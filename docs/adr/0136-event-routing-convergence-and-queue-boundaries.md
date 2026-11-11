# ADR 0136: Codify GitHub event routing, current-state convergence, and merge-queue boundaries

- Status: Proposed
- Date: 2026-11-11
- Decision owner/reviewers: ReviewSensei maintainers; independent source review
- Linked GitHub Issue: pending; user-directed lifecycle gap qualification (G01, G07, G13, G26, G28)
- Linked PR: pending; independent prospective contract

## Context

ReviewSensei's checked-in caller workflow subscribes to a deliberately narrow set
of GitHub events (`pull_request` `opened`, `reopened`, `synchronize`,
`ready_for_review`; `issue_comment` `created`; `pull_request_review_comment`
`created`; and `workflow_dispatch`) so repositories incur zero unexpected
model-inference spend or background polling loops. In live pull-request
lifecycles, however, GitHub also emits auxiliary and out-of-band transitions:
`pull_request` `edited` (title/body or base retarget), `converted_to_draft`,
`closed`, branch deletion/recreation, `pull_request_review_thread`
`resolved`/`unresolved`, comment `edited`/`deleted`, native human
`pull_request_review` `submitted`/`edited`/`dismissed`, and `merge_group`
merge-queue events. In addition, webhook deliveries can arrive duplicated,
delayed, out of order, or be displaced in GitHub Actions per-PR concurrency
queues (`cancel-in-progress: false`).

Without an explicit source-level contract across all event families, callers or
operators could misread an unsubscribed transition as automatic approval, treat
a PR-head check receipt as certifying a synthetic merge-queue commit, assume a
native "Resolve conversation" click clears an unfixed finding obligation, or
conflate human GitHub reviews with ReviewSensei ledger accounting.

## Decision

Introduce `review_sensei_github.event_routing_contract` and typed webhook refusal
(`GitHubMergeGroupUnsupportedError`) to codify five coupled boundaries while
keeping the existing caller workflow subscriptions unchanged:

1. **Exhaustive event decision table (`G01`):** Separate caller workflow
   subscriptions (`CALLER_SUBSCRIBED_EVENTS`) from runtime admission guards
   (`EVENT_DECISION_TABLE`) across all 13 event families. Routing-only preflight
   never authenticates live title, head, or draft state (`G22`), and exact
   boolean `draft` validation (`G24`) gates admission. Every unsubscribed or
   rejected transition maps to a deterministic read-time guard and an explicit
   bounded operator recovery command while preserving prior open finding
   obligations and immutable source-event identity.
2. **Current-state convergence and queue replacement (`G13`):** Reconcile
   admitted events and manual recovery actions against the authenticated live
   pull-request `(base_sha, head_sha)` target (`LivePullRequestTarget`) rather
   than assuming durable FIFO webhook delivery. Inspect accepted intents,
   retained results, and uncertain dispatches before scheduling work so
   duplicate or delayed deliveries reuse retained results with zero fresh model
   inference and out-of-order stale heads are ignored. When per-PR Actions
   concurrency displaces a pending run, emit an explicit `ReplacedQueueNotice`
   naming the superseded run and exact recovery command instead of silently
   dropping accepted intent.
3. **Explicit `merge_group` unsupported boundary and snapshot certification
   (`G07`):** Reject `merge_group` (`checks_requested`, `destroyed`) events
   deterministically with `merge_group_unsupported`
   (`GitHubMergeGroupUnsupportedError` in `WebhookVerifier.verify` before
   delivery-ledger recording or payload JSON parsing, and zero token/provider/
   ledger/check side effects in `reject_merge_group_event`). Bind
   `ReviewCertificationReceipt` verification strictly to one pull request's
   exact `(repository_id, pull_request_number, reviewed_base_sha,
   reviewed_head_sha)` so an ordinary PR-head approval or `ReviewSensei` check
   receipt can never certify a `gh-readonly-queue/*` ref, synthetic queue SHA,
   advanced queue base, or multi-PR merge group. Removing a PR from a merge
   queue leaves its underlying PR-head receipt and obligations intact.
4. **Bounded `verify` convergence over manual thread toggles (`G26`):** Keep
   native GitHub `pull_request_review_thread` `resolved`/`unresolved` UI toggles
   unsubscribed and separate from durable finding obligations. Clicking "Resolve
   conversation" without a proven fix or authorized override never clears a
   blocking or human-adjudication obligation and never manufactures approval.
   Explicit `@reviewsensei verify` runs with zero model inference under finite
   GitHub API read/mutation budgets, idempotently observes already-resolved
   decided threads, resolves reopened decided threads under exact `(base_sha,
   head_sha)` authority, holds stale or unconfirmed mappings, and converges to
   approval once legitimate dispositions exist.
5. **Human `pull_request_review` independence (`G28`):** Leave native human
   review gates strictly to GitHub branch protection. Human `submitted`
   (`approved`, `changes_requested`, `commented`), `edited`, and `dismissed`
   reviews are explicit no-ops for ReviewSensei: they never mutate ledger
   generation, never clear open obligations, never inject resolution markers
   (enforced by strict `app_slug` authorship), never substitute for incomplete
   coverage or unlock stale snapshots, and never block `@reviewsensei verify`,
   `@reviewsensei rescan`, `@reviewsensei review remaining`, or
   `workflow_dispatch`. Subsequent maintainer commands validate the actor's
   current repository association so revoked privileges fail closed at
   admission.

## Scope

Hosted GitHub event classification, webhook preflight refusal for `merge_group`,
current-state convergence modeling, snapshot certification verification, manual
thread `verify` qualification, human review independence, and operator
documentation (`docs/event-routing-and-triggers.md`). Existing reusable and
caller workflow YAML files, core review-engine modules, ledger wire formats,
and provider/API budgets remain unchanged. No automatic polling, background
reconciler, live branch-protection change, or historical ledger settlement is
introduced.

## Consequences

Every GitHub pull-request lifecycle event has a deterministic, tested outcome
with zero unapproved inference spend. Duplicate, delayed, or out-of-order
deliveries cannot trigger redundant paid analysis, and merge-queue commits
cannot inherit false certification from ordinary PR-head reviews. Operators must
invoke the documented bounded recovery command (`@reviewsensei verify`,
`@reviewsensei review status`, `@reviewsensei rescan`,
`@reviewsensei review remaining`, or `workflow_dispatch`) after unsubscribed
out-of-band transitions such as base retargeting, manual thread toggles, or
Actions queue replacement.

## Alternatives considered

- **Subscribing caller workflows to `edited`, `pull_request_review_thread`,
  `pull_request_review`, or `merge_group`:** Rejected under the approved product
  boundary because high-frequency UI edits, review comments, and speculative
  merge-queue rebuilds would multiply Actions runs, API reads, and inference
  cost without an approved queue-snapshot authority contract.
- **Treating GitHub webhooks as an ordered queue:** Rejected because GitHub does
  not guarantee ordered delivery and Actions concurrency (`cancel-in-progress:
  false`) retains only one pending slot per group, replacing intermediate runs.
- **Accepting native thread resolution or human PR approval as finding closure:**
  Rejected because GitHub UI toggles and human branch-protection approvals do
  not prove ReviewSensei's independent finding criteria or coverage obligations
  were satisfied.

## Validation

Focused unit and integration-style provider-free suites qualify each slice:
`tests/test_trigger_coverage_g01.py`, `tests/test_event_convergence_g13.py`,
`tests/test_merge_queue_boundary_g07.py`,
`tests/test_thread_resolution_convergence_g26.py`, and
`tests/test_human_review_independence_g28.py`, alongside existing setup,
trigger, webhook, package-boundary, and `G22` context-policy regression suites.

## Rollout and rollback

Normal source review and required CI precede any package or tag release. Because
caller workflow YAML bytes are unchanged, existing installed callers remain
compatible without workflow migration. Reverting the module and webhook guard
restores prior behavior without affecting persisted session ledgers.

## Follow-up work

If repository owners later request native GitHub merge-queue support, a separate
ADR and permission gate must define queue-commit diff scope, multi-PR attribution,
funding, and required-check naming before enabling `merge_group` triggers.
Cross-writer atomic fencing (`G02`/`G08`) and unstarted source-edit cancellation
(`G32`) remain tracked on their dedicated authority tracks.
