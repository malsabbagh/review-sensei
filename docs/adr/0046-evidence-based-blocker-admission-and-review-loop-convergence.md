# ADR 0046 - Evidence-based blocker admission and review-loop convergence

Status: Proposed
Date: 2026-09-19
Last amended: 2026-09-19
GitHub Issue: #136
Pull Request: [#138](https://github.com/malsabbagh/review-sensei/pull/138)
Owners/Reviewers: Maintainers
Approved by: not applicable

## Context

Authors can fix blocking findings, push, and receive a new set of merge
prerequisites. Some later findings are genuine regressions or missed defects;
others are reworded duplicates, optional polish, or unstable model output.
Issue #136 requires a deterministic, stateful policy around the existing
engine rather than a softer prompt, a raw comment cap, or an approval timeout.

Existing classification (`ReviewComment.blocks_approval`, ADR 0032) treats an
explicit model `blocking` boolean as authoritative. That sits too close to
publication: a model proposal is not a trusted merge gate. In-memory review
context (ADR 0042) and in-run budgets (ADR 0043) also do not define a
PR-wide round budget across fresh Actions jobs.

C1 added the versioned policy contract, pure evaluators, and doctor/plan
display. This amendment is slice **C2**: sit `evaluate_blocker_admission`
between candidate findings and publication while preserving proposed versus
effective classification. It does not change `REVIEWSENSEI_AUTO_APPROVE`
default-on semantics, completeness/coverage gates (#114), or OpenRouter
qualification (#115).

## Decision

Add a provider-neutral `ReviewConvergencePolicy` resolved from `--review-mode`
or `REVIEWSENSEI_REVIEW_MODE`. The compatible default is `legacy`. Operator
opt-in modes are `advisory`, `merge-focused`, and `strict`.

Four controls stay distinct:

| Dimension | Meaning in this contract |
| --- | --- |
| Analysis effort | Unchanged; reuse existing stage/budget limits. Deeper analysis does not grant stronger blocking authority. |
| Blocking policy | Trusted evaluator computes effective disposition from structured facts. |
| Re-review scope | Initial review versus fix verification. Scope planning remains C4. |
| Automation budget | One completed initial review and at most two completed verification rounds in the pilot defaults, plus a separate failed-attempt bound. |

`legacy` preserves today's effective blocking rule (explicit `blocking` wins,
else case-insensitive `high`/`critical`) and does not cap rounds. Operator
modes ignore the model boolean as authority. Merge-focused admits a blocker
only when the candidate has a specific violation or an explicitly required
repository contract, high/critical material impact, an actionable remedy,
validated evidence plus a failure condition or an independent analyzer/test
artifact, PR/fix/exception attribution, and no duplicate, still-valid human
disposition, or contradictory evidence. A required contract is a violation
source, not a substitute for high/critical impact. JSON validity, line
validity, model confidence, and LLM agreement are not proof. Late blockers
after a baseline require `new-regression` or `substantiated-missed-defect`.
Weak high-impact concerns and contradictions escalate to human adjudication
without inventing a proven blocker.

`advisory` uses the same admission rules but sets
`automatic_github_review_events=false` and `inline_advisory_threads=false`.
`strict` remains bounded and additionally lets a named mandatory rule qualify
independently of high/critical severity when the other gates pass.

C2 derives `BlockerCandidate` facts from structured comment and verification
fields (`derive_blocker_candidate`) and writes `effective_blocking` /
`needs_human` onto each finding (`admit_review_result`) before a publisher
formats comments or chooses a GitHub review event. Confirmed evidence
verification (#114) validates snapshot locations; it does not mint a failure
condition or actionable remedy. Operator-mode admission stays fail-closed
unless the caller supplies explicit `BlockerCandidate` facts. The model `blocking` field
remains the proposal. `ReviewComment.blocks_approval` uses the admitted
effective value when present. Publication markers and `REQUEST_CHANGES` follow
effective blocking. Human adjudication withholds `APPROVE` through
`evaluate_auto_approval` without inventing a proven blocker. Advisory
observations in operator modes are folded into the review summary so required
conversation resolution cannot turn optional notes into mechanical blockers.
`legacy` enforcement stays `display-only` and keeps ADR 0032/0035 events.

Round admission counts completed logical reviews, not provider calls, commits,
or retries. Same-head duplicates, publication recovery, and transport or
structural retries do not consume the completed-round budget and cannot emit
approval. No-progress and exhausted failed-attempt budgets hand off even when
the current invocation is a duplicate, recovery, or retry. The cap never
creates approval eligibility; a last allowed round may approve only when
independent gates already pass. Incomplete coverage or an unreviewed later
head cannot approve. Round enforcement in hosts remains C5.

Pilot defaults (`verification_rounds=2`, `failed_attempts=6`) are tunable
design hypotheses, not industry standards, and must be validated before any
future default promotion (C7).

## Scope

In scope:

- ADR, versioned JSON schemas, Python policy/evaluator types, and public
  exports.
- Decision-table tests for modes, severity/evidence/scope reasons, round
  counting, handoff, and legacy compatibility.
- Doctor/plan display and `--review-mode` / `REVIEWSENSEI_REVIEW_MODE`.
- C2 derivation and admission between findings and publication, including
  GitHub event tests for model `blocking=true/false` conflicts.

Out of scope:

- Durable session ledgers (C3), baseline-aware verification (C4), automation
  admission in hosts (C5), maintainer commands (C6), and evaluation rollout
  (C7).
- Changing `REVIEWSENSEI_AUTO_APPROVE`, GitHub permissions, branch protection,
  or website/OpenRouter epics.
- Recreating completeness, coverage, or OpenRouter qualification gates from
  #114/#115.

## Consequences

Positive:

- Existing installations keep current classification and unbounded autonomous
  rounds until they opt in.
- Merge-blocker correctness is a trusted policy, not a prompt or a model flag.
- Operator modes can demote an explicit model blocker and promote a
  well-supported finding the model marked optional, with both values visible.

Tradeoffs:

- C2 cannot yet bound PR-wide rounds; that requires C3–C5.
- Conservative derivation from structured fields fail-closes missing evidence.
  Garbage facts still yield garbage decisions.
- Two verification rounds remain a hypothesis until C7 evidence exists.

## Alternatives considered

### Soften the review prompt or lower severity

Rejected because prompt-only filtering does not persist across jobs, does not
distinguish regressions from scope creep, and cannot bound rounds.

### Cap comments or auto-approve after N runs

Rejected because a comment cap hides defects and a timeout/round cap must never
mint approval eligibility.

### Silently switch the default to merge-focused

Rejected because it would change `blocking` authority and round behavior for
current installations. Default remains `legacy` until an explicit migration.

### Treat the model `blocking` boolean as the new policy

Rejected because issue #136 requires trusted policy between proposals and
effective publication decisions.

### Overwrite `ReviewComment.blocking` with the evaluator result

Rejected because C2 must preserve proposed versus effective classification.

## Validation

Run evaluator decision-table tests, derivation/admission tests, schema
golden/negative fixtures, doctor and plan display tests, and the publication
suite including real GitHub event assertions for model `blocking` conflicts.
Ordinary CI stays offline and credential-free.

## Rollout and rollback

Rollout is a reviewed package. Existing installations remain on `legacy`.
Operators opt into `advisory`, `merge-focused`, or `strict` via
`REVIEWSENSEI_REVIEW_MODE` or `--review-mode`. Rollback by reverting the
package or returning the mode to `legacy`. No persisted review data or GitHub
thread mutation requires migration.

## Follow-up work

- C3: durable session ledger for PR-wide counters.
- C4–C7 as specified in issue #136.
- Coordinate with #114/#115 for the actual approval boundary; do not duplicate
  those gates here.

## Links

- Related issue: [#136](https://github.com/malsabbagh/review-sensei/issues/136)
- Pull request: [#138](https://github.com/malsabbagh/review-sensei/pull/138) (C2); C1: [#137](https://github.com/malsabbagh/review-sensei/pull/137)
- Related: [ADR 0032](0032-blocking-finding-classification-for-approvals.md),
  [ADR 0035](0035-request-changes-for-blocking-findings.md),
  [ADR 0039](0039-verify-candidate-findings-before-publication.md),
  [ADR 0042](0042-incremental-reviews-and-finding-lifecycle.md),
  [ADR 0043](0043-structured-run-outcomes-budgets-and-publication-recovery.md)
- Supersedes: none
- Superseded by: none
