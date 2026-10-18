# Large approval-lifecycle qualification

This source qualification extends issues #240–245 and #269. It uses real engine,
GitHub adapter, operation comment store and result-part implementations with
synthetic HTTP and a deterministic provider. The focused source observations below do not establish published package,
deployed Worker or live provider compatibility. The integration receipt separately
records the sdist-extracted installed-wheel run of these shared cases.

## Acceptance matrix

| Requirement | Source seam and exact test | Evidence / remaining condition |
| --- | --- | --- |
| Accepted bounded partial result replays without another inference, debit, decision or authority write | `GitHubApplication.generate_and_publish_reply` → owned operation/result comments → `HumanAssessmentPublisher.publish`; `LargePublicLifecycleTests.test_enabled_accepted_partial_replay_does_not_reapply_or_refill` | 100 independently SHA-256 varied IDs and 250 compact IDs, each with equally many historical comments; eight calls resolve exactly 32; replay preserves the operation documents, pending count and published bodies. |
| Crash after accepted result, before public receipt | `test_accepted_result_recovers_after_publication_interruption_without_inference` | An injected process-boundary exception leaves 100 pending; accepted bytes resume with zero inference, resolve 32 and publish one authority COMMENT; repeated replay is idempotent. This is an adapter interruption fixture, not an OS-kill/deployed delivery claim. |
| Crash after durable authority COMMENT | `test_replay_after_durable_comment_interruption_keeps_one_authority` | With 250 history comments, a finalization interruption leaves 68 pending; replay retains exactly one authority COMMENT and makes zero provider calls. |
| Changed authority cannot reuse accepted decisions | `test_accepted_replay_rejects_changed_source_evidence_and_authority` | Same-source body, actor, exact patch, result digest and finding-body mutations refuse without inference or publication. Original source/head/base fences still apply. |
| History scans remain within publication bounds | Same enabled replay and interruption cases; `ConversationPublisher` and `GitHubHttp.paginate` | Adaptive 100/50/25/5/1 pages retain exact offsets and existing 512 KiB response, item, 128-request, 32-write and 60-second publication ceilings. Large individual items still refuse. These are history reads, not authorization to select 250 human feedback sources. |
| 100/250 multi-invocation continuation | `test_100_250_findings_and_history_complete_over_bounded_new_mentions` | Composed source test passes with accepted computation transition release from ADR 0076. Pending counts are exactly 68/36/4/0 and 218/186/154/122/90/58/26/0; exact-source replay preserves counts, performs zero inference and adds no receipt or authority record. |
| Rich 250-finding public capacity | `test_rich_250_inline_authority_refuses_without_claiming_supported_capacity`; existing `PartitionedEligibilityReaderTests.test_complete_100_250_result_bound_reader_all_resolution_and_no_approval` | Independently SHA-256 varied 250 IDs exceed the inline 24 KiB future-resolution reserve. Refusal is intentional and lossless. Existing partitioned readers reconstruct rich authority, but v4 finalization is explicitly withheld pending compatible writer/reader activation. Compact IDs are a finite compression profile, not universal 250-item support. |
| Full baseline / complete rich inventory / indexed history primitives | `test_authenticated_partitions`, `test_partition_consumers`, `test_assessment_history_index`, `test_epic238_frozen_queue_faults` | Existing tests cover exact 100/250 reconstruction, fault/retention/counter primitives and source selection. They do not replace a joint public lifecycle qualification with root growth, tombstones, retained receipts, orphans and delayed readers. |
| Evidence acquisition and complete feedback boundaries | `ExhaustiveAcquisitionTests`, `CompleteFeedbackTests`, `PublicFeedbackTests` | Existing tests retain >100 pagination, 999/1000/1001 evidence bounds, byte/read/deadline refusal, 32-source complete selection and source revalidation. Public default assessment source remains separately bounded at 32 KiB. |

## Source measurements

On the composed source, the seven tests in `test_large_public_lifecycle` pass
in 1.508 seconds. The 100-item independently varied identity profile has 24,493
immutable canonical bytes, 12,858 serialized all-resolved inventory bytes and an
8,841-byte initial authority marker. The 250-item compact identity profile has
61,243 immutable bytes, 18,960 serialized all-resolved inventory bytes and a
3,577-byte initial marker. Both use one required path and distinct short Unicode
finding bodies. They do not qualify 100/250 distinct paths or large prose.

Nearby validation passes 120 operation/host/application/conversation tests after
the computation release change. The earlier unchanged primitive matrix passed
126 partition/reader/indexed-history/queue/OS-kill/recovery tests; complete-feedback
admission/composition passed 30 tests. These counts describe source execution,
not hosted or installed support.

## Replay compatibility and recovery

New accepted assessment payloads bind complete immutable inventory, original
result/facts, repository/base/head, source kind/ID/revision/body/actor metadata,
and exact evidence. Normalization permits only accumulated resolved identities
and their corresponding human-pending flag to change. A restored result must
match this context before any publication. Every citation is revalidated against
the complete current evidence group; the core inventory's strict application
rules remain unchanged. Only the adapter recognizes already-resolved IDs in a
verified accepted replay.

Older accepted payloads without this binding refuse with an actionable replay
error and require a new authorized mention. They must never be decoded as a new
allowance or used to authorize a changed result. The legacy operation-disabled
route has no new durable replay guarantee from this change.

No release, v5 movement, Worker deployment, permission or secret change is part
of this qualification. Keep issues #240–245 and #269 open for the remaining
rich-writer activation, joint capacity, compatible rollback and remaining installed/live
conditions; passing compact profiles does not discharge those obligations.
