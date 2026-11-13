# Finding, Drift, Artifact, and Trust Lifecycle Reference

This reference describes the current source contracts for **G10**, **G16** and
**G14**, with the separate **G15** trust hooks. The versioned lifecycle and native
union implementations are under final exact-head test/CI qualification, not a
production deployment or historical-settlement claim. See
[ADR 0134](adr/0134-bounded-active-findings-and-artifact-authority.md) for the
acceptance boundary and unchanged limits.

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
3. `NativeThreadBinding` — actual GitHub `thread_id`, `comment_id`, placement,
   and `PublishedSlot` state, or an explicit uncertain `intent:` handle with no
   numeric comment ID until authentic native readback confirms creation.

The current source edits uniquely moved findings with `PATCH` on the retained
comment rather than manufacturing a replacement thread identity. GitHub keeps
that comment's original review/commit/anchor; `PresentLocationAnchor` tracks the
current source location independently. Exact old body/native identity checks
precede the edit, and owned readback confirms the new body. A review ID alone
cannot confirm every displayed finding.

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

- **Sticky ambiguity:** All previously implicated candidate paths and
  `ambiguous-multi-target` survive fresh processes and later empty or single-target
  results. New structural evidence may add candidates; omission/deletion alone
  cannot remove the unresolved uncertainty.
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
  and `NativeThreadBinding`, reuse authenticated native roots for unique drift,
  resolve only warranted completed obligations, and hold `ambiguous-multi-target` / `omitted-uncertain`
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
  response loss, the persisted pre-effect intent and target slot remain
  `uncertain`. Under the supported session-owner boundary no duplicate write is
  dispatched before readback; this is not independent all-writer fencing. If exact App-owned readback
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

### Recovery routes and prerequisites

Held/refused conditions preserve obligations and original charges
(`fresh_allowance_minted=False`). These routes still require authentic retained
evidence, current authorization and capacity; they are not guaranteed completion
paths when evidence was never retained or has disappeared:
- Unresolved / omitted / drifted findings -> `verify` (with `ConfirmedFixEvidence`),
  `override` / `accept-risk` (`AuthorizedHumanOverride`), or `rescan`
  (`AuthorizedRescan`).
- Partial coverage -> `rescan` or maintainer `accept-coverage-uncertainty`.
- Deleted witness-backed or expired clean ledger marker -> restricted operator
  `reenroll`.
- Legitimate rename, reinstall, selection/permission restore, or dual-attested
  transfer -> `verify_and_migrate_trust_boundary` (with
  `reconciled_effect_sha256` first if `after_unknown_effect`).


## 5. Durable lifecycle authority and restart

The ordinary application selects the current session root before restoring a
supported `finding-lifecycle-reference-v1` or `finding-lifecycle-reference-v2`.
Its purpose-bound `finding-lifecycle-evidence-v2` envelope contains the closed
lifecycle codec and native-effect journal; authenticated version 1 evidence
remains readable. Producer, repository numeric identity, PR, snapshot,
accepted operation, root generation and predecessor lineage are validated
independently of the payload. Publisher caches are only reconstructed working
state. The reference remains after transaction completion and cannot be silently
removed by an older writer.

Reference version 2 carries bounded accepted-action semantic history beside the
unchanged finding manifest and lineage. Introduction or amendment requires the
exact authenticated inline terminal action result, not a caller-supplied summary.
The closed `caller-action-result-v2` carrier retains the existing compressed
business envelope and places a fixed-width SHA-256 history commitment outside
compression. Its reader verifies the full canonical history from that exact
current or archived root's reference; a missing or mismatching history holds.
Version 1 carriers with embedded history remain readable. The commitment is
integrity binding, never dispatch or settlement authority.

Subsequent finding checkpoints preserve the history. Its bytes count against the
same 8 KiB reference and 32 KiB root limits, including the prospective terminal
summary before effects. Keeping one canonical copy avoids relying on the future
compression ratio of unknown semantic digests. Partitioned finding evidence does
not require rewriting old parts to carry that summary; partitioned action-result
proof remains a separate unsupported boundary. No progress is inferred from
request counts, changed root hashes or host-thread resolution alone.

The payload preserves all 13 `ReviewComment` fields explicitly:
`path`, `line`, `body`, `blocking`, `severity`, `category`, `fix_effort`, `symbol`,
`defect_kind`, `evidence_id`, `side`, `effective_blocking`, `needs_human`.
Public comment/result serializers deliberately omit the last two admission
fields. They cannot serve as lossless restart codecs. Original criteria, concern
history, input identity, candidate paths, slots, disposition proofs and unknown
native intents are also retained. Comment fields excluded from dataclass equality
must be tested field by field.

Before effects, the journal stages/read-backs the planned state and exact intent
and preflights the full future receipt/storage/native/accounting tail. A confirmed
response is retained; an absent response leaves a pending intent. A new process
reconciles exact owned native readback instead of repeating inference or blindly
reposting. A changed, duplicate, foreign or missing native object remains a hold.
Failure after a known success does not turn that success into an absent effect.

A known multi-effect batch is admitted as a whole before its first mutation.
Its pending prefixes and final receipt state must fit cumulative retention,
effect slots and the original funding domains. This capacity preview is not
effect authority: each mutation still requires its own exact native binding,
fresh fence and readback. Unknown callback costs cannot qualify a cleanup batch.
Legacy multi-target supersession without a qualified complete fence remains a
hold. Targets still retained by the current active window or drift state are not
obsolete supersession targets in the first place.

For a source-qualified ordered GraphQL cleanup, the first unconfirmed or skipped
expected mutation stops new mutations in that batch. Confirmed prefix receipts
remain retained; already-resolved targets are still authenticated independently.
Exact pending-prefix compression sizes apply only to that enforced finite order.
Stop states and final receipt envelopes retain conservative bounds. Generic or
custom transports cannot claim the source's exact-sizing proof.
For a command admitted by this source plan, failed cleanup also stops later
native check and approval writes. Existing check/approval observations are not
reported as fresh completion. The final journal retains confirmed progress and
any pending intent; unknown dispatch keeps the action pending. A refusal before
business dispatch instead settles an explicit no-op when its original control
and terminal obligations can settle.

Command admission also separates a detached disposition preview from the actual
selected root. The preview can size the prospective root and staged snapshot
parts, but cannot supply authority, synthetic part reads or a replacement funding
allocation. A single source timestamp is shared with the actual disposition
write. The complete body-update, cleanup, check, conditional approval and terminal
tail must be funded before business writes; an unqualified execution reader or
unknown tail is an early refusal, not a successful command with a promised retry.
The original action's audit and terminal settlement obligations still apply.

Check discovery uses the existing checks-scoped capability, not the review-only
credential. For this qualified command path its one exchange moves before
business admission and is reused by completion; a refused command keeps the
original charge but performs no check write. No permission scope is broadened.
Exact same-caller lifecycle reuse can avoid a duplicate restore only when the
owned post-command root, unchanged lifecycle reference, admitted intent and full
decision projection match the source plan. Lost custody is a hold, not an unpriced
fallback read or installation of the preview root.

`native-review-union-v2` covers reused current inline instances on actual older
native roots as well as newly allocated part comments. The existing bounded
census verifies root/comment/review IDs, numeric producer, original native commit,
exact current body and final-frame digest. Replay allocates only new items.
Version 1 remains readable, but cannot silently consume or strip version 2
reused-root membership. Body-placed slot receipts bind the actual native review
and exact indexed explanation, not a nonexistent inline thread.

### Enrollment, migration and refusal

- Provably new admission creates its first lifecycle reference before effects.
- Supported current evidence is restored before planning, command inventory,
  verification and approval decisions.
- Migrating legacy state requires complete authentic lifecycle, native-effect
  and original-funding evidence. Older session-metadata migration and native-union
  v1 compatibility do not themselves reconstruct missing findings.
- A baseline-only enrolled session retains
  `durable_finding_inventory_recovery_required`. Invalid roots, foreign producers,
  broken lineage, missing parts and unsupported versions fail closed.
- Relabeling coverage, removing a baseline/reference, resolving a host thread,
  resetting the session or retrying unchanged is not a recovery mechanism.

Deploy compatible readers before enabling new writers. Rollback preserves the
reference, original receipts and funding; it must not ask an old reader to erase
state it does not understand. No live activation, reenrollment or release is
included in this source contract.

### Capacity and privacy

The feature uses the existing session evidence carrier, not transaction-only
`publication_staging-v2` or new fields in `HumanInventoryStore`. Inline compression
and bounded parts are lossless and do not recursively embed previous snapshots.
All limits apply together: 256 evidence parts, 200 comment allocations, 8 MiB
retained evidence, 512 KiB responses, 8 KiB queries, and the existing 32-part/1 MiB
`ResultPartStore` limit. The lifecycle payload has a 1 MiB ceiling. Native unions
retain their 4 KiB frame, 50-existing-root and ten-read census bounds. Existing
API/provider budgets and protected completion tails are unchanged. Overflow
refuses before an unrepresentable effect; it grants no extra census or allowance.

Representation selection includes the joint future root, not just the current
payload. Inline findings must leave room for retained semantic history and the
terminal result. A lifecycle-bearing admitted action may move dispositions into
their existing authenticated snapshot representation before effects when the
inline array would consume that reserve. The preview and writer use the same
selection and price its parts and readbacks; this does not truncate decisions or
enable a new snapshot writer.

Retained data includes normalized validated finding prose, internal admission
flags, hidden obligations and exact native intent commitments needed for replay. It
excludes authentication tokens, prompts, raw provider responses and newly captured
raw provider context/diffs. Public summaries/status do not expose a deferred
finding list/count. Storage confidentiality is still the existing carrier's
policy: hosted issue-comment evidence has repository visibility, and compression
is not encryption. This feature adds no storage destination or access grant.

### Qualification evidence

`test_durable_finding_lifecycle_restart.py` exercises ordinary application and
adapters across separate interpreters sharing only persisted synthetic services.
Codec, carrier, journal, native-receipt and native-union tests provide complementary
field fidelity, corruption, capacity and exact-readback controls. These are source
qualification fixtures, not live deployment evidence. Final exact-head end-to-end
checks and required remote CI must pass before claiming bounded G10/G14/G16
closure. G15, G02/G08 fencing, original-context recovery and historical incident
settlement remain separately scoped.

### Cold consumers and completion custody

An authenticated reply or human assessment can discharge its exact criterion,
not every hidden obligation. The native resolver's exact confirmed instance set
and retained numeric native receipts release contingent capacity only after the
criterion has positive resolution proof. Merely marking a thread resolved grants
nothing. A later cold review may then promote the next retained obligation.

Inline readers reuse only a root they selected themselves. The application pins
that lifecycle reference through publication preparation; a different reference
at journal construction stops without failure checks or reservation cleanup of
the competing writer. Legacy round completion follows only the journal's own
original-root lineage and still executes the ledger's fresh completion CAS.

A fresh explicit rescan may use complete authenticated lifecycle evidence rather
than mutable prose from an older native projection. Such prose does not replace
retained criteria or decisions, and the rescan does not overwrite the edited
projection. Corrupt selected references, lifecycle parts or required source
reads remain refusals; an unresolved exact-root publication still requires its
own unchanged native binding.

Small cold reply/human paths exercise the original 64-call allowance. Current
metered fixtures at 372 and 384 calls can refuse before native publication when
the complete durable tail does not fit; default 512-call qualification is
separate. Neither successful low-level codecs nor larger test allowances erase
these boundaries. Missing historical complete inventory remains a pre-analysis
handoff, not an implicit empty migration.

### Trust observations are not enrollment authentication

`AuthenticatedTrustSnapshot` is a domain input, not evidence that the ordinary
broker produced its fields. An enrolled boundary with incomplete live evidence
now holds with `trust-observation-incomplete`; missing owner/repository/fork
fields are not copied from enrollment, and actor IDs are never derived from
logins. Complete supplied-observation tests qualify model behavior only.

Ordinary authenticated enrollment, durable trust restoration, permission-history
epochs, installation selection history, actor authorization and funded fresh
observations at every native-effect boundary remain separate contracts. This
candidate neither creates those authorities nor changes access or activation.

### Ordinary-route acceptance map

The following fixtures use the production entry points and substitute persisted
synthetic GitHub/provider services. They complement, rather than replace, exact
candidate CI and deployed-runtime evidence. A test name is an acceptance target;
its presence alone is not a passing receipt.

| Acceptance | Producer, durable carrier and cold consumer | Regression fixtures |
| --- | --- | --- |
| Hidden obligations survive a new process | Ordinary application publication, session lifecycle reference, fresh application restore | `test_durable_finding_lifecycle_restart.test_complete_ordinary_lifecycle_survives_fresh_processes`; `test_finding_lifecycle_current_hosted` |
| Dispatch uncertainty survives process death | Pre-effect intent checkpoint, owned native readback, replay without reposting | `test_durable_finding_lifecycle_restart`; `test_finding_lifecycle_reconciliation` |
| Reply/human resolution releases only its own slot | Authenticated original criterion and native receipt, cold resolver confirmation, later cold review promotion | `test_finding_lifecycle_reply_restart`; `test_finding_lifecycle_consumers` |
| Lost resolution cannot clear a hidden blocker | Actual command/resolver transport, pending journal, cold native reconciliation | `test_finding_lifecycle_resolution_failure` |
| Rename/move/split preserves history and native identity | Ordinary drift plan, retained obligations and receipts, fresh drift consumer | `test_durable_finding_lifecycle_restart.test_rename_move_and_split_keep_native_identity_across_processes`; `test_finding_location_drift_g16` |
| Newer roots are not overwritten by stale preparations | Selected lifecycle reference, original-root completion lineage, fresh ledger CAS | `test_finding_lifecycle_custody` |
| Reused native roots remain exact members | Native group writer, versioned union receipt, bounded independent reader | `test_native_union_reused_roots`; `test_native_finding_receipts` |
| Overflow preserves original funds and all obligations | Complete pending/future envelope preview, existing evidence allocator, authenticated part reader | `test_finding_lifecycle_parts`; `test_finding_lifecycle_store`; `test_finding_lifecycle_current_hosted.test_unchanged_small_original_allowance_refuses_before_native_effects` |
| An ordinary owned body update preserves receipt identity | Native review observation, durable exact PUT precondition, independent post-write readback | `test_finding_lifecycle_review_update` |
| A known batch cannot spend its first effect before discovering it cannot retain the rest | Exact request set, cumulative checkpoint plan, original shared or independently owned funding domains | `test_finding_lifecycle_batch`; `test_thread_resolution_batch_preflight` |
| A new head selects its own authentic native receipt without erasing old evidence | Bounded native discovery, current marker scope and exact retained binding, cold command consumer | `test_native_finding_receipts`; `test_terminal_representability_g34` |
| Accepted action history survives restart without manufacturing semantic progress | Exact terminal result commitment, authenticated reference metadata, fresh ordinary action admission | `test_accepted_semantic_history`; `test_action_result_codec` |

Carrier tests separately exercise missing/corrupt/foreign parts, unsupported
versions and field-by-field lossless decoding. Legacy records with no authentic
complete finding inventory are an explicit migration hold, not a positive
restart case. These fixtures cannot establish external all-writer containment,
original historical cessation, permission epochs, or the provenance of a deployed
installation. The corresponding decision/evidence gates remain visible even when
this bounded ordinary lifecycle passes.

Pending shared-base checkpoints are specified in
[ADR 0138](adr/0138-bounded-shared-base-lifecycle-checkpoints.md). They reconstruct
the complete journal from an authenticated immutable full-snapshot base, never a
chain of partial inventories. Current-root fencing follows dependent reads even
when the outer carrier is inline. Unknown and final checkpoints remain
self-contained; all historical parts and original funding stay retained.

The separate opt-in broker v4 contract carries signed current observations from
existing GitHub/OIDC reads, with matching Python validation. The ordinary request
producer remains v3 until durable enrollment and boundary integration are
qualified. Current observations do not prove permission/selection continuity,
actor repository permission or freshness immediately before every native effect.
Missing facts cannot populate a synthetic `permission_epoch` or silently satisfy
the complete G15 trust contract.
