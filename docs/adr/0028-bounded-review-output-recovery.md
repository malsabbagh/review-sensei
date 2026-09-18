# ADR 0028 - Bounded review-output recovery

Status: Proposed
Date: 2026-09-06
GitHub Issue: not configured
Pull Request: #100
Owners/Reviewers: Maintainers
Approved by: not configured

## Context

Provider responses and GitHub API payloads are untrusted. Two valid operating
conditions currently turn recoverable reviews into failed workflow runs:

- a provider can return structurally valid JSON whose inline comment targets a
  context or deleted line; and
- GitHub's pull-request list response can exceed the 512 KiB transport ceiling
  when learning-PR reconciliation requests 100 complete pull requests at once.

Invalid provider output must never reach a publisher, and oversized GitHub
responses must not be accepted by raising the existing transport ceiling.

## Decision

`ReviewService` performs at most two provider-output attempts per stage. When
the first response fails JSON, stage-output, category, or proposal validation,
the service discards the complete attempt and issues one fresh provider request.
The retry appends a trusted, static correction that restates the JSON and
changed-line requirements without including the rejected response or validation
details. Only a completely validated attempt is accumulated. Transport
failures, oversized provider responses, invalid provider protocol objects,
prompt-limit failures, and aggregate result-limit failures are not retried by
this policy. A second invalid structured response fails closed.

A structurally valid inline comment with a path and line outside the added or
modified diff lines is different: it is omitted, a bounded diagnostic is
logged, and the independently valid summary and inline comments continue to
publication. The diagnostic contains the comment index and static reason only,
never model-controlled path or body text. The publisher independently
revalidates every retained location against the exact diff before writing.

The GitHub learning publisher retains the 512 KiB per-response ceiling and the
existing 1,000-pull-request discovery ceiling. Historical candidate discovery
starts with 20 pull requests per page. If and only if the transport raises its
typed response-too-large error, discovery retries the same item offset with
page sizes 10, 5, and finally 1. Successful pages already collected are retained
because every fallback page size divides the preceding size. An oversized
single-item response, a response containing more items than requested, or
history beyond the existing item bound fails closed.

## Consequences

Positive:

- A malformed response receives one bounded chance to recover without weakening
  publisher validation.
- One invalid inline coordinate no longer discards independently valid review
  content; it cannot reach the publisher.
- Rejected attempt content cannot leak into the corrected prompt or final
  result.
- Large repositories can reconcile learning PR history without accepting
  oversized GitHub responses.
- The provider-neutral core and GitHub publisher remain separate.

Tradeoffs:

- A malformed provider response can consume one additional provider call per
  stage.
- Historical learning-PR discovery can use more GitHub API requests.
- Pathological repositories can require up to one request per historical pull
  request, but only after larger pages exceed the transport byte ceiling.
- Repositories with more than 1,000 relevant historical pull requests retain
  the existing fail-closed pagination outcome.

## Alternatives Considered

### Fail the entire result for an invalid inline coordinate

Rejected because one model-coordinate error needlessly discards valid findings
and makes a review workflow unreliable. Omission is observable through a
sanitized diagnostic, while the publisher continues to accept only exact-diff
locations.

### Include the rejected output in a repair prompt

Rejected because it needlessly repeats untrusted or private provider content
and complicates prompt-injection and size bounds.

### Increase the GitHub response ceiling

Rejected because it weakens a shared untrusted-response resource bound and does
not scale with repository history.

### Replace REST discovery with GraphQL

Deferred because field-selected GraphQL would reduce payload volume but adds a
second transport/query contract. Adaptive bounded REST pages solve the observed
failure without changing authentication or dependency direction.

## Validation And Rollout

- Unit tests prove transactional, sanitized, single-retry recovery and
  exhaustion after two malformed attempts, plus omission and sanitized logging
  for invalid inline coordinates while valid findings remain publishable.
- Learning-publisher tests force oversized 20- and 10-item responses, prove the
  20/10/5 fallback, verify exact-offset continuation, reject unrelated retry
  errors, and prove that an oversized single-item response fails closed.
- Run the full repository validation sequence before publication.
- Rollout requires a reviewed package/public-workflow release; moving the public
  `v4` tag or deploying the broker remains a separate operator action.

## Rollback

Revert the service retry loop, learning pull-request page constants, tests, and
documentation. No repository data or generated learning-PR migration is
required.

## Links

- Related ADRs: 0001, 0007, 0022, 0026

## Amendment - public channel `v5`

Rollout still requires a reviewed package and public-workflow release. Moving
the public `v5` tag or deploying the broker remains a separate operator
action. `v4` remains accepted during migration.
