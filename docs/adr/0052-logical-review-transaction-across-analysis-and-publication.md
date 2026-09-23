# ADR 0052 - Logical review transaction across analysis and publication

Status: Proposed
Date: 2026-09-19
GitHub Issue: #146
Pull Request: not configured
Owners/Reviewers: Maintainers
Approved by: not applicable

## Context

Issue #146 F1 found that analysis and publication independently reserved the same PR round, leaving a successful analysis held and publication paused. The existing durable session ledger stores counters and reservations but no identity-bound completed-analysis checkpoint or publication phase.

## Decision Drivers

- Keep the existing provider-neutral engine and session-ledger adapters.
- Count one logical review round once, independently of GitHub write retries.
- Bind the serialized result to trusted repository/PR/head/base/policy and
  configuration identity without persisting prompts, source, or provider data.
- Make competing, stale, duplicate, and partial/crashed paths fail closed.

## Decision

Persist a bounded ReviewTransaction in the validated ReviewResult and SessionRecord. Reserve once before inference, checkpoint the validated result and count the logical round once, and transition publication pending/failed/succeeded with generation-checked mutations. Publication must match the durable repository/PR/base/head/policy/configuration/reservation/generation/result identity before any write.

## Scope

In scope:

- The bounded transaction value and its v1 schema representation.
- CAS-checked analysis checkpoint and publication phase transitions.
- CLI and GitHub application handoff when an operator session ledger is used.
- Focused negative-path and retry regressions.

Out of scope:

- Changing the installed `legacy`/operator default or retiring legacy execution;
  those are F7 requirements.
- Persisting raw prompts, diffs, model responses, or a new database/service.
- Workflow/setup migration and hosted release promotion; those belong to F5/F7.

## Consequences

Positive:

- A successful analysis cannot leave its own reservation blocking publication.
- Publication retries reuse the validated result and do not make another model
  call or increment completed-round counters.
- The ledger retains only bounded identity, digest, phase, and CAS metadata.
- Existing no-ledger and legacy callers remain compatible.

Negative or tradeoffs:

- The new operator path requires a matching identity-bound transaction artifact.
- The GitHub issue-comment adapter retains its existing best-effort
  cross-writer limitation; deployment serialization remains required for a
  strict distributed lock.
- The reservation and transaction-attachment mutations are separate CAS
  writes. The transaction-enabled and legacy writers must therefore be
  serialized per `SessionIdentity`; running both concurrently can admit a
  competing reservation before the transaction is attached.
- The session record and review-result schemas gain an optional field and the
  record-size budget must continue to be enforced.

## Alternatives Considered

### Commit the reservation before publication without a transaction artifact

- Summary: Count the analysis by committing the existing reservation, then let
  publication rely on the deterministic reservation id.
- Why not chosen: The result, effective configuration, and exact head/base are
  not durably bound; a caller could replay a different serialized result.

### Separate transaction store

- Summary: Add a database or hosted service for analysis/publication state.
- Why not chosen: It creates a new deployment and operational boundary that is
  unnecessary for the bounded metadata already owned by the session ledger.

## Implementation Notes

- `ReviewTransaction` is validated by runtime code and a closed v1 schema.
- The configuration digest is canonicalized from the effective provider
  identity (provider name/profile, endpoint, timeout/output budget, and routing
  policy), model, stage identities, category policy, orchestration flags, and
  publication mode; raw prompts, stage file paths/schemas, review limits, and
  credentials are excluded because publication does not re-run analysis and
  the canonical result digest binds the bounded output. Publication receives
  a trusted immutable context with the same shape and recomputes both
  configuration and evidence digests. The configuration and evidence context
  passed to `GitHubApplication.publish_review` is a trusted admission input
  from the authenticated workflow/operator boundary, not untrusted request
  data; integrations crossing that boundary must derive it from their own
  trusted checkout and effective settings.
- The analysis CLI preserves the existing ledger reservation behavior unless
  the caller explicitly passes `--transaction` (or the artifact-producing
  `--configuration-context-output`, which implies it). This keeps the F1
  transaction opt-in independent from whether the current host can write a
  context file; publication still requires the equivalent trusted context at
  its boundary. A caller that requested those artifacts is publishing from
  them, so a round that cannot be checkpointed exits non-zero instead of
  reporting success without the files it promised.
- Policy and evidence digest inputs use closed, versioned identity shapes.
  Caller-supplied context keys outside those shapes are rejected before any
  digest is accepted for publication admission.
- The result digest is computed over an explicit frozen projection of the
  canonical v1 review result with the transaction envelope excluded, avoiding
  a circular digest. Runtime-only additions to `to_dict()` are ignored; any
  additive or semantic change to the projected result fields is a
  digest-contract change and must be versioned with the public
  schema/compatibility policy.
- The session record retains the transaction phase and result digest but never
  stores the result body.
- Analysis failure clears the in-flight transaction, releases the reservation,
  and charges the existing failed-attempt path; the `analysis_failed` schema
  phase is reserved for a future explicit terminal record. Cancellation and
  abandoned-reservation cleanup require the original reservation owner and
  expected generation, including the reservation-only state left by a crash
  between reservation and transaction attachment. A completed analysis transitions to
  `publication_pending`; publication failures remain retryable and success is
  idempotent.

## Validation And Rollout

- Validation: Run focused transaction/session/application/CLI regressions,
  schema validation, the full offline test suite, and the repository quality
  commands. Exercise competing, stale, duplicate, disabled-write, and
  crash-between-phases paths.
- Rollout: F1 is ledger-gated and does not change the installed default. Later
  F5/F7 work will wire the hosted workflow and perform the reviewed migration.
- Rollback: Revert the F1 code/schema/ADR changes or omit the operator ledger;
  existing legacy/no-ledger publication remains the compatibility path.

## Follow-Up

- F2 persists the minimum compatible review history and prevents budget reset.
- F3 feeds the saved baseline into real analysis and blocker admission.
- F4 persists authenticated dispositions and one-use continuation grants.
- F5 wires the supported workflow/CLI/storage handoff, F6 adds observed
  end-to-end evidence, and F7 performs the required default replacement and
  legacy retirement.

## Links

- Related issue: #146
- Related PR: not configured
- Supersedes: none
- Superseded by: none
