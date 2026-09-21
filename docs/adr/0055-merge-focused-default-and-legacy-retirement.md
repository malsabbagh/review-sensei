# ADR 0055 - Merge-focused default and legacy retirement

Status: Proposed
Date: 2026-09-21
GitHub Issue: #146 F7
Owners/Reviewers: Maintainers

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

`legacy` remains readable only as historical persisted policy data. It is not a
live runtime selection: explicit CLI or configuration use fails with an
actionable migration error. The migration helper maps a historical `legacy`
value to `merge-focused` idempotently; it does not reset the session ledger,
finding identities, baseline, or counters. Advisory, strict, and disabled
policy choices retain their existing meaning.

Package publication, workflow-channel promotion, Worker/config deployment,
and managed-installation migration are release operations. They remain
operator-controlled and require exact-version/commit readback and a rollback
rehearsal; merging this ADR or its implementation does not perform them.

## Consequences

Fresh installs use merge-focused behavior without a user-supplied flag.
Existing explicit legacy configuration receives a deterministic migration
instruction instead of silently changing policy. Historical tests and readers
can still construct the legacy policy directly to validate compatibility.

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
