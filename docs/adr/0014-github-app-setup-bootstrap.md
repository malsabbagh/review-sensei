# ADR 0014 - GitHub App Setup Bootstrap

Status: Proposed
Date: 2026-08-16
GitHub Issue: #37
Pull Request: not configured
Owners/Reviewers: Maintainers
Approved by: not configured

## Context

Code Sensei is open-source-first. Repositories run Code Sensei themselves,
usually through GitHub Actions. Issue #37 asks for a safe installation-time
bootstrap for the optional GitHub App: when a repository is newly selected by
an App installation, Code Sensei should open a reviewable setup pull request
with the portable workflow and configuration. The bootstrap must never include
credentials, private keys, installation tokens, or raw webhook bodies.

## Decision Drivers

- Review business logic must stay independent of GitHub and provider SDKs.
- GitHub App webhooks are untrusted network input and must be signature
  verified before any repository write.
- Setup pull requests must be idempotent: one setup branch/PR per newly
  selected repository, no duplicates.
- Generated workflow files must use immutable Action pins and remain
  consumer-owned, not hosted-service-owned.
- Public errors must be sanitized and stable so embedding hosts can classify
  webhook and setup failures without leaking private data.

## Decision

Add `code_sensei.hosting.github.webhooks` for webhook signature verification,
delivery deduplication, and bounded payload validation. Add
`code_sensei.hosting.github.setup` for setup plan generation, a GitHub REST
transport seam, and an idempotent setup pull request service. Keep both outside
`src/code_sensei/providers/` and the provider-neutral review core.

The setup service handles `installation.created`,
`installation.new_permissions_accepted`, and
`installation_repositories.added`. It requires write grants for `contents`,
`pull_requests`, and `variables`, skips suspended/removed/deleted installations,
and checks existing branches and pull requests before creating anything. It
creates missing visible provider configuration variables without overwriting
operator values. Webhook payloads with multiple selected repositories produce
one setup PR per repository.

## Scope

In scope:

- GitHub App registration and least-privilege permission documentation.
- Webhook HMAC-SHA256 signature verification with constant-time comparison.
- Delivery deduplication keyed by app id and `X-GitHub-Delivery`.
- Bounded webhook body validation for supported setup events.
- Setup plan builder with immutable workflow and safe config files.
- Idempotent creation of visible provider-mode/model repository variables;
  `OLLAMA_API_KEY` remains a user-managed secret and is never initialized.
- A manual cleanup workflow that creates a PR deleting generated setup files;
  automatic post-uninstall cleanup is out of scope because the installation
  token is revoked at uninstall.
- GitHub REST transport seam for default branch, branch, ref, tree, commit, and
  pull request operations.
- Idempotent setup pull request creation for newly selected repositories.
- Tests for signatures, deduplication, lifecycle, idempotency, no-secret
  behavior, transport request shapes, and sanitized errors.
- Registration, auth, architecture, public contract, and ADR documentation.

Out of scope:

- Live GitHub App registration, deployment, or installation.
- Durable webhook queue, database, marketplace, billing, or OAuth customer
  accounts.
- Merging setup pull requests or modifying existing workflows.
- Hosted review service.

## Consequences

Positive:

- Newly installed repositories get a clear setup path without requiring an
  operator to copy workflow files manually.
- Webhook processing is bounded and fail-closed: invalid signatures, malformed
  bodies, unsupported events, and duplicate deliveries are rejected before
  setup writes.
- Setup writes are idempotent and least-privileged, using only generated files
  and a reviewable pull request.

Negative or tradeoffs:

- The package gains a GitHub App webhook boundary that requires operational
  care: webhook secret provisioning, runtime delivery deduplication, and
  monitoring of transient setup failures.
- GitHub may require `workflows` write to add workflow files. The setup service
  documents that permission as conditional and does not assume it is absent.

## Alternatives Considered

### Full hosted service

- Summary: Build a hosted review service with durable queue and database.
- Why not chosen: Open-source-first direction keeps hosted delivery outside
  issue #37.

### GitHub CLI-based bootstrap

- Summary: Run `gh` locally to create setup files.
- Why not chosen: Not suitable for a webhook runtime and introduces process and
  credential complexity.

### CLI-only setup

- Summary: Operators invoke a local command in each repository.
- Why not chosen: Does not satisfy installation-time webhook bootstrap.

## Implementation Notes

- `WebhookVerifier` accepts `X-Hub-Signature-256`, validates supported events,
  rejects missing or empty webhook secrets, validates repository metadata, and
  records `(app_id, delivery_id)` in an in-memory ledger.
- `SetupPlanBuilder` produces `.github/workflows/code-sensei-review.yml` and
  `.github/code-sensei/config.yml`; generated content contains no secrets.
- `SetupPullRequestService` creates one setup branch/PR per repository, checks
  existing branch and open PR state, and returns stable statuses.
- `GitHubSetupClient` uses `urllib` style requests with bounded responses and
  sanitized errors, matching the existing auth transport pattern.

## Validation And Rollout

- Validation: Focused unit tests cover webhook safety, setup lifecycle,
  idempotency, no-secret behavior, transport request shapes, and sanitized
  errors. Run the repository's deterministic quality gates before merge.
- Rollout: Merge through the normal branch/protection process. No hosted
  deployment is required.
- Rollback: Revert the webhook/setup modules, exports, tests, docs, and ADR. No
  stored data migration is needed.

## Follow-Up

- Implement a live webhook ingress if an operator deploys the App.
- Add durable delivery ledger support for multi-process deployments if needed.
- Verify GitHub App registration behavior for workflow file creation in target
  repositories.

## Links

- Related issue: #37
- Related PR: not configured
- Supersedes: none
- Superseded by: none
