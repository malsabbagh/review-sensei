# ADR 0041 - Production provider adapters and per-stage profiles

Status: Proposed
Date: 2026-09-16
GitHub Issue: #42
Pull Request: draft PR to be linked
Owners/Reviewers: Maintainers
Approved by: not applicable

## Context

The review engine is provider-neutral. Main already includes Ollama, fixture, an
`openai-compatible` Chat Completions adapter, and named run profiles
(`local-private`, `fast-triage`, `deep-verification`). Remaining gaps were a
shared adapter conformance suite, explicit per-stage profile routing, recording
observable revisions, and documenting CLI/schema/workflow exposure without
changing default local Ollama usage or silently expanding cloud egress.

## Decision

Keep one production additional adapter (`openai-compatible`) behind the existing
registry. Named profiles remain immutable presets: one adapter, one endpoint,
credential-by-reference, timeout/output budgets, `json_object` structured
output, and `permitted_fallback: none`.

A stage may set canonical `provider_profile`. Unprofiled and `local-private`
runs cannot select a remote stage profile. A remote run may narrow a stage to
`local-private`. Routing never switches adapters or forwards one profile's
credential to another endpoint. Per-stage models stay inside a profile's
closed allowlist.

`ReviewService` applies `#36` resource budgets across stages and structural
retries without model escalation. `#33` promotion records remain required for
profile eligibility; `validate_profile_promotion` rejects fixture-only
evidence. Installed GitHub workflows keep local/cloud Ollama modes and do not
gain a `--profile` input in this change.

## Scope

In scope: openai-compatible as the first additional production adapter,
conformance tests, per-stage profile routing, CLI/evaluate egress binding,
schema/docs, proposed ADR.

Out of scope: automatic provider racing, hidden cloud failover, a second review
engine, additional vendor adapters, reusable-workflow input expansion (#30),
full run-outcome persistence (#36 remainder), and marking this ADR Accepted.

## Alternatives considered

### Independent remote profile per stage on a local run

Rejected because it would expand cloud egress without a separate run-level
authorization.

### Adding a second cloud vendor adapter in this slice

Rejected until the openai-compatible seam and conformance suite are proven.

## Consequences

Positive:

- Operators can pin a stage to a named profile without duplicating review logic.
- Local/private policy cannot silently fail over to OpenAI or Ollama Cloud.
- Shared tests encode timeout, rate-limit, malformed-output, and secret-redaction
  contracts for all built-in adapters.

Tradeoffs:

- Mixing `fast-triage` and `deep-verification` in one run is rejected because
  they use different adapters and credentials.
- Workflows still use `REVIEWSENSEI_PROVIDER_MODE` rather than `--profile`.

## Validation and rollback

Run the provider conformance, routing, registry, CLI, service, and promotion
tests. Roll back by reverting this change; no persisted data migration is
required. Default Ollama usage without `--profile` remains unchanged.
