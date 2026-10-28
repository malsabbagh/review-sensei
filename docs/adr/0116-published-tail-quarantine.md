# ADR 0116: Isolate a published unfinished tail and ceased discussions

- Status: Proposed; implementation and qualification, no activation
- Date: 2026-10-28
- Decision owner/reviewers: repository owner; Astra High
- Related issue: user-directed TaskAlong PR37 recovery
- Depends on: ADR 0115 and PR390

## Problem

A successful native publication can retain no terminal accounting marker when
optional old-thread cleanup is unconfirmed. Later discussion executions can have
separate unknown effects, even though the visible ledger still names the full
review. Success, cancellation, missing artifacts and empty pending arrays do not
settle those effects. Ordinary terminal rollover must continue to refuse them.

The literal-path prepublication contract in ADR 0115 does not cover this state.
Changing only a registry row or accepting every `publication_succeeded` record
would not prove isolation of shared threads or preserve the complete hold.

## Decision

Add a distinct `quarantine-published-tail-v1` lineage, closed activation shape and
published-tail/reply source contract. Preserve the original literal-path guard.
The installed contract binds one successful full review and four discussion
instances (one failed and three cancelled), each with its exact attempt, source
version, frozen configuration, owning job and available accounting snapshot.
A complete bounded Actions census and fresh closing reads reject omitted,
additional, active, rerun or changed executions. Its cutoff is the first
registered creation run's authenticated creation time; later successor runs do
not rewrite the historical execution set. Any pre-write refusal excluded from
the footprint needs its own positively reviewed source/evidence binding.

Bind historical broker behavior through internally resolved reviewed installed
provenance: account/service/environment, fixed endpoints, complete routing
intervals and routed versions, bundle/source/build/bindings hashes, retained
authenticated readback hashes and the reviewed native-effect contract. This is
service-level historical provenance, not live attestation or request settlement.
Token/session issuance may complete; its qualified handler cannot independently
write native PR state or restart a ceased caller. Unrelated setup continuation
has separate targets and grants no PR recovery authority.

The provenance catalog and activation registry remain empty. The workflow stays
disabled until separately reviewed exact registration. A release does not
activate this path. Missing historical coverage or an unqualified allocation
frontier refuses before creation.

## Accounting, allocation frontier and capacity

Archive the predecessor unchanged, including its absent terminal marker, charges,
reservations, counters, history, configuration and uncertainty. Preserve available
execution snapshots; missing discussion snapshots remain explicitly unknown.
API/provider upper bounds include known observations and are not measured spend,
independent additional charges, refunds or transferable allowance. The successor
receives only its ordinary new-run allowance.

Treat startup archive reconciliation separately from strict operation/evidence
staging. An uncertain predecessor-bound archive POST can continue when another
execution's identical carrier becomes visible. Its own request may still arrive
later. Therefore successful publication alone does not prove zero late archive
allocation. The conservative certification envelope is one startup archive per
execution plus one later issue-comment allocation per reply: nine in total.
Per-surface maxima may be reserved together as a conservative larger superset.
The poisoned operation port and serial staging bound the later allocation
frontier; reaction cleanup allocates no carrier. Thread resolution can continue
after an unknown mutation and needs a separate existing-object exposure bound.

Retain closed counts, new-object bytes, maximum encoded bytes of one final
object, and maximum positive existing-object growth separately for evidence parts, operation comments, inline replies,
reviews, checks, threads and reactions. Reserve late evidence growth throughout
creation, later staging, run archives and decisions. Time and absence release
nothing. Preflight existing storage, successor/archive storage, future decisions
and ordinary review/verification retention plus the certified old exposure.
Keep the 256-part, 8-MiB, response, pagination, request and deadline limits.
Insufficient capacity refuses before staging; uncertainty never creates room.
The per-object wire bound includes Unicode/JSON escaping and final update growth;
a body character limit cannot stand in for a response byte bound. Combining
evidence and operation comments adds their counts/total bytes and takes the larger
per-object maximum. Native response metadata remains an additional reservation.

Separate permanent storage from current native scan admission. Storage retains
two carriers per outstanding decision plus four ordinary review/verification
carriers. Native admission certifies the current complete census, permanent old
exposure and the complete current transaction tail before its first effect,
including startup archive, receipt, positive root/metadata growth, publication,
checks and terminal accounting. Dispatch moves a promised effect into charged
usage; unknown responses release nothing. Later transactions recertify before
their first effect, preserving the last terminal root and every open hold on
refusal. This does not promise unlimited future transactions.

Route guarded issue-comment consumers through the same existing closed cursor
census, including the initial bootstrap and cold operation reconciliation.
Start every cursor at100; an oversized bounded prefix yields only a count of
complete direct node objects inside the reserved response envelope. Retry the
same cursor as complete JSON. Prefix content is never authority. The certificate
counts every failed probe and complete response under the original ten-request
and512-KiB ceilings. Cold REST paths retain their actual alignment, size choices
and terminal-page convention; current-head checks keep their one-page bound.
Qualify the final individual decision, verify and the next ordinary review for
the supported heavy14 and80raw/23hold journeys with empty pagination profiles.

The check census uses the existing check-publication capability's read permission;
the workflow metadata token need not gain Checks permission. For the final root
creation fence, slot validation and policy/head validation share one fresh numeric
maintainer read. Individual decisions validate complete quarantine PR facts on
the action fence's same fresh PR response, with independent permission/policy reads.
Neither reuse accepts a cached observation or removes a required fact.

An issued exact-root startup rollover with inline baseline and no external
decision/queue references reserves a 17-request archive tail: one POST, up to
ten complete unknown-response census requests, one immutable-part GET, one exact
old-root GET, one current-PR GET, one PATCH and up to two exact readbacks. The
process-bound snapshot must match the original slot, caller, owner, root and
lineage. Ordinary rollover and external-reference cases keep the 34-call generic
preflight. No counter is compressed and no unknown charge is released.

## Compact result representation

The guarded current hosted default-inventory route uses one complete accepted
operation result and two closed projections. Existing full-result and full human
inventory formats keep their original readers. New checkpoint descriptors bind
the original owned operation event, confirmed transition/accepted packet, exact
ordered carrier IDs, numeric producer/App, run/attempt, repository/PR, base/head,
reservation and trusted policy/configuration/evidence. Full source hashes and
canonical reconstructed payload hashes protect all serialized fields, including
fields outside `content_digest`. The admitted transformation is transaction
binding, with the existing paired partial/capacity-exceeded baseline downgrade.
No arbitrary patch language or unreserved full-copy fallback is supported.

The versioned inventory projection binds the independently selected original
checkpoint, its physical carrier digest/IDs and complete result bytes. It keeps
the entire finding index and a closed source-ordinal/instance mapping carrying
the original effective-blocking and human-admission flags. The reader rebuilds
the existing immutable domain inventory and checks the full document digest.
It preserves either original evidence-policy value and does not rerun current
admission policy or infer clean state from raw JSON. Instance-to-lifecycle
mapping is checked against the original comment independently of admission.
This narrow producer starts with no extra required paths or visible-prose
receipts. Other shapes are unsupported on this guarded new route until
qualified; an unreserved full-copy fallback is not permitted. Missing, changed,
duplicate, foreign or incomplete source data holds without recreation.

The checkpoint descriptor binds the exact `literal-malformed-v1` native format.
Its writer requires a fresh original result captured while the audited guard
selected complete-tail mode; it cannot adopt a legacy publication's format.
Only a successfully authenticated descriptor read selects it for that original
transaction/result. Standalone old results keep their exact legacy rendering,
including uncertain original publications; replay accepts no arbitrary alternate
body. Malformed-fence literalization preserves all original retained bytes and
does not claim to be a general CommonMark validator. Its ephemeral format
selector caches no response bytes or authority decision. Logical reconstructed
results retain the core two-MiB ceiling; standalone physical result parts retain
their one-MiB ceiling. Descriptor preflight charges physical descriptor parts.

Native admission includes the maximum one-MiB operation fallback, eight-KiB
checkpoint projection,64-KiB inventory projection, intent and complete mandatory
publication/accounting tail before startup. Later exact preparation specializes
that envelope. It cannot acquire previously unreserved mandatory capacity.

## Two independent published origins

Carry `published-current` and `published-earlier` in a closed published-origins
variant, keeping the existing two-origin and 100-assessment-per-origin limits.
Authenticate both original records, native review identities, complete indexes,
transaction/result bindings, inventories and full prose. Carry all original
human findings, regardless of later old resolution receipts or thread flags.
For PR37 this conservatively preserves the earlier five and current nine.
No similarity or matching fingerprint deduplicates across origins.

Only a fresh authorized individual decision naming the complete origin scope
and full instance changes its hold. In this variant, an exact historical target
can be settled even if a fresh instance or concern recurs. The event changes
only the historical cursor; its `quarantine:` receipt is not an ordinary `RS-`
disposition. Fresh inventory, thread and approval gates remain independent.
The literal-path variant retains its existing collision refusal. Unknown schema
versions, mixed origin kinds and incomplete unions refuse.

## Effect isolation and ownership

Create the successor root, carriers and fresh native review/thread identities
after every classified caller has ceased. Old root PATCHes name the old comment;
old reviews/checks carry old heads; old thread resolutions name existing IDs.
They cannot name subsequently created successor objects. The old supersession
selector uses unequal heads, not ancestry, so cessation ordering and fresh thread
identities are required together. Never reuse an old thread as the sole gate for
a fresh finding. Late old replies, reactions, appended assessment receipts and
metadata cannot discharge an immutable hold or supply a new maintainer command.

Use the existing serialized PR lane and positive one-shot slot. Preserve exact
head/base/policy/numeric maintainer fences before effects, complete owned
readback, fork refusal and no predecessor fallback. Stored creation ambiguity
can continue only after exact readback; absent/unconfirmed creation cannot retry.
Never PATCH or fabricate completion of the predecessor.

## Qualification

Require public-entry and rebuilt installed journeys for all five executions and
fourteen holds: empty fresh full review stays action-required; both historical
decision kinds affect only their exact targets; a fresh recurrence remains open;
only its separate current decision can change its gate; verify is idempotent.
Inject delayed old root/check/thread/review/reply effects after creation and after
decisions, stored/unstored creation acknowledgments, source/head/base/policy/role
changes, additional ownership, forks and storage exhaustion. Qualify actual c7
installed allocation paths, including the cross-execution archive continuation
followed by a second unknown allocation. Released readers must refuse the new
lineage before business effects. Production Worker composition must preserve
the existing token split, authority and all budgets. No credential or grant
change is part of this decision.
