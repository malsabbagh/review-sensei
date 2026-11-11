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
| `duplicate_code` | Multi-instance claim enters `ambiguous-multi-target` and withholds approval. | Multi-path `ConfirmedFixEvidence`, `AuthorizedHumanOverride`, or full `AuthorizedRescan`. |
| `whole_file_rewrite` | 100% diff coverage on a rewritten file without fix proof remains `omitted-uncertain`. | `ConfirmedFixEvidence` bound to original `resolution_criterion`, `AuthorizedHumanOverride`, or full `AuthorizedRescan`. |

---

## 3. Artifact Authority and Restricted Re-Enrollment (`G14`)

Module: `packages/python/review-sensei-github/src/review_sensei_github/artifact_authority.py`

| Class | Artifact kinds | Mutation / deletion rule |
|---|---|---|
| `authority` | `session_ledger_marker`, `retained_result_part`, `retained_context_part` | Must be App-owned, single-instance, current-generation, and digest-valid. Deletion, human edit, duplication, or foreign authorship fails closed (`auto_recreate=False`). |
| `evidence` | `owned_readback_receipt`, `archive_chain_part` | Missing or human-edited archive-chain comments refuse cleanly (`archive-part-missing` / `archive-part-corrupted`) without inventing history. |
| `projection` | `native_review_summary`, `inline_review_comment`, `check_run_text`, `comment_reaction` | Non-authoritative display only. Human edits or deletions never manufacture approval, erase obligations, or trigger comment-recreation spam. |

### Response Loss and Restricted Operator `reenroll`

- **HTTP response loss (`response_lost=True`):** If exact App-owned readback
  confirms an unedited single artifact matching `expected_sha256`, the write
  reconciles once (`reconciled-via-owned-readback`). If a human edited or
  deleted the artifact after response loss, state stays
  `uncertain-after-response-loss` with charges and obligations held.
- **Restricted `reenroll`:** `execute_restricted_operator_reenroll` requires
  maintainer/admin authority on an open, non-draft PR where ledger status is
  `missing` (with enrollment witness) or `expired`, and **refuses** whenever
  active blocking obligations, active reservations, uncertain writes, quarantine
  lineage, or assessment queues exist.

---

## 4. Repository and Trust Transitions (`G15`)

Module: `packages/python/review-sensei-github/src/review_sensei_github/trust_transitions.py`

All seven transitions are evaluated across `before_admission`,
`before_native_effect`, and `after_unknown_effect`:

1. `repository_rename`: Same `repository_id` and `owner_id`, changed slug ->
   refuses cross-slug writes until authenticated migration.
2. `ownership_transfer`: Changed `owner_id` or `owner_login` (even with
   **identical numeric `repository_id`**) -> refuses; requires explicit
   `previous_owner_transfer_attestation_sha256` plus new-owner maintainer
   attestation.
3. `fork_status_change`: Fork target (`is_fork=True`) -> refuses hosted writes
   unconditionally (`unsupported-fork-boundary`).
4. `app_reinstall`: Changed `installation_id` -> refuses until authenticated
   migration verifies the new installation.
5. `repository_selection_change`: Unselected repository -> refuses with
   `restore-repository-selection` + migration route.
6. `permission_revoke_or_restore`: Missing required permissions or unverified
   permission epoch change -> refuses until permissions are restored and
   re-verified.
7. `actor_privilege_downgrade`: Actor below operation privilege -> refuses until
   an authorized maintainer/admin issues the command or override.

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
