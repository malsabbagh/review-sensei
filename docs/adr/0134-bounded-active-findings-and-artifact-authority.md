# ADR 0134: Bounded active finding window, cross-file drift, and artifact/trust authority

- Status: Accepted
- Date: 2026-11-11
- Decision owner/reviewers: ReviewSensei maintainers; independent source review
- Linked GitHub Issue: pending; user-directed lifecycle gap qualification (G10, G16, G14, G15)
- Linked PR: Stage B ordinary publisher/application integration (`fix/stage-b-g10-g16-g14-g15-publisher-integration`)

## Context

Multi-round pull request reviews face four coupled failure modes when display,
identity, comment persistence, and repository trust are conflated:

1. **Coverage versus obligation collapse (G10):** Complete changed-file coverage
   in an incremental or fallback-full review does not prove an omitted prior
   blocking finding was fixed. Conversely, bounding the number of inline threads
   shown to reviewers must never silently drop non-displayed obligations, leak a
   public deferred-finding queue or count, or trigger unbounded continuation
   loops.
2. **Cross-file location and thread drift (G16):** Renames, copies, symbol
   extractions, line deletions, duplicate code, and whole-file rewrites move or
   invalidate diff anchors and cause GitHub to mark threads outdated or allow
   premature human resolution without resolving the underlying concern.
3. **Human edits and deletion of review artifacts (G14):** Reviewers and bots
   can edit or delete PR comments, review summaries, inline threads, checks, and
   reactions, including immediately after an HTTP response loss.
4. **Repository and privilege transitions (G15):** Repository renames, ownership
   transfers (where numeric `repository_id` remains unchanged while `owner_id`,
   slug, and installation authority change), fork conversions, GitHub App
   reinstalls, repository selection removals, permission revocations, and actor
   privilege downgrades can occur before admission, before a native write, or
   after an unconfirmed dispatch.

## Decision

### 1. Four-variable bounded active window (`review_sensei.active_finding_window`)

Separate four immutable state variables on every review round:
- `changed_file_coverage`: diff enumeration and per-file coverage state;
- `authoritative_decision_inventory`: complete durable ledger of all unresolved,
  omitted-uncertain, ambiguous, verified-fixed, overridden, and rescan-superseded
  obligations regardless of display capacity;
- `display_active_findings`: strict-cap (`max_active_findings`) priority-first
  window that preserves existing active/contingent native slots before promoting
  challengers;
- `approval_eligibility`: final gate requiring both satisfied coverage and zero
  blocking/uncertain/ambiguous obligations in the full authoritative inventory.

Public projections (`to_public_status`) are validated by
`assert_no_deferred_leakage`: they truthfully withhold approval and publish
explicit recovery routes (`verify`, `override`, `accept-risk`, `rescan`,
`accept-coverage-uncertainty`) without ever exposing a deferred-finding list or
count. Opt-in `#427` decision-provider receipts remain bounded proposals used
solely for intra-tier tiebreaking; they never drop obligations, downgrade
blocking status, or prove fixes.

**Relationship to ADR 0042:** ADR 0042 introduced `PublishedSlot` states
(`active`, `contingent`, `uncertain`, `released`) and bounded inline selection.
This ADR refines and extends ADR 0042 across multi-round lifecycles without
changing `PublishedSlot` wire or state semantics: ADR 0042's slot invariants
govern per-target reservation inside `display_active_findings`, while this ADR
separates `changed_file_coverage` and `authoritative_decision_inventory` so
non-displayed obligations remain durable approval gates.

### 2. Orthogonal criterion, anchor, and thread tracking (`review_sensei.finding_drift`)

Track `OriginalFindingCriterion` independently from `PresentLocationAnchor` and
`NativeThreadBinding` across `file_rename`, `file_copy`, `cross_file_move`,
`line_deletion`, `duplicate_code`, and `whole_file_rewrite`. Outdated anchors,
deleted lines, and host UI thread resolution never resolve an obligation.
Uniquely drifted active obligations update and reuse their existing native
thread in-place (falling back to `BODY_PLACEMENT` via `plan_finding_placement_detail`
when unanchored). Multi-target copies, split moves, and duplicate code hold in
`ambiguous-multi-target` until positive `ConfirmedFixEvidence` covers every
implicated path, an `AuthorizedHumanOverride` is recorded, or a complete
head-bound `AuthorizedRescan(mode="full")` (whose `reviewed_paths` include all
present candidate paths) supersedes prior state.

An `AuthorizedHumanOverride` on `ambiguous-multi-target` requires authenticated
maintainer/admin authority (`OWNER`, `MEMBER`, or `COLLABORATOR` meeting
`authorized_override` policy) bound to the exact `obligation_id` and `head_sha`
with a non-empty audit reason and `proof_sha256`. Recording an override is an
explicit whole-obligation disposition across the entire `candidate_paths` set;
partial-file fixes without maintainer override must instead supply
`ConfirmedFixEvidence` covering every implicated path.

### 3. Three-tier artifact classification (`review_sensei_github.artifact_authority`)

Classify GitHub review surfaces into:
- `authority`: `session_ledger_marker`, `retained_result_part`,
  `retained_context_part`;
- `evidence`: `owned_readback_receipt`, `archive_chain_part` (each requiring an
  explicit `expected_sha256` so missing expected digests fail closed);
- `projection`: `native_review_summary`, `inline_review_comment`,
  `check_run_text`, `comment_reaction`.

Mutations or deletions of `projection` surfaces never alter authority, erase
obligations, manufacture approval, or auto-recreate spam comments. Tampered,
duplicated, stale, or foreign `authority` markers and missing/corrupted
`archive_chain_part` carriers fail closed. After HTTP response loss, the target
slot enters an exclusive `uncertain` hold that blocks any concurrent or
duplicate re-dispatch before readback completes: exact unedited App-owned
readback reconciles idempotently at most once (`reconciled-via-owned-readback`),
whereas human edits, duplicates, or deletions preserve conservative
`uncertain-after-response-loss` holds. Restricted operator `reenroll` is
permitted only for authenticated maintainers/admins on open, non-draft PRs when
the marker is `missing` (backed by a cryptographic `enrollment_witness_sha256`
bound into the audit digest from verified prior delivery/check state) or
`expired` and zero active blocking obligations, reservations, staged
publications, in-flight publication phases, uncertain writes, quarantine
lineage, or assessment queues exist.

### 4. Repository and trust-boundary transitions (`review_sensei_github.trust_transitions`)

Bind `repository_id`, `owner_id`, `owner_login`, `repository_slug`, `is_fork`,
`installation_id`, `repository_selected`, `granted_permissions`, monotonic
`permission_epoch`, and actor authority in `AuthenticatedTrustSnapshot`.
Classify all seven transitions separately across `before_admission`,
`before_native_effect`, and `after_unknown_effect`. Both fork directions
(`False -> True` and `True -> False` unfork) fail closed on hosted write
boundaries (`unsupported-fork-boundary`) and are excluded from automated trust
migration. Enforce that unchanged numeric `repository_id` alone never proves
`ownership_transfer` trust: `verify_and_migrate_trust_boundary` binds
`migration.operator_id`, `operator_login`, `operator_association`, and
`operator_permission` to the authenticated `target` snapshot (requiring
override-level authority on `target`), rejects `permission_epoch` regressions,
and for `ownership_transfer` requires a domain-separated
`previous_owner_transfer_attestation_sha256` computed by
`compute_transfer_attestation_sha256` over `(repository_id, previous_owner_id,
previous_owner_login, new_owner_id, new_owner_login, pull_request, head_sha,
permission_epoch, previous_owner_witness)` and verified against expected prior-owner control
plane records. Legitimate renames, reinstalls, permission/selection
restorations, and dual-attested transfers recover while preserving 100% of
session charges, reservations, and obligation history
(`fresh_allowance_minted=False`).

## Scope

Core domain modules (`review_sensei.active_finding_window`,
`review_sensei.finding_drift`), GitHub adapter authority/trust modules
(`review_sensei_github.artifact_authority`,
`review_sensei_github.trust_transitions`), ordinary review/verify/command/reply
entry-point integration in `review_sensei_github.publication` (`ReviewPublisher`,
`ReviewApprovalFinalizer`) and `review_sensei_github.application`
(`GitHubApplication`), and deterministic end-to-end qualification suites for
G10, G16, G14, and G15. Existing wire schemas, workflow definitions, broker
endpoints, and historical incident ledgers remain unchanged, and all new guards
remain 100% backward-compatible with legacy callers.

## Consequences

- Approval is never granted when omitted, drifted, copied, or non-displayed
  obligations remain unresolved, even at 100% diff coverage.
- Every refusal or hold exposes a deterministic, bounded recovery route so a
  legitimate PR can always reach completion and approval without manual database
  edits or unbounded loops.
- Deleted or human-edited GitHub comments cannot erase obligations, spoof ledger
  state, or trigger comment-recreation storms.
- Cross-owner repository transfers sharing a numeric `repository_id` cannot
  inherit silent write or budget authority.

## Alternatives considered

- **Collapsing non-displayed findings into a public deferred queue:** Rejected
  by product constraints; exposes noisy counts and encourages unbounded loop
  expectations.
- **Treating outdated GitHub threads or deleted files as fixed:** Rejected; a
  moved or rewritten defect remains live until positive fix proof or explicit
  maintainer disposition.
- **Trusting numeric `repository_id` across owner changes:** Rejected; GitHub
  preserves `repository_id` across user-to-org and org-to-org transfers where
  governance and secret boundaries change completely.

## Validation

Qualified by provider-free deterministic suites:
- `tests/test_active_window_lifecycle_g10.py`
- `tests/test_finding_location_drift_g16.py`
- `tests/test_artifact_mutation_resilience_g14.py`
- `tests/test_repository_trust_transitions_g15.py`

## Rollout and rollback

Wired into ordinary `GitHubApplication` (`publish_review`,
`apply_maintainer_command`, `generate_and_publish_reply`, `recover_review`,
`execute_operator_reenroll`, `migrate_trust_boundary`) and `ReviewPublisher` /
`ReviewApprovalFinalizer` entry points behind additive options and enrolled
snapshots without live repository access mutations. Rollback reverts the
additive integration commits cleanly without schema migration or historical
ledger mutation.

## Follow-up work

Coordinate canonical writer fencing (G02/G08) and full recovery-context
retention (G17/G34) before enabling automated cross-process takeover.
