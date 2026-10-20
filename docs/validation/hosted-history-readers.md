# Hosted history reader acceptance

Scope: the ordinary hosted client startup, cleanup, operation admission/replay,
review publication, thread reconciliation and presealed activation fence. This
qualifies finite supported cases, not every possible GitHub history or provider.

All test names below are in `tests/test_hosted_history_readers.py`, class
`HostedHistoryReaderTests`, unless another module is named. The same class is
included in `tests/dist_safe/test_hosted_history_readers_contract.py` and
`tests/published_cli/lifecycle.py`. Installed lanes assert both package origins
and prohibit live sockets. Synthetic provider/broker boundaries do not establish
live provider or deployed-worker qualification.

| Requirement / issue | Code | Exact acceptance / evidence | Limit / recovery |
| --- | --- | --- | --- |
| Large history startup, #238/#240/#246 | `http.GitHubHttp.paginate`, hosted ledger resolver | `test_startup_resolves_real_ledger_after_broker_accepts_large_history` | Broker acceptance followed by successful real ledger load; no writes |
| Cleanup, #246/#269 | `release_reservation.release_own_reservation` | `test_cleanup_discovers_no_reservation_without_mutation` | No reservation is reported only after complete current ledger discovery |
| Lossless page retry, #240/#267 | default REST reader | `test_default_list_reader_returns_every_full_captured_shape_object` | All 27 full synthetic objects equal input; captured size 620,839 bytes; ten physical reads maximum |
| Exact accepted operation and complete findings, #238/#246/#267 | `operation_entry._issue_comments`, `operation_comments`, publication | `test_actual_cli_keeps_77_findings_and_accepted_operation_with_both_large_histories` | Actual hosted CLI, accepted operation, 77 full indexed findings, captured history distributions plus oversized accumulated review history; provider reconstruction forbidden |
| Durable checkpoint, bounded retry and idempotent replay, #246/#269 | remote ledger, operation host, retained publication | `test_large_history_crosses_startup_operation_checkpoint_publication_and_replay` | Captured history plus 77-finding remote checkpoint; 100/250 historical comments; one explicit retained publication retry after 250-case shared-budget exhaustion; one provider execution and one approval |
| Large individual comments, #240 | operation port | `test_250_large_history_operation_replay_keeps_single_execution`; `tests.test_github_operation_comments.PortTests.test_real_oversized_pages_reduce_size_without_losing_items_or_refilling_budget` | 250 x 8 KiB operation history; complete bodies and metadata preserved; no budget refill |
| Large review-thread roots, #240/#267 | `BoundedGraphQLPages`, publication scans, `FindingThreadResolver` | `test_thread_reconciliation_discovers_large_roots_before_any_mutation` | Same opaque cursor on size fallback; discovery before mutation; `tests/dist_safe/test_hosted_graphql_qualification.py` independently covers all four consumers, 100/250 owned roots, nonempty cursor, accepted mutation/readback/replay and changed-root refusal |
| Combined human verification and accepted replay, #246/#269 | shared REST history policy, conversation, finalizer, HTTP operation port | `tests.test_hosted_history_independent_acceptance.IndependentAcceptanceTests.test_real_http_operation_port_completes_and_replays_both_large_histories`; remaining tests in that class | Captured 27-comment history plus 538,767-byte review inventory completes in the original budget; fresh accepted replay makes no provider or operation writes; repeated status reply deduplicates; source/head/base/result races and stale sealed heads stay blocked |
| Sealed activation, #269 | ledger `_fetch_pages`, `_fence_path`, `_fence_scan` | `test_sealed_tail_binds_smaller_fence_pages_and_original_accounting`; all `tests.test_activation_tail` | Safe page size chosen before immutable plan; exact original accounting and consuming grants; <=64 shared calls |
| New growth after seal, #269 | same fence | `test_sealed_tail_growth_refuses_without_unplanned_smaller_reads_or_activation` | No root PATCH or unplanned size fallback; original ticket released and failure retained |
| Oversized individual / read-budget exhaustion, #240 | REST and GraphQL readers | `test_default_reader_refuses_oversized_individual_and_exhausted_walk`; `test_thread_reader_refuses_oversized_items_budget_and_invalid_pages_before_mutation` | No partial inventory or thread mutation; actionable refusal rather than approval |
| Invalid pages, denial and interruption, #240/#246 | same readers | `test_default_reader_does_not_return_partial_inventory_on_late_failure`; GraphQL negative test above | No retry on authorization, malformed response or cancellation; cursor loops and excessive nodes refuse |

## Retained boundaries

The fixture `tests/fixtures/regressions/pr316-history-shape.json` records sizes and
the public capture digest, never original comment bodies. The review history
shape is 12 entries / 476,638 bytes. The shared REST history sequence includes ten-item pages so repeated scans of
near-limit comment/review bodies fit the original composed budget. The previous
sequence needed nine reads for the captured issue history; this sequence needs
five. GraphQL retains its independent cursor page sizes.

Synthetic exact byte counts exercise the real
bounded JSON transport rather than a mocked overflow exception.

REST lists remain at most 1,000 items, ten physical reads by default and 512 KiB
per response. A list needing more smaller pages refuses. Individual records over
the response cap refuse at size one. GraphQL retries share ten physical reads and
retain caller cursor/ownership checks. Partial discovery never authorizes an
approval. Existing 64-read/60-second composed budgets remain unchanged; an exact
retained publication may require a fresh explicit publication-only invocation.
Unknown writes stay held until owned reconciliation, and this repair does not
resolve the separate distributed compare-and-swap architecture limitation.

Run/job count-envelope readers and the separate opt-in human-file library retain
their existing bounded refusal behavior. They are not deployed qualification
claims of this repair. Provider deadline (#314), invalidated baseline (#313),
provider configuration (#115–123/#146) and live provenance continue to use their
own acceptance suites and rollout gates. No decision on a stale head is promoted
by changing pagination.

## Rollout evidence

The failed installed 0.3.4 PR #316 run was
[39371424150](https://github.com/malsabbagh/review-sensei/actions/runs/39371424150):
broker admission passed, then startup and cleanup reported session ledger
discovery failure before provider dispatch. Source validation and wheel tests
must pass before this repair is released. Actual published bytes, signed v5,
current-head GitHub CI/review/approval and any live refusal must be recorded
separately; this document does not predeclare their success.
