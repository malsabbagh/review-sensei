# ADR 0020 - Cloudflare GitHub App Worker Package

Status: Proposed
Date: 2026-08-16
GitHub Issue: #37
Pull Request: not configured
Owners/Reviewers: Maintainers
Approved by: not configured

## Context

ReviewSensei is open-source-first: customer repositories own the review
workflow, runner, provider credentials, and merge decision. Issue #37 needs an
optional GitHub App installation path that receives signed installation events
and opens a reviewable setup pull request.

The first deployment shape used a CPython Cloudflare Container because the
existing Python adapter uses synchronous HTTP transport and process locks. A
Container requires the Workers Paid plan, which makes the optional setup path
unnecessarily expensive for low-volume installations. The free-tier package
must keep the review core independent while implementing the narrow setup
boundary directly in the Worker runtime.

The deployment must not persist webhook payloads, installation tokens, private
keys, provider data, or generated secrets. It must survive Worker restarts and
scale-out without relying on an in-memory delivery ledger.

## Decision Drivers

- Keep the provider-neutral review core and existing GitHub adapter seam intact.
- Bound and authenticate public webhook input before any GitHub API write.
- Make delivery claims durable and atomic across Worker instances.
- Use only Web Worker APIs and the SQLite Durable Object Free-tier surface.
- Keep raw bodies and credentials out of durable storage and logs.
- Make setup writes reviewable, idempotent, and installation-scoped.

## Decision

`deploy/cloudflare/` is a Worker-only package with two runtime pieces:

1. The Worker at `/github/webhook` enforces `POST`, a bounded body, required
   GitHub headers, and HMAC-SHA256 using Web Crypto. It parses the supported
   installation payloads, computes a body digest, and forwards only bounded
   delivery metadata to the ledger.
2. A single named SQLite-backed Durable Object, `DeliveryLedger`, stores
   `(app_id, delivery_id, digest, state, lease_until, updated_at)`. It retains
   accepted identities for one hour, permits lease takeover after five minutes,
   and never stores the raw webhook body.

After a durable claim, the Worker uses Web Crypto RSASSA-PKCS1-v1_5 signing to
create a short-lived GitHub App JWT. It exchanges that JWT for a repository-
scoped installation token, validates the granted `contents`, `pull_requests`,
and `variables` permissions, creates missing visible repository configuration
variables, and calls the GitHub REST API to create the setup branch and PR.
Tokens and private keys remain in the request/isolate memory only.

The generated workflow reads `REVIEWSENSEI_PROVIDER_MODE`,
`REVIEWSENSEI_LOCAL_MODEL`, and `REVIEWSENSEI_CLOUD_MODEL` from repository
variables. Defaults are `local`, `qwen3.5:4b`, and
`deepseek-v4-flash:cloud`, respectively. Existing variable values are never
overwritten. `OLLAMA_API_KEY` remains an operator-managed repository secret;
the bootstrap never creates a blank secret or handles its value.

If GitHub requires it for workflow-file writes, operators must additionally
grant `workflows: write`; the Worker does not broaden the requested token
permissions automatically.

Setup creation is idempotent. The Worker checks the setup branch and open setup
PR before writing, creates the branch from the repository default branch only
when needed, rechecks the open PR after branch creation, and creates at most
one setup PR per selected repository. A later delivery beyond the one-hour
ledger window is protected by the existing branch/PR check.

The package does not declare Cloudflare Containers or the `@cloudflare/containers`
dependency. It is intended to run on the Workers Free plan, subject to the
current Worker, Durable Object, SQLite, GitHub API, and App permission limits.

## Scope

In scope:

- Wrangler Worker configuration and SQLite Durable Object migration.
- Worker ingress, HMAC verification, payload validation, and bounded forwarding.
- Web Crypto GitHub App JWT signing and installation-token exchange.
- Bounded GitHub REST calls for setup branch and pull request creation.
- Idempotent creation of visible repository Actions variables for provider
  mode/model defaults.
- A generated manual cleanup workflow that opens a PR deleting only generated
  workflow/configuration files.
- One-hour delivery retention, lease/release flow, and setup idempotency.
- Free-tier deployment, local development, retry, and rollback documentation.
- Static package contract tests and the existing repository quality gates.

Out of scope:

- Registering or installing a GitHub App.
- Running `wrangler deploy`, creating Cloudflare resources, or changing billing.
- A hosted review queue, model-provider service, OAuth account system, or
  customer review data store.
- Persisting webhook payloads, installation tokens, private keys, diffs, or
  model output in Cloudflare storage.
- Automatically creating a cleanup PR after App uninstall; GitHub revokes the
  installation token at that authorization boundary.
- Porting the provider-neutral Python review engine into the Worker runtime.

## Consequences

Positive:

- The optional setup ingress can run without a Workers Paid plan or Docker.
- GitHub App identity and setup behavior remain separate from review execution.
- Durable claims prevent duplicate setup attempts across restarts.
- The Worker and ledger use only bounded metadata and standard Web APIs.

Negative or tradeoffs:

- GitHub App JWT signing and REST setup logic are duplicated in the deployment
  adapter rather than reusing CPython classes at runtime.
- Web Crypto key import must support GitHub's RSA PEM format and remains a
  security-sensitive adapter boundary.
- A single named ledger object serializes claims globally; higher volume would
  need sharding or a queue design.
- Existing deployments of the earlier Container package should be treated as
  a separate deployment and migrated deliberately; this package's migration
  creates only the Worker ledger class.

## Alternatives Considered

### Worker plus CPython Container

The previous implementation kept the Python webhook and setup adapters behind
a private Container. It remains technically viable but requires the Workers
Paid plan and Docker/Workers Builds, so it is not the default package.

### Python Worker only

Cloudflare's Python Worker runtime does not provide the existing adapter's
CPython/threading/synchronous transport contract. Porting the whole Python
adapter would be a larger and less bounded change than the small setup
transport needed here.

### In-memory delivery state

An in-memory ledger would lose claims on restarts and allow duplicate setup PR
writes after scale-out. The SQLite Durable Object remains the durable claim
boundary.

### D1 or a hosted database

A separate database would add a provisioning and ownership boundary for a
small identity ledger. A single SQLite Durable Object provides serialized
claims and bounded storage without another service.

## Validation And Rollout

- Validate the Wrangler JSON contract, Worker TypeScript typecheck, pinned
  setup workflow strings, secret exclusions, Python package tests, and
  `git diff --check`.
- Deploy first to a separate Worker/environment, configure a test GitHub App,
  and verify valid, duplicate, conflicting, malformed, suspended, and retried
  installation deliveries.
- Verify setup PR idempotency, permission skips, lease recovery, token expiry
  handling, and secret rotation before production.
- Confirm the Worker URL, Durable Object migration, and GitHub delivery status
  after deployment. This change does not perform those external writes.

## Rollback

Redeploy the previous Worker commit with the same `DeliveryLedger` class and
migration history. Keep the ledger namespace so accepted delivery identities
remain deduplicated. If an earlier Container deployment exists, roll it back
as its own deployment rather than deleting the shared ledger without a data
retention decision.

## Links

- Related issue: #37
- Related ADR: [0014 - GitHub App Setup Bootstrap](0014-github-app-setup-bootstrap.md)
- Package: [`deploy/cloudflare/`](../../deploy/cloudflare/)
- Cloudflare Workers pricing: https://developers.cloudflare.com/workers/platform/pricing/
- Cloudflare Durable Objects pricing: https://developers.cloudflare.com/durable-objects/platform/pricing/
- Cloudflare Web Crypto: https://developers.cloudflare.com/workers/runtime-apis/web-crypto/
