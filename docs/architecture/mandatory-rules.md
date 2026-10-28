Quarantine registry: append-only, empty; workflow disabled until reviewed exact one-shot registration. Release grants no activation.

Actions retain one caller HTTP allowance: root `api_request_limit` (default 512).
Only v3 purpose/source/repo/PR/head/run/attempt/job/workflow authority enables work.
Successors must prove cessation; uncertainty and abandoned capacity stay charged.
New runs must archive terminal predecessor accounting.
Only exact results/proven pre-entry aborts close actions; unknown work stays pending.
Historical readers retain exact review/archive evidence and fresh authority, without writes.
Finite operation/page limits can refuse with caller capacity unused.
Same-operation inventory handoff must retain budget/transport/token/scope,
fresh complete intent/parts reads and fresh ledger/Bot/PR/root/index/full-prose fences.
Canonical admission is evidence only; see ADR 0098.

Beyond legacy future-assessment capacity, closed schema 5 owned purpose-bound parts
preserve every admitted human instance/metadata/original index. References grant no authority:
require independent durable transaction and live repository/PR/base/head/numeric Bot
identity. Inference/decisions/approval require fresh complete original prose/index verification.
Receipt metadata fences never authorize decisions. All approval facts/provider qualification/other blockers
persist through resolution. Preflight largest future receipt and prose/frame bounds.
Only original live admission starts writes; durable intent precedes data. Unknown/incomplete
writes hold without blind recreation. One original finite evidence budget covers
publication/reply broker/storage/readers/mutable fences. Missing/conflicting evidence withholds
decisions. Require reader-first rollout and trusted complete emission. See ADR 0094.

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

<!-- selected-design -->

[Human file review](human-file-review.md) binds an owned exact result/root, complete inventory,
regular base/head blobs and current authorized source. Hosted composition reads canonical retained
state after broker authorization. V1 parts keep 32 files; groups keep 32 KiB and exact union coverage.
Media keeps caller accounting and independent Worker check ceilings.
Fresh source, permission, root, request, thread, qualification and full PR facts gate approval.
Current finding facts require an exact-result index and positive resolution proof; never infer
resolution from false flags. Never complete AI coverage/result/baseline/cache or carry across heads.
Unknown writes need exact owned readback; unproven entered actions refuse replay.
See [Proposed ADR 0103](adr/0103-bounded-hosted-media-confirmation.md) and [ADR 0077](adr/0077-explicit-human-file-review.md).

reassessment check that identity before planning and dispatch. Ollama advertises
the contract only with explicit fixed-model `require_completion_metadata=True`:
it requires normal completion, exact model and bounded integer token usage before
returning text. Present incomplete, length-terminated or over-cap metadata also
refuses in legacy mode; absent legacy metadata retains its prior disposition.

implementation. `packages/npm/cli/bin/review-sensei.js` is the only package
that exposes the `review-sensei` command; it selects one exact platform package,
validates its fixed in-package payload, and forwards argv/process behavior.
`review_sensei.cli:main` remains the only review engine. Native one-folder

independent lane. A
partial or compromised release is handled version-forward, never by moving a
tag or replacing a tarball. See [ADR 0031](adr/0031-npm-launcher-and-standalone-platform-packages.md).

(fixture adapter: test-only seam, omitted from production map)

`ReviewService.run` emits versioned `RunOutcome`. Hard budgets cap provider calls,
transport retries, prompt/output bytes and elapsed time. One structural-correction
retry differs from transport retry. Opt-in `RecoveryArtifact` republishes
validated results without models or trusted learnings/configuration changes. See
[ADR 0043](adr/0043-structured-run-outcomes-budgets-and-publication-recovery.md).

ledger baseline/digests cannot reconstruct them. Reusable runners publish
new or independently validated retained results. Opt-in publication-failure
diagnostics retain reviewed diff and trusted configuration/admission contexts up to seven
days, disabled by `artifacts: none`. Local analysis-only duplicates remain unchanged.
PR223 amendment to

Opt-in recovery binds original trusted policy, exact base/head diff and authenticated
prior baseline. Configuration/evidence/policy/reservation must match committed
transaction. Missing admission/ordinary retries refuse. Both lanes skip provider
credentials/inference; retain one read budget, broker/accepted-result/ledger/head
guards and writer slot.

API authentication and comment/review delivery. `prepare_publishable_review`
sits between a validated `ReviewResult` and any publisher: `legacy` keeps
single-pass comments and identifies them; `confirmed` publishes only candidates
whose evidence exists in the exact reviewed snapshot. Unverified candidates
never become findings, and incomplete verification cannot be treated as a
clean review. Candidate text and snapshot contents remain untrusted data.

evaluation loader and `FixtureProvider` into the unchanged `ReviewService`.
Reports retain only aggregate, non-monetary metrics. Live endpoints are

Deterministic, provider-independent CI gates Ruff format/lint, mypy, compileall,
branch coverage, three packaged Draft 2020-12 contracts and immutable Action pins.
Build both distributions/SHA-256s; run unpacked-sdist dist-safe/downstream against
installed wheels outside checkout. CodeQL Python/JS/TS findings use deterministic SARIF.
Checkout-only scripts/.github/packages/deploy tests stay in compatibility/quality jobs.
All workflow permissions default to

The public JSON schemas are structural contracts only. They reject malformed

number ceilings documented in the README and ADR 0007. Profiles can only move
downward. Canonical NFC UTF-8 repository paths and strict Git C-quoted paths are

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

marker. Before creating a setup pull request, the Cloudflare Worker or Python
adapter reads only the three known generated paths from the trusted default
branch, with a 128 KiB per-file limit and strict UTF-8 decoding. Older

content is not overwritten. The migration branch is created once per base and
tag (`review-sensei/setup-v5-<base12>-<tag>`), is reusable only when its parent
is the named base SHA, its author is the ReviewSensei App, its content is
exactly current, and its comparison changes generated paths only, and is never
force-moved; it changes no path outside the generated set, preserving
learnings, secrets, existing variables, and unrelated repository files. See ADR

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

an inline `categories` array. A stage cannot combine both forms. Reusing an id
in multiple stages is allowed only when its title and focus items are identical,
keeping the aggregated result vocabulary unambiguous.

The category block is configuration, not provider output. A stage with
categories must include `{review_categories}` in its template so focus data
cannot be configured but silently omitted from the model prompt. A stage whose
categories request supplemental context must also include `{review_context}`.
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
(`SymbolAwareContextPolicy`, `--enable-symbol-context`). Disabled by default,
it never expands trusted context. When enabled, `SymbolAwareContextSelector`
reads only the trusted-base snapshot identified by `--base-sha`. Head source

and an explicit `unsupported-language` outcome. Selection is static: files are
never executed. Exhausted budgets fail closed; other incomplete coverage is

write, egress, or approval defaults. Bounded relationship expansion reports
`directory-truncated` or `relations-truncated` so a capped graph cannot be
read as a complete one, and only Python sources count toward the
caller-directory cap. Default enablement requires evaluation under issue #33.

Custom stage and category directories are operator-selected configuration. They
must come from a trusted checkout or deployment bundle, not the pull-request
head. The loaders reject empty directories, symlinked or non-regular JSON files,

The context loader rejects traversal, symlinks, secret-like names, unsupported
text formats, unreadable or empty files, excessive file counts, and per-file or
aggregate size violations. Selection and serialization are deterministic.

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

- The GitHub learning publisher converts validated `learning_proposals` into a
  single ReviewSensei-owned draft PR per source PR. It uses content-addressed
  learning files, a stable source-PR branch/marker, hash-bound commit
  provenance, latest-default-head rereads, and non-force refreshes. Merged
  generations start fresh and omit files already byte-identical on the latest
  base; closed, deleted, manually edited, fork-owned, or ambiguous artifacts
  fail closed. Only merged learning files are eligible for later reviews.

The primary distribution is the open-source package and CLI. Repositories run
ReviewSensei themselves in GitHub Actions: checkout the trusted base, fetch the
head ref only to compute a bounded diff through `review-sensei prepare-diff`,
install the exact requested package from PyPI or, when that distribution is
unavailable, from the public repository at the executing workflow SHA, run the
provider, and upload the validated result plus the installed version. A source
fallback is used only for the package-not-found condition; other PyPI failures
remain fatal. This path does not require a hosted backend,

setup pull request per newly selected repository. The setup service never
includes secrets, private keys, installation tokens, raw webhook bodies,
authorization headers, or GitHub API bodies in generated files or PR bodies. It
creates the generated files and no repository variables at all; the only two
product overrides are the optional `REVIEWSENSEI_PROVIDER` and
`REVIEWSENSEI_MODEL` Actions variables, and `OLLAMA_API_KEY` remains a
user-managed secret.

hosted review engine. The Worker applies a bounded Web Crypto HMAC gate and
payload validation, then a single named SQLite-backed Durable Object stores
only delivery identity, digest, state, and a short lease. The same Worker uses

Retain signed engine provenance, installed-CLI/downstream tests and workflow drain.

provider-neutral review engine. `pyproject.toml` is the single package metadata
source; `setup.py` is retained only as a metadata-free compatibility shim for
legacy tooling. `MANIFEST.in` controls source-distribution documentation and

Release operations, PyPI registration, signed-tag ownership, verification,
rollback/yank, and compromised-release response are documented in
`docs/releasing.md`. The workflow cannot reserve the PyPI project or decide
whether a maintainer should publish; those are explicit external operations.

public schema version, and Worker identity. Build output is valid only when
every named artifact is present with an exact SHA-256 digest and an explicit
trusted provenance mechanism. Missing or extra artifacts fail closed. The
PyPI-primary and executing-commit install paths prove that identity in
`review_sensei.release_manifest`; live disposable-repository canary and `v5`
tag movement remain operator-only. See [ADR 0038](adr/0038-release-compatibility-manifest.md).

collected by the published tags. The page must not put source code, diffs,
prompts, review comments, credentials, or other sensitive ReviewSensei data in
`dataLayer`. Container tags must follow the site's privacy notice and
applicable consent requirements; adding a new data field or tag requires
maintainer review in the GTM workspace and repository policy review.

- The standard loader and its noscript fallback are the only integration in
  `docs/site/index.html`; additional collection is visible in the container's
  published tag configuration.
- Workspace access and publish permissions are limited to designated
  maintainers, and published container versions are retained for audit and
  rollback.

Rollback is code-only for the repository integration: revert the snippets in
a reviewed PR, merge to `main`, and verify that the deployed page no longer
requests the GTM endpoints. For an active incident, the GTM workspace owner

`malsabbagh/review-sensei` through a reproducible publication pipeline. An
exclusion manifest (`.publication/exclusions.json`) lists every internal-only
file pattern with a documented reason. A commit-oriented publication audit

ledger entry recording both SHAs and a mode-aware publishable tree hash. The
hash covers sorted path, tracked Git mode, and blob-digest records, so a
mode-only executable-bit change is a distinct publication identity. The

is superseded for now. The Cloudflare package is only an optional installation
bootstrap ingress; customer Actions still own review execution and provider
compute. A hosted review service would require a new ADR and a fresh issue set.

Optional OIDC brokers stay outside review logic. `oidc.py` verifies Actions identity;
`broker.py` gates `GitHubAppAuth`. Keys stay managed; no diffs/provider output.
Reject forks, missing/suspended installations, rate limits and failed evidence.
Worker exchanges/upstreams stop at 25s/10s or earlier caller/control deadlines.
Abort I/O; cache completed JWKS only. No POST replay; allowlisted diagnostics only,
never secrets or remote text.

        -> audit (non-secret metadata only)

adds a distinct version-2 session grant. The Worker transiently reloads complete
human sources to verify numeric actors, exact snapshot and selection digests;
credentials and ledger records retain metadata and hashes. It does not receive

Setup-v4 separates the customer caller, public execution workflow, and
issuance-only Worker:

resolves the tag at capability exchange time and checks the runtime SHA. The workflow may
receive the existing customer-owned `OLLAMA_API_KEY` only through a literal
name-only secret mapping. The setup App does not access that value. The Worker
does not receive source, diffs, prompts, review output, reply content, provider
credentials, or installation-token values for persistence; its ledger retains
only hashed identities and bounded counters. Current, custom, malformed, and

The generated caller is invocation-only: backend, model, endpoint, credential,
and every `github.*` policy resolve from `.reviewsensei.yml` on the trusted
policy commit, with `REVIEWSENSEI_PROVIDER` and `REVIEWSENSEI_MODEL` as the
only product overrides. Local, cloud, and OpenRouter runtime jobs expose the

through `hosted_runner` (ADR 0062), including read-only bootstrap. Local Ollama
retains its existing label array. Backend, model and policy still

Before an enabled review reaches either provider, the reusable workflow runs a
read-only authoritative pull-request preflight. It validates the repository

required fixes remain and stays `COMMENT`. `advisory` publishes `neutral`,
stays `COMMENT`, and never fails the check. `action_required` covers a

stand for a review that has not finished. A repository administrator must mark the check
required for it to gate merges in `blocking` mode; `doctor` reports the check identity and
producing App. Checks: write is a scoped broker capability

eligible review. Repositories that require this check must grant the
permission and broker capability for the merge gate to be enforced.

A shared deterministic finalizer then emits `APPROVE` only when an eligible
exact-head PR has a complete, qualified review with no unresolved ReviewSensei
root classified blocking. Non-blocking ReviewSensei roots and human threads may

diagnostic. The classification sweep asks only for bounded root data, is capped
at ten pages, and runs before a final PR preflight. The finalizer runs after

the persisted eligibility document instead of trusting a caller boolean. The
existing `pull_requests: write` capability and per-head approval
marker/idempotency boundary are shared, so approval adds no credential or
persistence boundary; ReviewSensei never calls a merge endpoint or enables
auto-merge.

[ADR 0059](adr/0059-dependency-review-and-partial-publication.md) permits
validated partial analysis to cross the identity-bound checkpoint/publication
boundary only with complete enumeration and evidence of reviewed work. The

base/head, configuration and context, so a same-head base change runs once.
The prior baseline stays in trusted publication admission inputs; fallback
findings retain human adjudication. Only a validated complete checkpoint
replaces the baseline. Missing, malformed, foreign or future history stays
blocked, and partial or failed work preserves the last complete baseline.
Closed operational diagnostic tokens reach stderr and Actions summaries
without requiring retained artifacts.

and plan can display. Issue #146 F7 (ADR 0055) makes `merge-focused` the
runtime default and retires live `legacy` selection, so the policy is now bound
into publication: operator modes run the blocker-admission evaluator before
GitHub review events and comment rendering, and only historical `legacy`
records keep the explicit finding `blocking` bit described above.

another provider call or counter increment. The result and ledger carry only
bounded repository/PR/base/head, policy/configuration/evidence digests,
reservation, generation, phase, and result-digest metadata; publication
recomputes the trusted context before any broker or publisher write. Legacy and

explicit `resolve` decision. Only a ReviewSensei-authored inline root can be
resolved; the publisher rechecks the exact head, performs a bounded GraphQL
root-to-thread lookup, and confirms an idempotent `resolveReviewThread` result.

complete bounded inventory in its App eligibility document. An authorized
explicit `@reviewsensei` explanation and validated provider assessment can refresh
only the human flag for that exact base/head and result. Literal human/diff

assessment identities from lifecycle concern fingerprints. Colliding concerns
retain separate identities derived from their validated v1 comments;
identical comments coalesce. Invalid or oversized inventories fail before

prompt and 16 KiB per-response bounds. The application passes its existing
tracker through preparation and both calls; counters and deadlines are never
reset. Schema/citation rejection and provider transport/completion failures do
not trigger this syntax correction. Safe parser diagnostics contain only fixed
reason labels, numeric line/column/offset, response byte count, correction status
and a closed budget reason. Raw output is neither echoed in the correction nor

budgets. Mode adapters retain distinct prompts and validators. Complete file
groups are atomic for reassessment; exhaustive parsed hunks are permitted only
for discovery. Publisher validation uses the original per-batch evidence map,
without concatenating it into the bounded conversation diff field. Failed or
unprocessed work retains pending findings and conservative coverage.

record blocks older authority. Normal review publication continues writing v1.
The work-policy digest extends transaction configuration only for opt-in runs.

the same planner and tracker. Broader discovery uses configured full-review
policy and its normal publisher; the merged eligibility retains exact prior
findings, blockers, coverage and qualification. Source reply, latest authority

is saved before dispatch and restored without resetting calls or deadlines.
Artifacts retain normalized receipts and exact evidence only under diagnostics
policy, with bounded size/count/expiry and current semantic validation. See

uses the immutable published v0.6.16 source. That reader refuses richer eligibility
and opt-in transaction contexts, but skips session comments exceeding its 16 KiB
ceiling. Upgraded readers treat oversized trusted terminal markers as unreadable
authority and prohibit initialization. Expanded writers require quiescing older
readers/writers; rollback retains upgraded readers rather than resetting state.

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

[ADR 0072](adr/0072-lossless-inline-review-evidence.md). Small baseline and human
inventories retain their legacy JSON shape; larger inventories share a bounded
canonical JSON/zlib/base64 reader. The existing authenticated ledger atomically

approval while reporting analysis coverage accurately. All shared readers must
be upgraded before richer writes; rollback readers fail closed on those records.
Representative capacity and retained-state measurements are recorded in the
ADR: fifty varied findings fit the baseline and resolution-growth contracts;
larger or more verbose inventories can still exceed inline bounds. Full finding

  Every admitted fallback preserves prior concern accounting: omitted,
  downgraded or ambiguously rediscovered concerns remain uncertain, make the
  review partial and retain the prior durable baseline until resolved.

Independent retained `TrustedOpenRouterQualification` evidence gates OpenRouter approval.
Evaluation/runtime digests differ; profile/caller/old `not-required` proves nothing.
Facts v2 keep provider provenance/bounded exact-head/result receipts. All finalizers require
matching App-owned eligibility readback and configured authority rechecks.
Legacy facts parse without exemption; other providers' new reviews retain policy.

Accepted replay binds complete immutable inventory, original result/facts, source identity
and exact evidence. Resolutions may accumulate; changed source/evidence/authority forbids reuse.
Revalidate full evidence; skip only durable resolved IDs. No inventory bypass or provider refill.
Unbound old payloads need a new authorized mention (ADR 0061; [finite matrix](validation/large-lifecycle.md)).

  OpenRouter separates requested model, observed model/upstream and reported fingerprint.
  Optional `ProviderTokenUsage` has only three bounded reported counters; invalid
  optional metadata stays unknown. Aggregate only counters every attempt reports;
  failures/cancellation are attempted work. Retain full bounded family/variant/region/
  tier prefixes through canonical YAML/resolver/CLI/hosted plan; published mappings
  require the same upstream family. Trusted `ProviderSettings` forbids ambient policy
  replacement; named profiles retain exact binding; prefixes enter policy digests.
  DeepSeek/Morph evidence eligibility is not qualification/approval. See proposed ADR 0081.

Live reports must bind actual requested routing-policy digest/base URL to selected target;
missing bindings or changed suffix/endpoint refuse support creation/loading. Old
reports parse but cannot newly authorize unbound live support; historical targets
remain eligible. Runtime approval binding remains missing. See ADR 0081.

`ConversationService.reply` uses generic execution and optional original `ResourceBudgetTracker`:
retain time/calls/aggregate prompt/output across transport retries and one JSON/non-object correction
under bounded defaults; no semantic correction. GitHub tracks before context preparation
through operation entry/generation. Expiry/exhaustion/cancellation forbid publication. Adapters get
remaining deadline/configured output cap; no backend policy.

Live quality and exact fixture conformance differ. Bounded controls keep complete fixture
inventory/identity and unchanged thresholds; promotion verifies both and all attempted live work.
Exclude fixture usage; recall counts misses. Old unbound reports parse but cannot newly authorize
support. Composite controls reject missing live engine/prompt identity with bounded diagnostics.
Strict adapter OpenRouter endpoint-metadata allowlisting precedes live dispatch/report construction
(proposed ADR 0082).

Only confirmed owned replies or a complete empty comment list permit fallback.
Genuine top-level legacy findings retain their exact-head inventory authority.
Unknown, malformed, conflicting or unreadable roots refuse fallback.
The metadata walk retains one shared ten-request, sixty-second bound.

Publisher aliases require original run/source/ref/runner/complete-step equality.
Missing/ambiguous facts or newer distinct executions block qualification.

Restored handles bind operation/inventory/source, original clock/charges and time/generation/
expiry/rollback limits. No replay reexecution, unknown-work publication, CLI/grant activation
or broker funding (ADR 0086).
Worker bounds are independent; receipts never refund caller POSTs or grant authority.
Reject redirects and mismatched receipts; isolate requests (ADR 0111).
`admit_with_tail` joins reservation/admission; owned readback precedes inference (ADR 0090).

Opt-in partitioned publication binds independent result/snapshot/policy/config/producer,
original budget/conditional generation/owned numeric readback. Fresh scans verify all prose;
partial roots keep links/capacity/full-ID status. Local replay only; default routes/all-resolved
approval unqualified (ADR 0089).
Continuation must verify full input scope; absent/changed scope refuses reuse.

Hosted-only reuse requires core-input proof and per-package signed provenance (ADR 0105).
