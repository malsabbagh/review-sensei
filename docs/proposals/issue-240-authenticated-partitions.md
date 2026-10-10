# Authenticated evidence partitions (interface v1.1)

Status: Proposed. Date: 2026-10-10. Decision owner: maintainer; reviewers:
Epic #238 coordinator and lanes B–F. Issue: https://github.com/malsabbagh/review-sensei/issues/240.
PR: pending. This isolated implementation is a prototype for review. No ADR
acceptance or production reader/writer rollout is implied. The coordinator owns
numbered ADR registration and architecture/index changes.

## Context and decision

Inline zlib evidence in ADR 0072 preserves small legacy records but varied
100/250 baselines exceed the actual root allocation. Keep inline encoding when
it fits. Opt-in writers stage immutable bounded parts in the existing local
ledger or GitHub issue comments. The authenticated session root alone activates
an ordered manifest. Content digests authenticate neither a producer nor a PR.

The closed manifest schema is `evidence-manifest.schema.json`, encoding
`partitioned-json-v1`. Binding carries repository/repository_id/PR/base/head,
policy and configuration digests, immutable review/inventory generation,
producer, purpose (baseline/human-inventory/feedback/evidence/queue), and document
schema version 1.0. Mutable receipt/session generation can advance without
rehashing original immutable evidence. Each ordered ref has opaque storage_id,
canonical envelope bytes, SHA256. The manifest binds decoded bytes, item count,
complete inventory SHA256, and conservative framed aggregate bytes. No ref is
interpreted as a path, URL or arbitrary network location.

Parts split a complete compressed canonical JSON stream. Each closed part binds
its index/count, root binding digest, inventory digest and base64 segment. Decode
only after ordered owner-checked readback of every part; enforce exact output
length, stream EOF, no trailing data, canonical JSON and full inventory digest.
Readers reject malformed/missing/foreign/unsupported latest authority and never
select an older clean record or initialize an empty session.

## Public seams and consumers

- `EvidenceReadBudget(clock=monotonic)`: shared 64-dispatch/60-second absolute
  budget; metadata enumeration, staging, owner checks, part reads, root reloads
  and final fences share one object. `consume(fence=True)` may consume the four
  calls reserved for finalization; no resolver resets the budget.
- `stage_evidence(identity, *, binding, document, item_count,
  max_manifest_bytes) -> manifest`: preflight complete bytes/count/root growth;
  write immutable parts; read all back through the common strict codec. No root
  activation. Hosted staging runs only inside a grant-authorized `replace`
  callback, so one logical attempt does not replay/consume its broker grant
  independently for each part.
- `read_evidence(identity, manifest, *, expected_binding) -> document`: require
  the caller's authenticated context and exact adapter owner/association.
- `baseline_from_history_document(value, *, reader=None)`: legacy/plain/inline
  readers preserved; a manifest requires an explicit trusted resolver.
- `read_session_baseline(ledger, record)`: verify immutable root association and
  reconstruct the entire baseline without changing record bytes/digest.
- `checkpoint_review_analysis`: opt-in adapter writers stage inside the existing
  conditional `replace` callback after transaction/generation validation, then
  validate complete evidence again before activating the root.

Coordinator patches required: CLI prior baseline reads (3854/3955), baseline
capacity preflight (4237) and verification planner capacity; hosted application
baseline read (537); hosted observed verification read (84); configure one shared
budget on every enabled hosted authority reader. Conversation/CLI and other
consumer edits stay coordinator-owned. Admission-context artifacts carry the
complete decoded baseline bounded by 2 MiB, independent of inline root capacity.
No public analysis/request/output profile is silently expanded.

## Finite bounds, ownership and retention

Prototype codec limits: 32 KiB framed part, 512-byte framing reserve charged per
part, 32 parts, 1 MiB framed aggregate, 2 MiB decoded aggregate, 8 KiB manifest.
Actual supported write count is lower when shared metadata/revalidation budgets
or actual root lifecycle allocation require it; preflight refuses before writes.
The shared operation budget reserves final fences. Legal codec sizes are not a
promise that every issue's metadata enumeration admits that many parts.

Per-PR retained object ceiling: 256 parts / 8 MiB. No automatic cleanup. Staged
orphans are nonauthoritative, content-identical retries reuse existing objects,
and full metadata scans must prove complete before writing. Exhausted retention
or scan budgets fail closed. Active evidence is never deleted. Local temporary
crash debris remains explicit unreadable retained state requiring investigation.
Local authentication uses the same operator-protected ledger-root boundary as
the session record, not a new signature or credential. Reject symlink/path
escape. GitHub requires configured Bot slug plus exact numeric author ID and
repository/PR issue_url for root and every part; human or foreign objects fail.

Preflight manifest with maximum-width opaque IDs against
`checkpoint_baseline_capacity(record)`, which reserves transaction/counter/
ownership changes and three progress entries while retaining existing
attestations/dispositions. Later mutation validates the complete root again.

## Activation, consequences, validation and rollback

Stage/readback precedes root activation under existing generation/reload checks.
GitHub PATCH is not atomic CAS, and local storage assumes one writer per identity:
no cross-process lock guarantee is introduced. Stale/ambiguous writes surface
conflict or unreadable authority; shared deployment serialization is still
required. Faults leave old authority or a fully referenced validated new root;
partial staged writes never represent complete approval evidence.

Alternatives: retain inline compression only (insufficient measured capacity),
increase one comment/root cap (breaks reader/transport/lifecycle limits), or add
external object service/signing keys (outside authorized infrastructure scope).

Validation uses varied 12/21/50/100/250 inventories; escaped/deep Unicode paths,
500 covered paths, all lifecycle growth; reorder/duplicate/missing/foreign
producer/snapshot/corruption/decompression cases; faults at every stage and
before/after activation; local and GitHub restart. Independent integrated human
queue/publication and installed/deployed qualification remain Epic acceptance,
not conclusions from codec unit tests.

Reader-first rollout: install upgraded readers on every authority path before
explicitly enabling new-format writers. Historical readers must reject richer
latest roots; never roll back required readers, delete parts/roots or reinterpret
unreadable state as empty. Keep ADR 0072's historical format support and record
reader compatibility evidence separately. Follow-up: coordinator consumer patches,
reviewed receipt/queue primitive for C, F concurrency/process-fault qualification,
architecture/ADR registration and exact source/artifact/deployed reader gates.

Budget clarification (v1.1): writes are charged dispatches despite the historical
`EvidenceReadBudget` name. The constructor's 60s absolute activation deadline is
stricter than the provider's independent 120s profile; callers start acquisition
at a deliberate phase, and long inference can make activation unavailable. A
resolver never restarts that deadline. `snapshot()` emits authenticated-receipt
fields schema_version/calls/deadline_unix_ms; constructor `snapshot=...` restores
original consumed calls and derives a process-local monotonic deadline from the
original absolute UTC wall deadline. Expired or future-expanded snapshots
refuse. Read-only invocations may get new budgets; resumed queue execution must
provide original durable accounting. Item count is a generic <=4096 ceiling;
baseline reconstruction verifies exact finding count and its existing <=512
contract. B/C/D/E readers must validate their own exact domain inventory counts.
