<!--
supplemental-ledger
These docs/architecture.md parts stay supplemental. They are historical narrative
or non-normative reference. Every normative line copied below is verbatim.
- preamble: pipelines guide snapshot pointer
- Boundary map diagram, except the test-only production-map exclusion
- Selected design: historical combined-workflow versus provider-protocol choice
- Future extension points: prospective adapter notes, except the learning-publisher rule
- OIDC exchange figure, except the metadata-only audit constraint
- explanatory sentences with no MUST, MUST NOT, never, only, refuse, or retain constraint
-->

```supplemental-ledger
omitted:
  - preamble
  - boundary-map-diagram
  - selected-design
  - future-extension-prospective-notes
  - oidc-exchange-figure
```

## Explicit human file review (local, opt-in)

The proposed [human file review adapter](human-file-review.md) binds selected
unsupported binary changes to the exact owned review/result, complete changed
inventory, base/head Git blobs and live authorized human source. Confirmation
defaults off and never rewrites AI coverage, eligibility, baseline or cache.
Host-derived `coverage_only_partial` provenance uses an explicitly versioned
content digest projection; old result digests remain unchanged when absent.
A separate fresh assessment can satisfy only the binary coverage blocker after
all files are confirmed. Other findings and approval facts remain in force.
There is no cross-head carry and no automatic hosted command. An explicit
verification may post one exact-head APPROVE when mixed coverage is the only
remaining blocker. That post does not mark the AI result, baseline, or cache
complete. The legacy finalizer still withholds a partial result. Broker
composition and deployed resource qualification remain pending.
Recording a confirmation is not approval.
See [Proposed ADR 0077](adr/0077-explicit-human-file-review.md).

## Provider response completeness and capability identity

reassessment check that identity before planning and dispatch. Ollama advertises
the contract only with explicit fixed-model `require_completion_metadata=True`:
it requires normal completion, exact model and bounded integer token usage before
returning text. Present incomplete, length-terminated or over-cap metadata also
refuses in legacy mode; absent legacy metadata retains its prior disposition.

## npm launcher and standalone boundary (issue #103)

implementation. `packages/npm/cli/bin/review-sensei.js` is the only package
that exposes the `review-sensei` command; it selects one exact platform package,
validates its fixed in-package payload, and forwards argv/process behavior.
`review_sensei.cli:main` remains the only review engine. Native one-folder

independent lane. A
partial or compromised release is handled version-forward, never by moving a
tag or replacing a tarball. See [ADR 0031](adr/0031-npm-launcher-and-standalone-platform-packages.md).

## Boundary map

(fixture adapter: test-only seam, omitted from production map)

`ReviewService.run` always emits a versioned `RunOutcome`. Hard resource budgets
cap provider calls, transport retries, prompt/output bytes, and elapsed time.
One structural-correction retry remains distinct from transport retry. Opt-in
`RecoveryArtifact` values can republish a validated result without invoking a
model or rewriting trusted learnings or configuration. See
[ADR 0043](adr/0043-structured-run-outcomes-budgets-and-publication-recovery.md).

the ledger's baseline and digests cannot reconstruct them. Reusable runners
publish only a newly produced result and retain an explicitly enabled diagnostics
bundle after publication failure. That bundle includes the reviewed diff and
trusted configuration/admission contexts, retains the existing seven-day limit,
and remains disabled for `artifacts: none`. Local analysis-only sessions retain
their existing duplicate behavior. See the PR223 amendment to

API authentication and comment/review delivery. `prepare_publishable_review`
sits between a validated `ReviewResult` and any publisher: `legacy` keeps
single-pass comments and identifies them; `confirmed` publishes only candidates
whose evidence exists in the exact reviewed snapshot. Unverified candidates
never become findings, and incomplete verification cannot be treated as a
clean review. Candidate text and snapshot contents remain untrusted data.

evaluation loader and `FixtureProvider` into the unchanged `ReviewService`.
Reports retain only aggregate, non-monetary metrics. Live endpoints are

## Quality and supply-chain boundary

The repository quality boundary is deterministic and provider-independent. The
`quality` CI job runs Ruff formatting/linting, mypy, compileall, and branch
coverage; the `schemas` job validates the three packaged Draft 2020-12
contracts and immutable GitHub Action pins; the `package` job builds both
distributions, records SHA-256 digests, and runs the packaged dist-safe and
downstream suites from an unpacked sdist after installing only the wheel
outside the checkout; CodeQL analyzes Python and JavaScript/TypeScript and its
deterministic SARIF findings gate is the enforcement surface. Checkout-only
tests that need `scripts/`, `.github/`, `packages/`, or `deploy/` remain in the
compatibility and quality jobs. All workflow permissions default to

The public JSON schemas are structural contracts only. They reject malformed

number ceilings documented in the README and ADR 0007. Profiles can only move
downward. Canonical NFC UTF-8 repository paths and strict Git C-quoted paths are

## Portable workflow boundary

`src/review_sensei/workflow.py` owns the portable manual GitHub Actions boundary.
It validates untrusted workflow inputs (`base_ref`, `head_ref`,
`head_repository`, `review_sensei_version`, and diff limits) before invoking git,
fetches explicit refs with list-based argv, resolves base/head refs to verified
commit OIDs, verifies a merge base, and writes a bounded triple-dot diff without
checking out or executing head code. The installed package exposes this through
`review-sensei prepare-diff`, so consumer repositories that do not contain Code
Sensei source can still run the reviewed workflow.

The generated caller is a read-only bootstrap: it grants `contents: read`,
`pull-requests: read`, `issues: read`, and `id-token: write`, routes only on
event shape, and calls the public reusable workflow at the configured tag with
the event context. Every product decision — provider backend, model, endpoint,
credential reference, runner requirement, and the GitHub policy switches —
comes from the trusted configuration commit resolved by the reusable workflow,
never from the caller. The reusable workflow resolves one plan from the
package, then runs the backend on the runner kind that plan requires: cloud
backends on GitHub-hosted compute, local backends on the operator's
self-hosted runner (labelled `ollama` in the example), which must have Ollama
and the selected model provisioned before dispatch. Provider egress is explicit

## GitHub App setup migration boundary

marker. Before creating a setup pull request, the Cloudflare Worker or Python
adapter reads only the three known generated paths from the trusted default
branch, with a 128 KiB per-file limit and strict UTF-8 decoding. Older

content is not overwritten. The migration branch is created once per base and
tag (`review-sensei/setup-v5-<base12>-<tag>`), is reusable only when its parent
is the named base SHA, its author is the ReviewSensei App, its content is
exactly current, and its comparison changes generated paths only, and is never
force-moved; it changes no path outside the generated set, preserving
learnings, secrets, existing variables, and unrelated repository files. See ADR

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

## Review stage configuration

an inline `categories` array. A stage cannot combine both forms. Reusing an id
in multiple stages is allowed only when its title and focus items are identical,
keeping the aggregated result vocabulary unambiguous.

The category block is configuration, not provider output. A stage with
categories must include `{review_categories}` in its template so focus data
cannot be configured but silently omitted from the model prompt. A stage whose
categories request supplemental context must also include `{review_context}`.

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

Document selection resolves the active lenses together, deduplicates paths, and
keeps the existing global limits of 64 documents, 128 KiB per supplied document,
and 512 KiB of unique supplied content. Present explicit-file sources, all
required-source matches, and applicable `AGENTS.md` within declared sources are
reserved in full. Mandatory overflow fails before provider invocation. Optional
directory documents are ranked by references to each lens's changed paths and
overlap with changed file stems in document paths, headings, and text; canonical
paths break ties. Fitting foundational documents remain eligible even without
lexical matches. No implicit repository scan or category override is required.

Under byte pressure, optional documents may become bounded **verbatim line
excerpts**, with complete-source and payload digests, original line ranges,
method/version, and explicit loss warnings. This is extractive summarization,
with zero additional provider calls; it cannot establish that omitted sections
contain no requirements. Discovery, inspection, and extraction have separate
bounds. Selection metadata belongs to the lens prompt and cache identity, and
loss counts appear on stderr. `--context-selection-output PATH` writes the full
bounded path/provenance inventory without document text. These counts describe
supplemental context, independently of source-review coverage and approval.
See proposed [ADR 0058](adr/0058-budgeted-document-context.md).

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

- The GitHub learning publisher converts validated `learning_proposals` into a
  single ReviewSensei-owned draft PR per source PR. It uses content-addressed
  learning files, a stable source-PR branch/marker, hash-bound commit
  provenance, latest-default-head rereads, and non-force refreshes. Merged
  generations start fresh and omit files already byte-identical on the latest
  base; closed, deleted, manually edited, fork-owned, or ambiguous artifacts
  fail closed. Only merged learning files are eligible for later reviews.

## Open-source integration

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

## Release engineering boundary

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

## Public landing-page analytics boundary

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

## Publication boundary

`malsabbagh/review-sensei` through a reproducible publication pipeline. An
exclusion manifest (`.publication/exclusions.json`) lists every internal-only
file pattern with a documented reason. A commit-oriented publication audit

ledger entry recording both SHAs and a mode-aware publishable tree hash. The
hash covers sorted path, tracked Git mode, and blob-digest records, so a
mode-only executable-bit change is a distinct publication identity. The

is superseded for now. The Cloudflare package is only an optional installation
bootstrap ingress; customer Actions still own review execution and provider
compute. A hosted review service would require a new ADR and a fresh issue set.

## OIDC installation-token broker

The optional OIDC installation-token broker is a narrow boundary outside the
provider-neutral review core: `oidc.py` verifies GitHub Actions identity and
`broker.py` applies workflow policy before reusing `GitHubAppAuth`. The App
private key remains in managed storage, the broker receives no diff or provider response,
and forks, missing or suspended installations, rate limits, and verification
or GitHub failures are rejected fail-closed.

        -> audit (non-secret metadata only)

## Issue-64 setup-v4 tagged publication architecture

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

through `hosted_runner` (ADR 0062). Read-only bootstrap and the local Ollama
label array retain their existing routing. Backend, model and policy still

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

[ADR 0072](adr/0072-lossless-inline-review-evidence.md). Small baseline and human
inventories retain their legacy JSON shape; larger inventories share a bounded
canonical JSON/zlib/base64 reader. The existing authenticated ledger atomically

approval while reporting analysis coverage accurately. All shared readers must
be upgraded before richer writes; rollback readers fail closed on those records.
Representative capacity and retained-state measurements are recorded in the
ADR: fifty varied findings fit the baseline and resolution-growth contracts;
larger or more verbose inventories can still exceed inline bounds. Full finding
