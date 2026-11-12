# ADR 0133: Historical review recovery matrix and nonterminal boundaries

- Status: Proposed
- Date: 2026-11-11
- Decision owner/reviewers: repository owner
- Related issue/PR: Stage D historical review recovery and attempt-evidence packets (G03, G29, G30, G31)
- Implementation: `fix/stage-d-g03-g29-g30-g31-historical-evidence`

## Context

Interrupted or historical ReviewSensei runs can leave pull requests in distinct
nonterminal states that look superficially similar in operator logs and must
remain strictly separated across historical incidents:

1. **Historical completion and nonterminal accounting gaps (G03 — including ReviewSensei `#443` run `43074015855` generation `11`):**
   Legacy or partially settled reviews may lack an explicit completion witness
   (`CompletionState.UNKNOWN` or `UNCONFIRMED`), sit across head changes, partial
   text coverage, or quarantined tail/body-limit contracts, or leave
   `transaction.phase == "publication_succeeded"` with null `terminal_transaction`,
   null `terminal_action`, null `analysis_abort`, and absent close diagnostics
   (whereas valid command actions carry a terminal `action` receipt with a null
   review `terminal_transaction` marker).
2. **Pre-entry aborts vs unconfirmed provider/remote aborts (G29 — `SiteVault #142` and `SiteVault #153`):**
   A workflow stopped before transaction preparation (`phase=no-transaction`) has
   a different side-effect and liability profile from a run that reserved a
   session slot, entered provider inference, or left an uncertain GitHub request.
3. **Zero-decision human reassessment vs failed finalizer (G30 — `ReviewSensei #408`):**
   When a maintainer posts a prose reassessment comment without explicit
   disposition IDs (`decision_count=0`), an interruption or completion of a
   zero-decision reply (`finalizer_entered=False`) differs fundamentally from an
   interruption after reassessment durably updated eligibility and entered
   finalization (`finalizer_entered=True`), and merged historical PRs must never
   be rerun live.
4. **Nonterminal publication overflow vs response loss (G31 — `TaskAlong #41`):**
   Historical prepublication overflows outside the pinned `c7` executor catalog
   (such as TaskAlong `#41` on `be1`) failed local `65,536`-byte review-body
   framing before any `POST /pulls/{pr}/reviews` write, whereas socket resets or
   runner terminations during `POST /pulls/{pr}/reviews` leave remote review
   creation uncertain until live reviews are reconciled.

Treating any of these states as interchangeable, or mistaking offline diagnostic
classifiers and formatter checks for live settlement receipts, risks stranding
pull requests, mutating historical evidence, or silently waiving accounting,
attribution, or byte/API ceilings.

## Decision

Introduce four deterministic, non-authoritative (`authority=False`) qualification,
attempt-evidence packet, and recovery-descriptor modules in `review_sensei_github`,
paired with bounded replay/restart routes that preserve every existing safety
invariant:

1. **Historical recovery route matrix and `AttemptEvidencePacket` (`recovery_route_advisor.py`, `historical_completion.py`, `review_completion_recovery.py` — G03):**
   - Classify historical and current records into a closed state vocabulary and
     deterministic recovery route (`ordinary_rollover`,
     `automatic_historical_completion`, `same_run_attempt2_retained_replay`,
     `pre_entry_abort_rollover`, `pending_reassessment_continuation`,
     `targeted_human_disposition`, `quarantine_published_tail_isolation`,
     `quarantine_prepublication_isolation`, `controlled_serialized_restart`),
     and emit immutable `AttemptEvidencePacket` records (`authority=False`)
     binding original identities, installed executor, funding, intent/execution,
     retained results, publication/check/cleanup/terminal receipts, and missing
     proofs without mutating ledgers.
   - Preserve valid terminal command actions whose review `terminal_transaction`
     marker is null (`state_code="terminal_action_without_review_transaction"`,
     `terminal_settlement_form="authenticated_terminal_action_null_review_marker"`,
     or `state_code="terminal_action_settled"` /
     `terminal_settlement_form="authenticated_terminal_action_with_prior_review_marker"`),
     while classifying ReviewSensei `#443` run `43074015855` generation `11`
     (`publication_succeeded` with null terminal/action/abort markers and
     missing close diagnostics) as
     `state_code="same_head_unclosed_publication_succeeded"` (or
     `"superseded_head_unclosed_publication_succeeded"` across head advance),
     `terminal_settlement_form="unsettled"`, and `inferred_cause="unknown"`
     with explicit missing-proof holds.
   - Never infer terminal action authentication from absent producer/result
     inputs. Direct classifiers and packets fail closed without a verified result;
     explicit authentication inputs are trusted verifier outputs only. Packet
     construction rejects a claimed authenticated settlement without positive
     authentication, while valid null-review-marker commands remain recognized.

2. **Pre-entry abort boundary and `NoTransactionAttemptPacket` (`pre_entry_abort_recovery.py` — G29):**
   - Bind original entry, reservation, caller dispatch, owner cessation, executor
     provenance, and abort/cleanup evidence in `NoTransactionAttemptPacket`
     (`authority=False`) for `SiteVault #142` and `SiteVault #153` shapes without
     resetting or reenrolling sessions.
   - Qualify `qualified_pre_entry_abort` (`pre_entry_abort_rollover`) strictly
     when an authenticated `analysis_abort` receipt, ceased owner, confirmed
     reservation release, clean caller dispatch (`uncertain == 0`), zero
     provider entry, and absence of late native effects are all proven; hold
     provider-entered, reservation-bearing, uncertain-dispatch, or unconfirmed
     runs in `no_transaction_provider_entered`,
     `no_transaction_active_reservation`, `no_transaction_unknown_dispatch`, or
     `no_transaction_unconfirmed_predecessor`, carrying full predecessor
     non-startup caller liability into `controlled-restart-registry-v1`.

3. **Pending reassessment attribution, `PendingReassessmentAttemptPacket`, and `OfflineContinuationQualification` (`pending_reassessment_recovery.py` — G30):**
   - Bind pending-work identity, pending reason, immutable accepted-result
     digests, exact source revision, and zero-funding / zero-inference
     continuation checks in `PendingReassessmentAttemptPacket` (`authority=False`,
     `offline_only=True`, `zero_decision_is_completion_witness=False`), keeping
     merged `ReviewSensei #408` strictly offline with zero live reruns and zero
     model inference.
   - Distinguish `unfinished_zero_decision_assessment` (`finalizer_entered=False`)
     from `failed_finalizer_after_reassessment` (`finalizer_entered=True`) using
     durable pending-receipt state, eligibility deltas (including newly resolved
     findings and `override`/`accept-risk` pending-set reductions), and
     finalizer-close observations; fail closed on duplicate receipt replay,
     head/base drift, or repeated zero-delta attempts reaching
     `NO_PROGRESS_ZERO_DECISION_LIMIT` (`2`).
   - Positive-cost continuation requires known sufficient original allowance.
     Missing accounting is an explicit hold, including for direct qualification
     construction. Zero-request checks require no funding but do not invent a
     known allowance. Preserve exact original/latest accepted-result digest
     equality independently of head identity.

4. **Nonterminal publication overflow, `PublicationOverflowAttemptPacket`, and response-loss qualification (`publication_overflow_recovery.py` — G31):**
   - Bind exact candidate/hold selection, publisher context, archive/installed
     provenance, cessation proof, formatter comparison, and boundary separation
     in `PublicationOverflowAttemptPacket` (`authority=False`,
     `formatter_fit_is_not_settlement=True`) for `TaskAlong #41` (`be1`) shapes.
   - Compare historical pre-archive framing against `ConfirmedNarrativeArchive`
     excerpt framing offline while verifying canonical-byte and SHA-256
     immutability of both `ReviewResult` and `ReviewConvergencePolicy`, enforcing
     `MAX_PUBLISHED_REVIEW_BODY_BYTES` (`65,536`), `MAX_PART_READS` (`64` ceiling /
     `60` ordinary), `MAX_RESULT_PARTS` (`32`), and the predecessor's frozen
     `512`-call caller allowance unchanged, and refusing silent enrollment of
     non-`c7` executors into `body-limit-prepublication-v1`.
   - Separate `confirmed_prepublication_overflow_before_write` from
     `predecessor_owner_active_hold`, `response_loss_unobserved_remote_hold`
     (`tx.phase == "publication_pending"` or `uncertain > 0`), and
     `response_loss_echo_observed_reconcilable` (persisted exact-marker review
     routed to `reconcile_observed_exact_review` or
     `reconcile_observed_review_hold`, refusing blind restart to prevent
     duplicate review `POST`s).
   - Record explicitly across all four packet families that offline classifiers,
     descriptors, and formatter-fit checks are not live settlement receipts
     (`authority=False`; live `verify` itself can mutate checks, threads, or
     approval even without inference).

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
