# Runtime and source provenance manifest

`review_sensei.provenance_manifest` provides an immutable, sanitized contract for
recording repository source identities separately from installed runtime versions,
workflow tag peeling, broker enforcement, and per-attempt recovery evidence (G11).

## Core distinctions

When inspecting or reproducing an execution attempt, keep the following layers
strictly distinct:

- **Repository source baseline (`main_sha`) vs. executing workflow commit:**
  `main_sha` records an inspected branch commit only (`main_sha_is_runtime_proof:
  false`). It is never proof of the installed Python wheel, hosted adapter, or
  deployed Worker revision.
- **Annotated workflow tag vs. peeled commit:** `reusable_workflow_tag_object`
  records the Git tag object SHA (when an annotated tag such as `v5` is used),
  whereas `reusable_workflow_peeled_commit` records the peeled commit SHA that
  actually supplied the reusable workflow definition.
- **Installed core vs. hosted adapter:** `installed_core_version`
  (`review-sensei`) and `installed_hosted_version` (`review-sensei-github`) are
  recorded from explicit runtime metadata, never inferred from `pyproject.toml`
  in a checkout.
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
  session comment (`MAX_SESSION_COMMENT_BYTES`), and remaining physical API
  request headroom (preserving `4` protected tail requests).
- Keeps `caller-requests-v1` and `caller-requests-v2` strictly inline (`8,192`B)
  and `caller-requests-v3` reader-only by default. When a carrier would overflow
  (for example, `38` partial-coverage entries fitting inside `10,240`B inline
  dispositions while producing a `9,750`B `caller-action-result-v1` carrier),
  admission fails closed pre-effect and records a compact terminal refusal
  receipt with deterministic recovery guidance.

## Combined capacity inventory and cross-stage interruption envelope (G12)

`review_sensei.combined_capacity` inventories all `41` representation limits
across `session_root`, `bounded_evidence`, `recovery_context`,
`terminal_action`, and `publication_and_api`, cross-verifying them against live
runtime constants (`verify_inventory_against_runtime_constants`):

- Measures exact UTF-8 byte expansion vs. Unicode codepoint length across 1-,
  2-, 3-, and 4-byte sequences (`measure_utf8_text`) while rejecting NUL bytes
  and lone UTF-16 surrogates.
- Detects simultaneous multi-component overflows where every individual field
  fits its own sub-cap yet the combined `SessionRecord` (`> 32,768`B), rendered
  session comment (`> 65,536`B), published review body (`> 65,536`B), stored part
  pool (`> 256` parts / `8` MiB), ordinary evidence reads (`> 60`), or physical
  API request budget (`> original_api_limit`) would overflow.
- Qualifies interruption and fresh-process replay invariants across all nine
  retained, publication, and terminal stages (`before_dispatch`,
  `in_flight_not_stored`, `in_flight_stored_response_lost`, and
  `definite_rejection`), retaining unknown charges, ignoring unreferenced orphan
  parts, reconciling lost-response writes via owned readback without duplicate
  effects, and preserving `authority: false` on all evaluations.
