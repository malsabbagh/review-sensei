# Architecture Decision Records

Project: ReviewSensei
Last updated: 2026-08-19

ADRs capture durable architecture decisions for this repository. Use `docs/adr/NNNN-short-title.md` for individual records.

Process: `docs/process/adr-process.md`

## Index

| ADR | Status | Decision | Related issue | Notes |
| --- | --- | --- | --- | --- |
| [0001](0001-provider-neutral-review-engine.md) | Accepted | Keep review behavior behind a provider-neutral service and protocol | Initial architecture | GitHub and model transports remain adapters |
| [0002](0002-repository-local-review-learnings.md) | Accepted | Store approved learnings in the reviewed repository | #1 | Only target/base content is trusted |
| [0003](0003-review-concurrency-policy.md) | Accepted | Expose deterministic concurrency policy for host enforcement | Initial architecture | Amended 2026-08-09 |
| [0004](0004-structured-review-stages.md) | Accepted | Configure ordered stages and reusable review categories | #2 | Operator-controlled configuration |
| [0005](0005-lens-context-sources.md) | Accepted | Select bounded supplemental context per review lens | #2 | Explicit trusted context root |
| [0006](0006-hosted-github-app-architecture.md) | Superseded | Hosted GitHub App as one release-unit modular service | #6 | Retained for history; open-source-first target does not include a hosted service |
| [0007](0007-bound-untrusted-review-inputs-and-publisher-outputs.md) | Accepted | Bound untrusted review inputs and publisher outputs | #7 | Security boundary, configuration, migration, and rollback |
| [0008](0008-versioned-public-schemas-and-compatibility.md) | Proposed | Version public JSON schemas and compatibility guarantees | #10 | Schema `$id`, deprecation, migration, and rollback policy |
| [0009](0009-ci-quality-supply-chain-gates.md) | Accepted | Establish CI quality and supply-chain gates | #3 | Pinned Actions, package/schema checks, CodeQL, and required checks |
| [0010](0010-secure-package-release-engineering-and-provenance.md) | Proposed | Secure package release engineering and provenance | #4 | Release workflow, package validation, trusted publishing, and rollback policy |
| [0011](0011-portable-immutable-github-actions-workflow.md) | Proposed | Portable immutable manual GitHub Actions workflow | #15 | Exact released package, local-first provider defaults, bounded prepare-diff |
| [0012](0012-privacy-safe-review-quality-evaluation.md) | Accepted | Keep synthetic evaluation offline by default with explicit live egress | #9 | Versioned corpus/report schemas and deterministic fixture CI |
| [0013](0013-github-app-jwt-installation-auth.md) | Proposed | GitHub App JWT and installation-scoped authentication | #12 | Narrow App-identity auth outside the review core; no hosted service |
| [0014](0014-github-app-setup-bootstrap.md) | Proposed | GitHub App setup bootstrap | #37 | Signature-verified, idempotent, no-secret setup PR creation |
| [0015](0015-oidc-installation-token-broker.md) | Proposed | OIDC-protected installation-token broker | #38 | Issuance-only backend reusing App auth; no review content |
| [0016](0016-product-identity-rename-to-reviewsensei.md) | Proposed | Product identity rename to ReviewSensei | #44 | Preserve internal repo slug and historical records |
| [0019](0019-reviewsensei-dev-landing-page.md) | Proposed | ReviewSensei.dev canonical landing page | #47 | Static HTML under docs/site deployed via GitHub Pages |
| [0020](0020-cloudflare-github-app-package.md) | Proposed | Cloudflare Worker-only + SQLite Durable Object package | #37 | Free-tier installation bootstrap ingress; no hosted review engine |
| [0021](0021-versioned-github-app-setup-migrations.md) | Proposed | Version generated setup files and migrate older installations through PRs | #37 | Bounded inspection; unknown or future files are left untouched |
| [0022](0022-actions-publication-learning-and-conversations.md) | Proposed | Immutable setup-v3 publication, deterministic learning PRs, and authorized conversations | #64 | Exact-head App writes behind an issuance-only OIDC capability broker |

## Policy

Publication-control ADRs 0017 and 0018 are internal-only and are excluded from
the public publication tree together with the publication workflow and operator
runbook.

Create or update an ADR for changes that affect:

- public APIs, events, schemas, or cross-module contracts
- data model, persistence, migrations, retention, or backfills
- authentication, authorization, secrets, privacy, or security posture
- deployment topology, infrastructure, queues, caching, or operational ownership
- dependency direction, package boundaries, or long-lived architectural patterns
- irreversible or expensive-to-reverse decisions

Use `.project-ai/templates/adr.md.tmpl` as the starter template.
