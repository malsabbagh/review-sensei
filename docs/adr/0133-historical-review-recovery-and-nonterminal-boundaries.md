# ADR 0133: Historical review recovery matrix and nonterminal boundaries

- Status: Proposed
- Date: 2026-11-11
- Decision owner/reviewers: repository owner
- Related issue/PR: Batch 3 historical review recovery gap closure (G03, G29, G30, G31)
- Implementation: `fix/gaps-g03-g29-g30-g31-historical-review-recovery`

## Context

Interrupted or historical ReviewSensei runs can leave pull requests in distinct
nonterminal states that look superficially similar in operator logs:

1. **Historical completion gaps (G03):** Legacy or partially settled reviews may
   lack an explicit completion witness (`CompletionState.UNKNOWN` or
   `UNCONFIRMED`) or sit across head changes, partial text coverage, or
   quarantined tail/body-limit contracts.
2. **Pre-entry aborts vs unconfirmed provider/remote aborts (G29):** A workflow
   stopped before transaction preparation (`phase=no-transaction`) has a
   different side-effect and liability profile from a run that reserved a
   session slot, entered provider inference, or left an uncertain GitHub request.
3. **Zero-decision human reassessment vs failed finalizer (G30):** When a
   maintainer posts a prose reassessment comment without explicit disposition
   IDs (`decision_count=0`), an interruption before reassessment completes
   (`finalizer_entered=False`) differs fundamentally from an interruption after
   reassessment finished and entered finalization (`finalizer_entered=True`).
4. **Nonterminal publication overflow vs response loss (G31):** Historical
   prepublication overflows outside the pinned `c7` executor catalog (such as
   TaskAlong `#41` on `be1`) failed local `65,536`-byte review-body framing
   before any `POST /pulls/{pr}/reviews` write, whereas socket resets or runner
   terminations during `POST /pulls/{pr}/reviews` leave remote review creation
   uncertain until live reviews are reconciled.

Treating any of these states as interchangeable risks either stranding pull
requests permanently or silently waiving accounting, attribution, or byte/API
ceilings.

## Decision

Introduce four deterministic, non-authoritative (`authority=False`) qualification
and recovery-descriptor modules in `review_sensei_github`, paired with bounded
replay/restart routes that preserve every existing safety invariant:

1. **Historical recovery route matrix (`recovery_route_advisor.py`, `historical_completion.py`, `review_completion_recovery.py` — G03):**
   - Classify historical and current records into a closed state vocabulary and
     deterministic recovery route (`ordinary_rollover`,
     `automatic_historical_completion`, `same_run_attempt2_retained_replay`,
     `pre_entry_abort_rollover`, `pending_reassessment_continuation`,
     `targeted_human_disposition`, `quarantine_published_tail_isolation`,
     `quarantine_prepublication_isolation`, `controlled_serialized_restart`).
   - Treat missing or unconfirmed completion witnesses as non-settled holds that
     must reconcile live native GitHub artifacts before claiming completion.

2. **Pre-entry abort boundary qualification (`pre_entry_abort_recovery.py` — G29):**
   - Qualify `qualified_pre_entry_abort` (`pre_entry_abort_rollover`) strictly
     when an authenticated `analysis_abort` receipt, ceased owner, confirmed
     reservation release, clean caller dispatch (`uncertain == 0`), zero
     provider entry, and absence of late native effects are all proven.
   - Hold provider-entered, reservation-bearing, uncertain-dispatch, or
     unconfirmed runs in `no_transaction_provider_entered`,
     `no_transaction_active_reservation`, `no_transaction_unknown_dispatch`, or
     `no_transaction_unconfirmed_predecessor`, carrying full predecessor
     non-startup caller liability into `controlled-restart-registry-v1`.

3. **Pending reassessment attribution and finalizer separation (`pending_reassessment_recovery.py` — G30):**
   - Distinguish `unfinished_zero_decision_assessment` (`finalizer_entered=False`)
     from `failed_finalizer_after_reassessment` (`finalizer_entered=True`) using
     durable pending-receipt state, eligibility deltas (including newly resolved
     findings and `override`/`accept-risk` pending-set reductions), and
     finalizer-close observations.
   - Preserve exact source comment ID, actor, body SHA-256, and head SHA across
     delayed or fresh-process recovery; fail closed on duplicate receipt replay,
     head/base drift, or repeated zero-delta reassessment attempts reaching
     `NO_PROGRESS_ZERO_DECISION_LIMIT` (`2`).

4. **Nonterminal publication overflow and response-loss qualification (`publication_overflow_recovery.py` — G31):**
   - Compare historical pre-archive framing against `ConfirmedNarrativeArchive`
     excerpt framing offline while verifying canonical-byte and SHA-256
     immutability of both `ReviewResult` and `ReviewConvergencePolicy`.
   - Enforce `MAX_PUBLISHED_REVIEW_BODY_BYTES` (`65,536`), `MAX_PART_READS`
     (`64` ceiling / `60` ordinary), and the predecessor's frozen `512`-call
     caller allowance without enlargement, require `archive_excerpt_fits_ceiling`
     on both same-head replay and `c7` superseded-head quarantine, and refuse
     silent enrollment of non-`c7` executors into `body-limit-prepublication-v1`.
   - Separate `confirmed_prepublication_overflow_before_write` from
     `response_loss_unobserved_remote_hold` (`tx.phase == "publication_pending"`
     or `uncertain > 0`) and `response_loss_echo_observed_reconcilable` (live
     exact-marker review already persisted on GitHub, routed to
     `reconcile_observed_exact_review` or `reconcile_observed_review_hold` and
     refusing blind restart to prevent duplicate review `POST`s).

## Scope

Hosted GitHub recovery classification, offline formatter comparison, bounded
`controlled-restart-registry-v1` descriptor construction, operator recovery
guidance (`docs/approval-recovery.md`), and offline regression suites. No public
JSON schema, caller API allowance, GitHub byte ceiling, token scope, or `c7`
quarantine catalog constant is expanded.

## Consequences

- Every Batch 3 historical/interrupted scenario has both a fail-closed
  qualification boundary and a deterministic bounded recovery path (`verify`,
  same-head retained archive replay, or `controlled-restart-registry-v1`).
- Advisory descriptors (`authority=False`) never bypass ledger CAS, review/index
  markers, or `api_run_accounting` conservation.
- Non-`c7` historical overflows (such as TaskAlong `#41`) recover via same-head
  `ConfirmedNarrativeArchive` replay or `controlled-restart-registry-v1` on an
  advanced head without mutating retained review artifacts.

## Alternatives considered

- **Enlarge the `65,536`-byte body limit or `512`-call caller allowance:**
  rejected; GitHub's review-body ceiling is external and enlarging API budgets
  violates bounded-execution accounting.
- **Append `be1` or arbitrary executors to the `c7` body-limit quarantine catalog:**
  rejected; quarantine contracts bind audited historical blobs and commits.
- **Reset `phase=no-transaction` or `publication_failed` state unconditionally:**
  rejected; provider-entered runs and lost HTTP responses carry real liability
  and potential remote effects that must be reconciled first.
- **Re-run model inference when finalization fails after reassessment:**
  rejected; `verify` reconciles threads, checks, and native approval without
  spending inference or risking finding drift.

## Validation

Offline socket-isolated unit and integration suites (`test_historical_recovery_matrix_g03.py`,
`test_no_transaction_abort_g29.py`, `test_pending_reassessment_g30.py`,
`test_publication_overflow_recovery_g31.py`, `test_approval_recovery_instructions.py`),
`ruff`, `mypy`, public contract/schema validators, and `>=85%` branch coverage on
all new recovery modules.

## Rollout and rollback

Ships in `review-sensei-github` alongside updated operator documentation in
`docs/approval-recovery.md`. Reverting removes the advisory classifiers and
descriptors without invalidating any persisted session, receipt, or review marker.
