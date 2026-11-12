# Runtime and source provenance manifest

`review_sensei.provenance_manifest` provides an immutable, sanitized contract for
recording repository source identities separately from installed runtime versions,
workflow tag peeling, broker enforcement, and per-attempt recovery evidence (G11).

## Core distinctions

When inspecting or reproducing an execution attempt, keep the following layers
strictly distinct (`build_provenance_manifest` and `ProvenanceManifest`):

- **Repository source baseline (`main_sha`) vs. executing workflow commit:**
  `main_sha` records an inspected branch commit only (`main_sha_is_runtime_proof:
  false`). It is never proof of the installed Python wheel, hosted adapter,
  consumer caller workflow, or deployed Worker revision.
- **Annotated workflow tag vs. peeled commit:** `reusable_workflow_tag_object`
  records the Git tag object SHA (when an annotated tag such as `v5` is used),
  whereas `reusable_workflow_peeled_commit` records the peeled commit SHA that
  actually supplied the reusable workflow definition. `build_provenance_manifest`
  (`require_distinct_annotated_tag=True`) and
  `classify_provenance_compatibility` (`require_annotated_tag_peel=True`) reject
  conflating an annotated tag object SHA with its peeled commit SHA.
- **Published GitHub release vs. deployed runtime:** `latest_release_tag`,
  `latest_release_tag_object`, and `latest_release_commit` record published
  release metadata (`release_is_deployment_proof: false`). A published release
  never populates or proves `installed_core_version`,
  `installed_hosted_version`, `selected_executor`, `broker_revision`, or
  consumer workflow adoption.
- **Timestamped CI run observations (`CiRunObservation`):** `ci_observations`
  records dated commit-level workflow runs (`ci_status_is_deployment_proof:
  false`, `is_deployment_proof: false`, `authority: false`), distinguishing
  code/gate failures from transient infrastructure errors such as
  `transient_artifact_upload_5xx` without inferring deployment from a green run
  or dispatching duplicate `main` runs.
- **Installed core vs. hosted adapter:** `installed_core_version`
  (`review-sensei`) and `installed_hosted_version` (`review-sensei-github`) are
  recorded from explicit runtime metadata (`auto_inspect_local_packages=False`
  by default so remote snapshots never read local developer environment
  metadata), never inferred from `pyproject.toml` in a checkout.
- **Frozen policy commit vs. reviewed PR snapshot:** `frozen_policy_commit` and
  `frozen_policy_sha256` identify the default-branch policy snapshot, kept
  distinct from `base_ref`, `base_sha`, `head_sha`, and `source_event`.
- **Three non-substitutable validation gates (`GateObservations`):**
  - `routing_preflight` checks workflow input syntax only; passing routing
    preflight never proves live PR state or broker enforcement.
  - `authenticated_live_validation` records live PR head/base/draft/fork
    validation at bootstrap; it does not prove broker capability enforcement or
    terminal accounting.
  - `broker_enforcement` records OIDC/session-grant or token enforcement at
    `broker_route` (`/github/token`, `/github/session-grant`, or `none`) and
    `broker_revision`.

### Dated repository provenance snapshot (2026-11-11)

The reference provenance snapshot recorded on `2026-11-11T23:06:17Z` keeps all
identities separate without claiming live consumer or broker deployment:

| Layer | Field(s) | Observed value (`2026-11-11`) |
|---|---|---|
| Repository `main` head | `main_sha` | `07179c0f2bd6b761a95449ce3ce29d4633c80094` (`main_sha_is_runtime_proof: false`) |
| Caller workflow | `caller_workflow` | `.github/workflows/review-sensei-review.yml@refs/heads/main` |
| Reusable workflow ref & annotated tag | `reusable_workflow_ref` / `reusable_workflow_tag_object` | `malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@v5` -> tag object `a72527097205110337a6841a545f548610c7fbaa` |
| Reusable workflow peeled commit | `reusable_workflow_peeled_commit` | `d626185fff387dab14aedd48b2b0684e92c84739` |
| Latest GitHub release | `latest_release_tag` / `latest_release_tag_object` / `latest_release_commit` | `v0.8.20` -> tag object `ffaf25468dbf682e43cb09bbfb047b6bd86c393e` -> commit `a4c605c51162dd37dc1b938e6e77fb535ff6a566` (`release_is_deployment_proof: false`) |
| Exact-main CI observations | `ci_observations` | Run `43094461694` (`CI` on `07179c0f...`): `failure` (`transient_artifact_upload_5xx` on GitHub Actions `CreateArtifact` HTML 5xx upload steps `Retain installed suite inventory`, `Archive CodeQL SARIF`, `Retain checkout inventory`); Run `43094456791` (`Push on main` on `07179c0f...`): `success` (`ci_status_is_deployment_proof: false`) |
| Installed consumer & deployed broker runtime | `installed_core_version`, `installed_hosted_version`, `selected_executor`, `broker_route`, `broker_revision` | `"unknown"` (unproven without runtime telemetry) |

## Unavailable telemetry and sanitization

- Unavailable provenance fields are represented explicitly as `"unknown"`.
- Building or comparing a `ProvenanceManifest` or `AttemptEvidencePacket` never
  triggers network probes, retries, token issuance, or deployment lookups to
  fill missing fields.
- Every field is validated against closed status vocabularies or bounded syntax
  and rejects control characters, credential-like tokens (`Bearer`, GitHub PATs,
  API keys, PEM headers, URL userinfo), and raw unified diff lines.
- Every manifest, compatibility report, recovery prescription, and evidence
  packet enforces `authority: false`: diagnostics never confer execution,
  publication, replay, or settlement authority.

## Separate lifecycle receipts and bounded recovery

`AttemptEvidencePacket` tracks nine distinct lifecycle receipts (`intent`,
`execution_started`, `provider_request_result`, `retained_result_parts`,
`publication`, `check`, `thread_cleanup`, `native_approval`, and
`terminal_accounting`) alongside conservative request accounting
(`reserved_requests`, `consumed_requests`, `unknown_usage_requests`, and
`protected_tail_requests`) and uncertain dispatch readbacks.

Every classified outcome carries a deterministic `RecoveryPrescription` that
preserves existing finding obligations, reservations, and unknown usage charges:

| Condition | Prescribed action | Command / route |
|---|---|---|
| Retained accepted result on matching provenance with unclosed publication/finalization | `replay_retained_result` | `supply-original-recovery-context` (zero fresh inference) |
| Retained partial coverage on matching snapshot within original budget | `review_remaining` | `@reviewsensei review remaining` |
| Published + terminal accounting settled, convergence recheck needed | `verify_convergence` | `@reviewsensei verify` (zero inference) |
| Workflow, package, broker, or policy provenance mismatch | `rescan_under_pinned_provenance` | `@reviewsensei rescan` after predecessor accounting is settled |
| Missing original admission/context evidence | `supply_original_context_or_rescan` | `supply-original-recovery-context` or `@reviewsensei rescan` |
| Expired session marker with no active quarantine hold | `controlled_reenrollment` | `controlled-reenroll` |
| Unresolved uncertain dispatch or legacy state | `reconcile_owned_readback_then_hold_or_dispose` | `reconcile-owned-readback` before `@reviewsensei verify` or human disposition |

## Digest-bound original recovery context and fresh-process handoff (G17)

`review_sensei.recovery_context` (`OriginalRecoveryContextBundle` and
`RecoveryContextStore`) and `review_sensei_github.publication_context_recovery`
retain the exact digest census required to replay a retained `ReviewResult` in a
fresh process with zero model inference:

- Bounds each canonical bundle (`original-recovery-context-v1`) to `8,192` UTF-8
  bytes and fresh-process HMAC-SHA256 handoff storage to `8` files / `65,536`
  aggregate bytes with `0600` permissions and zero raw diff or prompt payloads.
- Prices read-phase recovery against the `60`-call ordinary evidence read budget
  (preserving `4` final fence reads out of `64`) and the original API request
  allowance.
- Distinguishes default-reconstructible checkout inputs from runtime-only or
  overwritten verification baselines, and rejects read-only `#431`/`#436`
  observations and reader-only `caller-requests-v3` documents as recovery
  authority (`authority: false`).

## Pre-effect terminal representability admission (G34)

`review_sensei.terminal_representability` and
`review_sensei_github.action_representability` price every maintainer and review
terminal carrier *before* staging business mutations or external GitHub writes:

- Evaluates exact canonical UTF-8 sizes across the `8,192`-byte inline
  `caller-action-result-v1` manifest (`MAX_MANIFEST_BYTES`), the `10,240`-byte
  inline disposition array (`MAX_DISPOSITION_BYTES`), the `32,768`-byte
  `SessionRecord` root (`MAX_SESSION_RECORD_BYTES`), the `65,536`-byte GitHub
  session comment (`MAX_SESSION_COMMENT_BYTES`), maximum intermediate
  record/comment peaks, future lifecycle growth, and remaining physical API and
  evidence-read headroom (preserving `4` protected tail requests and `4`
  reserved fence reads).
- Preserves any authenticated `disposition_snapshot` on the sandbox record in
  `preview_session_command_representability` and serves pre-loaded snapshot
  entries from `_InMemoryPreviewLedger.read_disposition_snapshot` with `0` extra
  `evidence_budget` physical calls (`shared_budget.calls == 0`), eliminating the
  preview/actual mismatch where stripping `disposition_snapshot=None` on an
  authenticated 2-decision snapshot (`10,369`B `> 10,240`B) previously produced
  a `809`B `"recorded"` preview against a `9,354`B (`9,578`B 38-file variant)
  `"already-recorded"` `apply_session_command` result (`> 8,192`B).
- Keeps `caller-requests-v1` and `caller-requests-v2` strictly inline (`8,192`B)
  and `caller-requests-v3` reader-only by default. Preserves the older 39-entry
  (`38` 250B `file:` tokens + `1` mandatory 71B `result:` token) component
  witness (`10,195`B inline dispositions, `20,933`B `SessionRecord` envelope,
  `9,750`B / `9,690`B carrier vs. `8,192`B) as an independent historical
  component control; that component witness does not establish a live
  post-effect loss because earlier command-body and coverage-scope guards
  intercept single-command inline admissions before terminal settlement.

## Combined capacity inventory and cross-stage interruption envelope (G12)

`review_sensei.combined_capacity` inventories all `41` representation limits
across `session_root`, `bounded_evidence`, `recovery_context`,
`terminal_action`, and `publication_and_api`, cross-verifying them against live
runtime constants (`verify_inventory_against_runtime_constants`):

- Measures exact UTF-8 byte expansion vs. Unicode codepoint length across 1-,
  2-, 3-, and 4-byte sequences (`measure_utf8_text`) while rejecting NUL bytes
  and lone UTF-16 surrogates.
- Decomposes actual retained `ReviewResult` payloads and complete
  `SessionRecord` state (`decompose_session_and_result_footprint`,
  `evaluate_live_session_envelope`)—including retained `disposition_snapshot`
  part reads, `quarantined_state`, `historical_assessments`,
  `convergence_history`, `assessment_queue`, `publication_staging`,
  `api_run_accounting` (failing closed on malformed accounting payloads),
  maximum intermediate staging/replacement states
  (`max_intermediate_record_bytes`, `max_intermediate_comment_bytes`), and
  future lifecycle growth (`estimate_lifecycle_future_growth_bytes`)—detecting
  combined overflows across `32,768`B `SessionRecord`, `65,536`B session
  comment, `65,536`B published review body, `256` parts / `8` MiB part pool,
  `60` ordinary evidence reads (`4` reserved fence reads out of `64`), and
  physical API request allowances (`4` protected tail requests).
- Qualifies interruption and fresh-process replay invariants across all nine
  retained, publication, and terminal stages (`qualify_retained_interruption_replay`
  and `simulate_interruption_and_fresh_replay` across `before_dispatch`,
  `in_flight_not_stored`, `in_flight_stored_response_lost`, and
  `definite_rejection`), retaining prior and in-flight unknown charges, ignoring
  unreferenced orphan parts, verifying fresh-process `LocalSessionLedger` and
  `RecoveryContextStore` reloads without duplicate effects or fresh inference,
  and preserving `authority: false` on all evaluations.
