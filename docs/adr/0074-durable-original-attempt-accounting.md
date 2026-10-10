# ADR 0074 - Durable original-attempt accounting

Status: Proposed
Date: 2026-10-10
Owners/Reviewers: Maintainers
Approved by: not applicable
Related issues: [#238](https://github.com/malsabbagh/review-sensei/issues/238), [#240](https://github.com/malsabbagh/review-sensei/issues/240), [#242](https://github.com/malsabbagh/review-sensei/issues/242), [#245](https://github.com/malsabbagh/review-sensei/issues/245)
Related PR: [#257](https://github.com/malsabbagh/review-sensei/pull/257)

## Context

Live activation can prepay its final root reads and writes, yet a crash before
the new root commits can leave only an older accounting snapshot. A fresh
consuming grant authenticates a mutation; it cannot prove that the earlier
attempt spent no resources. Reopening the older snapshot would replenish the
original allowance. A local live-object proof correctly refuses this recovery,
but reliable hosted recovery needs a separate durable original-attempt witness.

The existing broker has SQLite-backed Durable Object storage. Its grant rows
are removed on consumption and do not retain the original source, admission,
accounting or unknown transition. Existing grants and endpoints remain unchanged
by this proposal.

## Proposed storage primitive

The dormant `OriginalAttemptJournal` module stores fixed-width digests, bounded
integers and transition states. No Worker route, broker handler or production
ledger constructs it. It is not an authenticated witness or a grant issuer.

A scope identifies one independently authenticated repository/PR lifecycle.
Within that scope a stable event key binds its first exact original operation,
inventory, execution, owner, reservation and initial root. Changing inventory
or operation cannot create another allowance for that event. A server-derived
start tick and conservative bootstrap debit are immutable after first admission.
The control horizon remains 60 seconds; ordinary and total dispatch ceilings
remain 60 and 64. Repeated admission returns existing metadata without refreshing
time or counters, including after expiry.

Each transition binds exact expected-prior and planned-target root digests and
generations, original sequence, request, dispatch and sealed-plan digests.
Its full declared liability is debited before external work. A synchronous
same-storage grant-consumption callback and the debit/in-flight record commit
in one SQLite transaction. Repeating the same transition reports its retained
state and never consumes or returns one-use authority again. A conflicting
transition refuses. Validated metadata is copied to closed immutable scalar
records before callbacks; callback mutation cannot change SQL keys or replay
identity. Accessors are refused. A retained server-clock high-water mark refuses
new transitions below admission or a committed observation. Observed expiry is
retained even when the clock later regresses; the original absolute deadline
never moves. Missing clock provenance refuses mutation rather than creating a
new origin. Previously committed transitions remain readable without issuing
another consumption permit. The callback's independent authentication and use of the
same storage are obligations of the future broker integration.

An in-flight transition blocks another protocol writer for that scope until
independent exact owned readback confirms the sealed target root. Expiry, an
older root observation or a lost response cannot release this guard. Exact
confirmation advances the mutable root cursor without changing original
identity or refunding prepaid calls. SQL and GitHub/provider writes are separate
transactions; this mechanism does not make them atomic.

The prototype caps 1,024 scopes, 256 source guards per scope, 64 transitions per
source and 16,384 retained transitions in its instance. These are finite storage
admission bounds, not a supported hosted profile. Expired records are not
evicted automatically. Capacity refuses before a new transition; future
maintainer recovery or garbage collection must prove that no current or
retained authority references a removed obligation.

## Authority gates before any public integration

The authorizer must independently prove approved caller/runtime/OIDC, exact live
complete source and numeric actor, the unique canonical current App-owned root,
all relevant original inventory/admission manifests, trusted resource policy and
origin, original reservation, and exact prior/target transition.

An initial combined admission does not yet exist in the old root. An opaque
admission digest plus a root GET cannot prove its derivation. The trusted
renderer/policy must establish that admission before it becomes authority.
Likewise, costs incurred before authoritative insertion require a reviewed
bootstrap reservation covering caller, GitHub/JWKS, Durable Object service hops,
retries and failures without unauthenticated denial of service. A caller-supplied
start timestamp or a fresh grant is insufficient. The prototype tests use
trusted synthetic origins and declared liabilities, not that missing public
bootstrap protocol.

Restore must use an authenticated server witness and preserve original charges,
deadline, output reservation, unknown dispatch and source bindings. A raw SQL
snapshot, a changed run or inventory, a parsed version-2 reference, or a fresh
local budget cannot bypass these checks. Until the authorizer, bootstrap,
complete cost accounting and restore adapter are reviewed and qualified,
all richer writers remain disabled.

## Validation and rollback

Real local SQLite transactions test sticky event conflicts, immutable origin,
debit plus same-transaction consumption and rollback, exact readback,
cross-source root serialization, retained unknown state after database reopen,
expiry, capacity and malformed metadata. These tests exercise the storage
primitive, not production BrokerLedger authorization, external root ownership
or a useful original-64 hosted lifecycle.

Required next evidence includes production broker integration, exact first
admission derivation, parent kills before/after debit and lost consume response,
pending remote writes that could arrive after a restart, concurrent sources,
all physical dispatch categories, and actual full/reply/command acknowledgement.
Rollback keeps the unused module unconnected. A future activated deployment
must retain original guards and unknown transitions; rollback cannot erase them
to obtain another allowance.

The bootstrap profile and producer contract are recorded in ADR 0076. This
record remains the storage primitive. The human-file recorder is ADR 0077.

## Sources

The implementation uses synchronous transactions and consumes SQL cursors before
external work, following the
[SQLite storage API](https://developers.cloudflare.com/durable-objects/api/sqlite-storage-api/)
and [Durable Objects rules](https://developers.cloudflare.com/durable-objects/best-practices/rules-of-durable-objects/).
It introduces no namespace, deployment binding, secret or external storage
service. Those API properties do not substitute for the authority gates above.
