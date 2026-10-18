# Explicit feedback continuation qualification

Scope: reusable trusted local host at the public selector, assessment and
publication APIs. Actual `LocalSessionLedger`, owned manifests and prepaid tails
are exercised against synthetic GitHub HTTP and a deterministic provider. No
deployed, installed CLI or live model qualification is implied.

| Issue / condition | Executable acceptance | Evidence / unresolved work |
| --- | --- | --- |
| #240/#242: explicitly target a pending finding after the first 32 | `PublicFeedbackContinuationTests.test_explicit_late_target_actual_local_receipt_before_publication` | 40 varied SHA-256 IDs; exact last ID reaches inference and durable accepted receipt before publication; 39 remain pending and approval is withheld. |
| #241/#243: same accepted source continuation cannot renew inference/counters | `test_accepted_interruption_replays_without_inference_but_keeps_publication_budget_blocked`, `test_fresh_owner_without_original_attempt_witness_cannot_activate_replay` | Original live owner reconstructs the receipt with zero provider calls and persists original accounting/time high-water. Full publication then exhausts the original control allowance. A fresh owner without the genuine original attempt witness refuses activation. Neither replay/publication liveness nor process recovery is qualified. |
| #240/#244: early semantic failure cannot starve later admitted work | `test_early_semantic_failure_does_not_starve_late_admitted_target` | First four selected targets fail literal citation validation; the late fifth target in the next batch succeeds, is retained, and does not clear the earlier four. No structural retry or allowance renewal. Automatic CLI queue scheduling remains unwired. |
| #242: unknown, foreign, ambiguous/duplicate or stale targets refuse | `test_unknown_or_foreign_target_has_no_provider_or_checkpoint_mutation`, `test_short_or_duplicate_target_selector_refuses`, `test_stale_snapshot_queue_refuses_before_provider_or_checkpoint_mutation` | No inference, receipt mutation, reply or approval. |
| #244/#269: source/current PR/base/head/latest authority and generation fences | `test_head_base_or_latest_inventory_changes_cannot_accept_receipt`, `test_source_changes_between_dispatch_and_acceptance_preserve_unresolved_union`, `test_changed_owned_carrier_binding_cannot_replay_accepted_receipt`, `test_competing_local_generation_refuses_before_provider_dispatch` | Source edit preserves original dispatched counter and response reservation; altered binding cannot replay; injected competing generation wins and prevents provider dispatch. This does not assert cross-process atomic CAS. |
| #240/#269: complete queue capacity and independent decoding limits | `QueueStateCarrierTests` | Complete 40/100/250 varied-ID queue storage round trips; corrupt, oversized, semantically invalid and mismatched-bound data refuse. High-entropy 250 remains too large for inline storage. |
| #240/#244: current varied 100 public continuation | `test_varied_100_falls_back_to_parts_and_does_not_claim_publication_capacity` | Exact target is accepted and retained through partition storage; publication exhausts the unchanged original allowance before reply or authority changes. Further owned read/reference optimization remains implementable work. |
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

A non-shipping reference-carrier experiment reduced the varied-100 admission
manifest from 8,104 bytes to 3,407 bytes (3,671 after acceptance), below the same
7,168-byte growth allocation. It still used 32 reads before publication. The
unchanged pending-report writer then consumed another 28 calls at the original
60-slot boundary: two provisional App pages existed, the next page was incomplete,
all 100 findings remained pending in authority, and no approval was possible.
Carrier deduplication alone therefore does not qualify this path and its new
reference contract is not shipped. Deterministic follow-on work can coalesce
actual owned-ledger transaction reads or safely reuse immutable report associations
while preserving every current source/PR/head/latest-authority fence and complete
report readback. This is implementable integration work, distinct from external
provider authorization or deployed qualification gates.
