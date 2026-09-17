# ADR 0045 - OpenRouter trusted configuration and CLI

Status: Proposed
Date: 2026-09-16
GitHub Issue: #97
Pull Request: draft PR to be linked
Owners/Reviewers: Maintainers
Approved by: not applicable

## Context

ADR 0041 established named provider profiles, per-stage routing, and the
`openai-compatible` adapter. Issue #96 added an explicit OpenRouter adapter with
a typed `OpenRouterRoutingPolicy` (upstream provider slug, no fallbacks, deny
data collection, ZDR). Operators still need a trusted configuration path that
exposes OpenRouter through the same CLI, profile, doctor, and plan contracts
without changing default local Ollama behavior or workflow inputs.

## Decision

Expose `--provider openrouter` and named OpenRouter profiles
(`openrouter-sonnet`, `openrouter-gpt`) through the existing configuration
resolver. `OPENROUTER_API_KEY` is resolved only at the CLI boundary; help text,
offline doctor/plan, and fixture mode never require or print the key.

`ProviderProfile` carries an optional `OpenRouterRoutingPolicy` and a
`qualification_status` (`qualified` or `unqualified`). New OpenRouter profiles
start `unqualified` until separate qualification evidence (issue O4) promotes
them. `ProviderSettings.for_profile()` and the registry pass
`openrouter_policy` from the profile without environment lookup.

Precedence remains deterministic:

1. `--profile` selects an immutable preset (provider, model, endpoint, budget,
   routing policy, credential env var).
2. Explicit CLI flags must match the profile or are rejected.
3. Unprofiled `--provider openrouter` uses allowlisted defaults and
   `OPENROUTER_UPSTREAM_PROVIDER` (default `anthropic`) for the routing policy.
4. `REVIEWSENSEI_PROVIDER_MODE=local|cloud` continues to map only to Ollama
   defaults; it is not reinterpreted as OpenRouter.

Doctor and plan report effective provider/model, execution location (always
local for the CLI process), inference location (loopback vs remote), credential
presence only, OpenRouter policy summary, and qualification status. Provider,
model, and routing-policy identity fields are included in
`configuration_digest` for evaluation and promotion evidence. Raw credentials are
never hashed.

## Scope

In scope: CLI `--provider openrouter`, OpenRouter profiles, registry wiring,
doctor/plan extensions, configuration digests, docs, and offline tests.

Out of scope: workflow/broker changes (#98), live qualification/promotion (O4),
arbitrary endpoint overrides, and catalog scraping.

## Alternatives considered

### Reusing `openai-compatible` with an OpenRouter base URL

Rejected because OpenRouter requires typed routing/privacy policy and a
dedicated allowlist; mixing vendors through the OpenAI adapter would hide policy
boundaries.

### Making `REVIEWSENSEI_PROVIDER_MODE=cloud` select OpenRouter

Rejected to preserve the existing Ollama Cloud contract documented in
installation and workflow examples.

## Consequences

Positive:

- Operators can select OpenRouter deliberately with visible egress and policy.
- Local Ollama defaults and existing profiles remain unchanged.
- Unqualified profiles cannot pass `validate_profile_promotion`.

Tradeoffs:

- Unprofiled OpenRouter requires `OPENROUTER_UPSTREAM_PROVIDER` discipline.
- Doctor does not probe OpenRouter inventory without a dedicated read-only API.

## Validation and rollback

Run `tests.test_openrouter`, `tests.test_cli`, `tests.test_diagnostics_and_patches`,
and `tests.test_registry`. Roll back by removing OpenRouter profiles and CLI
branches; the #96 adapter can remain registered without being selectable.
