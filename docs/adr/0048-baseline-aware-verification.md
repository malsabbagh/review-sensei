# ADR 0048 - Baseline-aware verification

Status: Proposed
Date: 2026-09-19
Last amended: 2026-09-19
GitHub Issue: #136
Pull Request: [#140](https://github.com/malsabbagh/review-sensei/pull/140)
Owners/Reviewers: Maintainers
Approved by: not applicable

## Context

Issue #136 C4 requires a later review to verify accepted concerns and detect
material regressions instead of restarting an unrestricted search for new
merge prerequisites. C1/C2 already define late-admission reasons
(`new-regression`, `substantiated-missed-defect`) but callers had to supply
those facts. ADR 0042 already reconciles finding identity and refuses to
treat omission as a fix. ADR 0037/#40 already names bounded related paths.
None of those pieces planned verification scope from a complete compatible
baseline or recorded causal lineage for a fix-introduced defect.

Target-branch provenance (whether a defect is PR-induced) stays distinct from
the last compatible completed review (what has already been assessed).

## Decision

Add a provider-neutral `ReviewBaseline` taken only from a complete compatible
review. Incomplete, partial, or budget-exhausted discovery cannot establish
that baseline. Operator-mode later passes call `plan_verification_scope`,
which reuses `IncrementalReviewPlan` (ADR 0042) and directory/symbol related
paths (#40 / `related_paths_for_change`) so the reviewed set is existing
concerns plus changes since that baseline plus bounded impact.

Later findings are classified before C2 admission:

- Same fingerprint or path/symbol/defect-kind identity is a continuing or
  reworded concern, not a new blocker.
- Distinct defect kinds on one symbol stay distinct.
- A fix is `verified-fixed` only against the original resolution criterion
  with independent confirmed evidence. Omission, a missing line, or a
  resolved UI thread is `omitted-uncertain`.
- A new material defect on a changed or related path may be
  `new-regression` with a causal parent fingerprint when a baseline blocker
  sits on that change.
- A substantiated defect on already-reviewed PR scope may be
  `substantiated-missed-defect`.
- Preference on already-reviewed code stays advisory.
- Unattributed target-branch issues stay `pre-existing`.
- Ambiguous identity matches require human adjudication.

Rebase, base SHA, model, engine, profile, policy digest, and
stage/context/learning digest changes invalidate the baseline. Invalidation
falls back to a full bounded review and does not treat the new pass as late
relative to untrusted evidence. Round counters are not reset here (C3/C5).
`legacy` stays unscoped. Publication is not refused (C5).

Doctor and plan display the verification scope. `admit_review_result` applies
the classification when a baseline is supplied and candidates are not.

## Scope

In scope:

- Baseline and verification-scope types, closed v1 schemas, classification
  and lineage evaluators, and IncrementalReviewPlan construction.
- Decision tests for cross-file effects, omission, deduplication, rebase,
  and model/policy invalidation.
- Doctor/plan display and admission wiring.

Out of scope:

- Host enforcement of round budgets (C5), maintainer commands (C6), and
  evaluation rollout (C7).
- Changing `REVIEWSENSEI_AUTO_APPROVE`, GitHub permissions, or recreating
  #114/#115 gates.
- Persisting findings inside the C3 session ledger.

## Consequences

Positive:

- Verification scope is explicit and reuses existing coverage identities.
- Late blockers carry a trusted reason and optional causal lineage.
- Unjustified new merge prerequisites on already-reviewed code stay advisory.

Tradeoffs:

- A caller still has to supply a complete prior `ReviewResult` to classify
  findings; C3 stores counters only.
- Directory siblings are a bounded stand-in for impact when symbol-aware
  related paths are not supplied.

## Alternatives considered

### Restart a full unrestricted review after every push

Rejected because issue #136 requires fix verification plus newly
changed/impacted code, not another complete discovery pass.

### Treat model omission as proof a concern is fixed

Rejected by ADR 0042 and restated here: omission is uncertain.

### Store findings in the C3 session comment

Rejected because C3 is a counter ledger without source or findings. C4
classifies from the last complete review object the caller already has.

## Validation

Run verification-scope, classification, schema golden/negative, doctor/plan,
and admission tests. Ordinary CI stays offline and credential-free.

## Rollout and rollback

Operator modes opt in through the existing review-mode contract. Rollback by
omitting `baseline=` from admission and ignoring the new plan fields.
`legacy` is unchanged.

## Follow-up work

- C5: automation admission and handoff using the C3 ledger.
- C6–C7 as specified in issue #136.

## Links

- Related issue: [#136](https://github.com/malsabbagh/review-sensei/issues/136)
- Pull request: [#140](https://github.com/malsabbagh/review-sensei/pull/140)
- Related: [ADR 0037](0037-symbol-aware-source-context.md),
  [ADR 0042](0042-incremental-reviews-and-finding-lifecycle.md),
  [ADR 0046](0046-evidence-based-blocker-admission-and-review-loop-convergence.md),
  [ADR 0047](0047-durable-review-session-ledger.md)
- Supersedes: none
- Superseded by: none
