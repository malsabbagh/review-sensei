# ADR 0060 - Stale-baseline full-review recovery

Status: Proposed
Date: 2026-10-05
Owners/Reviewers: Maintainers
Approved by: not applicable
GitHub Issue: none; source-confirmed 0.6.13 reproduction
Pull Request: pending

## Context

The 0.6.13 transaction CLI rejects `plan_verification_scope`'s `fallback-full`
plan with `durable_baseline_recovery_required` before inference. A legitimate
base change therefore has no supported automatic recovery: continue preserves
the old baseline and reenrollment rejects an unexpired healthy session.
Head-only reservation identity also suppresses changed-base work on the same
head. Artifact-free hosted jobs print only `action_required`, hiding the reason.

The reported Site PR 61 job [111630097843](https://github.com/malsabbagh/site-vault/actions/runs/37267327909/job/111630097843)
and Session job [111630834672](https://github.com/malsabbagh/session-vault/actions/runs/37268631416/job/111630834672)
ran 0.6.13 and ended with `action_required`, skipping publication. Their exact
live ledger reason was not retained; that status alone does not diagnose an
eligible baseline fallback. Synthetic source reproductions establish the
CLI defect independently. Provider deadlines, obsolete workflow authorization
and application code are separate concerns.

## Decision

Construct the current cache identity from the trusted current snapshot and
effective provider/model/profile/stages/context/learnings before reserving
analysis. Bind transaction attempt identity to that key, policy, configuration
and evidence digests. Exact retries remain duplicates; changed base or trusted
context on the same head becomes new work. Preserve unchanged legacy attempt
idempotency only when its persisted baseline and transaction prove that exact
identity. Reservation ownership, generation/CAS, pause and failed-attempt
limits remain authoritative; context changes never reset them.

Require completed history, successful baseline reconstruction, matching
repository/PR and a non-future baseline generation. Permit explicit planner
fallback reasons for base/rebase, model, engine, profile, trusted policy,
stage/prompt, document context or learning changes, and `coverage-incomplete`
from the bounded durable projection. The last case does not grant coverage:
all supplied work receives fresh full analysis. Missing, malformed, foreign,
future or generically incomplete history remains blocked. An untrusted ledger
or failed authorization cannot enter recovery.

Run the existing bounded service with no incremental plan or reused findings.
Keep the exact prior baseline and current key in admission context so later
finding classification still requires human adjudication for fallback
findings. Checkpoint a new baseline only with a validated complete result;
partial checkpoints preserve history and charge one failed attempt, while
wholly failed or interrupted work cannot replace it. Publication-only recovery
uses the existing result digest, transaction generation and exact-head/base
checks without another provider call or charge. Dispositions and progress
remain intact. ADR 0057 approval/check gates and ADR 0059 partial semantics
remain applicable.

Emit the existing sanitized diagnostic token to stderr as well as Actions
summary/output and optional outcome JSON. Baseline failures carry a closed
specific reason in `stage_summary.baseline_recovery`; attempted refreshes carry
`stage_summary.baseline_refresh`. No patch, prompt, model response or credential
is added to diagnostics. `action_required` means action is needed; its reason
distinguishes an operational recovery problem from finding adjudication. Exit
semantics remain unchanged.

## Scope

Includes CLI admission/reservation, safe diagnostics, lifecycle tests and
documentation. Excludes releases, tags, Worker deployment, credentials,
permissions, ledger resets, dispositions and live downstream reruns.

## Consequences

Healthy sessions recover from legitimate reuse invalidation without losing
history or treating old evidence as current. Full analysis can consume more
of the existing invocation budget and new fallback findings can still require
human adjudication. A completed analysis checkpoint may precede failed or
stale publication; the checkpoint remains exact-snapshot evidence and is never
treated as permission to publish or approve another snapshot.

## Alternatives considered

- Reset or reenroll the healthy ledger: discards history and weakens admission.
- Drop the prior baseline for publication: turns later findings into initial
  findings and bypasses human adjudication.
- Reuse stale results or increase budgets: does not establish current evidence.
- Keep head-only idempotency: suppresses legitimate same-head base changes.

## Validation

Synthetic CLI tests cover direct and actual chunked full recovery, same-head
base/stage changes and exact retry, interrupted/provider-failed/partial work,
publication replay, ownership/concurrency, pause, same-head failed-attempt
limits, empty work, foreign/future/incomplete history, narrowed coverage and
human-adjudication approval withholding. Existing transaction/publisher suites
cover stale head/base, digest/CAS and delivery gates. Record the red source
reproduction and green tests, Python aggregate/coverage, schema/workflow
validation, build and isolated clean-wheel lanes in the PR.

## Rollout and rollback

Use an ordinary reviewed package/workflow release after this PR. The draft PR
does not change installed 0.6.13 or the `v5` channel. No ledger schema migration
or reset is required. Reverting restores the old recovery refusal; keep
checkpoint artifacts and history, and do not change an operator's permissions
or dispositions as part of rollback.

## Follow-up work

Validate live recovery after an authorized release/channel update with freshly
captured sanitized metadata. Status-only downstream logs cannot establish the
exact cause of an older failed run.
