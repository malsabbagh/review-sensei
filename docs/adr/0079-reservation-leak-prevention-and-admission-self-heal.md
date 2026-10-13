# ADR 0079 - Reservation leak prevention and admission self-heal

Status: Proposed
Date: 2026-10-12
GitHub Issue: Pending triage
Pull Request: [#292](https://github.com/malsabbagh/review-sensei/pull/292), [#293](https://github.com/malsabbagh/review-sensei/pull/293), [#294](https://github.com/malsabbagh/review-sensei/pull/294)
Owners/Reviewers: Maintainers
Approved by: not applicable

## Context

ADR 0063 made abandoned analysis reservations recoverable, but only through
an explicit maintainer `@sensei review continue`. Two leak paths remained
when a push lands during analysis and the old run is cancelled:

- GitHub cancellation escalates to `SIGTERM`, whose default action killed
  the review process before its reservation abort could run.
- When cleanup did run after the head had moved, the ledger adapter
  refused the owning run's own `abort()` at the live-head check, even
  though releasing a reservation publishes nothing.

A lost runner gets no cleanup at all, so its reservation blocked every
later review until session expiry or a maintainer command.

## Decision

Prevent leaks first, then heal the rest at admission. The mechanism is
implemented across the three stacked pull requests listed above: leak
prevention (#292), admission self-heal (#293), and pause/resume clarity
plus this record (#294).

- `SIGTERM` is delivered as `KeyboardInterrupt` in the engine and hosted
  entry points, so cancellation unwinds through the existing abort
  cleanup. Workflow review steps forward TERM/INT from the bash wrapper
  to the Python child.
- The ledger adapter allows one narrow exception to the live-head
  check: a run may release (`abort`) a reservation whose broker-recorded
  `reservation_owner` matches its own verified run, even after the head
  has moved. The broker supports this with a `release_only` session
  exchange that returns the capability token and verified owner after a
  head move, records no enrollment witness, and issues no mutation
  grant. Every other mutation, including non-owner aborts and all
  publication writes, keeps the live-head check.
- An always-run cleanup step in the hosted and local jobs calls the
  owner-only `release-reservation` command: it releases only when this
  run owns the reservation and no publication transaction was saved.
- As a backstop, review admission runs the ADR 0063 recovery checks
  when it finds a held reservation: a provably completed owner run is
  recovered in the same job (one failed attempt charged to the old
  head, current head then reviewed); a still-running owner is waited on
  briefly (bounded) and then reported as blocked by that run; unclear
  evidence stays blocked without mutation. Saved publication work is
  never reclaimed.

Authorization envelope: admission self-heal is not a new trust
boundary. It runs the identical evidence seam as the maintainer
command — broker-verified session grant, GitHub Actions run evidence
proving the owner run completed, exact reservation/digest/generation
recheck, one CAS replacement, one failed attempt charged to the
abandoned head. The only differences are the grant's operation
(`review` instead of `command`) and that admission never unpauses.
A PR trigger therefore cannot steer recovery: without a completed
owner run proven through Actions evidence, admission stays blocked
exactly as before, and a maintainer pause stops admission before
any recovery is attempted.

This supersedes ADR 0063's rule that recovery requires an explicit
maintainer `continue`. The pause contract is unchanged and stricter:

- `@sensei review pause` is never cleared automatically — not by
  pushes, and not by admission self-heal. Admission checks the pause
  before attempting any recovery.
- `@sensei review continue` still unpauses and recovers any leaked
  reservation through the same seam. Chaining a review of the current
  head into the same workflow run remains a follow-up: the command and
  review operations are statically separate jobs in the reusable
  workflow, and admission self-heal already makes the next triggered
  review recover without a maintainer.
- Status messages tell the cases apart: "paused by maintainer,
  comment @sensei review continue to resume", "blocked by run X, still
  running", and "blocked by a held reservation, owner unknown" for
  legacy records. When several conditions hold at once the clauses
  compose in that order (pause first) instead of hiding one another;
  `docs/diagnostics.md` enumerates every form with operator guidance.

## Consequences

Positive:

- A cancelled or killed review no longer blocks the next review of the
  current head; the common push-during-analysis case needs no
  maintainer action.
- The live-head publication fence is unchanged everywhere it protects
  a write that publishes.

Negative or tradeoffs:

- Admission recovery spends bounded Actions reads on the
  held-reservation path only.
- The broker `release_only` exchange is additive and backward
  compatible; it must be deployed before clients rely on it.
