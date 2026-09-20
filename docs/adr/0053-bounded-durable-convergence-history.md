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
- at most three stable concern and resolution-criterion digests;
- two lifecycle progress markers; and
- an authenticated ledger digest.

The history participates in `record_sha256`. A malformed, oversized, or
tampered history therefore fails ledger parsing rather than permitting a fresh
initialization. The F3 slice will translate this persisted envelope into the
runtime baseline used by the review service; F2 deliberately does not change
the live inference path.

## Consequences

Fresh local and GitHub-comment ledger reads preserve a bounded completed
history without storing review source or model output. Capacity is checked
before writes and by untrusted-document loading. Existing records without the
optional field continue to parse for migration/recovery handling.

## Validation

Run session-ledger and schema regressions covering round-trip persistence,
digest tampering, bounded shape rejection, and local/GitHub comment framing.

## Links

- [Issue #146](https://github.com/malsabbagh/review-sensei/issues/146)
- [ADR 0047](0047-durable-review-session-ledger.md)
- [ADR 0048](0048-baseline-aware-verification.md)
- [ADR 0052](0052-logical-review-transaction-across-analysis-and-publication.md)
