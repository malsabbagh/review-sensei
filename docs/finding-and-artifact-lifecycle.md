# Finding, Drift, Artifact, and Trust Lifecycle Reference

This document specifies the runtime invariants and operator recovery contracts
implemented for **G10**, **G16**, **G14**, and **G15** (see
[ADR 0134](adr/0134-bounded-active-findings-and-artifact-authority.md)).

---

## 1. Bounded Active Window and Four-Variable Lifecycle (`G10`)

Module: `src/review_sensei/active_finding_window.py`

Every review round keeps four state variables strictly separate:

| State variable | Responsibility | What it never implies |
|---|---|---|
| `changed_file_coverage` | Tracks enumerated vs reviewed diff paths (`reviewed`, `partial`, `incomplete`, `unknown`) and explicit maintainer partial-coverage acceptance. | Never proves prior obligations were fixed. |
| `authoritative_decision_inventory` | Retains all obligations (`active-unresolved`, `omitted-uncertain`, `ambiguous-identity`, `verified-fixed`, `authorized-override`, `rescan-superseded`). | Never drops non-displayed obligations when the active window is full. |
| `display_active_findings` | Selects at most `max_active_findings` priority-first entries, retaining existing active/contingent native slots before promoting challengers. | Never exposes a public `deferred_count`, `deferred_findings`, or hidden queue. |
| `approval_eligibility` | Grants `approved` only when both `changed_file_coverage.coverage_satisfied` and `authoritative_decision_inventory.inventory_satisfied` hold (with zero uncertain native slots). | Never triggers automatic unbounded continuation loops. |

### Slot Turnover and `#427` Proposal Boundary

- **Ordinary omission is uncertain:** In `incremental` and `fallback-full`
  passes, omitting an unresolved prior obligation transitions it to
  `omitted-uncertain`, preserving its active slot and withholding approval.
- **Deterministic promotion:** A non-displayed obligation is promoted into
  `display_active_findings` **only** when a reserved active slot is released by
  `ConfirmedFixEvidence` (bound to the original `resolution_criterion` on a
  complete review), an `AuthorizedHumanOverride` with `slot_released=True`, or a
  complete head-bound `AuthorizedRescan(mode="full")`.
- **Proposal-only `#427` receipts:** `extract_decision_proposals` validates
  `ReviewResult.decision_pass` and maps candidate assessments strictly to an
  intra-tier `proposal_rank`. Decision-provider outputs cannot clear an
  obligation, downgrade `blocking`, or bypass `needs_human`.
- **Ordinary publisher & application entry points:** `GitHubApplication.publish_review`
  (`max_active_findings`, `confirmed_fixes`, `human_overrides`,
  `coverage_acceptance`, `authorized_rescan`), `GitHubApplication.apply_maintainer_command`,
  and `ReviewPublisher.publish` (`_plan_active_window_round`,
  `project_display_review_result`, `_commit_active_window_success`,
  `_commit_active_window_uncertain`, `reconcile_active_window_slot`) execute the
  four-variable window on the ordinary review/verify route, inspecting
  `active_window_state` and `active_window_public_status` with
  `assert_no_deferred_leakage` enforced on public summaries and shadow metadata.

---

## 2. Cross-File Location and Native Thread Drift (`G16`)

Module: `src/review_sensei/finding_drift.py`

Each `DriftTrackedObligation` separates:
1. `OriginalFindingCriterion` — immutable `obligation_id`,
   `original_fingerprint`, `resolution_criterion`, `origin_path`,
   `origin_symbol`, and `defect_kind`;
2. `PresentLocationAnchor` — current `head_sha`, `path`, `line`, `side`,
   `anchor_location` (`left`, `right`, `file`, `summary`), and `drift_kind`;
3. `NativeThreadBinding` — GitHub `thread_id`, `comment_id`, placement, and
   `PublishedSlot` state.

### Behavior by Drift Kind

| Drift kind | Anchor & thread handling | Resolution / recovery requirement |
|---|---|---|
| `file_rename` | Maps `old_path -> new_path`; updates existing native thread in-place without minting a duplicate active finding. | `ConfirmedFixEvidence` verifying `new_path`, `AuthorizedHumanOverride`, or full `AuthorizedRescan`. |
| `file_copy` | Holds obligation in `ambiguous-multi-target` across source and copied paths without duplicating threads. | `ConfirmedFixEvidence` covering **all** copied/source paths, `AuthorizedHumanOverride`, or full `AuthorizedRescan`. |
| `cross_file_move` | Unique symbol+kind move reuses `obligation_id` and updates existing thread; split multi-file move enters `ambiguous-multi-target`. | Unique target fix proof, or override / full `AuthorizedRescan` for split moves. |
| `line_deletion` | Outdated anchor or collaborator UI thread resolution never resolves the concern; falls back to `BODY_PLACEMENT` (`BODY_REASON_UNANCHORED`) and reopens prematurely resolved threads. | Positive `ConfirmedFixEvidence` on the surviving caller/router diff, `AuthorizedHumanOverride`, or full `AuthorizedRescan`. |
| `duplicate_code` | Multi-instance claim enters `ambiguous-multi-target` and withholds approval. | Multi-path `ConfirmedFixEvidence`, whole-obligation maintainer `AuthorizedHumanOverride`, or full `AuthorizedRescan` covering all candidate paths. |
| `whole_file_rewrite` | 100% diff coverage on a rewritten file without fix proof remains `omitted-uncertain`. | `ConfirmedFixEvidence` bound to original `resolution_criterion`, `AuthorizedHumanOverride`, or full `AuthorizedRescan`. |

### Multi-Target Override Scope and Rescan Coverage

- **Whole-obligation maintainer override:** `AuthorizedHumanOverride` requires
  authenticated maintainer/admin association (`OWNER`, `MEMBER`, or
  `COLLABORATOR` with override authority) bound to the exact `obligation_id` and
  `head_sha`. On `ambiguous-multi-target`, it dispositions the entire
  `candidate_paths` set explicitly; partial single-file code changes without an
  override must instead verify all implicated paths via `ConfirmedFixEvidence`.
- **Implicated-path rescan check:** `AuthorizedRescan(mode="full")` transitions
  drifted or multi-target obligations to `rescan-superseded` only when all
  present candidate/target paths are included in `reviewed_paths` and any
  uncertain native thread slot has been reconciled first.
- **Ordinary baseline, verification, and thread-publication integration:**
  `GitHubApplication.publish_review` (`enable_drift_tracking`,
  `drift_descriptors`, `host_outdated_threads`, `host_resolved_threads`),
  `GitHubApplication.apply_maintainer_command`, and `ReviewPublisher.publish`
  (`_plan_drift_round`, `project_drift_review_result`, `_commit_drift_success`,
  `_commit_drift_uncertain`, `drift_baseline`, `reconcile_drift_thread`)
  preserve `OriginalFindingCriterion` independently from `PresentLocationAnchor`
  and `NativeThreadBinding`, supersede outdated/renamed threads on confirmed
  native reviews, and hold `ambiguous-multi-target` / `omitted-uncertain`
  obligations against approval.

---

## 3. Artifact Authority and Restricted Re-Enrollment (`G14`)

Module: `packages/python/review-sensei-github/src/review_sensei_github/artifact_authority.py`

| Class | Artifact kinds | Mutation / deletion rule |
|---|---|---|
| `authority` | `session_ledger_marker`, `retained_result_part`, `retained_context_part` | Must be App-owned, single-instance, current-generation, and digest-valid. Deletion, human edit, duplication, or foreign authorship fails closed (`auto_recreate=False`). |
| `evidence` | `owned_readback_receipt`, `archive_chain_part` | Requires explicit `expected_sha256`; missing expected digest, missing artifact, duplicate carrier, or human edit refuses cleanly (`archive-part-missing` / `archive-part-corrupted`) without inventing history. |
| `projection` | `native_review_summary`, `inline_review_comment`, `check_run_text`, `comment_reaction` | Non-authoritative display only. Human edits, duplicates, or deletions never manufacture approval, erase obligations, or trigger comment-recreation spam (`authority_intact=True` reflects that authoritative ledger state is untouched). |

### Response Loss and Restricted Operator `reenroll`

- **HTTP response loss (`response_lost=True`) and idempotent readback:** On
  response loss, the target slot is locked in `uncertain` so no concurrent or
  duplicate write is dispatched before readback. If exact App-owned readback
  confirms an unedited single artifact matching `expected_sha256`, the write
  reconciles idempotently once (`reconciled-via-owned-readback`). If a human
  edited, duplicated, or deleted the artifact after response loss, state stays
  `uncertain-after-response-loss` with charges and obligations held.
- **Restricted `reenroll`:** `execute_restricted_operator_reenroll` (exposed via
  `GitHubApplication.execute_operator_reenroll` and guarded on
  `GitHubApplication.apply_maintainer_command` for `@reviewsensei review reenroll`)
  requires maintainer/admin authority on an open, non-draft PR where ledger
  status is `missing` (with `enrollment_witness_present=True` and cryptographic
  `enrollment_witness_sha256` bound into the audit digest) or `expired`, and
  **refuses** whenever active blocking obligations
  (`reenroll-blocked-by-active-obligations`), active reservations, ledger
  `publication_staging` / in-flight publication phases, uncertain writes
  (`reenroll-blocked-by-uncertain-write`), quarantine lineage, or assessment
  queues exist.
- **Ordinary consumer wiring:** `ReviewPublisher` and `GitHubApplication`
  evaluate `ObservedGitHubArtifact` and `verify_archive_chain` across
  `GitHubIssueCommentSessionLedger` (`_require_present_marker` and round
  preparation), `recover_retained_review_result` / `recover_review`,
  `_eligibility_from_reviews`, `_blocking_root_facts` / `_suppression_root_facts`,
  `_publish_check_state`, `narrative_archive`, and `_reconcile_marker`
  (`response_loss_hold` / `reconcile_response_loss_hold`), never auto-recreating
  deleted inline threads or uncertain reviews.

---

## 4. Repository and Trust Transitions (`G15`)

Module: `packages/python/review-sensei-github/src/review_sensei_github/trust_transitions.py`

All seven transitions are evaluated across `before_admission`,
`before_native_effect`, and `after_unknown_effect`:

1. `repository_rename`: Same `repository_id` and `owner_id`, changed slug ->
   refuses cross-slug writes (`cross_repository_write_blocked=False` for
   same-owner rename) until authenticated migration.
2. `ownership_transfer`: Changed `owner_id` or `owner_login` (even with
   **identical numeric `repository_id`**) -> refuses with
   `cross_repository_write_blocked=True`; requires explicit
   `previous_owner_transfer_attestation_sha256` (domain-separated SHA-256 via
   `compute_transfer_attestation_sha256` over `repository_id`,
   `previous_owner_id`, `previous_owner_login`, `new_owner_id`,
   `new_owner_login`, `pull_request`, `head_sha`, `permission_epoch`, and
   `previous_owner_witness`) plus new-owner maintainer attestation bound to
   `target.actor_id`.
3. `fork_status_change`: Fork boundary (`enrolled.is_fork` or `observed.is_fork`,
   including unfork `True -> False`) -> refuses hosted writes unconditionally
   (`unsupported-fork-boundary`) and blocks migration.
4. `app_reinstall`: Changed `installation_id` -> refuses until authenticated
   migration verifies the new installation and binds `installation_id` in
   `audit_sha256`.
5. `repository_selection_change`: Unselected repository -> refuses with
   `restore-repository-selection` + migration route.
6. `permission_revoke_or_restore`: Missing required permissions or unverified
   monotonic `permission_epoch` change (epoch regressions fail closed) ->
   refuses until permissions are restored and re-verified with an incremented
   `permission_epoch`.
7. `actor_privilege_downgrade`: Same-actor privilege drop or a new actor below
   `actor_meets_operation(operation)` -> refuses until an authorized
   maintainer/admin (`migration.operator_id == target.actor_id`) issues the
   command or override.

### Ordinary Admission, Native-Effect, and Migration Entry Points

- `GitHubApplication.enroll_trust_snapshot`, `trust_snapshot`, `trust_decisions`,
  `has_unknown_trust_effect`, `reconcile_unknown_trust_effect`, and
  `migrate_trust_boundary` bind `AuthenticatedTrustSnapshot` to
  `ReviewPublisher` and `ReviewApprovalFinalizer`.
- `before_admission` executes at the entry of `GitHubApplication.publish_review`,
  `GitHubApplication.apply_maintainer_command`, and
  `GitHubApplication.generate_and_publish_reply` before broker capability
  exchange, session reservation, or provider execution.
- `before_native_effect` executes inside `ReviewPublisher._preflight_pr` and
  `ReviewApprovalFinalizer._preflight` against live PR `base.repo.id`,
  `base.repo.full_name`, `base.repo.fork`, and `base.repo.owner` (`id`, `login`)
  before any GitHub review, thread, check, or approval mutation.
- `after_unknown_effect` executes on unconfirmed review transport/reconciliation
  failures, holding `(repository_id, pull_request)` in
  `unknown-effect-reconciliation-required` until explicit reconciliation or
  authenticated migration.

### Universal Review Recovery Guarantee Summary

Every held or refused condition preserves existing obligations and charges
(`fresh_allowance_minted=False`) and exposes a deterministic recovery route:
- Unresolved / omitted / drifted findings -> `verify` (with `ConfirmedFixEvidence`),
  `override` / `accept-risk` (`AuthorizedHumanOverride`), or `rescan`
  (`AuthorizedRescan`).
- Partial coverage -> `rescan` or maintainer `accept-coverage-uncertainty`.
- Deleted witness-backed or expired clean ledger marker -> restricted operator
  `reenroll`.
- Legitimate rename, reinstall, selection/permission restore, or dual-attested
  transfer -> `verify_and_migrate_trust_boundary` (with
  `reconciled_effect_sha256` first if `after_unknown_effect`).
