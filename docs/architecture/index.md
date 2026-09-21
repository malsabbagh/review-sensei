# Architecture Index

Project: ReviewSensei
Last updated: 2026-09-19

## System Overview

ReviewSensei is a provider-neutral Python review engine. It parses untrusted
unified diffs, combines them with operator-controlled stages and trusted
target-branch context, calls a replaceable model provider, and validates the
result before a future publisher can consume it.

## Modules And Boundaries

| Area | Purpose | Owner | Key paths | Notes |
| --- | --- | --- | --- | --- |
| Review core | Provider-neutral request, parsing, orchestration, and validated results | Maintainers | `src/review_sensei/{models,diff,service,stages,coverage,planning,baseline}.py` | Must not import GitHub or provider SDKs; coverage is explicit and chunking is opt-in; C4 verification reuses incremental plans |
| Evaluation | Bounded corpus loading, fixture/live scoring, privacy scan, and report contracts | Maintainers | `src/review_sensei/evaluation.py`, `evaluation/v1/` | Fixture mode is offline; live egress is CLI-gated |
| Validation boundary | Frozen downward-only limits, canonical paths, strict quoted-path decoding, and bounded file reads | Maintainers | `src/review_sensei/validation.py` | Shared by request, diff, stage, transport, and publisher contracts |
| Portable workflow boundary | Ref/repository validation, bounded diff preparation, and installed-package workflow composition | Maintainers | `src/review_sensei/workflow.py`, `examples/github-actions/review-sensei-review.yml` | Must not execute head code or interpolate untrusted refs into shell commands |
| Public schemas | Versioned JSON Schema documents and local validation helpers | Maintainers | `src/review_sensei/schemas/`, `src/review_sensei/schemas.py` | Packaged defaults, examples, and golden fixtures validate against v1 schemas |
| Trusted context | Target-branch learnings, lens documents, and opt-in Python symbol-aware excerpts | Maintainers | `src/review_sensei/{learnings,context}.py` | Inputs are bounded and explicitly rooted; symbol-aware selection is disabled by default |
| Provider adapters | Authentication, transport, and provider envelopes | Maintainers | `src/review_sensei/providers/` | Ollama is the first adapter |
| CLI | Host-neutral command-line composition | Maintainers | `src/review_sensei/cli.py` | Primary user path is running the CLI in the user's own GitHub Actions workflow |
| Concurrency policy | Deterministic scheduling keys and optional in-process admission leases | Maintainers | `src/review_sensei/concurrency.py` | GitHub-hosted enforcement uses workflow concurrency groups; no global lock |
| Release engineering | Reproducible package metadata, artifact validation, and provenance-backed publication | Release maintainers | `pyproject.toml`, `MANIFEST.in`, `.github/workflows/release.yml`, `scripts/validate_release.py` | External PyPI/GitHub release writes stay behind maintainer-owned tag/environment authority |
| GitHub App auth and approval boundary | JWT and installation-token authentication, exact-head review publication, and idempotent approval finalization | Maintainers | `src/review_sensei/hosting/github/` | Unresolved blocking ReviewSensei roots request changes and withhold approval; GraphQL classification sweep is bounded and fail-closed |
| GitHub App setup bootstrap | Webhook signature verification, delivery deduplication, visible provider-default variables, and idempotent setup pull request creation | Maintainers | `src/review_sensei/hosting/github/` | Fails closed on invalid webhooks; generated files and PR bodies contain no secrets |
| GitHub learning publisher | Stable source-PR draft identity, content-addressed learning files, and fail-closed reconciliation | Maintainers | `src/review_sensei/hosting/github/learning_pr.py` | Hash-bound marker/commit provenance, latest-base rereads, non-force refreshes, merged generations |
| Cloudflare deployment package | Worker ingress, SQLite delivery/broker claims, Web Crypto GitHub App auth, capability broker, and setup-v5 PR client | Deployment operators | `deploy/cloudflare/` | Optional installation bootstrap and issuance-only token boundary; no hosted review execution or provider data |

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
| GitHub learning pull requests | Outbound GitHub REST Git-object and pull-request reconciliation | `src/review_sensei/hosting/github/learning_pr.py` | One draft per source PR while open; merged generations and unsafe/manual artifacts are handled deterministically |
| GitHub App identity | Optional outbound installation-authenticated comments/reviews | `src/review_sensei/hosting/github/auth.py` | Custom App name/icon plus narrow JWT/installation-token auth; see `docs/github-app-auth.md` |
| GitHub App setup webhooks | Optional inbound installation events and outbound setup PR creation | `src/review_sensei/hosting/github/{webhooks.py,setup.py}` | Signature-verified setup bootstrap; see `docs/github-app-registration.md` |
| Cloudflare GitHub App package | Worker -> SQLite Durable Object -> GitHub REST | `deploy/cloudflare/` | Durable metadata only; Worker Free plan compatible; deployment remains operator-owned |
| PyPI/GitHub release | Outbound package publication and release assets | `.github/workflows/release.yml` | OIDC Trusted Publishing, checksums, SBOM, and artifact attestations; no long-lived upload token |

## Data Stores And Schemas

| Store | Schema/Model | Owner | Notes |
| --- | --- | --- | --- |
| Repository-local learnings | `LearningEntry` JSON files | Repository maintainers | Loaded only from the trusted target/base checkout |
| Learning PR provenance | v2 marker and commit trailer | ReviewSensei publisher | Binds repository/PR, pending batch, reviewed source head, latest base, exact title/body hashes, and the rendered source-title line; no provider or private review data |
| Review configuration | Stage and category JSON files | Repository operators | Treated as trusted configuration but structurally bounded |
| Review session ledger | `SessionRecord` JSON / GitHub issue comment | Maintainers | Bounded PR-wide round counters plus identity-bound `ReviewTransaction` phase/digest metadata; no source/result body; ADRs 0047 and 0052 |
| Supplemental context | Explicit Markdown/text sources and opt-in Python symbol-aware excerpts | Repository operators | Documents/learnings by default; symbol-aware selection requires trusted-base policy |


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
| [`0009`](../adr/0009-ci-quality-supply-chain-gates.md) | Accepted | CI quality and supply-chain gates | #3; GitHub-hosted runners, pinned Actions, package/schema checks, CodeQL, and required checks |
| [`0010`](../adr/0010-secure-package-release-engineering-and-provenance.md) | Proposed | Secure package release engineering and provenance | #4; package metadata, trusted publishing, and rollback |
| [`0011`](../adr/0011-portable-immutable-github-actions-workflow.md) | Proposed | Portable immutable manual GitHub Actions workflow | #15; PyPI-first package install with pinned source fallback, local-first provider defaults, bounded prepare-diff |
| [`0012`](../adr/0012-privacy-safe-review-quality-evaluation.md) | Accepted | Keep synthetic evaluation offline by default with explicit live egress | #9; Versioned corpus/report schemas and deterministic fixture CI |
| [`0013`](../adr/0013-github-app-jwt-installation-auth.md) | Proposed | GitHub App JWT and installation-scoped authentication | #12; Narrow App-identity auth outside the review core |
| [`0014`](../adr/0014-github-app-setup-bootstrap.md) | Proposed | GitHub App setup bootstrap | #37; Signature-verified, idempotent, no-secret setup PR creation |
| [`0020`](../adr/0020-cloudflare-github-app-package.md) | Proposed | Cloudflare Worker-only + SQLite Durable Object package | #37; Free-tier installation bootstrap deployment, no hosted review engine |
| [`0021`](../adr/0021-versioned-github-app-setup-migrations.md) | Proposed | Version generated setup files and migrate older installations through PRs | #37; bounded inspection and fail-closed unknown setup handling |
| [`0022`](../adr/0022-actions-publication-learning-and-conversations.md) | Proposed | Tagged setup-v4 publication, learning PRs, and authorized conversations | #64; exact-head writes and issuance-only capabilities |
| [`0023`](../adr/0023-pypi-first-github-package-fallback.md) | Proposed | Prefer PyPI and fall back to the executing-SHA public GitHub package source | #37; missing-release compatibility without unverified refs or masked install failures |
| [`0024`](../adr/0024-tag-only-setup-v4-channel.md) | Proposed | Operator-managed workflow channel for setup-v4 (currently `v5`; `v4` accepted during migration) | #64; Worker validates the write tag and broker checks its runtime resolution |
| [`0026`](../adr/0026-stable-reviewsensei-learning-pull-request-identity-and-reconciliation.md) | Proposed | Stable ReviewSensei learning pull-request identity and reconciliation | #97; one draft per source PR, hash-bound provenance, latest-base refresh, and fail-closed lifecycle |
| [`0027`](../adr/0027-opt-in-app-approvals-for-clean-pull-request-reviews.md) | Superseded | Historical App approvals for clean pull-request reviews | Superseded by ADR 0030; the clean-review policy is now the default |
| [`0028`](../adr/0028-bounded-review-output-recovery.md) | Proposed | Bounded review-output recovery | One sanitized provider correction and response-budgeted learning history discovery |
| [`0029`](../adr/0029-finding-classification-and-lens-presentation.md) | Proposed | Independent severity, fix effort, and lens-aware finding presentation | Additive v1 classification metadata and deterministic publisher rendering |
| [`0032`](../adr/0032-blocking-finding-classification-for-approvals.md) | Proposed | Classify blocking findings for approvals | Durable blocking markers and a shared finalizer ignore unresolved non-blocking findings and converge after review publication or blocking-thread resolution |
| [`0030`](../adr/0030-gate-app-approvals-on-resolved-review-threads-and-exact-head-review-safety.md) | Superseded | Gate App approvals on resolved review threads and exact-head review safety | Bounded GraphQL thread sweep; clean reruns can promote same-head comments |
| [`0034`](../adr/0034-ai-decided-review-thread-resolution.md) | Proposed | Let ReviewSensei decide whether its addressed inline thread can be resolved | Typed `resolve` decision, exact-head/App-root GraphQL guards, and deterministic approval finalization after a blocking resolution |
| [`0035`](../adr/0035-request-changes-for-blocking-findings.md) | Proposed | Request changes for unresolved blocking findings | Blocking findings emit `REQUEST_CHANGES`; a later same-head `APPROVE` wins only after those roots resolve |
| [`0036`](../adr/0036-learning-lifecycle-diagnostics-and-feedback.md) | Proposed | Learning lifecycle diagnostics and opt-in finding feedback | Advisory stale/conflict signals; feedback is not trusted review context |
| [`0037`](../adr/0037-symbol-aware-source-context.md) | Proposed | Opt-in deterministic bounded symbol-aware source context from trusted base | #40; Python AST selector; default remains documents/learnings; #33 evaluation required before default enablement |
| [`0040`](../adr/0040-community-managed-enterprise-boundary.md) | Proposed | Community / Managed / Enterprise product boundary | #89; MIT Community source preserved; official ops distinct from copyright; future commercial layer separate |
| [`0041`](../adr/0041-production-provider-adapters-and-per-stage-profiles.md) | Proposed | Production adapters, per-stage profiles, and shared conformance | #42; openai-compatible remains the additional production adapter; local runs cannot select remote stage profiles |
| [`0042`](../adr/0042-incremental-reviews-and-finding-lifecycle.md) | Proposed | Incremental reviews and stable finding lifecycle identities | #38; coverage modes, fingerprint lifecycle, optional in-memory metadata cache |
| [`0043`](../adr/0043-structured-run-outcomes-budgets-and-publication-recovery.md) | Proposed | Structured run outcomes, resource budgets, and publication-only recovery | #36; `ReviewService.run`, hard budgets, publication-only recovery |
| [`0046`](../adr/0046-evidence-based-blocker-admission-and-review-loop-convergence.md) | Proposed | Evidence-based blocker admission and bounded review-loop policy | #136; C1/C2; `legacy` unchanged |
| [`0047`](../adr/0047-durable-review-session-ledger.md) | Proposed | Durable PR-wide review-session ledger | #136 C3; local JSON and GitHub issue-comment adapters |
| [`0048`](../adr/0048-baseline-aware-verification.md) | Proposed | Baseline-aware verification | #136 C4; IncrementalReviewPlan + late classification |
| [`0049`](../adr/0049-automation-admission-and-handoff.md) | Proposed | Automation admission and handoff | #136 C5; cap never mints approval |
| [`0050`](../adr/0050-maintainer-disposition-and-handoff-status.md) | Proposed | Maintainer disposition and handoff status | #136 C6; `action_required` for handoff |
| [`0051`](../adr/0051-sequential-evaluation-and-shadowing.md) | Proposed | Sequential evaluation and observation-only shadowing | #136 C7; default stays `legacy` |
| [`0052`](../adr/0052-logical-review-transaction-across-analysis-and-publication.md) | Proposed | Logical review transaction across analysis and publication | #146 F1; one reservation, one checkpoint, retryable publication phases |
| [`0055`](../adr/0055-merge-focused-default-and-legacy-retirement.md) | Proposed | Make merge-focused the default and retire live legacy selection | #146 F7; migration and release-readback remain explicit operator gates |

Ownership, trademark, and licensing inventory:
[`docs/ownership-and-licensing.md`](../ownership-and-licensing.md),
[`TRADEMARKS.md`](../../TRADEMARKS.md).

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
