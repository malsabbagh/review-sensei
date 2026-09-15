# ADR 0034 - AI-decided ReviewSensei thread resolution

Status: Proposed
Date: 2026-09-15
GitHub Issue: not configured
Pull Request: draft PR to be linked
Owners/Reviewers: Maintainers
Approved by: not applicable

## Context

ReviewSensei can answer a maintainer's bounded `@sensei` mention, but the
resulting reply historically had no machine-readable way to say whether the
original ReviewSensei finding was fully addressed. Maintainers therefore had
to resolve every addressed thread manually before a clean same-head review
could be promoted to `APPROVE`.

## Decision

Extend the validated conversation-reply contract with an optional boolean
`resolve` field. Omitted or false values keep the thread open. The provider may
set it to true only when the bounded, exact-head context demonstrates that the
ReviewSensei finding is fully addressed; provider prose is not interpreted as a
resolution command.

The GitHub adapter honors `resolve: true` only when all of the following hold:

- the source is an inline pull-request comment;
- the root comment is authored by the configured ReviewSensei App;
- the current pull request is open, non-draft, same-repository, and still has
  the exact head SHA used for the conversation;
- a bounded GraphQL lookup maps the root REST comment id to one review-thread
  id; and
- the `resolveReviewThread` mutation returns that same id with `isResolved:
  true`.

The mutation is idempotent. A retry of an already-published reply may finish a
pending resolution, while issue comments, human-authored roots, stale heads,
ambiguous mappings, and malformed or unavailable GraphQL responses remain
unresolved and fail closed.

## Alternatives considered

### Resolve every ReviewSensei thread after any reply

Rejected because an explanatory reply does not prove that the finding is
addressed, and it would hide concerns that the agent was uncertain about.

### Resolve human-authored or issue-only threads

Rejected because those threads do not represent ReviewSensei findings and may
contain independent maintainer or contributor decisions.

### Require a separate maintainer confirmation for every AI decision

Not selected as the default path requested by the product owner. The exact-head
and App-root gates preserve a narrow mutation boundary; repositories that need
human confirmation can disable mention replies or omit the `resolve` field in
their provider policy.

## Consequences

Positive:

- The AI reviewer can close an addressed ReviewSensei finding without a
  separate manual cleanup pass.
- Resolution remains explicit, typed, bounded, exact-head, and restricted to
  App-owned inline threads.
- Existing reply markers and reconciliation provide safe retries.

Tradeoffs:

- A provider can make a mistaken resolution decision, so prompt quality and
  exact-head validation remain important controls.
- A successful resolution causes one additional provider review in the same
  workflow job; if that pass is interrupted or fails, the caller must retry the
  mention or synchronize a fresh review before approval can be promoted.
- GitHub GraphQL becomes a dependency for replies that request resolution.

## Validation and rollback

Add model/schema, prompt, GraphQL mapping, mutation, idempotency, stale-head,
human-root, issue-comment, malformed-response, and transport-failure tests.
Roll back by shipping a provider policy that omits `resolve`, disabling mention
replies, or reverting the adapter; no persisted data migration is required.
