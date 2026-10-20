# Remaining approval lifecycle acceptance

Scope was inspected on 2026-10-18 against main
`7a641b8b6402292f213159853400aff4f2e9a3d9`. PR309 and PR312 were merged and
there were no open PRs at that inspection. The signed annotated `v5` tag object
`bbf523c75618f0a996efa24719c38f6c7ed43943` resolves to release source
`1e67fc7c26d951b9a7b48d799db6bc2e3725c8bc`. Published engine 0.8.1 and hosted
0.3.1 identify that earlier source. Candidate wheels using those version labels
are unpublished different bytes; their digests and tested source must be read
from the candidate receipt, never inferred from the version string.

This matrix distinguishes a bounded supported path, a safe refusal, component
proof and unresolved deployment qualification. Passing a refusal test does not
qualify the refused workload. No issue is closed solely from this document.
DecisionProvider #161 and the mandatory-guidance policy decision #270 are
outside this change. No release, tag movement, Worker deployment or
permissions/secrets change is performed.

## Issue and evidence map

Methods inherited by the checkout and installed wrappers have one implementation
in their named shared helper. Existing component modules are retained rather
than replaced with duplicate assertions. Full gate receipts establish execution
of those modules; focused notes describe the additional experiments.

| Issue | Current boundary and exact acceptance evidence | Remaining condition |
| --- | --- | --- |
| #238 | This matrix; `test_epic238_packing_lifecycle`, `test_large_public_lifecycle`, installed lifecycle inventory below | Parent epic remains open until every claimed public workload and deployment row is established |
| #240 | `test_authenticated_partitions`, `test_partition_consumers`, `test_epic238_frozen_inventory_faults`; packaged `test_partition_contract` | Authenticated partition primitives do not activate richer public baseline writers; realistic 250-finding publication remains finite and may refuse |
| #241 | `test_epic238_inventory_publication` methods `test_varied_100_250_distinct_paths_and_partial_all_resolution_growth` and `test_varied_parts_retain_each_full_explanation_and_unique_target` | Full prose, inventory and baseline must fit together in the activated public graph; independent part success is insufficient |
| #242 | `test_assessment_queue`, `test_assessment_queue_storage`, `test_epic238_frozen_queue_faults`; public composition `test_large_public_lifecycle` | Durable targeted/fair queue is not fully wired into every public reply route; bounded pending output must not imply complete assessment |
| #243 | `test_complete_feedback_assessment`, `test_feedback_integration`, `test_feedback_attestation`, `test_reassessment_feedback`; explicit production broker IPC suite | Existing ordinary conversation is a bounded discussion window; it is not the complete authenticated feedback archive. Live source and grant authenticity remain external gates |
| #244 | `test_complete_evidence`, `test_exhaustive_evidence`, `test_local_evidence_accounting`, `test_epic238_packing_lifecycle.test_missing_or_oversized_complete_group_never_dispatches_or_approves` | Governing guidance/evidence that cannot fit is honestly pending/refused. No automatic clipping, guessed fragments or larger unqualified envelope |
| #245 | `test_epic238_process_faults` (real child kills), `test_epic238_process_interleaving.test_disjoint_replies_reject_stale_writer_then_preserve_both_resolutions`, baseline subprocess concurrency/cancellation | Synthetic host/process evidence does not establish production compare-and-publish atomicity or every unknown remote effect |
| #246 | Exact candidate engine/adapter wheel + sdist qualification, guarded installed lifecycle and downstream suites | All final native/npm artifact bytes, all five native targets, deployed reader inventory/drain, authorized canary and compatible rollback remain separate gates |
| #267 | `test_p1_public_profile`, `test_operation_entry`, `test_operation_publication_guard`; `python -W error::ResourceWarning -m tests.epic238_feedback_broker_qualification -v` | Real broker IPC uses signed synthetic OIDC and fake external transports. Its physical internal hops and caller accounting are distinct; complete original 64-dispatch/60-second deployed usefulness is not established |
| #269 | `test_assessment_history`, `test_assessment_history_index`, `test_history_association`, `test_evidence_capacity` | Joint current/history/root/physical retention and delayed deployed readers must pass together before rich writer activation |
| #313 | `baseline_cli_contract.BaselineCLIContract`: fresh processes for changed base/model, exact retry, omitted concern, failed/partial/oversized refresh, unknown trust, pause, concurrent owner and cancellation; [focused evidence](baseline-recovery.md) | Installed synthetic CLI proof is bounded; live hosted baseline invalidation is a separate rollout observation |
| #314 | Shared `provider_deadline_contract`, `test_run_outcomes`, `test_review_work`: partially spent/expired budget, durable callback, retry/correction/chunks, shorter transport timeout and cancellation | Transport timeouts bound cooperative adapters. Arbitrary uncooperative third-party Python code is not preempted; external provider latency is not inferred from loopback |
| #115 | `test_github_qualification_boundary`, `test_github_trusted_qualification`: independent checked retained bytes, effective identities, positive publisher/application/recovery/replay, exact-head delayed finalizer, mandatory App-owned readback and legacy/forged-provider rejection; installed-safe packaged-corpus control | Reusable host authority is explicit, not inferred by CLI/default deployment. Real #122 qualification and #123 installed/deployed operational evidence remain external; complete unbound legacy records require a new-head bound review, while authenticated incomplete rescans keep their existing conditions |
| #116 | `test_workflow`, `test_github_setup`, `test_comment_command_contract`, `test_github_trigger`, Worker parity tests | Retained historical pairs, generated current caller and actual selected deployed runner/Worker must retain independent identities; tag text does not attest compatibility |
| #117 | `openrouter_cli_contract.OpenRouterCLIContract`: actual CLI/resolver/adapter, strict Morph request, selected credential, missing key, incompatible backend, malformed output and sentinel redaction; existing installed `openai_configuration.py`, six published suites | This is not the full automatic/manual/learning/conversation/inline operation × control × provider cross-product. Source tests do not substitute for the missing installed/deployed rows |
| #118 | Separate provider-contract candidate aligns selectable DeepSeek/Morph with target eligibility | Target eligibility is not passing qualification or a pinned endpoint/region; live target gate #122 remains |
| #119 | Separate provider-contract candidate tests requested/observed model, upstream and fingerprint independently | Mutable alias and absent observed identity remain explicit unknowns; no enrichment request |
| #120 | Separate provider-contract candidate tests bounded provider-reported counters and evaluation aggregation | Missing/partial usage is unknown, local call accounting is distinct and no price/cost is invented |
| #121 | Separate provider-contract candidate tests bounded normalized provider/variant/region/tier routing and strict captured policy | Endpoint prefix syntax does not itself establish an available provider endpoint or capability; no fallback or privacy relaxation |
| #122 | Checked report-byte loader and duplicate/identity/fixture negatives; `test_openrouter_routing_evidence`, `test_live_evaluation`, and installed real packaged-corpus CLI tests bind route identity and separate exact fixture controls from actual quality under unchanged thresholds | Three independent authorized live invocations, matching retained reports and human adjudication remain absent. Three synthetic invocations prove the harness/gates, not real model qualification. Approved Ollama review is a different scope |
| #123 | Existing setup/plan/application control tests, candidate installed lanes, existing deployed release milestone credited only to its source | Qualified OpenRouter App→broker→Actions→installed-engine delivery, all controls and disposable-installation upgrade/rollback remain pending; no deployment performed |
| #146 | Existing `test_review_transaction`, `test_session_ledger`, baseline subprocess journeys, exact candidate package origins and published qualification receipts | Full released F1–F6/control/failure sequence, managed upgrade/state preservation and compatible in-flight rollback require their named artifacts and authorized installation |

## Installed and post-package inventory

`tests/published_cli/lifecycle.py` loads these exact guarded files, validates the
entire inventory before executing any file, and records skipped, empty and
load-error cases as qualification failures:

- `tests/dist_safe/test_approval_lifecycle_contract.py`
- `tests/dist_safe/test_local_review_contract.py`
- `tests/dist_safe/test_baseline_recovery_contract.py`
- `tests/dist_safe/test_provider_deadline_contract.py`
- `tests/dist_safe/test_openrouter_cli_contract.py`
- `tests/dist_safe/test_large_public_lifecycle_contract.py`
- `tests/dist_safe/test_openrouter_metadata_contract.py` (provider stack)
- `tests/dist_safe/test_conversation_deadline_contract.py` (shared provider stack)
- `tests/dist_safe/test_provider_common_flow_contract.py` (shared provider stack)

The lifecycle-only receipt below uses the original six declared files. The
provider stack adds metadata/configuration, shared conversation deadlines and
common-flow contracts, retaining the nine-file union. Its exact combined receipt
is recorded separately. [The provider matrix](provider-flows.md) distinguishes
the existing common interface and all six deterministic configuration factories
from installed and live/deployed qualification.

The sdist distribution contract declares every shared helper and preserves
checkout-only exclusions. Package CI uses only the two built wheels and the
sdist-extracted tests from outside the checkout. Published qualification still
selects all six suites by default; the immutable major-validator explicitly
selects its original five and does not claim newer lifecycle coverage.

No live calls are added to ordinary CI. Existing complete checkout discovery,
four-worker coverage combination, the 80% floor, disjoint inventories,
all-worker completion, pinned Actions and public dependency caches remain.
Focused subprocess and loopback cases stay in normal checkout/package lanes;
production broker IPC remains its explicit pinned-Node tier. No gate or test is
removed to reduce runtime.

## Supported interpretation and handoff

A complete current-head analysis with all current obligations satisfied may
converge according to its independent review policy. Partial analysis, missing
evidence, stale identity, unknown remote writes, unverified OpenRouter
qualification and real unresolved obligations remain non-passing. A completed
workflow, accepted explanation or green source suite does not replace those
facts. Baseline recovery retains concerns, accepted replies retain identity,
and exact retries must reuse durable evidence rather than buy another model
allowance.

The candidate receipt and PR validation section identify final code, commands,
counts, wheel digests, origins and remaining platform/deployment gates. Large
raw logs and generated review bodies stay in local/Actions artifacts. This
document does not promise universal approval or exhaustive correctness.

## Final local receipt

[Candidate receipt](remaining-lifecycle-candidate.json) identifies code/harness
`509a1c93a6b4a9f7398bc2f40aefa2415ca8b290`, all 848 frozen tracked inputs,
exact unpublished wheel/sdist hashes, installed origins and commands/results.
All 3,577 checkout tests pass normally and under branch coverage (**82.52%**,
unchanged 80% floor). Extracted package lanes pass 101 dist-safe and 11 downstream
tests; installed canonical OpenAI configuration passes 18. All six post-package
suites pass 1,148 rows, including 66 lifecycle methods. These are macOS arm64 /
Python 3.14.6 observations, not final native/platform or deployed qualification.

[Exact PyPI 0.8.1/hosted 0.3.1](published-0.8.1-expanded-lifecycle.json) passes
its original five suites (1,082 rows) and fails 35 of 82 expanded lifecycle rows
(including failing subcases; 66 parent methods executed). It retains 47 passing
lifecycle rows. These are ordinary failures, not skips or expected failures. The
old published artifact bytes do not contain the candidate's repairs. Historical
1,109-row receipts used the earlier 27-method lifecycle inventory and remain
valid observations of that different harness. No published version was replaced.

Supplemental approved DeepSeek CLI review used public diffs only. Its valid
transient-dispatch accounting finding was repaired and proved at the call
ceiling. Inspection showed that the earlier deadline gate precedes every reservation,
nonaccepting qualification labels already fail closed, and deadline priority
and sanitized transport errors follow the intended contract. Those suggestions
did not establish additional defects. This review is supporting evidence, not maintainer approval.
