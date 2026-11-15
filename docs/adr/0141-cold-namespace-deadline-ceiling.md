# ADR 0141: Cold namespace deadline representation ceiling

- Status: Proposed
- Date: 2026-11-15
- Decision owner/reviewers: ReviewSensei maintainers; independent source review
- Linked GitHub Issue: pending; user-directed bounded lifecycle qualification
- Linked PR: [#462](https://github.com/malsabbagh/review-sensei/pull/462)

## Context

Detached historical command accounting includes every remaining ordinary and
terminal request. Reserving the generic sixteen-digit maximum deadline for each
future request can overflow the unchanged 32,768-byte root even when the retained
manifest itself fits. Observed current dates alone do not bound future dispatches.

## Decision

The original cold namespace refuses new request deadlines above
9,999,999,999,999 Unix milliseconds. Its source-bound caller checks this negative
fence before incrementing the physical sequence, consuming a request allocation
or dispatching. Both original and paid-prefix projections use that same maximum
for every future request. The module seal protects the ceiling and its consumers.

## Scope

Only new requests under the original active historical namespace use the narrower
ceiling. Existing pending/completed receipts, including their original deadlines,
remain unchanged. The generic accounting model and current-command namespace
retain their existing domains. No timeout, allowance, terminal reservation,
evidence limit, comment allocation or census count increases or decreases.

## Consequences

The projection saves three decimal digits per future request while retaining all
possible unknown requests. A clock whose future deadline exceeds the ceiling
causes a pre-dispatch refusal, including during terminal processing. This bound
does not prove command admission, execution, settlement, recovery or approval.

## Alternatives considered

Projecting current timestamps without an enforced fence is unsound. Deleting
terminal slots, clearing uncertainty, rewriting retained manifests or increasing
the root limit would change independent obligations. A new timestamp wire codec
would require a broader durable-format migration.

## Validation

Boundary tests cover the maximum, one over, the generic maximum and invalid
scalars. Original-caller tests check refusal before sequence/effect/dispatch and
projection preservation of all existing receipts and future slots. Paid-prefix
tests retain real preparation debits, full terminal funding and final-command
refusal. Complete exact-head remote CI remains required.

## Rollout and rollback

This changes no stored schema or existing deadline. Reverting the narrower fence
must also restore the generic future projection bound. No historical record is
rewritten and no live recovery route is activated.

## Follow-up work

Qualify complete final-command source, funding, reader and loss outcomes
independently; a root that fits is not sufficient execution authority.
