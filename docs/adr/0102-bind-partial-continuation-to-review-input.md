# ADR 0102: Bind partial continuation to the complete review input

- Status: Proposed
- Date: 2026-10-22
- Decision owner / reviewers: ReviewSensei maintainers
- Linked GitHub Issue: No separate issue; user-authorized readiness repair of PR #342
- Linked PR: [#342](https://github.com/malsabbagh/review-sensei/pull/342)

## Context

Bounded continuation carries already-reviewed file and hunk coverage into the
next window. An ordinary result produced by the public `ReviewService.run` API
can have no durable transaction. Path names and hunk indexes alone do not bind
that result to the original diff. A two-file, one-call partial result can
therefore skip a changed first file on a new head, review only the second file,
and incorrectly report complete coverage. Hosted callers have additional
transaction fences; those do not repair the direct API seam.

The result's work-policy digest identifies execution policy, not source input.
The frozen v1 content-digest projection also has existing transaction/replay
compatibility obligations and must not silently change to repair this seam.

## Decision

Add an optional persisted `ReviewResult.continuation_scope_digest`, validated as
one lowercase SHA-256 value. The service computes a domain-separated identity
from the complete review request and effective execution configuration, including
the actual diff, source snapshot, admitted context, configured stages and routing,
review profile, limits/work policy and incremental plan. Validation-only
`expected_*` transaction arguments remain independent checks rather than scope
inputs: adding a trusted checkpoint after analysis must not change the actual
analysis identity.
Only the digest is stored; no source text, prompt or credential is added to
result or session metadata.

Before planning, provider execution or reuse of any prior coverage, recompute the
current scope and require the prior partial result to carry the same digest.
Missing or mismatching scope refuses continuation. Do not manufacture a missing
binding from the new request or reuse an old head's finding inventory. A valid
same-source continuation remains usable across fresh service instances and JSON
round trips. Existing transaction and provider/model/policy checks remain
independent requirements.

Bind the returned aggregate to its outer request. Nested per-chunk executions
must not substitute their narrower identities for the aggregate's scope, and
concurrent runs must not share mutable active-scope state.

The optional provenance field does not alter the frozen v1 review-content digest
or grant publication, human assessment or approval authority. Serialized payload
integrity continues to cover the actual result bytes. Every existing result,
provider, storage, publication and request budget remains unchanged.

## Scope

Public engine continuation, validated result serialization and its closed public
schema, direct and hosted callers of that service, and bounded continuation
regressions. No new publisher, credentials, broker capability, model call or
approval exception.

## Consequences

Unbound direct API results gain a durable source-context identity. Older partial
results remain readable as historical results, but cannot supply reusable
coverage without this binding. They require fresh analysis through the normal
authorized path. The new metadata consumes its actual bytes under existing
result limits; oversized results still refuse.

The digest is provenance and consistency evidence, not a signature or a human
attestation. A caller able to fabricate arbitrary review results does not gain
hosted write or approval authority from this field.

## Alternatives considered

1. Require a durable transaction for every direct API result. This would force
   local callers to invent repository/session identities or lose supported
   direct multi-window continuation.
2. Reuse the existing context-cache key or work-policy digest. Neither represents
   every actual input required to bind reused source coverage; repurposing a
   policy hash would hide its meaning and invalidate existing identities.
3. Retain only path and hunk positions. Those positions can stay identical when
   the source changes, which is the reproduced failure.

The dedicated bounded digest keeps continuation validation at the public service
boundary and leaves host transaction authority separate.

## Validation

Use genuine service-produced partial results. Reject a changed head, changed
diff at the same head, changed admitted context and changed stage configuration
before a second provider call or any reused coverage. Cover missing/malformed
scope metadata, same-source continuation, fresh-process JSON reload and the
existing 9-call/33-call progression. Preserve frozen v1 digest fixtures, result
size ceilings, hosted transaction rejection and original request accounting.
Synthetic regressions establish these contracts, not deployed provider quality
or native merge approval.

## Rollout and rollback

Qualify updated engine and hosted readers before publishing or activating new
continuation producers. The closed schema explicitly admits the optional field;
do not assume an older parser preserves it. Do not downgrade a continuation
reader or strip the field to force reuse. Missing evidence holds the continuation
until compatible readers or a separately authorized fresh review are available.
Package release, Worker deployment and signed v5 promotion remain separate
operations under the existing release policy.

## Follow-up work

Maintain the scope identity when adding source-relevant review inputs or routing
policy. No existing failed review bytes, coverage receipt or approval is recreated
by this change. This ADR remains Proposed pending maintainer review.
