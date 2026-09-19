# ADR 0049 - Automation admission and handoff

Status: Proposed
Date: 2026-09-19
Last amended: 2026-09-19
GitHub Issue: #136
Pull Request: [#141](https://github.com/malsabbagh/review-sensei/pull/141)
Owners/Reviewers: Maintainers
Approved by: not applicable

## Context

Issue #136 C5 requires the host to enforce the C1 round budget before
inference and before a new automated GitHub review event. C3 persists
PR-wide counters; C4 classifies later findings. Neither refused work.
Without this slice, a later Actions job can still start another model
call and emit another `REQUEST_CHANGES` after the allowance is gone.

Same-head duplicates, in-flight reservations, failed attempts, and
contradictory A→B→A advice must not mint extra rounds. The last allowed
round may still approve when independent eligibility already passes. The
cap itself never creates approval. Maintainer-authenticated GitHub
commands and non-passing required checks remain C6.

## Decision

When an operator mode (`advisory`, `merge-focused`, `strict`) has a
session ledger:

- `prepare_session_round` evaluates `evaluate_round_admission` with
  pause, duplicate, no-progress, and `--continue-rounds 0|1` flags
  before any provider call or `ReviewPublisher.publish`.
- A foreign in-flight reservation pauses the job (`handoff_reason=paused`).
- The same `reservation_id` after commit is a same-head duplicate: no
  new inference, no extra counted round, but an already-produced result
  may still be published.
- Unadmitted operator rounds are not reserved. Inference exits
  `skipped_policy` with `provider_calls=0`. Publication of a new
  automated review is skipped unless `may_emit_approve` is already true.
- Provider or publication failure aborts a held round reservation and
  charges `failed-attempt`. Failed attempts are bounded separately from
  completed rounds.
- `--continue-rounds 1` admits one extra verification round for that
  invocation only after the latest-head and coverage gates pass. Workflows
  must not pass it by default.
- `--no-progress REASON` is accepted only with an operator mode and an
  enabled local or GitHub-backed session ledger. The bounded printable reason
  is an operator attestation for this invocation; it is not persisted or
  echoed as trusted finding state.
- `detect_no_progress` treats a repeated non-empty blocking identity set
  or A→B→A oscillation as no-progress. Empty current sets are progress.
  C5 accepts the attestation as caller-supplied state; the GitHub publication
  adapter does not persist prior finding identities or infer no-progress on
  its own in this slice.
- `legacy` is unchanged. Without a ledger, operator modes cannot enforce
  and doctor reports action required. `REVIEWSENSEI_AUTO_APPROVE` and
  #114/#115 gates are unchanged.

C6 will publish a clearly non-passing required check for handoff. C5
maps handoff onto the existing `skipped_policy` run outcome plus a public
diagnostic token so CLI/GitHub callers see the reason without inventing
a second success meaning.

## Scope

In scope:

- Round-admission enforcement at inference and GitHub publication.
- Pause, duplicate, failed-attempt, no-progress, and bounded continuation.
- Doctor/plan automation-admission display.

Out of scope:

- Maintainer `@sensei` commands, conversation-resolution behavior, and
  required-check `action_required` (C6).
- Sequential evaluation, shadowing, and default-mode promotion (C7).
- Changing `REVIEWSENSEI_AUTO_APPROVE` or recreating #114/#115 gates.

## Consequences

Positive:

- Autonomous review/fix-verification loops stop at the reserved
  allowance instead of emitting another unsolicited blocker list.
- Concurrent jobs coalesce on the in-flight reservation.
- An independently eligible last-round result can still finalize
  `APPROVE`; exhaustion cannot.

Negative:

- Enforcement is opt-in on the C3 ledger. Operator mode without a ledger
  remains unbounded until the operator supplies one.
- Inference holds a reservation until publication commits, so a
  successful local-only review can pause the next job on the same head.

## Alternatives considered

### Count only at GitHub publication

Rejected. C5 requires admission before inference so a refused round
makes zero provider calls.

### Treat exhaustion as automatic approval

Rejected by issue #136: the cap never creates eligibility.

### Reset counters on a new head SHA

Rejected by C3: head is not the session key. A new head is a new
reservation identity for duplicate detection, not a budget reset.

## Validation

Run round-admission, session, GitHub publication skip, CLI inference
skip, failed-attempt, doctor/plan, and schema tests. Ordinary CI stays
offline and credential-free.

## Rollout and rollback

Operator modes opt in through the existing review-mode contract plus a
session ledger. Rollback by omitting the ledger or returning to
`legacy`. Unresolved findings are not discarded and a withheld decision
is not converted into approval.

## Follow-up work

- C6: maintainer disposition and non-passing handoff statuses.
- C7: sequential evaluation and opt-in rollout.

## Links

- Related issue: [#136](https://github.com/malsabbagh/review-sensei/issues/136)
- Pull request: [#141](https://github.com/malsabbagh/review-sensei/pull/141)
- Related: [ADR 0046](0046-evidence-based-blocker-admission-and-review-loop-convergence.md),
  [ADR 0047](0047-durable-review-session-ledger.md),
  [ADR 0048](0048-baseline-aware-verification.md)
- Supersedes: none
- Superseded by: none
