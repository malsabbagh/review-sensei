# ADR 0015 - OIDC-Protected GitHub App Installation-Token Broker

Status: Proposed
Date: 2026-08-16
GitHub Issue: #38
Pull Request: not configured
Owners/Reviewers: Maintainers
Approved by: not configured

## Context

Code Sensei is open-source-first: customer repositories run the review
workflow on their own GitHub Actions compute. A GitHub App installation token
can only be minted with the App private key, so distributing that key to
customer repositories would make the App identity and every installation it
can reach unsafe. GitHub Actions OIDC provides a short-lived identity
assertion that can be verified by a small issuance-only backend. The backend
must verify the GitHub OIDC issuer, configured audience, JWKS, RS256 signature,
time claims, and workflow claims before applying repository policy.

Issue #38 adds this minimal broker. It issues short-lived, repository-scoped
installation tokens by reusing the existing App authentication adapter. It
does not receive, store, or process review content and is not a hosted review
service.

## Decision Drivers

- The App private key never leaves managed storage.
- OIDC assertions are verified strictly against issuer, audience, JWKS, RS256,
  time, and required claims.
- Tokens are scoped to the requesting repository and permission subset.
- Forks, removed or suspended installations, rate limits, and revocation or
  transport failures fail closed.
- Audit records contain only non-sensitive metadata.
- The design has no review content and no hosted review service.

## Decision

Add `code_sensei.hosting.github.oidc` for bounded GitHub Actions OIDC JWT
verification and `code_sensei.hosting.github.broker` for workflow policy,
fork rejection, installation mapping, rate limiting, audit records, and token
exchange. The broker reuses `GitHubAppAuth.installation_token` and remains
outside the provider-neutral review core.

## Scope

In scope:

- RS256 OIDC verification using the configured issuer, audience, and JWKS.
- Required GitHub Actions repository and workflow claim validation.
- Approved workflow, ref, fork, installation, rate-limit, and permission
  policy before token issuance.
- In-memory installation mapping, rate limiting, and bounded audit records.
- urllib-based optional GitHub installation and fork lookups with fail-closed
  behavior.
- Sanitized stable error categories and operator documentation.

Out of scope:

- An HTTP frontend or deployment service.
- Review content, review execution, queues, webhooks, or hosted review
  delivery.
- Durable audit storage or customer account management.
- Distributing the App private key to customer repositories.
- New JWT or HTTP client dependencies.

## Consequences

Positive:

- Customer Actions workflows can obtain short-lived installation tokens
  without receiving the App private key.
- Workflow identity, repository scope, permissions, fork status, and rate
  limits are checked before reusing the existing token exchange path.
- Failure categories and bounded audit metadata are suitable for a future HTTP
  frontend without exposing credentials or assertion contents.

Negative or tradeoffs:

- Operators must run and protect a small issuance-only backend and configure
  its approved workflow policy.
- In-memory mappings, rate limits, and audit records do not provide
  multi-process durability.
- JWKS availability and GitHub API availability can deny otherwise valid
  requests because the broker fails closed.

## Alternatives Considered

### Distribute the App private key to Actions secrets

- Summary: Put the centralized App private key in each customer repository's
  GitHub Actions secrets.
- Why not chosen: A repository workflow or secret exposure would compromise
  the App identity and its installation-token authority.

### Use GitHub OIDC with PyJWT

- Summary: Add PyJWT as a convenience dependency for token parsing and
  verification.
- Why not chosen: The already-pinned `cryptography` library provides the
  required RS256 primitives, keeping the dependency surface narrow and
  avoiding another JWT transport dependency.

### Cache long-lived installation tokens in Actions

- Summary: Mint a token once and retain it in Actions caches or secrets for
  later workflow runs.
- Why not chosen: Long-lived or broadly scoped cached credentials enlarge the
  replay window and weaken revocation, repository scoping, and permission
  guarantees.

## Implementation Notes

- `verify_oidc_token` bounds token and JWKS reads, validates the issuer,
  audience, key id, RS256 signature, time claims, and required workflow claims.
- `OIDCBroker.exchange` audits rejected verification and policy decisions,
  reuses `GitHubAppAuth.installation_token`, and never puts tokens or keys in
  audit records or errors.
- `InMemoryAuditSink` is bounded to 10,000 records; the rate limiter is
  thread-safe and keyed by installation, repository, and workflow ref.
- Optional GitHub lookups use urllib and return no mapping or fork=true on
  transport or response errors.

## Validation And Rollout

- Validation: Unit tests cover valid and invalid OIDC signatures, claims,
  audiences, expiry, workflow policy, fork and mapping rejection, rate limits,
  auth error propagation, audit redaction, and bounded in-memory state. Run
  the repository's deterministic quality and package gates before merge.
- Rollout: Configure an operator-owned issuance-only runtime with the App key
  in managed secret storage, its JWKS and GitHub network access, an explicit
  audience, approved workflows, and least-privileged permissions.
- Rollback: Stop the broker, revoke affected installations or rotate the App
  key if needed, clear token caches, and revert the broker modules, docs, ADR,
  and tests. No review or durable broker data migration is required.

## Follow-Up

- Add an HTTP frontend with explicit 401/429/error mapping and deployment
  hardening.
- Replace in-memory mappings, rate limits, and audit records with a reviewed
  durable design if multiple broker instances are required.
- Add operational metrics and JWKS refresh/revocation procedures.

## Links

- Related issue: #38
- Related PR: not configured
- Supersedes: none
- Superseded by: none

## Amendment - issue #64 Worker broker

The approved issuance-only frontend is Cloudflare Worker `POST
/github/token`. In addition to the original issuer, audience, JWKS, signature,
and time checks, it requires bounded repository/actor/run claims, the
configured full workflow SHA in `job_workflow_ref` and matching
`job_workflow_sha` (plus only explicitly retained older pinned SHA pairs during
migration), allowed event/runner policy, and server-side repository/fork and
installation resolution. `installation_id` is not accepted from the caller. A SQLite
Durable Object admits bounded source-address/token-digest identities before
OIDC verification, then claims the verified assertion before any GitHub API
lookup. This ordering bounds forged-token JWKS work and prevents a replayed
valid assertion from consuming GitHub App API capacity. Public JWKS responses
are cached for five minutes per Worker isolate and concurrent refreshes are
coalesced; signature, issuer, audience, workflow, and time checks still run for
every exchange. The ledger stores only hashed replay/rate identities and
bounded counters.
The endpoint issues one of four fixed disjoint capabilities (`review_publish`,
`inline_reply`, `issue_reply`, or `learning_write`) and never stores or logs
assertions, installation tokens, source, diffs, prompts, results, replies, or
provider credentials. Responses are no-store and CORS is not supported.
