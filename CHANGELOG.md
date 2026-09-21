# Changelog

## 0.6.0 - 2026-09-21

- Package cutoff pairing the movable public workflow channel `v5` with the
  hosted maintainer-command interface: PyPI `review-sensei==0.6.0` and npm
  `@reviewsensei/cli@0.6.0` with matching platform packages, published
  through GitHub OIDC Trusted Publishing. Installed callers must move
  `REVIEWSENSEI_VERSION` to `0.6.0`; the `0.5.0` package does not contain the
  `github command` CLI or the session-ledger publication flags used by the
  promoted reusable runner.

- Added the issue #146 hosted maintainer-command path to the public reusable
  runner: `@sensei` maintainer commands resolve to the `command` operation
  through the new optional `comment_body`, `comment_actor`,
  `comment_actor_type`, and `comment_association` inputs (existing callers
  keep starting without them), run through the broker-attested session
  ledger, and publish with `--github-session-ledger`. Generated setup
  callers, command documentation, and the default-policy cutover remain
  pending in issue #146.

- Added the issue #146 convergence-integration foundation: review analysis
  and publication share one checkpointed transaction bound to repository,
  pull request, base/head, configuration, policy, evidence, reservation,
  generation, and result digests, with retry and recovery from the stored
  validated result without re-inference or double-counting, failing closed
  on generation, ownership, identity, and schema mismatches (ADR 0052); a
  durable, integrity-covered convergence history stores bounded PR-wide
  round state with terminal, idempotent suppression and replay integrity
  checks; no-progress handoff derives from post-admission blocker identities
  in that history instead of caller-controlled `--no-progress` switches; and
  maintainer commands carry durable identity, one-use continuation grants,
  broker-issued hosted mutation grants, and ledger-side pre-write
  verification scoped to repository, pull request, and head. Installed
  defaults and the `legacy` publication path are unchanged.

- Added the issue #136 C7 sequential evaluation and observation-only
  shadowing: `evaluate-convergence` replays frozen synthetic sequences
  against the real session admission seams, publishes metrics and
  limitations, and never treats the cap as approval. `REVIEWSENSEI_REVIEW_SHADOW`
  observes an operator policy without changing GitHub events. The
  compatible publication default remains `legacy`. `REVIEWSENSEI_AUTO_APPROVE`
  stays unchanged.

- Added the issue #136 C4 baseline-aware verification planner: a complete
  compatible review becomes the last-assessed baseline. Later operator
  passes reuse ADR 0042 incremental plans and bounded related paths to
  cover existing concerns plus changed and impacted code. Late blockers
  require `new-regression` or `substantiated-missed-defect` and may record
  causal lineage. Omission is not a fix. Rebase, model, and policy changes
  invalidate the baseline. Doctor/plan display the scope. `legacy` and
  `REVIEWSENSEI_AUTO_APPROVE` stay unchanged.

- Added the issue #136 C3 durable session ledger: local filesystem and
  GitHub issue-comment adapters store bounded PR-wide round counters with
  integrity digests, CAS generation, reservation/commit/abort, expiry, and
  explicit missing/tampered state. Identity reuses the ADR 0042
  `repository` + `pull_request` pair. Doctor/plan display the record.
  Operator-mode GitHub publication may reserve and commit a counted round
  without refusing the review. `legacy` and `REVIEWSENSEI_AUTO_APPROVE`
  stay unchanged.

- Added the issue #136 C2 effective blocker-admission path: operator
  `advisory` / `merge-focused` / `strict` modes derive structured candidate
  facts, run the C1 evaluator before GitHub publication, preserve proposed vs
  effective classification, fold advisory observations into the review
  summary, and withhold `APPROVE` for human adjudication. Confirmed
  verification proves evidence locations only; it does not invent a failure
  condition or promote `blocking=false` findings. Published
  `blocker_candidates` and input `input_blocker_candidates` are distinct
  bases. Attribution uses each comment's side against new-file or old-file
  line maps. Admitted `effective_blocking` / `needs_human` stay runtime-only
  and are omitted from the v1 comment schema. Intersecting `auto_approve`
  with `automatic_github_review_events` can only withhold GitHub events.
  Named mandatory rules, specific violations, and required contracts are
  explicit; free-form `defect_kind` is not a qualifier. Operator-mode
  policies always use publication enforcement. Leftover blocker facts
  under display-only enforcement fail closed. Confirmed findings require
  caller-supplied blocker facts to admit. Derived facts bind comment
  identity.
  Omitted publication policy stays `legacy`. `--recover-from` uses
  `legacy` unless an explicit operator `--review-mode` is passed.
  Ambient `REVIEWSENSEI_REVIEW_MODE` cannot break recovery. Operator-mode
  recovery of a serialized result still fails closed when that flag is
  set. Advisory still posts a `COMMENT` review for folded observations.
  `legacy` and `REVIEWSENSEI_AUTO_APPROVE` stay unchanged.

- Added the issue #136 C1 review-convergence policy contract: versioned
  `legacy` / `advisory` / `merge-focused` / `strict` modes, deterministic
  blocker-admission and round-handoff evaluators, and doctor/plan display via
  `--review-mode` / `REVIEWSENSEI_REVIEW_MODE`. Existing installations stay on
  `legacy`. GitHub publication and `REVIEWSENSEI_AUTO_APPROVE` are unchanged.

## 0.5.0 - 2026-09-18

- Official first public package cutoff: PyPI `review-sensei==0.5.0` and npm `@reviewsensei/cli@0.5.0` with matching platform packages, published through GitHub OIDC Trusted Publishing. Setup-v4 callers follow the movable public workflow tag `v5` (`PUBLIC_WORKFLOW_TAG=v5`, `@v5`); package versions remain immutable `X.Y.Z` (`v0.5.0`). The broker still accepts `v4` during migration via `BROKER_ACCEPTED_PUBLIC_WORKFLOW_TAGS` / `brokerAcceptedPublicWorkflowTags`.

- Hosted GitHub setup selects the backend with `REVIEWSENSEI_PROVIDER_MODE`
  (`local-ollama`, `cloud-ollama`, or `openrouter`; `local` and `cloud` are
  aliases) and the model with `REVIEWSENSEI_MODEL`. `REVIEWSENSEI_PROVIDER_PROFILE`
  is no longer an operator setting. Hosted OpenRouter derives
  `OPENROUTER_UPSTREAM_PROVIDER` from `HOSTED_OPENROUTER_DEFAULTS` and validates
  the resolved default model (`DEFAULT_OPENROUTER_MODEL`) when `model` is empty.

- Documented ownership, licensing inventory, trademark usage, DCO-without-CLA
  contribution licensing, and the Community / Managed / Enterprise boundary
  (issue #89): `docs/ownership-and-licensing.md`, `TRADEMARKS.md`, and
  ADR 0040. Root MIT text and package license metadata are unchanged.

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
- Added the issue #35 compatibility-manifest contract that binds workflow
  commit, Python, npm, schema, and Worker identities with exact digests and
  trusted provenance. PyPI-primary and executing-commit fallback paths prove
  that identity; live disposable-repository canary and `v5` movement stay
  operator-only.
- Wired deterministic candidate-finding evidence verification into publication
  so only snapshot-bound confirmed candidates become findings. Legacy
  single-pass comments remain the default and are identified as `legacy`;
  incomplete verification is `partial` and cannot be approved. Evaluation
  comparison, model verification, and run-budget enforcement stay deferred.
- Closed remaining issue #42 gaps: explicit per-stage provider profiles, a
  shared adapter conformance suite for Ollama, fixture, and openai-compatible,
  observable revision recording, and CLI/schema documentation. Named profiles
  still do not fail over or forward credentials; installed workflows keep
  local/cloud Ollama defaults and do not pass `--profile`.
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
  review-time digest for incremental cache keys. `learnings diagnose` now
  requires an explicit `--learning-root` instead of defaulting to the current
  directory, per ADR 0005; the lifecycle selection rule is stated once in
  `LearningStore.selectable_entries` and reused by `for_paths`,
  `evaluate_fixture`, and the comparison; a renamed fixture quality key now
  fails the comparison loudly; and the feedback text output reports whether
  the absence list was enumerated.
- Added a reproducible source-distribution test contract and a downstream
  consumer harness (issue #34). Dist-safe tests run after `pip install
  dist/*.whl` from an unpacked sdist suite that cannot import checkout `src/`;
  checkout-only tests stay in the compatibility/quality lane. An operator-gated
  `downstream-canary` workflow placeholder documents live-canary steps without
  running them in required CI. `tests/fixtures/distribution-contract.json` is
  the single source of truth for required packaged entries, enforced against the
  built sdist by `scripts/check_sdist_contract.py`; the checkout lane fails
  first when a declared helper is missing from `MANIFEST.in` or a lane module
  imports an undeclared tests-root helper. The install smoke test reads the
  installed version instead of pinning `0.1.1`, with CI supplying
  `REVIEWSENSEI_EXPECTED_VERSION` to keep the exact-artifact assertion.

- Wired issue #40's opt-in, deterministic, bounded symbol-aware source context
  into the live review path. Default reviews still use documents and learnings
  only. `--enable-symbol-context` plus a trusted `--base-sha` selects Python
  relationships from the immutable base snapshot, records excerpt provenance
  and coverage outcomes, and fails closed on budget exhaustion. Head source
  remains untrusted data. Coverage names the relationship bound it reached
  (`directory-truncated` or `relations-truncated`), only Python sources count
  toward the caller-directory cap, and `source_context` loading requires the
  schema's exact types instead of coercing malformed input to defaults.
  Evaluation under #33 is required before default enablement.

- Added incremental review coverage and stable finding lifecycle identities
  for issue
  [#38](https://github.com/malsabbagh/review-sensei/issues/38). Reviews can
  re-analyze changed and related paths, emit `coverage_mode`, and track
  new/still-present/fixed/outdated/uncertain findings without treating a later
  omission as a fix. Finding identity uses path, symbol, defect kind, and
  optional evidence id, so reclassifying a lens does not open a second
  discussion. Cache state is optional, in-memory, and metadata-only.

- Wired structured `RunOutcome` envelopes, hard `ResourceBudget` enforcement,
  and publication-only `RecoveryArtifact` recovery through `ReviewService`, the
  CLI, and GitHub publication. Every run now distinguishes a clean review,
  partial coverage, an intentional skip, provider/budget failure, and
  publication failure without logging prompts, responses, or source text.
  Recovery republishes a retained validated result and cannot rewrite trusted
  learnings or configuration.

- Added explicit per-file/per-hunk coverage, deletion-aware left/file finding
  locations, and opt-in bounded chunk orchestration for issue #39. Per-request
  limits are unchanged; total-work budgets prevent silent truncation and
  unbounded provider calls. Partial or unknown coverage cannot be approved.

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

## 0.1.1 - 2026-09-08

- Fixed standalone HTTPS provider calls by bundling and using a trusted CA
  bundle in native npm executables.
- Bumped the Python distribution and all npm launcher/platform packages to
  `0.1.1`.

## 0.1.0 - 2026-08-02
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
