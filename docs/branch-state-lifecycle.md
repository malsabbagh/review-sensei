# Branch, Ancestry, and Active-Work State Lifecycle

This document specifies ReviewSensei's deterministic contracts for pull-request
state transitions, branch deletion and recreation, Git commit-graph rewrites,
base-branch retargeting, and mid-flight draft or close transitions (**G05**,
**G06**, and **G23**), implemented in
`packages/python/review-sensei-github/src/review_sensei_github/branch_lifecycle.py`
with live enforcement in `publication.py`, `review_run.py`, and
`src/review_sensei/workflow.py` and verified by
`tests/test_reopen_branch_lifecycle_g05.py`,
`tests/test_force_push_ancestry_g06.py`, and
`tests/test_active_draft_close_g23.py`. See
[ADR 0135](adr/0135-branch-state-lifecycle.md) for the architectural decision
record.

---

## 1. Identity and Scope Invariants

1. **Pull-request session identity (`G05`):**
   Session ledger state, convergence history, and finding dispositions bind to
   `SessionIdentity(repository_id, pull_request)`—never to the mutable
   `head.ref` branch name.
   - Closing and reopening the **same** numeric pull request preserves the
     existing session ledger, unresolved obligations, and per-head failed
     attempt counters.
   - Deleting a branch and opening a **different** numeric pull request on a
     recreated branch of the same name starts a strictly isolated session with
     zero state or obligation inheritance.
2. **Exact snapshot vs. target-ref binding (`G06`):**
   - `ReviewContextCacheKey` binds cached baseline state to
     `(repository_id, pull_request, base_sha, head_sha, policy_digest, ...)`.
   - Live review and conversation publication gates bind to **both**
     `(base_branch, base_sha)` and `head_sha`.
   - Consequently, retargeting `base.ref` while `base.sha` remains identical
     (`same_sha_base_ref_retarget`) keeps the baseline cache compatible (no
     `rebase-or-base-change` invalidation) while fencing any mid-flight review
     or reply write with `skipped_stale_base` so branch-ref-scoped rules are
     re-evaluated cleanly without duplicate model inference when retained
     analysis is replayed.
3. **Pre-effect fencing vs. post-dispatch irreversibility (`G02` / `G05` / `G23`):**
   Live PR gates (`evaluate_live_pr_write_gate`, `evaluate_finalizer_pr_gate`,
   and `ConversationPublisher` preflights) block **newly authorized** writes
   whenever `state != "open"`, `draft is True`, `head.repo` is missing/forked,
   or head/base identity drifts. ReviewSensei never claims atomic revocation of
   an HTTP mutation that was already dispatched to GitHub before or concurrently
   with a state transition.

---

## 2. Close, Reopen, and Deleted/Recreated Head Branches (`G05`)

| Scenario | Live Writes While Closed / Deleted | Ledger & Obligation Continuity | Failed-Attempt Allowance | Approval Reuse |
| :--- | :--- | :--- | :--- | :--- |
| Closed PR (`state != "open"`) | Blocked (`skipped_pr_state`, `reason="pr_closed"`) | Preserved on `(repository_id, pull_request)` | Unchanged | Blocked while closed |
| Deleted head repository/ref (`head.repo is None`) | Fail-closed refusal (`review preflight head repository was invalid`) | Preserved on `(repository_id, pull_request)` | Unchanged | Blocked |
| Reopened at **same** `(base_sha, head_sha)` | Admitted | Preserves all unresolved obligations | **Zero** fresh allowance minted | Reuses valid same-SHA publication (`already_published`, zero inference) only when obligations are satisfied |
| Reopened or branch recreated at **changed** `head_sha` or `base_sha` | Admitted on new snapshot | Preserves unresolved baseline obligations for rebinding | Scoped to each `head_sha` | Prior-SHA approval invalidated; new snapshot requires verification |
| Duplicate / reordered `reopened` / `synchronize` deliveries | Idempotent | Monotonic generation; no duplicate review emission | Unchanged | Idempotent (`already_published`) |
| New PR `#N+1` on recreated branch name | Admitted under PR `#N+1` | Completely isolated from PR `#N` | Fresh per `(repository_id, N+1)` | Never inherits PR `#N` markers |

---

## 3. Force-Push, Ancestry, and Base Identity Transitions (`G06`)

`inspect_git_ancestry` and `classify_source_identity_transition` in
`review_sensei_github.branch_lifecycle` classify all seven commit-graph and
ref-retarget transitions:

1. **`fast_forward_head_advance`** (`prior_head` is ancestor of `new_head`,
   `base_sha` unchanged): baseline cache remains compatible for incremental
   verification; in-flight writes on the old head skip with
   `skipped_stale_head`.
2. **`rebase_with_base_sha_drift`** (`head_sha` and `base_sha` both change):
   baseline cache invalidates with `rebase-or-base-change` and falls back to
   full review on the new snapshot.
3. **`unrelated_history`** (`git merge-base` returns non-zero / no common
   ancestor): `prepare_diff` refuses with an actionable diagnostic instructing
   the operator to fetch deepen history, retarget the base branch, or align
   ancestry before rerunning.
4. **`force_push_revert_to_prior_sha`** (`H1` approved $\to$ `H2` introduces a
   new unresolved obligation $\to$ force-push reverts head back to `H1`):
   session obligations recorded at `H2` remain authoritative; `ReviewPublisher`
   publishes a superseding non-approving review on `H1` rather than reusing
   `H1`'s superseded approval or failing complete-inventory reconciliation.
5. **`force_push_identical_tree_new_sha`** (`H1` approved $\to$ force-push `H2`
   with identical tree/diff bytes): `H1`'s SHA-bound approval never certifies
   `H2` without reviewing and publishing on `H2`.
6. **`base_sha_advance_constant_head`** (`base_sha` advances while `head_sha`
   stays constant): baseline cache invalidates with `rebase-or-base-change`;
   in-flight writes skip with `skipped_stale_base`.
7. **`same_sha_base_ref_retarget`** (`base.ref` changes while `base.sha` and
   `head.sha` stay identical): `ReviewContextCacheKey` remains compatible,
   in-flight review/reply writes skip with `skipped_stale_base`, and same-run
   retained replay rebinds the new `base.ref` with **zero** fresh model
   inference.

Dirty or conflicting GitHub mergeability metadata (`mergeable=False`,
`mergeable_state="dirty"`) continues to review the exact three-dot
`base_sha...head_sha` diff whenever a valid merge-base exists (`G04` control).

---

## 4. Draft and Close Transitions During Active Work (`G23`)

`classify_active_work_state_transition` defines the exact cost, side-effect,
accounting, and recovery contract when a pull request transitions to
`draft=True` (`converted_to_draft`) or `state="closed"` (`closed`) at each of
the five active execution phases:

| Phase | Provider Cost Incurred | Caller Dispatches | Native Writes | Accounting Terminal | Transaction Phase | Deterministic Recovery Route |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `before_inference` | `False` (`0` calls) | Retained; no refund | Blocked (`0` writes) | `False` | `None` | Fresh run once ready/reopened |
| `during_provider_work` | `True` (`1` call, no refund) | Retained; no refund | Blocked (`0` writes) | `False` | `None` (`closed`) / `publication_failed` (`draft`) | Controlled restart (`restart-run`) once ready/reopened; unauthenticated rollover held |
| `after_accepted_result` | `True` (`1` call, no refund) | Retained; no refund | Blocked (`skipped_pr_state`) | `False` | `publication_failed` (review) / `pending` (reply) | Controlled restart (review) or same-action accepted-reply reentry with `0` duplicate inference |
| `before_final_preflight` | `True` (`1` call, no refund) | Retained; no refund | Blocked (`skipped_pr_state`) | `False` | `publication_failed` | Same-run `attempt=2` retained replay with **`0` duplicate model inference** |
| `after_dispatch` (`confirmed_review_before_approval`) | `True` (`1` call, no refund) | Retained; no refund | `COMMENT` kept; `APPROVE` blocked (`skipped_pr_state`) | `True` | `publication_succeeded` | Same-snapshot reuse (`already_published`, `0` inference) + persisted-eligibility approval finalization |
| `after_dispatch` (`lost_response_stored`) | `True` (`1` call, no refund) | Pending mutation preserved (`recorded_pending >= 1`) | Stored `COMMENT` reconciled without duplicate; `APPROVE` blocked | `False` (`terminal_close_unconfirmed`) | `publication_succeeded` | Hold uncertain physical dispatch until reconciled |
| `after_dispatch` (`lost_response_unconfirmed`) | `True` (`1` call, no refund) | Pending mutation preserved (`recorded_pending >= 1`) | Unconfirmed (`0` confirmed reviews; `APPROVE` blocked) | `False` | `publication_failed` | Hold uncertain physical dispatch until reconciled |

Malformed non-boolean `draft` payloads (`null`, `0`, `1`, `"false"`, `"true"`,
or absent `draft`) fail closed with `GitHubPublicationError` or
`GitHubConversationError` at every admission, publication, reply, and approval
finalization preflight (`G24`).
