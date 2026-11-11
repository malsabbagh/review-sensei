# ADR 0131: Qualify provenance manifests, recovery context, pre-effect representability, and combined capacity envelopes

- Status: Proposed
- Date: 2026-11-11
- Decision owner/reviewers: ReviewSensei maintainers; independent source review
- Linked GitHub Issue: pending; lifecycle gap closure Batch 1 (G11, G17, G34, G12)
- Linked PR: pending; independent prospective correction

## Context

Four coupled lifecycle boundaries previously lacked explicit cross-stage admission and capacity contracts:

1. **Runtime vs. source provenance (G11):** Inspecting a repository `main` commit does not prove which peeled reusable-workflow commit, installed `review-sensei` / `review-sensei-github` package version, frozen policy snapshot, or broker enforcement route ran in an attempt, nor can routing preflight substitute for authenticated live validation or broker capability enforcement.
2. **Original recovery context across fresh processes (G17):** Replaying retained review results without fresh model inference requires verifying exact digests of the original configuration, admission, convergence, work, and decision policies, selected context, learnings, verification baseline, and diff. Default checkout reconstruction fails when runtime-only inputs or overwritten baselines were involved, while storing raw diffs or prompts would violate data-minimization rules.
3. **Terminal result representability vs. pre-effect admission (G34):** A maintainer action such as `accept-coverage-uncertainty` can pass command-body, 10,240-byte inline disposition, and 32,768-byte `SessionRecord` checks yet still overflow the 8,192-byte inline `caller-action-result-v1` manifest ceiling inside `api_run_accounting` after business side effects have been staged.
4. **Combined capacity and cross-stage interruption envelope (G12):** Individual sub-caps (`10,240`B inline dispositions, `12,288`B convergence history, `8,192`B assessment queue root, `4,096`B publication staging, `8,192`B inline manifests) sum to more than the `32,768`-byte `SessionRecord` root, while consumed-grant markers, multibyte UTF-8 expansion, cumulative stored parts, and ordinary part reads can exhaust the `65,536`-byte comment/review limits, the `60`-call ordinary evidence read limit (`4` reserved fence reads), or the physical API request allowance.

## Decision

1. **Sanitized provenance and attempt evidence (`review_sensei.provenance_manifest`, G11):**
   - Record repository `main_sha` (`main_sha_is_runtime_proof: false`), annotated workflow tag object vs. peeled workflow commit, installed core and hosted package versions, frozen policy commit/digest, reviewed PR base/head/event, broker route/revision, and separate `GateObservations` (`routing_preflight`, `authenticated_live_validation`, `broker_enforcement`) without performing network probes when telemetry is `"unknown"`.
   - Separate nine lifecycle receipt stages (`intent` through `terminal_accounting`), preserve conservative request debits and unknown usage charges, reject credentials and raw diff lines, and enforce `authority: false` on every diagnostic artifact.

2. **Digest-bound original recovery context and HMAC fresh-process handoff (`review_sensei.recovery_context`, `review_sensei_github.publication_context_recovery`, G17):**
   - Introduce `OriginalRecoveryContextBundle` (`original-recovery-context-v1`, bounded to `8,192` UTF-8 bytes) carrying SHA-256 digests of original configuration, evidence, convergence/work/decision policies, provider identity, selected context, learnings, optional verification baseline, and diff, with zero raw diff or prompt storage.
   - Persist fresh-process handoff envelopes via `RecoveryContextStore` (`0600` atomic temp-file + rename, HMAC-SHA256 integrity tag, bounded TTL, `8` files / `65,536` total bytes) and price read-phase recovery within `60` ordinary reads (`4` reserved fence reads) and the original API request allowance.
   - Refuse #431/#436 read-only action-result observations and `caller-requests-v3` reader-only state when supplied as recovery or writer authority, returning deterministic `RecoveryPrescription` guidance.

3. **Pre-effect terminal representability admission (`review_sensei.terminal_representability`, `review_sensei_github.action_representability`, G34):**
   - Price exact canonical bytes across every terminal carrier (`caller-action-result-v1` inline `8,192`B manifest, `10,240`B inline disposition array, `32,768`B `SessionRecord` root, `65,536`B session comment, and remaining request headroom including `4` protected tail requests) via sandbox preview before mutating business state, review bodies, check runs, approvals, or receipts.
   - Keep `caller-requests-v1` and `caller-requests-v2` inline-only (`8,192`B ceiling) and `caller-requests-v3` reader-only by default. When an admitted action exceeds a carrier ceiling, fail closed pre-effect and emit a representable terminal refusal receipt with deterministic recovery steps.

4. **Combined capacity inventory and cross-stage interruption qualification (`review_sensei.combined_capacity`, G12):**
   - Maintain a runtime-verified `REPRESENTATION_LIMIT_INVENTORY` across `session_root`, `bounded_evidence`, `recovery_context`, `terminal_action`, and `publication_and_api`, paired with UTF-8 byte vs. codepoint accounting (`measure_utf8_text`, rejecting NULs and lone surrogates) and simultaneous footprint evaluation (`evaluate_combined_capacity`, `evaluate_live_session_envelope`).
   - Qualify interruption and fresh-process replay invariants across all nine retained, publication, and terminal stages (`before_dispatch`, `in_flight_not_stored`, `in_flight_stored_response_lost`, `definite_rejection`), preserving unknown request charges, rejecting unreferenced orphan parts, reconciling stored lost-response writes via owned readback without duplicate side effects, and replaying retained results with zero fresh inference.

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

Deterministic unit and integration suites (`tests/test_provenance_manifest_g11.py`, `tests/test_recovery_context_g17.py`, `tests/test_terminal_representability_g34.py`, `tests/test_combined_capacity_g12.py`) verify sanitization, non-substitutable gates, digest-bound fresh-process handoff, the 38-file coverage witness (`10,195`B dispositions vs. `9,750`B carrier manifest), six-axis "each component fits individually yet combined envelope fails" boundaries, 1/2/3/4-byte UTF-8 accounting, lost-response command writes before and after server storage, and zero-inference publication replay alongside existing lifecycle regression suites.

## Rollout and rollback

Ships through normal source review and CI with no schema migration, workflow change, or broker deployment dependency. Rollback reverts pre-effect admission helpers and diagnostic modules without invalidating existing `SessionRecord` or ledger documents.

## Follow-up work

Qualify partitioned `caller-requests-v3` writer activation and automated multi-part compaction independently once cross-writer fencing and historical settlement rules are established.
