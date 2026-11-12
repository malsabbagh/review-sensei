# ADR 0131: Qualify provenance manifests, recovery context, pre-effect representability, and combined capacity envelopes

- Status: Proposed
- Date: 2026-11-11
- Decision owner/reviewers: ReviewSensei maintainers; independent source review
- Linked GitHub Issue: pending; lifecycle gap closure Batch 1 (G11, G17, G34, G12)
- Linked PR: pending; independent prospective correction

## Context

Four coupled lifecycle boundaries previously lacked explicit cross-stage admission and capacity contracts:

1. **Runtime vs. source and release provenance (G11):** Inspecting a repository `main` commit (`07179c0f2bd6b761a95449ce3ce29d4633c80094`), a published GitHub release (`v0.8.20` -> tag object `ffaf25468dbf682e43cb09bbfb047b6bd86c393e` -> commit `a4c605c51162dd37dc1b938e6e77fb535ff6a566`), or a commit-level CI status does not prove which annotated reusable-workflow tag object (`a72527097205110337a6841a545f548610c7fbaa`) and peeled commit (`d626185fff387dab14aedd48b2b0684e92c84739`), installed `review-sensei` / `review-sensei-github` package version, executor, frozen policy snapshot, or broker enforcement route/revision ran in an attempt, nor can routing preflight substitute for authenticated live validation or broker capability enforcement.
2. **Original recovery context across fresh processes (G17):** Replaying retained review results without fresh model inference requires verifying exact digests of the original configuration, admission, convergence, work, and decision policies, selected context, learnings, verification baseline, and diff. Default checkout reconstruction fails when runtime-only inputs or overwritten baselines were involved, while storing raw diffs or prompts would violate data-minimization rules.
3. **Terminal result representability vs. pre-effect admission (G34):** A maintainer command replay on an authenticated 2-decision `disposition_snapshot` (`10,369`B `> 10,240`B inline array) previously stripped `disposition_snapshot=None` during sandbox preview (`809`B `"recorded"` preview vs. `9,354`B / `9,578`B `"already-recorded"` `apply_session_command` carrier `> 8,192`B), causing hosted command settlement to fail post-effect when constructing the inline `caller-action-result-v1` manifest. Separately, the historical 39-entry (`38` 250B `file:` tokens + `1` mandatory 71B `result:` token) component witness (`10,195`B inline dispositions, `20,933`B `SessionRecord` envelope, `9,750`B / `9,690`B carrier vs. `8,192`B) illustrates sub-cap asymmetry as a component control without establishing a live single-command post-effect loss.
4. **Combined capacity and cross-stage interruption envelope (G12):** Individual sub-caps (`10,240`B inline dispositions, `12,288`B convergence history, `8,192`B assessment queue root, `4,096`B publication staging, `8,192`B inline manifests) plus `disposition_snapshot` roots, `quarantined_state`, `historical_assessments`, intermediate replacement/staging peaks, and future lifecycle growth exceed the `32,768`-byte `SessionRecord` root when combined, while consumed-grant markers, multibyte UTF-8 expansion, cumulative stored parts, and ordinary part reads can exhaust the `65,536`-byte comment/review limits, the `60`-call ordinary evidence read limit (`4` reserved fence reads), or the physical API request allowance (`4` protected tail requests).

## Decision

1. **Sanitized provenance, release/CI separation, and attempt evidence (`review_sensei.provenance_manifest`, G11):**
   - Record repository `main_sha` (`main_sha_is_runtime_proof: false`), caller workflow and reusable workflow ref, annotated workflow tag object (`reusable_workflow_tag_object`) distinctly from peeled workflow commit (`reusable_workflow_peeled_commit`), published release tag/object/commit (`latest_release_tag`, `latest_release_tag_object`, `latest_release_commit` with `release_is_deployment_proof: false`), timestamped commit-level `CiRunObservation` entries (`ci_status_is_deployment_proof: false`, `is_deployment_proof: false`, distinguishing `transient_artifact_upload_5xx` such as exact-main `CI` run `43094461694` on `07179c0f2bd6b761a95449ce3ce29d4633c80094` from `Push on main` run `43094456791` `success` without dispatching duplicate `main` runs), installed core and hosted package versions (`build_provenance_manifest(..., auto_inspect_local_packages=False)` by default), executor, frozen policy commit/digest, reviewed PR base/head/event, broker route/revision, and separate `GateObservations` (`routing_preflight`, `authenticated_live_validation`, `broker_enforcement`) without performing network probes when telemetry is `"unknown"`.
   - Separate nine lifecycle receipt stages (`intent` through `terminal_accounting`), preserve conservative request debits and unknown usage charges, reject credentials and raw diff lines, and enforce `authority: false` on every diagnostic artifact.

2. **Digest-bound original recovery context and HMAC fresh-process handoff (`review_sensei.recovery_context`, `review_sensei_github.publication_context_recovery`, G17):**
   - Introduce `OriginalRecoveryContextBundle` (`original-recovery-context-v1`, bounded to `8,192` UTF-8 bytes) carrying SHA-256 digests of original configuration, evidence, convergence/work/decision policies, provider identity, selected context, learnings, optional verification baseline, and diff, with zero raw diff or prompt storage.
   - Persist fresh-process handoff envelopes via `RecoveryContextStore` (`0600` atomic temp-file + rename, HMAC-SHA256 integrity tag, bounded TTL, `8` files / `65,536` total bytes) and price read-phase recovery within `60` ordinary reads (`4` reserved fence reads) and the original API request allowance.
   - Refuse #431/#436 read-only action-result observations and `caller-requests-v3` reader-only state when supplied as recovery or writer authority, returning deterministic `RecoveryPrescription` guidance.

3. **Pre-effect terminal representability admission (`review_sensei.terminal_representability`, `review_sensei_github.action_representability`, G34):**
   - Preserve any authenticated `disposition_snapshot` on the sandbox record inside `preview_session_command_representability` and serve pre-loaded snapshot entries from `_InMemoryPreviewLedger.read_disposition_snapshot` with `0` extra `evidence_budget` physical calls (`shared_budget.calls == 0`), guaranteeing byte-for-byte parity between preview and `apply_session_command` across `recorded` and `already-recorded` receipts.
   - Price exact canonical bytes across every terminal carrier (`caller-action-result-v1` inline `8,192`B manifest, `10,240`B inline disposition array, `32,768`B `SessionRecord` root, `65,536`B session comment, maximum intermediate record/comment states, future lifecycle growth, ordinary evidence reads preserving `4` fence reads, and physical request headroom preserving `4` protected tail requests) before mutating business state, review bodies, check runs, approvals, or receipts.
   - Keep `caller-requests-v1` and `caller-requests-v2` inline-only (`8,192`B ceiling) and `caller-requests-v3` reader-only by default. When an admitted action exceeds a carrier ceiling, fail closed pre-effect and emit a representable terminal refusal receipt (`explicit_noop`) with deterministic recovery steps while preserving all authenticated snapshots and obligations.

4. **Combined capacity inventory and cross-stage interruption qualification (`review_sensei.combined_capacity`, G12):**
   - Maintain a runtime-verified `REPRESENTATION_LIMIT_INVENTORY` across `session_root`, `bounded_evidence`, `recovery_context`, `terminal_action`, and `publication_and_api`, paired with UTF-8 byte vs. codepoint accounting (`measure_utf8_text`, rejecting NULs and lone surrogates), complete retained `ReviewResult` + `SessionRecord` decomposition (`decompose_session_and_result_footprint`, `estimate_lifecycle_future_growth_bytes`, `evaluate_live_session_envelope`), and strict fail-closed validation on malformed `api_run_accounting`.
   - Keep all nine interruption-stage capacity scenarios as estimates, not evidence that a native operation was replayed. `simulate_interruption_and_fresh_replay` models outcomes; its optional private-context roundtrip uses separate instances in the same process.
   - `qualify_retained_interruption_replay` requires an existing retained ledger directory to qualify anything. A separate Python interpreter loads the exact expected session root and authenticates every referenced disposition snapshot part through `read_session_dispositions`, under the unchanged physical read budget. Missing/corrupt parts, a changed root, an unavailable process or timeout refuse qualification. The qualifier never manufactures retained roots, keys, policy digests, provider identities or original contexts.
   - Report `retained_artifacts_required`, `capacity_refused`, `root_verified` or `snapshot_graph_verified`, exact `qualified_components` and observed `physical_reads`. Legacy booleans are conservative projections. Native-effect/inference counters are null because these operations were not exercised; `fresh_handoff_verified` is false. Retained results, archives, recovery contexts, publication, terminal accounting and historical settlement remain explicitly unqualified, even when their capacity estimates fit. Existing unknown-charge estimates are retained, not presented as an observed original-budget recovery receipt.

## Scope

Core provenance, recovery-context, representability, and combined-capacity contracts (`review_sensei.provenance_manifest`, `review_sensei.recovery_context`, `review_sensei.terminal_representability`, `review_sensei.combined_capacity`) and hosted action/publication bridges (`review_sensei_github.publication_context_recovery`, `review_sensei_github.action_representability`, `action_run`, `action_reply`, `application`). Existing public JSON wire schemas, `caller-requests-v3` writer non-activation, workflow pin policy, and broker routes remain unchanged.

## Consequences

- Multi-component session growth, multibyte UTF-8 expansion, and `caller-action-result-v1` manifest overflows are caught pre-effect with representable refusal receipts rather than failing mid-commit after external mutations.
- Fresh-process publication replay validates full original context digests without storing raw diffs or prompts, and prescribes `@reviewsensei rescan` or explicit original-document supply when runtime-only or baseline state drifted.
- Read-only #431/#436 observations, diagnostic packets, and `caller-requests-v3` reader artifacts remain strictly non-authoritative (`authority: false`).

## Alternatives considered

- **Relying solely on per-component byte caps:** Rejected because valid `10,240`B dispositions, `12,288`B history, `8,192`B queue roots, `4,096`B staging, and `8,192`B manifests exceed `32,768`B when combined, and `38` partial-coverage entries (`10,195`B inline dispositions) overflow the `8,192`B inline action-result manifest.
- **Activating `caller-requests-v3` partitioned action-result writers immediately:** Rejected for this prospective boundary; `v3` remains reader-only until dedicated writer fencing and rollout qualification are completed.
- **Storing raw diffs or prompts in recovery handoff files:** Rejected to preserve strict secret/source data minimization.

## Validation

Deterministic unit and integration suites (`tests/test_provenance_manifest_g11.py`, `tests/test_recovery_context_g17.py`, `tests/test_terminal_representability_g34.py`, `tests/test_combined_capacity_g12.py`) verify sanitization, non-substitutable gates, annotated-tag vs. peeled-commit and `v0.8.20` release vs. `"unknown"` runtime separation, exact-main CI `CreateArtifact` 5xx observation handling, digest-bound fresh-process handoff, exact 2-decision `disposition_snapshot` preview/actual parity (`9,354`B / `9,578`B carrier, `0` extra `evidence_budget` calls) alongside the preserved 39-entry historical component witness (`10,195`B dispositions, `20,933`B envelope, `9,750`B / `9,690`B carrier vs. `8,192`B, without claiming live post-effect loss), exact `N-1`/`N`/`N+1` capacity boundaries (`8,192`, `10,240`, `32,768`, `65,536`, `60`/`64`, `512`), 1/2/3/4-byte UTF-8 accounting, lost-response command writes before and after server storage, and zero-inference publication replay alongside existing lifecycle regression suites.

## Rollout and rollback

Ships through normal source review and CI with no schema migration, workflow change, or broker deployment dependency. Rollback reverts pre-effect admission helpers and diagnostic modules without invalidating existing `SessionRecord` or ledger documents.

## Follow-up work

Qualify partitioned `caller-requests-v3` writer activation and automated multi-part compaction independently once cross-writer fencing and historical settlement rules are established.

### Narrow snapshot qualification correction (PR #452)

The prior root-only roundtrip compared snapshot manifests without opening their
parts and generated synthetic recovery context. Its positive preservation and
zero-effect fields were not full-graph replay evidence. The replacement preserves
the capacity API while deliberately returning unknown counters and explicit
qualification scope. Callers must tolerate null counters and inspect status
rather than infer successful recovery from a fitting envelope.

Validation uses production snapshot writers and a fresh interpreter, including
missing/corrupt part and changed-root controls. Read counts belong to the
read-only qualification attempt, not a reconstructed historical caller allowance.
No publication, provider call, mutation, schema migration or live cutover is
performed. Rollback must retain this evidence limitation; reverting executable
qualification must not restore unsupported success claims. Broader graph and
ordinary-route recovery qualification remains outstanding.

The snapshot probe starts with isolated Python import handling (`-I -S`), pins
the parent package origin, and checks probe/public-reader source fingerprints
before accepting evidence. Cwd and inherited `PYTHONPATH` cannot select a shadow
reader. Stdout is drained with a 1,025-byte hard retention bound (excess kills
the child); stderr goes directly to the OS discard sink. The existing deadline
remains in force. This is scoped same-installation reader evidence, not a
claim of whole-environment attestation or broader recovery qualification.

### Explicit snapshot preview capability

The preview no longer discovers private ledger caches, comment maps, or local
paths. `snapshot_preview_parts(identity, record)` is an optional, pure adapter
capability: it authenticates the exact v1/v2 graph from already-cached bytes,
including paged index and page references, and returns detached referenced parts.
`None` means unavailable, not zero-cost evidence. A missing capability cannot
supply partitioned preview evidence; inline v1 remains self-contained.

`hydrate_snapshot_preview` is a separate normal read-phase operation using
`read_session_dispositions` and the production bounded, authenticated readers.
It charges actual physical work through the existing shared budget. The hosted
command guard hydrates before pure preview and computes pricing from the updated
caller accounting; a conservative remaining-GET allowance is owned by the adapter
and includes v2 pages. This allowance is not a measured count: quarantine startup
bytes may satisfy subsequent reads even when ordinary listed-part reuse is off.
No earlier physical charge is refunded or reset. Pure previews neither stat/open files nor issue HTTP nor mutate source
caches. A cached proof does not authorize live recovery or replace a later
required fresh read/fence. Unsupported adapters refuse until they implement the
capability; no writer encoding, limit, durable schema or deployment changes.

### Invocation evidence and qualification scope (G11, prospective correction)

The optional `invocation_evidence` extension reuses the existing main/PR source,
installed-version, reusable tag-object/peeled-commit, executor, policy and broker
identities. It adds only missing joins: each release artifact's version, source
commit and SHA-256; observed imported-module ownership and initializer-file
SHA-256; caller tag object (or explicit direct-commit `not_applicable`), commit,
workflow bytes and trust observation; reusable workflow bytes and signature
observation; separate Worker revision, activated-registry digest, observation
scope, invocation digest and observation time. An absent extension preserves the
existing v1 wire body and digest. Missing observations stay `unknown`; there is
no release-label, current-main, CI or version-metadata fallback.

`inspect_current_process_packages` is opt-in local observation. It examines only
already-imported modules, their installed distribution's initializer inventory
and this interpreter's prefix. It imports no candidate package, makes no network
request, reads at most 1 MiB plus one overflow byte per initializer and returns no local paths or raw
file bytes. Special files are refused before opening and by descriptor checks;
nonblocking/no-follow open flags are used where supported. Unloaded, ambiguous,
missing or editable origins stay unknown or are
explicitly outside the installed distribution. A file hash observes disk bytes,
not in-memory code, all package contents or the downloaded wheel. Wheel identity
must come from separate authenticated release evidence. Automatic legacy version
inspection now refuses identified PR/head/event or retained-invocation manifests
rather than filling historical unknowns from today's environment.

`QualificationReceiptBinding` associates an existing receipt digest with the
exact manifest, invocation and route digests. The route covers source, both
artifacts, module observations, caller/reusable workflow identities and bytes,
executor, policy, broker, Worker and activated registry. The stronger comparator
requires all observed and expected fields, matching artifact/installed versions,
distribution-owned imports, supplied successful workflow-trust observations and
a passing receipt scoped to that exact route. Missing evidence refuses a complete
match; contradictory evidence is reported separately. Legacy `compatible` and
`has_complete_runtime_identity` remain declared-metadata observations only.

A `scope_matched` report is **not** signature verification, receipt authentication,
runtime qualification, deployment proof or authority. The diagnostic module
cannot authenticate the producer of supplied evidence; existing signature,
release-provenance and runtime verifiers still own that work. Its serialized
qualification-authenticity, runtime-qualification-proof, deployment-proof and
authority flags are always false. The fixed 16 KiB attempt-packet limit is
unchanged. No ordinary workflow telemetry producer, deployed invocation capture,
Worker deployment, registry activation or historical reconstruction is added.
G11 therefore remains deployment-evidence incomplete.

Validation includes source-only and mixed-version controls, tag/commit/bytes and
signature mismatches, wrong import origins, broker/Worker/policy/registry drift,
missing fields, wrong receipt scope, legacy-digest preservation and an offline
fixture-wheel installation in a fresh interpreter outside the checkout. The
fixture is not a published release or live-runtime qualification.
