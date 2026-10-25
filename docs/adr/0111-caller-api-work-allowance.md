# ADR 0111: Caller API work and independent Worker bounds

- Status: Proposed
- Date: 2026-10-25
- Decision owner/reviewers: repository owner; Astra High
- Related issue: user-directed API-work redesign (issue not created)
- Pull request: not yet created

## Context

The current metadata windows prepay Worker-internal requests from the caller's
allowance. Completion and publication can also construct new windows. Neither
behavior represents one repository-configured caller allowance across an Actions
run. A Worker POST with 9, 12 or 30 internal dispatches is one caller request.

## Decision

Add one root `api_request_limit`. The offline candidate default is 128; the
shipping default and supported maximum remain subject to complete measurements.
It counts ReviewSensei caller HTTP attempts, including direct GitHub/evidence
requests, OIDC and each Worker POST. Inference retains its separate limits.
Worker-internal auth/history/token dispatches keep independent service bounds;
receipt counts are observations, without caller predebits or refunds.

Identify one allowance by immutable repository ID and Actions `run_id`.
`run_attempt`, job and owner generation select execution owners, never budgets.
Freeze trusted base/default-branch config commit, digest and limit at admission.
Analyze and publish must use that same record.

Reserve finite source-controlled startup envelopes for each supported job's
initial attempt and one rerun from the same allowance. Envelopes include startup
auth/discovery and the initial persistence/confirmation tail. Their capacity
remains occupied after crashes and when unused. Later attempts and silent
same-slot restarts refuse before ReviewSensei HTTP. The operational config
minimum is the measured complete startup envelope, not merely a positive integer.
The parser accepts an integer shape; admission must validate this stronger bound.
No production envelope or shipping maximum has been selected in this first block.

Ordinary reservations must be authenticated and confirmed before consumption.
Pay their write/readback from already authorized capacity, never from the block
being acquired. Confirmed dispatches, reserved-undispatched requests, and pending
or uncertain requests all occupy allowance. Dispatch/confirmation changes the
category, without double-charging or refunding. Preserve pending request deadlines;
new requests get separately bounded deadlines. Provider execution cannot reset
caller accounting. Unknown prior consumption refuses instead of starting at zero.
Retain completed request identities for the run lifetime and confirm the exact
request/deadline, so delayed responses cannot settle a later request. Bind every
ordinary block to its issuing owner. A crash successor retains its predecessor's
charges but cannot consume the predecessor's undispatched remainder; explicit
authenticated orderly transfer is a separate adapter action, never automatic.
Conservative prior non-startup liability is separate from confirmed requests and
excludes the already prefunded startup envelopes.

All writers require the existing authoritative PR concurrency serialization.
Comment PATCH is not CAS; generation/readback only detect conflicts. Unsupported
overlap refuses. Owner handoff requires prior-owner cessation and confirmed
checkpoint ownership; timeout alone is insufficient.

Use one current authorization envelope for necessary identity/purpose/permissions,
separate from accounting. Remove obsolete executable compatibility, while retaining
historical bytes and rejecting incompatible in-flight authority explicitly.

## Scope

This first implementation block adds config parsing/display, immutable accounting
values/transitions, caller-only broker charging and caller-only terminal reserves.
The values deliberately authenticate nothing and cannot themselves authorize HTTP.
They do not implement a durable writer or certify that a reservation is confirmed.

The next bounded block embeds a closed accounting document inside the existing
session record's digest/schema and App-author boundary. Existing ledger mutations
preserve accounting charges, request history and foreign-owner reservations.
Live Actions reads check the current finite hosted owner and predecessor cessation;
a local exclusive startup claim rejects same-slot restarts before HTTP. The local
claim is not a distributed lock. Existing authoritative PR serialization remains
mandatory.

**Not completed by these blocks:** production bootstrap orchestration and all-job
startup policy, authoritative workflow-owner wiring, one-counter CLI/workflow
integration, current-only grant/readers, historical run rollover, and service-owned
Worker plan selection. Existing metadata-window constructors remain pending.
This commit is a review checkpoint, not a qualified runtime feature or release.

## Consequences

Worker fanout no longer consumes caller capacity. Failures still cost their
attempted caller POST. Pending/abandoned work cannot mint a new allowance.
Run-wide admission and complete-path minimum/default/max measurements are required
before publishing operational claims. No new Worker aggregate allocator is added;
existing external rate protection is not established by this change.

## Alternatives considered

- Larger coupled per-window profiles: rejected; they conflate two different pools
  and do not provide a run-wide bound.
- Caller allowance claims in Worker grants: unnecessary; Worker does not authorize
  workflow spending.
- Comment generation as a distributed lock: rejected; readback detects conflicts
  but cannot serialize concurrent writers.
- Automatic migration/refund on restart: rejected; unknown history is not capacity.

## Validation

Focused tests cover strict config shape; a fixture-derived startup minimum; both
startup attempts occupied before first persistence; owner changes without new
capacity; same-slot/third-attempt refusal; category conservation; uncertain deadline
retention; exact exhaustion; obsolete accounting versions; and 9/12/30 observed
Worker dispatches at identical one-POST caller cost. The startup envelope in those
value tests is explicitly synthetic, not a production cost measurement.

Full acceptance still requires physical startup/crash traces and real public
approval, withheld and failure paths with fake HTTP, authenticated restart,
unsupported-overlap refusal, real Worker enforcement and joint distribution gates.
The four previously held socket tests remain excluded.

The one-job persistence fixture now executes the real Python broker and ledger
create/update/readback APIs with fake external HTTP: 10 startup requests, then
14 recovery requests. Synthetic valid receipts reporting 9, 12 or 30 internal
dispatches leave these caller counts unchanged. It does not qualify the real
Worker or activate a public workflow path. Both startup slots remain prefunded:
28 initial + 50 recovery = 78, derived from the actual seven-request prelude,
existing ten-physical-request list bounds, native mutation/readback calls and
two fresh predecessor-job observations. A separate 31-request native replacement
block stays charged to its original owner, leaving 19 unallocated at 128. This is
conservative liability, not 109 observed requests or a full workflow success.
The successor cannot use the crashed owner's block; another conservative
31-request block would exceed available capacity by 12. That is a next-operation
admission constraint, not proof of an unavoidable 140-request physical minimum.
No limit is raised or reservation shrunk to make it pass.

The next bounded control enforces every physical opener call before dispatch.
Serialized owner changes must match exactly one supported startup claim. At128
the fresh31 reservation still refuses. A test-only limit derived as78+31+31=140
funds that reservation in the existing recovery mutation, confirms it, and then
executes one native replacement from the new block. In the small-history control
the physical counts are10+14+4; at the ten-page boundary they are28+50+31. The old31
remains charged in both cases. Redirects cannot hide extra physical requests;
failed/expired/unknown requests retain their charges. This does not recommend a
shipping default or complete all-phase integration; full useful-workflow
measurement remains pending.

An unknown initial POST with a retained owned record can recover without a
replacement POST. If the record cannot be established, recovery refuses instead
of recreating it. Lost PATCH responses use the planned readback slot to observe
the exact retained root without repeating the write. Active/changed predecessor
evidence, incomplete job inventories, same-slot restarts and third attempts
refuse. All-phase integration waits for review of this bounded result.

## Rollout and rollback

No deployment, credentials, package publication or activation. Preserve previous
experiments separately. Review this bounded commit before full integration.
Rollback this unshipped source block normally; never rewrite historical evidence
or reinterpret old authority as a fresh allowance.

## Follow-up work

Measure source-controlled startup slots, including workflow OIDC/PR calls outside
the CLI, then implement the authenticated serialized adapter with conservative
restart bounds. Wire the same accounting through every phase; remove obsolete
authority branches; qualify current Worker plans and full lifecycle failures.

## Historical c8f563f public-command experiment (superseded)

**This rejected experiment is retained as evidence; the finite plan below replaces its whole-remainder reservation.**

The opt-in offline host hook supplied the same native ledger and confirmed
caller tranche to the real core CLI admission, provider metadata and analysis
completion. The source-selected one-job startup plan remains28+50=78; its
fixture executes OIDC, broker, workflow PR/configuration and Actions reads outside
the engine command. It is not the complete all-job reusable-workflow inventory.
The first-run initializer confirms its native record before enabling the ordinary
tranche. It rejects a rerun creating a new allowance; historical rollover remains
unsupported. Native reserve/commit/abort use the same accounting-aware replace
seam. No metadata phase budget factory runs in this selected command.

At provisional128, the single ordinary tranche is50. The modest complete-review
fixture runs one fake provider call through normal operation-entry metadata and
retains its raw result. At the first completion-checkpoint preflight it has spent
20 ordinary requests, leaving30 usable. Native replacement requires its existing
three ten-request scans plus PATCH:31. It refuses before any request for that
mutation, a conservative-plan deficit of 1. This is not a physical lower bound or
permission to select129. Normal failure handling then spends four reads; both
abort mutations also refuse before writes. Total actual caller requests are34
(10 startup plus24 ordinary), with26 usable ordinary requests, no unallocated
capacity, and the original analysis transaction still held. No review, approval
or terminal-success check is published. Approval/withholding controls and the
complete publication/failure tail remain unqualified, so integration stops here.

A separate lost-initial-record-response control preserves the first owner's50
reserved requests. Recovery reads the actual retained marker, but its successor
reservation refuses before mutation; the predecessor's remainder is never made
usable. A comment carrier also requires its bounded write/confirmation room
before POST/PATCH; this does not replace the still-pending complete lifecycle
preflight. No activation or release entry selects these new hooks.

The accepted retained-rescan repair is independently ported. Its reader regression
uses a Bot-authenticated synthetic historical seed and a real native reservation
and readback before selecting the retained root. The actual full-rescan resolver
and reader then charge every GET, including native ledger fences, to the same
previously used caller transport, without another reader-budget constructor or a
write to that retained root. All20 decisions remain. This proves the read seam,
not production historical run-accounting rollover or an end-to-end hosted rescan.


## Finite complete-lifecycle qualification checkpoint

The current source-selected plan is in
[api_run_plan.py](../../packages/python/review-sensei-github/src/review_sensei_github/api_run_plan.py).
It covers the qualified fresh-review template: one changed file, at most one
finding, an inline baseline, and no preexisting decided threads. It does not
activate a workflow, establish arbitrary-input support, or choose a shipping
minimum/default/maximum.

The native evidence reader differs from ordinary HTTP pagination. Its existing
`_fetch_pages` loop can step up the five-size ladder after positive pages and
allows 1,000 items. A complete conservative walk bound is
`(1,000 + terminal 1) * ladder 5 = 5,005`; ordinary operation-comment walks retain
their ten-request ceiling. Four native discovery/rediscovery/preactivation/
readback walks plus PATCH bound an evidence-fenced mutation at 20,021 caller
requests. No scan, fallback, item, byte or timeout bound is removed to fit.
The twelve named publication authority stages and thirteen named history stages
are explicit in the plan. Their source invocations and all 32-part carrier
ceilings remain visible for independent review.

| Planned work | Reserved caller liability |
| --- | ---: |
| Admission observations, reservation and transaction attachment |55,057|
| Provider operation metadata and bounded result parts |91|
| Retention and analysis completion |20,117|
| Publication, inventory, check and normal approval |101,111|
| First-owner ordinary work total |176,376|
| Protected terminal/failure tail |70,081|
| One owner total |246,457|
| Initial plus one recovery startup |78|
| **Test-only qualification: startup plus two owners** |**492,992**|

These are conservative reservations, not physical requests or a recommended
product limit. The parser's provisional default remains 128. At128 the complete
initial/successor/terminal plan refuses before creating a marker or calling a
provider. Increasing the test configuration is not a proposal to ship that value.
The qualification value is computed from the declared plan, not increased until
a fixture passes.

`initialize_and_activate` now reserves the finite work and terminal tranches,
leaving one owner total free inside the original allowance. A larger configured
limit does not enlarge the first owner's tranche. Before inference, hooks verify
the intact owner-bound terminal tranche and preflight all remaining planned work.
A single caller-budget facade checks the already authenticated physical meter;
it creates no60/64 phase window and cannot serialize an old metadata snapshot.
Its known ordinary counter carries retained request IDs across restoration;
startup and opaque predecessor reservations are not relabeled as confirmed calls.
Every new physical request retains its own bounded deadline.

Terminal completion belongs to the application. Retention's existing pre-write
`publication_failed` uncertainty fence now uses a separately named mutation with
the same transaction/result/generation checks. It does not claim terminal
completion or add a write. Analysis cleanup claims its protected tail once before
its first read; nested error paths cannot retry an uncertain or unfunded abort.
The terminal selection is one-way and gives no access to another owner's blocks.

The actual core CLI plus public `GitHubApplication` controls use synthetic
external HTTP/provider responses and real admission, provider metadata,
retention, inventory, checks, approval and transaction mutations:

| Control | Physical attempts | Result |
| --- | ---: | --- |
| Complete review, no findings |82| One native approval, successful check, transaction completed once |
| Complete review with human decision pending |78| No approval, action-required check, transaction completed once |
| Provider failure |36| One cleanup owner; failed attempt recorded once |
| Interrupted first owner followed by supported recovery |101| Old capacity stays unavailable; successor completes normally |
| Unknown cleanup PATCH response |27| One pending request; no subsequent HTTP or repeated cleanup |

At normal approval,72 ordinary requests are confirmed, 246,385 remain reserved
undispatched, 78 startup units remain occupied, and 246,457 remain free for the
successor. On recovery, the authenticated predecessor's 246,455 remaining reserved
units stay opaque and unusable. Retained known request IDs survive; the observed
unknown response is not converted to a success or refunded. Live versus durable
confirmed counters differ by prepaid write/readback effects after sealing, as
in the accepted native checkpoint contract; the retained reservation covers that
uncertainty without minting capacity.

Worker receipts observing 9/12/30 session-internal requests yield identical
public-path caller counts. Action exchanges retain their independent 12 bound,
and publication exchanges retain their own service bound. No Workflow allowance
is sent as a Worker grant claim and no aggregate Worker allocator is introduced.
Real Worker qualification remains a separate gate.

The PR377 final exact-review selector is ported from tree `dc3b21a`. Its new
regression first reproduced the old incorrect prose selection, then passed. The
same-budget read proof again charges 20 GETs after a prior ordinary request,
preserves the ledger root and 20 decisions, and constructs no fallback window.

Before production/PR/release qualification, independently review this plan,
wire and qualify the full source-owned workflow bootstrap/job inventory, remove
remaining obsolete executable authorization/selector routes, integrate actual
main, and run joint installed core/adapter and platform gates. The adapter now
uses the core's named pre-write mutation, so coordinated versions/dependency pins
are required. No installed-wheel, deployment, release, live rescan or TaskAlong
approval is established by these offline controls. Full publication crash/CAS/
check/approval fault coverage and arbitrary-history run rollover remain pending.
