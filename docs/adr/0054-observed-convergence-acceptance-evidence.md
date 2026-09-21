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
or approval inputs. Missing material, unjustified late material findings,
duplicate normalization, reopens, and contradictions are measured. A metric
without an exercised fixture remains `null` and makes cutover readiness
`not_ready` rather than implying success.

For the fixed #146 deterministic integration candidate, the observed report
passes its proposed F6 gate only when it records all of the following:

- exact source, package, workflow, policy/configuration, fixture, and command
  identities;
- at least one successful application publication and a later fresh job that
  loads the durable completed baseline;
- a maintainer-labelled material regression with no missed or unjustified
  material blocker; no duplicate, reopen, or contradiction pending human
  adjudication;
- the complete configured initial plus verification budget, followed by an
  over-cap request that makes zero provider calls and creates no approval;
- observed shadow-state isolation and durable pause/continue command results.

These are deterministic integration thresholds, frozen for this candidate.
They do not claim real-world model recall or override #33's separate
maintainer-reviewed real-provider evidence requirements when model, prompt,
or routing behavior changes.

Shadow comparison remains an isolated validation technique. It must not share
the enforced ledger, publisher trace, grants, counters, or command path.

## Consequences

The C7 simulator stays available and explicitly limited. The observed report
is additive evidence, not a policy switch, release action, or claim of
real-world model recall. A `passed` verdict means this bounded F6 integration
gate passed; F7 still owns the default replacement, supported-installation
migration, and operator-controlled release evidence.

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
