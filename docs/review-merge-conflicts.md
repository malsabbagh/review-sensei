# Reviewing a branch with merge conflicts

ReviewSensei reviews the exact admitted base/head snapshot using the branch's
`base...head` diff. Transient GitHub `mergeable` and `mergeable_state` metadata
do not change that review scope. A completed review describes the analyzed
snapshot; it does not establish that GitHub can merge the pull request.

## When a run can start

The generated caller uses `pull_request` for automatic reviews and also supports
explicit `workflow_dispatch`. GitHub does not schedule `pull_request` workflows
while a pull request has a merge conflict. See GitHub's
[event-trigger documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#pull_request).
An existing authenticated manual review can analyze a conflicted branch diff,
subject to the same trusted policy, exact revisions, authority and finite budgets.
This contract does not change the caller's event subscriptions.

## Changes during a run

A changed base/head that is already visible at admission refuses the old request
before diff acquisition or inference. A source change after admission can consume
one bounded analysis before a later retention or publication fence refuses it.
Resolving a conflict by updating the head creates a new source snapshot; it does
not authorize publication of the old review on that new head. Reported mergeability
changes alone do not invalidate the admitted snapshot.

Open/draft state, repository identity, fork restrictions, writer authority and
publication eligibility retain their existing guards. Refused publication does
not establish terminal accounting or permission to retry. Keep its receipts and
inspect the bounded outcome through the [recovery procedure](approval-recovery.md).
GitHub's merge requirements remain independent of review completion or approval.

## Regression evidence

The G04 checkout tests cover 30 synthetic hosted scenarios: mergeability changes
before acquisition, during analysis and before publication; real local Git conflict
and resolution graphs; exact diff delivery to analysis; and source, state and
authority refusals. Seven methods include the generated caller's trigger contract.
Providers, GitHub and broker transports are fakes with live connections refused.
These tests qualify source behavior, not a deployed runner or live GitHub scheduling.
