# ADR 0013 - GitHub App JWT and installation-scoped auth

Status: Proposed
Date: 2026-08-13
GitHub Issue: #12
Pull Request: not configured
Owners/Reviewers: Maintainers
Approved by: not configured

## Context

Code Sensei is open-source-first. Repositories run Code Sensei themselves,
usually through GitHub Actions. Issue #12 asks for a real GitHub App identity
with a custom name/icon and the narrow JWT/installation-token path needed to
post comments or reviews as that App, without building a hosted review service.

## Decision Drivers

- The provider-neutral review core must not import GitHub or model-provider
  SDKs.
- App identity is registration/branding plus optional App-identity publication,
  not a hosted backend.
- App private keys, JWTs, and installation tokens must stay out of committed
  configuration, logs, and public errors.
- Installation tokens must be scoped to the repository/permission subset
  required by the future publisher.
- Token caching must not cross installation, repository, or permission
  boundaries.

## Decision

Add a narrow `code_sensei.hosting.github` auth adapter outside
`src/code_sensei/providers/`. It signs short-lived App JWTs with an injectable
private-key source, exchanges them for installation access tokens through an
injectable transport, verifies granted permissions are a subset-safe write
grant, and caches tokens by installation/repository/permission scope with
expiry-aware refresh. Add documentation for GitHub App registration, branding,
key rotation, and emergency revocation.

## Scope

In scope:

- GitHub App registration/branding and App-identity publication docs.
- GitHub authentication adapter outside the provider-neutral review core.
- Injectable secret-source interface for the App private key.
- Short-lived App JWT generation with testable clock and bounded clock skew.
- Installation access token minting for a repository/permission subset.
- Optional expiry-aware token cache keyed by installation/repository/permission
  scope.
- Opaque variable-length installation tokens.
- Suspension, deletion, removal, permission-change, revocation, transient, and
  rate-limit failure handling with sanitized messages.
- Unit tests using fake keys, clocks, transports, and HTTP responses.

Out of scope:

- Hosted review service.
- Durable delivery/job/queue storage.
- Webhook ingress.
- Tenant configuration, quotas, retention, or deployment.
- Marketplace listing.
- Review comment/review publication transport.

## Consequences

Positive:

- A future GitHub publisher can consume the same narrow auth path without
  moving GitHub SDKs or secret handling into the review core.
- Installations are least-privileged by default: tokens request only the
  repository/permission subset needed for comments/reviews.
- Failure classes are machine-readable and do not leak secrets.

Negative or tradeoffs:

- The package gains a runtime dependency on `cryptography` for RS256 signing.
- The adapter is an auth boundary only; operators still need to register the
  GitHub App and provision its private key before App-identity publication can
  be used.

## Alternatives Considered

### Use a hosted GitHub App service

- Summary: Build a hosted review service with GitHub App identity.
- Why not chosen: The project direction is open-source-first; hosted delivery,
  queues, tenant config, and webhooks are outside issue #12.

### Add GitHub auth inside the provider-neutral core

- Summary: Put JWT signing and token transport into `ReviewService`.
- Why not chosen: The core must stay independent of GitHub and provider SDKs.

### Use a JWT library and a full GitHub SDK

- Summary: Add `PyJWT` and `PyGithub` dependencies.
- Why not chosen: The narrow path only needs RS256 signing and one HTTP POST;
  `cryptography` plus a small transport seam keeps the dependency surface
  smaller and easier to audit.

## Implementation Notes

- Public entry points live under `src/code_sensei/hosting/github/`.
- `GitHubAppAuth.create_app_jwt()` signs `iat`, `exp`, and `iss` claims.
- `GitHubTokenTransport` posts a JSON body with permissions and the repository
  short name to
  `/app/installations/{installation_id}/access_tokens`.
- Cached token scopes include installation id, repository slug, and requested
  permissions. Expired or invalid tokens are not returned.
- Installation tokens are treated as opaque strings. No prefix, length, or
  format validation is applied.
- HTTP and JSON errors are converted to stable sanitized auth errors.

## Validation And Rollout

- Validation: Focused unit tests cover JWT claims/signature, rotation, cache
  isolation, refresh, permission checks, error mapping, redaction, and bounded
  private-key reads. Run the repository's deterministic quality gates before
  merge.
- Rollout: Merge through the normal branch/protection process. No hosted
  deployment is required.
- Rollback: Revert the auth package, dependency, docs, ADR, and tests. No
  stored data migration is needed.

## Follow-Up

- Implement the GitHub publisher transport that consumes validated
  `ReviewResult` values using this auth adapter.
- Add a GitHub App registration checklist and release docs if a packaged App
  is published later.

## Links

- Related issue: #12
- Related PR: not configured
- Supersedes: none
- Superseded by: none
