# ADR 0036 - Learning lifecycle diagnostics and opt-in feedback

Status: Proposed
Date: 2026-09-16
GitHub Issue: #41
Pull Request: draft PR to be linked
Owners/Reviewers: Maintainers
Approved by: not applicable

## Context

Approved repository learnings already load from the trusted target/base
checkout and stay separate from unmerged proposals. Maintainers still need
bounded tools for ownership, provenance, review dates, supersession, staleness,
and conflicts, plus a way to measure usefulness without creating an
automatically trusted memory store.

## Decision

Keep merged JSON files as the only review-time knowledge. Extend `LearningEntry`
with optional bounded lifecycle metadata (`owner`, `provenance`, `reviewed_at`,
`expires_at`, `supersedes`, `superseded_by`) that existing files may omit. A
deterministic diagnostic report surfaces stale, conflicting, missing-superseder,
and supersession-cycle signals for a human decision. Diagnostics never delete,
rewrite, or retire approved entries.

Opt-in finding feedback is a separate v1 `learning-feedback` document. Outcomes
are `useful`, `incorrect`, `obsolete`, and `unverified`. Absence of feedback is
not approval. Feedback records are evaluation/maintainer evidence only; they
cannot enter `ReviewRequest.learnings` or expand review scope.

Fixture evaluation may compare the same cases with and without selected active
learnings and report precision/recall/false-positive deltas as estimates, not
causal proof. Cache keys already bind a learning digest;
`LearningStore.selection_digest` is the canonical SHA-256 of the review-time
selection, so edits to entries that can reach a review invalidate incremental
state while edits to retired or superseded entries do not.

Rollback is ordinary Git history: revert or unmerge a learning file and the
next trusted-base load uses the remaining active entries.

## Alternatives considered

### Automatically retire stale or conflicting rules

Rejected because expiry and model opinions must not silently rewrite approved
knowledge.

### Count missing feedback as useful

Rejected because silence is not a maintainer decision.

### Host a mandatory knowledge database

Rejected; it would separate ownership, review, and rollback from the reviewed
repository.

## Consequences

Positive:

- Maintainers can inspect lifecycle problems without mutating trusted context.
- Evaluation can measure learning effect on a synthetic corpus without claiming
  production causality.
- Existing learning files continue to load under a documented default policy.

Tradeoffs:

- Conflict detection remains advisory where glob intersection is incomplete.
- Fixture canned responses often yield zero deltas unless a case's findings
  actually change with the prompt.

## Validation and rollback

Add schema, load, diagnostic, feedback, cache-digest, CLI, and with/without
evaluation tests. Roll back by reverting this change; no hosted data migration
is required. Git restore of learning files remains the policy rollback path.
