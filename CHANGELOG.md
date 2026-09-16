# Changelog

## Unreleased

- Bound model, prompt, and routing promotions to real-provider evaluation
  evidence. `engine_digest` and `prompt_digest` are now computed from package
  identity, engine limits, and stage templates including full category
  definitions; `promotion_record_from_reports` requires at least three
  independent live runs with unique `run.invocation_id` values for
  `status=supported`; and `require_supported_promotion` is the fail-closed
  release/docs gate. Fixture reports cannot mint supported promotions.
  Operators can emit or validate records with `review-sensei promotion` or
  `scripts/validate_promotion_record.py` without adding live or secret flags to
  CI.

## 0.1.1 - 2026-09-08

- Fixed standalone HTTPS provider calls by bundling and using a trusted CA
  bundle in native npm executables.
- Bumped the Python distribution and all npm launcher/platform packages to
  `0.1.1`.

## Unreleased

- Skip automatic ReviewSensei runs on draft pull requests instead of failing
  preflight with "pull request is not eligible for review". Review starts when
  a draft is marked ready for review.
- Added committed CodeQL SARIF fixtures and a CI proof step so a seeded
  warning or malformed report fails the findings gate, while clean Python and
  JavaScript/TypeScript reports pass. Fork pull-request CodeQL remains
  read-only, with no secrets and no `pull_request_target`.
- Extended `review-sensei doctor --network` with read-only loopback endpoint
  and model probes, optional repository-metadata checks that reuse an existing
  token, and compatibility-manifest validation. `plan` now records supplied
  base/head snapshot SHAs and still performs zero provider or GitHub writes.
- Added tag-ruleset readback for immutable `vMAJOR.MINOR.PATCH` tags and the
  movable `v4` channel, plus a validated `v4_promotion` JSONL record in the
  existing publication ledger. Live GitHub ruleset application remains
  maintainer-operator evidence.
- Closed remaining issue #30 gaps for trusted stage configuration: inactive
  category stages make zero provider calls while category-less independent
  output stages still run, generated setup-v4 callers now include the
  authoritative resolve-trigger job and stay compatible with the public
  reusable runner inputs, and custom stage JSON is loaded only from the
  reviewed trusted base.
- Closed remaining issue #27 gaps: publish steps now require success and are
  skipped when cancelled, confirm the live pull-request head with a bounded
  read-only `gh api` retry immediately before writes (SHA mismatch records
  `skipped_stale` and skips publish without failing the job; `401`/`403`/`404`
  fail closed immediately; other API unavailability uses exponential backoff
  with jitter then refuses publish; a malformed live SHA fail-closes), and expose an
  in-process bounded `ProviderAdmission` helper with lease release after
  cancellation or failure. Hosted GitHub Actions admission remains the
  PR-scoped reusable workflow concurrency group (`max_active=1`,
  cancel-in-progress for reviews). Logs for admission/cancellation stay
  metadata-only.
- Added issue #41 learning lifecycle diagnostics, opt-in finding feedback, and
  fixture with/without-learnings comparison. Existing learning files continue
  to load without the new metadata; superseded/retired entries stay off the
  prompt; diagnostics never mutate approved knowledge; absence of feedback is
  not approval; comparison reports estimates rather than causal proof.
  `LearningFeedback.from_dict` no longer coerces or drops non-string fields,
  `load_learning_feedback` re-checks `schema_version` independent of the schema
  layer, learning selection is shared through `LearningStore.selectable_entries`,
  and `learnings feedback` reports `known_learning_ids_scope` so an empty
  absence list cannot be read as approval. `evaluate --mode fixture
  --compare-learnings` keeps the fixture pass/fail exit contract instead of
  always exiting `0`, comparison deltas are restricted to an allowlist of
  quality keys, and `LearningStore.selection_digest` is the canonical
  review-time digest for incremental cache keys.
- Added the issue #103 `@reviewsensei/cli` npx launcher and five exact-target
  native package lanes. The launcher forwards the existing Python CLI without
  downloads or lifecycle hooks; native builds, tarball validation, npm SRI/
  provenance, platform-first publication, and version-forward rollback are
  documented but not published by this change.

- Added one transactional, sanitized correction attempt for invalid structured
  provider output, and changed learning-PR history discovery to bounded adaptive
  20/10/5/1-item pages so large pull-request bodies remain within the GitHub
  response-size ceiling.
- Aligned setup-v4 cloud and local execution so both provider modes support
  automatic/manual reviews, learning proposals, optional artifacts, and bounded
  multi-turn `@sensei` conversations. Published review summaries and inline
  findings instruct readers to reply with `@sensei`; authorized mentions now
  receive a temporary 👀 processing reaction that is removed after reply
  publication or another terminal outcome.
- Added issue #64's opt-in setup-v4 GitHub integration: public git-tagged
  reusable workflow callers, exact-head App-authored inline review publication,
  deterministic learning draft PRs, authorized bounded mention replies, and an
  issuance-only Cloudflare OIDC capability broker. Setup-v4 uses the protected
  public `v4` tag as its sole workflow channel; the broker resolves that tag and
  checks the runtime workflow SHA. Customer-owned `OLLAMA_API_KEY` is referenced
  by name only and all write/artifact switches default to false.
- Renamed the product identity from Code Sensei to ReviewSensei before public
  publication. The Python distribution is now `review-sensei`, the CLI is now
  `review-sensei`, and the import namespace is now `review_sensei`. The
  internal repository remains private; public URLs point at `reviewsensei.dev`
  and `malsabbagh/review-sensei`. Historical changelog entries and ADRs retain
  the original name for accuracy.
- Added a commit-oriented, fail-closed publication-boundary audit
  (`scripts/audit_publication.py`) that resolves an exact source commit and
  tree, applies the publication exclusion manifest, scans publishable blobs
  for credentials, private repository references, private-network endpoints,
  and unallowlisted identity metadata, and writes a deterministic redacted
  report. The publication workflow now runs the audit before dry-run and sync.
- Added an OIDC-protected GitHub App installation-token broker for issue
  [#38](https://github.com/malsabbagh/review-sensei/issues/38), with strict
  workflow policy, repository scoping, and fail-closed auditing.
- Added a narrow GitHub App JWT and installation-token authentication adapter
  for optional App-identity comments or reviews.
- Added GitHub App registration/branding, key rotation, revocation, and error
  contract documentation plus ADR 0013.

- Added community health files, issue forms, contributor onboarding, and
  maintainer governance guidance.
- Added a synthetic, privacy-scanned v1 evaluation corpus and report schema.
- Added credential-free fixture reviews and deterministic fixture evaluation.
- Added explicit live-model and remote-egress acknowledgements with offline CI.
- Added evaluation documentation, architecture boundaries, and ADR 0012.

## Unreleased

- Prepared reproducible package release metadata, archive validation, and a
  tag-driven OIDC/provenance/SBOM workflow for the first `0.1.0` publication.
- Added the provider-neutral, downward-only `ReviewLimits` contract for
  untrusted diff/request/provider data and publisher-facing results.
- Added strict NFC repository-path and Git C-quoted path validation, bounded
  one-pass diff analysis, bounded CLI/Ollama reads, sanitized failures, and
  stable first-wins exact-comment deduplication.
- Added migration and code-only rollback guidance; context root shorthand
  `path: "."` is replaced by explicit canonical sources.
- Replaced the hosted GitHub App architecture direction with an
  open-source-first product direction: Code Sensei runs in user repositories,
  primarily through GitHub Actions, without a hosted backend.
- Marked ADR 0006 as superseded for historical context.
- Added a portable immutable manual GitHub Actions workflow boundary:
  `code-sensei --version`, `code-sensei prepare-diff`, exact released package
  installation into a separate `RUNNER_TEMP` environment, bounded diff
  preparation without head checkout/execution, and a recorded installed-version
  artifact.
- Made local Ollama the default provider configuration
  (`http://127.0.0.1:11434/api`, no API key) and documented cloud provider
  egress as explicit opt-in.
- Added ADR 0011 for the portable immutable workflow decision.

## 0.1.0 - 2026-08-02

- Added repository-local learning entries and validated learning proposals for
  future GitHub draft-PR persistence.
- Added provider-neutral review domain models and validation.
- Added unified-diff changed-line parsing.
- Added Ollama local and cloud adapter.
- Added explicit provider registry for future adapters.
- Added host-independent review concurrency plans for latest-wins runs,
  per-pull-request provider serialization, and isolated non-review triggers.
- Added configurable multi-stage reviews with reusable category configuration
  files containing stable ids, titles, and focus items.
- Added a default architecture lens plus per-lens path applicability, approved
  learning selection, and bounded target-branch document context with SHA-256
  provenance.
- Added single-pass prompt rendering and fail-closed validation for empty,
  oversized, symlinked, duplicate, malformed, or unknown stage/category fields.
- Added segment-aware recursive lens globs and deletion/rename-aware changed-path
  selection so context is loaded only for applicable lenses.
- Added CLI, documentation, security policy, and CI.
