# Explicit human review of unsupported binary files

The library adapter and supported hosted command can record an authorized person's review of selected
binary changes that ReviewSensei cannot assess. AI coverage continues to say
`unsupported` with reason `binary`; the separate report says `human-reviewed`.
A file extension, model answer, pasted marker, URL, or general reassurance does
not select or clear a file.

The live caller treats the blanket approval sentence and a finding override as
commands. Hosted composition reconstructs the current canonical retained result
after broker authorization. The ordinary finalizer still withholds a partial AI
result. Deployment requires separate release receipts.

The current CLI does not construct independent OpenRouter qualification
authority, so media approval for that provider remains withheld. A persisted
`qualified` string never replaces current authority. The qualified synthetic
positive command fixtures use a non-OpenRouter provider.

## Host configuration and invocation

`HumanFileReviewPolicy()` defaults to `allow_confirmations=False`. A trusted
operator must explicitly pass `HumanFileReviewPolicy(allow_confirmations=True)`
to `review_sensei_github.human_file_review.HumanFileReviewPublisher`.
Neither repository prose nor model output controls this policy.

The host supplies its existing GitHub HTTP transport, trusted API URL,
repository name and numeric ID, PR number, exact App login and numeric user ID,
a scoped token, and the original `before_request` IO guard. Every physical
GET/POST is charged before dispatch; the guard returns a positive remaining
absolute timeout at most 60 seconds. An exhausted guard refuses. There is no
per-phase budget reset, implicit retry, or default unlimited transport. Request,
confirmation and later evaluation may be separate explicit human invocations,
possibly days apart; each invocation retains its one original budget. Hosted
composition also accounts for broker-internal transports, canonical retained
reads and the terminal check: 60 ordinary dispatches and four reserved
finalization calls inside the original 60-second deadline.

The host starts with the validated `ReviewResult` and its actual numeric owned
COMMENT review ID, then calls:

```python
# Invocation 1: initial bot request, with its original event IO guard.
request_comment_id, request = request_publisher.request_review(
    review_id=owned_review_id, result=result
)
# Invocation 2: an explicitly routed human reply, with its original event guard.
receipt_id = confirmation_publisher.confirm(
    request_comment_id=request_comment_id,
    source_comment_id=human_issue_comment_id,
    result=result,
)
# Invocation 3: a separate explicit read-only verification request.
assessment = evaluation_publisher.evaluate(
    request_comment_id=request_comment_id,
    result=result,
    has_open_review_threads=current_host_thread_state,
)
```

These methods independently authenticate the live repository/PR, latest numeric
App-owned review and exact result, complete changed-file inventory, immutable
base/head Git trees, source comment and current collaborator permission. Raw
`FileReviewRequest`/`FileReviewReceipt` documents are reference data, not grants.
Partitioned eligibility-v4 roots refuse; referenced eligibility uses the existing
trusted host reader.

The names above denote separately authorized invocations, not three phases that
may refill a guard. The compound `approve_reviewed_media` path reuses immutable
exact-commit inventory while retaining fresh mutable fences. Request creation
and compound approval use 31 physical dispatches in the flat two- and 41-file
adapter fixtures. Whole hosted profiles additionally include retention, broker,
current approval facts and final check; adapter counts are not deployment proof.

## Bot request and human reply

The request lists every unsupported binary change and every generated file
excluded by policy (`excluded-by-policy`, reason `generated`) with its exact
path, change kind, previous path for renames, old/new blob SHAs, regular file
modes, full file ID, and request digest. Only regular Git blobs (modes
`100644` or `100755`) are eligible. Symlinks, submodules, missing/truncated
tree evidence, unsupported text, other policy exclusions, incomplete file
enumeration and unreviewed hunks do not gain a human exemption. Deleted files
bind the old blob; new files bind the new blob; renames bind both paths and
blobs.

A maintainer or admin replies with this exact sentence to approve every file
in the current request:

```text
@reviewsensei I reviewed the media files and I approve
```

`@sensei` is also accepted. Extra prose is refused. The same person can still
name a subset with a whole literal command:

```text
@sensei media-reviewed <64-character-request-digest> <64-character-file-id> [<64-character-file-id> ...]
```

The digest and IDs must come from that exact request. No `all`, short IDs,
wildcards, URLs, code fences, inferred filenames or additional prose are
accepted. To confirm another subset, use a new comment. The blanket sentence
requires GitHub permission `maintain` or `admin`. Write access can still
confirm a named subset. A maintainer or admin can also record one finding as
acceptable risk, for example `@reviewsensei override RS-ABCDEF acceptable risk`
or `@reviewsensei RS-ABCDEF is acceptable risk`. Up to 10 IDs can be named in the same comment, separated by spaces or commas. That records the existing
accept-risk disposition on the GitHub session comment. It is not a verified fix.
One source comment cannot be edited into a new confirmation or rebound to a new
snapshot. The source must be a live issue comment on the same PR, with exact
numeric human `User` identity, `OWNER`/`MEMBER`/`COLLABORATOR` association and live
permission matching its login and numeric ID. A named subset accepts `write`,
`maintain`, or `admin`. The blanket sentence accepts only `maintain` or `admin`.
Inline
comments are not accepted by this initial command.

The App records a metadata-only receipt containing the request and source IDs,
source update time and full body SHA256, numeric reviewer identity/login, and
exact selected file IDs. It does not send media or human confirmation text to a
model, fetch blob contents, or persist the raw reply in another artifact. GitHub
already holds the original human comment. A receipt is counted only after exact
owned readback and fresh source/snapshot fences. Serial replay reuses an exact
receipt; an unknown POST response has no automatic retry and a later explicit
invocation can reconcile the existing owned receipt.

## Invalidation, coverage and approval

There is **no automatic carry across any base/head, result, owned review or file
inventory change**, even if a selected blob is byte-identical. A changed context
can change how a human should interpret a file. Generate a fresh request and use
a fresh human comment. File IDs also bind old/new blob IDs, modes and rename
paths. Source edits/deletions/moves, numeric actor replacement, association
change or permission revocation invalidate the corresponding human coverage.
Receipts remain visible for audit; they are not deleted or silently rewritten.
A different fresh authorized comment can renew the same selected files.
Missing or corrupt audit/root evidence refuses rather than assuming review.

Partial selections keep the remaining binary changes pending. Confirmations
never resolve text findings, human adjudication, learning obligations, failed
stages, incomplete document/source context, qualification or other approval
checks. The service computes an optional `coverage_only_partial` provenance
field only after all reviewable text stages complete and the remaining coverage
is exclusively unsupported binary files or generated files excluded by policy.
The complete owned review digest binds this field. New
provenance uses the explicit `reviewsensei:review-content:human-files:v1` content
digest projection; absent provenance preserves the historic result digest. An
old transaction reader cannot silently verify and drop the new approval input.

With every binary change freshly confirmed and this host-derived provenance,
`evaluate` can compute a combined coverage decision while preserving every
other frozen/current approval fact. It leaves the original AI result, baseline,
cache, eligibility marker and partial coverage unchanged and does not post
APPROVE or a passing check. `publish_mixed_approval` is the separate host call
that posts one exact-head APPROVE after that decision passes. A returned
assessment is a fresh observation. The ordinary finalizer still withholds the
partial review. The supported hosted media command composes these operations
after reconstructing current retained authority. Remote reads
and writes are not an atomic GitHub CAS; concurrent duplicate receipts refuse
evaluation.

## Finite limits and validation

A v1 request contains at most 32 changes and 32 KiB canonical JSON. Versioned
groups bind ordered v1 parts to one snapshot/review/result and the complete
inventory. The group retains the 32 KiB document cap; 41 files use parts of 32
and nine. Grouped receipts bind the same genuine source and exact root/part
digests. Missing parts, duplicate paths/IDs and incomplete union coverage refuse.
Named selections still admit at most 32 literal IDs. A human
command is at most 8 KiB strict UTF-8. Published bodies are at most 64 KiB; a
physical response is at most 512 KiB; each bounded list requires EOF within ten
100-item pages. These are refusal bounds, not a promise that every 32-file
lifecycle fits a host budget. Literal canonical Unicode/glob filenames retain
exact equality and are escaped for display.

Public schemas are `human-file-review.schema.json`,
`human-file-receipt.schema.json` and their `-group` counterparts. Runtime constructors additionally enforce
canonical paths, exact change/blob relationships, real timestamps and byte
bounds. Tests use synthetic HTTP only, retain original budgets, exercise full
and selected coverage, replay/invalidation and permission/content fences, and
leave AI coverage partial. See [ADR 0077](adr/0077-explicit-human-file-review.md)
for rollout and rollback.

## Mixed coverage decision

`review_sensei.mixed_coverage.evaluate_mixed_coverage` is a pure function. It
reports `coverage_only_partial` only when completed text review and the complete
binary inventory show that unsupported regular binaries are the entire remaining
coverage cause. Valid current confirmations can then permit an approval
decision object while the AI result status stays `partial`. The function does
not call GitHub, does not write APPROVE, and does not mark AI status, baseline,
or cache complete.

Mandatory or document context loss, incomplete source context, an unknown file,
a non-binary unsupported file, a failed stage, incomplete provider output,
persistence or capacity failure, missing qualification, an unresolved finding or
thread, permission revocation, a stale or expired receipt, contradictory reason
metadata, and an old schema withhold or refuse. Policy off leaves the binary
blocker in place. Replay of the same confirmation event returns the same receipt
identity.

`approve_reviewed_media` confirms and evaluates in one bounded composition,
then posts only after fresh source, request, latest root and full PR fences.
It admits the native write/readback tail before POST. Unknown responses and
exhausted allowances remain failures; an explicit retry of the original command
reconciles owned publications and repeats current authorization. No human
assessment is repeated or invocation budget silently renewed. See
[ADR 0103](adr/0103-bounded-hosted-media-confirmation.md).

Nested media reads prove the shared directory from complete shallow Git trees
and then read its bounded recursive subtree. Oversized or truncated evidence
refuses. If a request and genuine receipt fit but approval readback does not,
the invocation fails before native approval and preserves both audit records.
An explicit retry of the original command rechecks current authorization and
reuses those records; it does not ask for another visual assessment.
