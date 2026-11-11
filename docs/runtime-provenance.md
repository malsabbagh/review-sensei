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
