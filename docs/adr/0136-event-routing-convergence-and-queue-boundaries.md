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

### Guarded publication pair (implementation candidate)

The closed `finding-lifecycle-journal-v3` adapter document retains ordinary
individual effect records and adds one permanent `guarded_publication_pair`
descriptor. It binds the confirmed start-check receipt, the completed-check
request, and the following main-review request by their exact positions and
canonical request digests. The outer evidence and reference formats, physical
request accounting, effect-slot limits, and storage limits do not change.
Version 1 and version 2 keep their existing single-pending-suffix rule. Unknown
versions fail closed.

Only this validated pair can contain two pending effects. The descriptor is
liability and ordering evidence, not permission to dispatch. A source-issued,
invocation-local capability may dispatch the second member only after exact
confirmation of the first member and revalidation of the precommitted review
bytes. Failure, response loss, or changed framing stops the sequence; the
undispatched review remains a durable liability without acquiring a fictional
physical request charge. The descriptor is retained after confirmation. Later
ordinary effects are permitted only after both members are confirmed and remain
subject to the original single-pending rule.

Cold restoration never recreates the hot capability or sends an absent suffix.
An unknown or rejected completed check prevents reconciliation of the review as
a successful batch. Review confirmation still requires the exact numeric
ledger producer, current head, body and marker, with the original bounded
readback and comment checks. Missing, foreign, edited, ambiguous or incomplete
observations preserve uncertainty. This initial implementation may hold an
interrupted batch; it does not claim automatic completion of every crash case.

A complete source-bound capacity plan must cover the pending start checkpoint,
the pair checkpoint, all reachable receipt/uncertainty layouts, the final
checkpoint, and every native, source-read and terminal tail before the first
native write. For the exact empty-original retained-result producer, the original
validated pending-to-failed transaction transition may be committed atomically
with the first pending lifecycle intent in one next-generation root. Its private
receipt comes only from the actual retention validator and follows only the
original confirmed empty-blocker progress transition. An unknown joint CAS
cannot authorize native dispatch. Other producers retain their original writes.

The closed route prices at most three checkpoint attempts, including exception
finish, and its full native/source-read branch maximum. Completion uses the
original, unclaimed terminal reservation only after exact source, owner, name,
accounting and remaining-capacity validation. The generic additional terminal
tail remains unchanged outside this qualified route. The actual one-plus-one
512-call producer has a funded positive with this fusion; larger populations,
crash paths and the full failure matrix remain qualification obligations, not
implied closure. Historical assessment reads and all current authority fences
remain required.

The pair's not-yet-observed request/receipt digests, status codes, numeric
preconditions and numeric check paths occupy independent existing packed-v3 atom
slots. Their equality cannot change the compressed skeleton through aliasing.
Only the source-bound, uniquely referenced completed-check path may retain its
exact repository prefix and price the maximum int63 suffix; unrelated native
atoms keep the original UTF-8 bound. The first, still-unpaired carrier uses the
codec's inner zlib compressBound plus base64 framing across every supported stop
layout. These upper envelopes are sizing-only and cannot decode as lifecycle
authority. Ordinary carriers, original limits and cold logical validation remain
unchanged. Genuine plain isolation and nonmatching producer shapes retain the
original publication path before any deferral is issued.

### Detached assessment-event envelope experiment (not activated)

The existing one-part decision writer has a narrower lossless byte envelope
than an arbitrary evidence part. Its closed event contains one decision, one
previous one-part manifest, and a sequence at most 200. A 4096-byte printable
UTF-8 reason expands to at most 12288 bytes inside the existing ASCII canonical
JSON encoding. The pure `quarantine_event_envelope` calculation preserves the
maximum repository, producer, generic storage-ID and numeric widths, then uses
zlib compressBound and exact base64/part/comment/JSON framing. It yields 19205
body-JSON bytes per event. This value is not an admission or provenance proof;
no runtime census or writer consumes it. Generic cursors may name multi-part
manifests, so an eventual consumer must authenticate the actual source-owned
one-part chain and enforce the same bound at both creation and every writer.

For 52 obligations, the allocation count remains 109: 52 events, 52 complete
terminal-run archives and five existing extras. Full archives keep their prior
32768-byte carrier allowance and framing. Replacing only event widths saves
731900 prospective body bytes, without releasing any allocation, history,
reason, obligation or request reservation. The existing complete-census planner
still rejects even a hypothetical empty retained frontier combined with these
typed slots and the full review tail: its result is 11, outside the unchanged
ten-request ceiling. This experiment therefore does not qualify the 80-raw /
52-obligation journey or justify enabling the smaller runtime quote.

The full review tail is installed during source bootstrap, before the provider
result exists. It reserves a maximum accepted-result carrier plus independent
lossless checkpoint/inventory projections, not three duplicate full-result
copies. Later lifecycle planning currently adds that same generic tail alongside
already-observed bodies. Any future narrowing must bind the actual accepted
operation/result and every remaining permitted renderer/fallback branch, prove
which promised allocations have already materialized, and enforce that scope
before affected writes. A cached body is not fresh census evidence; all current
membership/body checks and cold source dependencies remain required.

The tail's small projection widths also require writer qualification: the
checkpoint and human-inventory adapters currently select compact projections
only with an installed quarantine capacity guard. Plain target-isolation without
that guard uses the raw writers. A prospective plain-profile optimization must
therefore prove/enforce its actual renderer variants, not assume that the tail
helper's advertised projection slots alone constrain those raw writes.

Creation already separates phase reachability from lifetime storage promises.
For exact plain literal-blocker isolation, `prepare_census` validates the full
lifetime allocation/storage set but bounds the creation read phase with only
retained bodies, the new root and new creation parts. Its phase guard disallows
later successor allocations until activation. The later lifecycle census instead
adds all future decision/archive slots and the generic full review tail. The
historical lifecycle census refusal therefore does not establish that creation
itself must fail before the provider can return an exact result.

A separate non-executable materialization observer can inspect the original
accepted-result capture against the selected transaction, current caller/root,
run, payload digest and exact event/part IDs. It reports actual renderer widths
alongside the unchanged generic tail, explicitly releasing zero allocations.
It performs no HTTP requests and is not a current-body read, capacity witness or
activation permit. Unknown physical requests remain reported and charged. A
future plan must authenticate the materialized slot association and prevent all
unpriced renderer/recovery growth before using any smaller remaining envelope;
this diagnostic report does not implement that transition.

A hot source-phase prototype now records the original successful analysis return,
its confirmed accepted-operation carriers, validated retention, and the journal
constructed by the original executing publication frame. An exact empty-current-
result plain-isolation route can close operation-artifact writes for the remainder
of that invocation. Both the source port and the original caller dispatch reject
later operation/result writes, including identical-payload retries to new IDs.
The dispatch boundary rechecks installed writer source; neither a returned report
nor a consumer-supplied payload can open the phase. Already-installed bodies and
mutable event envelopes remain independently represented in the complete census.
This prototype still uses the unchanged full runtime capacity quote.

Removing just the accepted-result maximum would leave seven unknown renderer
slots totaling 218154 body-JSON bytes, in addition to all 109 event/archive slots.
The current packed-reader proof still returns 11 even with no retained bodies.
The remaining slots are startup archive (36864), operation metadata (65536),
inventory intent and fallback manifest (8192 each), checkpoint projection (11278),
and two inventory projection carriers (60034 and 28058). A complete solution must
prove which of these cannot be newly allocated in the closed phase, while retaining
all installed carriers and original lifetime promises. No startup completion,
terminal settlement or cold-command authority is inferred from the hot capture.
Cold commands require their own original entry/no-new-analysis contract and
independent authentication of the retained source and physical carrier inventory.

The hot phase's physical issue-comment boundary additionally binds each write to
one executing original ledger request inside its captured journal checkpoint, or
to the original application's direct terminal publication completion PATCH. The
method, path and complete body must equal that source request. Repeating the same
source dispatch, or issuing an archive/event/part-shaped body outside those
source frames, is rejected before physical accounting. This closes additional
write paths but does not issue a smaller quote or authenticate membership.

A future invocation-scoped plan must coexist with the unchanged lifetime
allocation plan. It must preserve every promised event/archive slot while
separately proving which allocations this invocation can reach. Its opening
metadata must reject identities outside the authenticated initial frontier plus
exact own-write receipts before fetching bodies. An interrupted own part requires
an independently precommitted body/owner bound, fresh exact reconciliation and a
write freeze until reconciliation; a guessed identifier or body-size observation
is insufficient. A lost root PATCH requires the original exact CAS/readback, not
cached-body authority. Current full-body reads and fresh closing membership remain
mandatory. The source phase does not yet implement these membership/receipt
proofs and still uses the unchanged full census price.

The first original 80/23 frontier contains 29 known identities with 1181696 bytes
of retained body maxima and 109 future event/archive promises. A hypothetical
plan restricted to those identities plus up to five own-part slots requires four
physical reads under the existing planner. That arithmetic is not an admitted
plan: the complete remaining checkpoint sequence, native readback, failed and
unknown outcomes, and terminal publication completion must all be proven within
the original remaining allowance. Neither this arithmetic nor the hot source
capture covers a cold successor's bootstrap discovery or authorizes dropping
lifetime allocation promises.

The inert hot-phase diagnostic also registers the original executing publication
implementation before its wrappers are installed. At the first original census
it can read that frame's resolved policy, eligibility, check outcome and assembly
inputs, validate their original source/data graph, and project source-shaped
pending, stopped and finish journal-document widths. Missing/early/unsupported
frames report unavailable, never an empty scope. This does not activate the
ordinary witness, which still excludes quarantine, and does not execute a native
request, change a journal, issue a census quote or price the complete operation.
The original untouched terminal block is reported separately from ordinary
remaining work. Carrier growth, complete future membership and all finalizer/read
liabilities still need independent qualification before any smaller quote.
