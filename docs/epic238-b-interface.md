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

## Remaining qualification

A's concrete immutable partition API and the reviewed compact root reader are
required for host activation, publication failure/retry tests and complete
100/250 hosted lifecycle qualification. C/D supply operation and source receipts;
F supplies process/concurrency qualification. The coordinator owns installed
native/npm/action parity and reader-first drain/rollback evidence. Retain all
active/staged authority and obligations; no cleanup or deployment is authorized.
