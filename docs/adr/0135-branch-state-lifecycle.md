# ADR 0135: Qualify branch reopen, force-push ancestry, and active draft/close lifecycle contracts

- Status: Proposed
- Date: 2026-11-11
- Decision owner/reviewers: ReviewSensei maintainers; independent source review
- Linked GitHub Issue: pending; Batch 5 lifecycle gap qualification (G05, G06, G23)
- Linked PR: [#447](https://github.com/malsabbagh/review-sensei/pull/447)

## Context

Pull requests routinely undergo stateful lifecycle transitions while ReviewSensei
sessions exist or while an execution is actively running:

1. **Close, reopen, and branch deletion/recreation (`G05`):** Maintainers close
   and reopen pull requests at the same or a modified commit, or delete a head
   branch and recreate a branch of the same name under either the same pull
   request or a brand-new pull request number. Ambiguous identity binding risks
   either leaking obligations across distinct pull requests or discarding
   unresolved findings and resetting failed-attempt caps on same-PR reopen.
2. **Force-push ancestry, rebase, and target-ref retargeting (`G06`):** Force
   pushes can revert a branch back to an earlier approved commit SHA after a
   later commit recorded new unresolved obligations, rewrite history with an
   identical tree SHA, introduce unrelated history with no merge base, or
   retarget `base.ref` without changing `base.sha`. Conflating `base.ref`
   retargets with `base.sha` drift causes unnecessary cache invalidation, while
   treating any prior review on `H1` as proof of current completeness can either
   reuse a superseded approval or fail complete-inventory reconciliation.
3. **Draft and close transitions during active work (`G23`):** Converting a pull
   request to draft (`draft=True`) or closing it (`state="closed"`) can occur
   before inference, during provider inference, after an accepted result is
   retained, immediately before final mutation preflight, or after an outbound
   review request has already been dispatched (including across a lost HTTP
   response). The runtime must preserve incurred cost and uncertain-effect
   liabilities without claiming revocation of already-sent requests or forcing
   duplicate model inference where retained artifacts remain valid.

## Decision

Introduce `review_sensei_github.branch_lifecycle` as the canonical, pure,
type-checked lifecycle decision module and wire live write gates across
`ReviewPublisher`, `ReviewApprovalFinalizer`, `ReviewRun`, and `prepare_diff`:

1. **Strict numeric PR identity and reopen continuity (`G05`):**
   - Bind session state exclusively to `SessionIdentity(repository_id, pull_request)`.
   - Centralize pre-mutation live PR admission in `evaluate_live_pr_write_gate`
     and `evaluate_finalizer_pr_gate`, enforcing exact-boolean `draft` checks,
     `state == "open"`, non-null same-repository `head.repo` / `base.repo`,
     branch-ref liveness (`branch_ref_exists`), and exact
     `(base_branch, base_sha, head_sha)` matching without adding extra
     happy-path HTTP requests.
   - Fence delayed review, approval-finalizer, and mention-reply effects
     arriving after close (`skipped_pr_state`) or after changed-SHA reopen
     (`skipped_stale_head`), and evaluate out-of-order `reopened` / `closed` /
     `synchronize` deliveries against live authenticated GitHub PR state rather
     than stale event snapshots.
   - Preserve prior unresolved obligations and head-scoped `failed_attempts`
     across same-PR close/reopen, minting zero fresh failed-attempt allowance on
     same-SHA reopen while isolating distinct PR numbers on recreated branches.

2. **Git ancestry inspection and superseding obligation publication (`G06`):**
   - Provide `inspect_git_ancestry` (local Git collector) and
     `classify_source_identity_transition` (pure classifier over transport-neutral
     `GitAncestryEvidence`) to classify head/base commit-graph and ref-retarget
     transitions, failing closed via `GitAncestryEvidence.unavailable()` when
     ancestry evidence is absent.
   - Separate `same_sha_base_ref_retarget` (cache-compatible `base_sha` on
     constant `head_sha`, in-flight `skipped_stale_base` fence, zero-inference
     retained replay on the rebound `base.ref`) from `base_sha` drift
     (`rebase-or-base-change`) and combined head-change + base-ref retargets
     (`base-ref-retarget`).
   - Update `ReviewPublisher` reconciliation so that when a branch force-pushes
     back to a previously cleanly reviewed `head_sha` while carrying newer
     unresolved session obligations (`supersedes_prior_clean_review`),
     ReviewSensei publishes a superseding `COMMENT` review and withholds
     approval—while keeping complete-inventory reconciliation fail-closed for
     incomplete or unverified prior reviews.
   - Emit actionable merge-base diagnostics in `prepare_diff` when two commits
     share no common ancestor (`unrelated_history`).

3. **Five-phase active draft/close accounting and recovery (`G23`):**
   - Formalize `classify_active_work_state_transition` across
     `before_inference`, `during_provider_work`, `after_accepted_result`,
     `before_final_preflight`, and `after_dispatch` (`confirmed_review_before_approval`,
     `lost_response_stored`, and `lost_response_unconfirmed`).
   - Retain all physical caller dispatches and incurred provider costs without
     automatic refund at every phase.
   - Block newly authorized review, reply, check, and approval writes once
     `draft=True` or `state="closed"` is observed, while leaving already-sent
     `COMMENT` reviews intact (`already_dispatched_effects_revoked=False`) and
     holding `accounting_terminal=False` (`terminal_close_unconfirmed`) whenever
     a review `POST` response is lost.

## Scope

Applies to GitHub pull-request review, approval finalization, hosted reply
freshness gates, and local diff merge-base validation in `review_sensei` and
`review_sensei_github`. Session-ledger wire schemas, workflow YAML triggers, and
shared distribution manifests remain unchanged.

## Consequences

- Reopened pull requests and force-push reverts cannot bypass unresolved
  human-adjudication or blocking obligations, nor can same-SHA reopens reset
  per-head failed-attempt limits.
- Same-SHA `base.ref` retargets and `before_final_preflight` draft/close skips
  recover via retained replay without duplicate model inference.
- Mid-provider and pre-staging interruptions hold unauthenticated rollover fail
  closed (`previous caller run is not terminal`) and recover cleanly through
  authenticated `restart-run` archival.
- Lost HTTP responses concurrent with draft/close transitions never fabricate
  terminal settlement or duplicate an already-stored review.

## Alternatives considered

- **Keying sessions by `(repository_id, head_ref)`:** Rejected because branch
  names are mutable and reusable across unrelated pull requests.
- **Invalidating `ReviewContextCacheKey` on `base.ref` changes:** Rejected
  because `base.sha` already uniquely identifies the diff base tree; gating
  in-flight writes as `skipped_stale_base` while keeping `base_sha` cache
  compatibility avoids redundant inference.
- **Deleting or dismissing already-posted `COMMENT` reviews on late draft/close:**
  Rejected per the `G02` pre-dispatch fence boundary; post-dispatch cleanup
  mutations would consume extra budget and obscure audit history on a PR whose
  writer authority just ended.

## Validation

- `tests/test_reopen_branch_lifecycle_g05.py`: 10 end-to-end and unit tests
  covering close/reopen, deleted/recreated branches (`head.repo=None` and
  `branch_ref_exists=False`), cold-start and base-ref retarget matrix cells,
  PR `#7` vs. PR `#8` isolation, failed-attempt conservation, duplicate and
  out-of-order `reopened`/`closed`/`synchronize` deliveries, delayed review /
  approval / mention-reply effects after close and changed-SHA reopen, and live
  publisher/finalizer write gates (live consumer deployment telemetry remains
  tracked separately under `G11`).
- `tests/test_force_push_ancestry_g06.py`: 9 end-to-end, reconciliation-matrix,
  and real-Git ancestry tests covering commit-graph and combined head+base-ref
  retarget transitions, `H1 -> H2 -> H1` obligation preservation and
  `supersedes_prior_clean_review` control flow, identical-tree/diff force-pushes,
  same-SHA `base_ref` retarget zero-inference recovery, hosted `ReviewRun`
  unrelated-history pre-inference refusal and clone-free `unavailable()`
  confirmation, rebase/force-push delayed old-effect fencing plus `attempt=2`
  zero-inference retained retry journeys, post-`COMMENT` rewrite fencing, and
  `G04` dirty mergeability controls.
- `tests/test_active_draft_close_g23.py`: 8 comprehensive tests covering
  `draft=True` and `state="closed"` across all 5 active-work phases on review
  and reply routes, strict `G24` exact-boolean `draft` refusal across active
  bootstrap/publisher/finalizer/reply gates, exact `ApiRunAccounting` caller
  request conservation without refund, accepted-result retention, and
  lost-response `stored` vs. `unconfirmed` accounting holds.
- Full regression anchors (`test_g04_merge_characterization`,
  `test_draft_validation`, `test_reply_freshness_g33`,
  `test_bounded_v1_publication`, `test_github_publication`, `test_review_run`)
  and repository quality gates (`ruff`, `mypy`, schema/contract checks, and
  branch coverage).

## Rollout and rollback

Ships through normal source review and CI with no schema migration or workflow
input changes. Rollback reverts the branch commits cleanly without invalidating
persisted session-ledger comments.

## Follow-up work

- Confirmed at `07179c0f`: hosted GitHub Actions `review-run` checkouts refuse
  unrelated histories pre-inference via `prepare_diff` while preserving session
  obligations, and clone-free hosted paths fail closed via
  `GitAncestryEvidence.unavailable()` without unrequested REST calls. When
  clone-free GitHub App workflows need commit-graph classification directly,
  populate `GitAncestryEvidence` from GitHub's authenticated compare/commit REST
  endpoints rather than `inspect_git_ancestry`, keeping
  `classify_source_identity_transition` unchanged.
- Live installed-consumer workflow pins and deployed runtime version telemetry
  remain tracked separately under `G11`.


## G17 private-store stabilization on PR #451

This is a narrow correction to the recovery handoff described by
[ADR 0131](0131-provenance-retention-representability-and-capacity-envelope.md),
not new writer authority or a durable lifecycle schema. Authentication must be
exact lowercase hexadecimal HMAC-SHA256 before constant-time comparison. Selected
invalid envelopes refuse; unrelated invalid/unreadable cache entries are preserved
and skipped by pruning. Unauthenticated expiry fields cannot authorize deletion.
Envelope reads and directory enumeration are bounded before parsing/sorting, with
unchanged byte, file and request limits. Valid authenticated TTL expiry remains the
only pruning route.

The optional private context store requires POSIX ownership/mode enforcement.
Non-POSIX opt-in refuses before private file access or creation; the unconfigured
path remains available. This is an explicit unsupported boundary, not Windows ACL
qualification. POSIX integration tests remain POSIX-qualified; portable tests
exercise the non-POSIX refusal, no-byte/no-inference effects, and default behavior.
Full Windows private-store support remains pending an independently qualified ACL
mechanism. No production permission/ACL changes are made by this correction.

Validation uses malformed/non-ASCII signatures, oversized/deep JSON, invalid
signed TTL, unreadable files, bounded directory overflow, and actual hosted opt-in
refusal. It does not establish genuine subprocess lifecycle reconstruction,
installed applicability, or historical settlement. Rollback is a source revert;
keep all existing envelopes and ledgers, and do not treat restoring the former
permissive non-POSIX behavior as approved privacy enforcement.

### G17 ordinary-entry cold-process qualification

`OrdinaryRecoverySubprocessG17Tests` now exercises the ordinary hosted entry
route with opt-in private storage. The parent invokes production accepted-result,
ledger and context writers, then interrupts before publication. Five fresh Python
processes reopen those exact artifacts without diagnostic run files or inherited
provider credentials. A provider stub fails if inference is attempted. Missing,
tampered and malformed handoffs and changed diff bytes reach the intended context
hold with no publication; the unchanged case publishes through synthetic native
transport and settles the retained transaction. Every case preserves the original
nondefault 384-request configuration, existing accounting blocks, and conservation
of liabilities and remaining allowance. No fresh default allowance is injected.

This qualifies same-run, later-attempt cold reconstruction on POSIX through the
production transport seam, not deployed authenticated GitHub execution, distinct
workflow-run transfer, non-POSIX private storage, or historical recovery. The
native service and provider are fixtures; no real review or approval is submitted.
The matrix found no further source integration defect in this bounded slice.
