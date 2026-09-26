# ReviewSensei

ReviewSensei is an open-source, provider-neutral AI code review engine. It turns
unified pull-request diffs into validated summaries and inline findings. Ollama
is the first supported model provider, with a small adapter seam for additional
providers later.

> Alpha: this repository provides an open-source review engine and CLI. The
> primary usage path is running ReviewSensei in your own GitHub Actions
> workflow. Setup-v4 callers follow the operator-managed public `v5` tag; the
> Worker validates that tag before creating the setup PR and the broker resolves
> it again when issuing a capability. The broker still accepts `v4` during
> migration. The example workflow installs the
> requested exact package from PyPI first and falls back to its executing commit
> only when the distribution is unavailable. Both paths prove the intended
> release identity against a compatibility manifest; live canary and `v5`
> movement remain operator-only. It
> defaults to a local Ollama endpoint and does not require a hosted backend.

## What it does

- Keeps review business logic independent of GitHub and model vendors.
- Sends a structured prompt to a replaceable provider adapter.
- Requires provider output to be valid JSON.
- Filters inline comments that target files or lines outside the supplied diff
  while preserving independently valid findings, including deletions and
  file-level concerns, and reports an explicit coverage outcome for every
  changed file.
- Supports local Ollama servers and Ollama Cloud through the same adapter.
- Loads approved repository-local learnings and returns validated learning proposals.
- Supports ordered review stages with structured categories and explicit focus
  items.
- Selects lenses by changed path and can attach bounded, lens-specific
  learnings and architecture documents from a trusted target checkout.
- Exposes a host-independent concurrency plan with latest-wins pull-request
  runs and one provider slot per pull request.
- Publishes versioned JSON Schemas for review results, learnings, categories,
  stages, and concurrency plans.
- Adds optional blocking, severity, fix-effort, and lens labels with
  deterministic summary counts and visual cues for fast triage.
- Provides a narrow GitHub App JWT and installation-token auth adapter for
  optional App-identity comments or reviews.
- Avoids retaining raw prompts and provider responses in the engine.
- Provides `review-sensei doctor` and a zero-write `review-sensei plan`
  preview so skipped or failed setup is diagnosable without forking workflows.

## Quick start

ReviewSensei supports Python 3.11, 3.12, 3.13, and 3.14:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
review-sensei --version
```

The default backend is a local Ollama server: `local-ollama` at
`http://127.0.0.1:11434/api` with model `qwen3.5:4b`. It needs no credential,
so a local review runs with no repository setup at all. Select a backend on
the command line, or with the two supported environment overrides
`REVIEWSENSEI_PROVIDER` and `REVIEWSENSEI_MODEL`:

```bash
review-sensei --diff pr.patch --provider local-ollama
review-sensei --diff pr.patch --provider local-ollama --model qwen3.5:8b
```

The hosted backends are explicit, and each one requires its own credential:

```bash
OLLAMA_API_KEY="your-key" review-sensei --diff pr.patch --provider cloud-ollama
OPENROUTER_API_KEY="your-key" review-sensei --diff pr.patch --provider openrouter
OPENAI_API_KEY="your-key" review-sensei --diff pr.patch --provider openai-compatible
```

A backend that needs no credential never receives one implicitly: a key
exported for another backend does not travel to `local-ollama`.

### Run through npx

Node.js 22 or newer can invoke the same Python review engine through the
provider-neutral `@reviewsensei/cli` launcher:

```bash
npx --yes @reviewsensei/cli@0.6.9 --version
npx --yes @reviewsensei/cli@0.6.9 --help
```

The launcher selects one of the five native packages (macOS arm64/x64,
glibc Linux arm64/x64, or Windows x64), forwards arguments unchanged, and
does not contain a JavaScript review engine. It has no install/download hook;
`prepare-diff` still requires an external `git` executable. Provider and
configuration flags are identical to the Python CLI above. Linux musl and
other architectures receive a bounded unsupported-target error.

The package version is intentionally identical to the Python project and
release tag. See [`docs/installation.md`](docs/installation.md),
[`docs/releasing.md`](docs/releasing.md), and [ADR 0031](docs/adr/0031-npm-launcher-and-standalone-platform-packages.md)
for prerequisites, release ordering, provenance, and version-forward rollback
guidance.

Generate a bounded diff and review it. The shortest local run needs nothing
but the diff and a reachable local model server:

```bash
review-sensei --diff pr.patch
```

Repository context, learnings, and an explicit change identity are optional:

```bash
review-sensei prepare-diff \
  --repository . \
  --base-ref main \
  --head-ref feature \
  --output pr.patch
review-sensei doctor
review-sensei plan --diff pr.patch --repository owner/repository --pull-request 42
review-sensei \
  --diff pr.patch \
  --repository owner/repository \
  --pull-request 42 \
  --title "Reviewable change" \
  --learning-root /path/to/target-branch-checkout \
  --context-root /path/to/target-branch-checkout \
  --format json \
  --output review.json
```

The default output is readable terminal text. `--format markdown` and
`--format json` are explicit, and only the selected format is written to
stdout or `--output` while warnings, diagnostics, and the final status and
reason lines stay on stderr. The JSON format
is the validated `review-result` document containing `summary`, `comments`,
`provider`, `model`, and `learning_proposals`; learning proposals are not
stored or used automatically.

The review exit contract is stable:

| Code | Meaning |
| --- | --- |
| `0` | The review completed and no required fixes remain |
| `1` | The review completed with required fixes remaining |
| `2` | The review could not complete, the input was invalid, or a human decision is required |

A non-zero exit prints a `review-sensei: reason=<token>` line naming the cause,
and the exit codes are unchanged by the selected output format. Hosts and
launchers that select a lane from the historical 0/1 contract pass
`--exit-semantics operational`.

A local review is a bounded one-shot and needs no GitHub identity, OIDC token,
broker, or hosted state. `--local-session` is the one explicit
persistent-local-session operation: it keeps the durable session ledger in the
platform's per-user state directory (`~/Library/Application Support/review-sensei`
on macOS, `%LOCALAPPDATA%\review-sensei` on Windows, and
`$XDG_STATE_HOME/review-sensei` or `~/.local/state/review-sensei` elsewhere) so
repeated runs of the same change reuse one session, and `--session-ledger`
still overrides that location.

The published schema files live under `src/review_sensei/schemas/` and are
included in the installed package. Public compatibility rules, CLI flags, error
categories, and provider protocol expectations are documented in
[`docs/public-contracts.md`](docs/public-contracts.md).

The package also includes the repository contract schemas
[`review-category.schema.json`](src/review_sensei/schemas/review-category.schema.json),
[`review-stage.schema.json`](src/review_sensei/schemas/review-stage.schema.json),
and [`learning-entry.schema.json`](src/review_sensei/schemas/learning-entry.schema.json)
used by the repository contract validator. Runtime loaders remain the semantic
authority after structural validation.

## GitHub App identity

Optional App-identity publication is documented in
[`docs/github-app-auth.md`](docs/github-app-auth.md). It is a narrow
authentication adapter for GitHub App JWTs and installation access tokens; it
is not a hosted review service, webhook receiver, or durable delivery store.

### GitHub App setup-v5 integration

The optional setup-v5 integration adds a reviewable, generated caller by using
the operator-managed `v5` git tag as the only update channel:
`malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@v5`.
The Worker validates the tag before writing the caller, and the Cloudflare
broker resolves the same tag at capability exchange time and checks the
executing workflow SHA. Moving `v5` is therefore the public cutoff action.
The selected provider mode applies consistently to automatic pull-request
reviews, manual reviews, learning proposals, artifacts, and authorized
`@sensei` conversations. Cloud runs use GitHub-hosted compute; local runs use
the operator-controlled labeled self-hosted runner. Every generated write
switch except automatic approval defaults to `false`; automatic approval
defaults to `true` and requires automatic review and GitHub writes as separate
gates.

Cloud mode may pass the existing customer-owned `OLLAMA_API_KEY` secret by
name (`secrets.OLLAMA_API_KEY`) to the public reusable workflow. The App
does not create, read, persist, log, or reveal that secret value. The Worker
broker issues a short-lived, capability-scoped installation token for each
authorized exchange after verifying
the signed Actions OIDC identity, exact `v5` workflow ref and runtime SHA,
repository identity, fork status, installation, replay state, and rate limit.
For each write operation, the workflow obtains a separate short-lived token for
the requested capability: `review_publish` for review publication,
`inline_reply` or `issue_reply` for replies, and `learning_write` for learning
pull requests. The broker-issued token grants the workflow the capability to
make the corresponding GitHub API write; Cloudflare does not perform that
write. Review and reply capabilities grant `pull_requests: write`;
`learning_write` additionally grants `contents: write`. A review-publication
token cannot write repository contents.
Cloudflare does not run the review engine or send prompts to the model provider;
the GitHub Actions job performs those operations in the configured provider
environment.

During installation or migration, the Worker separately uses GitHub App
authentication to obtain a repository-scoped installation token for the setup
branch, pull request, and default variables.

Installation, reinstall, permission-acceptance, and repository-added events
reconcile absent or older generated clients through at most one setup-v5 PR.
Current tag-following setup-v5 is a no-op; managed v3/v4 and older generated clients are migrated
through a setup PR, while custom, malformed, and future setup files are no-write
cases. See [`docs/installation.md`](docs/installation.md),
[`docs/github-app-registration.md`](docs/github-app-registration.md), and the
proposed [`docs/adr/0025-cloud-local-feature-parity-and-conversation-reactions.md`](docs/adr/0025-cloud-local-feature-parity-and-conversation-reactions.md).
The optional OIDC broker documented there is a hosted issuance-only backend
that never distributes the App private key.

## Offline evaluation

The repository includes a synthetic, CC0-1.0 corpus that runs without network
access or provider credentials:

```bash
python scripts/validate_evaluation_corpus.py
review-sensei evaluate --mode fixture --corpus evaluation/v1/corpus.json \
  --output evaluation-report.json
```

Fixture evaluation is a deterministic regression signal, not a broad model
benchmark. See [`docs/evaluation.md`](docs/evaluation.md) for live-provider
acknowledgements, privacy limits, metrics, and approval policy.

The corpus also documents an exact standalone fixture-provider check. Use the
pinned `fixture-v1` model when comparing the output to the off-by-one oracle;
the normal CLI default is `qwen3.5:4b` in local mode.

## Bounded review contract

All diff text, paths, source comments, repository context, model output, and
configured metadata are untrusted. The provider-neutral core validates them
before a provider call and validates the complete result before a publisher can
consume it. The public defaults are a hard ceiling; an embedding application
may only tighten them:

```python
from review_sensei import ReviewLimits, ReviewRequest

limits = ReviewLimits(max_diff_bytes=256_000, max_comments=40)
request = ReviewRequest(diff=diff_text, limits=limits)
```

The default profile is: diff 1,048,576 bytes, 50,000 lines, 500 files, and
5,000 hunks; repository metadata 512 bytes; title 4,096 bytes; instructions
65,536 bytes; 64 metadata pairs with 128-byte keys and 4,096-byte values;
model identifiers 256 bytes; 100 learning entries; 64 active categories; 64
lens contexts; rendered prompts 4,194,304 bytes; provider response envelopes
1,048,576 bytes; summary 32,768 bytes; 250 comments with 16,384-byte bodies;
100 learning proposals with 16,384 bytes per serialized proposal and 262,144
bytes for the compact UTF-8 serialization of the complete proposal array
(including brackets and separators); 2,097,152 bytes per publisher-facing
result; and line numbers up to 2,147,483,647. These are byte limits unless a
unit is stated otherwise.

Every changed file in a review result receives an explicit coverage outcome
(`reviewed`, `partially-reviewed`, `excluded-by-policy`, `unsupported`, or
`budget-exhausted`). Incomplete enumeration is never reported as fully
reviewed. Finding locations may bind to a right-side added line (the legacy
default), a left-side deleted line, or a file-level concern. Per-request
limits stay in place; opt-in `--orchestrate-large-changes` adds a separate
total-work budget so larger diffs can be chunked without silent truncation or
unbounded provider calls.

Repository paths are strict canonical NFC UTF-8, `/`-separated, relative paths:
no empty, dot, parent, absolute, drive/UNC, backslash, surrounding whitespace,
control/format/surrogate, or silently normalized forms are accepted. Glob
tokens are allowed only in pattern fields; real Git paths may contain literal
glob characters and are preserved without being interpreted. Git C-quoted
paths are decoded as strict bytes before the same path checks. Exact duplicate
comments identified by `(path, line, body)` use stable first-wins ordering;
comments that differ in body remain distinct. Across heads, findings also carry
a stable fingerprint from path, symbol, defect kind, and optional evidence id
so a moved or rephrased concern does not open a duplicate ReviewSensei thread,
and a later pass that omits a finding is not treated as a fix. Expected failures are sanitized
and never include a prompt, diff, provider body, credential, or supplied secret
marker. Invalid structured provider output receives one fresh, sanitized
correction attempt per stage; the rejected attempt is discarded completely and
a second invalid response fails closed. Transport and resource-limit failures
are not retried by this policy.

Migration is code-free: canonicalize paths before constructing requests, replace
context-source `path: "."` with explicit root-level files/directories, and split
inputs that exceed a hard ceiling. Rollback is also code-only: revert the
limits/validation integration and restore the prior parser, constructors, and
provider read boundary; no stored data migration is required.

## Structured review stages

Pass `--stages-dir` to replace the packaged default stage with an ordered set of
JSON stage files. Reusable review categories—also called lenses—can live in a
separate directory with one JSON file per category:

```json
{
  "id": "correctness",
  "title": "Correctness",
  "focus": [
    "Incorrect behavior and invalid assumptions",
    "Boundary conditions and error handling"
  ],
  "applies_to": ["src/**", "tests/**"]
}
```

Stages reference the stable category ids:

```json
{
  "name": "Correctness and security",
  "outputs": ["comments"],
  "category_ids": ["correctness", "security"],
  "prompt_template": "Examine every focus item below. Use only its id as a comment category.\n<review-categories>\n{review_categories}\n</review-categories>\n<pull-request-diff>\n{diff}\n</pull-request-diff>"
}
```

Run it with:

```bash
review-sensei --diff pr.patch --stages-dir examples/stages \
  --categories-dir examples/categories \
  --context-root /path/to/target-branch-checkout
```

`--categories-dir` requires `--stages-dir`. Category files are loaded into a
validated catalog, and each stage's `category_ids` are resolved in the declared
order. An unknown or duplicate id fails before any provider call. For a small,
self-contained stage, the original inline `categories` array remains supported;
a stage cannot use inline categories and `category_ids` together.

Supported outputs are `summary`, `comments`, and `learning_proposals`.
Available placeholders are `{diff}`, `{repository}`, `{pull_request}`, `{title}`,
`{instructions}`, `{learnings}`, `{context_text}`, `{proposal_instruction}`,
`{review_categories}`, and `{review_context}`. A stage that declares categories
must include `{review_categories}`. If any of its categories declares context,
the stage must also include `{review_context}`. If the provider assigns a
category to a comment, it must use one of the active stage ids; classification
is optional for compatibility. If multiple stages reuse an id, the entire lens
definition must be identical.

Inline findings may also carry independent `blocking`, `severity`
(`critical`, `high`, `medium`, or `low`), and `fix_effort` (`trivial`, `small`,
`moderate`, `large`, or `unknown`) labels. `blocking` controls whether the
finding prevents approval; `false` identifies an optional follow-up.
If it is omitted, only case-insensitive canonical `critical` and `high`
severity values block approval. Missing, lower-severity, and legacy free-form
severity values are non-blocking.
Severity describes likely impact, while fix effort describes remediation scope
rather than a time estimate. The existing `category` id is the lens source and
is shown as a human-readable lens label; no composite priority score or second
`lens` field is emitted. When present, the same labels appear on inline GitHub
comments and deterministic blocking/severity/lens/quick-win counts appear in
the review summary. Labels are validated as printable, bounded provider text
and Markdown-escaped before publication so untrusted output cannot inject
structure or exceed the configured comment-body limit.
The formatted summary is checked against `max_summary_bytes`, and the complete
framed GitHub review body is bounded to 65,536 bytes before publication.

Stage files are trusted operator configuration because they control the model's
instructions. Category files have the same trust boundary. Load both from a
reviewed target-branch checkout or deployment bundle, never from an untrusted
pull-request head. Empty directories, duplicate ids or stage names, symlinked
files, oversized configurations, malformed categories, unknown fields or
references, and unknown placeholders fail before a provider is called. See
[`examples/categories`](examples/categories) and
[`examples/stages`](examples/stages) for a reusable catalog and three-stage
pipeline.

### Lens applicability and context

Each lens applies to the whole diff by default. Set `applies_to` to activate it
only when at least one changed repository path matches. `*` matches within one
path segment and `**` matches zero or more complete segments, so `src/**`
includes arbitrarily nested source files. Deleted paths and both sides of a
rename participate in lens selection. A lens can also select approved learning
categories and explicit document sources:

```json
{
  "id": "architecture",
  "title": "Architecture",
  "focus": [
    "Layer boundaries and dependency direction",
    "Consistency with architecture documents and decision records"
  ],
  "applies_to": ["**"],
  "context": {
    "learnings": {
      "categories": ["architecture"],
      "include_uncategorized": true
    },
    "documents": [
      {"path": "AGENTS.md"},
      {"path": "docs/architecture.md"},
      {"path": "docs/architecture", "include": ["**/*.md"]},
      {"path": "docs/adr", "include": ["**/*.md"], "exclude": ["drafts/**"]}
    ]
  }
}
```

`--context-root` identifies the trusted target-branch checkout. When omitted it
falls back to `--learning-root`; when neither is supplied, optional document
sources are skipped and required ones fail. ReviewSensei never scans the current
working directory implicitly. To intentionally select root-level Markdown files,
declare each root-level file or an explicit canonical directory (for example
`"path": "docs"`) with an `include` pattern. The root shorthand `"path": "."`
is intentionally rejected.

Document selection is deterministic and rejects traversal, symlinks, secret-like
filenames, unsupported formats, unreadable text, and oversized context. Each
document is sent with its repository-relative path and SHA-256 digest. The
packaged default pipeline includes an architecture lens whose optional sources
are `AGENTS.md`, `docs/architecture.md`, `docs/architecture/**/*.md`, and
`docs/adr/**/*.md`.

### Opt-in symbol-aware source context

By default, reviews do **not** load related source files. That keeps the
trusted context boundary at configured documents and approved learnings.
Operators may opt in to bounded Python symbol-aware context from the same
trusted base checkout:

```bash
review-sensei --diff pr.patch \
  --context-root /path/to/target-branch-checkout \
  --enable-symbol-context \
  --base-sha <trusted-base-commit-sha>
```

`--enable-symbol-context` can also be set with
`REVIEWSENSEI_ENABLE_SYMBOL_CONTEXT=true`. `--base-sha` is required when the
selector is enabled and identifies the immutable trusted-base snapshot. Optional
`--head-sha` is recorded only as untrusted coverage metadata; head trees, build
scripts, hooks, tests, and package installers are never checked out or
executed for this context.

The documented language set is Python. The selector uses static AST parsing
for enclosing functions/classes, local imports, Protocol/ABC interfaces,
same-directory callers, and conventional associated tests. JavaScript and
TypeScript files may be included as a whole-file fallback and are reported as
`unsupported-language`. Canonical path, traversal, symlink, file-type, and
secret-sensitive-file protections apply. Identical snapshot and policy inputs
produce identical excerpts and outcome ordering.

Each excerpt records path, base commit SHA, Git blob object id, line range,
selection reason, and SHA-256 digest. Ambiguous parses, excluded paths,
unsupported languages, and exhausted budgets appear in result
`source_context` coverage; they are not presented as complete understanding.
Bounded relationship expansion names the bound it reached, using
`directory-truncated` for a capped caller-directory listing and
`relations-truncated` for a capped import or caller candidate set.
When the opt-in policy is enabled, exhausting the configured file or byte
budget fails closed. Evaluation under
[#33](https://github.com/malsabbagh/review-sensei/issues/33) is required
before this selector can become the default.

## Repository-local learnings

Durable repository knowledge lives in the reviewed repository under
`.github/review-sensei/learnings/`, with one JSON object per file. These files are
normal repository content, so their history, review, merge, and rollback are
visible to maintainers.

```json
{
  "id": "provider-boundary",
  "title": "Keep provider calls behind adapters",
  "rule": "Review business logic must not import provider SDKs.",
  "scope": ["src/**"],
  "rationale": "Provider changes should not alter review behavior.",
  "category": "architecture",
  "source": "https://github.com/owner/repository/pull/123"
}
```

Only `active` entries are loaded. Scope patterns are matched against changed
repository paths; use `["*"]` for a repository-wide rule. The learning root
must be a checkout of the target branch or target commit, not the untrusted
head checkout. This prevents a pull request from changing its own review
context before it is merged.

Existing files may omit lifecycle metadata. Optional bounded fields include
`owner`, `provenance`, `reviewed_at`, `expires_at`, `supersedes`, and
`superseded_by`. Status may be `active`, `retired`, or `superseded`.
Superseded and retired entries stay on disk for provenance and Git rollback,
but they are not selected for later prompts. Diagnostics for stale,
conflicting, missing-superseder, or cyclic entries are advisory; ReviewSensei
never deletes or rewrites approved knowledge automatically.

```bash
review-sensei learnings diagnose --learning-root /path/to/target-branch --json
```

Opt-in finding feedback is a separate `learning-feedback` document with
outcomes `useful`, `incorrect`, `obsolete`, and `unverified`. Absence of
feedback is not approval, and feedback never becomes trusted review context.

Fixture evaluation can compare the same cases with and without selected
approved learnings. The report records precision/recall/false-positive deltas
as estimates, not causal proof:

```bash
review-sensei evaluate --mode fixture --corpus evaluation/v1/corpus.json \
  --compare-learnings --learning-root /path/to/target-branch
```

See [ADR 0036](docs/adr/0036-learning-lifecycle-diagnostics-and-feedback.md).

## Local Ollama

A local review defaults to the `local-ollama` backend on
`http://127.0.0.1:11434/api` with `qwen3.5:4b` and needs no credential:

```bash
review-sensei --diff pr.patch
```

Select the backend and model explicitly when you need to
(`--provider local-ollama --model qwen3.5:8b`, or `REVIEWSENSEI_PROVIDER` and
`REVIEWSENSEI_MODEL`). A backend that requires no credential never receives one
implicitly, so an exported `OLLAMA_API_KEY` is not attached to loopback
requests unless `--api-key-env` names it.

## Providers

The shipped registry adapters are `ollama`, `openai-compatible`,
`openrouter`, and the `fixture` test seam. Canonical backends — `local-ollama`,
`cloud-ollama`, `openai-compatible`, `openrouter`, and `fixture` — are what
`--provider`, `REVIEWSENSEI_PROVIDER`, and inference configuration name.
Operator defaults, workflow modes, named profiles, credentials, and hosted
OpenRouter allowlists are documented in [`docs/installation.md`](docs/installation.md).
The release-aware public matrix lives at
[reviewsensei.dev/providers/](https://reviewsensei.dev/providers/).

The core depends on this small interface:

```python
class ReviewProvider(Protocol):
    name: str
    model: str | None

    def complete(self, request: ProviderRequest) -> ProviderResponse: ...
```

Registering another provider means translating `ProviderRequest` into that
provider's API and returning `ProviderResponse`. The review service, diff
validation, output schema, and GitHub-independent behavior remain unchanged.
See [the architecture guide](docs/architecture.md).

The OpenAI-compatible adapter is restricted to `https://api.openai.com` by
default and rejects redirects so its bearer token cannot be silently forwarded
to another host. An explicitly trusted private or compatible service may be
selected from Python with `allow_custom_endpoint=True`; this is an operator
acknowledgement of data egress and still requires a non-empty API key. Custom
openers own their transport and redirect policy, while the built-in opener uses
the system CA store (or an explicitly configured, regular `SSL_CERT_FILE`) and
reuses one verified TLS context for the provider lifetime. Local Ollama and
Ollama Cloud remain separate provider profiles; selecting Cloud is the explicit
network egress path for Ollama deployments.

## GitHub integration

The GitHub App opens a setup-v5 PR containing a thin caller that follows the
operator-managed `v5` public git tag. The reusable workflow prefers the
requested exact package from PyPI and falls back to its executing workflow
commit only when that package/version is unavailable. Unrelated PyPI
installation failures remain fatal. Setup provisions 15 repository variables,
including 7 boolean controls, and manages 3 generated files. Automatic review,
GitHub writes, learning proposals, learning PRs, mention replies, and artifact
upload default to `false`; automatic approval defaults to `true` but is effective
only when review and write gates are enabled. `REVIEWSENSEI_REVIEW_MODE` is a
policy selector, not a boolean control, and defaults to `merge-focused`.
The App never creates the customer-owned `OLLAMA_API_KEY` secret.

When explicitly enabled, same-repository pull requests can run automatic review
with either provider mode: cloud on a GitHub-hosted runner or local Ollama on
the labelled self-hosted runner. ReviewSensei checks out only trusted base
content, constructs a bounded diff without executing head code, validates the
model result, and publishes one exact-head App-authored review containing its
summary and valid inline changed-line comments. Learning proposals are separate
draft PRs. Their historical candidate discovery retains the 1,000-PR bound but
starts with 20-item pages and retries the same item offset with 10, 5, then 1
item only when the transport reports an oversized response. The 512 KiB
transport ceiling remains unchanged. Fork pull requests fail closed before
provider or broker access.

When automatic review and GitHub writes are enabled, automatic approval is on by
default; set `REVIEWSENSEI_AUTO_APPROVE=false` for an explicit opt-out.
ReviewSensei publishes one exact-head `COMMENT` review with its summary and
valid inline findings, and enforces through one stable `ReviewSensei` check run
bound to that head and to the App that produced it: `success` for a complete
review with no required fixes, `failure` when required fixes remain, and
`action_required` for a partial, incomplete, or unpublished review. An earlier
conclusion on the same head is superseded by a pending `in_progress` write, so
a finished review cannot stand in for one that is still running. No persistent
`REQUEST_CHANGES` is emitted as a second gate. A repository administrator must
mark `ReviewSensei` required for it to gate merges; `review-sensei doctor`
reports the check identity and producing App, and nothing reads or changes
branch protection.

The same mode also runs one shared, idempotent approval finalizer. It emits
`APPROVE` for an eligible exact head whose review is complete and qualified and
whose bounded thread sweep finds no unresolved blocking ReviewSensei root.
Unresolved non-blocking ReviewSensei findings and human threads do not withhold
approval. An unclassified ReviewSensei root, malformed or incomplete thread
data, an unverified configuration, or a draft/closed/fork/stale/App-authored PR
fails closed or reports why approval was withheld. The finalizer runs after
review publication and after the AI resolves a blocking ReviewSensei thread;
`@sensei` replies themselves remain ordinary comments. Existing approval markers
prevent duplicate approvals, and ReviewSensei never calls a merge endpoint or
enables auto-merge.

The approval policy is deliberately conservative: explicit blocking metadata,
or an unclassified canonical `critical`/`high` severity, blocks approval.
Resolving a thread manually does not start GitHub Actions, so rerun the review
workflow (or push a new head) if a manually resolved blocking root was the last
blocker. See
[ADR 0032](docs/adr/0032-blocking-finding-classification-for-approvals.md)
for the criteria and rollback path.

Manual review and authorized `@sensei` replies use the same selected provider
mode. Each authorized mention receives a temporary 👀 reaction while the reply
is generated; ReviewSensei removes it after the reply or another terminal
outcome. Follow-up mentions in the same inline or PR conversation include the
bounded prior thread, diff context, findings, and trusted-base learnings. Cloud
mode sends that bounded conversation context to Ollama Cloud; local mode keeps
it on the configured local service. Published review summaries and inline
findings also tell readers to reply with @sensei followed by their question.
`review.json` is uploaded only when
`REVIEWSENSEI_UPLOAD_ARTIFACTS=true`; setup-v5 does not create a separate
version artifact. See [installation and migration](docs/installation.md) and
the reviewable
[`setup-v5 example`](examples/github-actions/review-sensei-review.yml).

## Concurrency policy

Hosts that run more than one review worker can reuse the same deterministic
policy:

```python
from review_sensei import ProviderAdmission, ReviewConcurrencyPlan, ReviewService

plan = ReviewService(provider).concurrency_plan(request)
# Enforce plan.workflow in the run scheduler.
# Enforce plan.provider in the provider-stage scheduler.
admission = ProviderAdmission(plan.provider)
with admission.acquire():
    result = ReviewService(provider).review(request)
```

The workflow group is keyed by repository and pull request and is latest-wins.
The provider group uses the same pull-request identity, admits one active
provider stage, and does not cancel the active stage. Different pull requests
therefore remain independent. `ReviewConcurrencyPlan.for_non_review_trigger()`
creates an isolated workflow group with no provider slot for triggers that do
not request a review.

GitHub-hosted runs enforce this with native workflow `concurrency` groups
(`max_active=1`, cancel-in-progress for reviews). Local hosts and tests can
use `ProviderAdmission` to enforce the same bound in-process, including lease
release after cancellation or failure and a hard cap on waiters. ReviewSensei
does not own durable queue state, a global cross-repository lock, or
cross-process cancellation.

## Data handling

ReviewSensei sends the prompt and full diff to the selected provider. With the
workflow's default local base URL, those values stay on the configured local
Ollama service; with a cloud base URL, the diff and selected context are sent to
that provider explicitly. The engine does not promise provider retention,
training, or deletion behavior. Read the provider's current terms before
sending private or regulated source code.

See [data handling](docs/data-handling.md) for the complete contract.

## Development

```bash
python -m pip install -r requirements/ci.txt
python -m pip install -e .
python scripts/check_action_pins.py
python scripts/validate_json_contracts.py
python -m ruff format --check setup.py src tests scripts
python -m ruff check setup.py src tests scripts
python -m mypy src
python -m compileall -q src
python -m coverage run --source=review_sensei --branch -m unittest discover -s tests -v
python -m coverage report --precision=2 --show-missing --fail-under=80
python -m unittest discover -s tests -v
python -m build
git diff --check
review-sensei --help
```

See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), and
[SUPPORT.md](SUPPORT.md) before opening an issue or pull request.

## License

ReviewSensei is released under the [MIT License](LICENSE). The synthetic
evaluation corpus under `evaluation/v1/` is CC0-1.0. For the ownership
inventory, trademark usage, contribution licensing (DCO; no CLA), and
Community / Managed / Enterprise boundary, see
[`docs/ownership-and-licensing.md`](docs/ownership-and-licensing.md),
[`TRADEMARKS.md`](TRADEMARKS.md), [`CONTRIBUTING.md`](CONTRIBUTING.md), and
[ADR 0040](docs/adr/0040-community-managed-enterprise-boundary.md).
