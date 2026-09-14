# ADR 0027 - App approvals for clean pull-request reviews

Status: Superseded
Date: 2026-09-02
GitHub Issue: #96
Pull Request: #98
Owners/Reviewers: Maintainers
Approved by: not applicable

This historical record describes the original approval rollout. The current
publisher applies the clean-review approval policy by default.

## Context

ReviewSensei publishes validated pull-request reviews through one exact-head,
repository-bound, fail-closed path. A clean result should satisfy a
branch-protection approval requirement while findings, unresolved threads,
forks, drafts, stale targets, and App-authored pull requests remain comments.

## Decision

The shared `ReviewPublisher` path emits `APPROVE` when the validated result
contains no blocking findings, the bounded GitHub review-thread sweep is fully
resolved, and the target is eligible for an App-authored review. Otherwise it
emits `COMMENT`. Marker reconciliation, exact-head checks, and conversation
reply behavior are unchanged.

## Scope

- Keep approval and comment writes behind the same validation and broker
  capability.
- Keep Python setup generation and Cloudflare Worker setup output identical.
- Preserve migration recognition for supported historical setup artifacts.
- Keep `@sensei` replies on conversation endpoints; reply content cannot cause
  an approval.

## Consequences

Positive:

- A clean validated review can satisfy branch-protection approval requirements.
- One publisher path provides consistent preflight, idempotency, and failure
  handling for both review outcomes.

Tradeoffs:

- Approval authority is exercised whenever all deterministic gates pass.
- An existing same-head marker remains idempotent; a clean rerun may promote a
  prior same-head comment after the thread sweep succeeds.
- Hosted acceptance still requires the released workflow, package, Worker,
  setup migration, and branch-protection configuration to be current.

## Alternatives Considered

### Separate approval publisher

Rejected because it would duplicate preflight, reconciliation, and error
handling without adding a safety property.

### Require a caller switch

Rejected because it allowed a successful clean review to remain a comment when
the repository's policy required an approval.

## Implementation Notes

- `ReviewPublisher.publish` evaluates the validated result, App-authored state,
  and the bounded thread sweep immediately before posting.
- A clean rerun can promote a same-head `COMMENTED` marker to `APPROVED`; an
  existing `APPROVED` marker remains a no-op.
- The existing `pull_requests: write` capability is used for the review POST.

## Validation And Rollout

Deterministic publisher tests assert exact `APPROVE` and `COMMENT` payloads for
clean, blocking, unresolved-thread, App-authored, stale, fork, and duplicate
cases. Hosted rollout remains an operator-owned evidence gate.

## Links

- Related issue: #96
- Related PR: #98
- Superseded by: ADR 0030 (approval criteria refinement)
