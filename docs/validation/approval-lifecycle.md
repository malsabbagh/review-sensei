# Approval lifecycle regression evidence

Baseline: fetched `origin/main` at `761b64a3112b0869a173a7a365255f6b07b5d0b8`
(PR #308 merge). The branch uses its own clone. No tracked AGENTS.md or
`.agents/skills` exists at this baseline; CONTRIBUTING.md supplies the gates.
No release version, Action tag, live ledger, credentials or protection is changed.

## Risk-to-test matrix

The existing checkout inventory was inspected before adding tests. Counts are
execution evidence, not a claim that every lifecycle risk is exhausted.

| Risk / oracle | Existing deterministic coverage | Follow-up evidence |
| --- | --- | --- |
| Fix / decision / optional classification; check and approval agree | `test_review_convergence`, `test_github_approval`, `test_github_checks` | Installed lifecycle cross-product: trusted admission through persisted eligibility and enforced check |
| Binary and generated confirmations retain partial AI status; permission revocation and stale confirmation block | `test_mixed_coverage`, `test_human_file_review`, `test_human_feedback` | Installed mixed text/media/generated journey; missing, stale and revoked confirmations; check agreement |
| Exact-head freshness; old decisions do not approve a new head | `test_pr308_ledger_regression`, `test_github_publication`, `test_finalizer_adaptive_reviews` | Preserved PR308 reader/command tests now also run on installed candidate and published wheels |
| Replay, continuation compatibility and schema integrity | `test_session_ledger`, `test_review_transaction`, `test_work_continuation` | Same unchanged five-disposition fixture, schema/digest tampering, reconstructed readers, reserve/commit replay |
| Permission / grant / identity failures do not mutate | `test_github_http`, `test_session_ledger`, `test_pr308_ledger_regression` | Packaged synthetic broker rejection and wrong-identity tests; no claim about original grant authenticity |
| Retry, idempotency and interruption before/after durable write | `test_pr308_ledger_regression`, `test_review_transaction`, `test_epic238_process_faults` | Packaged before-store / after-store interruption with fresh grant and readback |
| Bounded budgets fail closed without minting approval | `test_session_ledger`, `test_work_continuation`, `test_provider_completion_guards` | Installed bound checks and complete checkout gates retained |
| CLI default/operational exits; JSON/stdout and reason/stderr | `test_local_cli_contract`; installed `test_local_review_contract` called Python main directly | Installed fresh-process CLI journeys, including malformed provider output and operational exits |
| Published behavior differs from source candidate | `scripts/qualify_published_cli.py`, exact PyPI wheels and isolated origins | Same lifecycle suite added to published qualification; reports exact versions and failures independently of candidate results |

## CI baseline and measurement

Completed historical reference: CI run [39038340860](https://github.com/malsabbagh/review-sensei/actions/runs/39038340860),
head `a5190bb47853f3663d096c2c0a97477527df5cd1` (before PR308).
Elapsed from workflow creation to required-check completion: 514s. Quality job
508s, branch-coverage step 490s (3463 tests in 485.795s). Windows test step
379s; its four workers took 245.099 / 373.713 / 295.049 / 48.352s despite
866 / 866 / 866 / 865 tests (max/min 7.73). Equal counts were not equal costs.
The merged-main run is recorded separately when terminal.

Seven Python package/tool install lanes duplicate dependency installation (four
compatibility jobs, quality, schemas, npm), plus package build and Worker IPC.
Existing public pip caches hit in all seven recorded lanes; they cache downloads,
not environments. Their install steps cost 7–25s each. Candidate engine wheel
build is already single-use with an archived artifact; standalone builds are
platform-specific and retain their native ABI/security checks. Reusing one built
environment across OS/Python versions would remove meaningful coverage. No such
reuse or trusted cache writes from untrusted PRs are introduced.

The follow-up adds measured class scheduling and per-worker execution evidence,
then evaluates parallel branch coverage while preserving exhaustive discovery,
all-worker completion on failure, isolated data files and the existing 80% floor.
The actual before/after measurements and limits are recorded below after execution.

## Provider boundary

Normal PR CI uses synthetic fixtures and injected transports. Live provider tests
remain opt-in (`tests/published_cli/live_canary.py`), requiring explicit model,
endpoint, credentials and data-egress authorization. No provider credentials or
raw live responses belong in artifacts. The packaged ledger uses a synthetic
consuming broker: it cannot establish authenticity of the unavailable original
grant, and never contacts or changes PR308's live ledger.

## Actual published-package result

Independent command:
`python scripts/qualify_published_cli.py --version 0.7.1 --github-version 0.2.1 --output /tmp/qualification-unique`.
On macOS arm64 / Python 3.14.6, the five existing suites passed all 1,082
rows. The lifecycle suite failed 17 of 30 result rows (failing subcases are
individual rows): the release lacks the required-decision check behavior,
generated-file mixed coverage behavior and five-disposition ledger reader
compatibility present in merged PR308. These are preserved failures, not expected
failures or skips. Exact distribution hashes and suite totals are in
[published-0.7.1-lifecycle.json](published-0.7.1-lifecycle.json).
No claim is made that unpublished changes work in that release. Future release
qualification runs the same assertions against its own exact versions.

## Candidate execution and local scheduling evidence

Final credential-free checkout run: 3,512 distinct discovered IDs, every assigned
ID executed once, all four workers exit 0. Inventory SHA-256:
`65fcc32a68d2b46c112815c2efbdd2dc6f963c77fac8a760f46623c72d40365a`.
Elapsed 60.931s; workers 58.508 / 56.550 / 60.779 / 60.703s. Combined branch
coverage 82.36% (34,513 statements; 15,502 branches), above the unchanged 80%
floor. This is a local macOS arm64/Python 3.14.6 measurement, not an Actions
speedup claim. The committed 53 class costs were measured locally with serial
coverage; omitted classes estimate 0.02s/method. Costs never select the inventory.
A separate fixture proves both sides of a branch survive worker combination even
when one worker intentionally fails; exact inventory and all-worker completion
checks remain active. Hosted timings below establish the actual CI change.

The final sdist-extracted candidate lanes passed 51 dist-safe and 11 downstream
tests from `/tmp`, against only installed wheels. Shared lifecycle qualification
passed 25 test methods (including the eight classification combinations and four
confirmation cases). Engine/adapter versions remain 0.7.1/0.2.1 in these **local
candidate builds**, so wheel digests, source baseline and origin guards distinguish
them from PyPI's different bytes. These candidate artifacts are not published.

| Local candidate artifact | SHA-256 |
| --- | --- |
| engine wheel | `f728862d17253483188dfc3932234b17fc6804344f2f77111bef13a3a6131406` |
| adapter wheel | `873dfe666ae6c92c1a6491f184a4b9ef78dcdff1059103954c4fb95c44872380` |
| engine sdist | `beafe88a9d212662e7cddb1b83082a7d849f27dca2727c482ce973ddfa2e707a` |

Action pins, JSON/public schemas, Ruff formatting/linting, mypy (110 source files),
source compilation, sdist contract (28 required entries / 10 excluded checkout
modules), pip dependency consistency, publication bootstrap contracts (23 tests),
and targeted workflow/distribution/runner contracts passed. An initial local
serial attempt inherited retired provider settings and was invalid as a clean
baseline; those failures were reproduced and cleared in the sanitized environment.
A temporary nested adapter build directory also tripped the existing source-copy
inventory assertion; deleting that generated build output restored the full gate.

Supplemental authorized ReviewSensei CLI review used `deepseek-v4.1-flash:cloud`
on the staged public diff with no secrets in review input. Its useful finding
was loss of the timing receipt if coverage combination raises. The runner now
catches coverage/I/O errors, fails closed and still writes the report; a focused
fault test proves this. Speculative warnings about unittest reentrancy and the
already-guarded packaged helper did not establish a defect. Both coalesced test
class spellings are present in the actual discovery inventory, so both measured
keys are intentional. Positional confirmation fields were clarified as keywords.

The merged-baseline CI run [39088538249](https://github.com/malsabbagh/review-sensei/actions/runs/39088538249)
finished with quality green (688s job duration), Windows green (374s job), and
Linux x64 standalone failing because ECR Public rejected the pinned image pull
with `toomanyrequests: Data limit exceeded`. The first draft run hit the same
external failure before executing the build. No gate is disabled or bypassed.
