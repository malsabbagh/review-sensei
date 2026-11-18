# ADR 0138: Lossless lifecycle checkpoints with an immutable shared base

- Status: Proposed; implementation and exact-head qualification pending
- Date: 2026-11-13
- Decision owner/reviewers: ReviewSensei maintainers; independent source review
- Linked GitHub Issue: pending; user-directed G10/G14/G16 lifecycle qualification
- Linked PR: pending

## Context

A complete command must price every durable intent, possible stop, native effect,
confirmation and terminal checkpoint before its first business write. Pricing
each effect independently can admit a prefix without admitting its complete tail.
Repeating the entire finding journal in every immutable checkpoint can exceed the
existing single-page response bound even when the logical inventory is bounded.

Increasing capacities, deleting old checkpoints, dropping hidden obligations or
assuming that unknown native identifiers compress well are not acceptable fixes.
The ordinary full snapshot already retained by the authenticated session can
instead supply compression bytes to subsequent checkpoints in the same command.

## Decision

Introduce a separately versioned, closed representation for source-qualified
pending checkpoints. It contains the complete immutable base manifest and a
losslessly compressed complete target journal, with explicit target length and
digest. The dictionary derivation is versioned and deterministic: canonical
sorted unique string keys and values from the decoded base journal, bounded to
the final 32,768 bytes. The target remains the full journal, not a patch or a
partial inventory.

Only an independently authenticated full legacy snapshot may supply the base.
Dictionary checkpoints cannot be bases for other dictionary checkpoints. The
base's scope, content, producer and generation are checked; its generation may
be older than the current root. The current authenticated root selects the
checkpoint and commits its base manifest. The base contributes compression data,
not current authority, lineage, semantic progress or permission to dispatch.

Reconstruction checks encoded and decoded bounds, complete stream consumption,
canonical target JSON, target digest and length, followed by all existing journal,
native receipt and lifecycle validators. Missing, edited, foreign, recursive or
unreadable bases fail closed. A fresh current-root fence follows all dependent
reads, including when the outer checkpoint is inline. Existing v1 through v3
readers remain available; ordinary final snapshots remain self-contained.

The source-owned command plan commits the exact base manifest and pending target
documents. Only those exact documents receive exact compressed-size pricing.
Unknown outcomes retain conservative full-snapshot pricing. Each mutually
exclusive branch includes its own complete stop or success tail; admission uses
the maximum whole-branch resource liabilities, not the sum of incompatible
branches or an optimistic successful prefix.

## Capacity, funding and privacy

The existing base is retained rather than copied into new dictionary parts. All
old evidence and orphan allocations remain counted. Every additional dependent
read, validation, fresh fence, write and final checkpoint is priced under the
original operation budget before business effects. An inline outer carrier is
not evidence that its dependencies are read-free. Process-local authenticated
bytes may be reused only through the existing source-qualified custody proofs;
cold restoration must not assume that cache exists.

The 256-part, 200-allocation, 8 MiB retention, 512 KiB response and 8 KiB query
bounds do not change. Neither the lifecycle effect/byte limits nor protected
staging and terminal reserves change. There is no larger-history census, new
storage service, activation, permission change or automatic retry loop.

Compression adds no data to the journal and is not encryption. Existing data
handling rules still exclude credentials, prompts and raw provider captures.

## Qualification and consequences

Required coverage includes full original-budget command completion, one-short
funding refusal before business effects, cold restart from a pending checkpoint,
missing or changed base parts, foreign scope/producer, changed current root,
recursive base rejection, decompression overflow, trailing streams and target
tampering. The complete logical inventory, historical liabilities and native
receipts must survive every supported representation and crash boundary.

The representation adds a bounded dependency and decoding complexity. A final
self-contained snapshot limits ordinary future restoration cost but does not
erase earlier retained checkpoints. Offline size measurements establish design
feasibility only; they are not runtime qualification, a deployed rollout or full
closure of the residual-gap plan.

### Source-qualified paged checkpoint tranches

A plain hosted lifecycle checkpoint can exceed the generic ten-call operation
tranche without exceeding the frozen run budget. The exact source-owned adapter
now issues a one-use capacity proof tied to its caller, selected root, complete
ordered inventory, unchanged record bytes and future checkpoint parts. It chooses
a sealed sequence of page sizes from 100/20/10/5/1 and reserves every full
preactivation and final inventory, including a short terminal page. Each selected
size divides the accumulated offset: `(page - 1) * per_page` is exactly the next
unread offset. Changing sizes therefore neither skips nor overlaps entries. The
sequence and each response bound are part of the issued proof, not choices made
from later responses. Each complete census remains
at most ten physical reads and at most 200 allocations. This is not the denied
larger census or a change to generic operation funding.

The complete future plan includes each retained body, conservative root growth,
every possible new part and its metadata, with each response below 512 KiB.
Ordering, positive unique IDs, body growth and terminal completeness are checked
again on actual reads. There is no adaptive retry or smaller-page fallback after
admission. New parts append to the authenticated creation-order prefix; a changed
prefix or duplicate appended ID refuses. Existing ownership, dependency, native
head, expected-root and unknown-outcome checks remain independent.

A checkpoint with `n` new parts and `p` inventory pages reserves its source-owned
base calls plus `3*n + 2*(p-1)` and existing restart fences. The same original
run funds any specialized tranche and its expanded confirmation tail; protected
terminal liabilities remain reserved. Inline checkpoints consume the same opaque
pricing proof: both post-mutator inventories are included, not just the last
readback. Copied, mutated, foreign, stale or reused proofs do not authorize a
tranche. Missing funds refuse before the first checkpoint effect.

Focused qualification includes both actual 77-finding history cases at 512 calls,
PR342 recovery at 1024, zero-part inline paging, insufficient funding, response
growth, duplicate IDs, deleted dependencies before and after PATCH, changed
ordering and unsupported page/allocation envelopes. These original-budget
fixtures are source qualification, not universal capacity or final exact-head CI.


### Quarantine source-owned checkpoint prices

The complete authenticated quarantine census has a separately source-qualified
checkpoint price. With `n > 0` new parts and a closed census bound `p <= 10`,
its whole operation reserves `p + 6 + 2*n` calls. Once that exact operation has
issued its fresh owned-root snapshot, its target-bound remaining stage reserves
`2*n + 5`. Both core and native part-codec guards consume this proof; an outer
quote alone cannot bypass an unfunded inner guard. The exact document digest,
part hashes, binding, item count and manifest allowance are pinned. All original
confirmation and caller terminal tails remain protected.

Only the one-time installed native producer can issue these one-use witnesses.
Copied or substituted issuers, changed targets, foreign/stale snapshots and
unqualified adapters cannot obtain the smaller price. Unsupported inherited
adapters retain their conservative generic guards. Zero-part quarantine writes
still use the generic price until their actual inline writer is independently
qualified. No arithmetic-only discount, larger census or new allowance follows
from this contract.

The fixed 200-object bound counts existing observed comments together with
prospective allocations. The older ordinary REST profile's 300-comment positive
case is therefore incompatible with this bounded lifecycle profile and now has
an explicit pre-publication hold. That is a stated support limit, not successful
handling of a larger inventory. No observed objects are hidden or excluded to
fit the bound. A future larger profile would need separate authorization and
qualification; the current implementation does not provide it.


### Creation-phase census and lifetime capacity

The literal-blocker prepublication creation route can qualify its two complete
censuses separately from the successor's later lifetime. Before the first effect,
its lifetime plan still admits every known comment, future carrier, successor
root and native publication tail under the unchanged 200-allocation and 8 MiB
bounds. Phase pricing does not remove those future obligations.

A one-shot source-owned dispatcher restricts the creation phase to the exact
registered carrier and root POSTs, including their URL, bytes and authorization.
Review, check, reply and decision mutations cannot consume that phase witness.
The witness pins the native transport, caller, root, run, attempt, job,
configuration and registration; substituted executors and a later-installed
capacity guard refuse. Guarded profiles retain their original conservative plan
until their own source-qualified extension exists.

The original 52-obligation fixture qualifies creation within its existing funding
with 167 lifetime allocations and 6,427,467 lifetime wire bytes, retaining 109
future carriers and the native publication tail. Stored or absent responses and
interrupted carriers preserve the original one-shot state rather than reposting.
This is creation qualification only: later publication, decisions, terminal
settlement and deployed isolation remain separate requirements.

The large-comment P1 fixture independently completes its original 512-call run
using the sealed aligned page recipe. Changed, grown, reordered or oversized
frontiers still refuse before business publication. Neither this positive nor the
creation proof permits inventories above 200 or closes the complete gap plan.
