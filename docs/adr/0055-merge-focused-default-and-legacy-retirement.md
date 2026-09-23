# ADR 0055 - Merge-focused default and legacy retirement

Status: Proposed
Date: 2026-09-21
GitHub Issue: #146 F7
Owners/Reviewers: Maintainers

Status note: this record stays `Proposed` deliberately at merge time. The ADR
process requires explicit maintainer approval to mark a record `Accepted`
(`docs/process/adr-process.md`), so this cutover ships as an intentional
pre-acceptance change; the approval step is tracked as part of the F7 rollout
on issue #146, and the index row carries the matching `Proposed` status.

## Context

The sequential-evaluation rollout kept `legacy` as the runtime default while
the merge-focused policy accumulated durable-ledger, baseline, admission, and
observed-convergence evidence. That compatibility posture no longer gives new
installations the policy that the staged rollout is intended to promote.

## Decision

New runtime configuration, CLI invocation, generated setup files, and managed
workflow callers default to `merge-focused`. Setup provisions the effective
repository variable (`REVIEWSENSEI_REVIEW_MODE`) as well as recording the
default in the generated configuration file, so the live caller and its
operator-visible configuration agree.

Generated setup increments from v4 to v5. Classifiers retain byte-exact
recognition of historic v4 content, including the immediate pre-cutover v4
caller/configuration that already specifies `merge-focused`, and open a
reviewable v5 setup PR. The migration changes only generated setup files; it
does not reset durable session-ledger state or finding identities.

`legacy` remains readable only as historical persisted policy data. It is not a
live runtime selection: explicit CLI or configuration use fails with an
actionable migration error. The migration helper maps a historical `legacy`
value to `merge-focused` idempotently; it does not reset the session ledger,
finding identities, baseline, or counters. Setup additionally migrates an
existing `REVIEWSENSEI_REVIEW_MODE=legacy` repository variable to
`merge-focused` in place (only that variable, only that value), so a prior
installation cannot keep a retired value that the generated caller and its
workflow guard would reject. Advisory, strict, and disabled policy choices
retain their existing meaning.

The retirement is not reversible by configuration. Writing `legacy` back into
the repository variable feeds the generated caller and the workflow guard a
rejected mode, so the run fails with the migration error, and the next setup
run migrates the value again. Restoring the retired policy therefore requires
a forward version change that reintroduces it, not an operator variable write.

Implementation note: the one deliberate carve-out is a low-level embedder or
historical-fixture replay that constructs `ReviewConvergencePolicy(mode="legacy")`
directly. That carve-out is enforceable rather than conventional:
`ReviewPublisher.publish` raises `GitHubPublicationError` for a `legacy`
policy unless the caller also passes `allow_retired_legacy_policy=True`, so a
future caller cannot re-enable the retired policy by passing the policy object
alone. The carve-out is documented in `docs/public-contracts.md` and is not
reachable from the CLI: every CLI and configuration surface resolves its
policy through `resolve_review_mode`, which raises the migration error for
`legacy` before preparation or any GitHub write, and the generated caller,
Worker, and reusable workflow only ever pass the resolved operator mode.
Preparation and publication must bind the same policy digest, so a
legacy-bound prepared artifact cannot enter the default publication path.

Package publication, workflow-channel promotion, Worker/config deployment,
and managed-installation migration are release operations. They remain
operator-controlled and require exact-version/commit readback and a rollback
rehearsal; merging this ADR or its implementation does not perform them.

## Consequences

Fresh installs use merge-focused behavior without a user-supplied flag.
Existing explicit legacy configuration receives a deterministic migration
instruction instead of silently changing policy. Historical tests and readers
can still construct the legacy policy directly to validate compatibility.
Omitting a policy in the low-level verifier or publisher also resolves to
`merge-focused`; preparation and publication bind the same policy digest.
Existing prepared output bound to a different policy is rejected rather than
silently republished under the new default.

Merge-focused round admission requires a trusted session ledger. Without one,
`doctor` reports `status=action` and exits 2; `plan` includes
`session-ledger-required` in `skip_reasons`. A plan's `status=ready` means its
diff was analyzed, not that review execution is authorized. Neither diagnostic
creates a ledger, grants a round, invokes inference, or performs a GitHub write.

Managed setup recognition is byte-exact for both historical and current
versions. Editing generated configuration (including provider or model fields)
makes that installation a custom `unknown` no-write case. Operators should
configure the generated caller through repository Actions variables; custom
files require manual reconciliation, not a relaxed recognition rule.

The frozen managed-v4 recognition fixtures (`merge-focused-v4-caller.yml`,
`merge-focused-v4-config.yml`, `historical-v4-uninstall.yml`) are byte-pinned
historical artifacts: their literal contents describe the setup-v4 era and are
not documentation of current behavior, so they are read as recognition
evidence only and never as a source of the live tag or template.

File-level findings are folded into the review summary body on every review
event. The previous publisher folded them only when the event would be
`REQUEST_CHANGES`; the batch create-review request type defines no file
subject type and requires a `position`, and a direct probe on a `COMMENT`
review rejects a file-level entry with HTTP 422
(`subjectType` is not defined on `DraftPullRequestReviewComment`, `position`
is required). A second probe isolated the two fields: submitted with
identical coordinates, the entry carrying `subject_type: "file"` is rejected
with HTTP 422 naming only `0.subjectType` (`Field is not defined on
DraftPullRequestReviewThread`), while the same entry without `subject_type`
succeeds with HTTP 200 `COMMENTED`. The subject type is therefore rejected on
its own, not because of the coordinates, and because the schema is
event-independent the conditional could never have produced a per-file
thread on a non-blocking review. Unifying the fold removes a latent
`publication_failed` path without reducing what a non-blocking review
conveys; the publisher contract in `docs/public-contracts.md` documents the
behavior.

## Rollback

Rollback is a version rollback to the previous release (0.6.0), performed as a
release operation and rehearsed before this cutover ships. At this commit, the
verified properties of that rollback are:

- The previous release's Worker classifies a managed file whose setup marker is
  greater than its own `SETUP_VERSION` (`4`) as `unknown` and reports
  `skipped_unknown_setup`; it writes and deletes nothing, so a v5 installation
  is never rewritten or clobbered by the older Worker.
- The previous release recognizes only its own rendered templates plus the
  frozen `LEGACY_SHA256` and `RELEASED_RUNNER_SWITCH_V4_SHA256` artifacts.
  Repositories holding this cutover's byte-frozen `merge-focused-v4-*` fixtures
  remain `unknown` under it, so restoring the released v4 caller and
  configuration set is a required rollback step before the previous release's
  setup can reconcile them.
- A migrated `merge-focused` repository variable is a valid operator value for
  the previous release's mode resolver, and the previous release's reusable
  workflow declares no `review_mode` input and never reads that variable, so the
  stored value is inert under a rollback.
- Restore the reusable workflow and the generated callers together: this
  cutover's caller passes `review_mode`, an input only this cutover's workflow
  declares, so a workflow-only rollback leaves every generated caller passing an
  undeclared input and jobs fail before any step runs. The `v5` workflow channel
  moves back to the released revision as part of that same operator operation.
- No stored review data migration is needed in either direction: the session
  ledger, baseline, counters, and finding identities are untouched by this
  change.

Rollback does not restore `legacy` as a selectable policy and does not rewrite
the repository variable or any generated file; once the released caller set is
restored, the previous release's own setup and recognition rules apply.

## Validation

Exercise default and explicit-mode unit tests, generated workflow/setup tests,
the packaged artifact, and the repository regression suite. Before release,
an operator must capture exact released identities, effective-default readback,
a fresh install review, a managed-installation migration with preserved ledger
state, and rollback evidence.

## Links

- [Issue #146](https://github.com/malsabbagh/review-sensei/issues/146)
- [ADR 0046](0046-evidence-based-blocker-admission-and-review-loop-convergence.md)
- [ADR 0047](0047-durable-review-session-ledger.md)
- [ADR 0051](0051-sequential-evaluation-and-shadowing.md)
- [ADR 0054](0054-observed-convergence-acceptance-evidence.md)
