# ADR 0030 - Gate App approvals on resolved review threads and exact-head review safety

Status: Proposed
Date: 2026-09-07
GitHub Issue: not configured
Pull Request: 102
Owners/Reviewers: Maintainers
Approved by: not applicable

## Context

The automatic-review path currently considers the current validated result. A later clean run could approve a pull request while an earlier inline review thread remains unresolved. GitHub REST review comments do not expose resolution state, while the GraphQL reviewThreads connection does. Approval must remain exact-head bound, same-repository only, and fail closed when the thread sweep is unavailable or incomplete.

## Decision Drivers

- Never approve while an actionable inline finding or review thread remains open.
- Reuse the existing exact-head, repository, fork, draft, author, marker, and
  fail-closed publication boundary.
- Keep the approval rule deterministic and independent of provider wording,
  severity labels, or model confidence.
- Bound GitHub history reads and avoid collecting comment bodies when only
  resolution state is required.

## Decision

Use the existing automatic-review and GitHub-writes path to select APPROVE only
after the validated result has no inline findings and a bounded GitHub GraphQL
sweep confirms that every existing pull-request review thread is resolved. Any
open thread, App-authored PR, draft/closed/fork/stale target, or lookup failure
keeps the event as COMMENT or fails closed before writing. Use one deterministic
policy seam so the review process records why approval was blocked without
trusting provider text; no separate approval toggle is introduced.

## Scope

In scope:

- A bounded GraphQL review-thread resolution sweep immediately before an
  eligible approval event.
- A provider-independent approval decision seam with stable blocker reasons.
- Tests, public documentation, and the proposed ADR for the approval criteria.

Out of scope:

- Adding a separate approval toggle or new credentials.
- Approving drafts, forks, App-authored PRs, stale heads, or conversation
  replies.
- Inferring human acceptance from comment text or model-supplied metadata.
- Merge, deployment, release, issue closure, or branch-protection changes.

## Consequences

Positive:

- A clean rerun cannot silently approve over unresolved prior review threads.
- Resolved threads no longer block a clean exact-head approval.
- GraphQL responses contain only bounded resolution booleans, minimizing data
  exposure while preserving the existing REST publication path.

Negative or tradeoffs:

- A missing, malformed, unauthorized, or oversized thread response fails closed
  rather than approving; the review may need a retry or manual approval.
- A thread resolved after the final sweep does not retroactively change the
  already-published event; the per-head marker remains idempotent.
- GitHub GraphQL availability becomes a dependency only for clean review
  publication; finding-bearing COMMENT publication is unchanged.

## Alternatives Considered

### Keep checking only the current result

- Summary: Continue approving whenever the current validated result has no
  inline comments.
- Why not chosen: It can approve a pull request with unresolved findings from
  an earlier ReviewSensei or human review.

### Treat every REST review comment as open forever

- Summary: Use the REST comments endpoint and assume every returned comment is
  unresolved.
- Why not chosen: REST does not expose GitHub thread resolution state, so this
  would permanently block approvals after a maintainer resolves a thread.

## Implementation Notes

- `evaluate_auto_approval` receives the validated result, App-authored state,
  and the thread-sweep result. Any inline finding or unresolved
  thread yields a COMMENT event; the decision exposes stable blocker reasons
  for diagnostics without exposing provider content.
- `ReviewPublisher` runs the sweep only when approval is otherwise eligible and
  just before POST. It queries `reviewThreads` with a maximum of ten pages and
  asks only for `isResolved`; malformed pages, GraphQL errors, and transport
  failures fail closed before any write.
- Marker reconciliation still happens first. A marker for the same head keeps
  the operation idempotent, so resolving a thread does not create a second
  review for an already-published head.

## Validation And Rollout

- Validation: Deterministic HTTP fakes cover clean approval, open and resolved
  threads, cursor pagination, malformed/error responses, App-authored PRs,
  findings, stale heads, and ordinary COMMENT publication. Run the full
  repository quality sequence and verify no provider or conversation path
  consumes the approval policy.
- Rollout: After the public workflow/package and customer setup are current,
  verify a clean approval and a finding-bearing comment in a test repository
  with automatic review and GitHub writes enabled.
- Rollback: Disable automatic review or GitHub writes, or revert the code/setup
  PR if needed. No review data migration or thread mutation is required.

## Follow-Up

- Add hosted evidence for an approval blocked by an unresolved thread and a
  later approval after that thread is resolved. Keep branch protection and
  maintainer review as the final merge gate.

## Links

- Related issue: not configured
- Related PR: 102
- Supersedes: [ADR 0027](0027-opt-in-app-approvals-for-clean-pull-request-reviews.md)
- Superseded by: none
