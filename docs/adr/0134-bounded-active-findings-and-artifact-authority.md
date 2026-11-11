# ADR 0134: Bounded active finding window, cross-file drift, and artifact/trust authority

- Status: Proposed
- Date: 2026-11-11
- Decision owner/reviewers: ReviewSensei maintainers; independent source review
- Linked GitHub Issue: pending; user-directed lifecycle gap qualification (G10, G16, G14, G15)
- Linked PR: pending; independent prospective qualification

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
head-bound `AuthorizedRescan(mode="full")` supersedes prior state.

### 3. Three-tier artifact classification (`review_sensei_github.artifact_authority`)

Classify GitHub review surfaces into:
- `authority`: `session_ledger_marker`, `retained_result_part`,
  `retained_context_part`;
- `evidence`: `owned_readback_receipt`, `archive_chain_part`;
- `projection`: `native_review_summary`, `inline_review_comment`,
  `check_run_text`, `comment_reaction`.

Mutations or deletions of `projection` surfaces never alter authority, erase
obligations, manufacture approval, or auto-recreate spam comments. Tampered,
duplicated, stale, or foreign `authority` markers and missing/corrupted
`archive_chain_part` carriers fail closed. After HTTP response loss, exact
unedited App-owned readback reconciles once, whereas human edits or deletions
preserve conservative `uncertain-after-response-loss` holds. Restricted operator
`reenroll` is permitted only for authenticated maintainers/admins on open,
non-draft PRs when the marker is `missing` (with enrollment witness) or
`expired` and zero active blocking obligations, reservations, uncertain writes,
quarantine lineage, or assessment queues exist.

### 4. Repository and trust-boundary transitions (`review_sensei_github.trust_transitions`)

Bind `repository_id`, `owner_id`, `owner_login`, `repository_slug`, `is_fork`,
`installation_id`, `repository_selected`, `granted_permissions`, and actor
authority in `AuthenticatedTrustSnapshot`. Classify all seven transitions
separately across `before_admission`, `before_native_effect`, and
`after_unknown_effect`. Enforce that unchanged numeric `repository_id` alone
never proves `ownership_transfer` trust. Legitimate renames, reinstalls,
permission/selection restorations, and dual-attested transfers recover via
`verify_and_migrate_trust_boundary` while preserving 100% of session charges,
reservations, and obligation history (`fresh_allowance_minted=False`).

## Scope

Prospective core and GitHub-adapter qualification modules and deterministic test
suites for G10, G16, G14, and G15. Existing wire schemas, workflow definitions,
broker endpoints, and historical incident ledgers remain unchanged and are not
live-activated by this ADR.

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

Normal source review and CI precede any release or wiring into live hosted
entrypoints. Rollback removes these modules cleanly without schema migration or
historical ledger mutation.

## Follow-up work

Coordinate canonical writer fencing (G02/G08) and full recovery-context
retention (G17/G34) before enabling automated cross-process takeover.
