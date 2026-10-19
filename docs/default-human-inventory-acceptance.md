# Default human inventory acceptance

This is the focused follow-up to the PR316 publication failure in run
39199327349. Its base is main `6ad262d` (PR325 merged). The failed run's original
result bytes were unavailable; its finding count is unknown. Representative
fixtures reproduce the legacy 24 KiB inventory refusal without claiming to
reconstruct that run. Its incomplete coverage remains an approval blocker.

[ADR 0094](adr/0094-default-human-inventory-reference.md) defines the proposed
schema 5 composition. This document is a test map, not release or rollout
approval. Existing schema 4 component work is not activated wholesale.

## Supported matrix

The public application tests use fresh application and local ledger objects,
actual bounded GitHub HTTP parsing/pagination, actual default publication,
operation replay, human assessment and finalization. Only external GitHub and
provider responses are fixtures. They qualify this composition, not live hosted
service timing or real provider correctness.

| Requirement / issue | Implementation | Exact executable evidence | Qualification limit |
| --- | --- | --- | --- |
| Complete admitted metadata survives raw-result retention (#240, #241) | `default_human_inventory.py`, `default_human_reader.py`, application retention | `test_default_human_inventory_store.DefaultHumanInventoryStoreTests.test_77_complete_admitted_findings_survive_fresh_store_and_replay`, `test_250_complete_findings_cross_multiple_transport_parts`; `test_default_human_inventory_publisher.DefaultHumanInventoryPublisherTests.test_small_legacy_compatible_review_also_retains_exact_runtime_admission` | Runtime human/blocking flags are retained independently; raw JSON alone cannot authorize their reconstruction. |
| Large default publication and future all-resolved root fit (#241, #269) | Publisher schema 5 reference; compact repeated summary guidance; complete original inline/body prose | `test_default_human_inventory.DefaultHumanInventoryTests.test_complete_77_rich_human_findings_publish_and_fresh_default_reader_keeps_all_pending`; `test_default_human_inventory_publisher.DefaultHumanInventoryPublisherTests.test_full_body_overflow_refuses_before_inventory_check_or_review_writes` | Full IDs, complete metadata and original prose remain. Arbitrary body-only 250-finding reviews are not qualified. Existing individual/root bounds still apply. |
| Durable continuation through partial and terminal decisions (#242, #243) | Default application, operation entry, human assessment, pending report and finalizer | `test_default_human_inventory.DefaultHumanInventoryTests.test_77_100_250_original_rich_inventories_complete_across_fresh_default_applications` | POSIX local-ledger matrix: 77 rich / 100 rich / 250 representative findings over at most 32 paths; at most three fresh delivery invocations per unchanged source revision, zero additional inference on recovery, at most eight provider calls per source. Every HTTP dispatch is charged once to its original allowance. |
| Real blockers and incomplete evidence stay blocked (#238, #244) | Standard approval facts and check outcome mapping | `test_default_human_inventory.DefaultHumanInventoryTests.test_resolving_every_rich_human_finding_preserves_other_real_approval_blockers`; `test_default_human_consumers.DefaultHumanConsumerTests` | Human decisions do not waive required fixes, partial coverage or unverified qualification. Both canonical `qualified` and legitimate `not-required` states remain eligible when all other obligations pass. |
| No stale/missing/foreign authority or prose (#243, #245) | Independently selected durable transaction, numeric Bot, exact PR/base/head/index and canonical visible render | `test_default_human_inventory.DefaultHumanInventoryTests.test_changed_missing_or_foreign_authority_and_prose_refuse_before_inference_or_mutation`; `test_default_human_reader_binding.DefaultHumanReaderBindingTests` (full/metadata latest-result mismatch and zero-inference public refusal); `test_pending_receipt_metadata_fence.PendingReceiptMetadataFenceTests.test_prose_changed_during_receipts_cannot_publish_authority_or_approval` | Corrupt newest authority never reveals an older clean root. Receipt metadata fences cannot authorize a decision. |
| Crash and uncertain write recovery (#245) | Independent admission intent; complete part readback; original canonical result retention | `test_default_human_inventory_publisher.DefaultHumanInventoryPublisherTests.test_actual_staging_crash_retains_intent_and_retry_never_starts_again`, `test_lost_actual_root_post_reconciles_without_another_root_or_inference`; store lost-intent/lost-part/crash tests | An incomplete or unknown admission set holds rather than blindly restarting writes. A crash before admission requires a new full review. |
| Competing writers (#245) | Duplicate intent and strict part ownership rejection | `test_default_human_inventory_store.DefaultHumanInventoryStoreTests.test_interleaved_authorized_writers_leave_duplicate_intent_held` | Actual interleaving at the transport seam is exercised. Ordinary GitHub comments do not provide distributed atomic compare-and-swap. |
| Idempotent delivery and receipt replay (#245, #267) | Multipage two-scan staging, exact owned body/ID proof, source/head/root fences | All nine `test_v5_pending_staging.V5PendingStagingTests` cases and the inherited `test_legacy_pending_staging.LegacyPendingStagingTests` controls | Lost acknowledgments, partial delivery, duplicate/edit/foreign parts and source/head/root races are covered. Provider execution is not repeated on accepted operation replay. |
| Pagination, read budgets and oversized items (#267, #269) | Shared publication budget, operation-local successful page-size hint, adaptive complete scans | `test_github_operation_comments.PortTests.test_real_oversized_pages_reduce_size_without_losing_items_or_refilling_budget`, `test_later_oversized_page_preserves_exact_offset_at_smaller_size`, `test_oversized_individual_item_refuses_at_one_item_without_partial_inventory`; store deadline/capacity tests | Hints cache page size only; every scan reads fresh values through complete EOF. Oversized individual items and exhausted original deadlines fail closed. No allowance is increased. |
| Local immutable-part read cost (#240, #269) | Bounded POSIX immutable-byte cache with fresh containment/device/inode/size/mtime/ctime witness | `test_local_partition_read_cache` immutable-part cache tests; lifecycle matrix above | Mutable roots remain fresh. Windows uses full hashing; rich Windows capacity is explicitly held rather than positively qualified. |
| Reusable and installed CLI (#246) | Actual CLI, distribution-safe test and post-package lifecycle contract | Three `test_default_human_inventory_cli.DefaultHumanInventoryCliTests` cases; `dist_safe/test_default_human_inventory_contract.py`; `published_cli/lifecycle.py` | CLI test covers raw proposal admission, fresh recovery without result file/provider construction, partial human reply and changed trusted key refusal. Installed runs must prove wheel origin and exact bytes. |

## Related coverage and remaining conditions

Large legacy reports also consolidate common guidance and use bounded two-scan
staging; their wire eligibility and complete original explanations remain intact.
Human inventories larger than one provider request omit the optional processing
reaction to reserve the original allowance for durable delivery and reconciliation.

The broader epic remains open. Existing baseline invalidation (#313) and provider
remaining-deadline (#314) behavior is exercised by `test_baseline_recovery.py`,
`test_baseline_recovery_cli.py`, `test_provider_deadlines.py` and their installed
contracts. This follow-up does not rewrite or independently qualify every
configuration in #115–123/#146. The existing OpenAI-compatible configuration
and provider ingress suites remain required regressions. OpenRouter live
qualification still requires its three authorized provider proofs; fixtures
cannot replace them.

Release qualification (#246), real hosted broker elapsed-time behavior (#267),
and deployed reader-first compatibility (#269) still require their separate
release-owner evidence. This task does not merge, release, move v5, deploy
workers or change credentials/permissions. DecisionProvider #161 and architecture
governance #270 remain outside scope.

Full checkout validation must run the repository's required gates, including
normal discovery, disjoint coverage workers and the 80% threshold. Installed
validation must use a clean frozen source archive, engine and adapter wheels,
extracted-sdist contracts and all post-package CLI lanes. Passing component tests
alone is not issue-closure evidence.
