# Invalidated-baseline recovery acceptance evidence

Scope: issue #313, based on `7a641b8` (PR309 merge). These are synthetic
candidate-source and candidate-wheel observations, not published-version or
live hosted qualification. No provider credentials, original grants, remote
ledger writes, release, channel movement or deployment were used.

The fresh-process omission case failed before the fix: a baseline containing
one blocking authorization concern followed by a changed base and an empty
provider finding list returned `reviewed` instead of `partial`. The CLI only
reconciled prior concerns for incremental reviews and related-context overflow.
It now applies the existing conservative reconciliation to every admitted
`fallback-full` scope. An omission, downgrade or ambiguous rediscovery never
proves resolution or replaces the prior baseline. Recovered complete analyses
carry `fallback-full`; no schema or persistence format changes.

## Acceptance mapping

Both `BaselineRecoveryCLIContractTests` (checkout) and
`InstalledBaselineRecoveryContractTests` (wheel) inherit the exact methods in
`tests/baseline_cli_contract.py`. Each CLI invocation starts a separate Python
process with isolated mode, a synthetic working directory and no inherited
credentials. Only fixture transport observation/barriers are injected; socket
creation is prohibited. Installed children independently assert the engine
origin is under their environment prefix.

| Requirement | Exact shared method (`test_` prefix omitted) | Evidence |
| --- | --- | --- |
| Same head, changed base is fresh work; exact retry performs no inference or extra round | `same_head_base_change_full_refresh_and_exact_retry_have_one_round` | One new call/round, current baseline key, exact old admission baseline, unchanged result/recovery state on retry even with unavailable response file |
| Prior concern omission is not resolution | `omitted_prior_concern_preserves_history_and_partial_status` | Uncertain original fingerprint, partial result/outcome, exact prior history retained, one failed attempt |
| Model invalidation receives fresh bounded work | `same_head_model_change_refreshes_once_without_reusing_old_context` | Current model checkpoint, one new call/round, zero-call exact retry |
| Failed and partial full refreshes preserve history | `failed_and_binary_partial_refresh_cannot_replace_baseline` | Unreadable fixture response and binary coverage preserve history and release reservations |
| Oversized individual input refuses safely | `oversized_item_refuses_without_inference_or_state_loss` | No transport call and byte-for-byte equivalent durable record |
| Pause remains authoritative | `operator_pause_remains_authoritative_during_baseline_invalidation` | Paused outcome, zero transport calls, unchanged record |
| Recovery never promotes untrusted baseline history | `untrusted_baselines_refuse_without_inference` | Foreign identity, future generation, generically incomplete baseline and absent history refuse before transport |
| Concurrent callers cannot both own a refresh | `concurrent_fresh_process_cannot_take_refresh_ownership` | Owner blocked at fixture transport while contender is refused; only owner checkpoints and releases its reservation |
| Cancellation has bounded retry | `cancellation_unwinds_refresh_then_fresh_process_recovers` | POSIX SIGTERM unwinds with exit 130, no failed-attempt charge/history loss; subsequent process completes. On Windows the fixture transport raises cancellation because TerminateProcess cannot deliver a Python handler |

Existing `tests/test_baseline_recovery.py` additionally covers real chunking,
related-context overflow, baseline capacity, publication replay charging once,
new-finding human adjudication, changed stage prompts, missing artifact
diagnostics, failed-attempt limits and unknown planner reasons. Baseline
planning/classification contracts remain in `tests/test_verification_baseline.py`.

## Executed checks

- `PYTHONPATH=src:packages/python/review-sensei-github/src python3 -m unittest discover -s tests -p 'test*baseline*.py' -v`: 104 tests passed.
- `python3 -m unittest discover -s tests -p test_cli.py -v` with absolute checkout
  engine/adapter `PYTHONPATH`: 124 tests passed. An earlier invocation used a
  relative path, causing its existing isolated child to import the unrelated
  global editable install; rerunning with absolute source paths corrected the
  test environment without changing the test.
- Built a candidate engine wheel with `python3 -m build --no-isolation --wheel`,
  installed it into `/tmp/baseline-recovery-wheel`, and ran the guarded installed
  wrapper using `-I` from `/tmp`: all nine methods passed. The environment
  shares system dependencies; every tested engine import was verified under
  its own prefix. Test code came from the checkout for this focused run.
- Changed-file Ruff format/lint, mypy (69 source files), compileall and
  `git diff --check`: passed.

## Remaining qualification conditions

The integration owner must package the shared helper, retain the wrapper in
the sdist, and add it to post-package lifecycle selection. The full clean
sdist-extracted package lanes and required repository gates must run on the
integrated exact head. These focused observations do not establish external
provider behavior, SIGKILL recovery, hosted GitHub delivery, published package
behavior or Windows execution. Existing dedicated suites and their fresh
receipts must establish those boundaries separately.
