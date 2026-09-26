# ADR 0056 - Remove the PR-wide review count cap

Status: Proposed
Date: 2026-09-26
GitHub Issue: #181 C
Owners/Reviewers: Maintainers

Status note: this record stays `Proposed` deliberately at merge time, matching
the pre-acceptance convention of ADR 0055. The ADR process requires explicit
maintainer approval to mark a record `Accepted`
(`docs/process/adr-process.md`); the epic's own progress record on issue #181
carries the matching `Proposed` status.

## Context

The review-loop policy (ADR 0046) bounded every pull request with lifetime
counters: one completed initial review, a five-round default and eight-round
ceiling for verification rounds, and a 32-round validation bound. The bound
was enforced twice — as a workflow pre-check (`hosting/github/budget_admission.py`)
that skipped the review command when the allowance was spent, and as runtime
admission over the same ledger counters — with one-use continuation grants and
`@sensei review continue --rounds 1` as the only way to replenish it.

The cap conflated a lifetime count with consent. A pull request that was
legitimately updated forty times, each update reviewed and published, would be
refused round forty-one even though every fact that matters — the head is new,
coverage is complete, no unresolved concern was ignored — was satisfied. Issue
#181 requires the count gate removed: automatic updates must stay reviewable
for as long as the pull request lives, and refusal must come only from live
conditions.

## Decision

There is no PR-wide review count admission. `max_completed_initial_reviews`
and `max_completed_verification_rounds` are removed from the convergence
policy, its schema, and admission. The workflow budget-admission step is
deleted: the reusable runner starts the review CLI directly, while the package
still enforces work admission immediately before inference and rechecks
publication identity and authorization at the write boundary. A handoff
artifact or prior preflight is never independently trusted merely because it
exists.

No command grants an allowance. `@sensei review continue --rounds N` and
`@sensei review reenroll --rounds N` are not recognized and are not silently
reinterpreted as a plain continue: honouring the retired spelling would
misrepresent what the engine will do. One-use continuation grants are no
longer consumed; grant history already stored in a session record is retained
verbatim as inert evidence, bounded and digest-protected like every other
field.

What still bounds a round is a live condition, and each remains in force:

- per-invocation resource ceilings (provider calls, transport and structural
  retries, prompt/output bytes, elapsed time) and cancellation;
- the per-head failed-attempt retry bound, scoped to one head SHA so a changed
  head or an authorized re-run starts a fresh bound — it is not a cumulative
  lock;
- rate limiting, replay protection, duplicate suppression, and single-use
  reservations;
- operator pause, no-progress, and coverage/incomplete-coverage gates;
- publication identity and authorization, rechecked at the write boundary.

The ledger counters `completed_initial_reviews` and
`completed_verification_rounds` remain, but they are diagnostic history:
bounded for storage (0..1,000,000) and never consulted for admission. A record
whose counters exceed the retired 8/32 thresholds is valid history and cannot
be rejected for exceeding them.

Public diagnostics drop `round-budget-exhausted`; the live-cause diagnostic
`failed-attempt-budget-exhausted` remains and now covers retry-bound handoffs
only. Per-invocation exhaustion reports an incomplete/handoff outcome, never
approval and never a fabricated code defect. Publication recovery continues to
reuse the prior validated result and performs no new inference.

Upgrade behavior: a session that a count-capped install had paused because its
allowance was exhausted is admitted on the next eligible trigger. The obsolete
reason disappears while independent facts persist — a manual operator pause
keeps blocking until continued, dispositions and counters survive, and the
retained grant history is untouched. A resumed run never deletes history, and
lost established state requires repair; it must not manufacture an empty clean
review.

## Scope

Applies to the convergence policy and admission, the session ledger and its
schemas, the maintainer command surface, the reusable runner and generated
callers, and the public diagnostics/exit vocabulary. Resource budgets from
issue #36 and every per-invocation ceiling are out of scope and unchanged.
The frozen v4 caller fixtures keep their historical `--rounds` regex as
byte-exact history; only current templates change.

## Consequences

A legitimate changed-head update is reviewable for the lifetime of the pull
request, including sequences well past the former 5/8/32 thresholds across
fresh processes. Refusals become explainable as live causes, and
`failed-attempt-budget-exhausted` no longer doubles as a lifetime verdict.
Consumers that matched `round-budget-exhausted` or the removed
remaining-allowance fields must update; the change is deliberate and appears
in the changelog. Stored state stays forward-readable: old records with counts
at or above the retired ceilings, retained grants, and pauses all load and
behave per the upgrade rules above.

## Alternatives considered

### Keep the cap and raise the ceiling

Rejected: any fixed ceiling eventually refuses a legitimate update, and the
epic's acceptance case (forty successive reviews, fixture length not a product
limit) would merely move the wall.

### Replace the cap with a cumulative failed-attempt lock

Rejected: a cumulative lock permanently prevents future changed-head or
authorized reviews. Retry bounds must stay per invocation and per head, so
automatic retries of the same logical invocation remain bounded without
locking the pull request.

### Silently treat `--rounds` as a plain continue

Rejected: the operator asks for a budget that no longer exists; honouring the
spelling would misstate what the engine will do. The retired option is
rejected with an actionable message instead.

## Validation

Long-sequence and upgrade tests live in `tests/test_count_cap_removal.py`:
forty successive changed-head reviews across fresh processes (crossing the
former 5/8/32 thresholds, admission never failing on lifetime count), an old
count-exhausted record admitted on the next changed head, manual pause and
dispositions surviving the upgrade, counters beyond the old thresholds treated
as valid history, and the retired diagnostic absent from public vocabulary.
Round-admission, session, workflow-policy, disposition, and schema tests are
updated together with the change and stay offline and credential-free.

## Rollout and rollback

Removal ships as slice C of the epic on issue #181 in one reviewable change
covering engine, policy, schemas, commands, workflow callers, and tests.
Rollback is a forward version change that reintroduces a count policy; no
in-place downgrade is provided, because a restored cap could refuse rounds a
pull request has already legitimately completed.

## Links

- Related issue: [#181](https://github.com/malsabbagh/review-sensei/issues/181)
- Related: [ADR 0046](0046-evidence-based-blocker-admission-and-review-loop-convergence.md),
  [ADR 0047](0047-durable-review-session-ledger.md),
  [ADR 0049](0049-automation-admission-and-handoff.md),
  [ADR 0050](0050-maintainer-disposition-and-handoff-status.md)
- Amends: ADR 0046 (round budgets), ADR 0049 (count admission), ADR 0050
  (`--rounds` continuation), ADR 0052 (one-use grants), ADR 0053 (grant-bounded
  history)
- Supersedes: none
- Superseded by: none
