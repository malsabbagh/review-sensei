# Explicit feedback continuation qualification

Scope: reusable trusted local host at the public selector, assessment and
publication APIs. Actual `LocalSessionLedger`, owned manifests and prepaid tails
are exercised against synthetic GitHub HTTP and a deterministic provider. No
deployed, installed CLI or live model qualification is implied. Installed Python
library composition is qualified separately below.

| Issue / condition | Executable acceptance | Evidence / unresolved work |
| --- | --- | --- |
| #240/#242: explicitly target a pending finding after the first 32 | `PublicFeedbackContinuationTests.test_explicit_late_target_actual_local_receipt_before_publication` | 40 varied SHA-256 IDs; exact last ID reaches inference and durable accepted receipt before publication; 39 remain pending and approval is withheld. |
| #241/#243: same accepted source continuation cannot renew inference/counters | `test_accepted_interruption_replays_without_inference_but_keeps_publication_budget_blocked`, `test_fresh_owner_without_original_attempt_witness_cannot_activate_replay` | Original live owner reconstructs the receipt with zero provider calls and persists original accounting/time high-water. Full publication then exhausts the original control allowance. A fresh owner without the genuine original attempt witness refuses activation. Neither replay/publication liveness nor process recovery is qualified. |
| #240/#244: early semantic failure cannot starve later admitted work | `test_early_semantic_failure_does_not_starve_late_admitted_target` | First four selected targets fail literal citation validation; the late fifth target in the next batch succeeds, is retained, and does not clear the earlier four. No structural retry or allowance renewal. Automatic CLI queue scheduling remains unwired. |
| #242: unknown, foreign, ambiguous/duplicate or stale targets refuse | `test_unknown_or_foreign_target_has_no_provider_or_checkpoint_mutation`, `test_short_or_duplicate_target_selector_refuses`, `test_stale_snapshot_queue_refuses_before_provider_or_checkpoint_mutation` | No inference, receipt mutation, reply or approval. |
| #244/#269: source/current PR/base/head/latest authority and generation fences | `test_head_base_or_latest_inventory_changes_cannot_accept_receipt`, `test_source_changes_between_dispatch_and_acceptance_preserve_unresolved_union`, `test_changed_owned_carrier_binding_cannot_replay_accepted_receipt`, `test_competing_local_generation_refuses_before_provider_dispatch` | Source edit preserves original dispatched counter and response reservation; altered binding cannot replay; injected competing generation wins and prevents provider dispatch. This does not assert cross-process atomic CAS. |
| #240/#269: complete queue capacity and independent decoding limits | `QueueStateCarrierTests` | Complete 40/100/250 varied-ID queue storage round trips; corrupt, oversized, semantically invalid and mismatched-bound data refuse. High-entropy 250 remains too large for inline storage. |
| #240/#244: default varied 100 public continuation | `test_varied_100_falls_back_to_parts_and_does_not_claim_publication_capacity` | Exact target is accepted and retained through partition storage; publication exhausts the unchanged original allowance before reply or authority changes. The explicit opt-in profile below closes this bounded fresh path; default behavior remains unchanged. |
| #240/#242/#269: rich 250 publication and full history | Existing `LargePublicLifecycleTests.test_rich_250_inline_authority_refuses_without_claiming_supported_capacity`, partitioned eligibility readers and indexed-history suites | Rich inline eligibility exceeds the 24 KiB growth reserve; v4 is a reader-first prototype with publication disabled. Compatible active writer/readers, source-grant-bound queue activation, retained root/history/tombstone budgets, and delayed replay qualification remain required. |

Measured 40-finding original read counts:

| Phase | Original partition carrier | Packed inline carrier |
| --- | ---: | ---: |
| Complete feedback/evidence preparation | 7 | 7 |
| Original reservation retained | 12 | 12 |
| Admission and current authority fenced; enter activation | 16 | 16 |
| Owned admission-dispatch readback | 34 | 24 |
| Owned accepted result readback | 56 | 32 |
| Public reply and exact pending authority | Refused at 60 before reply | 56 |
| Accepted checkpoint reconstruction | Not qualified | 44, zero inference, mandatory replay mutation |
| Accepted checkpoint reconstruction, then publication | Not qualified | Refused at 60 after one provisional App reply; authority still has all 40 pending |

The allowance stays 64 physical calls with its existing reserved fence slots and
60-second control deadline. Enrollment is fixture setup preceding the assessment;
source preparation, persistence, replay and publication share one original
`EvidenceReadBudget`. A separate observer only verifies retained disk state after
faults and never supplies an execution allowance.

The ordinary GitHub application route still does not accept a selector or connect
the queue callbacks. Hosted activation also needs a consuming exact-source broker
grant for every mutation and authenticated reconciliation for unknown attempts.
These are implementation/qualification prerequisites, not a request for user
permission. No issue is closed from this finite matrix alone.

Admission consumes one authenticated receipt read, retaining a bounded deep copy
for full decode. Every real activation still fences the same original generation;
a competing writer cannot be absorbed by a second read just before dispatch.
Every accepted replay persists its original accounting/time high-water. Skipping
a write because the post-restore tracker matched itself would lose legitimate
higher current charges; five counter variants now require persistence before a
fresh restore. Skipping a write merely because decisions/counters were unchanged
would lose the wall-time high-water across processes. Replays at five seconds,
then a fresh clock regressed to three seconds, refuse. Expiry crossed during
validation and within-operation clock regression also refuse before mutation.
These safeguards preserve original absolute deadline/expiry and max elapsed age.

## Earlier exact source and package evidence

The [candidate receipt](public-feedback-continuation-candidate.json) binds frozen
source `8be565aa8c27d39d54e7ce7863ecb09710fd9995`. All 16 local gates pass,
including 3,766 normal tests, four complete coverage workers and 82.79% coverage
at the unchanged 80% floor. Exact installed wheels pass 248 distribution tests,
11 downstream tests, 18 OpenAI configuration cases and all 1,295 post-package
cases (213 lifecycle methods). All 876 tracked inputs and 119/48 installed
runtime files match frozen source and wheel bytes. The sdist contract and an
additional installed duplicate/unknown recorded-ID regression pass.

Independent final review reran the prior counter/time/expiry regressions and
131 focused tests with no concrete Standards or Spec findings. That receipt/documentation commit changed no tested runtime or harness bytes.
The bounded 100-finding profile below changes runtime and harness bytes and
requires new exact-head source, package, CI and hosted-review evidence.

## Explicit bounded 100-finding profile

ADR [0085](../adr/0085-bounded-complete-selected-feedback-reports.md) adds two
independent trusted-library opt-ins: an authenticated exact-inventory reference
for `pack_queue_state`/`unpack_queue_state`, and the `shared-guidance-v1` pending
report layout. The unchanged 100-finding fixture retains varied SHA-256 IDs and
original Unicode concern prose. The receipt is stored by the actual local ledger,
with original source/authority/attempt binding and every live authority fence.
The report moves only repeated generic guidance to the heading; every unresolved
full/display ID, path and supplied per-finding reason is retained.

| Acceptance / exact test method | Evidence |
| --- | --- |
| `test_referenced_varied_100_complete_publication_inside_original_budget` | 32 original calls after durable acceptance; 57 after complete public delivery and refreshed authority; one 11,467-byte report page (13,867 bytes including response instructions and its marker); 99 pending, no approval; original immutable inventory unchanged. The final complete-report readback adds one call. |
| `test_referenced_fresh_reader_reconstructs_inventory_but_not_attempt_witness` | Required original inventory is read from the actual retained Bot eligibility, not supplied by an unpersisted fixture queue. A new ledger owner without the genuine original attempt witness still refuses activation; no inference or mutation. |
| `test_referenced_original_owner_replay_persists_accounting_without_new_publication_allowance` | Zero inference; mandatory replay mutation persists original counters/time at 44 calls. Later publication exhausts the same original allowance; all 100 remain pending in authority. |
| `test_referenced_fresh_reader_refuses_missing_original_retained_authority` | Missing original inventory refuses before provider or receipt mutation. |
| `test_referenced_shared_report_source_head_base_and_latest_races_between_pages` | A 104-finding stress fixture with one valid resolution and three unresolved decisions with bounded individual reasons naturally produces two pages without changing the page cap. A source, head, base or latest-result race after page one stops page two and prevents authority changes/approval. |
| `test_referenced_delivery_conflict_concurrent_lost_and_unreadable_pages_keep_all_pending` | Same-ID body mutation, delivery concurrent with the negative lookup, uncertain/lost POST, partial POST, individual and final whole readback failures all leave 100 pending and no approval. |
| `PendingDeliveryTests.test_shared_report_rechecks_whole_report_after_later_page_mutation`, `test_shared_report_whole_readback_failure_cannot_claim_complete` | Both failed before the final complete-report scan was added to the opt-in layout. A prior per-page readback cannot stand in for a final complete report. |
| `QueueStateCarrierTests`, `PendingPresentationTests` | Closed v2 frame/reference/index shape, full original envelope hash, exact inventory/snapshot/order, independent bounds, legacy compatibility, every ID/path/reason retained, unknown layout and oversized individual body refusal. |

The reference carrier reduced the measured 100 admission manifest from 8,104 bytes
to 3,407 (3,671 after acceptance), within the unchanged 7,168-byte future-growth
allocation. It still uses the existing outer authenticated manifest and full
root generation/producer/source binding. Encoded, projected and reconstructed
bounds are independently enforced; an unknown/default reader without the exact
authenticated inventory refuses v2. It creates neither storage authority nor an
attempt witness. The legacy layout/default v1 carrier remain unchanged.

No deployed/CLI/Worker writer is activated. Rich 250 eligibility and a combined
100-finding/long-history profile remain unqualified. The original-owner replay
still lacks sufficient publication budget; a fresh owner still needs the genuine
attempt witness. Further deterministic integration can reduce actual owned-ledger
transaction reads under its existing locking/ownership contract. None of these
limits authorizes cap increases, a new allowance, policy changes, omitted data,
provider inference replay or implicit source grants.
