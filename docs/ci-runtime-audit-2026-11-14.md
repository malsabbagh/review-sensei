# ReviewSensei test and CI runtime audit, November 14, 2026

## November 15 Worker process scheduling

The Worker lane now runs its original Python qualification entry points in at
most two child processes on the same runner. The body-limit and published-tail
wrappers are separate invocations; each owns an independent temporary fixture,
Node IPC broker and synthetic transport. Neither wrapper has module/class setup
hooks. The bridge transpiles production TypeScript in memory, uses fixture-owned
trace files, and binds redirect probes to dynamically assigned loopback ports.
No shared mutable fixture, additional runner, test retry, or new funding is used.

All eleven original qualification modules remain selected exactly once, including
the feedback module's custom main that writes metrics and the media modules'
required environment flag. Each failure is retained while remaining selections
continue; missing process execution fails the controller. Native Worker tests,
typechecking, binding generation and dry-run bundling retain their original steps.
The job retains per-process outcomes and elapsed times alongside feedback metrics.

Four synthetic controller tests pass: exact entry-point/environment inventory,
bounded overlap with a failing child and no skipped work, launch-failure
accounting, and child cessation on controller interruption. No actual Worker
journey was rerun locally for this scheduling change.
The original Worker correctness failures remain open, and only current-head
remote execution can qualify this scheduling change or its end-to-end duration.

## Result and limits

**The under-30-minute goal is not achieved or verified.** This change makes a
bounded improvement: refreshed complete historical scheduling hints,
a Linux-specific seed instead of macOS weights, reviewed installed-suite
sharding with fixture protection, and removal of two proven, negligible-cost
duplicate assertions. It does not remove a platform, a recovery
scenario, a process boundary, an accounting assertion, or any safety limit.

### November 15 publication update

The scoped draft is stacked on PR #462 at
`50dbc4a234ac23eee9bf901a92d300f7e1298e6a`, not on the unpublished repair work.
It adds a socket guard at the imported quarantine modules' actual unittest
fixture boundary, seven independently reviewed checkpoint methods (all 28
cases), and five intact current-run classes (all 87 methods). Exact source,
fixture and worker-inventory checks preserve original-root fallback when a
split is ineligible; protection drift fails rather than executing unprotected.
No original journey body, recovery/accounting assertion or platform lane is
removed. Timing hints never determine test inclusion.

The final publication selection passed 68 isolated synthetic controls plus the
exact distribution-command oracle: 69 checks on Linux Python 3.11 (29.156 s)
and 3.12 (36.897 s). They did not import product or held fixtures. The oracle
retains all six existing reviewed hook arguments and the new advisory
installed-cost path.
These are focused results, not native installed-suite qualification or a
measured end-to-end improvement. The draft's normal remote checks are required;
the old held local selections are not rerun to publish it.

The matrix and discovery counts below describe the **initial scheduling slice**,
before the later synthetic runner controls. Full discovery of the combined
candidate has not been repeated under the existing holds. The archived installed
inventory maps from 68 units to 78 with the two reviewed splits while retaining
all 1,139 archived IDs; this is not fresh discovery of the advancing repair.
The archived aggregate work implies a 93.45-minute ideal four-worker floor, so
sharding alone cannot deliver the under-30-minute objective.

Production source-validation optimization and all inactive shared-history,
Git-seed and digest-preparation prototypes are excluded from this draft.
Later repair changes to the action-reply fixture and its helper closure need
explicit source review before refreshing class-sharding pins; a mismatch keeps
the unsplit root. Neither this draft nor its timings qualify those later changes.

### November 15 base reconciliation

The CI merge candidate `83533fb7` combines the published scheduling branch with
repair head `5b42780f`. Discovery against its built wheels and extracted sdist
failed because the packaged published-tail journey imports
`original_finish_interrupt.py`, which was absent from the sdist. The packaging
repair adds that existing helper to both `MANIFEST.in` and the distribution
contract; it does not remove any journey or change the failure injection.

The updated source pins were reviewed against the original `50dbc4a2` inputs:

- `test_review_run.py` adds reservation-state assertions and corrects the expected
  refusal diagnostic inside the existing process-death method. It adds no shared
  fixture state, setup/teardown hook, method or class.
- `test_action_reply_entry.py` replaces two production-method patches with
  invocation-local profile observers. Both the observer and the already-pinned
  `forbid_replacement_phase_budget` helper restore the previous profiler in
  `finally`; the observer chains it. Reply-source selection remains instance
  local. The eight methods, temporary fixture roots and wrapper socket/environment
  guards are unchanged. The helper closure is already included in the source pins.

These changes preserve the seven-method and five-class isolation contracts.
Only their reviewed source hashes are refreshed. Any further source drift still
revokes splitting, and guard drift still fails closed. This source review and
discovery-only package check do not execute or clear the historical held tests,
qualify the complete installed suite, or demonstrate a sub-30-minute run.

Discovery and planning against the rebuilt package also exposed Python 3.14
constant `slice` objects in fixture bytecode. The fingerprint now serializes the
exact start, stop and step, recursively rejecting unsupported or cyclic values.
Synthetic controls check equal/different compiled slice fingerprints and refusal
without invoking hostile component callbacks. With that correction, isolated
Python 3.14 discovery and planning retains all 1,139 installed identities across
78 units (68 before splitting), with both guarded contracts accepted and zero
test bodies executed. This is planning evidence, not execution timing.

The initial audit baseline is `c021eeb205d0ca3c5293d572ef2c2dc45c4b087b`.
Discovery-only inspection found **6,969 test IDs in 833 classes**, and the initial
audit working tree contained 6,975 IDs after removing two duplicates and adding
eight timing-import regressions.

For the initial scheduling slice, only the audit change was stacked onto PR #462 head
`50dbc4a234ac23eee9bf901a92d300f7e1298e6a`. This intentionally excludes the three
unpublished source-hardening commits between that head and c021, including their
ten new source-registry tests. It is not deletion of those tests by this PR.
That base has **6,959 IDs in 833 classes**; the initial scheduling slice has
**6,965 IDs in 834 classes**. Every other publication-base identity is retained.
The candidate inventory SHA-256 is
`32863b68c94e7032b6ad2943a5bb522144b657d6ad0656c8c84171d5962e00f3`.
This is discovery evidence, not a full-candidate pass. The matrix below describes
that initial slice, not the later runner additions or unpublished repair branch.

The complete per-class matrix for that initial slice is
[`ci-runtime-class-matrix-2026-11-14.csv`](ci-runtime-class-matrix-2026-11-14.csv).
It lists every class discovered in that slice, source file, all method/scenario names,
declared contract, cross-platform lanes, installed-helper status, available
historical timings, and proposed action. Blank timing cells mean unmeasured,
not zero. Declaration descriptions are orientation; actual assertions remain
in the referenced source. Installed-helper status does not imply that every
method is selected by every installed wrapper.

## Historical evidence

| Receipt | Inventory | Measured elapsed | Result |
| --- | ---: | ---: | --- |
| [macOS Python 3.13, run 43602325609/job 132469545939](https://github.com/malsabbagh/review-sensei/actions/runs/43602325609/job/132469545939), artifact 14121768243 | 6,813 | 9,561.497 s / 159.36 min | All IDs accounted; all four workers exited 1 |
| [Linux Python 3.14, run 43602325609/job 132469545764](https://github.com/malsabbagh/review-sensei/actions/runs/43602325609/job/132469545764), artifact 14122445830 | 6,813 | 8,988.009 s / 149.80 min | All IDs accounted; all four workers exited 1 |
| [Linux Python 3.11 coverage, run 43468107061](https://github.com/malsabbagh/review-sensei/actions/runs/43468107061), artifact 14068628792 | 6,352 | 8,632.589 s / 143.88 min | All IDs accounted; coverage combined; all four workers exited 1 |
| [Windows Python 3.12, run 43468107061](https://github.com/malsabbagh/review-sensei/actions/runs/43468107061), artifact 14062660968 | 6,352 | 2,444.625 s / 40.74 min | All IDs accounted; all four workers exited 1 |
| [Installed suite, run 43626273898/job 132542807638](https://github.com/malsabbagh/review-sensei/actions/runs/43626273898/job/132542807638), artifact 14131367317 | 1,139 | 8,207.605 s / 136.79 min | All 1,139 started; each worker execution-complete; all four exited 1 |

A checkout receipt's `complete: true` records inventory/coverage completeness;
it is **not** a passing-test declaration. These failed historical paths can cost
less than their eventual passing paths. Neither a combined coverage file nor
`coverage_ok: true` proves the required 80% report floor was reached. Current
branch coverage is unmeasured by this audit.

The macOS job used Python 3.13.17 arm64 and checked out merge
`08d2f787d2f00e709a08506348731e903747a563`, merging head `7003da31e81999cc641a5a9bfb1813719a9fc5e4`
into base `71272181fd3dde301ad304c6ba88d150b5238a0a`. Bare-head discovery has
6,810 IDs; three `WriterEpochContentionTests` identities from the archived job
account for the difference. The reconstructed 6,813-ID inventory matches the
receipt's SHA-256 exactly:
`289266f1465064086bde3354d41ca04bc9bdffd80662bbd1ec9f4ef6e3e39928`.
This distinction prevents treating a different checkout as the measured source.
The older 6,352-ID discovery likewise reproduces its recorded inventory hash.

Provenance for the refreshed seeds, including artifact IDs, source heads,
receipt hashes and failing exits, is retained in
[`checkout-timing-sources.json`](../tests/fixtures/checkout-timing-sources.json).
The three merge-only writer-epoch tests are not removed by this change. Their
cost hint is absent from the branch-only inventory, so future merged discovery
still runs them using normal fallback scheduling.

### Where the time goes

The macOS receipt's workers executed 1,724 / 1,689 / 1,555 / 1,729 methods,
respectively, in 7,428.111 / 8,339.631 / 9,558.290 / 7,677.649 seconds. The
controller executed 116 methods in 3.101 seconds. The imbalance is real, but
measured worker-class time totals **32,972.183 seconds**. Even perfect scheduling
on four equal workers needs approximately **137.38 minutes**, before startup
and other CI gates.

| Historical class | macOS seconds | Contract that must remain |
| --- | ---: | --- |
| `test_quarantine_checkpoint.QuarantineCheckpointTests` | 1,157.773 | Changed/foreign/wrong-owner roots at both fences; stored, absent and failed confirmations; no extra writes or cleared uncertainty |
| `test_materialized_checkpoint_coalescing.MaterializedCheckpointCoalescingTests` | 1,102.303 | Fresh census adjacency, changed receipt ownership/deadline/block, single PATCH and original confirmation liability |
| `test_hosted_cold_dictionary_mutations.HostedColdDictionaryMutationTests` | 1,080.227 | Deleted, edited and foreign-producer mutations for both dictionary and outer lifecycle; real fresh-process reader; no repost |
| `test_action_run.CurrentCommandEntryTests` | 995.731 | Ordinary authenticated command entry and native-effect boundaries |
| `test_verified_fixed_command_prefix.VerifiedFixedCommandPrefixTests` | 949.605 | Complete command-prefix funding, including contingent fixed-target outcomes |
| `test_review_run.ConsecutiveReviewRunTests` | 947.955 | Multiple actual review entries, original retained state and accounting |
| `test_quarantine_command_preflight.ColdCommandPreflightTests` | 930.409 | Cold complete command admission before effects |
| `test_quarantine_checkpoint.QuarantineRolloverTests` | 921.097 | Startup rollover fence and lost-response matrix, original predecessor held |
| `test_override_metadata_integration.HostedOverrideMetadataTests` | 735.091 | Hosted metadata and disposition integration |
| `test_quarantine_checkpoint.QuarantineCarrierPlanningTests` | 693.868 | Funded/unfunded staging; human/wrong-bot/association variants |

The installed receipt presents a separate bottleneck: the indivisible scheduling
unit `test_quarantine_contract.py::test_quarantine_checkpoint` alone consumed
**7,478.681 seconds (124.64 minutes)**. `test_current_run_contract.py` consumed
4,683.103 seconds; the quarantine journey unit consumed 2,711.911 seconds.
The reported final shard's 282 methods were not the whole suite. All four shards
together started 1,139 methods. More workers alone cannot reduce an indivisible
124-minute unit to under 30 minutes.

## Initial scheduling-slice changes (`ac42fe6`)

1. **Refresh all measured class hints, not only the old top 100.** macOS and Linux
   now have 802 applicable class costs each; coverage and Windows have 681 each.
   Previously the platform top-100 seeds covered only about 35.5% of the newer
   macOS measured work. Unknown classes remain discovered and executable.
2. **Use actual Linux measurements for Linux scheduling.** Linux 3.14 timings
   seed both Linux interpreter lanes; they are not represented as measured Linux
   3.11 times. Windows and coverage retain their own separate inputs.
3. **Add an offline, failure-explicit refresh helper.**
   `scripts/refresh_checkout_costs.py` checks complete phase accounting, finite
   timings, duplicate class ownership and total inventory counts before writing
   a cost file. Failed-test receipts require `--allow-failing-tests` and report
   `tests_passed: false`. The helper never imports or executes tests, changes
   scope, launches CI, or supplies qualification evidence.
4. **Remove exactly two redundant methods.** See the proof table below. No
   runtime production module changes, generic budget changes, fixture caches,
   response-limit changes, or source-verification bypasses were made.
5. **Keep four workers and every gate.** In that initial slice, the new regression
   module is explicitly checkout-only in the distribution contract and no
   installed helper changed. The later additions described above change both
   installed wrapper fixtures and runner scheduling, but not wheel import guards
   or reusable release-workflow bytes.

### Scheduling result: modeled, not a new full-run measurement

The original historical inventories reproduce every archived worker-assignment
hash using the original cost seeds. Repartitioning the same IDs with refreshed
weights leaves every identity exactly once and every class intact. Holding each
historical class duration fixed gives:

| Lane | Original modeled critical worker | Refreshed modeled critical worker | Modeled reduction |
| --- | ---: | ---: | ---: |
| macOS | 9,552.802 s | 8,243.600 s | 1,309.202 s / 13.70% |
| Linux 3.14 | 8,982.469 s | 7,763.760 s | 1,218.709 s / 13.57% |
| Coverage | 8,620.932 s | 7,480.671 s | 1,140.261 s / 13.23% |
| Windows | 2,384.125 s | 2,283.221 s | 100.904 s / 4.23% |

These exclude subprocess/fixture overhead and do not predict changed contention,
changed source, new tests, or passing-path costs. They must not be described as
observed wall-time improvements. The full per-worker counterfactual is recorded
in [`ci-runtime-scheduling-model-2026-11-14.json`](ci-runtime-scheduling-model-2026-11-14.json).

### Exact duplicate proof

| Removed method | Canonical retained method | Evidence |
| --- | --- | --- |
| `test_context.ReviewContextSelectionTests.test_enabled_policy_rejects_head_snapshot_kind` | `test_context_lifecycle.ContextLifecycleTests.test_context_snapshot_rejects_untrusted_head_kind` | Identical `ContextSnapshot("a" * 40, kind="head")` construction and `ContextLoadError` assertion; identical symbol origins; neither class has a setup affecting this method |
| `test_outcomes_verifier_manifest.ContractsTests.test_manifest_rejects_mismatched_release_versions` | `test_promotion_release.PromotionAndReleaseTests.test_manifest_rejects_mismatched_release_versions` | Identical document, `SHA = "a" * 64`, validator and `ReviewInputError`; no relevant setup; return annotation is the only non-behavioral method difference |

All four original methods passed together in **0.001 seconds** before removal.
The removals are cleanup, not an explanation for hours of saved runtime.

Other similar cases were deliberately retained: trigger-command tests use
different pull fixtures; checkout and installed local-CLI tests exercise different
import/distribution boundaries. Expensive quarantine fence, rollover and
coalescing scenarios vary effect location, fault type, ownership, cold state or
liability and are not demonstrated duplicates. No high-cost redundant scenario
was established.

## Preserved platform and gate matrix

| Gate | Required platforms/runtime | Distinct contract |
| --- | --- | --- |
| Checkout compatibility | Linux 3.11 and 3.14, Windows 3.12, macOS 3.13 | Complete discovered checkout inventory, controller hook once, four disjoint workers |
| Quality | Linux 3.11 | Complete branch coverage, **80% floor**, Ruff, mypy, compile and quarantine-registry continuity |
| Schemas/Action pins | Linux 3.11 | Public JSON, immutable legacy readers, corpus, site, protection policy, seeded CodeQL and fixture evaluation |
| Package | Linux 3.11 | Built wheel/sdist, clean core-only/hosted installs, dist-safe and downstream suites, installed publication and launcher qualification |
| npm contracts | Linux Node 22 / Python 3.11 | Launcher/package contracts and lifecycle-disabled packing |
| Linux native | x64 and ARM64 | Debian 12 build/smoke and exact source-bound archive |
| Intel native | Intel macOS, Python 3.11.9 | ABI, frozen engine boundary and CLI parity |
| Other native | Apple Silicon macOS and Windows x64 | Native build/smoke and exact archive |
| Candidate assembly | Linux | Requires all five native bundles, preserves exact provenance |
| Installed candidate consumers | Both Linux architectures, both macOS architectures, Windows x64 | Offline installation and actual installed launcher/native bytes |
| Cloudflare Worker | Linux Node 22 / Python 3.11 | Production broker IPC, accounting, media, caller/command/quarantine/body/tail composition, TypeScript checks and dry-run bundle |
| CodeQL | Python and JavaScript/TypeScript | Actual analysis plus findings gate |
| Required checks | Aggregate | Every dependency succeeds; skipped/failed/cancelled gates are not accepted |

The separate path-filtered major-tag-validation workflow preserves Linux/macOS
Python 3.14 historical released-wheel regression. Manually dispatched downstream,
local-contract and readback workflows remain unchanged. Native build → assembly →
installed-consumer dependencies matter to total elapsed time; their individual
30/15/15-minute timeouts are not an aggregate 30-minute guarantee.

## Initial-slice verification and continuing local execution holds

These receipts belong to the initial `ac42fe6` scheduling slice. They are not
relabelled as validation of the later combined runner candidate.

- Baseline and final discovery were read-only, without executing test bodies.
- Four duplicate candidates: 4 passed in 0.001 seconds before cleanup.
- A first targeted audit command ran 167 tests in 27.346 seconds and terminated
  successfully. It included the checkout-runner regression module and the four
  small duplicate-test modules, not the full product suite.
- **Execution-scope correction:** an exact member of a pre-existing unreconciled
  historical selection was supplied after that command: the runner method
  `test_real_workers_report_success_for_the_complete_inventory`. The first audit
  command had repeated it. This was a coordination error, not reconciliation of
  the original selection. Its original hold remains in force, and no repeat of
  that method or broad runner selection is used to clear it.
- After receiving the complete held manifest, the final targeted selection
  explicitly excluded that one held method and passed **166 tests in 7.456
  seconds**. The runtime difference is due to a different selection and is not
  claimed as a speed improvement. No other known held selection overlaps it.
  The same explicit 166-test selection also passed on the stacked publication
  candidate in **10.714 seconds**, excluding the held method.
- Full local checkout/coverage/installed-suite executions remain held until the original
  selections are reconciled. Targeted success is not a full-candidate pass. No
  replacement CI run was started for that initial slice. The subsequently
  authorized separate draft PR runs its normal remote checks; it does not clear
  the old local holds.
- Whole-repository Ruff format/check passed for 1,032 Python files; Action pin
  validation passed for 19 workflows; JSON contracts, public schemas, helper
  compilation and `git diff --check` passed. Validation initially used an
  interpreter without editable imports, then passed with the documented source
  paths and pinned test environment. No source-package changes require a new
  full mypy or release build in this scheduling-only slice. No merge, deployment, live recovery,
  native approval, protection change or access expansion was performed.

## Credible path to under 30 minutes

1. **First reconcile the held executions and fix correctness failures.** A faster
   failed run is not the objective. Refresh timings from a complete passing
   exact-candidate run when existing owners permit it; retain all original
   accounting/effect evidence.
2. **Profile the dominant setup/source-verification paths.** Begin with quarantine
   checkpoint, coalescing, dictionary producer and ordinary command entry. Separate
   fixture generation, source verification, serialization and subprocess time.
   Reusing an immutable fixture seed is a candidate only after proving independent
   mutable state, unchanged source-freshness guarantees and the same actual crash
   and cold-reader boundaries. Do not cache authority or disable authenticity.
3. **Qualify the reviewed installed scheduling splits.** The draft divides the
   historical 7,478-second checkpoint unit into seven methods and the 4,683-second
   current-run unit into five intact classes. Source/fixture eligibility and
   native elapsed costs still require exact-candidate remote qualification.
   Preserve module/class setup, wrapper load hooks, import guards and exact total
   accounting; scheduling does not reduce the aggregate-work floor.
4. **Then consider additional independent CI shards while preserving every OS.**
   At fixed historical macOS costs, 16 equal workers still need at least 34.35
   minutes of class work. Twenty equal independent workers model approximately
   27.48 minutes before overhead; twenty-four model 22.90 minutes. Those are
   resource estimates, not tested runner settings or approved compute expansion.
   Existing runner bounds remain 1–16, and CI remains at four workers. Actual
   runner CPU/IO capacity, GitHub concurrency and total spend must be measured.
5. **Acceptance is complete exact-head green CI under 30 minutes**, with the same
   platform matrix, original inventory minus only documented true duplicates,
   at least 80% branch coverage, every installed/native/Worker gate, and timing
   receipts showing no missing or duplicated execution. Record end-to-end workflow
   duration as well as each lane; include the build/assembly dependency chain.

This is an isolated test/tooling scheduling change, not a durable production
contract change; no new ADR is required. The unchanged safety and runtime
contracts remain the authority.

### November 15 original close fault injection

Three lifecycle diagnostic methods now interrupt the original caller at the
selected close-method entry instead of replacing protected production methods.
The test-only observer binds the original constructor's successful return, run,
attempt and exact instance, chains the prior profiler, interrupts once, and
restores the prior profiler on exit. Each diagnostic asserts that the original
entry was reached; the existing accounting, publication and privacy assertions
remain unchanged. All three methods passed in 87.553 seconds.

The helper has no shared mutable fixture state. Its new optional exception and
method arguments preserve the existing published-tail KeyboardInterrupt default.
The current-run class and quarantine method contracts now pin this newly used
helper as well as the reviewed test_review_run source. Method inventories and
module socket/environment guards remain unchanged. These checks neither replay
nor settle historical held operations.

### November 15 remote failure receipts

PR #463 head `8578a7d99f0e67adca64f1d1947fa4deb1732c30` completed its
[installed-package job](https://github.com/malsabbagh/review-sensei/actions/runs/43695252733/job/132754313183)
with failures. Its
[retained timing artifact](https://github.com/malsabbagh/review-sensei/actions/runs/43695252733/artifacts/14156612392)
has SHA-256 `f05153289c0f6df260dc51ccda7e077380b1c964ef361ef1ea86c111526467e2`.
All 1,139 assigned test IDs started once across the four workers; each worker
reported execution complete and zero fixture skips. The suite's aggregate `complete` field
remained false. Ten failing subcases covered close diagnostics, ordinary
quarantine journeys, published-tail journeys and body-limit recovery. This is
execution-inventory evidence, not a passing installed-consumer result.

| Worker | Tests started | Elapsed seconds | Largest recorded scheduling unit |
| --- | ---: | ---: | --- |
| 0 | 358 | 4,174.995 | Ordinary quarantine journey: 1,858.190 s |
| 1 | 334 | 5,210.708 | Current command entry class: 1,222.929 s |
| 2 | 311 | 3,816.158 | Published-tail journey: 2,251.084 s |
| 3 | 136 | 4,537.986 | Body-limit recovery: 1,400.564 s |

Worker 3 also spent 1,046.109 seconds in quarantine capacity, 551.306 seconds
in rollover, and 487.501 seconds in stored/lost checkpoint acknowledgements.
The full suite reported 5,210.789 seconds. Recorded unit work totals
17,732.468 seconds: even ideal scheduling over these same four workers would
take 4,433.117 seconds (73 minutes 53 seconds) before overhead. Early failing
journeys can omit later work, so this is not a forecast for a complete passing
run. It demonstrates why scheduling alone cannot meet the 30-minute objective
at these observed costs.

The same head's
[Worker qualification job](https://github.com/malsabbagh/review-sensei/actions/runs/43695252733/job/132754313338)
ran all nine commands once: seven passed and two failed. The bounded controller
elapsed 3,169.810 seconds. Body-limit recovery failed its complete-reader recipe;
published-tail recovery reached the final command and rejected its additional
native/checkpoint branch. Keeping the two failures distinct is necessary when
assessing subsequent fixes. Neither that result nor a shorter partial local
journey establishes complete recovery or sub-30-minute CI.

Later source and fixture repairs require new exact-head CI after the current
useful runs finish. These receipts do not authorize additional workers, retries,
larger bounds, removal of platform/security/recovery tests, or release activation.
