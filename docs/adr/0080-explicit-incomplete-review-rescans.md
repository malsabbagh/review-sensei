# ADR 0080: Explicit same-head rescans of incomplete reviews

- Status: Proposed
- Date: 2026-10-16
- Decision owner / reviewers: ReviewSensei maintainers
- Linked GitHub Issue: none; targeted maintainer-authorized bug fix
- Linked PR: [#305](https://github.com/malsabbagh/review-sensei/pull/305)

## Context

An explicit rescan of a published partial review currently aliases the ordinary
snapshot reservation. Admission returns `already_published` before inference.
Publication also suppresses a new non-blocking result when that head already has
a review. `continue` only unpauses, `verify` rechecks approval, and `reenroll`
recovers expired or missing sessions; none authorizes fresh analysis.

## Decision

The hosted `review --rescan` path verifies an actual human Actions dispatch or
an unedited PR issue-comment rescan, current maintainer permission, repository,
PR and current head. Both provider lanes append `--rescan` only when the event
asks for one: an issue comment must match the rescan grammar, and a
`workflow_dispatch` counts only when its inputs select this review operation
for a pull request. An explicit `rescan` input of false, or any other
operation, is not a rescan. Callers that omit `rescan` keep the existing
dispatch contract, because a maintainer dispatch of this review workflow is
itself the explicit request. `GITHUB_TRIGGERING_ACTOR` must be present and
match the sender; a missing value fails closed. Request creation must follow
the latest App publication,
so queued stale events and reruns of the original review cannot reopen it. The
existing `review_actions` capability verifies a dispatch run without extending
the session token. The broker still supplies the existing head-bound session
capability. Latest App publication eligibility must explicitly show incomplete
analysis or coverage; missing or unreadable evidence fails closed. Complete
reviews and new heads retain ordinary admission.

The request identity is the issue-comment database ID or Actions run ID, never
the run attempt. Admission reserves a distinct transaction and consumes the
request in the same generation-checked ledger replacement. Two monotonic replay
watermarks persist across heads. Six bounded audit receipts retain the prior
published transaction and review ID, ordinary reservation, and new reservation.
Old published reviews remain the durable publication history; the ledger keeps
its bounded history policy. Watermarks prevent replay after receipt eviction.

Ordinary duplicate deliveries stay suppressed after reanalysis. An active
reservation, maintainer pause, session expiry, current-head fence, or existing
per-head failed-attempt limit still prevents work. Failed analysis consumes the
request and charges the existing budget; a fresh explicit request is required
for another attempt. Publication failures recover the original result instead
of rerunning inference. No counters, baselines, dispositions, or historical
published reviews are reset. Reanalysis inspects the full snapshot without
incremental inference reuse, and binds operation-entry identity to the actual
analysis reservation.

Publication permits the new result only when its transaction is bound to a
consumed rescan receipt and the latest App publication remains incomplete. Exact
result retry reconciliation, finding deduplication, approval, coverage and live
head/base checks remain in force. Excluded files remain excluded and cannot gain
approval merely because a rescan was requested.

## Scope

Engine admission, hosted CLI authentication and publication, and both provider
lanes of the public reusable workflow. Existing v5 callers can consume the
updated reusable workflow without installing a new caller. Existing rescan
comments continue through their `review` route. Human-file attestation
integration and broker command grammar are separate work.

## Consequences

New optional integrity-protected session state is readable by the updated
engine. Older writers fail closed on its digest/schema, rather than deleting
unknown authority. Request and record sizes remain bounded. An edited/deleted
comment or revoked permission cannot authorize a new attempt. Trusted workflow
and broker identity remain prerequisites; caller strings alone grant nothing.

## Alternatives considered

Clearing the ledger loses history and resets budgets. Fabricating a commit
changes the reviewed snapshot. Globally disabling duplicate suppression allows
unbounded ordinary retries. Reusing a completed reservation conflates distinct
analysis results and publication retries.

## Validation

Regression coverage is `tests/test_explicit_rescan.py`. It exercises actual CLI
admission and publication with synthetic GitHub responses, partial coverage,
repeated delivery, complete reviews, unauthorized/edited/stale requests, active
and conflicting reservations, failed analysis, budget exhaustion, new heads,
comment-body equality, current permission and run identity, replay watermarks,
and both reusable provider lanes:

- `test_dispatch_and_comment_bind_distinct_stable_request_identities`
- `test_unauthorized_and_stale_events_make_no_mutation`
- `test_dispatch_rejects_wrong_run_identity_but_does_not_confuse_workflow_and_pr_heads`
- `test_published_partial_rescans_once_and_retains_truth_and_history`
- `test_bounded_receipts_keep_watermarks_after_eviction`
- `test_ordinary_retry_after_failed_rescan_publication_requires_recovery_without_inference`
- `test_real_cli_published_partial_rescan_and_duplicate_delivery`
- `test_reusable_runner_wires_both_provider_lanes_without_caller_upgrade`

Status stays Proposed until a maintainer accepts this record. The tests above
ship in the same pull request. Run the repository gates and clean-wheel
consumer checks before merge.

## Rollout and rollback

Release engine and hosted packages together, updating the hosted exact engine
dependency and the reusable workflow's exact package installs through the normal
release runbook. Promote the protected v5 channel only after release validation.
Then request a new dispatch or rescan comment on the still-current PR head.
No broker deployment, ledger deletion, or consumer commit is required.

Rollback must preserve the new session field. Older engines intentionally fail
closed on records carrying it; do not erase receipts or replay watermarks to
make an old writer proceed. Roll forward with a corrected compatible reader.

## Follow-up work

Wire the existing human-file attestation lifecycle into the actual approval
command flow separately. Reanalysis does not grant human coverage or approve
policy-excluded content.
