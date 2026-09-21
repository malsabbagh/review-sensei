# ADR 0054 - Observed convergence acceptance evidence

Status: Proposed
Date: 2026-09-20
GitHub Issue: #146 F6
Owners/Reviewers: Maintainers

## Context

The C7 sequence replay is a deliberately lower-level simulator. Its callers
supply blocker identities and approval eligibility, and it does not execute
the hosted application, durable ledger, publisher, or finalizer. It therefore
cannot establish the acceptance evidence required before the #146 default
replacement.

## Decision

Provide a distinct, bounded observed-sequence harness alongside the simulator.
For each logical job it recreates the review service, GitHub application,
publisher/finalizer, and ledger adapter. The model and GitHub HTTP transport
are fixture edges; all admission, baseline, command, publication, and
finalization decisions remain production code.

The public observed report records only bounded fixture identities and
decision evidence: per-job provider calls, baseline load, publication status,
captured approval events, durable round/work counters, and command results.
Finding quality compares maintainer-labelled material identities with fixture
model output. Labels are evaluation oracles only; they never become admission
or approval inputs. Missing material and unjustified late material findings
are measured. Metrics without a fixture that exercises them, including cap,
duplicate, reopen, and contradiction behavior, are `null` and make cutover
readiness `not_ready` rather than implying success.

Shadow comparison remains an isolated validation technique. It must not share
the enforced ledger, publisher trace, grants, counters, or command path.

## Consequences

The C7 simulator stays available and explicitly limited. The observed report
is additive evidence, not a policy switch, release action, or claim of
real-world model recall. The report's cutover verdict stays `not_ready` until
the fixed #146 acceptance metrics and thresholds are exercised and recorded.

## Validation

Run the observed-sequence, schema, publisher/finalizer, installed-artifact,
and full repository regressions. Inspect the generated report for explicit
unknown metrics before relying on it for cutover decisions.

## Links

- [Issue #146](https://github.com/malsabbagh/review-sensei/issues/146)
- [ADR 0048](0048-baseline-aware-verification.md)
- [ADR 0049](0049-automation-admission-and-handoff.md)
- [ADR 0050](0050-maintainer-disposition-and-handoff-status.md)
- [ADR 0051](0051-sequential-evaluation-and-shadowing.md)
