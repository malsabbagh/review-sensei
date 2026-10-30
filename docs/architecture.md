# Architecture

## Quarantined review lineage

[ADR 0115](adr/0115-quarantined-review-lineage.md) proposes an explicit isolated
successor for a recognized unresolved predecessor. The predecessor root, history,
uncertainty and accounting remain immutable evidence. The successor preserves all
historical assessment obligations and uses a genuinely new run's finite allowance;
individual authenticated decisions and normal current review gates are required
before approval. A closed complete census, exact owned-root fences and fresh scope
checks guard checkpoint and later startup writes.

The c7 body-limit contract also requires local reproduction of the complete
framing failure against authenticated original admission and immutable Git
context. Partial coverage and all raw candidates remain evidence. A separate
installed registration binds the complete original/refused execution census,
source/broker provenance and per-surface exposure. The literal-path contract
keeps its original complete-result predicate. Published-tail isolation carries
both publication origins independently; see [ADR 0116](adr/0116-published-tail-quarantine.md).

Retain future storage; admit full native tails before effects. Unknown writes never refund. Guarded readers share a cold cursor.

Storage includes all future decision/archive carriers within256 parts/8 MiB.
Native admission covers fresh complete censuses, permanent old allocation and
positive update exposure, and the complete current transaction tail. Each later
transaction recertifies. Evidence and operation comments share one collection;
separate bounds cover reviews, inline replies/threads, checks and reactions.
Physical transport guards check every native mutation. Exhaustion stops before
effects with history intact. Fixed old reactions and old-head checks do not
enter current authority readers. Acyclic provenance hashes bind execution scope
first, then actual qualification bytes; neither hash supplies authority.

The shared guarded cursor covers cold bootstrap, operation reconciliation and
publication readback. Every new cursor starts cold. Bounded prefix counts
control retry size; complete freshly fetched JSON supplies evidence. The
certificate includes failed probes, encoded objects and response metadata within
ten physical requests. Conservative cold-reader certificates cover other surfaces.

A published review with unfinished cleanup/accounting reports
`published_terminal_pending`, `accounting_terminal:false`, and an operational
failure. The publication remains truthful; no terminal marker is invented.
Conflicting historical decisions preserve the prior immutable settlement and
report its actual action and actor. Same-action, same-actor repeats are explicit
idempotence; no receipt attributes an unrecorded action to a maintainer.

Quarantine registry: append-only, empty; workflow disabled until reviewed exact one-shot registration. Release grants no activation.

## Repository caller accounting and current Actions authority

One caller HTTP allowance: root `api_request_limit` (default 512).
Only v3 purpose/source/repo/PR/head/run/attempt/job/workflow authority enables work.
Successors prove cessation; uncertain/abandoned capacity stays charged.
Archive predecessors unchanged: terminal or confirmed historical v2 receipt. Settle holds explicitly.
Only exact results/proven pre-entry aborts close actions; unknown work stays pending.
Historical readers require exact review/archive evidence, fresh authority and no writes.
Finite operation/page limits can refuse with caller capacity unused.

`review_run` and `action_run` own one lifecycle process across startup, review,
command/reply, publication and cleanup. `api_run_requests` charges immediately
before physical dispatch and persists owner-bound intent. `api_run_plan` allocates
finite work and protected confirmation/terminal capacity. Policy commit P and its
exact configuration bytes are distinct from reviewed base B and head.

Worker POSTs cost one caller request. Internal dispatches are observations;
Worker exchanges have independent 60-dispatch/25-second ceilings and bounded
upstream requests. Inference and checkout/install/artifact infrastructure have
separate limits. `run_authority` authenticates current bindings, and
`prior_rescan_publication` supplies historical evidence without write authority.
A media receipt alone does not provide typed terminal-result proof.
See [ADR 0111](adr/0111-caller-api-work-allowance.md).

## Default human inventory references

Schema 5 owned purpose-bound parts retain all admitted human instances, metadata and
original indexes beyond legacy capacity. References grant no authority: require independent
transaction, live repo/PR/base/head and numeric Bot. Inference/decisions/approval require
fresh complete original prose/index; receipt metadata never grants decisions. Retain all
approval/provider/blocker facts through resolution. Preflight future receipts/prose/frames.
Only original live admission starts writes; intent precedes data. Unknown/incomplete writes
hold without recreation. One original finite budget covers publication/reply broker,
storage/readers/fences. Missing/conflicting evidence withholds decisions. Require reader-first
rollout and trusted complete emission (ADR 0094).

Explicit rescan source admission is documented in ADR 0112 in the [ADR index](adr/README.md).

## Publication and recovery

Writers default to at most 64 findings; readers accept 250. Only trusted
`github.publication_inventory: complete` enables larger emission after reader-first
rollout. Preserve every identity/explanation within body/item/path/future-assessment
bounds; refuse overflow without truncation. Exact replay verifies prose.
Retain bound result bytes in owned PR comments before GitHub-ledger
`publication_pending`/review/check writes despite routing/diagnostics.
Never retain diffs/prompts/credentials or write when writes-disabled.
Recovery binds exact trusted transaction/admission/acceptance. One budget covers
reads/writes; unconfirmed writes need complete owned readback. Missing/conflicting
parts refuse. Unbound gains no checkpoint authority. See ADR 0093.

The packaged v5 schema validates structure; explicit rescan uses the trusted reader
and one original command read allowance.

## Explicit human file review (local, opt-in)

[Human file review](human-file-review.md) binds an owned exact result/root, complete inventory,
regular base/head blobs and current authorized source. Hosted composition reads canonical retained
state after broker authorization. V1 parts keep 32 files; groups keep 32 KiB and exact union coverage.
Media keeps caller accounting and independent Worker check ceilings.
First-directory subtree proofs reduce shared-prefix reads; ADR 0103 describes capacity tradeoffs.
Broker failures use the closed diagnostic catalog.
Native media approval follows the review endpoint's HTTP 200 contract; audit
comments use HTTP 201. Existing owned readback and current snapshot fences
stay in place. Protocol failures expose fixed operation/status fields without raw bodies.
Fresh source, permission, root, request, thread, qualification and full PR facts gate approval.
Current finding facts require an exact-result index and positive resolution proof; never infer
resolution from false flags. Never complete AI coverage/result/baseline/cache or carry across heads.
Unknown writes need exact owned readback; unproven entered actions refuse replay.
See [Proposed ADR 0103](adr/0103-bounded-hosted-media-confirmation.md) and [ADR 0077](adr/0077-explicit-human-file-review.md).

For the operational flows and separate analysis, persistence, reassessment and
approval bounds, see [Review pipelines and customization](review-pipelines.md).
That guide is a fixed snapshot of `a598d4a` (0.6.18), not a live mirror of this
architecture page. Refresh ownership and validation steps live in the guide itself.

## Provider response completeness and capability identity

Qualified library capabilities bind the exact credential-free endpoint, model,
provider and `bounded-complete-text-v1` adapter contract. Shared discovery and
reassessment check that identity before planning and dispatch. Ollama advertises
the contract only with explicit fixed-model `require_completion_metadata=True`:
it requires normal completion, exact model and bounded integer token usage before
returning text. Present incomplete, length-terminated or over-cap metadata also
refuses in legacy mode; absent legacy metadata retains its prior disposition.
This change activates no qualified profile or workflow. Remote tokenizer mapping,
producer authority and full lifecycle qualification remain separate gates. See
[ADR 0075](adr/0075-complete-provider-response-contract.md).

## npm launcher and standalone boundary (issue #103)

The npm surface is an adapter around the existing Python engine, not a second
implementation. `packages/npm/cli/bin/review-sensei.js` is the only package
that exposes the `review-sensei` command; it selects one exact platform package,
validates its fixed in-package payload, and forwards argv/process behavior.
`review_sensei.cli:main` remains the only review engine. Native one-folder
bundles are produced by the pinned PyInstaller spec in
`packaging/standalone/`, with JSON defaults and `review-sensei` distribution
metadata collected for `--version` outside a Python checkout.

The supported target table is deliberately closed: macOS arm64/x64, glibc
Linux arm64/x64, and Windows x64. Package assembly and independent tarball
validation are confined to ignored build/dist staging. Release publication is
version-serialized and platform-first through npm Trusted Publishing, with
launcher-last after exact registry version/integrity readback; PyPI remains an
independent lane. A
partial or compromised release is handled version-forward, never by moving a
tag or replacing a tarball. See [ADR 0031](adr/0031-npm-launcher-and-standalone-platform-packages.md).

## Boundary map

```text
trusted target/base checkout
     |                    |                        |
     v                    v                        v
LearningStore    RepositoryContextStore    SymbolAwareContextSelector
     |                    |                        |
     +------> ReviewLensContext          SourceContextSelection
                    |                        |
                    +----------- ReviewRequest <--+
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
                    OllamaProvider    OpenAICompatibleProvider
                                  |           |
                                  +-----------+
                                              |
                                              v
                         validated ReviewResult

(fixture adapter: test-only seam, omitted from production map)
                           |                   |
                           v                   v
          prepare_publishable_review   learning proposals ----> GitHub learning PR
                           |
                           v
              confirmed findings, or identified legacy comments
                           |
                           v
                    GitHub or other publisher adapters
                           |
                           v
                      structured RunOutcome

ReviewRequest ----> ReviewConcurrencyPlan ----> host scheduler
ReviewService.run ----> ResourceBudget admission ----> RunOutcome
ReviewConvergencePolicy ----> evaluate_blocker_admission / admit_review_result
                               (legacy: ADR 0032 events; operator modes: publication)
SessionLedger --------------> local JSON / GitHub issue-comment adapters
                               (C3 counters; C5 will enforce)
ReviewBaseline -------------> IncrementalReviewPlan + late classification
                               (C4 verification scope; C2 admits)
```

`ReviewService.run` emits versioned `RunOutcome`. Hard budgets cap provider calls,
transport retries, prompt/output bytes and elapsed time. One structural-correction
retry differs from transport retry. Opt-in `RecoveryArtifact` republishes
validated results without models or trusted learnings/configuration changes. See
[ADR 0043](adr/0043-structured-run-outcomes-budgets-and-publication-recovery.md).

An operator-ledger analysis checkpoint commits the logical review round before
GitHub publication. An identical retry whose publication is pending or failed
reports `publication_recovery_required` without another provider call or ledger
charge. It requires the original validated result and trusted admission contexts;
ledger baseline/digests cannot reconstruct them. Reusable runners publish
new or independently validated retained results. Opt-in publication-failure
diagnostics retain reviewed diff and trusted configuration/admission contexts up to seven
days, disabled by `artifacts: none`. Local analysis-only duplicates remain unchanged.
PR223 amendment to
[ADR 0052](adr/0052-logical-review-transaction-across-analysis-and-publication.md).

Opt-in recovery binds original trusted policy, exact base/head diff and authenticated
prior baseline. Configuration/evidence/policy/reservation must match committed
transaction. Missing admission/ordinary retries refuse. Both lanes skip provider
credentials/inference; retain one read budget, broker/accepted-result/ledger/head
guards and writer slot.
See [ADR 0097](adr/0097-explicit-trusted-publication-recovery.md).

The review service owns the business invariants. Provider adapters own protocol,
authentication, timeout, and response-envelope details. Publishers own GitHub
API authentication and comment/review delivery. `prepare_publishable_review`
sits between a validated `ReviewResult` and any publisher: `legacy` keeps
single-pass comments and identifies them; `confirmed` publishes only candidates
whose evidence exists in the exact reviewed snapshot. Unverified candidates
never become findings, and incomplete verification cannot be treated as a
clean review. Candidate text and snapshot contents remain untrusted data.

The versioned synthetic corpus flows through a bounded, provider-neutral
evaluation loader and `FixtureProvider` into the unchanged `ReviewService`.
Reports retain only aggregate, non-monetary metrics. Live endpoints are
authority-gated by the CLI; CI uses fixture mode.

## Quality and supply-chain boundary

Deterministic, provider-independent CI gates Ruff format/lint, mypy, compileall,
branch coverage, three packaged Draft 2020-12 contracts and immutable Action pins.
Build both distributions/SHA-256s; run unpacked-sdist dist-safe/downstream against
installed wheels outside checkout. CodeQL Python/JS/TS findings use deterministic SARIF.
Checkout-only scripts/.github/packages/deploy tests stay in compatibility/quality jobs.
All workflow permissions default to
`contents: read`, and the `required-checks` aggregate publishes the stable
`Required checks` status used by branch protection. The optional downstream
canary workflow is operator-gated and is excluded from that aggregate.

The public JSON schemas are structural contracts only. They reject malformed
shapes and unknown fields, but runtime loaders (`ReviewCategory`, `Stage`, and
`LearningEntry`) remain the semantic authority for path safety, placeholder
allowlists, cross-file references, and bounded values. Schema documents,
workflow text, diffs, comments, and model output remain untrusted input. CI tool
versions are direct, exact pins in `requirements/ci.txt`; the packaged runtime
depends on the pinned-compatible `jsonschema` library for public contract
validation. `review-sensei-github` depends on `cryptography` for GitHub App JWT
signing and OIDC verification.

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

`src/review_sensei/workflow.py` owns portable manual GitHub Actions. Before git it
validates untrusted `base_ref`/`head_ref`/`head_repository`/`review_sensei_version`/
diff limits. It fetches explicit refs with list argv, resolves verified base/head
commit OIDs, checks merge base and writes bounded triple-dot diffs without head
checkout/execution. Installed `review-sensei prepare-diff` exposes it without consumer
Code Sensei source.

The generated caller is a read-only bootstrap, granting only `contents: read`,
`pull-requests: read`, `issues: read` and `id-token: write`. It routes only on event
shape and sends event context to the configured public reusable tag. Backend, model,
endpoint, credential reference, runner and GitHub policy come only from its resolved
trusted configuration commit, never caller inputs. The reusable workflow resolves one plan from the
package, then runs the backend on the runner kind that plan requires: cloud
backends on GitHub-hosted compute, local backends on the operator's
self-hosted runner (labelled `ollama` in the example), which must have Ollama
and the selected model provisioned before dispatch. Provider egress is explicit
opt-in through the canonical `inference.backend` field (or the optional
`REVIEWSENSEI_PROVIDER` Actions variable); the local backend uses
`http://127.0.0.1:11434/api` with no API key, cloud backends use their fixed
allowlisted endpoints and require the matching repository secret. The workflow
does not accept an arbitrary provider URL input, so a dispatch-supplied URL
cannot redirect the provider credential. The workflow installs the exact
requested `review-sensei==X.Y.Z` package from PyPI into a separate
`RUNNER_TEMP` virtual environment. When that exact distribution is
unavailable, it installs the same version from the executing workflow commit
SHA in the public GitHub repository; other PyPI failures remain fatal. The
install step verifies the package metadata and dependency set before review.

## GitHub App setup migration boundary

The installation bootstrap is a narrow deployment adapter, not a repository
configuration owner. Generated setup files carry a `ReviewSensei setup version`
marker. Before creating a setup pull request, the Cloudflare Worker or Python
adapter reads only the three known generated paths from the trusted default
branch, with a 128 KiB per-file limit and strict UTF-8 decoding. Older
generated files, partial current setups, byte-exact managed v3 workflows, and
managed v4 workflows following another valid public tag produce a reviewable
migration PR; all current files are a no-op. A custom, malformed,
or future-version file produces a no-write result so repository-owned workflow
content is not overwritten. The migration branch is created once per base and
tag (`review-sensei/setup-v5-<base12>-<tag>`), is reusable only when its parent
is the named base SHA, its author is the ReviewSensei App, its content is
exactly current, and its comparison changes generated paths only, and is never
force-moved; it changes no path outside the generated set, preserving
learnings, secrets, existing variables, and unrelated repository files. See ADR
0021.

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
  from the target branch or target commit. Optional lifecycle metadata does not
  change that boundary; retired/superseded entries and finding feedback never
  enter the prompt. `LearningStore.selection_digest` is the canonical SHA-256
  of the review-time selection used by incremental cache keys;
  `learning_digest` is the scope-agnostic primitive it is built from and
  digests exactly the entries it is given.
- `review-sensei learnings diagnose` reports stale, conflicting, missing
  superseder, and supersession-cycle signals for a human decision and never
  mutates approved files.
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
  OpenRouter separates requested model, observed model/upstream and reported fingerprint.
  Optional `ProviderTokenUsage` has only three bounded reported counters; invalid
  optional metadata stays unknown. Aggregate only counters every attempt reports;
  failures/cancellation are attempted work. Retain full bounded family/variant/region/
  tier prefixes through canonical YAML/resolver/CLI/hosted plan; published mappings
  require the same upstream family. Trusted `ProviderSettings` forbids ambient policy
  replacement; named profiles retain exact binding; prefixes enter policy digests.
  DeepSeek/Morph evidence eligibility is not qualification/approval. See proposed ADR 0081.

Live quality and exact fixture conformance differ. Bounded controls keep complete fixture
inventory/identity and unchanged thresholds; promotion verifies both and all attempted live work.
Exclude fixture usage; recall counts misses. Old unbound reports parse but cannot newly authorize
support. Composite controls reject missing live engine/prompt identity with bounded diagnostics.
Strict adapter OpenRouter endpoint-metadata allowlisting precedes live dispatch/report construction
(proposed ADR 0082).
- `Stage` declares one provider invocation, its expected output sections,
  optional structured `ReviewCategory` values, and an optional canonical
  `provider_profile`. Per-stage profiles cannot expand a local/private run onto
  a remote endpoint.
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
- `ReviewComment.blocking`, `ReviewComment.severity`,
  `ReviewComment.fix_effort`, and `ReviewComment.category` are independent
  optional classification metadata. Explicit `blocking` controls approval;
  absent values block only for case-insensitive critical/high severity; missing,
  lower-severity, and legacy free-form values are non-blocking.
  Severity describes impact,
  fix effort describes remediation scope, and the existing category id is the
  single source for the originating lens.
- Provider-neutral presentation renders available classification labels on
  inline comments and deterministic blocking/severity/lens/quick-win counts in the
  summary, using the same effective merge-impact classification as approval. A
  wholly legacy result is rendered unchanged.
- Classification labels are bounded printable provider text, and GitHub
  publication validates the input overview against `max_summary_bytes` and
  budgets formatted review text with the overview allowance plus admitted
  finding prose, capped by the host-body ceiling. It validates
  each formatted inline body against `max_comment_body_bytes`, and the
  complete framed review body against its 65,536-byte transport ceiling before
  posting.
- `ReviewResult.learning_proposals` contains unapproved durable-context
  proposals. The core never writes them to a repository.
- `ReviewConcurrencyPlan` exposes two host-enforced scopes for a pull-request
  review: a latest-wins workflow group and a non-cancelling, single-slot
  provider group. Both are keyed by repository and pull request, so different
  pull requests can run independently. Non-review triggers receive an isolated
  workflow group and no provider slot. `ProviderAdmission` optionally enforces
  one group in-process for tests and local hosts, with bounded waiters and
  lease release. GitHub-hosted runs use reusable workflow `concurrency`
  groups instead.
- `ReviewResult.coverage_mode` is `full`, `incremental`, or `fallback-full`.
  The review service emits it on every validated aggregate. Incremental mode
  re-reviews caller-supplied changed paths plus related dependency paths;
  incompatible base, learnings, model, or configuration, missing cache state,
  or incomplete related context falls back to a full bounded review.
  Every admitted fallback preserves prior concern accounting: omitted,
  downgraded or ambiguously rediscovered concerns remain uncertain, make the
  review partial and retain the prior durable baseline until resolved.
- `ReviewResult.finding_lifecycles` records stable fingerprints and states
  (`new`, `still-present`, `fixed`, `outdated`, `uncertain`). A later pass
  that omits a finding does not mark it fixed. Exact `(path, line, body)`
  duplicates remain first-wins.
- Optional `ReviewComment.symbol`, `defect_kind`, and `evidence_id` feed the
  fingerprint. Line number, `category`, and exact prose are not the identity;
  an absent `defect_kind` buckets under the canonical `unknown` kind.
- `ReviewContextCache` is an optional in-memory, repository/PR-scoped metadata
  adapter. It is not a hosted service and never stores prompts or raw
  provider responses.
- GitHub publication keeps one review per exact head, writes v2 finding
  markers with fingerprints, and does not open a second ReviewSensei thread
  for an already published fingerprint. Any review carrying inline findings
  reads existing markers through one bounded GraphQL review-thread sweep
  before the write, on every coverage mode including `full`. Human comments
  and resolutions are left intact.
- `ReviewResult.coverage` is an optional additive v1 object. `ReviewService`
  always emits it. Legacy documents without the field have unknown coverage and
  cannot be auto-approved. Every enumerated changed path has one outcome:
  `reviewed`, `partially-reviewed`, `excluded-by-policy`, `unsupported`, or
  `budget-exhausted`.
- Npm named lockfiles receive raw hunk review with optional bounded member
  evidence (exact record SHA-256, supplied-diff hunk index, original side/line).
  Evidence never replaces source or certifies package identity, format version,
  graph completeness, or registry contents. Other recognized lock formats
  receive `unsupported` / `lockfile-format`; generated non-lockfiles retain
  their existing exclusion. Completeness is decided before lifecycle resolution
  and complete-pass caching, so unreviewed paths cannot be silently resolved.
  Filtered or summary-only pipelines with no executed review stage mark their
  otherwise reviewable material `unsupported` / `no-review-stage`; chunking
  cannot convert that absence of work into validated partial evidence.
- `ReviewComment` must use a repository-relative path. Right-side comments
  require a positive new-file line; left-side comments require a deleted
  old-file line; file-level comments omit `line`.
- `ReviewComment` and `ReviewResult` reject bodies, lines, counts, proposals,
  and serialized output above the selected `ReviewLimits` profile. Exact
  `(path, line, side, body)` duplicates are deduplicated first-wins in stable stage
  order, while distinct bodies remain separate.
- By default, every comment must target a validated left-side, right-side, or
  file-level location in the exact reviewed snapshot. Unrepresentable locations
  are retained rather than dropped.
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

`applies_to` defaults to the whole diff. CLI applicability covers deleted paths
and both rename sides independently of inline locations. Active lenses select
approved learning categories and optional/required repository sources: files or
directories with filters. `--context-root` falls back to trusted `--learning-root`,
never the ambient directory. The packaged architecture lens selects conventional
sources; other lenses need configuration.

Document selection resolves the active lenses together, deduplicates paths, and
keeps the existing global limits of 64 documents, 128 KiB per supplied document,
and 512 KiB of unique supplied content. Present explicit-file sources, all
required-source matches, and applicable `AGENTS.md` within declared sources are
reserved in full. Mandatory overflow fails before provider invocation. Optional
directory documents are ranked by references to each lens's changed paths and
overlap with changed file stems in document paths, headings, and text; canonical
paths break ties. Fitting foundational documents remain eligible even without
lexical matches. No implicit repository scan or category override is required.

Optional documents can become bounded verbatim line excerpts with source/payload
digests, original line ranges, method/version and loss warnings.
Extraction makes zero provider calls and cannot prove omitted requirements absent.
Discovery, inspection and extraction have separate bounds. Selection metadata
enters lens prompts and cache identity. Loss counts appear on stderr.
`--context-selection-output PATH` writes bounded path/provenance without text.
Context counts are independent of source coverage/approval. See ADR 0058.

Symbol-aware source selection is a separate opt-in policy
(`SymbolAwareContextPolicy`, `--enable-symbol-context`). Disabled by default,
it never expands trusted context. When enabled, `SymbolAwareContextSelector`
reads only the trusted-base snapshot identified by `--base-sha`. Head source
remains the untrusted diff. The documented language set is Python with
deterministic AST parsing; JavaScript/TypeScript receive a bounded fallback
and an explicit `unsupported-language` outcome. Selection is static: files are
never executed. Exhausted budgets fail closed; other incomplete coverage is
recorded on the review result as `source_context` without changing GitHub
write, egress, or approval defaults. Bounded relationship expansion reports
`directory-truncated` or `relations-truncated` so a capped graph cannot be
read as a complete one, and only Python sources count toward the
caller-directory cap. Default enablement requires evaluation under issue #33.

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
- Invalid JSON, stage output, categories, or learning proposals receive one
  sanitized provider correction attempt. The rejected attempt is discarded
  transactionally; a second invalid response raises `ReviewFormatError`.
- Provider transport failures, oversized responses, invalid provider protocol
  objects, prompt-limit failures, and aggregate result-limit failures are not
  retried by the structured-output recovery policy.
- Empty or malformed stage pipelines raise `ReviewInputError` before a provider
  is called.
- A model-supplied comment category outside a stage's declared ids raises
  `ReviewFormatError`.
- A structurally valid inline comment that targets an unchanged or deleted line
  is omitted with a sanitized diagnostic; valid comments and the summary remain
  eligible for publication. The publisher still revalidates every retained
  location against the exact diff and fails closed before any write.
- Invalid, oversized, duplicate, retired, or out-of-repository learning files
  are rejected or excluded before prompt construction.
- Missing required lens sources, unsafe paths, symlinks, secret-like files, and
  oversized document context raise `ContextLoadError` before provider use.
- Invalid concurrency identities are rejected before a host can use their group
  keys. The core does not create cross-process locks or persist queue state.
  `ProviderAdmission` is an optional in-process lease helper; GitHub-hosted
  runs use workflow concurrency groups instead. Admission and cancellation
  logs are metadata-only and omit prompts, diffs, and source content.
- Error messages avoid including prompts, diffs, API keys, or provider bodies.
- Expected failures from the bounded path, diff, request, transport, and result
  seams are static/sanitized and do not include supplied secret markers.

## Review admission without a PR-wide count

There is no automatic-review budget to spend: a legitimate changed-head
update is reviewable no matter how many rounds this pull request has already
completed, and no workflow pre-check, grant, or `--rounds` request exists to
extend or refuse an allowance. The reusable runner (`review-sensei-run.yml`)
starts the review CLI directly; the package still enforces work admission
immediately before inference through the durable session ledger
(`session.prepare_session_round`), and it rechecks publication identity and
authorization at the write boundary. What can still refuse a round is a live
condition, not a lifetime count: an operator pause, a no-progress verdict, or
the per-head failed-attempt retry bound. Counters in the ledger
(`completed_initial_reviews`, `completed_verification_rounds`) are diagnostic
history, bounded for storage only, and never decide admission.

## Future extension points

- Additional model adapters register with `ProviderRegistry`.
- A GitHub publisher can consume `ReviewResult`, reuse the GitHub App auth
  adapter in `src/review_sensei/hosting/github/`, and implement review creation,
  thread replies, and idempotency.
- The GitHub learning publisher converts validated `learning_proposals` into a
  single ReviewSensei-owned draft PR per source PR. It uses content-addressed
  learning files, a stable source-PR branch/marker, hash-bound commit
  provenance, latest-default-head rereads, and non-force refreshes. Merged
  generations start fresh and omit files already byte-identical on the latest
  base; closed, deleted, manually edited, fork-owned, or ambiguous artifacts
  fail closed. Only merged learning files are eligible for later reviews.
- A workflow or queue adapter can enforce `ReviewService.concurrency_plan()`
  without adding GitHub or provider-specific dependencies to the review engine.

## Open-source integration

The primary distribution is the open-source package and CLI. Repositories run
ReviewSensei themselves in GitHub Actions: checkout the trusted base, fetch the
head ref only to compute a bounded diff through `review-sensei prepare-diff`,
install the exact requested package from PyPI or, when that distribution is
unavailable, from the public repository at the executing workflow SHA, run the
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
creates the generated files and no repository variables at all; the only two
product overrides are the optional `REVIEWSENSEI_PROVIDER` and
`REVIEWSENSEI_MODEL` Actions variables, and `OLLAMA_API_KEY` remains a
user-managed secret.
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

`.github/workflows/release.yml` is a tag-driven Python/package release workflow,
not runtime application code. It validates a `vX.Y.Z` tag against package
metadata and the changelog, builds and inspects the sdist/wheel before any
external write, and uses a dedicated PyPI environment with GitHub OIDC Trusted
Publishing rather than a long-lived upload token. The build also emits
checksums, an SBOM, and GitHub artifact attestations before the publish and
GitHub-release jobs attach the immutable assets. Workflow permissions are
scoped per job and third-party actions are pinned to full commit SHAs. Active
workflow jobs use GitHub-hosted runners, including the full cross-platform
compatibility matrix.

Release Marshal is the existing preparation workflow and dedicated signer,
not a new service or dispatcher. Its App-token signed `vX.Y.Z` push fans out
`release.yml` (Python) and `publish-npm.yml` (npm), each at the immutable signed
source. Both independently verify the preparation receipt, exact source/CI,
core/npm manifest version and unique original run. Separate workflow lifecycles
allow Python finalizer retry while npm is waiting or reading registry state.
The default-branch manual npm publisher is retired for future releases.

npm keeps native builds/smoke tests, exact same-run artifact ID/digest,
source metadata and tarball attestations, signed source/ref/workflow and exact
original build invocation, protected approval, platform-first publication,
bounded registry readback and clean consumer verification. It verifies the
matching reviewed public Python release through authenticated metadata. Artifact selection
binds the original run. Both channels record separate authenticated job/artifact
result receipts. The Python publisher and build-attestation workflow identity
remain `release.yml`; hosted identity/versioning remain independent. Moving
npm publication to `publish-npm.yml` requires owner verification of all six
Trusted Publishers before activation; this implementation changes no persistent
trust, credentials or environment policy. Old immutable sources keep their
original workflow. npm publication follows successful Python finalization;
the two build graphs and run/retry lifecycles stay independent. An early npm
publisher holds until the public release is ready, then uses its original
bundle on retry. Public release metadata fits the existing Contents read grant;
draft visibility and Contents write are unnecessary. See
[ADR 0100](adr/0100-independent-release-channel-runs.md).

Release helpers expose fixed internal phase/outcome/timing observations without
raw exceptions or remote response bodies. Bounded numeric command/HTTP statuses
help separate source, artifact, API and Python-publication holds. These records
grant no authority and change no publication predicate or recovery behavior;
see the safe-diagnostics amendment in [ADR 0069](adr/0069-verified-draft-github-release.md).

For future signed releases containing this policy, npm channel completion also
depends on a separate five-target registry-installed consumer matrix after
publication. Signed qualifier outputs bind its exact version/source. Fresh
prefixes, caches and npm configurations isolate installation; the installed
native launcher executes credential-free fixture and exit-code checks without
a Python fallback. All five latest verification jobs participate in channel
acceptance. A verification failure leaves published packages immutable and the
channel incomplete; recovery retries verification in its original run. See
[Proposed ADR 0104](adr/0104-published-npm-consumer-verification.md).

For the next release, Python/docs builds produce the verified GitHub draft
without waiting for native/npm assembly; PyPI approval and publication release
that draft independently of npm. Major-channel admission verifies both engine
and hosted PyPI wheel/sdist publication attestations against signed immutable
source, publisher run/attempt and protected environments.
Retain signed engine provenance, installed-CLI/downstream tests and workflow drain.
npm remains an independently attested native-binary
distribution with its own protected approval and integrity readback. Both
complete successful channel runs at the same signed tag/source remain a Pages
condition, outside v5 admission. See
[ADR 0095](adr/0095-pypi-qualified-major-channel.md).

Publisher aliases require original run/source/ref/runner/complete-step equality.
Missing/ambiguous facts or newer distinct executions block qualification.
The current API attempt and original publishing attempt are separate receipt
identities. Validation code comes from the recorded protected default-branch
commit; the signed release source supplies the frozen workflow and manifest data.

Release operations, PyPI registration, signed-tag ownership, verification,
rollback/yank, and compromised-release response are documented in
`docs/releasing.md`. The workflow cannot reserve the PyPI project or decide
whether a maintainer should publish; those are explicit external operations.

Release Marshal keeps authored changelog notes when supplied and otherwise
generates escaped commit-title notes from the exact first-parent range since the
previous version's signed annotated tag. Notes, version metadata and generated
docs share the existing allowlisted direct App commit and receipt digest. Exact
prepared-source CI still precedes signing and owner publisher approval. See
[ADR 0071](adr/0071-generated-release-notes.md).

Supported release combinations are bound by a versioned compatibility
manifest: workflow commit, Python distribution digest, npm artifact digests,
public schema version, and Worker identity. Build output is valid only when
every named artifact is present with an exact SHA-256 digest and an explicit
trusted provenance mechanism. Missing or extra artifacts fail closed. The
PyPI-primary and executing-commit install paths prove that identity in
`review_sensei.release_manifest`; live disposable-repository canary and `v5`
tag movement remain operator-only. See [ADR 0038](adr/0038-release-compatibility-manifest.md).

## Public landing-page analytics boundary

`docs/site/index.html` is a static GitHub Pages artifact. The Google Tag
Manager bootstrap for container `GTM-5W7TJV38` is an intentional exception to
the otherwise self-contained page: normal visits request
`https://www.googletagmanager.com/gtm.js?id=GTM-5W7TJV38`, while browsers
without JavaScript receive the corresponding `ns.html` iframe. The remote
container can execute tags and is therefore code outside the repository's
reviewed commit.

Ownership is split explicitly:

- ReviewSensei maintainers own the bootstrap, container ID, data-layer
  contract, privacy requirements, and repository changes.
- Google Tag Manager workspace administrators own container versions, tag
  configuration, publish permissions, and emergency pause/revert actions.
- The GitHub Pages workflow owns delivery of the reviewed bootstrap after a
  merge to `main`.

The browser-to-Google request may expose normal request metadata such as the
source IP address, user agent, referrer, and cookies, plus any additional data
collected by the published tags. The page must not put source code, diffs,
prompts, review comments, credentials, or other sensitive ReviewSensei data in
`dataLayer`. Container tags must follow the site's privacy notice and
applicable consent requirements; adding a new data field or tag requires
maintainer review in the GTM workspace and repository policy review.

Controls:

- `GTM-5W7TJV38` and both Google endpoints are literal, reviewed source
  values; no visitor or workflow input can select a container or script URL.
- The standard loader and its noscript fallback are the only integration in
  `docs/site/index.html`; additional collection is visible in the container's
  published tag configuration.
- Workspace access and publish permissions are limited to designated
  maintainers, and published container versions are retained for audit and
  rollback.
- Changes to the integration or its privacy/data-layer contract go through a
  normal PR and the existing Pages deployment workflow.

Rollback is code-only for the repository integration: revert the snippets in
a reviewed PR, merge to `main`, and verify that the deployed page no longer
requests the GTM endpoints. For an active incident, the GTM workspace owner
can pause the container or restore the last approved container version before
the Pages revert completes. No review-engine data migration is required.

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
ledger entry recording both SHAs and a mode-aware publishable tree hash. The
hash covers sorted path, tracked Git mode, and blob-digest records, so a
mode-only executable-bit change is a distinct publication identity. The
internal `publish` GitHub Actions
workflow gates the audit, dry-run, and sync behind maintainer approval; the
workflow and operator runbook are intentionally excluded from the public tree.

Deferred hosted review service: the hosted GitHub App architecture in ADR 0006
is superseded for now. The Cloudflare package is only an optional installation
bootstrap ingress; customer Actions still own review execution and provider
compute. A hosted review service would require a new ADR and a fresh issue set.

## OIDC installation-token broker

Optional OIDC brokers stay outside review logic. `oidc.py` verifies Actions identity;
`broker.py` gates `GitHubAppAuth`. Keys stay managed; no diffs/provider output.
Reject forks, missing/suspended installations, rate limits and failed evidence.
Worker exchanges/upstreams stop at 25s/10s or earlier caller/control deadlines.
Abort I/O; cache completed JWKS only. No POST replay; allowlisted diagnostics only,
never secrets or remote text.

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

## Issue-64 setup-v4 tagged publication architecture

The proposed [feedback mutation protocol](adr/0073-authenticated-feedback-mutation-grants.md)
adds a distinct version-2 session grant. The Worker transiently reloads complete
human sources to verify numeric actors, exact snapshot and selection digests;
credentials and ledger records retain metadata and hashes. It does not receive
review diffs or provider responses. This grant alone cannot authenticate original
attempt accounting or authorize the disabled richer hosted queue route.

Setup-v4 separates the customer caller, public execution workflow, and
issuance-only Worker:

```text
customer setup-v4 caller (operator-managed @v5 tag; all five opt-ins false)
    -> public reusable workflow
       -> trusted-base checkout and bounded diff
       -> PyPI package or executing-SHA public GitHub source fallback
       -> provider-selected cloud or local review/reply runtime
       -> typed result/reply validation
       -> Actions OIDC assertion
          -> Cloudflare POST /github/token
             -> exact @v5 ref + tag-resolved runtime SHA + installation resolution + metadata-scoped repository/fork checks
             -> hashed replay/rate Durable Object claim
             -> one least-privileged capability token
       -> App-authored exact-head review (COMMENT or eligible APPROVE), learning PR, or authorized reply
          -> temporary eyes reaction around each authorized provider turn
```

The generated setup PR is limited to the workflow caller, uninstall workflow,
and config file. The operator-managed v5 tag is validated during
installation/reconciliation and remains in the generated caller; the broker
resolves the tag at capability exchange time and checks the runtime SHA. The workflow may
receive the existing customer-owned `OLLAMA_API_KEY` only through a literal
name-only secret mapping. The setup App does not access that value. The Worker
does not receive source, diffs, prompts, review output, reply content, provider
credentials, or installation-token values for persistence; its ledger retains
only hashed identities and bounded counters. Current, custom, malformed, and
future clients are no-write cases. A byte-exact managed v3 workflow or managed
v4 workflow following another valid public tag is stale and is migrated; absent
and legacy/v2 clients are also reconciled through at most one reviewable
setup-v5 PR per selected repository.

The generated caller is invocation-only: backend, model, endpoint, credential,
and every `github.*` policy resolve from `.reviewsensei.yml` on the trusted
policy commit, with `REVIEWSENSEI_PROVIDER` and `REVIEWSENSEI_MODEL` as the
only product overrides. Local, cloud, and OpenRouter runtime jobs expose the
same operation matrix—automatic/manual review, review and learning publication,
optional artifacts, and bounded `@reviewsensei` replies—while retaining different
compute and egress boundaries. `local-ollama` uses the labelled self-hosted
Ollama runner; `cloud-ollama` uses the hosted Ollama Cloud lane; `openrouter`
uses the hosted OpenRouter lane for allowlisted models. Hosted reusable jobs
default to GitHub; an operator may select one validated trusted Linux label
through `hosted_runner` (ADR 0062), including read-only bootstrap. Local Ollama
retains its existing label array. Backend, model and policy still
resolve from trusted configuration, not this infrastructure input.
Older setup-v4 callers remain recognized as managed content and migrate through
the existing reviewable setup PR path.

Independent retained `TrustedOpenRouterQualification` evidence gates OpenRouter approval.
Evaluation/runtime digests differ; profile/caller/old `not-required` proves nothing.
Facts v2 keep provider provenance/bounded exact-head/result receipts. All finalizers require
matching App-owned eligibility readback and configured authority rechecks.
Legacy facts parse without exemption; other providers' new reviews retain policy.
Default CLI/hosted composition withholds until a trusted host explicitly wires
the evidence authority. See [the runtime boundary](openrouter-qualification.md#runtime-approval-boundary-115)
and [ADR 0083](adr/0083-trusted-runtime-openrouter-qualification.md) for scope,
independent inputs and legacy recovery limits.

Live reports must bind actual requested routing-policy digest/base URL to selected target;
missing bindings or changed suffix/endpoint refuse support creation/loading. Old
reports parse but cannot newly authorize unbound live support; historical targets
remain eligible. Runtime approval binding remains missing. See ADR 0081.

Before an enabled review reaches either provider, the reusable workflow runs a
read-only authoritative pull-request preflight. It validates the repository
numeric id, open/non-draft state, same-repository non-fork head, exact head
SHA, base ref, and (when supplied) base SHA, then exports the API's canonical
base SHA and a bounded standard-base64 title. The provider checks out that
canonical base commit. Reply operations use a no-op preflight and cannot
select a review step; unavailable GitHub metadata fails closed with an
explicit error rather than allowing a stale or ambiguous review.

The automatic-review and GitHub-writes caller path defaults to automatic
approval. Review publication is one exact-head review, and
enforcement is one stable `ReviewSensei` check run bound to that head and to
its producing App (ADR 0057). A completed `auto-approve` or `blocking` review
publishes `failure` on the check when required fixes remain, `action_required`
when required human decisions remain without required fixes, and `success` when
all required fixes and required decisions are satisfied; `auto-approve` submits
`REQUEST_CHANGES` when required fixes remain and `COMMENT` otherwise, while
`blocking` publishes `failure` when
required fixes remain and stays `COMMENT`. `advisory` publishes `neutral`,
stays `COMMENT`, and never fails the check. `action_required` covers a
partial, incomplete, or unpublished review in `auto-approve` and `blocking`,
and `cancelled` is a run that ended without a conclusion. A pending review
first writes `in_progress`, so an earlier conclusion on the same head cannot
stand for a review that has not finished. A repository administrator must mark the check
required for it to gate merges in `blocking` mode; `doctor` reports the check identity and
producing App. Checks: write is a scoped broker capability
(`check_publish`), so a missing permission leaves the check unpublished and
reports `check_capability_unavailable`; it does not prevent approval for an otherwise
eligible review. Repositories that require this check must grant the
permission and broker capability for the merge gate to be enforced.

A shared deterministic finalizer then emits `APPROVE` only when an eligible
exact-head PR has a complete, qualified review with no unresolved ReviewSensei
root classified blocking. Non-blocking ReviewSensei roots and human threads may
remain open. Blocking or unclassified ReviewSensei roots, draft/closed/stale/
fork targets, App-authored PRs, and malformed or unavailable thread state
withhold approval or fail closed, and the withheld decision carries a bounded
diagnostic. The classification sweep asks only for bounded root data, is capped
at ten pages, and runs before a final PR preflight. The finalizer runs after
review publication and after the AI resolves a blocking root, and it re-reads
the persisted eligibility document instead of trusting a caller boolean. The
existing `pull_requests: write` capability and per-head approval
marker/idempotency boundary are shared, so approval adds no credential or
persistence boundary; ReviewSensei never calls a merge endpoint or enables
auto-merge.

Recorded-decision reconciliation discovers the complete bounded native thread
inventory before changing threads. Matching already-resolved roots are observed
without repeating mutation fences, so a resolved prefix cannot consume every
subsequent verification window. Unresolved-thread writes still recheck the root,
decision and live PR immediately before mutation. Final approval independently
rechecks current eligibility and thread state; resolved observations do not grant
coverage, waive missing decisions, or authorize stale source. Request and time
limits remain unchanged, and a later authenticated verification can continue
from durable decisions and the current native resolution state.

[ADR 0059](adr/0059-dependency-review-and-partial-publication.md) permits
validated partial analysis to cross the identity-bound checkpoint/publication
boundary only with complete enumeration and evidence of reviewed work. The
same repository/PR/base/head/policy/configuration/result-digest checks apply to
first publication and crash recovery. Partial checkpoints charge one failed
attempt, preserve the last complete baseline, and do not increment completed
review counters or create convergence/no-progress markers. Successful transport
preserves the `partial` run outcome and approval/check limitations; incomplete
or wholly failed analysis remains fail-closed.

[ADR 0060](adr/0060-stale-baseline-full-review-recovery.md) permits bounded
fresh full analysis when a valid durable baseline loses incremental reuse
compatibility. The analysis attempt is deterministic for the exact current
base/head, configuration and context, so a same-head base change runs once.
The prior baseline stays in trusted publication admission inputs; fallback
findings retain human adjudication. Only a validated complete checkpoint
replaces the baseline. Missing, malformed, foreign or future history stays
blocked, and partial or failed work preserves the last complete baseline.
Closed operational diagnostic tokens reach stderr and Actions summaries
without requiring retained artifacts.

Issue #136 adds a versioned review-convergence policy (ADR 0046) that doctor
and plan can display. Issue #146 F7 (ADR 0055) makes `merge-focused` the
runtime default and retires live `legacy` selection, so the policy is now bound
into publication: operator modes run the blocker-admission evaluator before
GitHub review events and comment rendering, and only historical `legacy`
records keep the explicit finding `blocking` bit described above.
The approval choice is `github.reviews` in `.reviewsensei.yml`: `blocking`
publishes and enforces without ever approving, and `advisory` imposes no
ReviewSensei merge gate.

F1 of issue #146 adds an identity-bound `ReviewTransaction` handoff for
operator-ledger runs. Analysis reserves once, checkpoints the validated result
and increments the logical round once, then GitHub publication advances the
same durable transaction through pending, failed, or succeeded phases without
another provider call or counter increment. The result and ledger carry only
bounded repository/PR/base/head, policy/configuration/evidence digests,
reservation, generation, phase, and result-digest metadata; publication
recomputes the trusted context before any broker or publisher write. Legacy and
no-ledger callers remain on their existing compatibility path until the later
issue #146 workflow/default slices.

Conversation turns are authorized before capability exchange or provider
execution. The reply capability adds an App-authored `eyes` reaction to the
source comment, the provider receives the already-bounded thread/diff/findings/
learnings context, and a `finally` cleanup removes the reaction after reply
publication or another terminal outcome. The validated reply may also carry an
explicit `resolve` decision. Only a ReviewSensei-authored inline root can be
resolved; the publisher rechecks the exact head, performs a bounded GraphQL
root-to-thread lookup, and confirms an idempotent `resolveReviewThread` result.
Issue comments, human roots, stale heads, and malformed or unavailable thread
responses remain unresolved. Reply markers still provide idempotency, so a
retry can reconcile an already-published response and finish a pending
resolution without creating a duplicate. After a successful AI resolution of a
blocking root, the adapter invokes the shared finalizer on the same exact head;
this avoids a second provider pass, synthetic commit, or Actions-dispatch
capability while preserving the normal approval gates.

Maintainer finding lookup distinguishes empty App `COMMENTED` review containers
from genuine legacy finding authority through bounded review-specific reads.
Only confirmed owned replies or a complete empty comment list permit fallback.
Genuine top-level legacy findings retain their exact-head inventory authority.
Unknown, malformed, conflicting or unreadable roots refuse fallback.
The metadata walk retains one shared ten-request, sixty-second bound.
Fetched candidate inventory is reused rather than read twice. Nonempty newer
records and other review states keep their existing fail-closed behavior; see the
amendment to [ADR 0050](adr/0050-maintainer-disposition-and-handoff-status.md).

Pending `needs_human` findings follow the separate evidence-backed reassessment
path in [ADR 0061](adr/0061-human-reply-reassessment.md). A new review records a
complete bounded inventory in its App eligibility document. An authorized
explicit `@reviewsensei` explanation and validated provider assessment can refresh
only the human flag for that exact base/head and result. Literal human/diff
citations establish provenance; the model supplies semantic judgment. Fresh
source authorization, latest persisted eligibility and all existing approval
gates are checked before approval. Legacy, incomplete, stale or unsupported
inventories keep withholding; no session baseline or cross-head disposition
changes. Supported resolution uses the existing review publication capability,
with the issue-reply token kept on conversation operations.

[ADR 0065](adr/0065-complete-human-inventory-and-approval-retry.md) distinguishes
assessment identities from lifecycle concern fingerprints. Colliding concerns
retain separate identities derived from their validated v1 comments;
identical comments coalesce. Invalid or oversized inventories fail before
publication instead of silently disappearing. A missing legacy inventory or a
malformed latest authority record returns an actionable full-review error,
without generating ordinary chat. Published prose and short finding IDs do not
prove a complete inventory bound to the original result digest. Once a complete
inventory is durably reassessed, retries re-enter the existing exact-head
finalizer without another inference or reassessment review. All other eligibility
facts, current authorization, live thread checks and approval markers remain
authoritative.

Accepted replay binds complete immutable inventory, original result/facts, source identity
and exact evidence. Resolutions may accumulate; changed source/evidence/authority forbids reuse.
Revalidate full evidence; skip only durable resolved IDs. No inventory bypass or provider refill.
Unbound old payloads need a new authorized mention (ADR 0061; [finite matrix](validation/large-lifecycle.md)).

The default reassessment path prioritizes the complete supplied current patches for validated
pending paths within the existing 12 KiB budget. Missing, conflicting or
oversized required evidence produces a deterministic acknowledgment and a
separate insufficient-evidence outcome, with no inference, review capability or
eligibility/approval mutation. Ordinary conversation selection remains unchanged.
Provider output rejection exposes a closed reason code without source or model
payload, and accepted decisions still require verbatim human and relevant current
diff evidence. The legacy service permits at most one fresh JSON-syntax correction
within the original resource tracker, provider token limit, 48 KiB per-request
prompt and 16 KiB per-response bounds. The application passes its existing
tracker through preparation and both calls; counters and deadlines are never
reset. Schema/citation rejection and provider transport/completion failures do
not trigger this syntax correction. Safe parser diagnostics contain only fixed
reason labels, numeric line/column/offset, response byte count, correction status
and a closed budget reason. Raw output is neither echoed in the correction nor
logged. This does not add durable replay authority to the legacy path.

[ADR 0067](adr/0067-shared-review-work-mechanics.md) introduces the opt-in
`advanced.review_work.mode: unified` mechanism. Both full discovery and human
reassessment use immutable snapshot-bound evidence, one deterministic rendered
request planner, and one executor for provider calls, retries and aggregate
budgets. Mode adapters retain distinct prompts and validators. Complete file
groups are atomic for reassessment; exhaustive parsed hunks are permitted only
for discovery. Publisher validation uses the original per-batch evidence map,
without concatenating it into the bounded conversation diff field. Failed or
unprocessed work retains pending findings and conservative coverage.

Unified reassessment permits one bounded structural correction per batch within
the shared retry/call allowance. Citation, rationale and reserved-marker failures
do not trigger semantic correction retries. Explicit cross-file requirements use
eligibility payload version `2.0` inside the recognized `eligibility:v1` envelope;
the facts remain v1. Upgraded readers accept v1 and v2, and a latest unsupported
record blocks older authority. Normal review publication continues writing v1.
The work-policy digest extends transaction configuration only for opt-in runs.

Partial-result continuation also requires a persisted source-context identity at
the public `ReviewService.run` boundary. A domain-separated digest binds the full
request, effective stage/provider/model routing, review profile, work/resource
budgets, capabilities and incremental plan. The service checks it before planning
or reusing coverage, including when a direct API result has no transaction.
Continuation must verify full input scope; absent/changed scope refuses reuse.
Same-input serialized continuation works.
Nested chunk runs bind their aggregate to the outer request.
The digest remains separate from the frozen v1 content digest and existing
transaction fences, and consumes bytes under the original result ceiling. See
[ADR 0102](adr/0102-bind-partial-continuation-to-review-input.md).

Closed context requests permit one host-validated joint or discovery wave using
the same planner and tracker. Broader discovery uses configured full-review
policy and its normal publisher; the merged eligibility retains exact prior
findings, blockers, coverage and qualification. Source reply, latest authority
and base/head fence mutations, and result replay preserves persisted concerns.
Explicit opt-ins expose a private HMAC-authenticated v2 receipt store; admission
is saved before dispatch and restored without resetting calls or deadlines.
Artifacts retain normalized receipts and exact evidence only under diagnostics
policy, with bounded size/count/expiry and current semantic validation. See
[shared work operations](shared-review-work.md) for CLI and retention controls.
The reusable workflow serializes provider work per PR across full/reply/command
modes and backends without cancelling active siblings; GitHub's single pending
slot can replace queued work, which cannot clear a pending finding.

The offline pinned-reader gate, `scripts/check_review_reader_compatibility.py`,
uses the immutable published v0.6.16 source. That reader refuses richer eligibility
and opt-in transaction contexts, but skips session comments exceeding its 16 KiB
ceiling. Upgraded readers treat oversized trusted terminal markers as unreadable
authority and prohibit initialization. Expanded writers require quiescing older
readers/writers; rollback retains upgraded readers rather than resetting state.

This architecture preserves the provider-neutral core: GitHub transport,
Actions OIDC, broker capabilities, setup lifecycle, publication markers, and
conversation authorization remain under `src/review_sensei/hosting/github/`
or `deploy/cloudflare/`.

### Learning publication lifecycle (Issue #97)

Learning publication is a hosting-side reconciliation boundary. The source
PR's numeric repository id, PR number, head SHA, and default base head are
reread before each mutation. Open PR discovery uses the stable `(repository id,
PR number)` marker identity and proves the generated tree from immutable Git
objects; the batch digest is only the pending content identity. A refresh
creates a provenance-bearing commit with parent `H` (and `R` when the default
advanced) and updates the branch with `force: false`. A new generation is
created only after a merged prior generation, and its branch is created at the
final commit so an interrupted run leaves a bounded, recoverable orphan.
Metadata contains only the source link, one sanitized source-title line,
reviewed head, proposal summaries, approval warning, and exact marker.
Title/body hashes plus the source-title line in the commit-bound rendering
prevent manual edits from being overwritten. A source-title transition is
accepted only when a bounded authoritative GitHub issue-timeline read proves
the old-to-current rename chain with strictly chronological `created_at`
values after the candidate draft's immutable `created_at` boundary; a
self-consistent PR title/body cannot prove its own ownership. Candidate state
is revalidated after discovery and before any Git-object or ref mutation, so a
close/merge race has a no-write outcome.
Historical candidate discovery preserves the 1,000-PR ceiling while requesting
20 complete pull requests per page initially. A typed response-size failure
reduces the page size through 10, 5, and 1 while resuming at the same item
offset, keeping large PR bodies inside the GitHub client's unchanged 512 KiB
per-response bound without duplicating or skipping candidates. An oversized
single-item page, overfull page, or pagination-incomplete response still fails
closed.
See ADR 0026 and the learning publisher implementation for the complete
candidate precedence and recovery rules.

## Ownership, licensing, and commercial boundary

Copyright licensing, trademark usage, and the Community / official-ops /
prospective commercial boundary are documented in
[`docs/ownership-and-licensing.md`](ownership-and-licensing.md),
[`TRADEMARKS.md`](../TRADEMARKS.md), and
[ADR 0040](adr/0040-community-managed-enterprise-boundary.md). Public
App/broker source remains MIT; official channel control does not reclassify
that source as proprietary. Package identities and runtime contracts are
unchanged by that documentation.

Authenticated abandoned analysis recovery is described in
[ADR 0063](adr/0063-authenticated-reservation-recovery.md). The broker can bind a
held reservation to its OIDC run. `@reviewsensei review continue` requires live Actions
completion evidence before reclaiming analysis, including a constrained origin
proof for older PR-triggered runs. Recovery preserves session budgets and saved
publication work; cooperating command/review jobs serialize the comment write.

Complete review evidence now uses the lossless inline contract proposed in
[ADR 0072](adr/0072-lossless-inline-review-evidence.md). Small baseline and human
inventories retain their legacy JSON shape; larger inventories share a bounded
canonical JSON/zlib/base64 reader. The existing authenticated ledger atomically
binds the full baseline and preserves its lifecycle reserve. Human eligibility
version 3 stores immutable full prose separately from the mutable resolution
list and reserves room for every possible resolution. Whole inventories are
independent of provider batches. Capacity failure carries an exact-result-bound
`persistence_status` and `baseline_capacity_exceeded` outcome; it withholds
approval while reporting analysis coverage accurately. All shared readers must
be upgraded before richer writes; rollback readers fail closed on those records.
Representative capacity and retained-state measurements are recorded in the
ADR: fifty varied findings fit the baseline and resolution-growth contracts;
larger or more verbose inventories can still exceed inline bounds. Full finding
text is budgeted separately from the input overview, while the framed review
body remains bounded to 65,536 bytes.

Only confirmed archives enable narrative excerpts; all finding prose and authority stay intact.
Complete-profile fitting can use a labelled UTF-8 provider-narrative excerpt
with digest and links to the complete original transaction-bound result parts.
The original result stays unchanged, and all confirmation reads share the
current caller budget. The final frame includes links and future disposition
reserves within its existing ceiling. Unconfirmed carriers prevent this fallback.
See [ADR 0113](adr/0113-archive-backed-provider-narrative-excerpts.md).

### Explicit incomplete-review reanalysis

An explicit hosted rescan authenticates the human event and latest incomplete
App publication before reserving fresh analysis on an unchanged head. Session
replay watermarks and bounded receipts distinguish this request from ordinary
snapshot duplicates while retaining the existing per-head failure budget and
current-head fences. Both reusable workflow provider lanes pass `--rescan` for
manual dispatches and issue-comment review requests. Publication recognizes
receipt-bound transactions and preserves all approval and coverage gates.
The caller and package recognize a bounded standalone rescan command, rather
than treating a mention plus a keyword anywhere in prose as authority. Current
CLI/web setup generators share that grammar; exact frozen prior caller bytes
permit managed upgrades while operator edits fail closed. See
[ADR 0080](adr/0080-explicit-incomplete-review-rescans.md).

`ConversationService.reply` uses generic execution and optional original `ResourceBudgetTracker`:
retain time/calls/aggregate prompt/output across transport retries and one JSON/non-object correction
under bounded defaults; no semantic correction. GitHub tracks before context preparation
through operation entry/generation. Expiry/exhaustion/cancellation forbid publication. Adapters get
remaining deadline/configured output cap; no backend policy.

## Bounded hosted history readers

Hosted REST lists and review-thread GraphQL queries use adaptive page sizes
within their existing read, item and response-byte limits. Prepaid ledger plans
fix the selected fence size before activation. The retry rules and finite
qualification matrix are in [ADR 0096](adr/0096-bounded-hosted-history-readers.md)
and [hosted history acceptance](validation/hosted-history-readers.md).

Same-operation inventory handoff must retain budget/transport/token/scope,
fresh complete intent/parts reads and fresh ledger/Bot/PR/root/index/full-prose fences.
Canonical admission is evidence only; see ADR 0098.

Restored handles bind operation/inventory/source, original clock/charges and time/generation/
expiry/rollback limits. No replay reexecution, unknown-work publication, CLI/grant activation
or broker funding (ADR 0086).

The explicit `ConversationPublisher.prepare_selected_assessment_context` API
routes selectors into the existing complete finding/evidence/source composition
instead of loading unused generic discussion context. Its compact report path
uses exact owned POST-ID readback followed by the complete report scan; the
ordinary conversation route remains available. See
[Proposed ADR 0087](adr/0087-selected-assessment-context-and-owned-page-readback.md).

Worker bounds are independent; receipts never refund caller POSTs or grant authority.
Reject redirects and mismatched receipts; isolate requests (ADR 0111).

`admit_with_tail` joins reservation/admission; owned readback precedes inference (ADR 0090).

Opt-in partitioned publication binds independent result/snapshot/policy/config/producer,
original budget/conditional generation/owned numeric readback. Fresh scans verify all prose;
partial roots keep links/capacity/full-ID status. Local replay only; default routes/all-resolved
approval unqualified (ADR 0089).

Hosted media reconstruction uses the exact-result finding index at each matching
instance and lifecycle for admission classifications. Serialized model proposals
remain audit evidence; missing or conflicting index authority leaves their
blocking fence active. Human resolution comes from the matching resolution
inventory. Coverage, receipts and current approval fences remain independent.

Hosted-only reuse requires core-input proof and per-package signed provenance (ADR 0105).

### Durable maintainer decision recovery (unreleased)

[ADR0106](adr/0106-durable-decision-snapshots-and-metadata-phases.md) describes
exclusive authenticated decision snapshots and explicit analysis metadata phases.
They preserve full audit records and current authority while keeping request,
codec and storage limits. See [the recovery protocol](approval-recovery.md)
for supported commands and version applicability.

Explicit remaining-coverage admission and checkpoint accounting are specified in
[Proposed ADR 0107](adr/0107-bounded-remaining-coverage.md). Productive windows
keep their partial status and independent approval obligations. Strict progress
receipts affect checkpoint accounting.


### Inactive publication selection handoff (PR392)

The pure selector can freeze an optional `ReviewResult.publication_selection`
before accepted-result retention. The closed record binds the independent
source result, trusted admission criteria, policy, exact occupancy and selected
result projection to an ordered create/reuse/reopen intent. It excludes
unselected candidate queue. Its explicit digest domain includes every handoff
byte, while historical results without the field keep their previous digest.
Cold accepted-operation replay preserves these intents without selection or reads.

Both ordinary and partitioned publishers reject this inactive record before
network or host effects. Plans containing creates or zero actions are inactive.
The occupancy digest authenticates no state on its own: an independent complete
host inventory and original criteria remain required. A contingent reservation
records count liability, not authority to reopen. Native revision/readback,
reopening proof, numeric capacity certification and activation remain unfinished.
See the proposed [ADR 0119](adr/0119-high-signal-review-verdict.md).

Never execute inactive selection.
