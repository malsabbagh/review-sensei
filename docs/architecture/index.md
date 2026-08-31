# Architecture Index

Project: ReviewSensei
Last updated: 2026-08-19

## System Overview

ReviewSensei is a provider-neutral Python review engine. It parses untrusted
unified diffs, combines them with operator-controlled stages and trusted
target-branch context, calls a replaceable model provider, and validates the
result before a future publisher can consume it.

## Modules And Boundaries

| Area | Purpose | Owner | Key paths | Notes |
| --- | --- | --- | --- | --- |
| Review core | Provider-neutral request, parsing, orchestration, and validated results | Maintainers | `src/review_sensei/{models,diff,service,stages}.py` | Must not import GitHub or provider SDKs |
| Evaluation | Bounded corpus loading, fixture/live scoring, privacy scan, and report contracts | Maintainers | `src/review_sensei/evaluation.py`, `evaluation/v1/` | Fixture mode is offline; live egress is CLI-gated |
| Validation boundary | Frozen downward-only limits, canonical paths, strict quoted-path decoding, and bounded file reads | Maintainers | `src/review_sensei/validation.py` | Shared by request, diff, stage, transport, and publisher contracts |
| Portable workflow boundary | Ref/repository validation, bounded diff preparation, and installed-package workflow composition | Maintainers | `src/review_sensei/workflow.py`, `examples/github-actions/review-sensei-review.yml` | Must not execute head code or interpolate untrusted refs into shell commands |
| Public schemas | Versioned JSON Schema documents and local validation helpers | Maintainers | `src/review_sensei/schemas/`, `src/review_sensei/schemas.py` | Packaged defaults, examples, and golden fixtures validate against v1 schemas |
| Trusted context | Target-branch learnings and lens documents | Maintainers | `src/review_sensei/{learnings,context}.py` | Inputs are bounded and explicitly rooted |
| Provider adapters | Authentication, transport, and provider envelopes | Maintainers | `src/review_sensei/providers/` | Ollama is the first adapter |
| CLI | Host-neutral command-line composition | Maintainers | `src/review_sensei/cli.py` | Primary user path is running the CLI in the user's own GitHub Actions workflow |
| Concurrency policy | Deterministic scheduling keys returned to hosts | Maintainers | `src/review_sensei/concurrency.py` | Enforcement remains host-owned |
| Release engineering | Reproducible package metadata, artifact validation, and provenance-backed publication | Release maintainers | `pyproject.toml`, `MANIFEST.in`, `.github/workflows/release.yml`, `scripts/validate_release.py` | External PyPI/GitHub release writes stay behind maintainer-owned tag/environment authority |
| GitHub App auth | JWT and installation-token authentication for App-identity comments/reviews | Maintainers | `src/review_sensei/hosting/github/` | Narrow transport concern outside `src/review_sensei/providers/`; not a hosted service |
| GitHub App setup bootstrap | Webhook signature verification, delivery deduplication, visible provider-default variables, and idempotent setup pull request creation | Maintainers | `src/review_sensei/hosting/github/` | Fails closed on invalid webhooks; generated files and PR bodies contain no secrets |
| Cloudflare deployment package | Worker ingress, SQLite delivery/broker claims, Web Crypto GitHub App auth, capability broker, and setup-v4 PR client | Deployment operators | `deploy/cloudflare/` | Optional installation bootstrap and issuance-only token boundary; no hosted review execution or provider data |

## Dependency Direction

The CLI composes the review core, trusted-context loaders, and provider
adapters. The provider-neutral core depends only on domain contracts and the
`ReviewProvider` protocol. Provider adapters translate validated requests to
transport calls; future GitHub publishers consume only validated
`ReviewResult` values and remain outside the core.

## External Integrations

| Integration | Direction | Contract | Notes |
| --- | --- | --- | --- |
| Ollama API | Outbound | `src/review_sensei/providers/ollama.py` | Local and cloud endpoints share the adapter contract |
| Future publisher | Outbound | Validated `ReviewResult` | No GitHub SDK dependency exists in the core |
| GitHub App identity | Optional outbound installation-authenticated comments/reviews | `src/review_sensei/hosting/github/auth.py` | Custom App name/icon plus narrow JWT/installation-token auth; see `docs/github-app-auth.md` |
| GitHub App setup webhooks | Optional inbound installation events and outbound setup PR creation | `src/review_sensei/hosting/github/{webhooks.py,setup.py}` | Signature-verified setup bootstrap; see `docs/github-app-registration.md` |
| Cloudflare GitHub App package | Worker -> SQLite Durable Object -> GitHub REST | `deploy/cloudflare/` | Durable metadata only; Worker Free plan compatible; deployment remains operator-owned |
| PyPI/GitHub release | Outbound package publication and release assets | `.github/workflows/release.yml` | OIDC Trusted Publishing, checksums, SBOM, and artifact attestations; no long-lived upload token |

## Data Stores And Schemas

| Store | Schema/Model | Owner | Notes |
| --- | --- | --- | --- |
| Repository-local learnings | `LearningEntry` JSON files | Repository maintainers | Loaded only from the trusted target/base checkout |
| Review configuration | Stage and category JSON files | Repository operators | Treated as trusted configuration but structurally bounded |
| Supplemental context | Explicit Markdown/text sources | Repository operators | Selected beneath an explicit trusted context root |

## Architecture Decisions

| ADR | Status | Decision | Notes |
| --- | --- | --- | --- |
| `0001` | Accepted | Provider-neutral review engine | Core/provider boundary |
| `0002` | Accepted | Repository-local review learnings | Trusted base-branch data |
| `0003` | Accepted | Host-enforced review concurrency | Scheduling contract |
| `0004` | Accepted | Structured review stages and category catalog | Operator configuration |
| `0005` | Accepted | Bounded supplemental context per lens | Context trust boundary |
| [`0006`](../adr/0006-hosted-github-app-architecture.md) | Superseded | Hosted GitHub App as one release-unit modular service | #6; retained for history; superseded by open-source-first direction |
| [`0007`](../adr/0007-bound-untrusted-review-inputs-and-publisher-outputs.md) | Accepted | Bounded review inputs and publisher outputs | #7; Multi-tenant security boundary |
| [`0008`](../adr/0008-versioned-public-schemas-and-compatibility.md) | Proposed | Versioned public schemas and compatibility guarantees | #10; Public JSON, CLI, provider, and error contracts |
| [`0009`](../adr/0009-ci-quality-supply-chain-gates.md) | Accepted | CI quality and supply-chain gates | #3; Pinned Actions, package/schema checks, CodeQL, and required checks |
| [`0010`](../adr/0010-secure-package-release-engineering-and-provenance.md) | Proposed | Secure package release engineering and provenance | #4; package metadata, trusted publishing, and rollback |
| [`0011`](../adr/0011-portable-immutable-github-actions-workflow.md) | Proposed | Portable immutable manual GitHub Actions workflow | #15; PyPI-first package install with pinned source fallback, local-first provider defaults, bounded prepare-diff |
| [`0012`](../adr/0012-privacy-safe-review-quality-evaluation.md) | Accepted | Keep synthetic evaluation offline by default with explicit live egress | #9; Versioned corpus/report schemas and deterministic fixture CI |
| [`0013`](../adr/0013-github-app-jwt-installation-auth.md) | Proposed | GitHub App JWT and installation-scoped authentication | #12; Narrow App-identity auth outside the review core |
| [`0014`](../adr/0014-github-app-setup-bootstrap.md) | Proposed | GitHub App setup bootstrap | #37; Signature-verified, idempotent, no-secret setup PR creation |
| [`0020`](../adr/0020-cloudflare-github-app-package.md) | Proposed | Cloudflare Worker-only + SQLite Durable Object package | #37; Free-tier installation bootstrap deployment, no hosted review engine |
| [`0021`](../adr/0021-versioned-github-app-setup-migrations.md) | Proposed | Version generated setup files and migrate older installations through PRs | #37; bounded inspection and fail-closed unknown setup handling |
| [`0022`](../adr/0022-actions-publication-learning-and-conversations.md) | Proposed | Tagged setup-v4 publication, learning PRs, and authorized conversations | #64; exact-head writes and issuance-only capabilities |
| [`0023`](../adr/0023-pypi-first-github-package-fallback.md) | Proposed | Prefer PyPI and fall back to the executing-SHA public GitHub package source | #37; missing-release compatibility without unverified refs or masked install failures |

The validation boundary is shared rather than adapter-specific: `ReviewLimits`
can only tighten its public hard ceilings; canonical NFC UTF-8 paths and Git
C-quoted paths fail closed without normalization; the CLI and providers perform
bounded reads; and `ReviewResult` applies stable first-wins exact-comment
deduplication before publisher use. See ADR 0007 for the complete units,
migration, rollback, and sanitized-failure contract.

## Process Links

- Delivery process: `docs/process/index.md`
- ADR process: `docs/process/adr-process.md`
- Outcome-control policy: `.project-ai/policies/agentic-mode.json`
- Tool-risk policy: `.project-ai/policies/tool-risk.json`
- Code-retrieval policy: `<plugin-root>/skills/project-development/references/context-retrieval.md`
- Per-work-item state and evidence: `.project-ai/output/runs/`

## Agentic Outcome-Control Boundary

Versioned JSON run, evidence, verifier-result, and final-evaluation records are machine truth. Markdown is a human rendering. Runtime-specific orchestrators must honor repository policy, canonical stage transitions, approval gates, assurance requirements, bounded recovery, and fail-closed readiness.

## Operational Notes

- Build: python3 -m compileall -q src
- Test: python3 -m unittest discover -s tests -v
- Deploy: optional Cloudflare package under `deploy/cloudflare/`; no live account or deployment is configured in this repository
