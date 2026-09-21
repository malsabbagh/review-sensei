# ADR 0053 - Bounded durable convergence history

Status: Proposed
Date: 2026-09-20
GitHub Issue: #146 F2
Owners/Reviewers: Maintainers

## Context

The session ledger previously persisted round counters and the F1 transaction,
but a new process could not distinguish a completed compatible review from a
lost or expired review history. Retaining raw prompts, diffs, or provider
responses would make the session comment an unbounded and unsafe source store.

## Decision

Add an optional, integrity-covered `convergence_history` envelope to the
existing session record. It is canonical UTF-8 JSON, has a 2048-byte component
limit, and is also constrained by the record's existing 4096-byte limit.

The envelope contains only closed metadata:

- lifecycle state;
- reviewed base/head, policy and coverage digests;
- at most two stable concern and resolution-criterion digests; and
- three lifecycle progress markers, each of which can carry a canonical
  admitted-blocker-set digest. These three markers are the durable F3
  repeat/oscillation window; they are independent of the two verification
  rounds governed by ADR 0048 and the round budget in ADR 0049.
- an authenticated ledger digest.

The history participates in `record_sha256`. A malformed, oversized, or
tampered history therefore fails ledger parsing rather than permitting a fresh
initialization. Only the current digest payload carries the envelope: a record
holding one is rejected under the legacy or operator-paused digest shape, so an
in-place upgrade can never keep a digest computed without it. The F3 slice
translates this persisted envelope into the runtime baseline used by the review
service and reserves enough space for three trusted blocker-set identities.
The baseline snapshot therefore retains two, rather than three, findings:
keeping all three would exceed the fixed 2048-byte component bound when F3
records the minimum repeat/oscillation evidence. F2 deliberately does not
change the live inference path.

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

## Validation

Run session-ledger and schema regressions covering round-trip persistence,
digest tampering, bounded shape rejection, and local/GitHub comment framing.

## Links

- [Issue #146](https://github.com/malsabbagh/review-sensei/issues/146)
- [ADR 0047](0047-durable-review-session-ledger.md)
- [ADR 0048](0048-baseline-aware-verification.md)
- [ADR 0052](0052-logical-review-transaction-across-analysis-and-publication.md)
