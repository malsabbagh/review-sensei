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
   `OPENROUTER_UPSTREAM_PROVIDER` (initially `deepseek`) for the routing policy.
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
wiring (#98/#112), setup `REVIEWSENSEI_PROVIDER_MODE` and `REVIEWSENSEI_MODEL`
hosted selection, docs,
and offline tests.

Out of scope: live qualification/promotion (O4), arbitrary endpoint overrides,
and catalog scraping.

### Hosted workflow contract (#99)

The reusable workflow selects one backend from `provider_mode`:
`local-ollama`, `cloud-ollama`, or `openrouter` (`local` and `cloud` are
aliases). Model selection is `REVIEWSENSEI_MODEL` forwarded as the `model`
input; each backend applies its own default when the variable is empty.
`provider_profile` and `allow_unqualified_profile` are unused and fail closed
when set. The previous hosted `allow_unqualified_profile=true` acknowledgement
is removed: selecting `REVIEWSENSEI_PROVIDER_MODE=openrouter` is the operator
opt-in to GitHub-hosted OpenRouter egress. Hosted OpenRouter uses
`--provider openrouter --model` against the published allowlist in
`provider_config.HOSTED_OPENROUTER_DEFAULTS` and derives
`OPENROUTER_UPSTREAM_PROVIDER` from that allowlist.

Callers forward `REVIEWSENSEI_PROVIDER_MODE`, `REVIEWSENSEI_MODEL`, and
`OPENROUTER_API_KEY` after the public `v4` tag includes this contract. The
`openrouter` job body mirrors the `cloud` job; `tests/test_ci_policy.py`
asserts provider-job parity so drift is caught in CI.

## Amendment (2026-09-17, PR #112)

Hosted OpenRouter validation resolves an empty `model` input to
`DEFAULT_OPENROUTER_MODEL` before allowlist checks in
`validate_hosted_workflow_model`. The reusable workflow validates the resolved
model in `validate-provider-mode` and again in the `openrouter` provider job
after applying the job default. Non-allowlisted models fail with an explicit
message that hosted runs do not forward `--allow-unqualified-profile`; operator
opt-in is `REVIEWSENSEI_PROVIDER_MODE=openrouter` plus a published allowlist
slug.

## Amendment - public channel `v5`

Callers forward `REVIEWSENSEI_PROVIDER_MODE`, `REVIEWSENSEI_MODEL`, and
`OPENROUTER_API_KEY` after the public `v5` tag includes this contract. `v4`
remains accepted during migration.

## Amendment (2026-09-23) - route the default model through Morph

The published `deepseek/deepseek-v4.1-flash` model returned HTTP 404 when pinned
to upstream `deepseek`: OpenRouter's data-policy filter removed that endpoint
under the existing `data_collection=deny`, ZDR, required-parameters, and
no-fallback contract. A live review with the same model and constraints passed
when pinned to upstream `morph`. Set the unprofiled CLI and hosted default
upstream to `morph`; keep the strict routing policy and model allowlist. A
future endpoint change must fail closed and be requalified before changing the
trusted upstream mapping.

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
