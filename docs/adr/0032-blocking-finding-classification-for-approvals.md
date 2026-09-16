# ADR 0032 - Blocking finding classification for approvals

Status: Proposed
Date: 2026-09-14
Last amended: 2026-09-15
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
and exact-head safeguards must remain in place. A resolved blocking finding
must also re-evaluate approval without requiring another provider pass.

## Decision Drivers

- Make the merge decision explicit and visible beside each finding.
- Allow optional follow-ups without withholding an otherwise eligible approval.
- Preserve a deterministic fallback for providers that omit the new field.
- Retain exact-head, marker, and publication idempotency boundaries while
  making blocking-thread state the sole ReviewSensei approval gate.

## Decision

Add optional boolean `blocking` metadata to `ReviewComment` and the v1 review
comment/result schemas. The default prompt instructs providers to emit it for
every finding: `true` means the finding must be resolved before merge and
`false` marks an optional follow-up. Explicit `blocking` always wins. When it
is absent, only case-insensitive `critical`/`high` severity values block.
Missing, lower-severity, and legacy free-form severity values are non-blocking.

Review publication records blocking findings as `REQUEST_CHANGES` when
automatic approval is enabled, otherwise `COMMENT`. A hidden, exact-head-bound
marker on each root persists the typed blocking decision. See
[ADR 0035](0035-request-changes-for-blocking-findings.md) for the two-execution
same-head event policy.
Automatic approval defaults to enabled when automatic review and GitHub writes
are enabled; `REVIEWSENSEI_AUTO_APPROVE=false` is the explicit opt-out. The
shared approval finalizer then performs the only `APPROVE` write. It may
approve when no unresolved ReviewSensei root is classified blocking; unresolved
non-blocking ReviewSensei findings and human threads do not withhold that
approval. The finalizer runs after a review is published and after the AI
resolves a blocking ReviewSensei thread. It is marker-idempotent, exact-head,
same-repository, non-draft, and non-App-authored; malformed or incomplete
ReviewSensei root metadata fails closed.

## Scope

In scope:

- The provider-neutral result model, schemas, prompt, presentation, and GitHub
  approval policy.
- Deterministic tests for explicit and severity-fallback classifications.
- Public-contract, installation, architecture, and ADR documentation.

Out of scope:

- GitHub App permissions, setup variables, credentials, providers, or branch
  protection changes.
- Changing GitHub branch protection, maintainer approvals, or merge policy.
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
- A misclassified non-blocking issue can be approved; the normal maintainer
  merge gate remains an independent safeguard.

## Alternatives Considered

### Continue treating every finding as blocking

Rejected because it cannot represent the requested optional-follow-up workflow.

### Infer blocking from severity only

Rejected because impact and merge readiness are related but different decisions;
an explicit boolean lets maintainers and providers state the intended outcome.

### Treat every unresolved review thread as blocking

Rejected because it contradicts the explicit finding classification and leaves
optional follow-ups capable of permanently withholding an approval.

## Validation

Run model/schema, service, presentation, approval-finalizer, GitHub publisher,
and conversation-resolution tests. Verify that an unresolved explicit `false`
root does not block, an explicit `true` root does, and resolving the last
blocking root invokes one exact-head idempotent approval finalization.

## Rollout and Rollback

Rollout is a reviewed package and workflow update. New default prompts emit the
flag immediately; callers that omit it use the severity fallback. Verify both a
non-blocking approval and a blocking comment in a test repository before
enabling the flow for protected production branches. Verify the default-on
approval path and the explicit `REVIEWSENSEI_AUTO_APPROVE=false` opt-out.

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
