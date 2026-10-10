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

## Owned feedback and checkpoint-count amendment

The initial prototype remains immutable at
`09b7c86f85f55c9a7a99e411f2a4905b776c4ccb`, draft PR255. This amendment
preserves the `assessment-queue-v1` wire and adds optional `feedback` to
`reassess`/`validate_work`. The value must be the complete frozen D
`FeedbackSelection` matching repository/PR/base/head. Its full digest also
binds plan authority; its explicit target tuple must match the dispatch target
tuple. Full selected bodies enter B's reviewed request, parse, scope/cached
validation and evidence-citation seams. The unused legacy source body is not a
citation source or a substitute for clipping. Before those dependencies are
present, the optional path refuses rather than shortening evidence.

For a successful batch, known response accounting and validated decisions now
activate together. The original pre-dispatch response reservation remains
durable until that activation; death before it retains unknown output and never
redispatches the uncertain attempt. A terminal semantic rejection saves measured
bytes plus pending obligations. Structural/transport retries keep original
bounded counter/charge checkpoints. An identical final execution/counter state
does not need a duplicate activation. One successful batch needs exactly three
C activations: admission, charged reservation, accepted result plus accounting.
The accepted receipt includes all scoped-out pending IDs before returning.

The coordinator's closed A envelope is exactly `{schema_version: "1.0",
kind: "assessment-queue-state", journal: <complete C v1 journal>}`. The host
unwraps after authentication, checks complete inventory count against A
`item_count`, immutable root digest/binding and closed C parsing. The journal
lookup key binds immutable trigger kind/numeric ID, repository/PR/initial
inventory, excluding edited body/timestamp, selection order/targets and mutable
generations. Full D selection/targets remain in operation binding; changed
authority on the same trigger refuses replenishment.

Measurements on immutable A `0ca0f8e82e6f94e026cfcb027b43ff684ef00a54` with
the actual C read/mutation callbacks, one four-finding batch and shared64/60s
allowance are **storage lower bounds**, not a complete hosted operation:

| Phase | GitHub fixture consumed / persisted | Local consumed / persisted |
|---|---:|---:|
| Enrollment | 3 / — | 9 / — |
| Two original-absence reads | 5 / — | 11 / — |
| Admission | 15 / 10 | 26 / 19 |
| Charged dispatch | 28 / 22 | 45 / 38 |
| Accepted activation | 41 / 35 | 54 / refused |

Local accepted staging refuses its whole-inventory preflight without durability.
GitHub fixture reaches accepted durability but omits consuming-broker grants,
source authorization/guards, real provider time, acknowledgement and other
baseline/human/feedback/evidence/prose roots. The measured activation-tail gap
of six GitHub/seven local dispatches between stored accounting and actual
consumption is an independent restart-accounting blocker. No allowance is
increased/refilled, and hosted writers remain off pending qualification.

A separate hypothetical one-call-per-authorized-source 250-finding domain run
with an initial semantic rejection/replay retains32 receipts,126 pending IDs
and about1,724,154 canonical decoded bytes. Source33 refuses before inference,
preserving the journal. Even without rejection,250/4 requires63 authorized
sources and exceeds the32-slot registry. The2MiB decoded limit has not been
raised, and combined256-part/8MiB retention remains unqualified. The frozen B/D
dependency profile exercises full feedback and the original eight-call 250
domain fixture; those passes do not qualify hosted throughput or durable
activation-tail accounting. Diagnostic early admission remains unchanged.

## Explicit factored history prototype

`FactoredAssessmentJournal` uses the new closed wire `assessment-history-v2`;
legacy `AssessmentJournal` and `assessment-queue-v1` receipts/queue remain
unchanged. `from_legacy(legacy)` reconstructs and retains every original receipt
and source binding. `from_document`, `to_document`, `receipt(source_digest,
operation_id)` and `record(queue,source_digest,receipt)` preserve existing C
semantics. No eviction or expiration mints admission. The journal source key
binds D's stable authenticated event kind/numeric ID/repository/PR, scoped by
the host to initial inventory digest/generation. Full D content/actors/timestamp/
targets bind the operation ID. An edit of the same event refuses changed
authority rather than generating a new source allowance.

The wire has exactly `schema_version`, complete `queue`, unique `strings` and
`operations`. Strings are interned once in first-encounter order under sorted
object keys. Each operation is a lossless typed tree: an integer references the
string table, `{"integer": n}` represents an original integer, arrays represent
original arrays, `{"fields": {...}}` represents an original object, and booleans
and null retain their native meaning. Other numbers/types, unknown references,
duplicate/unused strings, extra fields, alternative encoding, noncanonical
tables and excessive nesting refuse. Reencoding must equal the exact input.
The full accepted/pending/attempted records, original timestamps/budgets/
reservations, and full text are reconstructed without clipping or reordering.

Admission bounds are independent: at most32 active operations,256 total retained
operations,16,384 pooled strings,32 tree depth,2MiB actual decoded factored JSON,
2MiB reconstructed single record and8MiB total reconstructed records. Logical
reconstruction sizes are charged while traversing references, before allocating
a large serialized expansion. Active means a receipt still has a response
reservation or neither exhausts its original call limit nor completes every
admitted batch. Classifying a receipt as settled does not erase its original
limits or supply authority. The256 history ceiling accommodates the measured64
source profile and is not a provider/root/control-budget increase. Every old
source remains exactly queryable; source257 refuses finite capacity.

The genuine frozen-B engine runs use one enforced provider call per fresh source
and one initial semantic rejection.100 findings complete across26 sources;
250 complete across64. Same-source rejection replay makes zero provider calls;
edited authority and expired original receipts refuse without inference. Source33
continues without deleting earlier source authority. Representative measured
final decoded/reconstructed bytes are99,094/541,714 for100 and
372,162/3,028,681 for250, well inside the unchanged decoded ceiling. Timing fields
vary slightly across runs. These are domain qualification, not hosted success.

Whole-document compression still couples all retained history to every mutation.
A's actual partition codec, including closed host envelope and framing, measures
100/26sources at78 queue-only retained parts/about1.1MiB;250/64sources at300 parts/
about6.58MiB. The first256-part pressure appears at source56's accepted transition
(258 candidate parts). A complete activation must preflight this before storage
writes and preserve the previously authenticated root/receipts. Other authority
artifacts reduce available retention further. Thus this version fixes32-slot/
decoded-history refusal but does not qualify whole-host250 retention. A binary/
base64 SHA pool probe reduced decoded bytes but increased compressed bytes
(36,514 to43,949 on a representative final journal), so that representation is
not adopted merely to claim fewer parts.

Reviewable continuation alternatives are domain-specific lossless factoring of
admitted queues/pending reason runs, or immutable settled-operation segments plus
a complete authenticated inventory/reference root and bounded exact source index.
Segments must keep all old receipts/tombstones bound to that root and require
authenticated exact same-source lookup before new budgets mint; they cannot GC
old source authority or impose an unbounded history scan. Merely raising the
retention/control caps or dropping undispatched admission bindings is rejected.

## Closed host adapter prototype

`AssessmentQueueHostAdapter(queue,binding,source_digest,operation_id,
attempt_reservation_id,read,activate)` is opt-in and does not authenticate itself.
The host supplies exact immutable A binding and its authenticated original
attempt reservation, never a randomly minted replay ID. It exposes
`checkpoint(now,retention_seconds)` for the existing reassessment entry point.

`read() -> QueueHostState(envelope,root_generation,binding,item_count,
attempt_witness)` is the latest authenticated source/head/root-fenced read.
Proven absence is envelope=None with item_count=0; otherwise the manifest count
must equal the entire immutable inventory count, not selected targets. The closed
envelope remains exactly `{schema_version:'1.0',kind:'assessment-queue-state',
journal:<v1 or v2 document>}`. The adapter validates shape,2MiB envelope limit,
immutable binding, snapshot/inventory identity/count, monotonic generation and
original source-operation lookup. Legacy import is explicit and lossless.

`activate(QueueHostMutation) -> QueueHostState` receives the complete envelope,
expected_generation, exact binding/count, stable source_digest, operation_id,
original attempt_reservation_id, typed CheckpointMutation and request_identity.
The latter hashes the exact C mutation envelope/binding/source/operation/attempt/
context; it is not a capability or a substitute for the sealed final root digest
and original read accounting in the broker grant. Each activation needs a fresh
dedicated consuming grant bound to those actual final root/accounting/source
inputs. Source/head/session fencing, original control64/60s allowance, A's sealed
prepaid tail and ambiguous-write reconciliation remain trusted activate duties.
The exact activated envelope must read back at a later generation with the
authenticated original witness before C returns durability. No unfenced fallback
exists. Restored mutations without original-attempt proof refuse while receipt
reads remain available. The active root accounting carrier remains until ack.

The adapter tests check contract refusal and original-root preservation using
synthetic callbacks; they do not qualify the consuming broker or A's physical
tail. Whole lifecycle measurement must include every original OIDC/grant/source/
root/part/head/provider/ack dispatch and retained baseline/human/feedback/evidence/
prose artifact. Fixed planning multipliers, restored allowance replenishment,
or standalone codec constructor passes cannot qualify hosted admission. Public
writers remain off; numbered ADR/shared schemas/public call sites remain owned
by the coordinator. Diagnostic early restore and multistage durable scope
expansion are still pending.

## Indexed immutable checkpoint prototype

`assessment_history_index.py` defines opt-in `assessment-history-index-v1`.
The exact host envelope remains `{schema_version: "1.0", kind:
"assessment-queue-state", journal: <closed indexed document>}`. Public routes
and the v1/v2 host adapter do not select this prototype automatically.

The indexed document has exactly `schema_version`, `state`, and `sources`.
`state` is a complete factored immutable inventory/current queue plus zero or
one original inline operation receipt. Every other stable source remains in
`sources`, bound to its exact operation, active status, original sealed control
summary, and one exact already-written accepted checkpoint part. No source
key, receipt, human obligation, text or budget is evicted. Each reference binds
storage ID, canonical framed-part SHA-256/bytes, and decoded envelope bytes.
Old-source lookup reads that one exact authenticated child and checks its
inline source/operation/receipt and immutable inventory, plus monotonic latest
resolutions. It never traverses the child's older source references.

`history_references()` returns the complete direct source segment inventory.
A must authenticate every referenced part's transitive association with the
latest owned root and protect the complete graph before activation or orphan
cleanup. Prose references alone are insufficient. There is no graph cleanup or
public hosted graph reader here. `resolve_history_part` uses A's strict owned
part decoder and the caller's original shared allowance; it refuses missing,
altered, foreign-producer/snapshot, multipart and wrong-envelope children.

`source_read_accounting(source_digest, operation_id, authenticated_root,
expected_binding)` requires the trusted host reader's exact latest fenced
`QueueHostState`: same full indexed envelope, immutable binding, generation
shape and complete inventory count. It returns only the already sealed
original control summary. A typed result is a callback trust contract, not
cryptographic authentication; the host must supply actual ownership,
freshness, original-attempt and complete index association proofs. There is no
raw-document-only control budget accessor or default refill.

`from_retained` is an explicit lossless v1/v2 migration. The host must already
have durably segmented and authenticated every exact inline original receipt
under its original authority and accounting. The converter requires the
complete source-key set, all original sealed summaries and owned references,
then directly revalidates every child. Missing or extra sources, changed
receipts and unavailable original accounting refuse. It performs no writes
and cannot invent historical witnesses. Unmigrated v1/v2 journals remain
readable by their existing adapters.

The finite retained ceiling remains 256, separately from 32 active operations.
Individual decoded envelopes remain bounded by 2 MiB; each direct lookup
requires only the current envelope and one bounded child. A whole-history
reconstruction is not required or inferred. Physical retention remains A's
256 parts/8 MiB, each framed part at most 32 KiB; every test preflights the
entire prospective store before a write. No allowance or retention ceiling
has been increased.

Queue-only measured runs used A's actual framed codec, F's actual varied
100/250 Unicode inventory construction (three distinct observations and one
unique path per finding), complete D feedback bodies/exact target sets, one
original provider call per authorized source, an initial semantic rejection,
source33, exact rejected/accepted replay, same-source changed authority and
expiry refusal. The 250 run retained 64 source authorities (63 exact child
references plus the current inline receipt) across all transitions. One
measurement retained 194 checkpoint pieces, 4,534,182 framed bytes; the largest
current envelope was 85,746 decoded bytes/30,091 framed bytes, and the largest
current-plus-child lookup was 168,900 decoded bytes. All 63 referenced
historical envelopes totaled 4,450,885 decoded bytes. The 100 run used 26
sources/79 pieces, 836,729 framed bytes; maxima were 35,823 decoded/13,347 framed
bytes and 70,761 decoded bytes per current-plus-child lookup. Exact piece/byte
counts include replay snapshots and can vary slightly with original timing and
execution IDs; every observed transition must satisfy the unchanged bounds.

The synthetic transport charges physical part write/readback and exact old
receipt reads against the same original per-source control object (maximum
observed 12). These are **queue-only transport lower bounds**, excluding A root
scan/fences, consuming broker/OIDC, original source guards, provider transport,
acknowledgment and the other authority artifacts. No qualified joint hosted
root factory exists yet. Baseline, complete human/result/visible prose, full
feedback, evidence and control reserves remain explicitly unqualified until
the coordinator composes actual serialized artifacts through A's reviewed
graph reader and measures the complete original 64-call/60-second lifecycle.
D60's full-history source56/258-part refusal remains the existing supported
profile blocker; this opt-in primitive is not a claim that hosted250 is enabled.

### Structural profile acquisition clock

The queue-only synthetic codec/retention profile uses an explicit deterministic
acquisition clock and a fixed initial wall clock for each original control
budget. Coverage CPU is excluded from this structural measurement; it is not a
host elapsed-time or throughput qualification. Same-source replay retains the
same budget object, calls and original deadline. A negative test advances that
clock to the unchanged 60-second boundary and refuses a fresh archived receipt
read without changing the journal, counters or deadline. Original provider
execution accounting, changed-source refusal and seven-hour receipt expiry
checks remain unchanged. Production time sources and limits are unchanged.
