# ADR 0057 - One check run as the single merge authority and evidence-bound default approval

Status: Proposed
Date: 2026-09-26
Last amended: 2026-09-26
GitHub Issue: #181
Pull Request: [#187](https://github.com/malsabbagh/review-sensei/pull/187)
Owners/Reviewers: Maintainers
Approved by: not applicable

Status note: this record stays `Proposed` deliberately at merge time. The ADR
process requires explicit maintainer approval to mark a record `Accepted`
(`docs/process/adr-process.md`), so this slice ships as an intentional
pre-acceptance change; the approval step is tracked with the epic #181 rollout,
and the index row carries the matching `Proposed` status.

## Context

ADR 0035 made blocking findings a visible GitHub change request: finding
reviews were published as `REQUEST_CHANGES`, and the shared finalizer emitted
`REQUEST_CHANGES` whenever unresolved blocking roots remained. That state is
not bound to a reviewed head and is not bound to a pending review: GitHub keeps
the App's latest review decision until a later review event replaces it, so the
change request outlives the execution that created it, competes with the
App's own approval, and says nothing about whether a review of the current head
is still running. It is also invisible to local runs and to the local CLI,
which cannot express a review event at all.

Epic #181 requires one authoritative check for merged enforcement
(`github.reviews` maps to review publication plus automated enforcement or
approval), with the check bound to its expected App producer and reviewed head,
and explicitly no persistent `REQUEST_CHANGES` as a second independent gate.
The same slice makes eligible auto-approval the default for `auto-approve`
while keeping approval an evidence-bound, exact-head decision rather than a
caller-supplied boolean.

## Decision Drivers

- One enforcement authority: a required check is the only GitHub mechanism
  that a repository administrator can require for a merge, and it is bound to
  a name and a producing App.
- Exact-head binding: a conclusion must describe one reviewed head, and a
  pending review of a head must not be represented by an earlier conclusion on
  the same head.
- Producer identity: a check run is owned by the App that creates it, and a
  required check is matched to that producer, so ReviewSensei must never adopt,
  complete, or overwrite a same-named run owned by another App.
- Honest failure modes: missing permissions, an App-authored pull request, or
  an unqualified configuration must be reported as withheld enforcement, never
  as a passing or neutral conclusion.
- Approval stays a satisfier of branch protection, not a gate: it is emitted
  only for an eligible exact head, and the decision must be reproducible from
  persisted state rather than from a caller boolean.

## Decision

Publish one stable check run per reviewed head from the Python GitHub adapter.
The check is named `ReviewSensei` and carries the external id
`reviewsensei-review-v1`; the expected producer is the App slug that publishes
the review. The adapter looks up existing runs for the exact head and reuses
one only when its name and installed App slug match the expected producer;
otherwise it creates a new run. No other App's run is ever completed or
inherited.

The pending window is exactly the publication window. A new eligible review
first writes the run as `in_progress` with no conclusion, then writes the final
conclusion after the review result is known, so an earlier `success` on the
same head cannot stand in for a review that has not finished. A run that dies
inside the window leaves `in_progress` behind, and the next review of that head
rewrites the same run; a caller that knows its run ended without a conclusion
can publish `cancelled` directly.

Conclusion mapping is policy-dependent and never encodes incomplete
enforcement as `neutral` or `skipped` outside advisory mode, because GitHub
accepts those conclusions for required checks:

| Mode | Condition | Conclusion |
| --- | --- | --- |
| `auto-approve`, `blocking` | complete, no required fixes | `success` |
| `auto-approve`, `blocking` | complete, required fixes remain | `failure` |
| `auto-approve`, `blocking` | partial, incomplete, or summary-only | `action_required` |
| `auto-approve`, `blocking` | publication failed | `action_required` |
| `auto-approve`, `blocking` | run ended without a conclusion | `cancelled` |
| `advisory` | any | `neutral` |

The review itself is always published as a `COMMENT` review: one exact-head
App review with the summary body and the valid inline findings. A persistent
`REQUEST_CHANGES` is never emitted as a second authority, so the enforcement
state a repository enforces is exactly the check run's conclusion for the
reviewed head.

Approval is emitted only in the default `auto-approve` mode, and only for an
eligible exact head: trusted complete review evidence for that head, no
unresolved required finding or incomplete-review obligation, verified provider
qualification, an eligible pull-request identity, and an authorized write. The
mode is the single authority - `blocking` enforces through the check and never
approves, and `advisory` publishes `neutral` and never approves. Eligibility is
persisted as a bounded document in the published review body and re-read at
finalization, so a delayed or recovery finalization decides from durable
evidence rather than from a caller-supplied boolean. Missing or malformed state
cannot imply eligibility: an unreadable document, an incomplete thread scan, or
an unclassified ReviewSensei root withholds approval and reports a bounded
diagnostic (`required_fixes_open`, `review_incomplete`,
`review_threads_incomplete`, `approval_withheld`, ...).

The check-publication capability is scoped and separate from review
publication. `Checks: write` is a distinct App permission, and the broker
grants a distinct `check_publish` capability. When that capability or
permission is unavailable, the review is still published, approval is withheld,
and the run reports the `check_permission` diagnostic instead of implying
enforcement it does not have.

YAML cannot make a check required, and no product surface claims otherwise.
`doctor` reports the expected check identity and producing App and states that a
repository administrator must mark `ReviewSensei` required; ReviewSensei never
reads or changes branch protection. GitHub does not run required checks on
pull requests authored by the App itself, so an App-authored pull request is not
gated by its own check: ReviewSensei withholds approval and reports
`app_authored`.

## Scope

In scope:

- Check-run identity, producer binding, pending/concluding writes, conclusion
  mapping, and cancellation in the Python GitHub adapter.
- Review-event selection (always `COMMENT`) and the removal of the persistent
  change-request authority, superseding ADR 0035.
- Default approval eligibility, persisted eligibility evidence, delayed
  finalization, and the withheld-approval diagnostics.
- The scoped `check_publish` broker capability, App permission documentation,
  and `doctor` gate-identity reporting.
- Tests for identity/producer, permission failure, pending/current-head state,
  conclusion mapping, cancellation, same-head races, and advisory transitions.

Out of scope:

- Making the check required on any repository, or reading/changing branch
  protection. That is an administrator action on the host.
- Reconciling obsolete required-check state or prior change-request state when
  an operator disables writes or switches policy. That authorized transition is
  epic #181 slice G.
- Merge operations and auto-merge enablement, which remain operator-only.

## Consequences

Positive:

- One head-bound authority: the enforcement state for a head is one check
  conclusion, visible to administrators, local runs, and hosted runs alike.
- A pending review is distinguishable from a concluded one, and an interrupted
  run cannot leave a passing conclusion standing for an unfinished review.
- Default approval is reproducible: an eligible exact head is approved, a
  withheld approval carries a bounded reason, and no caller boolean can force
  either outcome.

Tradeoffs:

- Enforcement is opt-in at the host: until an administrator requires
  `ReviewSensei`, the check is informative rather than blocking, and a
  misconfigured repository can believe it is gated when it is not. `doctor`
  reports the identity to reduce that risk; it cannot remove it.
- App-authored pull requests can never be gated by their own check, so they are
  reported and never approved rather than silently ungated.
- Existing installations that relied on `REQUEST_CHANGES` lose that signal, so
  the transition must explain the replacement and reconcile stale
  change-request state (slice G).

## Alternatives Considered

### Keep `REQUEST_CHANGES` alongside the check

Rejected. A persistent change request is not head-bound and competes with the
App's own approval; two authorities can disagree, and the disagreement is
invisible to anyone reading only the check.

### Encode non-passing enforcement as `neutral` or `skipped`

Rejected. GitHub accepts `neutral` and `skipped` conclusions for required
checks, so a required check with those conclusions would silently pass. Only
`advisory` publishes `neutral`, because that mode promises no ReviewSensei
gate.

### Approve from a caller-supplied eligibility boolean

Rejected. The epic requires that no test and no caller relies on an unverified
boolean. Eligibility is derived from persisted, exact-head evidence and re-read
at finalization, so a delayed run cannot approve from stale or unverifiable
inputs.

### Trust a same-named check run regardless of its App

Rejected. A required check is matched to its producer; adopting another App's
run would let an unrelated App's conclusion stand for ReviewSensei's review.

## Validation

`tests/test_gate_and_default_approval.py` captures real adapter HTTP events for
the default-approval rows (complete eligible positive, optional-only findings,
required inline and body findings, partial coverage, invalid/incomplete
evidence, stale head, unqualified configuration, App-authored pull request,
writes disabled, and delayed finalization with and without a persisted
document) and the one-gate rows (identity/producer, permission failure,
pending-before-conclusion, cancellation, same-head race across thread pages,
and advisory transitions). Every row asserts the captured events - review
events, check-run writes and conclusions, GraphQL operations and cursors,
diagnostics, and call counts - and asserts that no merge endpoint or auto-merge
field is ever called. The updated `tests/test_github_publication.py` suite pins
the comment-only review event and the withheld-approval diagnostics, and the
Worker/broker suites cover the `check_publish` capability exchange.

## Rollout and Rollback

Rollout: the App must be granted `Checks: write` and the broker must serve the
`check_publish` capability before the gate is effective; the package and
workflow release that enables the check is a reviewed release operation.
Administrators must mark `ReviewSensei` required for the check to gate merges,
and `doctor` reports the identity to use.

Rollback: reverting the release restores the previous review-event behavior and
stops publishing the check. A repository that made `ReviewSensei` required must
reconcile that rule through an authorized transition (slice G), because a
required check that is no longer published blocks merges that the previous
release would have allowed. Approval remains idempotent per exact head in both
directions, so a rollback does not duplicate or revoke an approval by itself.

## Follow-Up

- Slice G: reconcile obsolete required-check and prior change-request state when
  writes are disabled or the policy changes, with executable operator
  instructions.
- Hosted evidence after rollout: check identity readback, a required-check
  gating observation, an interrupted-run observation, and an App-authored
  pull-request observation.

## Amendment 2026-09-26: branch-surface reads

The Decision section said ReviewSensei "never reads or changes branch
protection". [ADR 0056](0056-host-fact-placement-and-honest-approval-presentation.md),
adopted in the same slice, has the publisher read the branch's
conversation-resolution requirement - branch rules, then classic branch
protection - before it opens optional inline threads, treating an unreadable or
unexpected response as unknown, which fails closed. The accurate boundary,
which `doctor` and the installation guide now state, is that the App requests
no `Administration` permission and never *changes* branch protection; the
conversation-resolution probe exists only for placement, and no enforcement or
approval decision reads branch protection. No other decision in this record
changes.

## Links

- [Epic #181](https://github.com/malsabbagh/review-sensei/issues/181)
- [ADR 0030](0030-gate-app-approvals-on-resolved-review-threads-and-exact-head-review-safety.md)
- [ADR 0032](0032-blocking-finding-classification-for-approvals.md)
- [ADR 0035](0035-request-changes-for-blocking-findings.md) (superseded by this record)
- [ADR 0043](0043-structured-run-outcomes-budgets-and-publication-recovery.md)
- [ADR 0055](0055-merge-focused-default-and-legacy-retirement.md)
