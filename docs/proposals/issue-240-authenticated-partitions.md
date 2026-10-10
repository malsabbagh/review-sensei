# Authenticated evidence partitions (interface v1.1)

Status: Proposed. Date: 2026-10-10. Decision owner: maintainer; reviewers:
Epic #238 coordinator and lanes B–F. Issue: https://github.com/malsabbagh/review-sensei/issues/240.
PR: https://github.com/malsabbagh/review-sensei/pull/250. This isolated implementation is a prototype for review. No ADR
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


## Queue-slot amendment v1 (separate from codec v1.1)

The initial prototype codec remains immutable at
`ba89418d21898d14e6d1e535a5409c47c7cff3b2`. This amendment adds
`SessionRecord.assessment_queue` and the closed
`https://reviewsensei.dev/schemas/v1/assessment-queue-root.schema.json`.
Root fields are schema_version 1.0, inventory_digest, immutable
inventory_generation, state_manifest (purpose queue), and active_operation.
The active summary binds operation/source/authority digests, execution identity,
ordered aware UTC deadline/expiry, bounded call/retry/prompt/output counters,
and original durable read_accounting calls/deadline_at_ms. C confirmed the exact
response_bytes_reserved field: persist the enforced output limit before dispatch,
convert it to measured response_bytes only with durable response capture, retain
unknown reservations without refund or redispatch. Runtime enforces measured plus
reserved <= the prototype 1 MiB aggregate ceiling; C additionally validates each
original admitted budget and journal/receipt binding. JSON Schema expresses scalar
bounds; runtime supplies cross-field arithmetic, exact dates and root allocation.

`assessment_queue_manifest_capacity(record) -> int` allocates manifest bytes
inside the total 8192-byte root including a 1024-byte summary growth reserve,
beside retained baseline/history, dispositions/attestations and maximum lifecycle
counter/transaction/ownership growth. Allocation is recomputed before activation;
new baseline checkpoints reserve the whole queue root. The root parser checks
actual canonical root/summary bytes, immutable generation and session association.
`read_session_assessment_queue(ledger, record) -> object | None` reconstructs the
complete authenticated document and checks document schema version; C owns closed
journal parsing, complete inventory count, all receipts, and source/tombstone
semantics. Both adapters validate all queue references before activation/load.
An existing slot cannot be removed through replace; retired state must retain an
explicit authenticated tombstone. Writers remain disabled by default.

C's current journal limit is 32 retained source operations under the unchanged
2 MiB total decoded ceiling. This is not 32 fresh storage budgets: every staged
baseline/queue/feedback/evidence object and orphan shares the per-PR 256-object /
8 MiB retention envelope and scan/dispatch budget. No automatic cleanup exists.
A full old/new history can refuse a later checkpoint even when individual codec
objects fit. Hosted object fixtures demonstrate storage boundaries, not permission
to issue repeated root writes. Each broker-bound replace consumes its one-attempt
grant; a staging callback covers only that mutation's parts. The coordinator must
compose authenticated fresh grants per permitted mutation or a reviewed transaction
protocol, and qualify the actual consuming broker before host acceptance. Current
conditional reload/PATCH remains non-atomic; single-writer deployment serialization
and ambiguity reconciliation remain required. No broker validation is weakened.


## Local accounting amendment v1

Local authority I/O uses the original shared EvidenceReadBudget for bounded root,
part and enrollment reads; bounded containment inspections; retention enumeration
and each enumerated entry stat; durable enrollment/immutable/temp-file writes;
explicit expired-witness/root retirement; and final root activation/readback.
One durable write dispatch includes directory creation, mandatory fsync and
unconditional temporary cleanup. This is an adapter dispatch unit rather than an
OS syscall counter, matching the HTTP adapter's complete request unit. Root temp
write and authority activation are separate charged dispatches. Writes/fault
cleanup charges are conservative and never refunded. Scan entries share the same
finite global allowance instead of gaining an additional 256-entry free scan.

The root is read back immediately before activation. Immediately after temp-file
fsync, another count/deadline fence precedes link/replace; an elapsed deadline
refuses activation and removes the uninstalled temp file. Expiry or conflict after
activation is ambiguous failure, never acknowledgement. Owned root digest readback
is required before replace returns. Three final fence dispatches (root reload,
activation, readback) may use the four reserved calls; the host retains the fourth
for its source/final fence. No cross-process atomic compare-and-swap is claimed.
Root/enrollment reads now request only their bounded maximum plus one byte.

A complete varied 250-finding local checkpoint fits the finite allowance; its
original acquisition allowance can refuse a subsequent queue admission. Fresh
initial queue admissions have separate authorization and no active operation to
resume. Every resumed C execution must pass restored original durable accounting,
without reset. Expired session re-enrollment also refuses an assessment queue:
unresolved work and receipts need explicit retained tombstone authority, and an
expired ledger is insufficient proof that queue obligations were satisfied. No
retirement implementation is supplied by this amendment.


Local accounting compatibility correction: explicitly supplied/restored budgets,
opt-in partition writers and observed queue/partition roots always retain the same
shared counter/deadline object. Default inline-only callers retain their previous
ledger lifetime behavior rather than inventing an evidence operation across every
legacy command. The first richer-root read is bounded and charged before authority
is returned, then all subsequent control I/O uses that budget without refill.
An expired v0.1 record with its historical implicit TTL and no assessment_queue
field may follow the existing explicit migration/re-enrollment path; invalid
explicit legacy expiry still refuses under the original migration rules; any queue-bearing expired record,
including an unsupported legacy-shaped object, refuses retirement. Fixtures cover
both cases. This exemption grants no receipt, dispatch or queue accounting reset.

## Activation-tail proposal v0 (review required, writers OFF)

The current `stage_partitioned_evidence` planning estimate `4 * parts + 16`
and local estimate `8 * parts + 16` are conservative protocol planning guards.
They are not public analysis limits, storage-byte limits or the true resource
ceiling. The genuine shared ceiling remains 64 dispatches / 60 seconds, including
metadata, writes, root validation and fences. The estimates currently refuse some
plans that actual finite dispatch accounting could fit. They are not increased
or removed in this patch.

Measured A `0ca0f8e` / C `09b7c86` one-finding successful C trace emits five
checkpoints: admission, dispatch charge, response accounting, accepted result,
final checkpoint. Replaying its complete journals through A with one original
budget refuses before accepted persistence on GitHub (44 calls, stage planning
requires 24 while only 16 ordinary calls remain), and before response-accounting
persistence locally (52 calls, requires 24 with 8 ordinary calls remaining).
A proposed three-checkpoint replay combines response accounting and acceptance.
With a pre-enrolled root it persists all three GitHub mutations in 36 calls;
local settlement refuses at 43 because planning requires 24 with 17 ordinary
calls remaining. These are storage lower bounds using synthetic object transport;
source/evidence/provider calls, consuming broker grants, final delivery/ack,
active accounting summaries and integrated C callback semantics remain unqualified.
They do not demonstrate a successful production one-call lifecycle.

An actual callback snapshot undercounts its activation tail: seven dispatches
locally; five on first GitHub queue activation and six on subsequent ones in the
minimal replay without broker head checks. Persisting those preactivation actual
counters as complete original accounting permits restart to refill spent calls.
Current host composition must refuse that inference; default writers stay OFF.

Proposed finite seam, not an implemented or accepted interface:

1. `plan_activation_tail(before, draft, *, max_scan_pages)` computes bounded
   ordinary/fence dispatch counts from exact old/new referenced part inventories
   and configured head checks. It binds immutable operation/source/authority/
   execution identity, original deadline, expected current root digest/generation,
   and the final root/ref inventory. Full scans exceeding the declared page bound
   refuse before another dispatch; a partial scan never proves current authority.
2. `reserve_tail(plan)` conservatively debits the original budget before creating
   the durable root summary. The caller serializes the charged count, including
   the nonrefundable reservation, then seals the ticket to the resulting root
   digest. An adapter-only prepaid ticket consumes the reserved tail without
   charging twice, never refunds unused capacity, and refuses dispatch/deadline,
   root/ref/page/head changes beyond its sealed bounds. No ordinary resolver can
   use an outstanding activation ticket or replenish its counter.
3. For the current local I/O units, tail bound is `3 * new_part_count + 4`:
   all complete part reads/inspections, root reload, temporary write, activation
   and root readback. For GitHub it is
   `2 * new_part_count + old_part_count + 2 * max_scan_pages + head_reads + 1`
   under the current validation/reload/PATCH/readback sequence. The counts include
   every baseline and queue reference, and hosted head_reads is one when required.
   Metadata/other root growth exceeding the sealed bound is refusal, not free work.

A committed tail summary alone cannot prove that no later failed activation
attempt spent more calls than that older summary records. Crash-before-commit and
ambiguous-write restart therefore require an independently reviewed original
attempt witness or precise same-operation restart refusal. A conservative option
within existing fields is to reserve the entire remaining operation allowance in
the first successfully admitted root: durable calls becomes 64, the original
process executes only through a bounded prepaid ticket, and restart has no new
inference or acknowledgement allowance. This sacrifices automatic continuation
rather than refunding interrupted control work. Failed initial admission also
needs source/one-attempt grant replay refusal; a new automatic grant cannot erase
it. Fresh operator authorization must be separately scoped and preserve old
receipts/obligations. That permission/witness composition belongs to the host
review (R11); no new infrastructure or credential is proposed. The first admitted
root and every later transition still need exact accounted ticket tests, original
source replay tests, ambiguity/kill tests and final-source-fence accounting before
host acceptance. No tail protocol or release acceptance is conveyed here.

## Literal path and unsupported resolver amendment

Actual baseline finding/reviewed/related inventory paths permit canonical literal
Git glob characters and quoted Unicode filenames. They retain exact identity;
configured directory/pattern validators keep their existing rules. Traversal,
absolute/drive paths, repeated separators, backslashes, controls and non-NFC text
remain rejected. Plain, inline and partition readers round trip the complete
inventory including those filenames. Public session reader helpers now refuse a
missing/noncallable trusted producer/resolver or missing shared budget with
sanitized ReviewInputError, allowing consumers to hand off unreadable authority.
No manifest is interpreted as empty state or resolved through an arbitrary path.

### Live-only prepaid activation amendment (prototype, writers off)

The adapter-owned opt-in primitive requires an explicitly supplied original
EvidenceReadBudget and implements the prepaid tail; the public
64-dispatch/60-second ceiling is unchanged. `EvidenceReadBudget.reserve_tail`
debits the complete ordered `ActivationTailPlan` before the root accounting is
sealed. `EvidenceTailTicket` is one-use, cannot be restored, and admits only its
sealed root/context and exact labeled dispatches. Actual dispatches do not debit
the prepaid liability again. Unused optional pages, refused activation,
ambiguous writes, expired deadlines and failed readbacks are never refunded.
Retries are not admitted by these plans: a failed transport burns the attempt.
The ordinary four-call fence reserve remains in force; tail admission simulates
ordinary/fence admission in the exact planned order, rather than adding four
free calls to the cap.

Both adapters expose `reserve_for_tail(identity, *, operation_binding, slot,
reservation_id, expected_generation, head_sha=None, now=None)` (GitHub also
requires `max_scan_pages`). It uses the existing reservation mechanism, requires
new owned durable reservation readback, and retains proof in the original
ledger object and original budget. `operation_binding` is closed over
`operation_id`, `source_digest`, `authority_digest`, `execution_identity`,
`inventory_digest`, and immutable `inventory_generation`. Loaded, duplicate,
restored-budget, new-ledger (even sharing the budget), and previously failed
attempts cannot authorize `replace_with_tail`. The typed failure is
`NonResumableActivationError`, reason `original-attempt-witness-required`.
This live proof is not durable authenticated original-attempt accounting.
Classifying an invocation as a new operation, including failures before the
reservation root exists, requires the host's existing authenticated attempt
witness. The primitive does not qualify crash/restart completion or enable any
consumer writer. Completed roots remain readable using an independent bounded
read-only invocation after the original execution deadline.

`replace_with_tail(identity, prepare, *, operation_binding,
attempt_reservation_id, seal_accounting, now=None)` also requires
`max_scan_pages` on GitHub. Preparation stages and reads back complete pieces
under the original budget. It uses an adapter-specific exact staging preflight,
not the codec's legacy conservative `4 * parts + 16` planning guard. Retained
piece scans, metadata pages, write/head checks and staging readbacks remain
charged. After preparation, the adapter fixes current-root digest, ordered
complete new/old manifest references, draft shape, original absolute UTC
deadline, operation binding, and (GitHub) root ID, head and current grant digest.
`seal_accounting(draft, {calls, deadline_at_ms}) -> SessionRecord` may change only
`active_operation.read_accounting` and the record digest. Complete journal
bytes, inventory generation, reservation, manifest, record generation and all
other lifecycle fields must remain identical. The accounting carrier and
reservation remain nonnull through acknowledgment; the host owns lifecycle
projection and journal semantics.

The local tail is `3 * Nnew + 4`: complete new-piece containment/directory/read
operations, current-root reload fence, durable temporary-root write (including
sync/cleanup), deadline-checked activation fence, and owned root readback fence.
The GitHub tail is `2 * Nnew + Nold + 2 * S + 2`, where `S` is the declared finite
metadata page maximum: complete new-piece validation; current-root discovery
and old-piece validation; one live-head GET; one PATCH fence; owned discovery
and complete new-piece readback. Both scans reserve all `S` pages; page one is a
fence and later pages are ordinary optional liabilities that remain charged
when unused. A full last page cannot establish termination and refuses without
an extra request. Staging refuses before POST when future root/piece retention
cannot fit the declared scan bound. Every referenced baseline and queue piece
is counted, including duplicate references; there is no per-layer allowance.
No cleanup, active-reference deletion, cross-process lock or atomic GitHub CAS
is introduced.

GitHub exposes `bind_tail_grant(identity, BrokerSessionGrant, *,
operation_binding, attempt_reservation_id, max_scan_pages, now=None)` on the same
live ledger. It requires the actual `BrokerClient`, its exact closed v1 parsed
attestation, unchanged original command/run/head/workflow claims (only issuance
time may differ), current owned reservation/root and live head. These reads are
charged to the original budget before installation. Grant reuse is refused;
the next checkpoint consumes fresh broker verification before writes. Issuance
and broker-internal dispatches are host composition work and must also be
charged; the adapter does not provide a free grant allowance. V1 grant claims
do not authenticate operation/read-accounting restart proof. The coordinator's
v2 metadata-only feedback binding and separate original-attempt witness remain
pending composition, with v1 compatibility retained here.

The executable hosted fixture uses the actual BrokerClient parser and a service
that consumes each grant once. In a pre-enrolled PR with no baseline, one queue
piece and a minimal synthetic complete journal, original issuance/reservation
costs 8 dispatches; fresh issuance/binding plus checkpoint 1 reaches 24;
checkpoint 2 reaches 44; checkpoint 3 preparation reaches 57 and refuses the
seven-dispatch tail because its ordinary work would consume the fence reserve.
All 57 dispatches equal 49 GitHub attempts plus 8 fixture broker attempts. No
source/evidence/provider/finalization/publication/acknowledgment work is present
in that lower-bound fixture. This is a precise useful-route refusal, not a
successful C operation. F independently measured production cold acquisition
plus consume at 12 physical attempts and warm at 11 including internal work;
those costs cannot be substituted with the fixture's two outer broker requests.
The existing host three-checkpoint measurement of 41 storage attempts plus
production grants already exceeds 64 before source/ack work. Combined admission
remains a release gate; no ceiling increase or hidden exclusions are accepted.

### Parsed v2 feedback-grant compatibility amendment

This separate adapter amendment uses only the coordinator's frozen
`broker_client.py` and `feedback_attestation.py` dependency at
`0d8bfabf0ce68cd812cd4ed007e32edc3b63b354`. No Worker, ADR index, consumer,
release or queue-journal implementation is imported. V1 grant checks remain
unchanged. A live attempt cannot change grant versions during rebinding.

For parsed version2/operation `feedback`, rebinding preserves the complete
original feedback/source selection, numeric actor identity, role, repository,
PR, head, run and workflow claims. Only issuance time and the closed mutation
object may change. The mutation must match all six immutable operation binding
fields and the original reservation ID; its root digest/generation must equal
the current authenticated owned root. Its original absolute UTC accounting
deadline must equal the retained live budget, and calls must not decrease below
either the previous grant or the completed root's prepaid liability, nor exceed
the actual retained live budget. Legitimately changing reason/request/dispatch
references are left to the host checkpoint composition, whose exact grant
request and complete journal projection remain its responsibility. These
reference checks run during original reservation admission, before installing
a fresh grant, and after consumed verification/current-root discovery before
checkpoint preparation. Caller accounting is never restored or adopted as
original-attempt proof.

### Direct owned history association amendment (prototype, default off)

`enable_history_graph=False` remains the default on both adapters. The opt-in
reader uses C's immutable `assessment-history-index-v1` parser at
`c425739851989bda81649eefe2d917dd70b522fc`. Only `assessment_queue.py`,
`assessment_history_index.py` and its exact synthetic fixtures are selected as
dependencies. The Python feedback parser separately selects coordinator
`5e799c0b13902be22d66c720213e9e76d6d1cd57`: primitive JSON string
`admission-dispatch` requires both exact request and dispatch digests. Neither
dependency enables a route, coalescing, source acknowledgement or writers.

The concrete owned API is:

```python
associate_assessment_history(
    identity, *, expected_binding, expected_root_sha256,
    expected_generation, max_scan_pages, now=None,
) -> HistoryAssociation
read_associated_history(
    proof, *, source_digest, operation_id, now=None,
) -> object  # exact decoded old journal, never a recursive resolver
```

`max_scan_pages` is mandatory only for the hosted adapter (1..60); the local
adapter takes no remote scan scope. The caller supplies the full trusted
immutable queue binding and exact current owned root digest/generation. The
adapter loads the complete current envelope under the original shared budget,
uses the strict C parser, checks the actual complete inventory count (1..250),
snapshot and inventory digest, and verifies a current PR head on GitHub even
when no mutation broker is configured. Local snapshot trust remains the
caller's immutable Git context. Each root read, metadata page, head request,
physical part inspection and part read is charged. No helper creates or restores
a new operation allowance. Reader fences retain the existing four reserved
activation calls; they do not gain additional free calls.

The opaque proof is registered on the same ledger and budget object. It binds
the owned root digest/generation, the canonical full binding and complete ordered
direct source/reference/original-accounting inventory digest. Its sealed bytes
cannot be replaced, transferred to another ledger or reused with another budget.
The cache and proof registries each have a 2 MiB aggregate byte ceiling and 64
entry ceiling. `host_state()` exposes a snapshot with no attempt witness; like
C's `QueueHostState`, the snapshot does not authenticate itself or prove restart.

Lookup selects only an exact source/operation member of that current inventory.
Before and after the one child fetch it reloads the complete current root and
checks the current head. The child must have the canonical adapter ID, exact
configured Bot integer author and repository/PR association on GitHub, exact
framed size/hash, immutable binding hash, a single part, and bounded inflation
(32 KiB framed / 2 MiB decoded). It must contain that source's exact inline
original receipt and identical immutable inventory; C still owns receipt
validation. An older packet's index is reference data only. No API associates a
raw supplied journal, follows its references, grants arbitrary ID access, or
claims that unfetched objects were authenticated.

All direct child IDs enter the activation plan scope and prepaid dispatch
inventory. The complete manifest plus baseline and direct-child cardinality and
declared framed bytes must fit the shared 256-part / 8 MiB retention allowance;
actual staging scans also include every orphan and visible retained object.
Root allocation and future summary/lifecycle growth retain their existing
actual-record preflight. A prepaid activation validates every direct child,
including children unrelated to the selected replay source. No dispatch is
refunded or charged again. A missing, foreign, unidentified, substituted or
omitted reference refuses activation. Existing archived source/operation,
reference, tombstone and original accounting remain exact; archival of a prior
current receipt requires that exact prior owned single-part manifest. Ordinary
`replace` refuses graph mutation. Legacy-to-index migration and a first indexed
root containing invented archived accounting explicitly refuse without a future
reviewed complete original-source proof. This amendment supplies no GC.

Measured small read-only fixture: complete root with two direct children,
lookup of the second child, including both root/head fences, costs **10 hosted
physical requests** (`3 * (scan + current part + head) + child`) or **15 local
I/O units** (`3 * (root + 3 current-part units) + 3 child-part units`). The
unselected first child may be missing without causing that sparse lookup to
follow it; full activation refuses its absence. The activation-reader fixture
precharges and consumes all **9 local part units** for current + two children,
with no refund on a missing child. These are storage-reader measurements, not
provider/source/broker/acknowledgement lifecycle measurements.

The retained 194-piece C profile is not qualified for whole hosted execution.
At five objects per metadata page, repeated complete scans and full child
validation can exceed the genuine shared 64-call/60-second ceiling before root
activation. The boundary fixture supplies 198 visible retained objects, allows
exactly 39 pages, then refuses without a 40th request or an omitted inventory.
Local retained-entry scans are likewise charged; no larger profile is admitted
by silently skipping scans, source fences, head checks or original proof.
The earlier actual production-broker cost and whole-operation refusal remain
release gates. Live-only mutation proof, explicit restart refusal, residual
non-atomic GitHub races and all writer-off qualifications remain unchanged.

The new dependency can charge every physical BrokerClient dispatch through
`before_request`. The adapter accepts that hook only when it is the bound
`consume` method of this exact original EvidenceReadBudget; then the client
charges once and clamps its timeout without an additional adapter debit.
Absent a hook, the legacy adapter debit remains. A hook with a fresh budget or
unreviewed wrapper refuses before verification. This does not account for
Worker-internal requests by assumption: production acquisition/internal-call
composition and its previously measured12/11 cost remain a release gate.

The selected parser additionally carries the one-line integer trigger-ID
strictness correction from coordinator
`22dbfc481ee8cd7de69c549295ff7c59d0970f95`; broker_client remains byte-identical
to0d8bfab. A fractional trigger ID that compares equal to an integer cannot
reach authority issuance. No other coordinator parity files are imported.
