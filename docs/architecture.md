# Architecture

## Boundary map

```text
trusted target/base checkout
     |                    |
     v                    v
LearningStore    RepositoryContextStore
     |                    |
     +------> ReviewLensContext
                    |
trusted category files --> ReviewCategoryCatalog
                    |          |
trusted stage files --> Stage category ids
                               |
diff changed paths --> active lens selection
                               |
                               v
                         ReviewRequest
                               |
                               v
                                ReviewService
                                        |
                                        v
                           single-pass prompt renderer
                                        |
                                        v
                              ReviewProvider protocol
                                  |           |
                                  v           v
                           OllamaProvider   future adapters
                                  |
                                  v
                         validated ReviewResult
                           |                   |
                           v                   v
                    review comments   learning proposals ----> future draft learning PR
                                               |
                                               v
                                future GitHub or other publisher adapters

ReviewRequest ----> ReviewConcurrencyPlan ----> host scheduler
```

The review service owns the business invariants. Provider adapters own protocol,
authentication, timeout, and response-envelope details. Publishers will own
GitHub API authentication and comment/review delivery when that boundary is
implemented.

The versioned synthetic corpus flows through a bounded, provider-neutral
evaluation loader and `FixtureProvider` into the unchanged `ReviewService`.
Reports retain only aggregate, non-monetary metrics. Live endpoints are
authority-gated by the CLI; CI uses fixture mode.

## Quality and supply-chain boundary

The repository quality boundary is deterministic and provider-independent. The
`quality` CI job runs Ruff formatting/linting, mypy, compileall, and branch
coverage; the `schemas` job validates the three packaged Draft 2020-12
contracts and immutable GitHub Action pins; the `package` job builds both
distributions and verifies a wheel from outside the checkout; CodeQL is the only
job granted `security-events: write`. All other workflow permissions default to
`contents: read`, and the `required-checks` aggregate publishes the stable
`Required checks` status used by branch protection.

The public JSON schemas are structural contracts only. They reject malformed
shapes and unknown fields, but runtime loaders (`ReviewCategory`, `Stage`, and
`LearningEntry`) remain the semantic authority for path safety, placeholder
allowlists, cross-file references, and bounded values. Schema documents,
workflow text, diffs, comments, and model output remain untrusted input. CI tool
versions are direct, exact pins in `requirements/ci.txt`; the packaged runtime
depends on the pinned-compatible `jsonschema` library for public contract
validation and `cryptography` for GitHub App JWT signing.

The shared `validation.py` module owns the provider-neutral untrusted-input
boundary. Its frozen `ReviewLimits` profile defaults to 1,048,576 bytes/50,000
lines/500 files/5,000 hunks per diff, 4,194,304-byte prompts, 1,048,576-byte
provider responses, and 2,097,152-byte publisher results, with independent
metadata, learning, category, context, summary, comment, proposal, and line
number ceilings documented in the README and ADR 0007. Profiles can only move
downward. Canonical NFC UTF-8 repository paths and strict Git C-quoted paths are
validated once and reused by models, stages, and diff analysis; no path is
silently normalized. The CLI reads at most the diff ceiling plus one byte, and
each provider adapter owns an equivalent bounded transport read.

## Portable workflow boundary

`src/review_sensei/workflow.py` owns the portable manual GitHub Actions boundary.
It validates untrusted workflow inputs (`base_ref`, `head_ref`,
`head_repository`, `review_sensei_version`, and diff limits) before invoking git,
fetches explicit refs with list-based argv, resolves base/head refs to verified
commit OIDs, verifies a merge base, and writes a bounded triple-dot diff without
checking out or executing head code. The installed package exposes this through
`review-sensei prepare-diff`, so consumer repositories that do not contain Code
Sensei source can still run the reviewed workflow.

The workflow boundary defaults to local Ollama at
`http://127.0.0.1:11434/api` with no API key and the `qwen3.5:4b` model. The
example therefore runs on a
maintainer-controlled self-hosted Linux runner labelled `ollama`; the runner
must have Ollama and the selected model provisioned before dispatch. The
workflow binds checkout to `github.event.repository.default_branch` and
rejects a dispatch `base_ref` that differs from that trusted branch. Cloud
provider egress is explicit opt-in only: an operator must set the repository
variable `REVIEWSENSEI_PROVIDER_MODE=cloud`, which uses the fixed Ollama Cloud
endpoint and the `deepseek-v4-flash:cloud` model by default, and requires
`OLLAMA_API_KEY`. The workflow does not accept an arbitrary provider URL input,
so a dispatch-supplied URL cannot redirect the provider credential. The example
workflow first installs the exact requested `review-sensei==X.Y.Z` package from
PyPI into a separate `RUNNER_TEMP` virtual environment. When that exact
distribution is unavailable, it installs the same version from a public GitHub
source commit pinned in the workflow; other PyPI failures remain fatal. The
install step verifies the package metadata and dependency set before review.

## GitHub App setup migration boundary

The installation bootstrap is a narrow deployment adapter, not a repository
configuration owner. Generated setup files carry a `ReviewSensei setup version`
marker. Before creating a setup pull request, the Cloudflare Worker or Python
adapter reads only the three known generated paths from the trusted default
branch, with a 128 KiB per-file limit and strict UTF-8 decoding. Older
generated files and partial current setups produce a reviewable migration PR;
all-current files are a no-op. A custom, malformed, or future-version file
produces a no-write result so repository-owned workflow content is not
overwritten. The migration branch is rebuilt from the current default branch
and changes only generated paths, preserving learnings, variables, secrets,
and unrelated repository files. See ADR 0021.

Public JSON documents are versioned under `src/review_sensei/schemas/` with v1
`$id` values. The `review_sensei.schemas` module and
`scripts/validate_public_schemas.py` validate packaged defaults, examples, and
golden fixtures against those schemas before publication. Public schemas,
stable error categories, provider transient metadata, CLI flags, and
compatibility guarantees are documented in `docs/public-contracts.md` and ADR
0009.

Proposal aggregate sizing uses the compact UTF-8 serialization of the complete
proposal array, including its brackets and separators, so the publisher-facing
bound is deterministic across stages.

## Selected design

The project considered two approaches:

1. Put Ollama calls, GitHub event handling, prompt construction, and comment
   posting in one workflow or server module.
2. Keep a provider-neutral review service behind a small provider protocol, then
   add Ollama and GitHub as replaceable adapters.

ReviewSensei uses the second approach. It keeps model changes local, makes the
review rules testable without network access, prevents GitHub credentials from
becoming part of the core, and lets a future provider or publisher reuse the
same validated result.

## Domain contracts

- `ReviewRequest` contains a unified diff and optional repository metadata.
- `ReviewRequest.learnings` contains only approved `LearningEntry` values loaded
  from the target branch or target commit.
- `ReviewRequest.active_category_ids` records the configured lenses whose
  `applies_to` patterns match the changed paths. The default pattern is `**`;
  `*` matches within one segment and `**` spans zero or more segments.
- `ReviewRequest.lens_contexts` contains the approved learnings and bounded
  `ReviewDocument` values selected for each active lens.
- `RepositoryContextStore` resolves only explicitly configured sources inside a
  trusted target/base checkout. It never defaults to scanning the process's
  working directory.
- `ProviderRequest` is the provider-facing prompt contract.
- `ProviderResponse` normalizes provider text without retaining raw request
  context.
- `Stage` declares one provider invocation, its expected output sections, and
  optional structured `ReviewCategory` values.
- `ReviewCategory` gives a category a stable lowercase id, a human-readable
  title, one or more concrete focus items, path applicability, and optional
  learning/document selectors. Active categories are serialized into
  `{review_categories}` without rescanning inserted diff or instruction text
  for template placeholders.
- `ReviewCategoryCatalog` owns reusable category identity and resolves the
  `category_ids` declared by stages. Unknown and duplicate ids fail before a
  provider request.
- `ReviewResult` is the publisher-facing, validated result.
- `ReviewResult.to_dict()` produces a v1 `review-result` document validated by
  the packaged JSON Schema.
- `ReviewResult.learning_proposals` contains unapproved durable-context
  proposals. The core never writes them to a repository.
- `ReviewConcurrencyPlan` exposes two host-enforced scopes for a pull-request
  review: a latest-wins workflow group and a non-cancelling, single-slot
  provider group. Both are keyed by repository and pull request, so different
  pull requests can run independently. Non-review triggers receive an isolated
  workflow group and no provider slot.
- `ReviewComment` must use a repository-relative path and a positive line.
- `ReviewComment` and `ReviewResult` reject bodies, lines, counts, proposals,
  and serialized output above the selected `ReviewLimits` profile. Exact
  `(path, line, body)` duplicates are deduplicated first-wins in stable stage
  order, while distinct bodies remain separate.
- By default, every comment must target an added or modified line in the diff.
- When a stage declares categories, any category supplied on its comments must
  match one of that stage's ids. A category remains optional so providers that
  omit classification do not invalidate an otherwise actionable finding.
- Learning scopes are repository-relative path patterns and are selected before
  prompt construction.
- Lens documents include repository-relative path and SHA-256 provenance. They
  are reference data, not instructions, and are serialized under the owning
  lens through `{review_context}`.

## Review stage configuration

Category files contain one `ReviewCategory` each and are indexed by stable id in
a `ReviewCategoryCatalog`. Stage files are sorted by filename and executed in
that order. Each stage contains a `name`, an `outputs` array, a
`prompt_template`, and either `category_ids` resolved from a supplied catalog or
an inline `categories` array. A stage cannot combine both forms. Reusing an id
in multiple stages is allowed only when its title and focus items are identical,
keeping the aggregated result vocabulary unambiguous.

The category block is configuration, not provider output. A stage with
categories must include `{review_categories}` in its template so focus data
cannot be configured but silently omitted from the model prompt. A stage whose
categories request supplemental context must also include `{review_context}`.
Template placeholder names are allowlisted, and replacement occurs in one regex
pass so tokens embedded in untrusted diffs, learnings, documents, or
instructions remain literal.

`applies_to` defaults to the whole diff and controls whether a lens is active.
The CLI derives applicability paths independently from inline-comment locations,
including deleted paths and both sides of renames. An active lens can select
approved learning categories and repository-relative document sources. A source
can be a file or directory with include/exclude patterns and can be optional or
required. The CLI resolves documents against
`--context-root`, falling back to the already trusted `--learning-root`. It does
not implicitly use the current directory. The packaged architecture lens opts
into conventional architecture sources; other lenses receive no document
context unless configured.

Custom stage and category directories are operator-selected configuration. They
must come from a trusted checkout or deployment bundle, not the pull-request
head. The loaders reject empty directories, symlinked or non-regular JSON files,
duplicate stage names or category ids, unknown category references, oversized
files, excessive file counts, malformed categories, unknown configuration
fields, and unknown template placeholders before any provider request is made.

The context loader rejects traversal, symlinks, secret-like names, unsupported
text formats, unreadable or empty files, excessive file counts, and per-file or
aggregate size violations. Selection and serialization are deterministic.

## Failure behavior

- Provider transport or envelope failures raise `ProviderError`.
- Invalid JSON or invalid comment shapes raise `ReviewFormatError`.
- Empty or malformed stage pipelines raise `ReviewInputError` before a provider
  is called.
- A model-supplied comment category outside a stage's declared ids raises
  `ReviewFormatError`.
- Invalid locations fail closed instead of being sent to a publisher.
- Invalid, oversized, duplicate, retired, or out-of-repository learning files
  are rejected or excluded before prompt construction.
- Missing required lens sources, unsafe paths, symlinks, secret-like files, and
  oversized document context raise `ContextLoadError` before provider use.
- Invalid concurrency identities are rejected before a host can use their group
  keys. The core does not create cross-process locks, cancel network requests,
  or persist queue state; the embedding host enforces the returned policy.
- Error messages avoid including prompts, diffs, API keys, or provider bodies.
- Expected failures from the bounded path, diff, request, transport, and result
  seams are static/sanitized and do not include supplied secret markers.

## Future extension points

- Additional model adapters register with `ProviderRegistry`.
- A GitHub publisher can consume `ReviewResult`, reuse the GitHub App auth
  adapter in `src/review_sensei/hosting/github/`, and implement review creation,
  thread replies, and idempotency.
- A GitHub publisher can convert validated `learning_proposals` into a draft PR
  under the reviewed repository's base branch. Only merged learning files are
  eligible for later reviews.
- A workflow or queue adapter can enforce `ReviewService.concurrency_plan()`
  without adding GitHub or provider-specific dependencies to the review engine.

## Open-source integration

The primary distribution is the open-source package and CLI. Repositories run
ReviewSensei themselves in GitHub Actions: checkout the trusted base, fetch the
head ref only to compute a bounded diff through `review-sensei prepare-diff`,
install the exact requested package from PyPI or, when that distribution is
unavailable, from a full-SHA-pinned public GitHub source commit, run the
provider, and upload the validated result plus the installed version. A source
fallback is used only for the package-not-found condition; other PyPI failures
remain fatal. This path does not require a hosted backend,
GitHub App installation tokens, a durable job queue, a database, or deployment
infrastructure. The default provider configuration is local Ollama, so private
source data is not exported by default.

Optional GitHub App identity: a GitHub App registration can provide ReviewSensei
branding, name, and icon. If App-identity comments or reviews are needed, the
App can use JWT and installation-token authentication as a narrow transport
concern. That authentication work belongs outside the provider-neutral review
core and is implemented under `src/review_sensei/hosting/github/` with
registration, key-rotation, and revocation guidance in
[`docs/github-app-auth.md`](github-app-auth.md). It is not a hosted review
service. The default manual workflow does not post comments and does not
require an App.

Installation-time setup bootstrap is also outside the review core. When an App
installation is created or repositories are added, the webhook/setup boundary
verifies webhook signatures, deduplicates deliveries, and opens a reviewable
setup pull request per newly selected repository. The setup service never
includes secrets, private keys, installation tokens, raw webhook bodies,
authorization headers, or GitHub API bodies in generated files or PR bodies. It
creates missing visible provider-mode/model repository variables without
overwriting operator values; `OLLAMA_API_KEY` remains a user-managed secret.
The generated manual cleanup workflow can open a PR deleting the setup files,
but the App cannot do that automatically after uninstall because its token is
revoked.
See [`docs/github-app-registration.md`](github-app-registration.md) and ADR
0014.

An optional Cloudflare deployment package under `deploy/cloudflare/` supplies a
live ingress for that narrow setup boundary without turning ReviewSensei into a
hosted review engine. The Worker applies a bounded Web Crypto HMAC gate and
payload validation, then a single named SQLite-backed Durable Object stores
only delivery identity, digest, state, and a short lease. The same Worker uses
Web Crypto RS256 signing, installation-scoped GitHub tokens, and bounded REST
calls to create idempotent setup pull requests and defaults. No raw body, token,
key, diff, or provider output is persisted. The package does not use Cloudflare
Containers, so this deployment path is compatible with the Workers Free plan.
See [`deploy/cloudflare/README.md`](../deploy/cloudflare/README.md) and ADR
0020.

## Release engineering boundary

The distributable package is a separate, operator-owned boundary around the
provider-neutral review engine. `pyproject.toml` is the single package metadata
source; `setup.py` is retained only as a metadata-free compatibility shim for
legacy tooling. `MANIFEST.in` controls source-distribution documentation and
example inclusion, while setuptools package-data rules carry the packaged
default stage/category JSON into wheels.

`.github/workflows/release.yml` is a tag-driven release workflow, not runtime
application code. It validates a `vX.Y.Z` tag against package metadata and the
changelog, builds and inspects the sdist/wheel before any external write, and
uses a dedicated PyPI environment with GitHub OIDC Trusted Publishing rather
than a long-lived upload token. The build also emits checksums, an SBOM, and
GitHub artifact attestations before the publish and GitHub-release jobs attach
the immutable assets. Workflow permissions are scoped per job and third-party
actions are pinned to full commit SHAs. When the repository variable
`ENABLE_UBICLOUD_HOSTED` is `true`, every active workflow job uses the
`ubicloud-standard-2` Linux runner; the compatibility workflow omits its
Windows/macOS entries because those hosts are not available in that mode. An
unset or false variable retains the GitHub-hosted Ubuntu fallback and the full
cross-platform compatibility matrix.

Release operations, PyPI registration, signed-tag ownership, verification,
rollback/yank, and compromised-release response are documented in
`docs/releasing.md`. The workflow cannot reserve the PyPI project or decide
whether a maintainer should publish; those are explicit external operations.

## Publication boundary

The private development repository produces the public
`malsabbagh/review-sensei` through a reproducible publication pipeline. An
exclusion manifest (`.publication/exclusions.json`) lists every internal-only
file pattern with a documented reason. A commit-oriented publication audit
(`scripts/audit_publication.py`) resolves an explicit source commit and exact
Git tree, applies the exclusion manifest before reading blobs, rejects unsafe
publishable non-blob entries, scans publishable blobs for credentials, private
repository references, private-network endpoints, and unallowlisted identity
metadata, and writes a deterministic redacted report. The audit fails closed
without echoing matched values. `scripts/publish_public.py` exports the
approved commit's file set to the public repository and appends a provenance
ledger entry recording both SHAs. The internal `publish` GitHub Actions
workflow gates the audit, dry-run, and sync behind maintainer approval; the
workflow and operator runbook are intentionally excluded from the public tree.

Deferred hosted review service: the hosted GitHub App architecture in ADR 0006
is superseded for now. The Cloudflare package is only an optional installation
bootstrap ingress; customer Actions still own review execution and provider
compute. A hosted review service would require a new ADR and a fresh issue set.

## OIDC installation-token broker

The optional OIDC installation-token broker is a narrow boundary outside the
provider-neutral review core: `oidc.py` verifies GitHub Actions identity and
`broker.py` applies workflow policy before reusing `GitHubAppAuth`. The App
private key remains in managed storage, the broker handles no review content,
and forks, missing or suspended installations, rate limits, and verification
or GitHub failures are rejected fail-closed.

```text
GitHub Actions OIDC id_token (audience=sts.reviewsensei.dev, id-token: write)
    -> OIDCBroker.exchange
        -> verify_oidc_token (issuer/audience/JWKS/RS256/exp/iat/nbf/claims)
        -> approved workflow identity + fork rejection
        -> installation mapping
        -> rate limit
        -> GitHubAppAuth.installation_token (repo-scoped, permission-checked)
        -> short-lived InstallationToken (no key/JWT in response)
        -> audit (non-secret metadata only)
```

## Issue-64 setup-v3 publication architecture

Setup-v3 separates the customer caller, public execution workflow, and
issuance-only Worker:

```text
customer setup-v3 caller (exact public workflow SHA; all opt-ins false)
    -> public reusable workflow
       -> trusted-base checkout and bounded diff
       -> PyPI package or pinned public GitHub source fallback
       -> GitHub-hosted cloud review or trusted local Ollama review
       -> typed result/reply validation
       -> Actions OIDC assertion
          -> Cloudflare POST /github/token
             -> exact claims/workflow SHA + repository/fork/install checks
             -> hashed replay/rate Durable Object claim
             -> one least-privileged capability token
       -> App-authored exact-head review, learning PR, or authorized reply
```

The generated setup PR is limited to the workflow caller, uninstall workflow,
and config file. The public reusable workflow is immutable by full SHA and may
receive the existing customer-owned `OLLAMA_API_KEY` only through a literal
name-only secret mapping. The setup App does not access that value. The Worker
does not receive source, diffs, prompts, review output, reply content, provider
credentials, or installation-token values for persistence; its ledger retains
only hashed identities and bounded counters. Current, custom, malformed, and
future clients are no-write cases, while absent and legacy/v2 clients are
reconciled through at most one reviewable setup-v3 PR per selected repository.

This architecture preserves the provider-neutral core: GitHub transport,
Actions OIDC, broker capabilities, setup lifecycle, publication markers, and
conversation authorization remain under `src/review_sensei/hosting/github/`
or `deploy/cloudflare/`.
