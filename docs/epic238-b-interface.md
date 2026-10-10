# Issue 241 inventory and publication interface

Status: proposed integration contract under [Epic 238](https://github.com/malsabbagh/review-sensei/issues/238).
This draft implements owned domain and presentation seams. It does not enable
partition writers, claim deployed support, or mark an ADR Accepted. The
coordinator owns the shared authority schema, ADR and call-site integration.

## Identity and retained authority

`finding_instance_fingerprint(ReviewComment) -> str` is instance-v1:
SHA-256 of `reviewsensei:finding-instance:v1:` followed by canonical UTF-8 JSON
of `{path, line, side, body}`. FILE explicitly has `line: null`; RIGHT is explicit.
Bytes are not clipped or normalized. This is the existing exact-duplicate key.
Optional annotations and provider/stage order cannot rename an explanation.
Distinct paths, lines, sides or bodies remain distinct even if their lifecycle
concern fingerprint agrees. Lifecycle identity remains unchanged and separate.

Display prefixes extend deterministically on collisions. An irreducible prefix
collision refuses projection. Assessment decisions use complete 64-hex identities;
prefixes do not authorize resolution. Newly published inline roots carry separate
instance metadata beside the existing concern/classification marker. A legacy
concern/location alone cannot prove an exact duplicate, so publication retains
the explanation. A prior lifecycle state cannot hide a body explanation.

Legacy inventory reads preserve their stored identities and resolutions. There
is no historical rehash, fabricated location, ledger reset or clean fallback.
Legacy serialization remains limited to 64 paths and 24 KiB with every possible
resolution reserved before publication. An oversized new inventory is refused
before remote mutations until compatible partition readers/writers are wired.

## Immutable complete inventory and compact resolution

`PendingHumanReview.inventory_document()` returns immutable schema-3 evidence
with base SHA, complete findings, exact required paths, original instance metadata,
effective classification and separate concern fingerprints. Legacy findings have
unknown metadata explicitly represented as null. `inventory_digest` binds this
whole document. Stable display identity does not imply identical authority when
classification, metadata or required paths change.

`PendingHumanReview.from_inventory_document(document)` validates domain content
only. The host must first prove A's ordered parts, exact closed binding, ownership,
counts, digest, lengths, snapshot and generation. This function grants neither
authentication nor eligibility. Legacy `from_dict` retains the old piece bounds.

`HumanInventoryResolution(inventory_digest, resolved=())` serializes compact
schema-1 resolution state. `restore(inventory)` rejects a foreign digest, unknown
identity or loss of an existing resolution. Trusted source/decision/operation
receipts remain independent requirements; a digest is not actor authentication.

Domain admission is at most 250 findings, eight evidence paths per finding,
2,000 distinct paths and 2 MiB canonical complete evidence including the full
all-resolved reserve. These are not host, transport or prompt support claims.

## Complete visible prose preparation

`prepare_finding_prose_parts(comments, head_sha=...)` deterministically packs
whole full explanations and location references, preserving exact-instance
duplicate semantics and authoritative required/optional classification.
Each piece is at most 65,536 UTF-8 bytes and 64 unique paths; the finite aggregate
is at most 32 pieces and 1 MiB of rendered prose. Every header is preflighted
using the longest supported part-count framing. One explanation that cannot
fit is refused, never clipped. Parts contain no eligibility or root authority.

`verify_finding_prose_readbacks(parts, observed, producer_id, head_sha)` checks
complete ordered numeric host IDs, exact bodies/digests/bytes, App producer,
snapshot and piece bounds. It returns receipts for the coordinator's compact
root. The trusted host must additionally bind repository, PR, base, result,
generation and placement and repeat readback/fences before delayed approval.

Use existing COMMENT-review publication for visible parts. All inventory/prose
readbacks must finish before a compact latest root becomes eligible or the check
concludes. Missing or ambiguous writes, edits, unknown latest roots, ownership
changes and stale snapshots withhold. Serialized writers, a shared 64-read/60s
operation budget, root growth and retained parts are integration prerequisites.
No root reader or partition adapter writer is enabled by this draft.

## Measured synthetic profiles

The independent varied fixture uses distinct causes, symbols and evidence digests,
with 100/250 distinct paths. It is synthetic source evidence, not a hosted run.

| Findings | Prose observations per finding | Complete inventory bytes | All-resolved state bytes | Visible pieces | Total visible bytes |
| --- | --- | --- | --- | --- | --- |
| 100 | 1 | 105,725 | 6,821 | 2 | 70,662 |
| 100 | 3 | 217,325 | 6,821 | 3 | 182,077 |
| 250 | 1 | 264,897 | 16,871 | 4 | 177,014 |
| 250 | 3 | 544,409 | 16,871 | 8 | 456,386 |

Additional synthetic regressions preserve mixed Unicode/escaping, complete
resolution growth, 20 resolved findings plus a new shared-concern explanation,
64/65 paths, literal Git-path glob characters and exact framed byte boundaries.
The observed SiteVault eight-location pattern is synthetic normalized input;
no unavailable provider metadata or underlying SiteVault defect is inferred.
Provider narrative appears separately after host counts and next action. Partial
coverage continues to withhold independently of optional findings.

## Dependent nonactivating prototype

The separate `codex/epic238-b-partition-prototype` branch incorporates frozen A
`ba89418d21898d14e6d1e535a5409c47c7cff3b2` and D
`3749b14d5df9b017785e5ea9a18e38db5020ca51`. The initial independent identity/prose
commit remains `647a2db3a17e0e01e7c3017e5cecde9cc7a1ec10` in draft #251.

`HumanAssessmentService.reply/_request/_parse` and `validate_assessment_evidence`
accept optional `feedback: FeedbackSelection`. Complete selected bodies enter
JSON reference data through D's full prompt preflight, including instructions,
pending findings, current diff and framing. A resolved citation must occur wholly
within one original selected source. Generated metadata and adjacent source
concatenation cannot provide that evidence. Base/head and available context PR
identity are checked; explicit selected targets must contain the current batch.
The authenticated caller/C still binds repository and the entire ordered target
tuple and feedback digest. Legacy single-source admission remains 4 KiB.

`human_inventory.stage_human_inventory/read_human_inventory` wrap A's strict
codec with purpose `human-inventory`, exact trusted binding, actual manifest
allocation, complete finding count and full inventory digest validation.
Callbacks are **uncharged physical transports** taking remaining seconds; these
wrappers charge each write/read on the caller's shared budget. Do not pass an
already charged ledger adapter into these callbacks. Ledger stage/read adapters
instead use A's existing conditional mutation and accounting directly.

`CommentReviewProseStore.stage(parts, head_sha)` uses the caller's existing review
publication capability to POST only COMMENT reviews, then GET each numeric review
ID. Exact configured Bot login and numeric account, PR URL, head, COMMENTED state,
full UTF-8 body and ordered instances/placements are verified. The finite complete
scan uses one review per response to bound escaping under the REST ceiling. It
shares the same 64-dispatch/60s budget; a busy historical PR can therefore refuse
before mutation. Retention is at most 256 prose reviews/8 MiB, with no deletion.
Identical staged reviews are reused. Ambiguous writes and partial readback raise
`ProseStagingError` with known IDs; retry reconciliation still proves all bodies.

`VisibleProseReceipt` is `visible-prose-v1`; its strict document includes placement
`review-body`, index, numeric review and producer IDs, head, body hash/bytes and
complete instance/path lists. `revalidate` performs fresh complete owner/body
reads. A receipt alone grants no eligibility, and this store is not called by
the current ReviewPublisher or finalizer. Every part rejects authority markers.

Synthetic detail-3 profiles stage/reconstruct inventory and COMMENT prose, then
freshly revalidate both under one budget: 100 findings use three inventory parts,
1,150 manifest bytes, 11,010 visible receipt document bytes and 19 dispatches;
250 use six inventory parts, 1,667 manifest bytes, 27,824 receipt document bytes
and 43 dispatches. Four final fences remain reserved. Whole visible receipts
require bounded partitioning or actual root allocation; these measurements do
not imply that embedding receipts directly in a root fits. Fault tests cover
each POST/readback, lost response reconciliation, metadata ambiguity, edited or
foreign bodies/owners/PR/head/placement, changed inventory parts, restored budgets,
and actual manifest allocation refusal. No activation root is written.

## Remaining qualification

The discrete reader amendment implements the conditionally reviewed v4 proposal:
complete result-bound inventory and visible receipt envelopes, exact independently
authenticated PartitionedEligibilityContext, actual owned numeric root/App
identity, closed manifests, original shared budget and mandatory complete fresh
COMMENT readback. Legacy readers and raw schema-3 stage seams remain unchanged.
Total framed root/all-resolution/future allocation is <=32 KiB and actual caller
allocation, including maximum activation counter width. Synthetic detail-3
100/250 v4 profiles use 22/46 dispatches before four fences, three/six inventory
parts plus one visible receipt part, and 12,441/26,533 all-resolved marker bytes
before outer framing/lifecycle reserves. Reader parsing is positive; finalization
of v4 objects explicitly withholds until the remaining activation gates pass.

Finalizer review scans use existing adaptive 100/50/25/5/1 pagination at the same
offset with 64/60 maximum and original caller budget. Twenty synthetic legal
33,774-byte roots now load in eight reads. Exact EOF and late malformed/newer
owned authority remain fail-closed; 1,000-item, one-object transport and shared
deadline/call exhaustion remain refusals. The unpublished-check exemption is
unchanged pending separate explicit policy disposition.

A's concrete immutable partition API is incorporated in the dependent prototype.
The reviewed compact latest-root reader and coordinator call sites are still
required for activation and complete 100/250 hosted lifecycle qualification.
C/D supply operation and authenticated source receipts;
F supplies process/concurrency qualification. The coordinator owns installed
native/npm/action parity and reader-first drain/rollback evidence. Retain all
active/staged authority and obligations; no cleanup or deployment is authorized.
