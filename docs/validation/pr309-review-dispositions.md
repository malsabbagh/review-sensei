# PR309 review dispositions

Review source: [ReviewSensei review 5561919198](https://github.com/malsabbagh/review-sensei/pull/309#pullrequestreview-5561919198)
at head `b04230946ce3b2f13707ed0cc74780701ac869c1`, based on current main
`ead45bdaed3e0d1d0025b43ecac043a775e43b98`. Both inline threads and all eleven
body-only optional findings were inspected. The issue discussion contained only
the session ledger; it was read, not edited.

| Finding | Disposition and evidence |
| --- | --- |
| **RS-DB07A7**, inline 4306155210 | **Fixed.** Timing files are actually read inside the temporary-directory lifetime, but truncated JSON, invalid UTF-8, invalid shapes or unexpected fields could escape and lose the parent report. The reader now validates only worker timing/count fields, records a sanitized `timing_error`, preserves actual subprocess exits, writes the parent report and exits 1. A subprocess regression overwrites worker timing at process exit; it fails on the old runner and passes with the repair. |
| **RS-FCECA8**, inline 4306155214 | **Not reproduced; premise does not match the lanes.** The checkout runner collects editable-source coverage; the installed-wheel lane uses direct unittest commands and import guards, without this runner or `--coverage`. Coverage 7.16.2 resolves a non-directory `--source` value as an importable package, including a package outside cwd. The branch-union fixture now uses such an external package; a separate empty-data fixture proves the required `coverage report --fail-under=80` returns nonzero. An actual installed 0.8.0/0.3.0 probe records 201 engine and 102 adapter initializer lines beneath the isolated prefix, with branch data and cwd outside checkout. A prefix-only import guard in the checkout runner would incorrectly reject its intentional editable installs. No authority override or claim of owner assessment was made. |
| RS-19ABB1 | **Already satisfied.** `test_pr308_ledger_regression.py` and both fixtures are explicit required helpers in `distribution-contract.json` and explicit `MANIFEST.in` entries. The installed lifecycle module adds the packaged tests directory before importing the helper. A missing required helper must fail discovery, not become a skip. Candidate sdist/wheel CI and published lifecycle qualification exercise the hook. |
| RS-37DFE7 | **Strengthened.** The scheduling test now checks total assigned measured costs `[10, 11]`, in addition to disjoint/exact inventory and deterministic assignment. Separating heavy classes while putting both light classes on the wrong worker no longer passes. |
| RS-3C152F | **Verified, no change needed.** The two named modules exist directly under `tests/dist_safe`; neither name collides with a module directly under `tests`. The explicit search paths are intentional for these non-package lane directories. Full installed qualification loads all 27 methods, and import guards reject checkout runtime imports. |
| RS-5FD605 | **Not reproduced.** `published-cli.yml` invokes qualification without `--suite`, so all six suites, including lifecycle, run. The immutable historical validator explicitly selects five suites in a different workflow and does not upload a lifecycle result. Keep required artifact failure behavior. Published 0.8.0/0.3.0 qualified all six with no exclusions. |
| RS-644AA6 | **Not reproduced under unittest's protocol.** `TestCase.run` calls `startTest` before entering the try/finally that calls `stopTest`; a failed `startTest` does not invoke `stopTest`. Class setup failures do not call a test's `stopTest` without `startTest`. Arbitrary custom tests can violate the protocol, but no such case is present. Damaged/missing worker evidence now fails closed through the timing reader rather than hiding the exit status. |
| RS-9998F7 | **Addressed.** Reports now distinguish `execution_complete` and `coverage_ok` (null when not requested), retaining `complete` as their aggregate for compatibility. Coverage-failure tests assert execution remains complete while coverage is false and the lane fails. |
| RS-332E71 | **Documented contract retained.** `.coverage` in runner cwd is the intended combined-data location used by the following CI `coverage report` command. CONTRIBUTING now states this explicitly. The fixture intentionally tests that public handoff, not a scratch filename. |
| RS-493ACE | **Both keys are live.** Current discovery includes both `test_coalesced_admission.CoalescedAdmissionTests` and `tests.test_coalesced_admission.CoalescedAdmissionTests`. They are distinct unittest IDs in the existing inventory. Costs use their exact dotted class IDs; deleting either estimate does not normalize discovery and would discard a measured cost. |
| RS-5391C2 | **Retain explicit configuration failure.** Omitting `--costs` already selects count-based scheduling. An explicitly requested tracked file that is missing should fail rather than silently switch scheduling. Normal CI checks out this required repository fixture. No required tests or checks are omitted. |
| RS-7092B6 | **Already documented.** `tests/published_cli/README.md` explicitly distinguishes receipt rows (including failing subcases) from method counts. Historical 17/30 and 19/33 failure rows remain labelled as suite snapshots; published 0.8.0 has 27 passing lifecycle methods/rows. |
| RS-77B72C | **Addressed.** The validation README indexes immutable historical snapshots, labels 0.7.1/0.2.1 failures as historical, and links a separate exact-PyPI 0.8.0/0.3.0 passing receipt. The original observations and hashes are preserved. |

The full published 0.8.0/0.3.0 run passed 1,109 rows with no suite exclusions,
verified installed origins and an exact adapter-to-engine pin. This is independent
of candidate builds and checkout tests. Synthetic broker fixtures still cannot
prove authenticity of the original unavailable grant. Current-head CI and actual
hosted review receipts are recorded in the PR description and pickup comment;
skipped review jobs do not constitute approval.

## Follow-up hosted review on 4b8b8f3

[Review 5561952765](https://github.com/malsabbagh/review-sensei/pull/309#pullrequestreview-5561952765)
actually executed against `4b8b8f38b7b536a5d4bfb81bef53ae63818d8967`, with 23
reviewed paths and complete enumeration. It reported no required fixes and 17
required human decisions. Workflow success was not treated as approval. All 15
ordinary CI jobs passed on that head. Responses below are engineering evidence,
not maintainer assessments or approval overrides.

| Finding | Disposition and evidence |
| --- | --- |
| RS-C06B95, 4306187841 | **Not reproduced.** Engine and adapter wheel names are distinct (`review_sensei-0.8.0-...` versus `review_sensei_github-0.3.0-...`). The uploaded candidate artifact retains `github/` for the adapter. Package consumers install from local build paths, not by re-downloading this artifact, so artifact layout cannot silently change those consumers. The actual CI artifact is checked separately. |
| RS-F296F1, 4306187844 | **Additional fault coverage.** Existing real-process tests already fail on a nonzero worker and preserve both branch arms; the corrupt-combine test already retains the parent report and exits 1. A new fault matrix explicitly tests missing worker coverage, missing worker timing and zero executed tests, all with a simulated exit-0 worker: each fails closed and skips combination. The empty-data coverage-floor test remains. |
| RS-877514, 4306187845 | **Not reproduced; now fault-tested explicitly.** Every parent worker command always includes `--worker-report`, independently of the optional parent `--report`. Validation and exit decisions are outside `if args.report`. Nonempty assignments plus `executed == assigned` reject zero execution, absent reports and invalid data even if the subprocess exits 0 and a coverage file exists. |
| RS-467F25, 4306187846 | **Invariant already enforced.** Partition creates only nonempty groups, checks distinct/nonempty discovery, and zero executed does not match an assigned positive count. The new zero-execution case explicitly proves rejection; no redundant positive-count gate is needed. |
| RS-3CA8AB, 4306187848 | **Not reproduced.** `.coverage` in cwd is the documented public handoff to the following `coverage report` command. A fresh Coverage instance combines only explicitly named current-worker files. The branch-union test now seeds an unrelated stale `.coverage` record and proves it is absent from the new combined data. Moving the result into disposable scratch would break the required next-step handoff. |
| RS-018138, 4306187849 | **Documented estimate retained.** Unmeasured classes intentionally estimate 0.02 seconds/method, in the same units as measured class seconds, as documented in the original evidence. Using one second/method would distort those units and overwhelm measured costs. All 53 estimates and all discovered classes remain separate from test selection; exact inventory and per-worker class timings expose drift. This is an estimate, not a claim of a measured duration for every class. |
| RS-C915F8, 4306187855 | **Already covered by the oracle.** The `(fix=False, decision=False, optional=True)` combination expects check success and approval true while actually admitting an optional candidate. If that candidate starts blocking, the actual check/approval becomes non-passing and the assertion fails. Expected output staying invariant across the optional dimension is precisely the required nonblocking behavior, not a missing assertion. |
| RS-A7CAB7, 4306187857 | **Required failure retained.** The PR308 helper/fixtures are declared required packaged assets; their runtime dependencies are the explicitly installed engine/adapter and runtime dependencies. A failed required import must fail discovery and the lane, never become a fallback or skip. Published qualification and candidate sdist tests execute the helper. |
| RS-6A1F60, 4306187858 | **Improved stable diagnostics.** Unreadable/invalid reports now carry `executed: null` and `class_seconds: {}` with `timing_error`, distinct from a valid `executed: 0` report. The caller still compares valid counts to the assigned manifest and rejects a mismatch. Fault tests check both cases. |
| RS-3AEAFE, 4306187860 | **Hardened against future name collisions.** Contract modules now load from their exact `tests/dist_safe/*.py` paths. A regression seeds both same-name search-path files and already-cached wrong modules; the two intended contract files still run. Current imports were correct, and the installed runtime origin guards remain. |
| RS-3CD8B6, 4306187866 | **Improved diagnostic clarity.** Import/file-load failures now produce a sanitized `load_error`, a failed receipt row and `tests_run`; an empty suite produces an explicit `empty-suite` row and exits 1. No empty or failed discovery is accepted as qualification. |
| RS-34F1B4, 4306187868 | **Test's assignment contract is valid.** Two positive-cost classes and two requested workers create two nonempty groups under the deterministic least-loaded scheduler. The first class loads one worker; the second chooses the still-empty worker. Both independent successful workers deliberately corrupt their reports at exit. Existing partition/fixture tests prove nonempty group assignment. |
| RS-59CDD2, 4306187869 | **Intent documented.** Order-independent deterministic class assignment is intentional for reproducible CI scheduling, and is now stated on `partition`. The exact-inventory and measured-load assertions remain independent checks. |
| RS-978E09, 4306187875 | **Already wired in.** `test_approval_lifecycle_contract.load_tests` adds `PreservedLedgerRegressionTests` to unittest's module suite. `loadTestsFromModule` calls this hook. The published lifecycle receipt contains 14 passing PR308 methods among its 27 methods, including the exact five-disposition public-reader regression. |
| RS-14E573, 4306187878 | **Already checked and exercised.** The two asserted include edges establish `ci.txt -> test.txt -> major-tag-validation.txt`; the pinned YAML requirement remains reachable. Actual CI installs the files and runs the YAML-dependent tests. The earlier single direct-include string assertion did not test transitive resolution either. |
| RS-695BB7, body-only | **Already triggered.** Both pull-request and main-push path filters explicitly include `scripts/qualify_published_cli.py` and `tests/published_cli/**`, in addition to the requirement files. The suite registry lives in the included script. No trigger change is needed. |
| RS-84CF4F, body-only | **Same protocol evidence as RS-644AA6.** Standard unittest calls `startTest` before the try/finally invoking `stopTest`; class setup failures do not call this instance's `stopTest`. No failing protocol case was reproduced. Missing timing evidence from any abnormal worker still fails closed. |

## Incremental review on f3938c1

[Review 5561981364](https://github.com/malsabbagh/review-sensei/pull/309#pullrequestreview-5561981364)
ran on `f3938c11675c197c5eea8cdb8f09c85a0f546383`. It reported partial analysis,
23 reviewed paths, 14 required human decisions and two optional suggestions.
Ordinary CI passed on that SHA; the actual ReviewSensei gate remained
`ACTION_REQUIRED`. A partial review and engineering replies are not approval.

| Finding | Engineering response |
| --- | --- |
| RS-87E28B, 4306215492 | Existing real-worker failure, missing-coverage/timing, zero-execution, corrupt-combine and damaged-report tests all return nonzero on the stated faults. Both subprocess exit and evidence completeness participate in the final return. The following 80% report floor is an additional gate, not a replacement for inventory checks. |
| RS-9D54F1, 4306215495 | Workers compare the exact loaded ID list with their assigned manifest before executing; discovery mismatch produces exit 1. The existing missing-worker-test fixture tests this path. The parent inventory hash identifies that same discovery list; no untrusted worker-provided hash is used. Missing coverage and worker failure have separate explicit fault tests. |
| RS-B3A794, 4306215497 | `published-cli.yml` calls `qualify_published_cli.py` with default scope. Its `SUITES` includes lifecycle, and it invokes all six files. The exact published 0.8.0/0.3.0 run passed all six, including lifecycle's 27 methods. The artifact has a real producer. |
| RS-8869C7, body-only | The engine was already installed at the adapter's exact required version, so pip had no unsatisfied engine requirement. Hardened the adapter-install command to include **both local wheel paths as explicit direct requirements**, retaining the engine-only smoke beforehand. An incompatible adapter pin now conflicts with the explicitly supplied local engine instead of selecting another PyPI engine. Runtime dependencies may still resolve normally. |
| RS-1C0D05, body-only | The compatibility matrix explicitly defines both `os` and `python-version` on all four entries. Windows writes the timing file; other platforms legitimately warn that no file exists. All four matrix jobs and artifact expressions have actually executed successfully. |
| RS-F2B8C7, 4306215499 | The test and two fixtures are explicit helpers in `distribution-contract.json` and entries in `MANIFEST.in`. Generic contract tests require every helper to be covered by the manifest, and the actual sdist verifier checks all required entries. No missing declaration was reproduced. |
| RS-D5C4A5, 4306215504 | Added defensive `started = None`, guarded timing accumulation and reset after stop, even though standard unittest respects the protocol. A real subprocess fixture calls stop without start: it no longer raises from timing, but zero executed versus one assigned still fails the parent. This also addresses RS-644AA6 and RS-84CF4F defensively. |
| RS-E16AE0, 4306215506 | Load errors already had a distinct failed row, `tests_run=0`, `load_error` and exit 1. Clarified output further by skipping TextTestRunner entirely on load failure, avoiding its empty-suite OK text. A real installed-environment probe confirms the sanitized ImportError receipt fails with zero tests run. |
| RS-246B34, 4306215508 | The claimed arithmetic defect does not reproduce: total is the number of rows and pass/fail are complementary counts of those rows. unittest does not emit parent addSuccess after failing subtests. A direct reporter probe with three failing subtests yields total=3, pass=0, fail=3, tests_run=1. Parent/subtest rows and method counts are documented separately. |
| RS-DF23FC, 4306215512 | The bootstrap test intentionally puts the wrong search-path copy first and preloads wrong modules, a stronger adversarial case than placing the correct directory first. It restores both module entries in finally and patches/restores sys.path. Production-path loading is independently exercised by the actual installed-wheel script. No leakage or missing cleanup was reproduced. |
| RS-B16F23, 4306215514 | **Fixed:** moved the main guard after InstalledProcessExitTests. Direct execution of the file now runs all seven local CLI contract methods against the published wheels, rather than omitting the new class. The lifecycle loader continues to run all 27 methods. |
| RS-AA5643, 4306215518 | Documented the one-shot process contract on load_contracts: cached contract names are replaced for that process, keeping class globals available while tests run. Production invokes a fresh subprocess; the embedding bootstrap test explicitly isolates/restores names. This is not a reusable global module-loading API. |
| RS-01B731, 4306215524 | Both Windows and quality workflow commands pass the fixture via `--costs tests/fixtures/checkout-class-seconds.json`; the runner reads it before partitioning. CONTRIBUTING and the original evidence document that consumption and estimate fallback. Both coalesced class keys are present in actual discovery. No missing consumer was reproduced. |
| RS-046EF5, 4306215528 | Extended the fault matrix to an exit-7 worker with corrupt timing and coverage: the parent retains exit=7, executed=null, execution_complete=false and exits 1 without combining. A valid completed inventory with a test failure remains execution-complete but non-successful, by design; real exits are never collapsed into the completeness field. |
| RS-48E63C, optional body-only | The historical validator installs its dedicated `requirements/major-tag-validation.txt`, not ci.txt. Installer/bootstrap tests then create their own exact-wheel environments. It watches broader requirement changes to validate the include contract. No missed dependency or silent test skip was reproduced. |
| RS-D2EDB0, optional body-only | Already covered by BootstrapContracts.test_default_suite_set_includes_lifecycle_and_subsets_are_explicit: the five historical names are accepted; empty, duplicate and unknown selections are rejected. The historical validator uses current harness code against pinned old wheels, not an old harness lacking --suite. Its actual Linux/macOS runs passed. |

Current-head CI/review run links and any remaining required human assessments are
reported on the PR. No maintainer override, manual session-ledger edit or silent
draft-state change is used to bypass these assessments.


## Review and explicit full rescan on 169a2b6

The [incremental review 5562011726](https://github.com/malsabbagh/review-sensei/pull/309#pullrequestreview-5562011726)
was partial (18 required human decisions, one optional suggestion). The explicit
[rescan 39109501099](https://github.com/malsabbagh/review-sensei/actions/runs/39109501099)
actually ran the released engine 0.8.0 and hosted adapter 0.3.0 against this PR's
head, using the approved DeepSeek4.1Flash model. Its
[review 5562027053](https://github.com/malsabbagh/review-sensei/pull/309#pullrequestreview-5562027053)
reported complete analysis of 23 paths, no required fixes, 23 required human
decisions and three optional suggestions. These are engineering responses,
not maintainer assessments or approval overrides. A completed workflow is not an
approved review.

| Incremental finding | Engineering response |
| --- | --- |
| RS-0F0BEA, body | Both local wheel paths are explicit pip requirements. An unmatched shell glob remains an invalid literal and pip fails; it cannot silently select a registry replacement. Actual candidate artifact installs passed. |
| RS-C7C31A, body | CONTRIBUTING documents the default `.coverage` in cwd; `--report` controls JSON only. The stale-data replacement and both-branch tests verify this handoff. Workers use absolute temporary paths. |
| RS-2FA573, body | The root sdist glob is the engine archive. The adapter wheel lives under github/ and cannot match that root archive glob. Separate engine/adapter artifact contracts pass. |
| RS-A3D570, body | The scheduling test checks actual fixture totals 10 and 11, reversed-input determinism and invalid costs. The 0.02-second unknown-class estimate is documented, not presented as a measurement. |
| RS-0085D2, body | Worker JSON is an internal same-script-version protocol, deliberately strict. Unknown fields fail closed, preventing an unexpected field from overriding parent-owned exit evidence. |
| RS-808AD4, 4306244038 | All three paths exist and are required helpers in distribution-contract.json. Manifest and actual-sdist verification passed; additional grafting is unnecessary. |
| RS-C15AED, 4306244042 | The whole PR adds scripts/run_checkout_tests.py and its fault tests. An incremental view omitting the earlier addition does not mean implementation is absent. The documented behavior is exercised. |
| RS-E7DF67, 4306244045 | The include edges are tested, pinned dependencies remain transitively reachable, and actual CI installs succeed. Historical validation uses its dedicated requirements file. A comment about indirection is optional style. |
| RS-6788C5, 4306244050 | No current dependency between declaration order exists. Selection validates known suite names; proposed future membership changes need their own contract. |
| RS-51034F, 4306244054 | Default qualification runs all six suites. Explicit historical subsets record selected and excluded names; assertions are intentionally not weakened based on version. Published 0.8.0/0.3.0 passes all six. |
| RS-905EBE, 4306244056 | harness_ref is exactly the harness revision used, not wheel_source_ref. The evidence index explicitly labels immutable snapshots and wheel hashes; later reruns are separate evidence. |
| RS-0E3645, 4306244058 | Worker write errors are not swallowed: stderr is captured and replayed and the worker exits nonzero. Parent read errors retain timing_error, null executed and the actual exit. Corrupt-report tests verify failure evidence. |
| RS-2038D5, 4306244063 | Cold isolated processes insert the helper root before import; required-helper and actual-sdist checks prevent missing packaged assets. Runtime prefix guards prevent accidental source imports. Explicit lifecycle file loading also rejects cached/path-shadow contract modules. |
| RS-D1716B, 4306244066 | Both module-root variants actually occur in discovery, through existing unittest hooks. They are distinct discovered IDs, not dead fixture entries. |
| RS-7665AF, 4306244068 | Each thread owns one distinct index and the parent reads only after pool.map joins. No competing writer or process-pool implementation exists. |
| RS-E1C672, 4306244071 | The allowlist omits PYTHONPATH/PYTHONHOME and -I ignores Python environment/site customization. No TMPDIR-under-scratch promise exists; all explicit test artifacts use scratch paths. |
| RS-7101E3, 4306244075 | Real reporter probe: three passing subtests yield one passing parent row, tests_run=1. addSuccess already records it, so empty-suite is not triggered. |
| RS-614C40, 4306244081 | Ordinary import exceptions produce sanitized failure receipts. Interrupt/SystemExit still propagate; the outer qualifier requires both process success and valid evidence, so absence cannot become green. Current contract modules do not exit during import. |
| RS-FC19A1, optional body | The current published flow resolves exact release versions and defaults to all six suites. Historical pinned validation deliberately declares its narrower scope. |

| Full-rescan finding | Engineering response |
| --- | --- |
| RS-35008C, body | **Fixed:** reject non-object cost JSON and invalid key/value types with a controlled error before any worker runs. New public CLI tests cover lists, booleans, malformed JSON, string/bool/NaN values and missing files, assert exit 1, no traceback and no execution marker. |
| RS-6BB0F2, body | pip resolves declared dependencies, so installing normal declared runtime requirements does not hide missing dependency declarations. Both local distributions are explicit requirements; engine-only smoke still precedes adapter installation. |
| RS-838165, body | The whole PR contains the runner implementation and public fault tests for its flags; the prior head passed 12 runner methods. The added cost-input method increases this to 13. |
| RS-1F2240, body | Missing/invalid explicitly requested cost files must fail, rather than silently discard measured scheduling. The cost-input fix now also reports missing files as controlled exit 1 before execution. |
| RS-5CF75D, 4306258099 | Discovery uses the same unittest TestLoader.discover default pattern and start directory as the serial command, including load_tests hooks. Actual inventory counts are verified; verbosity affects output only. |
| RS-B3CE32, 4306258104 | Worker-file, exact-inventory and combine-error tests exist. Complete test execution may still fail assertions: combining such coverage preserves diagnostics while the final real exit remains nonzero. |
| RS-EA7C1B, 4306258106 | The actual published workflow is the producer, defaults to all six suites and uploads its receipt. Fresh exact published-wheel qualification passed all 1,109 rows. |
| RS-A17F9F, 4306258109 | The lean explicit helper allowlist and manifest/sdist verification cover current referenced fixtures. New future fixtures must be declared; unrelated test data need not be grafted. Missing required assets fail the installed journey. |
| RS-2DE957, 4306258112 | HOSTED_SUITES is consulted inside the selected-suite loop, so excluded suites are not invoked. Both exact wheels are deliberately installed and their dependency pin checked for every qualification. |
| RS-7821B2, 4306258116 | Cwd coverage handoff is documented and tested. Both package roots are installed by the coverage CI jobs; missing tools/packages correctly fail instead of silently reducing coverage. |
| RS-81408E, 4306258120 | execution_complete means assigned inventory ran, not that assertions passed. Actual exit is separate and required for success. Coverage may intentionally be combined from completed failing tests for diagnosis; folding exit into inventory completeness would lose that distinction. |
| RS-405590, 4306258124 | Raw elapsed timing permits zero for clock precision; scheduling weights require positive finite values. The worker report is not a directly consumable costs file (it contains nested metadata). Refreshing estimates requires aggregation/normalization; bad explicit weights fail visibly. |
| RS-53957B, 4306258127 | requirements/test.txt includes pinned coverage and is included by ci.txt. Both actual coverage lanes installed it and passed; a missing required coverage tool should fail. |
| RS-944155, 4306258133 | Each contract module inserts its helper root before load_tests is invoked, including cold isolated runs. Missing required packaged helpers must fail collection rather than skip qualification. Actual sdist and published journeys passed. |
| RS-CC9839, 4306258138 | Qualification intentionally treats skipped checks as failed qualification; rows include kind=skipped, so they cannot masquerade as independent passes. The exit requires not failed as well as wasSuccessful. |
| RS-C0B387, 4306258141 | The standard library addSubTest appends to failures/errors directly; it does not call this overridden addFailure. Actual probe: three failing subtests produce exactly three failed rows, zero passes, tests_run=1. |
| RS-EF1683, 4306258144 | The whole PR includes production flags/implementation and tests. An incremental diff omitting earlier additions is not evidence that implementation is absent. |
| RS-504389, 4306258146 | Deterministic order-independent partitioning is an intentional documented contract. Sorted class keys and tie-breaks implement it; measured totals 10/11 and reversed input are asserted. |
| RS-81D3A7, 4306258149 | The test already asserts stale.py is absent and verifies both new probe.py arcs from distinct workers. Reusing the public default .coverage path is intentional and proves replacement. |
| RS-655CF8, 4306258152 | Parent replays child stderr. The test also asserts structured timing_error, null executed, real zero worker exits and false completeness; traceback absence is supplementary. Bytes repr is valid generated Python and write_bytes deliberately creates invalid UTF-8 evidence. |
| RS-2ADC2A, 4306258157 | Runner tests separately reject missing/incomplete coverage data. The explicit following CI coverage report enforces the 80% floor; the empty-data CLI test protects that command. The runner is not advertised as enforcing a percentage floor itself. |
| RS-7CE03B, 4306258158 | The simulated worker deliberately exits zero with valid timing but missing coverage; parent exit 1, coverage_ok=false and combine-not-called prove rejection of that evidence fault. Requiring the simulated worker to fail would test a different condition. |
| RS-3A22AE, 4306258163 | The public subprocess test already checks no traceback, real child exit 0, executed=0, timing_error=null and false inventory completeness. An unrelated parent exception could not satisfy its valid structured receipt checks. |
| RS-F58AD6, optional body | Both requirements files are part of the whole PR diff. Include-contract tests and actual CI installs verify their use. |
| RS-714000, optional body | test.txt intentionally includes the shared pinned YAML requirements file. Its only dependency is PyYAML; no unnecessary historical validation tool is pulled in. |
| RS-5BBC8A, optional body | The bootstrap test already accepts the exact historical five-suite selection, checks default all-six membership and rejects empty/unknown/duplicate names. |

## Maintainer follow-up to the pending receipt

The pending receipt on parent head `6ac71fecee3709128bb4bed56278267e5409bfcd`
was checked against the full candidate code and focused regressions. These
engineering dispositions do not clear the hosted review's partial-analysis
status. Any maintainer risk decision must be newly recorded for the exact head.

| Finding | Follow-up disposition |
| --- | --- |
| RS-1BC712 | Hardened: resolve both fixed contract paths and their directory before either module executes; reject symlink escapes. A filesystem regression fails on the parent and passes with the guard. Test-harness contents remain trusted qualification-ref code, not authenticated wheel contents. |
| RS-1F1771 | Clarified why the checkout JWT tests preserve the former CI cryptography pin. Published-wheel qualification creates a separate environment and resolves wheel-declared runtime dependencies. |
| RS-38C8CD | False positive: missing worker coverage means that worker's data file is absent. Valid timing, assigned execution and exit zero isolate this fault; combine must not run. Existing fault and real-process branch-union tests cover distinct cases. |
| RS-4B3351 | Retain the documented distinction: complete/execution_complete describe evidence/inventory completeness; each actual worker exit is recorded, and every exit must be zero for lane success. A real assertion-failing worker produces exit 1 despite complete diagnostic evidence. |
| RS-4EFE15 | False positive: the runner verifies every worker's timing/count and data-file presence before combining; missing or incomplete evidence forces exit 1 before the percentage report. |
| RS-50CE88 | Strengthened bootstrap isolation: restore the complete module mapping; the existing path patch restores the original path object. Synthetic modules import only the already loaded unittest module. |
| RS-73F040 | Documented explicitly: any skipped method/subtest is kind=skipped and a failed qualification row, forcing exit 1 even when unittest permits skips. |
| RS-8ED9CC | Retain diagnostic coverage from completed failing workers. Combining their trace never overrides their nonzero exits; both the runner and CI lane fail. This is intentional diagnostics, not successful qualification. |
| RS-987788 | False positive: every coverage worker explicitly receives --branch and --source=review_sensei,review_sensei_github. The CI floor remains 80%. |
| RS-C3443B | Strengthened: each damaged payload now requires its exact sanitized error. Deliberate NaN input tests semantic rejection by the permissive Python JSON decoder's downstream finite-number validation. |
| RS-C4F1C3 | Retain unittest parent-method semantics: passing subtests produce one parent pass row, failed subtests produce individual failure rows, and final success requires wasSuccessful plus zero failed rows. The separate counts are documented. |
| RS-E5FD12 | False positive: both local wheels are explicit pip requirements in one transaction. A conflicting exact engine dependency cannot replace the explicit local requirement; resolution must fail. The clean consumer still resolves other declared runtime dependencies normally. |
| RS-F3589F | Retain subset assertions for the tiny stable if/return probe, not equality against the entire arc graph. Exact executed counts intentionally protect exhaustive inventory; cross-platform CI tests the same contract. |
| RS-F77278 | Added the exact scheduling-fixture SHA-256 and origin commit to the measurement document, distinct from test inventory hashes. |

No ADR is needed: the follow-up changes qualification harness bounds, regression
assertions and documentation; runtime schemas, authentication, persistence and
approval policy are unchanged. A code-path review by this follow-up is separate
from hosted AI evidence and does not turn a partial hosted result into complete.
