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
`openrouter_policy` from the profile without environment lookup. For
unprofiled `--provider openrouter`, the CLI resolves routing policy from
`OPENROUTER_UPSTREAM_PROVIDER` and `ProviderRegistry.create` rejects settings
whose explicit policy disagrees with that env-derived default.

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
doctor/plan extensions, configuration digests, reusable-workflow OpenRouter job
wiring (#98), setup `REVIEWSENSEI_PROVIDER_PROFILE` variable/config parity, docs,
and offline tests.

Out of scope: live qualification/promotion (O4), arbitrary endpoint overrides,
and catalog scraping.

### Hosted workflow contract (#98)

The reusable workflow adds an `openrouter` job that runs only when
`provider_mode == 'cloud'` and `provider_profile` is `openrouter-sonnet` or
`openrouter-gpt`. This preserves ADR 0011/0025 egress opt-in: a profile value
alone cannot move reviews onto GitHub-hosted compute. The job shares the
`reviewsensei-provider-*` concurrency group with cloud/local provider jobs so a
newer review for the same pull request cancels any in-flight provider-mode
review.

Hosted OpenRouter review and mention-reply commands forward
`--allow-unqualified-profile` because the workflow is operator-controlled and
profiles remain `unqualified` until separate qualification evidence exists.
Caller workflows forward `provider_profile` and `OPENROUTER_API_KEY` only after
the public `v4` tag includes the reusable-workflow contract.
`validate-provider-mode` rejects unsupported profile values and requires
`provider_mode=cloud` whenever a profile is set.

The `openrouter` job body intentionally mirrors the `cloud` job for #98.
ADR 0025's parameterized-job ideal remains the follow-up; until then
`tests/test_ci_policy.py` asserts provider-job parity (reply parsing,
concurrency groups, publish/reply counts) so drift is caught in CI.

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
