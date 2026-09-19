# ADR 0051 - Sequential evaluation and observation-only shadowing

Status: Proposed
Date: 2026-09-19
Last amended: 2026-09-19
GitHub Issue: #136
Pull Request: [#143](https://github.com/malsabbagh/review-sensei/pull/143)
Owners/Reviewers: Maintainers
Approved by: not applicable

## Context

Issue #136 C7 requires offline sequence evidence, published metrics and
limitations, observation-only shadowing of the operator policy, and an
explicitly authorized pilot. C1–C6 already define modes, publication
admission, the durable ledger, verification scope, round enforcement, and
maintainer commands. None of those slices may promote `merge-focused` to
the installed default. Existing installations stay on `legacy` until a
later, documented migration with rollback.

A small synthetic sentinel is not a claim of zero missed defects. Prompt-only
filtering is not a substitute for the stateful policy. GitHub publication
must not change because a shadow mode is set.

## Decision

Add an offline replay (`replay_review_sequence`) that drives the real C3/C5
session seams against frozen synthetic steps. The public
`convergence-sequence-report` records admit/handoff outcomes, round counts,
handoffs, no-progress events, and `cap_created_approval`, which is always
false: the cap never mints approval. CLI `evaluate-convergence` replays a
built-in sentinel; `--compare-default` also replays the compatible `legacy`
publication default.

Shadowing is `REVIEWSENSEI_REVIEW_SHADOW`. It must be an operator mode
(`advisory`, `merge-focused`, or `strict`). `legacy` is rejected because it
is already the compatible default. `observe_shadow_admission` evaluates the
shadow policy against current session facts and never reserves, skips, or
publishes. GitHub publication and inference skip stay on the resolved review
mode. Doctor and plan display the shadow policy as observation-only.

The authorized pilot remains an explicit `--review-mode` /
`REVIEWSENSEI_REVIEW_MODE` opt-in. This slice does not change
`DEFAULT_REVIEW_MODE`, does not alter `REVIEWSENSEI_AUTO_APPROVE`, and does
not recreate #114/#115 gates. Promoting `merge-focused` as the product
default requires later evidence plus a documented migration/rollback.

## Scope

In scope:

- Sequence replay, comparison against `legacy`, public sequence-report
  schema, CLI `evaluate-convergence`, shadow env/doctor/plan display, and
  attaching an observation-only shadow payload to `PublicationResult`
  without changing GitHub events.

Out of scope:

- Changing the installed default from `legacy`.
- Live paid model runs, production tag/deployment changes, or closing #136.
- Recreating completeness/qualification gates owned by #114/#115.

## Consequences

Positive:

- Maintainers can compare `legacy` publication with operator admission on
  frozen sequences without changing installs.
- Shadowing observes the proposed policy while GitHub events stay legacy.
- Sequence reports carry explicit limitations.

Negative:

- A synthetic sentinel is not production recall/precision evidence.
- Shadow payloads are diagnostic only; hosts must not treat them as skip
  authority.

## Alternatives considered

### Promote merge-focused when C7 lands

Rejected: the issue allows promotion only after evidence and a documented
migration. Compatible default remains `legacy`.

### Change GitHub events from the shadow decision

Rejected: shadow is observation-only. Publication skip stays on the
resolved review mode.

## Validation

Run sequence, shadow, doctor/plan, CLI `evaluate-convergence`, schema
fixture, and existing C5 publication tests. Ordinary CI stays offline and
credential-free.

## Rollout and rollback

Default publication remains `legacy`. Enable shadow with
`REVIEWSENSEI_REVIEW_SHADOW`. Enable enforcement with an explicit operator
`--review-mode` plus a session ledger. Rollback by unsetting shadow and
returning to `legacy`; unresolved findings are not discarded.

## Follow-up work

- Authorized `merge-focused` pilot on labelled histories.
- Default-mode promotion only after recorded evaluation evidence and a
  migration/rollback ADR.

## Links

- Related issue: [#136](https://github.com/malsabbagh/review-sensei/issues/136)
- Pull request: [#143](https://github.com/malsabbagh/review-sensei/pull/143)
- Related: [ADR 0046](0046-evidence-based-blocker-admission-and-review-loop-convergence.md),
  [ADR 0049](0049-automation-admission-and-handoff.md),
  [ADR 0050](0050-maintainer-disposition-and-handoff-status.md)
- Supersedes: none
- Superseded by: none
