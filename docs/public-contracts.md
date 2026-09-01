# Public Contracts

ReviewSensei exposes a small set of public contracts: Python objects, CLI
behavior, configuration JSON files, provider protocol expectations, and
publisher-facing review JSON. These contracts are versioned so integrators can
depend on stable behavior without parsing internal code.

## Versioning And Deprecation Policy

The package uses semantic versioning for releases. Public schemas use a path
version in the `$id`, for example `/v1/`. Within a major version, additive JSON
fields are allowed when they do not change existing field meanings. Breaking
changes to JSON shapes require a new schema version, a new `$id`, and a new
major package release or an explicit deprecation window documented in the
changelog.

Deprecated fields and CLI flags should remain accepted for at least one minor
release after announcement. Removal is a breaking change and follows the
versioning policy above.

## JSON Schemas

Published schemas live under `src/review_sensei/schemas/` and are included in the
installed package. Each schema has a draft 2020-12 `$schema` and an `$id`
containing `/v1/`.

| Schema | Document |
| --- | --- |
| `review-result.schema.json` | Publisher-facing validated review output |
| `review-comment.schema.json` | One inline review comment |
| `learning-entry.schema.json` | Approved repository-local learning entry |
| `learning-proposal.schema.json` | Unapproved provider-proposed learning entry |
| `review-category.schema.json` | Review category/lens configuration |
| `stage.schema.json` | Stage configuration |
| `concurrency-plan.schema.json` | Host-enforced concurrency policy |
| `evaluation-corpus.schema.json` | Versioned synthetic evaluation corpus |
| `evaluation-report.schema.json` | Privacy-safe evaluation result and metrics |

The `$id` policy is fixed: the path after the package namespace must include
`/v1/` for v1 documents. Schema identity is the `$id`; emitted v1 documents do
not carry a `schema_version` field.

The validation entry point is `review_sensei.schemas`:

```python
from review_sensei.schemas import validate_public_document

validate_public_document(review_dict, "review-result")
```

Invalid documents raise `ReviewInputError` with a stable error category and do
not expose untrusted content in the message.

## Evaluation CLI contract

`review-sensei evaluate` accepts `--mode fixture|live` and a versioned
`--corpus`. Fixture mode is credential-free and rejects live/egress
acknowledgement flags. Live mode requires `--allow-live-model`, a non-empty
`--provider-version`, and `--allow-data-egress` for remote endpoints before a
provider is constructed. A report exits zero only when every applicable
deterministic and quality threshold passes.

The corpus and report documents use `/v1/` schema ids. Matching is transparent:
expected findings match one actual comment by canonical path, changed line,
category, and normalized body terms. Quality metrics are actionable precision,
false-positive rate, expected-finding recall, location validity, and category
coverage. Byte counts, provider calls, elapsed time, and `ceil(bytes / 4)` are
non-monetary performance proxies.

## Stable Python Imports

These imports are public and stable within a major version:

- `review_sensei.ReviewRequest`
- `review_sensei.ReviewResult`
- `review_sensei.ReviewComment`
- `review_sensei.LearningEntry`
- `review_sensei.LearningProposal`
- `review_sensei.ReviewLimits`
- `review_sensei.ReviewCategory`
- `review_sensei.ReviewCategoryCatalog`
- `review_sensei.Stage`
- `review_sensei.ReviewConcurrencyPlan`
- `review_sensei.ConcurrencyGroup`
- `review_sensei.ReviewService`
- `review_sensei.load_repository_learnings`
- `review_sensei.load_review_categories_from_dir`
- `review_sensei.load_stages_from_dir`
- `review_sensei.errors.ReviewSenseiError`

`ReviewResult.to_dict()` produces a JSON-compatible document that validates
against `review-result.schema.json`.

## Provider Protocol

Provider adapters implement a small protocol and live under
`src/review_sensei/providers/`:

```python
class ReviewProvider(Protocol):
    name: str
    model: str | None

    def complete(self, request: ProviderRequest) -> ProviderResponse: ...
```

Providers translate a validated `ProviderRequest` into their native API and
return a normalized `ProviderResponse`. They must not import GitHub SDKs, must
read bounded response bodies, and must sanitize transport errors before they
reach the review service. Timeout and network failures should raise
`ProviderError(transient=True)`.

## CLI Contract

The command is `review-sensei`. Supported flags are:

| Flag | Environment | Purpose |
| --- | --- | --- |
| `--version` | none | Print the installed ReviewSensei version and exit |
| `--diff` | none | Required unified diff file path |
| `--provider` | `REVIEWSENSEI_PROVIDER` | Provider registry key |
| `--base-url` | `OLLAMA_BASE_URL` | Optional Ollama API root override; mode defaults to loopback or Ollama Cloud |
| `--model` | `OLLAMA_MODEL` | Optional model override; mode defaults to the configured local/cloud model |
| `--api-key-env` | none | Name of environment variable holding the API key |
| `--timeout-seconds` | `OLLAMA_TIMEOUT_SECONDS` | Provider request timeout |
| `--repository` | none | Repository identifier |
| `--pull-request` | none | Pull request number |
| `--title` | none | Review title metadata |
| `--instructions` | none | Additional reviewer instructions |
| `--learning-root` | none | Trusted target-branch checkout root |
| `--learning-directory` | none | Repository-relative learnings directory |
| `--context-root` | `REVIEWSENSEI_CONTEXT_ROOT` | Trusted target-branch context root |
| `--no-learning-proposals` | none | Do not request durable learning proposals |
| `--categories-dir` | `REVIEWSENSEI_CATEGORIES_DIR` | Review category directory |
| `--stages-dir` | `REVIEWSENSEI_STAGES_DIR` | Stage directory |
| `--output` | none | Write JSON to a file instead of stdout |

`REVIEWSENSEI_PROVIDER_MODE` defaults to `local`. The default local model is
`qwen3.5:4b`; setting the mode to `cloud` selects
`deepseek-v4-flash:cloud` and requires `OLLAMA_API_KEY`.

`review-sensei --version` reads package metadata and prints an exact `X.Y.Z`
version. This is the version recorded by the portable manual GitHub Actions
workflow before a review runs.

The command also supports a `prepare-diff` subcommand for portable diff
preparation in consumer repositories:

```bash
review-sensei prepare-diff \
  --base-ref main \
  --head-ref feature \
  --head-repository owner/repo \
  --repository . \
  --output pr.patch \
  --max-diff-bytes 1048576 \
  --max-diff-lines 50000 \
  --max-diff-files 500 \
  --max-diff-hunks 5000
```

| Flag | Purpose |
| --- | --- |
| `--base-ref` | Base ref to validate and resolve |
| `--head-ref` | Head ref to validate and resolve |
| `--head-repository` | Optional `owner/repo` slug for fork review |
| `--repository` | Local target repository root |
| `--output` | Unified diff output path |
| `--max-diff-bytes` | Tighten the public diff byte ceiling |
| `--max-diff-lines` | Tighten the public diff line ceiling |
| `--max-diff-files` | Tighten the public diff file ceiling |
| `--max-diff-hunks` | Tighten the public diff hunk ceiling |
| `--version` | Print the installed ReviewSensei version and exit |

`prepare-diff` validates refs and optional repository slugs before invoking
git, fetches only explicit refs with list-based argv, resolves base/head to
commit OIDs, verifies a merge base exists, and writes a bounded triple-dot diff.
It never checks out or executes head code. The `head_remote` parameter exists
only as a Python test seam; the workflow does not expose it. The example
workflow additionally binds checkout to the repository default branch and
requires a maintainer-controlled `ollama` self-hosted runner for its local
provider default.

Exit codes are stable:

| Code | Meaning |
| --- | --- |
| `0` | Review completed and output was written |
| `1` | Input, validation, provider, formatting, or filesystem failure |

## Error Categories

Expected failures expose `error_category` on the exception class:

| Class | Category |
| --- | --- |
| `ReviewInputError` | `input` |
| `ReviewFormatError` | `format` |
| `LearningLoadError` | `learning` |
| `ContextLoadError` | `context` |
| `ProviderError` | `provider` |
| `GitHubAuthConfigurationError` | `github_auth_configuration` |
| `GitHubAuthUnavailableInstallationError` | `github_auth_unavailable_installation` |
| `GitHubAuthInsufficientPermissionError` | `github_auth_insufficient_permission` |
| `GitHubAuthTransientError` | `github_auth_transient` |
| `GitHubAuthError` | `github_auth` |
| `GitHubWebhookSignatureError` | `github_webhook_signature` |
| `GitHubWebhookError` | `github_webhook` |
| `GitHubSetupError` | `github_setup` |
| `GitHubSetupTransientError` | `github_setup_transient` |
| `GitHubOIDCError` | `github_oidc` |
| `GitHubOIDCInvalidTokenError` | `github_oidc_invalid_token` |
| `GitHubOIDCAudienceError` | `github_oidc_audience` |
| `GitHubOIDCIssuerError` | `github_oidc_issuer` |
| `GitHubOIDCExpiredError` | `github_oidc_expired` |
| `GitHubOIDCSignatureError` | `github_oidc_signature` |
| `GitHubOIDCClaimsError` | `github_oidc_claims` |
| `BrokerPolicyError` | `broker_policy` |
| `BrokerRateLimitError` | `broker_rate_limit` |
| `BrokerRejectionError` | `broker_rejection` |
| `ReviewSenseiError` | `unknown` |

`ProviderError` also exposes `transient`:

```python
try:
    response = provider.complete(request)
except ProviderError as exc:
    if exc.transient:
        # Retry only after a bounded delay.
        ...
```

Transient failures are network timeouts and equivalent transport errors.
Malformed provider envelopes, oversize responses, and invalid HTTP statuses are
not transient.

## GitHub App Auth Contract

Optional GitHub App-identity publication uses the narrow authentication
adapter documented in [`docs/github-app-auth.md`](github-app-auth.md):

```python
from review_sensei.hosting.github import (
    EnvPrivateKeySource,
    GitHubAppAuth,
    RequestedPermissions,
)

auth = GitHubAppAuth(
    app_id=123456,
    private_key_source=EnvPrivateKeySource("GITHUB_APP_PRIVATE_KEY"),
)
token = auth.installation_token(
    installation_id=789,
    repository="owner/repo",
    requested_permissions=RequestedPermissions({"pull_requests"}),
)
```

`GitHubAppAuth` signs short-lived App JWTs with an injectable private-key
source and exchanges them for installation access tokens scoped to a
repository and requested permission set. Cached tokens are keyed by
installation, repository, and permission scope and refresh before expiry.
Installation tokens are opaque variable-length strings. Auth errors expose the
stable categories listed above and do not include private keys, JWTs,
installation tokens, authorization headers, or API bodies.

## GitHub App Webhook And Setup Bootstrap Contract

Optional App installation bootstrap uses webhook signature verification and an
idempotent setup pull request service. Public entry points include:

```python
from review_sensei.hosting.github import (
    EnvWebhookSecretSource,
    SetupPullRequestService,
    WebhookVerifier,
)
```

`WebhookVerifier` accepts a `X-Hub-Signature-256` header, validates the
delivery body with a bounded read, rejects empty or missing webhook secrets,
rejects duplicate delivery ids, and returns `VerifiedDelivery` metadata for
supported installation events. Setup writes only happen for
`installation.created`, `installation.new_permissions_accepted`, and
`installation_repositories.added`. The service creates one setup branch/PR per
selected repository from `VerifiedDelivery.repositories` (or the single
`repository` fallback), checks for existing branches and pull requests before
writing, and never includes private keys, installation tokens, raw webhook
bodies, authorization headers, or GitHub API bodies in generated files or PR
bodies.

Setup and webhook errors expose the stable categories listed above. Webhook
signature failures use `github_webhook_signature`; malformed or duplicate
deliveries use `github_webhook`; setup validation failures use `github_setup`;
transient GitHub setup transport failures use `github_setup_transient`.
Sanitized messages do not include secret markers or untrusted response bodies.

## Migration And Rollback

Schema migrations are source-compatible when possible: add optional fields
inside v1 without changing required field meaning. For breaking changes,
introduce a new schema version and keep the old schema importable until the
next major release.

Concurrency keys are part of host behavior. If a key encoding changes, older
runs and newer runs can create separate workflow/provider groups. Treat that as
a migration event, drain or cancel active older runs deliberately, and update
host configuration before rollback.

Rollback is code-only for review limits, schemas, and error metadata: revert
the package change and restore the prior imports/configuration. No stored
review data migration is required.

## Issue-64 publication contracts

`ReviewPublisher` reconstructs a typed `ReviewResult`, revalidates all
locations against the exact diff, rereads the open non-draft same-repository PR
head, and submits one App-authored review whose summary and inline comments
append the fixed follow-up instruction `To discuss this finding, reply with
@sensei followed by your question.` and carry the marker
`<!-- reviewsensei:review:v1 repo=<id> pr=<n> head=<sha> result=<sha256> -->`.
The marker, App slug, exact head commit, and repository identity form the
idempotency boundary; stale, fork, duplicate, ambiguous, or invalid writes
fail closed.

Learning proposal identity is a canonical SHA-256 digest. Only the deterministic
`.github/review-sensei/learnings/sensei-<16-hex>.json` path may be materialized
on a `review-sensei/learnings/pr-<number>-<16-hex>` branch, and the PR is always
draft. Existing identical base content skips; different content conflicts.

Conversation replies require a standalone case-insensitive `@sensei` mention
from a human OWNER, MEMBER, or COLLABORATOR. Reply bodies are bounded and
validated before marker append; source update time, exact head, root-thread
identity, PR state, and fork state are reread before publication. The inline
reply marker binds source comment, updated-time digest, PR, and head.
Generated review and comment-event execution supports both provider modes.
Cloud operations use GitHub-hosted compute and send bounded review or
conversation context to Ollama Cloud; local operations use the labelled
self-hosted runner and configured local Ollama service. After authorization and
before provider execution, ReviewSensei adds an App-authored `eyes` reaction to
the source comment. It removes that reaction after reply publication or another
terminal outcome. Each subsequent standalone `@sensei` mention is a new bounded,
idempotent conversation turn over the current thread and exact PR head.

All setup-v4 switches (`REVIEWSENSEI_AUTO_REVIEW`,
`REVIEWSENSEI_GITHUB_WRITES`, `REVIEWSENSEI_LEARNING_PRS`,
`REVIEWSENSEI_MENTION_REPLIES`, and `REVIEWSENSEI_UPLOAD_ARTIFACTS`) default
to `false`. The generated caller may contain only the name-only secret mapping
`OLLAMA_API_KEY: ${{ secrets.OLLAMA_API_KEY }}`; no secret value is generated
or handled by the App setup boundary.
