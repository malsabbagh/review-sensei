# ADR 0129: Bounded pull-request lookup diagnostics

- Status: Proposed
- Date: 2026-11-10
- Decision owner/reviewer: existing recovery diagnostics owner; independent review
- GitHub Issue: none; user-directed gap correction
- Pull request: pending publication
- Context: User-directed broker diagnostics improvement

## Problem

The broker's pull-request head lookup can reject a capability after provider work
has been retained. A generic lookup code loses the distinction between an HTTP
failure and a malformed successful response. Source/workflow refs cannot identify
the actual deployed Worker, and an empty native inventory cannot prove cessation.

## Decision

Keep the existing diagnostic version, error codes, outer HTTP status, refusal,
request limits and retry behavior. A typed internal lookup error can add one
optional top-level `pull_request_lookup` object to the existing token-error
response and bounded failure log. Old consumers ignore this additive object;
new consumers continue to accept old responses.

The object has at most four fields: `outcome` from the closed vocabulary `http`,
`malformed`, `oversized`, `timeout`, `transport`, `unknown`; optional integer
`upstream_status` from 100 through 599; optional `requested_capability` from the
nine existing capability names; and optional `worker_version`, a 36-character
UUID from the trusted platform version-metadata binding. Missing or invalid
status/identity remains unavailable. Caller body values, source SHAs, version
tags and timestamps cannot supply deployed identity. This change declares the
optional binding interface only; it does not configure it or deploy anything.

Serializers construct fresh closed objects, excluding response bodies, headers,
tokens, URLs, account identifiers and arbitrary exception properties/text. The
caller validates the optional object under the existing 4,096-byte response
inspection limit. Invalid supplementary metadata is dropped while the original
recognized reason is retained; an oversized envelope keeps the existing unknown
fallback. Outcome summaries round-trip only validated fields.

## Qualification and limits

Synthetic tests exercise the real bounded GitHub response decoder, token-route
error serialization and caller error/outcome rendering, including malformed and
oversized metadata, leakage canaries, old responses and unchanged 403 refusal.
No telemetry service, binding configuration, access grant or capacity is added.
These fields are observations, never settlement, successful-exit, non-dispatch,
cessation, retry or approval authority. They cannot reconstruct a past response
whose evidence was not recorded. Runtime activation and actual binding provenance
remain separate operator qualification; workflow/source identity is insufficient.

## Scope and consequences

Only the existing PR-head reader, broker error serialization and caller diagnostic
parsing/rendering change. Other readers and authorization paths retain their
existing behavior. Pre-response failures without a known typed outcome retain their original error
and emit no supplementary envelope; no status or failure stage is invented. The additive envelope is bounded independently of arbitrary
upstream metadata. Old callers preserve the original reason but omit new facts.
No immutable Worker identity can be reported unless the operator separately
configures the genuine platform binding; this patch leaves configuration unchanged.

## Alternatives considered

Embedding status in the existing code would change stable reason handling. Adding
fields inside the version-one diagnostic object would make old closed parsers
reject the entire reason. Raw exception/header/body logging would violate privacy.
An optional separate closed envelope preserves compatibility without those costs.

## Validation

Targeted GitHub API/Worker diagnostic tests exercise the real bounded response
decoder and public Worker fetch response. Caller tests use a synthetic HTTPError
through BrokerClient, CLI outcomes and Actions summary rendering. Tests assert a
single exchange, unchanged refusal codes and no leakage. TypeScript and Python
static checks complement those tests; exact-head remote CI remains authoritative.

## Rollout and rollback

This is source-only. Deployment/package activation and any platform binding
configuration remain separate operator actions. Old/new Workers and callers can
coexist through the additive optional envelope. Reverting this patch drops the
new observations without rewriting retained state or changing old failure reasons.

## Follow-up work

Independently qualify the installed caller and deployed Worker and any genuine
version binding before operational reliance. Historical recovery still requires
original authority, effects, funding and closure evidence; diagnostics grant none.
