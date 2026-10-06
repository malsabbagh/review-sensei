# ADR 0063 - Authenticated abandoned analysis recovery

Status: Proposed
Date: 2026-10-06
GitHub Issue: Pending triage
Pull Request: [#215](https://github.com/malsabbagh/review-sensei/pull/215)
Owners/Reviewers: Maintainers
Approved by: not applicable

## Context

An analysis job can reserve a durable session and fail while assembling context,
before inference. Older runners did not clean up that failure. Later reviews
correctly refuse the held reservation, but `review continue` previously cleared
only `operator_paused`, leaving the session blocked until expiry. Process death
and cancelled jobs also need recovery without treating elapsed time as evidence
that their writer has stopped.

## Decision

Clean up every exceptional exit after CLI admission using the existing owned
reservation and generation checks. Interrupts abort without charging a failed
attempt. Other failures charge the existing failed-attempt budget. Cleanup
never changes a checkpointed transaction or another writer's reservation.

The OIDC broker returns optional `reservation_owner` (`run_id`, reviewed
`head_sha`) on a scoped session exchange. These values come from verified OIDC
and the broker's live-head session scope. The analysis adapter records them in
the first reservation write. The optional field participates in the existing
record digest; old records retain their original digest and remain readable.
Owner metadata disappears when the hold is committed or aborted.

An authenticated `@sensei review continue` with a held analysis reservation
collects live, bounded GitHub Actions evidence. A lazy, separate broker capability
`review_actions` carries only `actions: read`; the broker's one-use command grant remains the sole authority
for the session-comment write. Evidence must show the recorded owner run's
latest attempt is completed in the same repository. Recovery targets the exact
reservation owner, so unrelated CI does not block it. Another review cannot
join the held reservation: admission refuses a hold before inference. The
shared PR write lane serializes current command/review writers, and the exact
record checks reject observable changes from older or external writers.

For legacy records without an owner, recovery requires an analysis transaction,
an exact-head `pull_request` run associated with the same PR, the recognized
ReviewSensei caller path, one unique run interval containing the ledger write,
and one completed failed/cancelled/timed-out hosted or local analysis job whose
interval contains that write. Manual, unknown, successful, ambiguous or
truncated legacy evidence stays blocked. Timestamp containment identifies the
origin; elapsed age never grants recovery authority.

Evidence is re-collected in the mutation seam. The exact record digest,
generation and reservation must still match. The existing adapter rechecks the
live PR head and reads back the comment. One replacement drops only abandoned
analysis, unpauses, and records one failed attempt scoped to its original head.
It preserves completed-round counters, expiry, dispositions and convergence
history. Saturated failed budgets stay saturated. Repeated continuation without
a hold does not charge or rewrite. Saved publication work is never reclaimed.

Command and provider review jobs share the PR's existing provider concurrency
group without cancelling each other. Workflow-level latest-review cancellation
is retained for automatic `pull_request` reviews. Manual `workflow_dispatch`
reviews serialize after authoritative PR preflight instead of cancelling a
writer in this lane; live-head publication fences still apply. Reply jobs
retain their run-specific isolation. GitHub comment
PATCH still has no conditional ETag/CAS primitive. Generation/digest checks and
readbacks detect observable conflicts, while the shared Actions lane serializes
cooperating writers. This does not claim strict CAS against external writers.

## Scope

In scope: pre-inference failure cleanup, broker-attested owner metadata,
authenticated continuation recovery, bounded legacy origin proof and regression
tests. No automatic timer unlock, publication bypass, counter reset, provider
budget increase, release mutation, or arbitrary ledger edit.

## Consequences

Recoverable failed sessions do not wait for the 30-day expiry. Recovery records
failure honestly rather than marking a completed review. An active owner run
or inconclusive legacy origin requires waiting or investigation. Actions access
failure, unavailable metadata, or more than 100 candidate runs/jobs fails closed.
The App must grant Actions read for recovery; ordinary review and continuation
without a held reservation do not request that capability. Existing caller
workflows keep their permissions, avoiding reusable-workflow permission
elevation failures.

## Alternatives considered

- Age-based leases: rejected because slow or suspended jobs can still write.
- Clearing every reservation on continue: rejected because it races active writers.
- Re-enrollment: rejected for valid sessions because it resets existing budgets.
- A new hosted lock/database: deferred; reuse the serialized Actions lane and
  document the REST adapter's existing limitations.

## Validation

Tests cover context failure before provider calls, active/abandoned runs,
legacy origin ambiguity, reruns, changed generations, head freshness, one-use
mutation grants, repeated recovery, per-head budget saturation, owner integrity,
and atomic first-write ownership. Full credential-free source and broker tests,
format/lint/type/schema gates, coverage and package lanes are required.

## Rollout and rollback

Publish a reviewed new patch release and update the managed workflow through
normal protected-channel promotion. Existing immutable releases stay unchanged.
Deploy the reviewed broker and grant/accept the App's optional Actions read
permission before recovery. Owner-bearing new records need that broker version;
legacy PR-run recovery uses its separate read capability. No live session is changed by
merging or publishing the code; a maintainer must issue continuation explicitly.
Rollback by reverting the code and workflow through normal review. Older engines
fail closed on newly owner-bearing records rather than ignoring integrity fields.
Recover or complete those holds with an owner-aware engine before rollback,
or restore that engine afterward; an older engine cannot resume those records.

## Follow-up work

Associate the triaged issue and implementation PR. Extend legacy proof only with
stronger authenticated evidence, never guessed origin identities. A strict
remote conditional-write backend remains a separate architectural decision.
