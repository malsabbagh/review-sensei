# ADR 0035 - Request changes for unresolved blocking findings

Status: Superseded
Date: 2026-09-16
Last amended: 2026-09-26
GitHub Issue: #86
Pull Request: draft PR to be linked
Owners/Reviewers: Maintainers
Approved by: not applicable

## Context

ReviewSensei classified blocking findings and withheld `APPROVE`, but it still
published every finding review as `COMMENT`. Two executions can observe the
same exact head independently: one may conclude the review is clean and
approve, while another publishes blocking comments. A `COMMENT` plus a withheld
approval is not GitHub's merge-blocking **changes requested** state, so a later
or earlier `APPROVE` from the same App can leave the pull request unblocked.

## Decision Drivers

- Make blocking findings a visible GitHub change request on the exact head.
- Keep a later execution able to approve after those blocking roots resolve.
- If one execution approves and another finds blocking comments on the same
  commit, the blocking outcome must win regardless of finish order.
- If the change request finishes first, a racy later approve must not dismiss
  it until the thread sweep proves the blocking roots are resolved.
- Preserve exact-head, App-authored, draft/fork, marker, and fail-closed
  thread-sweep guards, and keep `REVIEWSENSEI_AUTO_APPROVE=false` comment-only.

## Decision

When automatic approval is enabled, a finding review with blocking comments is
published as `REQUEST_CHANGES`. The shared finalizer remains the sole `APPROVE`
writer. After a comment-only or clean publication, and after an AI resolution,
it emits `REQUEST_CHANGES` when unresolved blocking ReviewSensei roots remain,
including findings published by an earlier execution. It emits `APPROVE` only
when the bounded thread sweep shows no unresolved blocking ReviewSensei root.

Same-head last-write policy:

- A later blocking result still posts `REQUEST_CHANGES` with its inline
  comments even if another execution already approved or commented that head.
- A later clean execution does not approve over an existing
  `CHANGES_REQUESTED` review while the sweep still sees unresolved blocking
  ReviewSensei roots.
- Deleted or resolved blocking roots do not deadlock that change request; the
  later clean run may `APPROVE`.
- Reviews are re-read immediately before `APPROVE`, and again after a second
  sweep when a change request was already visible, so a racy later change
  request is not dismissed unless that later sweep also proves no open
  blocking roots remain.
- Idempotent finalizer `REQUEST_CHANGES` writes own a
  `reviewsensei:changes-requested` marker; an unmarked App change request does
  not count as already published.

`REVIEWSENSEI_AUTO_APPROVE=false` remains comment-only: no `APPROVE` and no
`REQUEST_CHANGES`.

## Scope

In scope:

- GitHub review event selection for blocking vs non-blocking publication.
- Finalizer behavior across two executions on one exact head.
- Tests and public documentation.

Out of scope:

- GitHub App permission changes (`pull_requests: write` already covers both
  events).
- Branch protection, required reviewers, or dismissing stale reviews as a
  repository setting.

## Consequences

Positive:

- Blocking findings request changes in GitHub, not only withhold approval.
- Two overlapping executions on one commit converge on the blocking outcome
  until those roots are resolved, then one idempotent approval.

Tradeoffs:

- A mistaken blocking classification can request changes until it is resolved
  or a later exact-head run proves the roots are addressed.
- GitHub records the App's latest review decision; an `APPROVE` after resolved
  roots dismisses the earlier change request by design.

## Alternatives Considered

### Keep publishing findings as COMMENT and only withhold APPROVE

Rejected because it does not create a change request, so a later App approval
or a dismissed stale review can leave blocking work unenforced.

### Let whichever execution finishes last win unconditionally

Rejected because a slower clean run would dismiss an earlier change request
before the blocking comments are visible or resolved.

## Validation

Publisher and finalizer tests cover blocking `REQUEST_CHANGES`, comment-only
opt-out, a later blocking result overriding the same-head approval, a racy
later approve that must not dismiss an unresolved change request, and
promotion to `APPROVE` after blocking roots resolve.

## Rollout and Rollback

Rollout is a reviewed package and workflow update. Rollback by reverting the
publisher/finalizer change or setting `REVIEWSENSEI_AUTO_APPROVE=false`.

## Follow-Up

Collect hosted evidence for overlapping same-head executions after rollout.

## Links

- Related issue: #86
- Pull request: draft PR to be linked
- Supersedes: none
- Superseded by: [ADR 0056](0056-one-check-run-as-the-single-merge-authority.md)
- Related: [ADR 0032](0032-blocking-finding-classification-for-approvals.md)

The `REQUEST_CHANGES` review event described above is superseded: enforcement
is now the single head-bound `ReviewSensei` check run, and review publication is
always a `COMMENT` review (ADR 0056). The concern this record introduced -
that a blocking finding must not be discharged by a racy approval - is carried
forward by the check conclusion and by the withheld-approval rule.
