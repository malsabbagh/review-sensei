# ADR 0123: Observe hosted lifecycle outcomes without granting completion

- Status: Proposed
- Date: 2026-11-01
- Decision owner: ReviewSensei maintainers
- Reviewers: Independent technical review requested
- Linked GitHub Issue: [#161](https://github.com/malsabbagh/review-sensei/issues/161)
- Linked PR: Pending publication

## Context

Follow-up investigations of ReviewSensei403, SessionVault122 and SiteVault141
found retained publication success without authenticated caller completion.
Later commands correctly refused the unfinished predecessor. The original logs
and absent optional artifacts did not identify a precise tail operation. Other
failures occur before a transaction exists or during broker/policy admission;
these must remain distinguishable. None establishes a shared low-level cause.

## Decision

Wrap the four hosted CLI lifecycle routes in a context-local, stdlib-only
diagnostic collector. Existing bootstrap and execution boundaries supply facts
already available in memory. Emit one versioned JSON stderr line bounded to
4096 UTF-8 bytes including prefix and newline, independently of artifacts.
Rendering or sink failure cannot replace the original result or exception.
Hard kills may still prevent emission.

Keep requested, bootstrap-observed, admitted, frozen transaction and predecessor
identities separate. Default policy identity is distinct from the reviewed PR
base. Record missing values as null and transition/recovery decisions as
unevaluated. Use closed labels and validated broker diagnostics; omit raw text,
provider data and credentials. Counts describe recorded state, not a complete
historical effect proof. No business decision reads collector state.

## Scope

Hosted CLI and review-run observation hooks, a new diagnostic module, operator
documentation, source regressions and an installed-wheel contract. Existing
accounting, completion acceptance, ordinary rollover, historical recovery,
permission checks, schemas, budgets, release versions and workflows are unchanged.

## Consequences

Future failures can retain the last observed stage, original scope, predecessor
binding and available accounting even when artifacts are disabled. A later
refusal cannot infer a predecessor's missing tail cause. Oversized or unavailable
observations use a minimal explicit fallback. The line is diagnostic data,
never a terminal witness, recovery grant or native approval receipt.

## Alternatives considered

Adding fields only to optional artifacts leaves `ARTIFACTS=none` incidents
opaque. Instrumenting only the engine misses bootstrap and command admission.
Passing a collector through every execution object would enlarge many authority
interfaces for observational work. A context-local sink confines this change
to entry and existing phase boundaries without process-global cross-run reuse.
Changing completion status allowlists would alter authority and is deferred.

## Validation

Public CLI tests preserve exits, original roots, counters, unknown effects and
zero native mutation on refusals. Three independent captured roots retain their
original identities, policy and clock gaps. Synthetic controls cover broker
upstream refusal, no-transaction predecessors, non-policy default-branch drift,
withheld approval with caller completion, uncertain cleanup/close, outcome-file
failure, cancellation, privacy, bounded fallback and nested context isolation.
Run the full checkout, static, distribution and installed/downstream gates;
record actual results in the implementation PR.

## Rollout and rollback

Land through the ordinary reviewed package change. This PR performs no release,
deployment or recovery activation. Revert the CLI context and observation hooks
to stop emission; no persistent migration or receipt deletion is necessary.

## Follow-up work

Qualify typed prospective completion across every selected tail. Separately
coordinate authenticated generation selection and old-write isolation for base
retarget, base advance and force-push. Evaluate bounded policy-admission refresh
without stale authority. Historical reconciliation remains with its existing
owner and requires independent provenance, effect and funding evidence.
