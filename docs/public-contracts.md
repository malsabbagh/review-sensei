# Public Contracts

## npm launcher contract (issue #103)

`@reviewsensei/cli` exposes exactly one `review-sensei` bin and five exact
version optional dependencies: `@reviewsensei/cli-darwin-arm64`,
`@reviewsensei/cli-darwin-x64`, `@reviewsensei/cli-linux-arm64-gnu`,
`@reviewsensei/cli-linux-x64-gnu`, and `@reviewsensei/cli-win32-x64`. Node.js
22+ is required. Platform packages declare their `os`/`cpu` constraints and
contain a one-folder native payload; no package has lifecycle scripts or a
runtime download.

The launcher uses only `process.platform`, `process.arch`, and the Linux
glibc runtime report to select a target. It resolves the installed package's
own metadata, checks exact version and a regular in-package executable, then
spawns with raw argv, `shell: false`, inherited stdio/cwd/env, and bounded
credential-free errors. It forwards supported termination signals and
preserves child exit status. Review behavior, provider configuration, and
`prepare-diff` semantics remain those of `review_sensei.cli:main`.

The public target and release-order decision is recorded in [ADR 0031](adr/0031-npm-launcher-and-standalone-platform-packages.md) and tracked by
internal issue #103.

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
| `learning-feedback.schema.json` | Opt-in finding feedback; not trusted review context |
| `review-category.schema.json` | Review category/lens configuration |
| `stage.schema.json` | Stage configuration |
| `concurrency-plan.schema.json` | Host-enforced concurrency policy |
| `evaluation-corpus.schema.json` | Versioned synthetic evaluation corpus |
| `evaluation-report.schema.json` | Privacy-safe evaluation result and metrics |
| `promotion-record.schema.json` | Real-provider promotion evidence |
| `recovery-artifact.schema.json` | Publication-only recovery artifact |
| `run-outcome.schema.json` | Structured run outcome and diagnostics |
| `candidate-finding.schema.json` | Provider-neutral candidate finding with bounded evidence; canonical path rules are enforced by `CandidateFinding.from_dict`, not the schema |
| `verification-result.schema.json` | Candidate evidence verification result |
| `coverage-manifest.schema.json` | Per-file and per-hunk review coverage |
| `compatibility-manifest.schema.json` | Cross-runtime release compatibility manifest |
| `canary-binding.schema.json` | Canary evidence bound to one compatibility-manifest digest |
| `channel-promotion.schema.json` | Audited workflow-channel promotion or rollback record (`v4_promotion` schema name is historical) |

Compatibility-manifest worker ranges support bounded numeric, caret, tilde, and
wildcard forms. An unqualified `x` or `*` is intentionally an explicit
all-non-negative-semver range; releases that need a compatibility gate should
prefer a bounded range. The manifest may also record the exact 40-character
workflow commit and a closed `provenance_kind`
(`github-artifact-attestation`, `pypi-trusted-publishing`, or
`npm-oidc-provenance`) while keeping the legacy open `provenance` string for
v1 schema compatibility. The JSON schema still accepts legacy open `provenance`
values such as `"signed"`, but the binding loader in
`validate_compatibility_manifest` is intentionally stricter: new binding
manifests must include `workflow_commit` and a trusted `provenance_kind`, and
legacy open provenance strings that are not trusted kinds fail at load time. A
checksum fetched beside an untrusted artifact is rejected.

Implemented now: validating and building that manifest, proving PyPI-primary
and executing-commit install identity, binding fixture/downstream canary
evidence from the #34 contract, recording serialized workflow-channel promotion/rollback,
and failing closed on mismatched digests, outdated Worker ranges, partial
publication, and in-flight tag movement. Operator-only / future: assembling
the complete multi-lane artifact set, live disposable-repository canary,
attestation verification at consumer install time, and actually moving `v5`.

The `$id` policy is fixed: the path after the package namespace must include
`/v1/` for v1 documents. Schema identity is the `$id`. Legacy review-result
documents do not carry `schema_version`; the newer operational contracts emit
their explicit `schema_version` marker.

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
non-monetary performance proxies. Reports also emit bounded `engine_digest` and
`prompt_digest` values used by promotion evidence.

`review-sensei promotion emit|validate` reads evaluation report files and
promotion records only. It never constructs a provider, never accepts live-model
or egress flags, and cannot mint `status=supported` from fixture reports.
`require_supported_promotion` is the fail-closed Python gate for promoting a
model, prompt, generation setting, or routing configuration.

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
- `review_sensei.ProviderAdmission`
- `review_sensei.AdmissionLease`
- `review_sensei.AdmissionOutcome`
- `review_sensei.AdmissionRejected`
- `review_sensei.AdmissionCancelled`
- `review_sensei.ReviewService`
- `review_sensei.FindingLifecycle`
- `review_sensei.IncrementalReviewPlan`
- `review_sensei.ReviewContextCache`
- `review_sensei.ReviewRun`
- `review_sensei.RunOutcome`
- `review_sensei.ResourceBudget`
- `review_sensei.RecoveryArtifact`
- `review_sensei.CandidateFinding`
- `review_sensei.EvidenceReference`
- `review_sensei.VerificationResult`
- `review_sensei.PublishableReview`
- `review_sensei.CoverageManifest`
- `review_sensei.TotalWorkBudget`
- `review_sensei.plan_change`
- `review_sensei.verify_candidate`
- `review_sensei.verify_candidates`
- `review_sensei.PromotionRecord`
- `review_sensei.engine_digest`
- `review_sensei.prompt_digest`
- `review_sensei.promotion_record_from_reports`
- `review_sensei.validate_promotion_against_report`
- `review_sensei.require_supported_promotion`
- `review_sensei.validate_promotion_record`
- `review_sensei.prepare_publishable_review`
- `review_sensei.format_candidate_finding`
- `review_sensei.load_repository_learnings`
- `review_sensei.load_learning_feedback`
- `review_sensei.summarize_learning_feedback`
- `review_sensei.learning_digest`
- `review_sensei.LearningFeedback`
- `review_sensei.LearningDiagnostic`
- `review_sensei.load_review_categories_from_dir`
- `review_sensei.load_stages_from_dir`
- `review_sensei.SymbolAwareContextSelector`
- `review_sensei.SymbolAwareContextPolicy`
- `review_sensei.SourceContextSelection`
- `review_sensei.SourceContextCoverage`
- `review_sensei.plan_review_execution`
- `review_sensei.outcome_for_plan`
- `review_sensei.errors.ReviewSenseiError`
- `review_sensei.CompatibilityManifest`
- `review_sensei.validate_compatibility_manifest`
- `review_sensei.build_compatibility_manifest`
- `review_sensei.prove_release_identity`
- `review_sensei.CanaryBinding`
- `review_sensei.ChannelPromotionRecord`

`RunOutcome.to_dict()` produces a JSON-compatible document that validates
against `run-outcome.schema.json`. `ReviewService.run` always returns a
`ReviewRun` with that envelope. `ReviewService.review` raises
`ReviewInputError` when resource budgets are exhausted; other failures surface
as `ReviewFormatError` or `ProviderError`. Statuses distinguish a clean review,
partial coverage, an intentional skip, provider or budget failure, publication
failure, and an already-published head. Resource budgets cap provider calls,
transport retries, structural retries, prompt/output bytes, and elapsed time.
Closed diagnostic tokens are enumerated by `PUBLIC_DIAGNOSTICS` in
`review_sensei.outcomes`; `cancelled` is reserved for host-layer cancellation
and is not emitted by `ReviewService` today. Structural correction remains one
bounded retry and is counted separately from transport retries via the
`structural_retries` field. Publication recovery loads an opt-in
`RecoveryArtifact`, revalidates
identity, expiry, integrity, and current publication policy, and never invokes
a model or writes trusted learnings or configuration.

`ReviewResult.to_dict()` produces a JSON-compatible document that validates
against `review-result.schema.json`.

Operational results always include the optional `review_status` key. Directly
constructed results default to `incomplete`; the trusted review service marks
its stage aggregate `complete`. Results parsed from legacy JSON without the
key are classified as `incomplete` and serialize explicitly, so approval paths
fail closed. Consumers that enforce the original v1 shape should treat this
additive field as an optional extension during the deprecation window.

When `--enable-symbol-context` is set, results may also include optional
`source_context` coverage (enabled flag, completeness, trusted-base snapshot,
languages, excerpt counts, and per-path outcomes). Per-path outcomes name the
bound that was reached, including `directory-truncated` for a capped
caller-directory listing and `relations-truncated` for a capped import or
caller candidate set. This field is omitted on the default document/learning
path. It never includes source text and does not change GitHub write, egress,
or approval defaults. The loader requires the schema's exact types and rejects
a malformed record rather than coercing it to defaults.

Results also serialize `evidence_policy`. Omitted or `legacy` identifies the
compatible single-pass comment mode. `confirmed` means `prepare_publishable_review`
replaced comments with candidates whose evidence exists in the exact reviewed
snapshot. Candidate text, excerpts, and source remain untrusted data: they
cannot expand tool, write, or egress permissions. Rejected, duplicate,
malformed, and insufficient-evidence candidates are unpublished, and
incomplete coverage is `partial` rather than a clean review. Published
confirmed findings include the claim, triggering conditions, severity
rationale, and bounded evidence locations—not hidden reasoning transcripts.

A second model's agreement is not proof and is not part of this contract.
Production enablement still depends on the evaluation policy in issue #33 and
the run/budget contract in issue #36. Automatic approval remains unchanged
except that an incompletely verified `confirmed` review cannot be approved.

`coverage_mode` is an additive v1 field (`full`, `incremental`, or
`fallback-full`) and is omitted for the default `full` mode.
`finding_lifecycles` is an optional array of `{fingerprint, state}` objects
and is serialized whenever records exist, including on a `full` review, so a
round trip does not reset lifecycle state. `ReviewComment` may include
`symbol`, `defect_kind`, and `evidence_id` for stable concern identity.
`category` is a presentation lens and is not part of that identity, so
reclassifying a finding does not open a second discussion; a comment without
`defect_kind` buckets under the canonical `unknown` kind. Legacy documents
without those keys remain valid.

An incremental pass whose reviewed set is empty returns without calling a
provider. Its `summary` is engine-authored rather than provider output and
carries no findings, and the pass charges no provider call to the resource
budget. Approval is unchanged: the shared finalizer remains the sole approval
writer and still refuses to approve while unresolved blocking ReviewSensei
threads exist.

### Coverage and finding locations

`ReviewResult.coverage` is an optional additive v1 object. `ReviewService`
always emits it. Legacy or third-party results that omit the field keep the
compatible approval path and are not blocked solely for missing coverage; once
`coverage` is present, incomplete or partial states block auto-approval. Every enumerated changed path has one outcome:
`reviewed`, `partially-reviewed`, `excluded-by-policy`, `unsupported`, or
`budget-exhausted`. Incomplete enumeration marks the plan incomplete and is
never fully reviewed. When enumeration is incomplete, ``ReviewService`` sets
``review_status`` to ``incomplete``; partial per-path outcomes with complete
enumeration use ``partial``. Both values block auto-approval via
``review-incomplete`` / ``review-partial`` and ``coverage-incomplete`` /
``coverage-partial`` blockers.

`ReviewComment.side` is optional. Omitted or `RIGHT` is a new-file line, the
legacy right-side contract. `LEFT` is a deleted old-file line. `FILE` is a
path-level concern and omits `line`. The GitHub publisher validates those
locations against the exact snapshot and retains unrepresentable findings in
the review body instead of dropping them.

Per-request `ReviewLimits` are unchanged. Opt-in `--orchestrate-large-changes`
partitions a larger change into bounded chunks under a separate
`TotalWorkBudget` (default 8 chunks and 8 provider calls). The CLI and
`plan_change` read ceiling widens to `TotalWorkBudget.max_total_diff_bytes`
(8 MiB) in orchestration mode; the default single-request path remains bounded
by `ReviewLimits.max_diff_bytes` (1 MiB). Related changed paths are named in
chunk instructions; trusted context and write permissions are not expanded.

### Finding classification and presentation

`ReviewComment` accepts optional, independent classification fields. `blocking`
is a boolean that determines whether the finding prevents approval;
when omitted, only case-insensitive preferred `critical`/`high` values are
blocking; missing, lower-severity, and legacy free-form values are non-blocking.
`severity` uses the preferred `critical`, `high`, `medium`, or `low`
values to describe likely impact; `fix_effort` uses `trivial`, `small`,
`moderate`, `large`, or `unknown` to describe remediation scope; and `category`
remains the configured lens id that produced the finding.
Classification labels are bounded by the provider-text limit, must be
printable, and are Markdown-escaped at render time; this keeps legacy values
readable without allowing control-character or structural injection or
oversized formatted publication bodies.

The provider-neutral presentation layer renders available labels on inline
comments and appends deterministic summary counts grouped by blocking state,
severity, and lens, plus a quick-win count for trivial/small effort. Results without classification
metadata keep their existing summary and inline body text byte-for-byte. The
GitHub publisher applies this rendering without changing marker hashing, exact
head checks, event selection, location validation, or duplicate reconciliation.

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

The built-in `openai-compatible` adapter targets an explicit HTTPS
`/v1/chat/completions` endpoint and requires a non-empty API key; it has no
provider fallback. The built-in `openrouter` adapter targets the allowlisted
`https://openrouter.ai/api/v1` Chat Completions endpoint, requires
`OPENROUTER_API_KEY` (resolved only at the CLI boundary), and applies a typed
`OpenRouterRoutingPolicy` (upstream provider slug, no fallbacks, deny data
collection, ZDR). Named registry profiles are deterministic presets:
`local-private`, `fast-triage`, `deep-verification`, `openrouter-sonnet`, and
`openrouter-gpt` (with `local`, `private`, and `local/private` aliases for the
first). Profiles carry bounded timeout/output-token budgets, `json_object`
structured output, endpoint/credential policy, optional OpenRouter routing
policy, `qualification_status`, and `permitted_fallback: none`. New OpenRouter
profiles start `unqualified` and cannot pass `validate_profile_promotion` until
separate qualification evidence exists. A stage JSON file may set
`provider_profile` to a canonical profile name. Unprofiled and `local-private`
runs cannot select a remote stage profile; a remote run may narrow a stage to
`local-private`. Routing never forwards one profile's credential to another
endpoint and never escalates models after a failure.
`ProviderSettings.for_profile()` never reads the environment or forwards a
credential to a profile that disallows it.

Ollama, fixture, and `openai-compatible` share a conformance suite covering
request/result shape, malformed envelopes, resource limits, timeout/cancellation,
redirects, missing credentials, rate limits, and sanitized errors. Observed
provider/model values are recorded on `ProviderResponse`; an optional
`revision` is recorded when the adapter can observe one (`system_fingerprint` or
response model id). `revision` is internal adapter metadata bounded by
`ReviewLimits.max_revision_bytes`; it is not part of the public evaluation-report
or `ReviewResult` schemas and is not persisted in run outcomes. Promotion flows
may aggregate an observed revision separately as `observed_revision`.
Fixture-only promotion records cannot certify a named profile;
`validate_profile_promotion` requires supported live evidence that matches the
profile's adapter and allowed models.

The adapter's default endpoint allowlist contains only `https://api.openai.com`.
An operator who intentionally owns a different HTTPS-compatible service must
construct the adapter with `allow_custom_endpoint=True`; this explicit opt-in
acknowledges that review data and the supplied bearer credential leave the
machine. The default opener rejects redirects and reuses a verified TLS context
from the system CA store (or a regular file named by `SSL_CERT_FILE`). Injected
openers are test/transport seams and are responsible for preserving the same
no-redirect policy. Ollama local and Cloud profiles are independent paths; the
Cloud profile is the explicit Ollama egress option.

## CLI Contract

The command is `review-sensei`. Supported flags are:

| Flag | Environment | Purpose |
| --- | --- | --- |
| `--version` | none | Print the installed ReviewSensei version and exit |
| `--diff` | none | Required unified diff file path |
| `--profile` | none | Named provider profile (`local-private`, `fast-triage`, `deep-verification`, `openrouter-sonnet`, `openrouter-gpt`) |
| `--provider` | `REVIEWSENSEI_PROVIDER` | Provider registry key (`ollama`, `openai-compatible`, `openrouter`, `fixture`) |
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
| `--enable-symbol-context` | `REVIEWSENSEI_ENABLE_SYMBOL_CONTEXT` | Opt in to bounded Python symbol-aware source context from trusted base |
| `--base-sha` | none | Trusted base commit SHA required when symbol-aware context is enabled |
| `--head-sha` | none | Untrusted head SHA recorded only as coverage metadata |
| `--symbol-context-allowed-path` | none | Allowed repository-relative path pattern for symbol-aware context |
| `--symbol-context-max-files` | none | Maximum files selected by symbol-aware context (default 16) |
| `--symbol-context-max-bytes` | none | Maximum total bytes selected by symbol-aware context (default 131072) |
| `--symbol-context-max-depth` | none | Maximum relationship depth (default 1) |
| `--no-learning-proposals` | none | Do not request durable learning proposals |
| `--orchestrate-large-changes` | none | Opt in to bounded chunk orchestration under the total-work budget |
| `--categories-dir` | `REVIEWSENSEI_CATEGORIES_DIR` | Review category directory |
| `--stages-dir` | `REVIEWSENSEI_STAGES_DIR` | Trusted-base stage directory |
| `--output` | none | Write JSON to a file instead of stdout |

Stage and category catalogs are trusted operator configuration. Hosted reviews
read them only from the reviewed trusted base checkout (the validated base
SHA), never from the pull-request head. Manual dispatch `stages_dir` /
`categories_dir` override repository variables `REVIEWSENSEI_STAGES_DIR` /
`REVIEWSENSEI_CATEGORIES_DIR`. Empty values keep the packaged defaults. Unsafe
paths, symlinks, malformed schemas, unknown keys or category references, and
budgets above the public ceilings fail before any provider call.

A pipeline stage whose configured lenses are all inactive for the changed paths
makes zero provider calls. A category-less stage that declares an independent
output (for example a summary-only stage) still makes one provider call. Cloud
and local CLI/workflow invocation share this applicability rule.

Generated callers pass `stages_dir` and `categories_dir` only when those inputs
exist on the public reusable runner they reference. Compatibility tests fail
before release if a caller `with:` key is absent from
`review-sensei-run.yml` `workflow_call.inputs`.

`REVIEWSENSEI_PROVIDER_MODE` defaults to `local` (alias for `local-ollama`).
Hosted backends are `local-ollama`, `cloud-ollama`, or `openrouter`; `local`
and `cloud` remain aliases. `REVIEWSENSEI_MODEL` overrides the model for the
selected backend. Selecting hosted `openrouter` is the operator egress
acknowledgement; the reusable workflow rejects `allow_unqualified_profile=true`,
does not forward `--allow-unqualified-profile`, and only runs models on the
published hosted allowlist. OpenRouter requires the customer-owned
`OPENROUTER_API_KEY` secret; Ollama Cloud requires `OLLAMA_API_KEY`.

`review-sensei --version` reads package metadata and prints an exact `X.Y.Z`
version. This is the version recorded by the portable manual GitHub Actions
workflow before a review runs.

The command also supports workflow helper subcommands for hosted runs:

```bash
review-sensei resolve-hosted-openrouter both
```

`resolve-hosted-openrouter` accepts `model`, `upstream`, or `both`. It reads
hosted workflow environment variables (`MODE`, `CALLER_MODEL`,
`HOSTED_REVIEWSENSEI_MODEL`, optional `BACKEND_MODEL`/`BACKEND_DEFAULT`, and
`OPENROUTER_UPSTREAM_PROVIDER`), validates the model against the published
hosted OpenRouter allowlist, and prints the resolved value to stdout. The
`both` action emits `model<TAB>upstream` with no trailing newline; the reusable
workflow depends on that exact tab-separated format when deriving
`OPENROUTER_UPSTREAM_PROVIDER`.

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

The command also supports bounded offline `doctor` and `plan` subcommands.
They share the same top-level command dispatch as `prepare-diff`, `evaluate`,
and `github` (`argv[0]` selects the parser) and never construct a provider or
write to GitHub.

```bash
review-sensei doctor --json
review-sensei plan --diff pr.patch --repository owner/repo --json
```

`plan` analyzes a supplied diff with the same bounded `analyze_diff` path as
review. Without `--diff`, the plan is incomplete rather than ready. Optional
`--base-sha` and `--head-sha` record snapshot identity when supplied.
`doctor --network` performs read-only GET probes and never mints a token or
sends a generation request.

The command also supports a `learnings` subcommand for advisory lifecycle
diagnostics and opt-in finding feedback. It never mutates approved knowledge,
never constructs a provider, and never treats missing feedback as approval.

```bash
review-sensei learnings diagnose --learning-root /path/to/target-branch --json
review-sensei learnings feedback --file feedback.json --json
review-sensei evaluate --mode fixture --corpus evaluation/v1/corpus.json \
  --compare-learnings --learning-root /path/to/target-branch
```

`--compare-learnings` is fixture-only. Selection reuses the same
`LearningStore.selectable_entries` rule a review uses, so active entries that
are superseded stay out of both. Reported precision/recall/false-positive
deltas are estimates from the same synthetic cases with and without selected
approved learnings, not causal proof from production feedback.

The `--compare-learnings` exit code is the with-learnings fixture result
(`with_learnings_passed`), matching plain `evaluate --mode fixture`.
`without_learnings_passed` is reported in the document but deliberately does
not affect the exit code: the without-learnings arm is a baseline that may
legitimately fail, and failing the command on it would block the very
comparison an operator runs to judge a learning. Read both fields when using
the comparison as promotion evidence.

`learnings diagnose` requires an explicit `--learning-root`. Per
[ADR 0005](adr/0005-lens-context-sources.md) the trusted target/base checkout
is always supplied; the CLI never implicitly scans the current directory.

`learnings feedback` reports `known_learning_ids_scope`. It is `store` when
`--learning-root` supplied an approved store and `unset` otherwise; when it is
`unset`, `known_learning_ids_without_feedback` is empty because no store was
loaded and must not be read as "every known learning has feedback".

Exit codes are stable:

| Code | Meaning |
| --- | --- |
| `0` | Review completed and output was written; `doctor` configured checks passed; `plan` ready; `learnings` diagnostics/feedback rendered |
| `1` | Input, validation, provider, formatting, or filesystem failure |
| `2` | `doctor` action required or diagnostic validation error; `plan` validation error |
| `3` | `doctor` requested probe unverifiable with current permissions; `plan` incomplete (no diff) |

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

## Issue-64 and Issue-97 publication contracts

`ReviewPublisher` reconstructs a typed `ReviewResult`, applies
`prepare_publishable_review` for the configured evidence policy, revalidates all
locations against the exact diff, rereads the open non-draft same-repository PR
head, and submits one App-authored review whose formatted summary and inline
comments append the fixed follow-up instruction `To discuss this finding, reply
with @sensei followed by your question.` and carry the marker
`<!-- reviewsensei:review:v1 repo=<id> pr=<n> head=<sha> result=<sha256> -->`.
The default evidence policy is `legacy` single-pass mode. `confirmed` publishes
only snapshot-bound confirmed candidates; unverified comments never become
findings. The formatted summary and inline bodies must fit their configured
`ReviewLimits`; the complete framed review body must also fit the GitHub
transport ceiling of 65,536 bytes before any POST is attempted.
The marker, App slug, exact head commit, and repository identity form the
idempotency boundary; stale, fork, duplicate, ambiguous, or invalid writes
fail closed.

Learning proposal files remain content-addressed by their full canonical
SHA-256 digest at
`.github/review-sensei/learnings/sensei-<16-hex>.json`. The publisher now keeps
one ReviewSensei-owned draft per source PR: its preferred branch is
`review-sensei/learnings/pr-<number>`, with a deterministic `-g-<digest>`
generation only when a merged generation still owns the stable ref. Every
generated body ends with the exact v2 marker
`<!-- reviewsensei:learning:v2 repo=<id> pr=<number> batch=<sha256> head=<sha> -->`.
The body also carries one sanitized `Source title: ...` line, binding the
descriptive title envelope to the source metadata used for that generation.
When a source PR is renamed, an existing draft is refreshed only after a
bounded authoritative issue-timeline read proves the old-to-current rename
chain using strictly chronological `created_at` values after the candidate
draft's immutable `created_at` boundary. Historical events before that
boundary, matching mutable PR text, or recomputed hashes alone never
authorize a metadata overwrite.
The batch digest covers the sorted, de-duplicated files that are still pending
against the latest default head, so identical reruns are no-ops and changed
batches refresh the same open draft.

Before any write, the publisher rereads the repository, source PR, and default
branch ref; validates same-repository non-fork ownership; and proves candidate
PR metadata, immutable commit provenance, complete tree delta, and canonical
learning entries. Commit provenance binds repository id, source PR, latest
base SHA, source head, batch, and SHA-256 hashes of the exact title and body.
Refresh commits use non-force ref updates. Merged history starts a fresh
generation and omits files already byte-identical on the latest base. Closed-
unmerged, deleted-unproven, fork-owned, manually edited, ambiguous, malformed,
or pagination-incomplete artifacts fail closed without creating a duplicate or
overwriting maintainer content. A deleted branch from a provably canonical
merged artifact is treated as retired, so a fresh generation may be created.
PRs are always drafts and are never merged automatically.

Historical learning-PR discovery scans at most 1,000 entries. It starts with 20
pull requests per page and, only for the transport's typed response-too-large
error, retries the same item offset with page sizes 10, 5, and 1. This preserves
the GitHub HTTP client's 512 KiB per-response ceiling without skipping or
duplicating candidates. An oversized single-item response, an overfull page, or
incomplete pagination fails closed.

Conversation replies require a standalone case-insensitive `@sensei` mention
from a human OWNER, MEMBER, or COLLABORATOR. Reply bodies are bounded and
validated before marker append; source update time, exact head, root-thread
identity, PR state, and fork state are reread before publication. The inline
reply marker binds source comment, updated-time digest, PR, and head.
The provider may return an optional boolean `resolve` decision. Missing or
false keeps the thread open; true is honored only for an inline thread whose
root comment is authored by the ReviewSensei App. The publisher maps that root
comment to GitHub's GraphQL review-thread id, rechecks the exact head, and then
performs an idempotent `resolveReviewThread` mutation. Issue-only comments,
human-authored roots, stale heads, ambiguous mappings, and malformed or failed
GraphQL responses never resolve a thread. A successful resolution is reported
as `replied_and_resolved`; the reply marker still permits a later retry to
complete a resolution that followed an already-published reply.
Generated review and comment-event execution supports both provider modes.
Cloud operations use GitHub-hosted compute and send bounded review or
conversation context to Ollama Cloud; local operations use the labelled
self-hosted runner and configured local Ollama service. After authorization and
before provider execution, ReviewSensei adds an App-authored `eyes` reaction to
the source comment. It removes that reaction after reply publication or another
terminal outcome. Each subsequent standalone `@sensei` mention is a new bounded,
idempotent conversation turn over the current thread and exact PR head. When a
reply returns `resolve: true` for a blocking root and the resolution mutation
succeeds, the same provider job invokes the deterministic exact-head approval
finalizer. It does not call the provider again.

All setup-v4 switches except `REVIEWSENSEI_AUTO_APPROVE` default to `false`.
Automatic approval defaults to `true` and can be disabled with
`REVIEWSENSEI_AUTO_APPROVE=false`. When automatic review and GitHub writes are
enabled, blocking findings publish as `REQUEST_CHANGES` and non-blocking
findings as `COMMENT`. The shared finalizer emits `APPROVE` only for an
eligible exact head with no unresolved ReviewSensei root classified blocking.
A later execution on the same head still requests changes after an earlier
approval if blocking comments remain, and it approves after an earlier change
request only once those roots are resolved. Non-blocking ReviewSensei
follow-ups and human threads may remain open; blocking or unclassified
ReviewSensei roots and `@sensei` replies remain ordinary comments except for
the review event above. Partial, incomplete, or summary-only artifacts are
always comments, even when approval is enabled. Draft, closed, stale, fork, or
App-authored pull requests are never approved. A malformed, unauthorized,
incomplete, or over-limit thread response fails closed before the write. The
approval marker deduplicates each exact-head approval while repeated comments
and approvals remain no-ops. The finalizer runs after review publication and
after an AI resolution of a blocking root. Manual thread resolution alone does
not trigger a workflow run. The generated caller may contain only the name-only secret mapping
`OLLAMA_API_KEY: ${{ secrets.OLLAMA_API_KEY }}`; no secret value is generated or
handled by the App setup boundary.

The approval decision is deterministic: explicit blocking findings or
unclassified canonical `critical`/`high` severity block approval; malformed or
unknown ReviewSensei roots fail closed. Inline comments and review summaries
render that same effective merge-impact classification. The policy exposes stable
blocker reasons for diagnostics while the GraphQL query requests only bounded
`isResolved` fields.
