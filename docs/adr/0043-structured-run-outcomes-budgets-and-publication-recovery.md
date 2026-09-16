# ADR 0043 - Structured run outcomes, resource budgets, and publication-only recovery

Status: Proposed
Date: 2026-09-15
GitHub Issue: #36
Pull Request: draft PR to be linked
Owners/Reviewers: Maintainers
Approved by: not applicable

## Context

Callers could not distinguish a clean review from an intentional skip, a
partial result, or an execution/publication failure without parsing logs.
Provider retries mixed structural correction with transport failure, and a
validated result that failed to publish could only be recovered by invoking the
model again.

## Decision

Every `ReviewService` and GitHub publication attempt emits a versioned
`RunOutcome`. `ReviewService.review` raises `ReviewInputError` when resource
budgets are exhausted; callers that need the structured envelope should use
`ReviewService.run` instead. Statuses are `reviewed`, `partial`, `skipped_stale`,
`skipped_policy`, `provider_failed`, `budget_exhausted`, `publication_failed`,
and `already_published`. Diagnostics are closed tokens. Actions summaries and
`GITHUB_OUTPUT` expose the same envelope without prompts, responses, or source
text.

`ResourceBudget` is enforced before each provider call. Transport retries for
explicitly transient HTTP/rate-limit failures are bounded and may honor a
numeric `Retry-After` hint. The existing one-shot structural-correction retry
remains separate and does not rewrite unsafe GitHub writes; publishers continue
to reconcile markers instead of repeating POST bodies blindly.

Publication recovery is opt-in and publication-only. A caller supplies an
integrity-checked, identity-bound, time-limited `RecoveryArtifact`. Recovery
revalidates current repository/base/head/policy, invokes no model, and cannot
open or refresh learning pull requests or trusted configuration.

## Scope

In scope:

- `ReviewService.run`, CLI `--outcome` / `--recover-from` / `--recovery-artifact`
- GitHub `recover_review` and outcome projection from `PublicationResult`
- opt-in Actions summaries and artifact sidecar files

Out of scope:

- expanding write permissions, egress, trusted context, or automatic approval
- a hosted artifact store or automatic artifact download
- converting workflow preflight identity failures into successful skip jobs

## Consequences

Positive:

- Hosts can branch on a stable machine-readable status.
- Budgets fail closed before additional provider spend.
- A retained validated result can be published after a transport failure
  without repeating model inference.

Tradeoffs:

- The public run deadline ceiling remains 120 seconds.
- Missing or expired recovery artifacts are explicit publication failures.
- Ineligible workflow preflight still fails the job rather than emitting a
  successful skip outcome.

## Alternatives Considered

### Retry GitHub review POST bodies after ambiguous failures

Rejected because duplicate reviews are worse than a fail-closed outcome. Marker
reconciliation remains the only write-recovery path.

### Recover by rewriting repository learnings or stage configuration

Rejected because trusted base content is not an execution cache.

### Upload recovery artifacts by default

Rejected because review text can contain source-derived information. Retention
stays behind the existing `upload_artifacts` opt-in.

## Validation And Rollout

- Deterministic tests cover budget exhaustion, transport vs structural retry,
  cancellation/deadlines, partial coverage, skipped writes, expired/tampered
  artifacts, missing artifacts, concurrent already-published recovery, and
  canary-secret redaction.
- Run the repository unit suite before publication.
- Leave this ADR Proposed until maintainers accept it.

## Rollback

Revert the service/CLI/publisher wiring, workflow outcome flags, tests, and
documentation. Existing review JSON remains valid. No repository data migration
is required.

## Follow-up Work

- Convert workflow preflight ineligibility into `skipped_policy` /
  `skipped_stale` outcomes without failing the Actions job.
- Coordinate artifact provenance with issue #35 if a hosted recovery store is
  added later.

## Links

- Related ADRs: 0001, 0007, 0022, 0028
- Parent issue: #24
