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

## First public-command integration measurement

The opt-in offline host hook now supplies the same native ledger and confirmed
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
mutation, a conservative-plan deficit of1. This is not a physical lower bound or
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
