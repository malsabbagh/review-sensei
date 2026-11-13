# ADR 0136: Codify GitHub event routing, current-state convergence, and merge-queue boundaries

- Status: Proposed
- Date: 2026-11-11
- Decision owner/reviewers: ReviewSensei maintainers; independent source review
- Linked GitHub Issue: pending; user-directed lifecycle gap qualification (G01, G07, G13, G26, G27, G28, G32)
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

1. **Exhaustive event decision table and installed caller mapping (`G01`, `G27`,
   `G32`):** Separate caller workflow subscriptions (`CALLER_SUBSCRIBED_EVENTS`)
   from runtime admission guards (`EVENT_DECISION_TABLE`) across all 13 event
   families and bind `.github/workflows/review-sensei-review.yml` through
   `hosting/github/trigger.py`, `webhooks.py`, `comment_intent.py`, and
   `action_run.py` via `map_installed_caller_subscription`. Routing-only
   preflight never authenticates live title, head, or draft state (`G22`), and
   exact boolean `draft` validation (`G24`) gates admission. Ordinary comment
   admission enforces immutable source-comment revision fences (`G27`), while
   stranded unstarted actions after source mutation (`G32`) require both
   verified `NoBusinessDispatchEvidence` and qualified cessation/canonical
   fencing (`UnstartedCallerRecoveryGate`). A request count is not non-dispatch
   authority: the process-issued capture must cover the transport's complete
   physical mutation inventory. Each mutation needs a confirmed owned root
   activation/admission or predecessor-archive receipt, or an exact configured
   broker authentication endpoint classification. Root receipts follow native
   readback and bind the outgoing root; unrelated business mutations, copied or
   changed captures, later mutations, missing receipts and unknown outcomes hold
   for exact result recovery. This process-local proof is not serialized historical
   cessation authority and creates no replacement allowance.
2. **Current-state convergence, ownership, and finite stop rule (`G13`):**
   Model reconciliation of admitted events and manual recovery actions against
   a supplied authenticated live pull-request `(base_sha, head_sha)` target
   (`LivePullRequestTarget`) and supplied `AcceptedOperationRecord` history,
   rather than assuming durable FIFO webhook delivery. The classifier assigns
   explicit ownership (`RECONCILIATION_OWNERS`) and enforces a finite no-progress
   stop (`MAX_NO_PROGRESS_RECONCILIATION_ATTEMPTS = 2`,
   `finite_no_progress_stop`) when that history proves repeated unchanged
   non-progressing attempts, resetting when the supplied state advances. It
   launches no background reconcilers or paid retries. The deterministic
   `simulate_actions_concurrency_queue` helper returns `ReplacedQueueNotice`
   values naming displaced pending runs and recovery commands; the convergence
   classifier holds when supplied `was_queued_replaced=True`. Neither helper
   detects real Actions queue replacement or publishes a notice from a displaced
   run. Durable history/progress inputs and live queue-replacement observation
   remain integration requirements, not guarantees of the installed caller.
3. **Explicit `merge_group` unsupported boundary and snapshot certification
   (`G07`):** Reject `merge_group` (`checks_requested`, `destroyed`) events
   deterministically with `merge_group_unsupported` across `trigger.py`,
   `WebhookVerifier`, and `action_run.event_source` with zero token/provider/
   ledger/check side effects. Bind `ReviewCertificationReceipt` verification
   strictly to one pull request's exact `(repository_id, pull_request_number,
   reviewed_base_sha, reviewed_head_sha)` so an ordinary PR-head approval or
   `ReviewSensei` check receipt can never certify a `gh-readonly-queue/*` ref,
   synthetic queue SHA, advanced queue base, or multi-PR merge group. Removing a
   PR from a merge queue leaves its underlying PR-head receipt and obligations
   intact.
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
   `workflow_dispatch`. Subsequent maintainer commands validate both the
   actor's `author_association` (`OWNER`, `MEMBER`, `COLLABORATOR`) at
   routing/webhook admission and live collaborator permission
   (`lookup_collaborator_permission`) inside `action_run.execute_command` so
   revoked privileges cannot apply overrides or manufacture native approvals.

## Scope

Hosted GitHub event classification, webhook preflight refusal for `merge_group`,
current-state convergence modeling, snapshot certification verification, manual
thread `verify` qualification, human review independence, and operator
documentation (`docs/event-routing-and-triggers.md`). Existing reusable and
caller workflow YAML files, core review-engine modules, ledger wire formats,
and provider/API budgets remain unchanged. No automatic polling, background
reconciler, live branch-protection change, or historical ledger settlement is
introduced.

### Delivery receipts are not completed operations

The comment and event-routing verifier records a delivery observation only after
validation and route construction succeed. A failure while classifying or
executing the lifecycle leaves the delivery eligible for bounded reinspection;
any native-effect uncertainty remains owned by the authenticated session and
caller accounting, not by the delivery observation cache. The existing default
`InMemoryDeliveryLedger` is verifier-process-local, bounded to 10,000 entries
with oldest-entry eviction, and lost on restart. Alternate `DeliveryLedger`
adapters do not make receipt observation into completed-operation authority.

A duplicate delivery flag alone cannot declare convergence or suppress admission:
a caller can fail after receiving a route and before accepting the operation.
Redelivery therefore re-evaluates the supplied current source/session state
rather than trusting the delivery flag. Where matching accepted-operation
history is supplied to the convergence classifier, completed work is reused,
pending/uncertain work holds, and unadmitted work may reach the existing
admission gate. This is not proof that ordinary cold-process routing reconstructs
all prior accepted operations. Route selection never itself dispatches inference
or establishes terminal native completion.
This correction does not introduce durable webhook processing storage, relax
original budgets, or replace the G02/G08 writer fence and G13 durable-history
integration requirements. Legacy installation/setup verification is unchanged.

### Current-root accepted action observation (G13/G32 partial slice)

The existing `ApiRunRequestTransport.admit_action`, execution-entry checkpoint
and terminal-result writer are the durable producer for ordinary command/reply
actions. `accepted_action` now provides a typed observation of those exact
accounting bytes, source revision, operation key, head, phase, uncertainty and
terminal outcome. Ordinary source lifecycle classification consumes the same
normalizer rather than independently interpreting the action mapping. The
selected record is locally integrity-checked and detached before normalization:
post-construction mutation of its nested accounting cannot retain the old root
hash while changing the observation. The accounting digest hashes the canonical
stored mapping directly, not a second model serialization. Neither check
authenticates an arbitrary copied/evolved record or establishes native freshness.
Terminal normalization uses the existing retained-result decoder and exact
operation / producer binding before returning a result digest.

`read_current_action` is a separate read-only entry point for a fresh process.
It requires configured Bot ownership and the existing native read budget, loads
one current root and refuses missing/invalid authority. An authenticated
actionless root means only that this root contains no action. It does not mean
that earlier accepted operations are absent. Selected-root normalization adds no
HTTP requests to the ordinary lifecycle, while explicit current-root reads
consume the existing ledger budget. Neither entry point follows ancestors,
changes census/storage limits, writes state, or issues a replacement allowance.

An observation is not an execution or settlement capability and does not make a
stale selected root current. Existing live source, caller ownership, uncertainty,
positive non-dispatch and cessation/fencing checks remain mandatory. In
particular `execution_started=false` never becomes `NoBusinessDispatchEvidence`.
The action intent does not retain a review base or full finding-state inventory.
Consequently these observations deliberately cannot be converted into G13
`AcceptedOperationRecord.made_progress` receipts: even an explicit no-op may
have performed native finalization. No no-progress count is inferred from
request counts, an outcome label or a changed root hash.

This qualifies only cold reconstruction of one existing action observation and
its ordinary source-lifecycle consumption using synthetic native HTTP. Full
accepted-operation history production/consumption, durable semantic-progress
receipts, queue-replacement observation, cross-process non-dispatch/cessation
and installed/deployed qualification remain open. It closes neither G13 nor
G32 and settles no historical obligation.

## Consequences

The event decision table gives each covered GitHub pull-request lifecycle
event a deterministic, tested classification without dispatching inference.
With the required live target and accepted-operation history supplied, the
convergence classifier rejects stale events, reuses matching retained work and
holds uncertain work without scheduling redundant inference. This modeled
behavior is not an end-to-end guarantee that duplicate, delayed, or out-of-order
deliveries cannot cause repeated paid analysis in an installed caller: full
durable-history production/consumption and queue-replacement observation remain
open. Merge-queue commits cannot inherit certification from ordinary PR-head
receipts under the exact-snapshot certification boundary. Operators must
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
`tests/test_accepted_action.py`, `tests/test_trigger_coverage_g01.py`,
`tests/test_event_convergence_g13.py`,
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

## Bounded ordinary accepted semantic history (2026-11-13 candidate)

The ordinary action runner now distinguishes accepted result observations from
semantic progress. A qualified action records `accepted-semantic-history-v1`
in its version-2 finding reference, within the existing 8 KiB reference limit.
The version-2 action-result carrier commits to those exact canonical summary
bytes with a fixed-width SHA-256 outside its compressed business envelope.
Legacy version-1 full-summary result carriers remain strictly readable. No new
top-level root field, history census, polling subscription, caller allowance,
evidence limit, or cancellation authority is added. The summary retains at most
the two latest
accepted operation identities and exact source tuples; older results remain in
the existing immutable predecessor archives. Its saturated two-attempt streak
survives ordinary different-run rollover and fresh processes.

Semantic observations describe the complete **reviewed carrier state**, not a
new observation of mutable native GitHub state. They bind reviewed base/head,
full inventory identity, unresolved obligations, changed-file coverage, pending
native intents, trust uncertainty, and retained native receipts. Root hashes,
request accounting, round numbers, terminal command outcomes and new receipt
bytes alone are not progress. Positive progress requires an authenticated
reviewed-state advance, such as fewer unresolved obligations in the same full
inventory, more reviewed coverage in the same enumerated diff, or resolution of
an uncertain native binding. A completed command never becomes a completed
review record.

The pre-admission consumer opens the current owned carrier and at most the exact
immediate predecessor when needed. Rollover also consumes the result it already
authenticates, refusing an exhausted unchanged streak **before** successor
archive/allocation. Pending publication and uncertain original effects retain
priority over no-progress refusal; observation never settles those liabilities.
Settlement uses the source-issued, integrity-revalidated current caller
checkpoint: unchanged carriers reuse the bound observation, and changed inline
carriers are decoded without another remote read. Authenticated quarantine
history contributes its complete immutable inventory and pending obligations to
the existing fixed-width, domain-separated inventory/unresolved hashes and count.
A real historical decision can therefore advance progress when the current
finding carrier is unchanged; a colliding current-finding no-op cannot. The
historical observation needs its exact registered source root, or an exact owned
GET receipt proving unchanged complete lineage across the same caller's CAS.
Missing custody is not an empty historical inventory. This adds no settlement
read, wire field, fresh budget or history reset; raw current findings are kept
separately in invocation-local context so the aggregate is applied once. The existing final expected-root
CAS and native readback authenticate the resulting summary. The ordinary command
representability preview reserves bounded summary bytes within the unchanged
root limit. The admitted-intent preflight prices a worst-width new observation,
its retained predecessor, future reference metadata, and the existing maximum
inline action-result envelope before dispatch. Final exact representability remains
a separate check, including business-root changes and original request funding.
The command preview uses the identical business-result encoder and prices the
fixed-width external commitment. Unknown semantic digest entropy therefore cannot
change result width. Results without a finding reference keep version-1 carriers;
unknown inline metadata there retains a conservative compression/base64 bound. Every future
finding checkpoint reserves the same new summary against its actual and maximum
future manifest, plus the maximum existing terminal result width in the same
root. Separate old/new widths fitting is not sufficient for admission.

Reference metadata amendments do not rewrite the finding manifest or its lineage.
Core validation independently decodes the terminal inline action result using its
exact intent, owner generation, configuration and producer binding, then verifies
the SHA-256 of the exact reference summary against the closed version-2 carrier
commitment (or literal summary equality for legacy version-1 results). A decoder
must receive the summary from that authenticated current or archived root, not
from a newer root or a caller assertion. Missing, mismatched, mixed-version and
foreign history refuses; no reconstruction or empty-history fallback is allowed.
All other rollover fields retain exact equality with the
ordinary predecessor transition. This works with partitioned finding carriers;
partitioned action-result proof is deliberately unsupported and held. Metadata
cannot be cleared/downgraded or lost at the next finding checkpoint. Version-2
reference data takes precedence over older inner-journal history.

Qualification includes a synthetic-native, four-process ordinary CLI chain:
review, first status command, second distinct-source status command, then finite
refusal with no additional review/provider effect. A separate five-process chain
includes an intervening ordinary review and confirms the same finite stop, without
ancestor traversal. An actual owned partition checkpoint issues a digest-bound
semantic observation; changed document bytes and stale root observations reject.
This is not the old supplied
`AcceptedOperationRecord` simulator. Original-source mutation and unstarted
cancellation tests remain separate; no process-issued cancellation receipt is
serialized or reconstructed from these history observations.

Remaining limits are explicit, not closure claims. A changed partitioned carrier
needs a semantic observation from its actual checkpoint producer; until then the
confirmed action result retains a named semantic-unavailable observation and
prior summary instead of adding an unpriced post-effect census. Intervening
review-only runs preserve reference metadata without interpreting the review as a
new accepted command. Legacy results lacking this summary do not silently become
empty accepted history. Existing command, discussion and recovery executors may
continue without semantic qualification, under their unchanged source-authority,
reviewed-snapshot and finalization gates, retaining explicit unavailable metadata.
That compatibility route cannot assert reviewed-snapshot convergence or authorize
cancellation. Semantic convergence claims hold on missing/incomplete history;
malformed summary data refuses rather than taking that compatibility path.
Queue replacement still needs an actual authenticated producer. Cold G32 recovery
still requires genuine zero-dispatch plus cessation/epoch evidence and is not
conferred by action observation, history, terminal accounting or this summary.

The command finalizer prices both complete and contingent override projections
before native cleanup. The generic G12/G14 boundary remains named
`finding_lifecycle_verified_fixed_subset_capacity_unqualified`: endpoint
projections alone cannot bound every independently variable release subset.
A source-qualified ordinary command may instead use the retained reply evidence
and finite cleanup-prefix contract below. Unsupported readers, source frames and
release layouts retain the pre-native hold; this is not full-gap closure.

### Source-qualified command cleanup prefixes

A fresh ordinary command can consume the original inline finding index and its
later human-assessment frame when both complete bodies, numeric native review
identities and head bindings match retained authenticated native-write receipts.
The frame pair is not inferred from a copied marker, a newer approval or missing
history. Its closing frontier and fresh pull-request fence still apply before
execution.

The exact transferred command plan prices the complete preconfirmed group and
every ordered confirmed cleanup prefix, including layouts with and without that
group. It does not enumerate or assume arbitrary success subsets. Planning and
execution validate all selected thread/comment IDs and whole-body digests,
including already-resolved targets, through the existing discovery query. Local
rewalk of those same nodes adds no HTTP reads. An edited original criterion is
not released merely because GitHub reports its thread resolved.

Only observed confirmed members can release their original contingent slots.
The durable shadow for later check/approval intents includes the preconfirmed
group after cleanup, but never in the earlier review-body update. First uncertain
cleanup preserves the confirmed prefix and unresolved obligations and blocks
later native completion. Copied plans and changed authority frames remain held.

Qualification uses the actual original 512-call reply producer, a fresh command
with reopened and preconfirmed targets, mixed confirmed/refused cleanup, native
body mutation after plan transfer and cold verification. A new command with an
actual one-call-short 457-call configuration refuses before business writes and
does not change the producer's original 512-call history. This is a bounded
source contract, not general cold-uncertain recovery or authority to settle an
unknown effect.

### Source-qualified human reply cleanup prefixes

A closed, single-page, inline-authority human assessment on an issue comment may
now price its complete finalization before publishing the visible reply. This is
an actual reply producer, not a command/disposition capability: validated source
and assessment evidence determine the exact new authority frame and admitted
reply intent. Complete native discovery must match the retained immutable comment
and thread IDs and native body digest. Fresh source, head, authority and per-target
REST ownership fences remain required during execution.

The process-local reply plan commits the original selected lifecycle document,
source frame, request order, original budget, and finite release projections. It
prices report delivery, authority publication, cleanup, possible approval, check
publication, uncertainty readback and durable terminal retention together. Newly
allocated review IDs retain conservative encoding bounds. Same-journal reuse is
allowed only across exact source-owned custody; it avoids duplicate restoration,
not the fresh pending-intent checkpoint/CAS before native publication. A copied,
mutated, foreign or stale plan grants no execution or sizing exemption.

Reachable releases are the source-authenticated preconfirmed group and the
confirmed ordered mutation prefix. The preconfirmed group is released only when
all its members are still positively observed. Unexpectedly resolved suffix
members remain contingent for a later priced invocation. First unconfirmed cleanup
stops later native check/approval writes and returns current-incomplete status;
old approval/check observations cannot substitute for current completion. Lost
responses retain their intents and cold G32 holds, without inferred cessation.

Qualification includes an unchanged 512-call two-finding producer/reply sequence
and an actual one-call-short original allocation refusal before visible or native
business effects. Its command consumer is qualified only through the exact
source-bound prefix contract above; neither path qualifies arbitrary paginated,
external-inventory, custom-reader, or cold uncertain recovery paths. A non-approving policy with an inline blocking finding retains
`reply_capacity_inline_blocking_tail_unqualified` until its additional scan and
check outcomes have a source-qualified complete tail. All storage, response,
query, effect and funding limits remain
unchanged; this source-qualified slice is not full-gap closure.
