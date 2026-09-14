# ADR 0032 - Blocking finding classification for approvals

Status: Proposed
Date: 2026-09-14
GitHub Issue: not configured
Pull Request: draft PR to be linked
Owners/Reviewers: Maintainers
Approved by: not applicable

## Context

ReviewSensei currently treats every inline finding as merge-blocking, so an
otherwise healthy pull request cannot receive an App approval when the review
contains an optional follow-up. Maintainers need reviews to distinguish issues
that must be resolved before merge from suggestions that can be deferred or
ignored. The existing exact-head, same-repository, non-draft, non-App-authored,
and resolved-review-thread safeguards must remain in place.

## Decision Drivers

- Make the merge decision explicit and visible beside each finding.
- Allow optional follow-ups without withholding an otherwise eligible approval.
- Preserve a deterministic fallback for providers that omit the new field.
- Retain the existing GitHub thread-resolution safety gate and publication
  idempotency boundary.

## Decision

Add optional boolean `blocking` metadata to `ReviewComment` and the v1 review
comment/result schemas. The default prompt instructs providers to emit it for
every finding: `true` means the finding must be resolved before merge and
`false` marks an optional follow-up. Explicit `blocking` always wins. When it
is absent, only case-insensitive `critical` and `high` severity values block;
`medium`, `low`, missing, and non-canonical severity values are non-blocking.

Automatic approval requires no blocking findings and the existing bounded
GitHub review-thread sweep to report every prior thread resolved. Non-blocking
findings are published with their review and can accompany `APPROVE`; unresolved
threads, App-authored pull requests, drafts, forks, closed/stale targets, and
publication failures retain their existing fail-closed behavior.

## Scope

In scope:

- The provider-neutral result model, schemas, prompt, presentation, and GitHub
  approval policy.
- Deterministic tests for explicit and severity-fallback classifications.
- Public-contract, installation, architecture, and ADR documentation.

Out of scope:

- GitHub App permissions, setup variables, credentials, providers, or branch
  protection changes.
- Automatically resolving, dismissing, or muting any review thread.
- Changing the existing exact-head preflight, markers, or review-state
  reconciliation.

## Consequences

Positive:

- Reviews distinguish merge blockers from actionable follow-ups at the finding
  and summary levels.
- Teams can preserve optional improvement suggestions without unnecessarily
  stopping a compliant pull request.
- The fallback uses existing canonical severity terminology when a provider
  does not emit the new field.

Tradeoffs:

- Provider classification now directly informs approval eligibility, so prompt
  quality and deterministic validation are important controls.
- A misclassified non-blocking issue can be approved; unresolved GitHub threads
  and the normal maintainer merge gate remain independent safeguards.

## Alternatives Considered

### Continue treating every finding as blocking

Rejected because it cannot represent the requested optional-follow-up workflow.

### Infer blocking from severity only

Rejected because impact and merge readiness are related but different decisions;
an explicit boolean lets maintainers and providers state the intended outcome.

### Remove the unresolved-review-thread gate

Rejected because it would allow automatic approval despite unresolved human or
earlier review feedback and weakens the existing publication-control boundary.

## Validation

Run model/schema, service, presentation, approval-policy, and GitHub publisher
tests. Verify that explicit `false` can publish an approval after a resolved
thread sweep; explicit `true` and omitted `critical`/`high` classifications
remain comments; and omitted non-severe classifications are eligible subject to
the same thread/preflight gates.

## Rollout and Rollback

Rollout is a reviewed package and workflow update. New default prompts emit the
flag immediately; callers that omit it use the severity fallback. Verify both a
non-blocking approval and a blocking comment in a test repository before
enabling the flow for protected production branches.

Rollback by disabling automatic review or GitHub writes, or reverting the
feature. No persisted review data or GitHub thread mutation requires migration.

## Follow-Up

Collect hosted evidence for explicit blocking, explicit non-blocking, and
severity-fallback review runs after rollout. Revisit the fallback if provider
telemetry shows frequent omitted classifications.

## Links

- Related issue: not configured
- Pull request: draft PR to be linked
- Supersedes: [ADR 0030](0030-gate-app-approvals-on-resolved-review-threads-and-exact-head-review-safety.md)
- Superseded by: none
