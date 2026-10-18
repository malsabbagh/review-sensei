# ADR 0081 - OpenRouter response metadata and routing identity

Status: Proposed
Date: 2026-10-18
GitHub Issues: #118, #119, #120, #121
Pull Request: pending
Owners/Reviewers: Maintainers
Approved by: not applicable

## Context

OpenRouter returned model aliases were previously recorded as revisions, hiding
reported fingerprints. Responses discarded upstream and usage metadata. The
trusted routing policy accepted provider families but rejected documented
slash-delimited variants. Hosted DeepSeek/Morph selection and qualification
eligibility had no overlap.

## Decision and scope

Keep `ProviderResponse.model` as the effective requested model for OpenRouter.
Add optional `observed_model`, `observed_upstream`, `revision_source`, and typed
`ProviderTokenUsage`. Only a reported `system_fingerprint` populates OpenRouter
`revision`, with source `provider-system-fingerprint`; it is a reported
fingerprint, not proof of an immutable model build. Existing Ollama, compatible,
and fixture semantics remain unchanged. Response metadata is non-authoritative:
it neither replaces operator attestations nor grants qualification or approval.

Optional identifiers use bounded ASCII identifier syntax and existing model and
revision byte limits, with a 128-byte upstream limit. Invalid or absent protocol
fields become unknown individually. Usage retains only `prompt_tokens`,
`completion_tokens`, and `total_tokens`: integers from 0 through 2^63-1,
excluding booleans. Invalid or missing counters become unknown. Nested usage,
reasoning, prices, credentials, and arbitrary provider objects are discarded.
Typed constructors reject invalid caller values. No enrichment request occurs.

Live evaluation adds optional `metrics.provider_reported_usage` to the existing
v1 report schema, following its additive-field policy. Each counter's aggregate
is known only when every attempted call reported it and its sum fits the same
bound. `complete` requires all three known totals and at least one attempt.
Missing reports, failures, truncation and cancellation remain attempts with
unknown usage. Existing byte-based proxies and durable work budgets keep their
separate meaning. Historical reports without this optional object still validate;
closed readers must update to the current packaged v1 schema before consuming
new reports. Tests explicitly show the old closed schema rejects new telemetry
and accepts the legacy shape, while the current schema accepts both. Retained
qualification evidence must not be rewritten as part of reader migration.
Fixture report output stays unchanged.

Routing accepts one through four normalized lowercase ASCII segments of at most
64 characters each, within the existing 128-byte total limit. Segments contain
letters, digits, underscores or hyphens and start with a letter or digit. Exact
input is serialized into `order` and identity digests. No suffix is stripped on
failure, no fallback occurs and privacy/parameter settings remain fixed.
Canonical `advanced.routing.upstream_provider` uses the same immutable policy
validator as the adapter. For models with a published upstream mapping, the
selected prefix must stay within that mapped family. Resolver, offline plan,
hosted plan and CLI retain the full prefix; the review CLI passes its resolved
policy explicitly into trusted `ProviderSettings`. The registry requires a typed
policy and preserves named-profile binding; it does not compare trusted settings
to ambient retired environment variables. Legacy `evaluate` continues resolving
its explicit environment-based policy at the CLI boundary. Existing unlisted
model configuration remains unqualified and keeps its prior explicit-routing
behavior.

A family or incomplete endpoint prefix can still match multiple eligible
endpoints; syntax acceptance does not establish endpoint existence or capability.

Add the existing hosted DeepSeek/Morph combination to qualification eligibility,
retaining historical Haiku/Anthropic eligibility for compatibility. Selectability,
eligibility and a passing qualification record remain distinct. Morph is a family
constraint, not a pinned deployment/region. Current endpoint capability and live
three-run qualification remain unverified under #122; no profile is promoted.

Live OpenRouter reports additionally retain optional v1
`run.openrouter_routing_policy_digest` and `run.openrouter_base_url` from the
actual requested adapter configuration. The checked qualification gate requires
both to match its independently supplied target for every live report. Missing
fields, a suffix/other upstream's policy digest, or a different endpoint cannot
mint or load support for the family target. The policy digest uses the same
canonical JSON algorithm as the existing routing identity; observations and
operator-attested revision/window/reproducibility retain their separate meanings.
Old reports remain structurally valid and their bytes must be preserved, but
unbound live evidence can no longer newly authorize support, including for the
still-selectable historical Haiku target. The report schema also admits the
producer's existing `skipped` case status, allowing live evaluation of packaged
contract-rejection cases to serialize without turning skipped tests into passes.

## Alternatives considered

A free-form metadata mapping was rejected because it could retain arbitrary
provider content. Replacing the existing response model meaning across all
adapters would break consumers; optional independent observations keep that
migration unnecessary. Deriving missing totals or retaining partial sums as run
totals would conceal unknown work. Relaxing privacy filters or switching the
hosted upstream merely to make target sets overlap would change the operator's
policy; eligibility reuses the existing Morph configuration instead.

## Validation

Synthetic adapter, conformance, evaluation, schema, CLI configuration and
installed-wheel tests cover independent identities; malformed metadata and
counters; partial, zero and unknown usage; attempted-call accounting; exact
routing; strict-policy failure; distinct digests; and target eligibility.
No live inference or API enrichment is authorized by this decision.

`QualificationRoutingEvidenceTests` covers matching historical/current synthetic
controls, missing/altered policy or endpoint fields, retained-byte rebinding and
excluded suffixes. The installed metadata contract exercises the actual CLI
against the packaged corpus through fake transport, checking serialized route
binding and fail-closed unbound evidence. Packaging remains an integration gate.

Official references checked 2026-10-18:

- [Response and usage fields](https://openrouter.ai/docs/api/reference/overview)
- [Endpoint routing semantics](https://openrouter.ai/docs/guides/routing/provider-selection#targeting-specific-provider-endpoints)
- [Current candidate catalog](https://openrouter.ai/deepseek/deepseek-v4.1-flash/providers)

The candidate catalog is a source to recheck during live qualification, not
retained capability evidence for the strict policy.

## Consequences, rollout and rollback

No persistence migration or credential change is required. Consumers can adopt
the optional typed fields and updated v1 schema independently. New observations
cannot qualify older evidence. Routing suffixes change policy and configuration
digests. Rollback may remove optional telemetry and eligibility, but must not
restore model-as-revision identity, fabricate usage or relax routing constraints.

## Follow-up

#122 owns authorized live qualification and retained artifacts. #115's trusted
runtime evidence loader remains separate; these metadata fields are not an
alternative trust boundary. No release, deployment or support-label change is
part of this patch.

The separately proposed ADR 0082 repairs live evaluation applicability with
explicit local fixture conformance and actual live quality. It preserves every
threshold and does not supply #122's authorized live evidence.
