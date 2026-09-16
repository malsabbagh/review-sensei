# ADR 0042 - Incremental reviews and stable finding lifecycle identities

Status: Proposed
Date: 2026-09-15
GitHub Issue: #38
Pull Request: draft PR to be linked
Owners/Reviewers: Maintainers
Approved by: not applicable

## Context

Exact `(path, line, body)` deduplication and one App review per pull-request
head stop same-head noise, but they do not identify the same concern after a
move, a rebase, or a rephrased comment. A later provider pass that simply omits
a finding also cannot be treated as proof that the defect is gone. Incremental
re-review of changed and related files needs an explicit coverage mode, a
bounded optional cache, and fail-closed fallback to a full review.

## Decision

Keep the existing first-wins exact-comment contract and per-head review
identity. Add a separate stable fingerprint from validated path, symbol,
defect kind, and optional evidence id—not from line number or exact prose
alone. Reconcile findings into `new`, `still-present`, `fixed`, `outdated`,
and `uncertain`. Omission on a later pass is `uncertain` unless independent
evidence confirms the same concern is gone, or the path was not in this pass's
reviewed set, in which case the finding stays `still-present`.

Defect kind is part of that identity and `category` is not. `category` is a
presentation lens, so reclassifying the same defect must not fan one concern
out into a second thread. A comment without `defect_kind` buckets under the
canonical `unknown` kind, so identity does not depend on whether the provider
filled the field. Two comments on the same path and symbol that declare
different defect kinds are therefore distinct concerns, and one that declares
no defect kind shares identity with other unclassified comments on that
symbol.

A concern re-reported under a different fingerprint—a moved symbol or a
refined defect kind—keeps one live record under the current pass's identity.
A record carried over from an earlier pass keeps the newer of its own
generation and the current one, so a late pass cannot downgrade lifecycle
state it did not observe.

`ReviewService` may accept an optional `IncrementalReviewPlan` and an
in-memory `ReviewContextCache`. Cache keys bind repository, pull request,
base SHA, head SHA, engine, model, profile, and stage/context/learning
digests. Changed base, learnings, model, or configuration invalidate matching
entries on every run that can name a cache identity, not only on incremental
runs. Missing, stale, incomplete related context, a missing caller-supplied
changed-path list, or incompatible state falls back to a full bounded review.
Prior findings stay in scope for a fallback only when this run verified the
prior key is compatible; an incompatible or unverifiable key discards them.
The cache stores only small metadata: never raw prompts or provider
responses, and no hosted cache is required. Only a validated complete pass
becomes the authoritative cache record, so a partial or incomplete aggregate
cannot let a later incremental run treat its lifecycle set as verified.

A skipped incremental pass makes no provider call and therefore charges none
to the resource budget. Because no provider ran, its summary is engine-authored
rather than provider output: it states that no changed path remained since the
last accepted review, carries no findings, and is bounded by the same summary
limits. A skip does not approve anything on its own—the shared finalizer
remains the sole approval writer and still refuses to approve while unresolved
blocking ReviewSensei roots exist on the pull request.

Publication emits an explicit `coverage_mode` on the review marker. Inline
findings use a v2 marker that carries the fingerprint. Already published
ReviewSensei fingerprints are not posted again. Human comments and
resolutions are ignored. Concurrent older generations cannot replace newer
lifecycle state. Approvals, write permissions, egress, and trusted context
are unchanged.

Duplicate suppression is a property of publication, not of incremental mode:
whenever a review has at least one inline finding to publish, publication
reads existing App-authored finding markers through one bounded GraphQL
review-thread sweep before the write, including on plain `full` reviews. A
full re-review of a pull request is the common case that would otherwise
duplicate threads, so the sweep is not gated on `coverage_mode`. It adds one
GraphQL request to the publication path, and any pagination or transport
uncertainty fails closed rather than publishing a possible duplicate.

## Alternatives considered

### Treat a missing finding on a later pass as fixed

Rejected because model omission is not evidence. Fixed requires a confirmed
matching concern.

### Mandate a hosted review cache

Rejected because the product is open-source-first. Missing cache state must
fall back to a full review.

### Key identity on exact line and comment body

Rejected because moved code and rephrased findings would duplicate
discussion, while the existing exact-comment contract already covers
same-head duplicates.

## Consequences

Positive:

- Moved or rephrased concerns keep one ReviewSensei discussion.
- Incremental passes can skip unchanged work while retaining related paths.
- Coverage mode is explicit, so a partial pass cannot be mistaken for a full
  clean review.

Tradeoffs:

- Distinct defects need symbol or defect-kind identity; path-only comments
  may share a fingerprint.
- Cross-head fingerprint recovery depends on App-authored v2 markers already
  present on the pull request.
- Every publication carrying inline findings performs one additional bounded
  GraphQL review-thread sweep.

## Validation and rollback

Add fingerprint, omission-is-not-fixed, cache invalidation, incremental skip,
fallback-full, and publication duplicate-skip tests. Roll back by omitting
`IncrementalReviewPlan`, ignoring v2 markers, and reverting additive v1 JSON
fields; no hosted data migration is required.

## Follow-up

Evaluation under #33 should later measure saved provider usage, duplicate
comment reduction, and recall against full reviews on the same change
sequences. This ADR does not enable that measurement.
