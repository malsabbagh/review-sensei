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

**Not completed by these foundation blocks:** production bootstrap orchestration
and all-job startup policy, authoritative workflow-owner wiring, full installed
CLI/workflow integration, current-only grant/readers, historical run rollover,
and service-owned Worker plan selection. Later offline CLI/application integration
is described below. Unselected metadata-window constructors remain pending.
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
the physical counts are10+14+4. The earlier whole-walk control observed28+50+31
at the ten-page boundary; current operation allocation instead refuses that later
mutation before PATCH. The original31 remains charged. Redirects cannot hide extra physical requests;
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


## Historical 4c35a3b finite attempted-work checkpoint

The previous `bcd64fce` lifecycle evidence remains in Git and the frozen
qualification packet. Astra accepted its public-path evidence but rejected its
worst-case reservation plan as production admission. Its 492,992 test setting,
5,005-request native walk bound and 20,021-request mutation bound are not admission
minima or selected product limits. The current policy is in
[api_run_plan.py](../../packages/python/review-sensei-github/src/review_sensei_github/api_run_plan.py).

After the existing 78-unit startup reservation, divide the remaining allowance
between the first owner (ceiling half) and its supported successor (floor half).
Each owner's share protects 50 terminal attempts: two finite read operations and
three finite mutation operations for the selected failure-cleanup route. These
are attempted-work slots, not a promise to complete every history. The success
route uses the same protected tail. The rest is that owner's work tranche. Both
shares remain inside the original frozen allowance; neither is a new phase or
rerun window. An abandoned owner's remainder stays unavailable.

Every selected read operation gets at most ten physical requests from the live
owner's already reserved tranche. Every native ledger mutation also gets at most
ten requests **in total**, across discovery, rediscovery, preactivation, live
fences, PATCH and readback. Nested readers share that operation. Every oversized
response, fallback and terminal page consumes a physical attempt. Existing item,
body, author, exact-root, duplicate and complete-enumeration checks remain.
Enumeration stops before the next request would consume the protected tail of
one write plus two confirmation requests. Incomplete enumeration cannot authorize
a mutation, and a failed operation cannot obtain a fresh allocation to continue.

The actual native PATCH/readback still authenticates exact canonical bytes and
detects generation conflicts; it is not a distributed lock. Exhaustion before
PATCH clears only locally prepared bytes and records no mutation. Exhaustion or
failure after PATCH retains its original intent and allocation. Reconciliation
can use only that allocation's remaining confirmation requests, on the same owner
and block. A known HTTP response is distinct from a confirmed publication. Unknown
request responses retain their pending charge; exhausted semantic confirmation
can retain an unconfirmed publication even when all dispatched HTTP responses
were known. Neither condition authorizes another PATCH/POST.

Owned result-part comment writes similarly protect their finite confirmation
room. A lost POST permits only the original confirmation allocation, never a
new complete-list allowance or another POST. Publication admission uses the actual
canonical retained payload's part count and selected mutation/capability minimum;
part storage still validates its complete owned inventory. It does not reserve
all alternative carriers or the theoretical pagination maximum before beginning.
A larger history may refuse even under an otherwise qualified configuration.

The normal provider hook checks the intact protected terminal reservation and
attempts metadata with the existing work tranche. It no longer preflights every
theoretical remaining branch. The application remains the sole terminal-completion
owner. The pre-write unconfirmed-publication fence retains all transaction checks
without claiming terminal completion. A failed/unknown abort remains one-shot;
nested error handlers cannot repeatedly read or write cleanup.

The real core CLI and public `GitHubApplication` controls retain one file, zero
or one finding, an inline baseline and no decided threads. Only external HTTP
and provider responses are synthetic. No released workflow selects this path.
The smallest measured settings for these **fixed fixtures** are:

| Control | Smallest fixture setting | Physical attempts | Result |
| --- | ---: | ---: | --- |
| Complete review, no findings |313|82| Native approval, success check, transaction completed once |
| Complete review with pending human decision |305|78| No approval, action-required check, transaction completed once |
| Interrupted owner followed by supported recovery |314|101| Predecessor capacity retained; successor completes normally |

The common test configuration is derived from the measured 68 work requests:
`78 + 2 * (50 + 68) = 314`. Initial admission can use the odd unit, explaining
313 for initial approval; the successor cannot, explaining 314 for recovery.
Work is admission 11 + provider metadata 4 + completion 8 + publication 45.
Terminal completion adds four requests. This is a fixture regression bound,
**not a shipping default/minimum/maximum**. Provisional 128 remains unqualified
and refuses before creating a marker or calling a provider.

At common 314, normal approval has 72 known ordinary attempts, zero pending
requests, 46 reserved-undispatched and 118 unallocated for the successor; startup
still occupies 78. Recovery has 74 retained known ordinary attempts, 162 reserved
undispatched (116 belong to the predecessor), zero free capacity and one observed
unknown response in the abandoned owner. That abandoned uncertainty remains
covered by its opaque reservation, never converted into confirmed dispatches or
refunded. Live/durable confirmed counts differ by already prepaid effects after
sealing, as in the original authenticated checkpoint contract.

One-less controls refuse honestly rather than claim a completed lifecycle.
At 312 the approval control refuses its final request, records publication_failed
and emits no native approval. A previously completed check can already say success;
that check alone is not proof of completed publication or native approval. At 304
the human control also records publication_failed. At 313 the recovered approval
control refuses with the predecessor reservation unchanged. None is a new success
or permission to replay a mutation. These partial outcomes remain visible evidence.

Provider failure uses 36 physical calls and one cleanup owner. A lost cleanup
PATCH uses 27, retains one explicit pending request and makes no subsequent HTTP
request. History-growth controls stop after seven enumeration requests before
PATCH; confirmation growth stops after two readback requests and keeps the write
unconfirmed. An exact full page must still fund its final empty page. Oversized
fallbacks are charged and can exhaust either the read or confirmation allocation.

Synthetic valid receipts observing 9/12/30 session-internal dispatches preserve
identical caller totals. Action exchanges retain their separate 12 service bound,
publication exchanges their own service bound. No Worker-internal debit/refund or
workflow-capacity claim is added. Actual service-owned Worker enforcement and
aggregate rate protection remain separate qualification work.

The exact PR377 receipt-review selector remains ported from `dc3b21a`. Its reader
proof still charges 20 fresh GETs after one prior ordinary request, preserves the
root and all20 synthetic decisions, and constructs no fallback allowance. This
is not a complete retained-rescan lifecycle proof. Follow-on work separately needs
the full pre-provider transaction writes, both trigger paths, typed exhaustion
propagation and completion/approval/withholding controls.

Before production selection or removal of authorization readers: independently
review this allocation contract, qualify the full workflow startup/job inventory
and real Worker limits, integrate current main, remove obsolete executable
selectors/authority, and complete joint installed core/adapter/platform checks.
The core pre-write helper requires coordinated versions and dependency pins.
Arbitrary historical rollover, all publication crash/check/approval faults and
TaskAlong's 40-file/70-finding workload remain unqualified. No live activation,
provider request, credentials, rescan retry, package publication or release occurs.


## Single-process review entry checkpoint

Astra selected one package process for the review route, rather than a new
same-job handoff authority. The new `review-sensei-github review-run` entry claims
startup once, constructs one physical caller transport and accounting owner,
and runs the engine review, completion, fresh-head validation and publication.
The hosted and local plan use the same entry; local model discovery is one
metered, credential-free request. Inference retains independent provider limits.
The production reusable workflow has not selected this entry yet.

The policy commit P is authenticated independently from the reviewed base B and
head H. Startup checks the local checkout and policy-file object, authenticated
repository/default-branch/configuration bytes, the exact PR revisions and the
Actions run/job owner. Read-only owner checks precede the session enrollment
exchange. A changed policy, head, configuration or overlapping writer refuses.
The API allowance remains frozen; a changed working copy cannot authorize a new
limit. Command/reply and retained-publication recovery remain separate,
unqualified routes. Explicit rescan/remaining input is refused here pending its
own complete lifecycle qualification.

The actual nine-request startup prefix is repository policy metadata, default
policy ref, configuration contents, PR metadata, OIDC, Actions capability, run,
job inventory and session capability. Adding the two previously missing policy
reads changes source-prefunded startup from 28+50 to **30+52=82**. The accepted
operation policy is unchanged: ten requests per read/native operation, two write
confirmation requests, fifty protected terminal attempts and equal owner shares.
This is an inventory correction, not a larger ordinary allowance or a default
selection. Checkout, package installation, Git transport and artifact upload are
infrastructure outside the ReviewSensei API-work allowance. Business policy,
authentication, PR/evidence, OIDC/Worker and publication calls are inside it.

The engine passes its completed validated result, prior baseline, current key,
configuration/evidence contexts and exact diff through an in-process callback
after checkpointing. Publication revalidates the durable transaction normally.
Diagnostic JSON is output only: overwriting it cannot replace these inputs or
create authority. A lost marker POST or checkpoint PATCH can spend only its
original funded exact readback; there is no repeat mutation or capacity refill.
Handled provider failure/cancellation use the one protected cleanup owner. A
cancel before publication preserves the saved pending result.

The current fixed small-fixture input derives as `82+2*(50+68)=318`; local
model discovery adds one work request per owner, yielding test input320. Neither
value is a shipping default/minimum/maximum. The actual package entry measures
**84 caller attempts for approval**, **80 for legitimate human withholding**,
and **85 for local approval**. The fifteen focused tests also cover in-memory
publication despite corrupted diagnostics, duplicate startup, stale head,
configuration change, overlap, provider failure, cancellation, refused POST
charging, insufficient publication capacity and successful funded lost-write
readback. No replacement metadata-budget constructor runs. Default128 remains
unqualified and now refuses before business HTTP in this entry.

Still unqualified: the proposed workflow consolidation removing its separate
release-reservation and shell live-head/publication business processes; actual
hard-kill/recovery and multi-page/multi-part controls; real production Worker
identity, service-owned ceilings, grant and receipt composition; current-main,
installed and platform checks; obsolete authority removal; and the shipping
range. This first entry qualifies fresh-session startup. A different run's
existing session is refused rather than deleting its history, replacing its
accounting or silently creating a new allowance. Run-record rollover requires a
separate authenticated retention contract before claiming general operation.
No package publication, live activation, credentials or release is performed.
