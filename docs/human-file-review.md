# Explicit human review of unsupported binary files

The opt-in library adapter can record an authorized person's review of selected
binary changes that ReviewSensei cannot assess. AI coverage continues to say
`unsupported` with reason `binary`; the separate report says `human-reviewed`.
A file extension, model answer, pasted marker, URL, or general reassurance does
not select or clear a file.

This is a local library feature awaiting review and hosted integration. The
released CLI, mention workflows and ordinary approval finalizer do not route
`media-reviewed` yet. There is no new YAML option, automatic approval, broker
capability, release, or workflow activation in this change.

## Host configuration and invocation

`HumanFileReviewPolicy()` defaults to `allow_confirmations=False`. A trusted
operator must explicitly pass `HumanFileReviewPolicy(allow_confirmations=True)`
to `review_sensei.hosting.github.human_file_review.HumanFileReviewPublisher`.
Neither repository prose nor model output controls this policy.

The host supplies its existing GitHub HTTP transport, trusted API URL,
repository name and numeric ID, PR number, exact App login and numeric user ID,
a scoped token, and the original `before_request` IO guard. Every physical
GET/POST is charged before dispatch; the guard returns a positive remaining
absolute timeout at most 60 seconds. An exhausted guard refuses. There is no
per-phase budget reset, implicit retry, or default unlimited transport. Request,
confirmation and later evaluation are separate explicit human invocations,
possibly days apart; each invocation retains its one original budget. Any future
public factory must also account for its broker/internal transports and prove
its complete resource profile. This adapter does not qualify a whole hosted
64-dispatch/60-second operation.

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
Partitioned eligibility-v4 roots refuse in this initial adapter.

The names above denote separately authorized invocations, not three phases that
may refill a guard. If a host performs confirmation and evaluation in one
invocation, both must share that invocation's original guard and may refuse when
their combined cost exceeds it. The two-file synthetic fixture measures 18,
43 and 33 physical attempts respectively; it does not qualify a combined
confirmation-plus-approval handler or promise useful 32-file hosted support.

## Bot request and human reply

The request lists every unsupported binary change with its exact path, change
kind, previous path for renames, old/new blob SHAs, regular file modes, full file
ID, and request digest. Only regular Git blobs (modes `100644` or `100755`) are
eligible. Symlinks, submodules, missing/truncated tree evidence, unsupported text,
policy-excluded files, incomplete file enumeration and unreviewed hunks do not
gain a human exemption. Deleted files bind the old blob; new files bind the new
blob; renames bind both paths and blobs.

An authorized person replies with a whole literal command and selected full IDs:

```text
@sensei media-reviewed <64-character-request-digest> <64-character-file-id> [<64-character-file-id> ...]
```

`@reviewsensei` is also accepted. The digest and IDs must come from that exact
request. No `all`, short IDs, wildcards, URLs, code fences, inferred filenames or
additional prose are accepted. To confirm another subset, use a new comment.
One source comment cannot be edited into a new confirmation or rebound to a new
snapshot. The source must be a live issue comment on the same PR, with exact
numeric human `User` identity, `OWNER`/`MEMBER`/`COLLABORATOR` association and live
`write`/`maintain`/`admin` permission matching its login and numeric ID. Inline
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
is exclusively binary. The complete owned review digest binds this field. New
provenance uses the explicit `reviewsensei:review-content:human-files:v1` content
digest projection; absent provenance preserves the historic result digest. An
old transaction reader cannot silently verify and drop the new approval input.

With every binary change freshly confirmed and this host-derived provenance,
`evaluate` can compute a combined coverage decision while preserving every
other frozen/current approval fact. It leaves the original AI result, baseline,
cache, eligibility marker and partial coverage unchanged and never posts
APPROVE or a passing check. A returned assessment is a fresh observation, not a
durable authorization. The ordinary finalizer still withholds the partial
review. Before automatic publication is enabled, a reviewed host must compose
this decision with fresh finalizer guards, broker authorization, durable
receipts and the complete resource/concurrency profile. Remote reads and writes
are not an atomic GitHub CAS; concurrent duplicate receipts refuse evaluation.

## Finite limits and validation

A request contains at most 32 binary changes and 32 KiB canonical JSON. A human
command is at most 8 KiB strict UTF-8. Published bodies are at most 64 KiB; a
physical response is at most 512 KiB; each bounded list requires EOF within ten
100-item pages. These are refusal bounds, not a promise that every 32-file
lifecycle fits a host budget. Literal canonical Unicode/glob filenames retain
exact equality and are escaped for display.

Public schemas are `human-file-review.schema.json` and
`human-file-receipt.schema.json`. Runtime constructors additionally enforce
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

Public APPROVE integration is still pending. The shared finalizer is unchanged
until a later change is authorized to call this function.
