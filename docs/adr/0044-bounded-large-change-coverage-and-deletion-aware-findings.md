# ADR 0044 - Bounded large-change coverage and deletion-aware findings

Status: Proposed
Date: 2026-09-15
GitHub Issue: #39
Pull Request: draft PR to be linked
Owners/Reviewers: Maintainers
Approved by: not configured

## Context

The bounded review contract caps per-request diff, prompt, and result size.
The default stage targets added and modified lines, and the GitHub publisher
emits right-side inline comments. Large changes, deletions, binary/generated
files, and cross-file relationships were therefore either rejected at
preflight or reviewed without an honest account of what was not covered.

Raising those per-request ceilings or silently truncating a diff would hide
unreviewed work and can look like a complete review.

## Decision

Keep engine per-request `ReviewLimits` intact. Add a separate `TotalWorkBudget`
and an explicit coverage manifest so every enumerated changed file and hunk
receives one of `reviewed`, `partially-reviewed`, `excluded-by-policy`,
`unsupported`, or `budget-exhausted`. Incomplete enumeration marks the whole
plan incomplete and can never be fully reviewed.

Finding locations are additive in the v1 comment contract: omitted `side`
remains a right-side new-file line; `LEFT` binds to a deleted old-file line;
`FILE` is a path-level concern with no line. The publisher validates those
locations against the exact reviewed snapshot. An inline location that GitHub
cannot represent is retained as a file-level comment or a structured summary
section rather than dropped as clean.

Chunk orchestration is opt-in (`orchestrate_large_changes` / `--orchestrate-large-changes`).
It partitions supported work deterministically into chunks that each obey
per-request limits, retains related changed-path names as context, and stops
when the total-work provider-call or chunk budget is exhausted. Chunk failure
keeps already-validated findings and records an explicit partial outcome.
Partial, incomplete, or unknown coverage blocks App approval.

## Scope

In scope:

- Coverage reporting for every single-request review
- Deletion-aware left-side and file-level locations
- Opt-in bounded chunk orchestration with total-work budgets
- Compatibility for legacy right-side comments and results without coverage

Out of scope:

- Raising per-request diff/prompt/result ceilings
- Expanding write permissions, egress, trusted context, or automatic approval
- Hosted cache/state for incremental reviews (#38)
- Model-based evidence verification (#37)

## Consequences

Positive:

- Callers can see exactly which files were reviewed, excluded, or left uncovered
- Deleted lines and file-level concerns can be published without dropping them
- Large changes can be reviewed in bounded chunks without unbounded provider calls

Tradeoffs:

- Results that omit coverage cannot be auto-approved
- Opt-in orchestration can use more provider calls than a single request, bounded by the total-work budget
- Generated-file policy is an explicit suffix/name/prefix list, not a learned classifier

## Alternatives considered

### Raise per-request diff ceilings

Rejected because it weakens the bounded review contract and still cannot
describe binary, generated, or leftover work.

### Silently truncate to the first fitting files

Rejected because truncation would look like a complete review of the change.

### Require a new v2 result schema for locations

Rejected for now. Additive optional `side`/`coverage` fields preserve existing
right-side consumers; a later major version can require them.

## Validation

Unit tests cover coverage outcomes, deterministic chunk order, binary/generated/
rename/deletion behavior, left/right/file publication, summary fallback,
legacy results without coverage, chunk failure with retained findings, provider
call budgets, approval blockers, and chunked-vs-baseline recall/usage.

## Rollout and rollback

Coverage is always emitted by `ReviewService`. Orchestration remains off unless
requested. Roll back by reverting the service/publisher adapters; legacy
right-side comments continue to parse.

## Follow-up work

Coordinate coverage identities with incremental invalidation (#38) and evidence
verification (#37). Evaluation of live-model recall across chunk boundaries
remains an #33 promotion concern.
