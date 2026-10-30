# ADR 0119: High-signal completion and a durable review verdict

Status: Proposed
Date: 2026-10-29
Decision owner: ReviewSensei maintainers
Reviewers: implementation owner and independent policy reviewer
GitHub issue: Not assigned; user-directed strict published-comment policy request
Pull request: https://github.com/malsabbagh/review-sensei/pull/392

## Context

A strict cap on active published finding comments separates the complete review
verdict from selected comments. Omitting a required fix or consequential human
decision from publication cannot produce clean approval. Conversely, completed
candidate deduplication, disproof against available source, and out-of-diff
filtering do not establish incomplete execution. Selected findings must still
resolve through existing independent proof or explicit override rules.

Historical comment JSON omits runtime admission facts. A selection hash over
that JSON alone therefore cannot preserve a selected human decision or effective
blocker across accepted-operation replay.

## Decision

Add trusted YAML `review.quality` with `current`, `balanced`, and `high-signal`.
Omission is `current`; `balanced` currently keeps the same completion behavior.
Noncurrent quality participates in policy provenance and the policy digest.
No environment or command-line quality override is introduced.

High-signal completion uses structured verifier completion facts. Completed
rejection, exact duplication and location filtering do not add rejected-candidate
counts or alone downgrade completion. Missing or malformed source evidence,
unfinished verification and incomplete coverage retain their execution gates.
A new optional low-impact observation does not become a human obligation solely
because its baseline is incompatible; existing and ambiguous obligations remain.

Before accepted-result retention, trusted admission freezes an optional
`ReviewResult.review_verdict`. This closed document binds the policy digest,
selected-comment JSON digest, and one selected classification byte per comment:
`A` advisory, `B` blocking, or `H` human decision. The existing 250-comment ceiling
bounds this vector; it is not the future active-publication cap. Two independent
booleans retain required blocking and human decisions outside selection. There
are no excluded candidate identities, prose, counts, or publication queue.

The verdict participates in the explicit `reviewsensei:review-content:verdict:v1`
content-digest domain and sealed transaction verification. Cold readers restore
selected admission from the content-bound vector, rather than recomputing it
from model fields. Projection rejects changes to admitted candidates and retained
lifecycle obligations. Accepted-operation replay reuses the frozen result.

Approval facts version 3 carries the verdict separately from selected blocking
findings and `PendingHumanReview`. Selected resolution/override may clear its
own inventory; unselected bits remain current-review gates. Public checks consume
the same bits, including delayed finalization, without exposing excluded prose
or counts. Partial execution remains a coverage/status condition, not a fabricated
human-assessment record.

Each new high-signal review performs one full discovery pass over its review
input under existing work budgets. The authenticated baseline still reconciles
prior obligations. Exact partial/remaining continuations reuse completed work
and retain prior verdict bits. Broader-review composition also retains prior
bits. A new qualifying complete clean review can converge; a narrowed or
no-provider reuse cannot establish that an excluded requirement disappeared.

A confirmed authorized override plus confirmed native closure can release
published occupancy while its obligation and expiry remain. Only an actual
outstanding automatic-reactivation liability remains contingent. Later reopening
must reacquire capacity and reuse the existing target. No expiry rule changes.

## Scope

This draft implements quality provenance, completion semantics, durable verdict
facts, a pre-retention projection seam, replay/approval/check consumers, and a
pure count selector contract. The numerical strict active-publication cap,
authenticated occupancy census, native target update/reuse, and full publication
selection wiring are unfinished. This ADR does not enable a strict cap or a live
recovery, change any capacity bound, or qualify a release.

## Consequences

Full discovery can cost more than incremental discovery. Existing call, token,
time and total-work budgets still apply; this adds no automatic retry or review
loop. Verdict bytes must be included in the later complete lifecycle capacity
proof. Selected admission metadata survives replay without becoming a permanent
aggregate gate after its selected inventory is independently settled.

## Alternatives considered

- Deriving verdict solely from selected comments loses required unselected work.
- A sticky aggregate for all required findings prevents selected fixes and
  overrides from converging.
- Retaining every excluded candidate as a queue adds unwanted obligations and
  storage; identity-free current-review booleans avoid that queue.
- Recomputing selected admission from historical comment JSON loses trusted facts.
- Clearing aggregate bits through incremental/no-provider reuse lacks evidence.

## Validation

Focused tests cover configuration and policy identity, completed rejection versus
incomplete verification, selected and unselected verdict round trips, accepted
operation replay, selection/admission tampering, schema 4/5 facts, continuation
and broader-review retention, full discovery, explicit selected overrides,
delayed public checks, and released-slot recurrence. Broader source tests and
installed-package qualification are recorded with the implementation handoff;
this record alone is not a qualification receipt.

## Rollout and rollback

Keep the opt-in default at `current`; historical result bytes and digest behavior
remain unchanged when no verdict exists. Introduce readers/writers together in a
separate reviewed policy PR. Existing verdict-bearing accepted transactions must
remain readable or be withheld by an older reader; rollback must not strip their
facts or reinterpret them as clean. No release or production activation is part
of this slice.

## Original native occupancy projection

The inactive host seam `native_publication_occupancy` projects an already-issued
complete native publication read. It makes no additional HTTP request, does not
retry or enlarge the ten-call walk, and cannot authenticate a reconstructed
evidence document. Original transport, credential, caller, producer, final union
frame and exact inline finding index remain required. The complete numeric
root/body/opaque-thread association participates in each target proof; opaque
thread IDs retain their full supported width instead of being truncated into a
slot identifier. Revalidation compares the whole projection to the original
independent read, including membership and target versions.

This is one publication's native thread union, not global PR occupancy. It
refuses legacy, missing and body-placed current inventories. Other historical
publications, body findings and uncertain creation attempts still need their
own independently authenticated coverage before production selection. An
unresolved target reserves a slot. A resolved UI target is uncertain and also
reserves a slot; it is not treated as a confirmed release or an authenticated
automatic reopening liability. No caller-supplied release flag is introduced.

The numerical cap, global occupancy composition, confirmed settlement/reopening
protocol and in-place native revision/readback remain unfinished. Existing
publication entry points still reject every handoff-bearing result before
effects. This seam grants no mutation authority and enables no policy.

## Follow-up work

Prove and choose the numerical strict active-comment cap from the complete
lifecycle. Authenticate physical occupancy and unknown writes, freeze concrete
selection before accepted retention, implement stable native target updates,
qualify reopening reservations and public-entry journeys, and include all added
verdict bytes in capacity certification before enabling strict publication.


## Proposed inactive handoff amendment

Before retention, `freeze_publication_selection` requires an unsealed result
with trusted high-signal admission and candidates exactly covering that result.
The frozen optional handoff binds source, original admission criteria, policy,
occupancy, selected result and ordered target actions. Validation against the
original independent source is separate from cold result parsing. A digest is
not authority to invent dispositions or missing admission criteria.

The new `reviewsensei:review-content:publication-selection:v1` domain binds the
complete handoff around the existing selected-result digest. Validation uses a
pure projection and never removes a field from a sealed result temporarily.
Old result digests are unchanged without a handoff. Accepted replay retains
selection; it does not repeat discovery, selection or provider execution.

All handoff-bearing results are inactive at both publication entry points,
including empty and create-only plans, and fail before effects. An active target
has reuse intent; released targets must reacquire a slot. A contingent target
retains its reservation but reopening intent remains inert and needs separately
authenticated transition evidence. No inherited overflow exception exists.

An independent sidecar was considered, but would separate accepted replay from
its target intent and permit ordinary create fallback. Storing the closed record
inside the accepted result binds that intent locally. No production caller,
default N, native revision protocol or enlarged census is introduced. Rollback
must retain these bytes or refuse; dropping the handoff invalidates a sealed
transaction. Focused tests cover cold replay, closed parsing, source/criteria
binding, immutable obligations, target proofs, tampering and zero-effect refusal.
