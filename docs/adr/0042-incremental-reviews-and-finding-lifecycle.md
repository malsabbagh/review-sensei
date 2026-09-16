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

`ReviewService` may accept an optional `IncrementalReviewPlan` and an
in-memory `ReviewContextCache`. Cache keys bind repository, pull request,
base SHA, head SHA, engine, model, profile, and stage/context/learning
digests. Changed base, learnings, model, or configuration invalidate matching
entries. Missing, stale, incomplete related context, or incompatible state
falls back to a full bounded review. The cache stores only small metadata:
never raw prompts or provider responses, and no hosted cache is required.

Publication emits an explicit `coverage_mode` on the review marker. Inline
findings use a v2 marker that carries the fingerprint. Already published
ReviewSensei fingerprints are not posted again. Human comments and
resolutions are ignored. Concurrent older generations cannot replace newer
lifecycle state. Approvals, write permissions, egress, and trusted context
are unchanged.

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

## Validation and rollback

Add fingerprint, omission-is-not-fixed, cache invalidation, incremental skip,
fallback-full, and publication duplicate-skip tests. Roll back by omitting
`IncrementalReviewPlan`, ignoring v2 markers, and reverting additive v1 JSON
fields; no hosted data migration is required.

## Follow-up

Evaluation under #33 should later measure saved provider usage, duplicate
comment reduction, and recall against full reviews on the same change
sequences. This ADR does not enable that measurement.
