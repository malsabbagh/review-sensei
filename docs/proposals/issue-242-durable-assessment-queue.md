# Durable assessment queue and receipts

Status: Proposed. Interface: `assessment-queue-v1`. Date: 2026-10-10.
Decision owner: maintainer. Reviewers: Epic #238 coordinator and lanes A/B/D/F.
Issue: https://github.com/malsabbagh/review-sensei/issues/242. PR: pending.
The coordinator owns numbered ADR/schema registration and hosted/CLI call sites.
This prototype does not enable partition writers or claim deployed durability.

## Decision and scope

An explicitly selected source uses exact full B finding-instance IDs supplied
by a trusted adapter. No ambient prose, short-ID prefix, or ordinary `review
continue` command selects assessment targets. Unknown, ambiguous, foreign,
stale and resolved targets refuse on **new operation admission**. Existing
receipts are checked first; replay of a resolved target returns its original
validated result without inference. Immutable inventory generation/digest and
source snapshot, provider/model/routing policy, exact evidence, budgets and
target set bind admission. Mutable resolutions/root generation cannot create a
fresh operation. The same source under changed authority refuses instead of
receiving another allowance. A genuinely new authorized source is distinct.

`AssessmentQueue` retains all 250 possible instance IDs and resolutions. It
selects a finite window before the existing deterministic planner packs it.
Selected scope changes dispatch, never inventory. Attempted IDs rotate behind
unvisited IDs only after their charge has been persisted before dispatch.
Semantic/citation rejection is terminal for that operation and never receives
a satisfaction-fabricating retry. Missing/oversized evidence and budget-refused
work stays unvisited and pending; admission-blocked groups are skipped when
selecting a dispatch window so admissible late work is still reached. No omission
or broader discovery retires it.
Fully resolved inventories require no new inference for finalizer retries.

`AssessmentCheckpoint` serializes accepted normalized decisions and every
pending ID through host callbacks. `execute_plan(checkpoint=...)` saves admission,
charged dispatch, response/retry accounting and accepted decisions before
returning. Unknown remote dispatch outcomes remain pending without same-operation
redispatch. Queued items may use the original remaining allowance. Absolute
120-second inference deadline, expiry, actual calls and byte/retry counters
survive a new process's monotonic clock. `response_bytes_reserved` is persisted
before dispatch using the trusted enforced response ceiling. Known received
bytes atomically replace that reservation; an uncertain response retains it
without claiming its actual bytes are recoverable. Known bytes plus reservations
must fit the original aggregate output budget. Retention is six hours by default and
at most 24 hours; expiry does not reset inference admission.

`AssessmentCheckpoint.load_admission(plan, tracker, budgets, decode)` restores
authenticated original accounting before the executor renders requests. Fresh
request digests still must equal the admitted map before any activation or
dispatch. Deadline-independent bounded revalidation can reuse accepted results
after the inference deadline; a deadline-sensitive renderer instead receives a
precise deadline refusal. Bounded planner preflight still precedes this executor
seam. Legacy diagnostic `WorkRecoveryStore.load` currently requires rendered
digests, so its owner must supply the equivalent early admission seam to restore
accounting before rendering; C does not redefine diagnostic authority.

`HumanAssessmentWork.apply_to(latest)` unions revalidated decisions without
reopening concurrent resolutions. `validate_work` still checks exact authority,
snapshot, complete evidence and citations. These methods grant no approval.
Checkpoint/source readback precedes source acknowledgement; source delivery,
assessed/resolved/unprocessed counts and finalizer status remain separate.

## Persistence seams and exact proposed root

`AssessmentCheckpoint(operation_id=..., read=..., write=..., now=...)` uses
`prepare_queue(queue, pending, snapshot)` before target admission. `read` returns
the latest authenticated owned receipt or proven absence. `write` must reload
latest authority, guard the source, stage and read back A's immutable parts,
activate a generation-fenced root, and read back/reconcile ambiguous writes
before returning. A foreign/malformed latest object never becomes absence.
No diagnostic recovery key, directory or artifact setting is required; the
optional diagnostic recovery authority cannot be combined with this seam.

A owns the proposed `SessionRecord.assessment_queue` slot and parser. Proposed
canonical schema ID:
`https://reviewsensei.dev/schemas/v1/assessment-queue-root.schema.json`.
Its closed fields are:

- `schema_version`: `1.0`.
- `inventory_digest`: B's 64-hex immutable inventory-document digest.
- `inventory_generation`: immutable initial generation, 0 through 2147483647.
- `state_manifest`: A's common manifest with purpose `queue`.
- `active_operation`: null or a closed summary containing `operation_id`,
  `source_digest`, `authority_digest` (64 hex each), `execution_identity` (32
  hex), aware UTC `deadline_at`/`expires_at`, `provider_calls` (0–8),
  `transport_retries`/`structural_retries` (0–2), `prompt_bytes` (0–2 MiB),
  `response_bytes`/`response_bytes_reserved` (0–1 MiB each; sum within the original
  aggregate output budget), and `read_accounting` with `calls` (0–64) and
  absolute UTC `deadline_at_ms`.

The complete `AssessmentJournal` payload has exactly `schema_version`, `queue`
and `operations`. Queue fields are `schema_version`, `snapshot`,
`inventory_digest`, `inventory_ids`, `pending_order`, `resolved_ids`. Registry
entries have exactly `source_digest`, `operation_id`, `receipt`. The immutable
initial queue and all accepted receipts remain present. Maximum 32 retained
source operations and 2 MiB **total** canonical decoded bytes; exhausted
retention refuses new admission. No automatic GC/eviction is supplied.

Each receipt has exactly `schema_version`, `operation_id`, `plan_id`,
`execution_identity`, `resource_budget`, `request_digests`, `created_at`,
`deadline_at`, `expires_at`, `saved_at`, `elapsed_ms`, `counters`, `completed`,
`pending`, `attempted_ids`, `response_bytes_reserved`, `admitted_queue`. `completed` entries bind batch ID,
request digest and normalized result. `pending` retains exact ID/reason pairs;
`attempted_ids` distinguishes charged attempts from unvisited work.

Root allocation is at most 8192 canonical bytes including summary and manifest;
A must preflight actual available session/lifecycle reserve rather than assuming
an 8 KiB manifest fits. A's proposed codec remains 32 KiB per part, 32 parts,
1 MiB framed aggregate and 2 MiB decoded aggregate. Physical writes, staging,
readback, root reloads and source/final fences share A's original restored
64-dispatch/60-second acquisition budget with four reserved fences. Receipt
write frequency can reduce actual inference throughput below eight calls; safe
whole admission/refusal takes precedence over replenishing that budget.

Every checkpoint activation consumes a fresh one-attempt broker grant. The
original session grant cannot be reused for admission, charge, accounting and
accepted receipts. `CheckpointMutation` exposes reason (`admission`, `dispatch`,
`accounting`, `accepted`, `pending`, `replay`, `finalize`), batch ID, admitted
request digest, actual bounded dispatch digest and trusted root generation.
`AssessmentCheckpoint.on_mutation(document, context)` and its paired
`root_generation()` getter let the hosted adapter obtain a narrowly scoped
grant after checking exact source/head/session authority for each mutation.
The descriptor is not authorization. Mutating root generations are deliberately
excluded from the immutable operation receipt and cannot reset its allowance.
The callback must stage, reconcile ambiguous writes and read back the active
receipt before returning; rejection propagates with no legacy write fallback.
The write-only callback remains a local codec/test seam, not a hosted grant.

A measured 100-finding synthetic journal needs 26 receipt activations for each
eight-call operation and five for the final one-call operation. Its canonical
payload sizes are 49,156 / 81,751 / 111,594 / 128,192 bytes as pending counts
fall 68 / 36 / 4 / 0. These are not hosted storage measurements: charging every
part write/read and root/source fence against 64 physical dispatches may admit
substantially fewer calls. Real consuming-broker lifecycle/restart and the
combined journal/baseline/human/feedback/evidence/prose 256-part/8-MiB retention
and cumulative scan limits remain coordinator/F integration gates.

The existing remote generation check/PATCH is not atomic CAS. This codec cannot
guarantee concurrent writers preserve resolutions without host serialization
and stale/ambiguous-write detection. Integration must retain that limitation
and withhold on conflict. No new scheduler, storage service or credential is
introduced.

## Validation and remaining integration

Dedicated tests cover a late target after the first 32; 50 unresolved findings
visited over two fresh bounded operations; citation rejection without retry;
100 synthetic varied identities completing with pending counts 68/36/4/0;
checkpoint-before-dispatch; kill after charge/response/accepted result; conservative
unknown output reservations; absolute
deadline/expiry; resolved-target replay; concurrent resolution union and stale
writer refusal. Queue-only arithmetic for 250 gives
218/186/154/122/90/58/26/0. **That is not a full 250-finding lifecycle pass:**
pre-B main rejects this varied 250 fixture at the legacy 24 KiB marker preflight.
The B-dependent 250 test explicitly skips until admission is integrated; it then
exercises a semantic rejection, same-operation replay and eight fresh authorized
sources, retaining all nine receipts under the 32-operation ceiling.
Do not weaken its fixture or bypass the persistence boundary to advertise a pass.

Pending: A root/schema/activation adapter and shared read-budget restoration;
B complete inventory partitions; D full feedback/source guards; coordinator
hosted/CLI call sites; F real child-process/interleaving and source-ack replay
qualification. Durable scope expansion deliberately withholds additional
inference until a reviewed multi-stage receipt contract is integrated; the
existing non-queued scope path remains unchanged.

## Alternatives, rollout and rollback

Rejected alternatives: restarting sorted first-32 work, parsing targets from
prose, treating absent/edited source or diagnostic artifacts as durable authority,
refunding interrupted charges, evicting replay receipts, or silently resolving
omitted findings. The prototype preserves current eight-call/120-second bounds
and finite storage rather than promising arbitrary workloads.

Keep this proposal Proposed until explicit maintainer approval. Upgrade all
authority readers before enabling new writers. Rollback must retain compatible
queue/manifest readers and every obligation/receipt; deleting a latest root or
falling back to an older clean record cannot authorize inference or approval.
