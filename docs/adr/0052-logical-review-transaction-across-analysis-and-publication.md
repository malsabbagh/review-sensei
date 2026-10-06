# ADR 0052 - Logical review transaction across analysis and publication

Status: Proposed
Date: 2026-09-19
Last amended: 2026-10-06
GitHub Issue: #146
Pull Request: not configured
Owners/Reviewers: Maintainers
Approved by: not applicable

Status note (2026-09-26): the one-use continuation grants this record lists as
persisted state are retained as inert history only
([ADR 0056](0056-remove-pr-wide-review-count-caps.md), epic #181 C). The
identity-bound analysis checkpoint, publication phase, and publication-only
recovery this record describes are unchanged.

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

## Amendment: committed analysis is not successful publication (2026-10-06)

Review: [PR223](https://github.com/malsabbagh/review-sensei/pull/223), maintainers;
the record remains Proposed pending maintainer acceptance. The hosted review
completed analysis but failed creating its GitHub review. A single workflow retry
then classified the committed round as `already_published` and attempted to open
a missing result. The original transport/HTTP cause was not retained.

The existing transaction decision still applies: one completed analysis consumes
one round, and publication retries must reuse its identity-bound result. An
identical operator-ledger analysis retry with pending/failed publication now
reports `action_required: publication_recovery_required`, without inference,
ledger mutation, or budget reset. A succeeded transaction remains an idempotent
skip, and local analysis-only sessions retain their duplicate behavior.
Reusable runners clear their prior output files and publish only when analysis
produces a new result. They preserve the live-head and marker-reconciliation
gates; an ambiguous review-create response does not authorize another blind POST.

Explicitly enabled diagnostics retain the result, recovery artifact, outcome,
reviewed diff, and trusted configuration/admission contexts after publication
failure, under the existing seven-day limit. This adds source-bearing recovery
inputs to that opted-in bundle; `artifacts: none` still stores none. Workflow
authentication credentials, raw provider responses, and prompts are not added;
the reviewed source itself can contain sensitive data, so retention is an
operator decision. The ledger stores only its
existing bounded state and digests, never the result or diff. Missing output
cannot be reconstructed from findings or digests. Abandoned-analysis recovery
does not apply to completed analysis.

Rejected alternatives: repeat inference or reset the session budget; reconstruct
the result from ledger findings; unconditionally retain source artifacts; or
loop over an ambiguous review-create POST. Each loses identity, budget, privacy,
or single-publication guarantees. Safe cause diagnostics expose only a transport
category or numeric HTTP status, not response bodies or exception chains.

Validation covers pending/failed/succeeded retries, zero provider calls and
unchanged ledger state, changed contexts, local-session compatibility, stale
runner outputs in both lanes, and sanitized HTTP/transport errors. The full
offline suite, clean installed distribution, native parity, and exact-head CI
remain release gates. Package publication and reviewed `v5` promotion are separate
operator actions; the old installed runtime does not execute PR source. Rolling
back this correction restores the misleading skip/missing-file behavior and
failure-only artifact loss, but requires no ledger migration. Rollback never
authorizes a budget reset or retry without the original bound result.

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
