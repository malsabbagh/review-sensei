# ADR 0071 — Lossless inline review evidence with lifecycle capacity

Status: Proposed
Date: 2026-10-09
Owners/Reviewers: Maintainers
Related issue: reproduction and scope recorded in [PR #231 review](https://github.com/malsabbagh/review-sensei/pull/231#pullrequestreview-5467025468)
Implementation PR: pending
Related decisions: ADR 0053, ADR 0065, ADR 0066, ADR 0070

## Context

The 0.6.18 review of PR #231 completed enumeration and reviewed all ten files.
Run 37898058657 reported twelve findings and a 5,441-byte baseline; its diagnostic
identified only the two-finding writer cap, with 11,264 bytes available. PR #232
preserved publication instead of crashing but represented this capacity failure
as a partial review and `coverage-partial`. Coverage itself was complete. The two
human-assessment obligations were a separate reason to withhold approval.

The original two-finding projection reserved space in a small comment ledger for
publication state, blocker progress, and future transitions. Merely increasing
that count does not prove lifecycle capacity. Projection also makes admission
contexts and later verification lose obligations. The human inventory had a
similar mismatch: a twenty-finding, 2 KiB prose contract was used for the complete
inventory although validated reviews admit 250 findings with 16 KiB prose and
unified reassessment already plans bounded provider batches.

## Decision

Keep the existing storage adapters, atomic compare-and-swap update, ownership,
producer authentication, repository/PR identity, stale generation checks,
transaction/result digests, and retry protocol. Store full metadata inline in
that same authenticated record. Small records retain their established JSON
shape. Larger records use a closed `zlib-json-v1` envelope containing base64 data,
canonical decoded byte length, and a SHA-256 of the canonical JSON. This digest
is integrity evidence, not a substitute for ledger or producer authentication.

Baseline encoding retains every stable finding fingerprint, resolution
criterion, blocking fact, generation, cache identity, reviewed path and related
path. All consumers, including trusted admission contexts, use the same reader.
No projection can claim completeness. Findings are sorted by fingerprint and
paths by value for stable retry identities. Unique paths are counted after
validation and deduplication. Deep valid repository paths use the canonical
4,096-byte path validation; defect kinds use the validated 256-byte identity
limit, rather than the old narrower 128-character persistence field.

Readers enforce encoded bytes before base64 decoding, cap decompression at the
claimed length plus one, require stream termination without trailing streams,
verify exact length and digest, reject noncanonical/duplicate-key JSON, and
validate the closed expanded schema and runtime byte bounds. Expansion is
limited to the existing 2 MiB result budget. The baseline's 11,264-byte encoded
ceiling, 12,288-byte history, 20,480-byte record, three-entry progress window and
future lifecycle reserve remain intact. Record-dependent allocation can be
smaller. The metadata inventory ceiling remains 512; the validated result limit
remains 250. These are whole-inventory limits, distinct from provider batch caps.

Human inventory encoding retains full validated prose and all identities and
required paths. The immutable complete inventory is compressed; its mutable
resolved identity list remains outside that compressed member. Before admitting
an inventory, the writer proves that every resolution can fit the same 24 KiB
publication marker inventory bound. Any resolution subset is therefore safe.
The outer eligibility version is `3` for encoded human inventories. Versions
`1` and `2` remain readable. No finding is resolved by omission, inventory
compression, an incomplete batch, or a human request for approval. Provider
responses remain limited to twenty decisions per batch; trusted aggregates can
retain the complete validated inventory. Prompt, output, calls, deadlines and
required-evidence checks remain independently bounded.

A complete analysis that exceeds remaining baseline capacity retains every
publishable finding, uses the established fail-closed partial transaction, and
carries `persistence_status: capacity-exceeded` in its exact content digest.
Approval facts retain this cause across delayed finalization. CLI output and
GitHub review/check distinguish complete analysis, failed baseline persistence,
and withheld approval. The outcome is `baseline_capacity_exceeded`; coverage is
not relabeled incomplete. Partial analysis without this field keeps its existing
semantics. Human adjudication remains an independent approval requirement.

## Scope and consequences

This covers baseline serialization, all shared readers, full-review admission,
reply reassessment inventories and aggregates, verification, and capacity
status. No external service, repository permission, credential, retention
mechanism or infrastructure is introduced. Encoding reduces redundant framing
and repeated paths but cannot promise that arbitrary high-entropy inventories
fit. Oversized evidence is refused rather than clipped. Finding prose remains
out of session baselines; the human marker continues to contain the full prose
already required for reassessment. Encoding is not encryption or redaction.

Published review bodies still obey the configured formatted-summary budget and
GitHub body limit. An enormous prose inventory may be valid in memory yet too
large to publish in one review. Such publication is refused visibly;
it does not silently publish a smaller human-obligation set. Existing path,
required-path, work and HTTP pagination limits also remain intentional bounds.
API scans at their limit must not be interpreted as complete discovery.

## Alternatives considered

Raising a count without sizing later state recreates the original failure.
Truncating optional contexts or whole inventories loses authority and can create
false clean verification. Splitting evidence into many issue comments requires
new object ownership, manifest/read-back, retention/garbage collection, atomic
activation, duplicate discovery and recovery protocols. External artifacts add
availability and retention dependencies. Lossless inline evidence gives ordinary
larger inventories a useful complete baseline without these new failure modes.
Multi-object storage remains a separately reviewed option for truly large,
high-entropy reviews; this decision does not imply unbounded review support.

## Validation

Regress 12 and 24 distinct realistic findings, 500 covered paths plus thirteen
findings on duplicate paths, long Unicode paths, 200-byte defect kinds, exact
capacity boundaries, real 250-finding encoded overflow, tamper/truncated/trailing
streams and expansion bombs, schema validation, trusted admission round trips,
local restart, GitHub comment reconstruction, publication failure/retry, human
prose above 2 KiB, 25-obligation multi-batch reassessment and resolution growth.
Run all local gates, packaged wheel lanes, immutable legacy-reader qualification,
approved-model source review, and exact-head CI before rollout.

## Rollout and rollback

Upgrade all shared-ledger readers and delayed approval/reply finalizers before
allowing new writers. Old readers accept unchanged small legacy records and
reject richer encoded records or eligibility version 3; they must never infer
approval from an older marker when the latest record is unsupported. No schema
migration or forced clearing is needed for existing valid records.

This PR does not release, deploy, merge, move channels or rerun PR #231. After
maintainer approval, publish a normal separately authorized release and promote
its runtime only after reader qualification. A rollback to an older reader with
encoded records fails closed and needs reader restoration; do not delete or
reset obligations to make a rollback appear successful.

## Follow-up work

Keep HTTP terminal-page qualification and truly large segmented publication as
separate investigations. Validate runtime-channel/broker compatibility during
reader-first deployment and rerun the exact affected hosted review only after
separate authorization. Record observed capacity distributions with numeric
metadata, never provider output, source text or credentials.
