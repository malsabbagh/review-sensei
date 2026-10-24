# ADR 0106: Durable decision snapshots and analysis metadata phases

Status: Accepted; published in core 0.8.12 / hosted 0.3.13. Qualified workflow
activation and installed/live acceptance remain separate rollout evidence.
Date: 2026-10-23
Owners/Reviewers: Maintainers; independent Astra review
Linked issue: User-requested approval liveness repair
Linked PR: [361](https://github.com/malsabbagh/review-sensei/pull/361)

## Context

A fully audited twenty-decision ledger can occupy 9,941 bytes. Its next valid
entry exceeds the embedded 10,240-byte disposition allowance. Smaller command
batches cannot repair this lifetime storage dead end. One metadata deadline
also cannot cover an arbitrarily long, separately bounded provider invocation.

## Decision and scope

Keep small inline records byte-compatible. When needed, stage a complete
versioned decision snapshot through the existing authenticated immutable-part
codec, read it back, then commit its exclusive root in the existing serialized
session mutation. The root and all decisions bind the repository/PR, reviewed
transaction source/policy/configuration, generation, producer and result digest.
The session digest covers the root. Reads authenticate and validate the whole
inventory; absent or conflicting authority refuses, with no empty fallback.
Preserve every actor, role, reason, provenance, exact finding and expiry field.
Existing current-inventory and live authority fences still decide applicability.

This is a full replacement snapshot, not an unbounded replay journal. It avoids
an ever-growing root or chain. Existing partition, read, retention and decoded
byte ceilings apply, with a 4,096-entry codec ceiling. Old parts remain retained;
capacity can be exhausted before that ceiling. The ten-ID command limit and
original per-command HTTP/time allowance remain. A root activation failure
leaves old authority intact; an ambiguous response requires owned readback.
GitHub serialized commands retain the adapter's best-effort concurrent-writer
fences; they do not become a distributed CAS lock.

Review invocations explicitly own admission, provider execution and completion
phases. Admission uses one original metadata allowance. Provider execution keeps
its own existing resource/OperationHost limits. The first post-provider action,
on success or failure, opens one completion connection and allowance; repeated
completion or cleanup uses that same instance. It never refreshes an expired
phase. Fresh connections derive from retained private connection settings rather
than stripping a budget from a shared transport. Ordinary maintainer commands,
library callers and publication recovery retain their original allowance.

## Consequences and alternatives

Core serialization/disposition semantics change, so a real core release and its
npm wrapper are required, with a compatible hosted pin. Increasing embedded
limits, dropping audit fields or clearing sessions would hide the defect and
was rejected. A chained journal would require bounded projection/compaction
proofs and was not selected. Full snapshots trade extra immutable storage for
simple complete authority and bounded reads.

## Validation

Require 10+10+5 fully audited public commands, actual production broker policy
and signatures with synthetic external GitHub, preserved original dispatch
accounting, eventual one native approval and successful check, thread retry and
replay. Cover greater-than-32/256 codec inventories, corruption, foreign scope,
missing parts, legacy readers, lost responses/CAS conflicts and long-provider
actual local/hosted CLI checkpoint/cleanup. Installed artifacts and a coordinated
live canary remain release gates, not inferred from synthetic success.

## Rollout and rollback

Writer opt-in defaults off at library construction. The qualified public hosted
command composition opts in only with the compatible core reader. Old readers
must refuse the new root, never write an empty replacement. Coordinate release
and signed channel activation; do not deploy a writer against old readers.
Rollback disables new writes while retaining a compatible reader. Never delete
parts, rewrite immutable versions or run an old writer over activated records.

## Follow-up work

Explicit cumulative coverage continuation and finalization diagnostics were
delivered separately in PRs 362 and 363 and the same published package pair.
See [ADR 0107](0107-bounded-remaining-coverage.md) and the
[recovery guide](../approval-recovery.md). Component qualification and publication
do not establish a completed live approval canary.
