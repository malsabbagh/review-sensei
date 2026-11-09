# ADR 0124: Opt-in review-engine decision providers

- Status: Proposed
- Date: 2026-11-09
- Decision owner/reviewers: ReviewSensei maintainers
- Issue: [#161](https://github.com/malsabbagh/review-sensei/issues/161)
- PR: pending qualification
- Approver: none; no activation approved

## Context

Review candidates benefit from bounded triage, but model assessment cannot grant
evidence, human disposition or publication authority. The existing inactive
comment-intent contract binds exact requested model identities and has a different
consumer. SystemOne can return a dated model revision and optional distributions.

## Decision

Add a separate typed review-decision contract and explicit adapter registry.
Keep the comment-intent API and inactive consumer unchanged. Offer local Ollama
schema-constrained generation and OpenRouter's SystemOne choice endpoint with
independent model, credential reference and upstream settings.

Run one aggregate decision phase after complete structural analysis. Bind every
request to the original inventory, reviewed diff and available repository/base/head
identity. Missing evidence is explicit. Deterministic policy retains all original
candidates and blocking proposals, identifies optional suggestions and suspected
duplicates, and admits at most one targeted review-provider verification batch.
Model-selected labels and probabilities do not supply proof or approval.

Use the original run tracker for calls, bytes and time, plus a subordinate aggregate
allowance covering every decision batch and follow-up. Unknown attempts retain
their allowance. Failed/partial analysis and analysis transport retries withhold
new decision work. No fallback, recursive review, shell execution or model pull.

## Scope

Core library/CLI/configuration, explicit wire adapters, result receipt/schema,
hosted planning, provider-free contracts and qualification plan. No comment-intent
activation, live model call, release, credential grant, recovery activation or
historical state mutation. The separately owned hosted recovery reconstruction
requires its matching configuration-identity companion before hosted write use.

## Consequences

Incomplete decisions preserve findings and withhold completeness. Similar prose
does not coalesce findings; conservative retention may limit immediate noise gains.
An enabled result carries a versioned, bounded, digest-bound receipt. Original
disabled output and digest behavior remains compatible. Explicit policy identities
prevent cross-configuration reuse. Enabled analysis does not populate the ordinary
analysis cache. Current hosted runner/credential combinations are validated rather
than silently changed; additional combinations need explicit workflow work.

## Alternatives considered

Repurposing comment-intent proposals would conflate independent consumers and model
binding. Routing Jev through chat/completions would use the wrong contract.
Unbounded per-chunk decisions would lose aggregate accounting and cross-stage
context. Model-only suppression or duplicate merging would erase unverified
concerns. A second approval framework would conflict with existing publication
and human-disposition authority.

## Validation

Mocked wire tests exercise exact endpoints, schemas, choices, response identities,
probabilities, usage, redirects and attempt charging. Aggregate tests cover both
chunking paths, disabled equivalence, zero candidates, invalid output, unknown work,
original blockers, one follow-up, receipt round-trips and capacity fallbacks.
Configuration tests exercise the actual core CLI and read-only plans. Installed
distribution tests and existing package/schema/type gates qualify source behavior.
These tests do not establish live compatibility, cost, latency or model quality.

## Rollout and rollback

Disabled is the default. Integrate the recovery identity companion and qualify
matching installed readers before a hosted enabled test. A live smoke requires an
explicit runtime/model/privacy boundary and call/spend cap. Keep existing receipts
and holds when disabling; do not rewrite prior results or reset accounting.

## Follow-up work

Complete the frozen paired evaluation, establish live adapter evidence and retain
the observations before recommending any configuration. More capable runner secret
plumbing, genuine duplicate coalescing and importing analysis unknown-output
reservations are separate reviewed extensions, not implicit activation here.
