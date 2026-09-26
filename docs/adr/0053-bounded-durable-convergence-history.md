# ADR 0053 - Bounded durable convergence history

Status: Proposed
Date: 2026-09-20
Last amended: 2026-09-26
GitHub Issue: #146 F2
Owners/Reviewers: Maintainers

Status note (2026-09-26): the completed-round counters this record bounds are
diagnostic history only; no round is refused for exceeding a count
([ADR 0056](0056-remove-pr-wide-review-count-caps.md), epic #181 C). The
storage bounds, envelope, and integrity checks below are unchanged, and
retained continuation grants stay counted as stored members because their
history is preserved verbatim.

## Context

The session ledger previously persisted round counters and the F1 transaction,
but a new process could not distinguish a completed compatible review from a
lost or expired review history. Retaining raw prompts, diffs, or provider
responses would make the session comment an unbounded and unsafe source store.

## Decision

Add an optional, integrity-covered `convergence_history` envelope to the
existing session record. It is canonical UTF-8 JSON, has a 4096-byte component
limit, and is also constrained by the record's 8192-byte limit.

The envelope contains only closed metadata:

- lifecycle state;
- reviewed base/head, policy and coverage digests;
- at most two stable concern and resolution-criterion digests; and
- three lifecycle progress markers, each of which can carry a canonical
  admitted-blocker-set digest and (for newly written markers) the owning
  transaction id. These three markers are the durable F3
  repeat/oscillation window; they are independent of the two verification
  rounds governed by ADR 0048 and the round budget in ADR 0049.
- an authenticated ledger digest.

The two-finding limit is the F3 writer projection. During rolling upgrades the
reader and schema continue to accept the F2 three-finding projection; a later
checkpoint rewrites it to the two-finding/three-marker writer shape.

The history participates in `record_sha256`. A malformed, oversized, or
tampered history therefore fails ledger parsing rather than permitting a fresh
initialization. Only the current digest payload carries the envelope: a record
holding one is rejected under the legacy or operator-paused digest shape, so an
in-place upgrade can never keep a digest computed without it. The F3 slice
translates this persisted envelope into the runtime baseline used by the review
service and reserves enough space for three trusted blocker-set identities.
The baseline snapshot therefore retains two, rather than three, findings: the
narrower projection leaves the reserved headroom for the repeat/oscillation
evidence and for the reviewed-path metadata that the next round classifies
against. F2 deliberately does not change the live inference path.

The bounds are derived from the reserved shape rather than chosen for
appearance. With repository-realistic identity content (a populated cache key,
finding paths and symbols, and identity-bearing progress markers) that shape
encodes to 2294 bytes with no path evidence and 3202 bytes for a twenty-four
path checkpoint, so the F2 2048-byte bound refused every realistic completion
without paths ever being the deciding member. The committed regression fixture
`test_repository_realistic_checkpoint_fits_the_component_bound` holds that
projection, so the derivation is reproducible rather than a claim about
fixtures that no longer exist. The envelope bound is therefore 4096 bytes, and
the record bound stays twice it: the largest non-envelope members (a
publication transaction, four maximal dispositions, and four maximal
continuation grants) measured 5704 bytes, which leaves a realistic record
bound by the envelope while a pathological combination of both is still
refused. Widening a bound never admits review source, prompts, diffs, or
provider output; the envelope remains closed metadata, and both capacities are
still checked before writes and by untrusted-document loading.

The convergence key is the canonical admitted blocker identity set, not the
provenance of equivalent evidence. Fresh candidate evidence still has to pass
admission and is reflected in the prepared result; evidence that leaves the
same blocker identities admitted is not verified progress and cannot lift a
terminal suppression. A maintainer may use the authenticated `@sensei review
reenroll` recovery command when a new operator decision is required.

Each F3-eligible transaction owns exactly one durable blocker marker. A
recovery replay may exclude the last marker only when that marker carries the
same transaction id; a marker from a prior round with the same blocker set is
still comparable evidence. Checkpoint placeholders are lifecycle-only entries
and do not evict the bounded comparable blocker window; the admission CAS
replaces the current transaction's placeholder with its owned marker.

The application owns the canonical post-admission preparation step. A publisher
adapter may expose only `publish`; in that case the application supplies the
same admitted result rather than bypassing F3 or requiring an adapter-specific
preparation method. The built-in GitHub publisher additionally rebinds the
prepared artifact at its write boundary.

## Session witness

Durable history is only meaningful while the record it describes exists, so F2
also closes the "a deleted marker looks like a first enrollment" gap. The
broker owns a hashed enrollment witness keyed by repository, pull request, and
the verified current head, reached through the closed `review_session`
capability (which requests only `pull_requests: write`). That witness is
retained for 90 days from its most recent use, matching the maximum session
lifetime, and is pruned in the same transaction that records it. The
GitHub-backed ledger treats an App-authored comment as a marker document only
when it carries a terminal marker line, so a quoted prefix cannot wedge a pull
request. A witness combined with a missing marker fails closed and names the
recovery command (`@sensei review reenroll`), the only operation allowed to
retire an expired or witness-only session.

## Consequences

Fresh local and GitHub-comment ledger reads preserve a bounded completed
history without storing review source or model output. Capacity is checked
before writes and by untrusted-document loading. Existing records without the
optional field continue to parse for migration/recovery handling.

## Migration and rollback

The envelope shape is intentionally fail-closed, but the reader remains
backward-compatible with the F2 envelope: it accepts up to three baseline
findings and the earlier two-marker progress window, while new checkpoints
write the F3 projection of two findings and up to three markers. Existing
integrity-valid records are not silently truncated during load; a later
checkpoint rewrites the bounded projection. Records that predate
`convergence_history` remain compatible and continue to follow the existing
migration path. Rolling back the code does not authorize publishing from a
record that failed the current integrity or shape checks; such a record still
requires the authenticated `@sensei review reenroll` recovery path.
Once `publication_suppressed` is persisted for a result, retries of that same
transaction return a terminal handoff with its transaction identity; a later
head or an authenticated reenrollment creates the new transaction needed for
publication.

## Validation

Run session-ledger and schema regressions covering round-trip persistence,
digest tampering, bounded shape rejection, and local/GitHub comment framing.
The bound tests use repository-realistic identity content and a realistic
reviewed-path count, because a minimal fixture cannot detect a component bound
that the reserved F3 writer shape already exceeds. Oversized path evidence is
covered by a narrowing test that pins the writer's reserved budget to the
envelope bound and proves the narrowed record plans a fallback-full scope, and
by a guard test that the reader never trusts a narrowed baseline as complete.

## Amendment 2026-09-26: narrowed path projection

The 4096-byte envelope is a hard component bound, but the reviewed-path
metadata the F3 writer persisted was not bounded by the reserved shape the
derivation measured. A pull request whose review retained more path evidence
than the envelope reserves could therefore reach a state where no later
checkpoint fits: the write path refused the envelope, and a live, readable
record could never advance, because reenrollment deliberately refuses live
records.

The writer now narrows the persisted path projection to the budget the
envelope reserves, deterministically and by value, admitting reviewed evidence
before related context, and clearing both `complete` and `coverage_complete`
whenever it drops anything. A narrowed baseline reads as incomplete rather than
as a truncated complete scope: the current reader plans a fallback-full scope,
and a reader released before this amendment reaches the same scope through its
existing incomplete-baseline handling. The runtime baseline, checkpoint gating,
and approval eligibility are unchanged, and no rollback authorizes publishing
from a record whose persisted scope was narrowed.

## Links

- [Issue #146](https://github.com/malsabbagh/review-sensei/issues/146)
- [ADR 0047](0047-durable-review-session-ledger.md)
- [ADR 0048](0048-baseline-aware-verification.md)
- [ADR 0052](0052-logical-review-transaction-across-analysis-and-publication.md)
