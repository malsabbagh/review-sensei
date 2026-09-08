# ADR 0027 - Opt-in App approvals for clean pull-request reviews

Status: Superseded
Date: 2026-09-02
GitHub Issue: #96
Pull Request: #98
Owners/Reviewers: Maintainers
Approved by: not applicable

This ADR is retained as a historical record. Its separate approval toggle was
removed by [ADR 0030](0030-gate-app-approvals-on-resolved-review-threads-and-exact-head-review-safety.md).

## Context

ReviewSensei currently publishes every validated App-authored pull-request review as a COMMENT event. A clean result cannot satisfy branch-protection approval requirements, while the existing exact-head, repository, fork, marker, and fail-closed guards must remain shared. Setup-v4 callers and the public reusable workflow need an explicit opt-in without changing App installation permissions or conversation reply behavior.

## Decision Drivers

- Preserve the existing exact-head, base/repository, fork, marker/idempotency,
  and fail-closed publication boundary.
- Make formal approvals an explicit repository-controlled opt-in with a safe
  default and no App permission or secret changes.
- Keep findings as comments and keep `@sensei` replies on conversation
  endpoints, so approval cannot be triggered by reply content or model output.
- Keep Python setup generation and the Cloudflare Worker setup output in sync,
  while migrating older managed setup-v4 files through a reviewable PR.

## Decision

Add REVIEWSENSEI_AUTO_APPROVE as a false-by-default setup/workflow/CLI option. Thread it through GitHubWriteOptions to ReviewPublisher, which selects APPROVE only when the option is true, the validated result has zero inline comments, and the final pull-request review-thread sweep has no unresolved threads; all other reviews remain COMMENT. Keep the existing preflight and marker/idempotency path unchanged, and preserve historical setup-v4 recognition as managed stale content. ADR 0030 refines the clean-review definition and its bounded thread-sweep contract.

## Scope

In scope:

- `REVIEWSENSEI_AUTO_APPROVE` in setup variables/configuration, the reusable
  workflow input, generated setup-v4 callers, and the CLI publication command.
- The `GitHubWriteOptions` and `ReviewPublisher` seams and deterministic tests
  for approval, comments, disabled writes, stale heads, and duplicates.
- Public contract, installation guidance, architecture references, and this
  ADR.

Out of scope:

- New GitHub App permissions, credentials, broker capabilities, persistence, or
  provider adapters.
- Automatic approval for fork pull requests, reviews with any inline finding,
  unresolved review thread, or any `@sensei` conversation reply.
- Public snapshot/tag movement, package release, Worker deployment, setup PR
  merge, or hosted acceptance; those remain operator-owned follow-up gates.

## Consequences

Positive:

- Clean validated reviews can satisfy branch-protection approval requirements
  when a repository explicitly opts in.
- One publisher path keeps approval and comment writes under the same
  validation, exact-head, and idempotency checks.
- The default remains non-approving, and existing installations can migrate
  without reinstalling the App or changing its `pull_requests: write` grant.

Negative or tradeoffs:

- Enabling the switch grants ReviewSensei authority to approve clean reviews;
  repository maintainers must make that policy choice deliberately.
- An existing marker for the same head still suppresses a second write, so
  enabling approval after a comment was already published does not retroactively
  create an approval.
- Hosted approval behavior remains unverified until the reviewed public
  workflow, package, Worker, setup migration, and customer opt-in are rolled
  out and exercised.

## Alternatives Considered

### Separate approval publisher

- Summary: Add a second publisher/application path dedicated to formal
  approvals.
- Why not chosen: It would duplicate preflight, marker reconciliation, response
  mapping, and failure handling, increasing drift across the authorization
  boundary without providing a new safety property.

### Always approve clean reviews

- Summary: Change every no-finding review to `APPROVE` without a new switch.
- Why not chosen: It would be a breaking authorization change for existing
  repositories and would violate the requested disabled-by-default opt-in.

## Implementation Notes

- `ReviewPublisher.publish(auto_approve=False)` computes `APPROVE` only when
  `auto_approve` is true, `ReviewResult.comments` is empty, the final bounded
  review-thread sweep is clean, and the preflight PR author is not the
  ReviewSensei App. Otherwise it sends the existing `COMMENT` event and
  comments payload; this preserves setup/migration PR reviews because GitHub
  rejects an App approving its own pull request. ADR 0030 defines the sweep and
  its fail-closed behavior.
- `GitHubApplication.publish_review` forwards the option only for review
  publication. Reply methods do not consume it and continue to use
  `inline_reply`/`issue_reply` capabilities and comment endpoints.
- Setup-v4 current templates add `enable_auto_approve` and the false default;
  Python and TypeScript classifiers retain exact recognition for the previous
  provider-parity workflow/config plus setup-v3 and legacy artifacts.
- The existing `pull_requests: write` permission is sufficient for
  `POST /repos/{owner}/{repo}/pulls/{number}/reviews` with `event: APPROVE`.

## Validation And Rollout

- Validation: deterministic publisher HTTP fakes assert the exact `APPROVE` or
  `COMMENT` event and no-write behavior for disabled, finding, open/resolved
  thread, App-authored, stale, fork, and duplicate cases; CLI,
  setup-generation, action-policy, Worker parity, and full repository quality
  checks pass. Review output, marker identity, and conversation tests remain
  comment-only.
- Rollout: after this change is merged, publish the audited public snapshot,
  release the package or verify the executing-SHA fallback, deploy the Worker,
  move the protected `v4` tag, trigger fresh setup reconciliation, merge the
  generated setup PR, explicitly set `REVIEWSENSEI_AUTO_APPROVE=true` together
  with existing write/review switches, then verify a clean approval and a
  finding comment in a test repository.
- Rollback: set the new switch to `false`, stop/redeploy the prior Worker and
  public workflow tag through the normal operator process, and revert the code
  and setup migration PR if needed. No review data migration is required and
  customer default branches are never mutated by rollback.

## Follow-Up

- Record hosted approval/finding-flow evidence and confirm branch-protection
  behavior after the public snapshot, Worker, package, and customer setup are
  all current. Keep the issue open until those external gates are verified.

## Links

- Related issue: #96
- Related PR: #98
- Supersedes: none
- Superseded by: ADR 0030 (approval criteria refinement)
