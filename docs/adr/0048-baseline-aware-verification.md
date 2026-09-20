# ADR 0048 - Baseline-aware verification

Status: Proposed
Date: 2026-09-19
Last amended: 2026-09-20
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
  with independent confirmed evidence. The confirmation token must equal the
  stored criterion digest; a bare boolean is rejected. Omission, a missing
  line, or a resolved UI thread is `omitted-uncertain`.
- A new material defect on a changed or related path may be
  `new-regression` with a causal parent fingerprint when a baseline blocker
  sits on that change.
- A changed-path finding without a trusted causal parent remains
  `needs-human`; the changed path alone does not prove that the fix caused the
  defect.
- A substantiated defect on already-reviewed PR scope may be
  `substantiated-missed-defect`.
- Preference on already-reviewed code stays advisory. The preference signal is
  intentionally limited to the normalized category IDs `style`, `nit`, and
  `preference`; other free-form category names are not treated as preferences.
- Unattributed target-branch issues stay `pre-existing`.
- Ambiguous identity matches require human adjudication with an explicit
  `human-adjudication` late reason; every late classification carries a reason.
- Evidence confirmations are keyed by the stored concern digest. A baseline
  finding without that stable digest cannot be auto-confirmed; callers must
  provide an explicit evidence-backed candidate instead.
- The related-path helper intentionally reports same-directory siblings among
  the changed set; callers union that impact context with the changed paths in
  the incremental plan.

The durable session record supplies the baseline only after transaction
checkpointing validates a completed review. The transaction-enabled CLI
(including an explicit `--transaction`) restores that serialized baseline
before each later admitted round and passes the resulting
`IncrementalReviewPlan` into `ReviewService.run`. Missing, incomplete, or
incompatible durable history is an `action_required` recovery outcome before a
provider call or a new reservation charge; it is never treated as fresh
initial-review authority. A successful verification on that CLI path
checkpoints the next baseline atomically with the validated result. In this F3
slice the GitHub application only restores and forwards the baseline for
hosted admission; hosted write-through checkpointing remains follow-up F5
work.

Rebase, base SHA, model, engine, profile, policy digest, and
stage/context/learning digest changes invalidate the baseline. Invalidation
falls back to a full bounded review. Its baseline-derived classifier returns
`needs-human` with an explicit `human-adjudication` late reason because an
invalidated or incomplete baseline cannot prove that a later finding was
missed; that path remains fail-closed for evidence and still requires the
normal human/evidence gates. Round counters are not reset here (C3/C5).
`legacy` stays unscoped. Publication is not refused (C5).

Doctor and plan display the verification scope. A preview based only on the
session counter never reports `verify`, because it cannot prove baseline
compatibility; only a complete compatible `ReviewBaseline` can authorize
late admission. `admit_review_result` applies the classification when a
baseline is supplied and candidates are not. That baseline-derived path
remains fail-closed for C2 evidence; callers with trusted evidence must supply
explicit blocker candidates, which take precedence over derived baseline
candidates.

## Scope

In scope:

- Baseline and verification-scope types, closed v1 schemas, classification
  and lineage evaluators, and IncrementalReviewPlan construction. A complete
  baseline must carry complete coverage; caller-supplied reviewed paths may
  constrain a fallback scope but do not establish a legacy baseline. A result
  with no coverage stays incomplete, and a clean result with neither cannot
  establish a baseline.
- Decision tests for cross-file effects, omission, deduplication, rebase,
  and model/policy invalidation.
- Doctor/plan display and admission wiring.

Out of scope:

- Host enforcement of round budgets (C5), maintainer commands (C6), and
  evaluation rollout (C7).
- Changing `REVIEWSENSEI_AUTO_APPROVE`, GitHub permissions, or recreating
  #114/#115 gates.
- The bounded durable baseline representation itself (delivered by #146 F2).

## Consequences

Positive:

- Verification scope is explicit and reuses existing coverage identities.
- Late blockers carry a trusted reason and optional causal lineage.
- Unjustified new merge prerequisites on already-reviewed code stay advisory.

Tradeoffs:

- Durable recovery depends on the completed, compatible baseline checkpoint;
  incomplete or incompatible history requires explicit operator recovery.
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

- #146 F4: attested continuation grants and hosted session mutation.
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
