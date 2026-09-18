# Installation

## From npm (launcher)

Node.js 22+ is required. The launcher introduced by this change is a thin, shell-free
`npx` entry point for the existing ReviewSensei Python engine; it does not
contain a JavaScript engine, system-Python fallback, runtime downloader, or
install lifecycle hook. An external `git` executable is also required for
`prepare-diff`.

```bash
npx --yes @reviewsensei/cli@0.5.0 --version
npx --yes @reviewsensei/cli@0.5.0 prepare-diff \
  --repository . --base-ref main --head-ref feature --output pr.patch
```

The launcher and all optional platform packages use one exact version from
`pyproject.toml` and the signed `vX.Y.Z` tag. Supported targets are macOS
arm64/x64, glibc Linux arm64/x64, and Windows x64. Linux musl and unsupported
architectures fail before any executable is spawned. Provider flags and
configuration variables are the same as the Python CLI, including explicit
local versus Ollama Cloud mode.

If npm reports that the optional package is missing or has a version mismatch,
reinstall the exact launcher version; do not run a downloader or fall back to
system Python. For a release incident, use a higher patch version rather than
overwriting an npm tarball or moving a tag.

## Diagnose installation and preview a review

Offline doctor never opens sockets, mints tokens, or calls a model:

```bash
review-sensei doctor --json
```

Repair actions:

| Check | Typical repair |
| --- | --- |
| `package` action | Reinstall the exact `review-sensei` version; do not mix checkout `src/` with a wheel. |
| `packaged-assets` action | Reinstall the package so default stages/categories are present. |
| `stages`/`categories` action | Point `--stages-dir`/`--categories-dir` at trusted-base directories that contain valid JSON, not a PR-head copy. |
| `provider-mode` action | Set `REVIEWSENSEI_PROVIDER_MODE` to `local` or `cloud` (Ollama only; not OpenRouter). |
| `endpoint` action | Start a local Ollama runner on loopback, or correct `OLLAMA_BASE_URL`. |
| `model` action | `ollama pull` the configured model. |
| `repository-metadata` action | Use a read-only `GITHUB_TOKEN` you already have; doctor never mints a broker token. |
| `compatibility` action | Supply a validated compatibility manifest path. |

Optional `--network` probes are read-only GETs. Example local output:

```text
status: pass
version: 0.5.0
pass: package — 0.5.0
pass: packaged-assets — default stages and categories available
pass: provider-mode — local (offline check)
pass: stages — packaged default stages selected
pass: categories — packaged default categories selected
pass: context — no supplemental context configured
pass: endpoint — local runner endpoint reachable
pass: model — configured model is installed
unknown: repository-metadata — repository metadata not checked (repository not supplied)
unknown: compatibility — compatibility evidence not supplied
```

Preview a review without provider or GitHub writes:

```bash
review-sensei plan --diff pr.patch --repository owner/repo --pull-request 42 \
  --base-sha <base> --head-sha <head> --json
```

The plan always reports `provider_calls=0` and `github_writes=0`. See
[`docs/diagnostics.md`](diagnostics.md) for exit codes.

## From source

```bash
git clone https://github.com/malsabbagh/review-sensei.git
cd review-sensei
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

## Distribution test contract

The package job (and the local commands below) prove that tests load only
packaged or generated assets. After `python -m build --outdir dist`, record
`sha256sum dist/*.tar.gz dist/*.whl`, install **only** the wheel into a clean
virtualenv, extract the sdist, and run the dist-safe suite from that unpacked
`tests/` tree with `REVIEWSENSEI_DIST_SAFE_LANE=1` and
`REVIEWSENSEI_CHECKOUT_ROOT` pointing at the developer checkout.

The import guard is armed by default, so it cannot be lost by forgetting a
variable: `review_sensei.__file__` must resolve under `sys.prefix`, meaning an
editable install or an unrelated checkout cannot stand in for the wheel you just
built. `REVIEWSENSEI_CHECKOUT_ROOT` adds the stricter rule that the package must
not resolve under that checkout's `src/`, and must name a directory containing
`src/` so a typo fails closed. `REVIEWSENSEI_DIST_SAFE_LANE=1` marks the suite as
the packaged one so checkout-only modules are asserted absent. Set
`REVIEWSENSEI_ALLOW_CHECKOUT_IMPORT=1` only to opt out deliberately.

```bash
python -m build --outdir dist
sha256sum dist/*.tar.gz dist/*.whl
python -m venv /tmp/review-sensei-wheel-venv
/tmp/review-sensei-wheel-venv/bin/python -m pip install dist/*.whl
sdist_root=/tmp/review-sensei-sdist
rm -rf "$sdist_root"
mkdir -p "$sdist_root"
tar -xzf dist/*.tar.gz -C "$sdist_root"
suite="$(printf '%s\n' "$sdist_root"/review_sensei-*/tests | head -n 1)"
export REVIEWSENSEI_CHECKOUT_ROOT="$PWD"
export REVIEWSENSEI_DIST_SAFE_LANE=1
/tmp/review-sensei-wheel-venv/bin/python -I -m unittest discover -s "$suite/dist_safe" -v
/tmp/review-sensei-wheel-venv/bin/python -I -m unittest discover -s "$suite/downstream" -v
```

Checkout-only tests (`scripts/`, `.github/`, `packages/`, `deploy/`, and the
broader `tests/test_*.py` modules) stay in the developer checkout lane:
`python -m unittest discover -s tests -v`. The live downstream canary workflow
is operator-gated and is not a required CI job.

## Configuration

Copy the documented variables from [`.env.example`](../.env.example) into the
environment used to run ReviewSensei. The CLI reads:

| Variable | Default | Purpose |
| --- | --- | --- |
| `REVIEWSENSEI_PROVIDER` | `ollama` | Provider registry key |
| `REVIEWSENSEI_PROVIDER_MODE` | `local` | `local` keeps requests on loopback; `cloud` selects Ollama Cloud |
| `REVIEWSENSEI_LOCAL_MODEL` | `qwen3.5:4b` | Local Ollama model |
| `REVIEWSENSEI_CLOUD_MODEL` | `deepseek-v4.1-flash:cloud` | Ollama Cloud model |
| `OLLAMA_BASE_URL` | mode-specific | Optional explicit Ollama API root override |
| `OLLAMA_MODEL` | mode-specific | Optional explicit model override |
| `OLLAMA_API_KEY` | empty | Optional bearer credential |
| `OLLAMA_TIMEOUT_SECONDS` | `900` | Total request timeout |

Optional GitHub App-identity publication uses `GITHUB_APP_PRIVATE_KEY` by
default through `EnvPrivateKeySource`. The App id is passed when constructing
`GitHubAppAuth`. See [`docs/github-app-auth.md`](github-app-auth.md) for
registration and secrets guidance.

The default configuration is local-first: `REVIEWSENSEI_PROVIDER_MODE=local`
selects `qwen3.5:4b`, points at a local Ollama API, and leaves
`OLLAMA_API_KEY` empty. Cloud egress is explicit opt-in: set
`REVIEWSENSEI_PROVIDER_MODE=cloud` and `OLLAMA_API_KEY`; the default cloud model
is `deepseek-v4.1-flash:cloud`. `OLLAMA_BASE_URL` and `OLLAMA_MODEL` remain
available as explicit overrides.

Optional CLI `--profile` selects a named preset (`local-private`,
`fast-triage`, `deep-verification`, `openrouter-sonnet`, `openrouter-gpt`)
without changing these workflow defaults. Installed GitHub workflows continue to
use `REVIEWSENSEI_PROVIDER_MODE` and do not pass `--profile`. `fast-triage` is
an explicit CLI/OpenAI path and requires `OPENAI_API_KEY`; it is not enabled by
the reusable workflow.

Installed GitHub workflows select the backend with one variable and one shared
model:

| `REVIEWSENSEI_PROVIDER_MODE` | Where it runs | Secret | Default model when `REVIEWSENSEI_MODEL` is empty |
| --- | --- | --- | --- |
| `local` or `local-ollama` (default) | Self-hosted runner labelled `ollama` | none | `qwen3.5:4b` |
| `cloud` or `cloud-ollama` | `ubuntu-latest` + Ollama Cloud | `OLLAMA_API_KEY` | `deepseek-v4.1-flash:cloud` |
| `openrouter` | `ubuntu-latest` + OpenRouter | `OPENROUTER_API_KEY` | `deepseek/deepseek-v4.1-flash` |

Set `REVIEWSENSEI_MODEL` to override the model for whichever backend is
selected. The slug must match the active backend shape (Ollama slugs for
local/cloud jobs, vendor/model slugs for OpenRouter); cross-backend values
fail closed when each provider job resolves and validates its fallback chain.
When `REVIEWSENSEI_MODEL` is empty, the hosted workflow resolves the model
per backend:

- **Local Ollama jobs** use `REVIEWSENSEI_LOCAL_MODEL`, then `qwen3.5:4b`.
- **Cloud Ollama jobs** use `REVIEWSENSEI_CLOUD_MODEL`, then
  `deepseek-v4.1-flash:cloud`.
- **OpenRouter jobs** use `REVIEWSENSEI_MODEL` when set (must be an
  allowlisted vendor/model slug), otherwise the hosted allowlist default
  (`deepseek/deepseek-v4.1-flash`). They do not consult
  `REVIEWSENSEI_LOCAL_MODEL`, `REVIEWSENSEI_CLOUD_MODEL`, or
  `OPENROUTER_MODEL` (CLI-only).

Legacy `REVIEWSENSEI_LOCAL_MODEL` and `REVIEWSENSEI_CLOUD_MODEL` apply only
to their respective Ollama jobs. Hosted OpenRouter accepts only the published
allowlist in
`provider_config.HOSTED_OPENROUTER_DEFAULTS` (default
`deepseek/deepseek-v4.1-flash`, plus `anthropic/claude-3.5-sonnet` and
`openai/gpt-4o-mini`).

OpenRouter CLI flags and environment (used with `--provider openrouter` or
`--profile openrouter-sonnet` / `openrouter-gpt` for local CLI runs):

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | empty | Required bearer credential for OpenRouter modes |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | Allowlisted OpenRouter API root |
| `OPENROUTER_MODEL` | `deepseek/deepseek-v4.1-flash` | Default model for unprofiled OpenRouter CLI runs |
| `OPENROUTER_UPSTREAM_PROVIDER` | `deepseek` | Upstream slug for unprofiled OpenRouter routing policy |
| `OPENROUTER_TIMEOUT_SECONDS` | `120` | Request timeout |
| `REVIEWSENSEI_OPENROUTER_TIMEOUT_SECONDS` | unset | Overrides `OPENROUTER_TIMEOUT_SECONDS` when set |

`doctor` and `plan` report credential presence only; they never print the key.
OpenRouter CLI profiles start `unqualified` and require
`--allow-unqualified-profile` for live review until separate qualification
evidence exists. Hosted workflows reject `allow_unqualified_profile=true` and
do not forward `--allow-unqualified-profile`; use the CLI for unqualified
profile runs. Hosted OpenRouter uses `--provider openrouter --model` and
derives `OPENROUTER_UPSTREAM_PROVIDER` from the hosted OpenRouter model
allowlist in `provider_config.py`.
Every OpenRouter request includes `provider.data_collection=deny` (plus
no fallbacks and zero-data-retention) through the typed routing policy.

To roll back from OpenRouter, set `REVIEWSENSEI_PROVIDER_MODE` back to `local`
or `cloud`, and remove `OPENROUTER_API_KEY` when it is no longer needed.

## GitHub workflow example

The setup-v4 caller example in
[`examples/github-actions/review-sensei-review.yml`](../examples/github-actions/review-sensei-review.yml)
uses the operator-managed `v4` git tag directly. The Worker validates that tag
during installation/reconciliation, and the broker resolves the same tag when
authorizing a run (using the public Git ref advertisement before a bounded REST
fallback on GitHub.com). That workflow
tries to install the exact `REVIEWSENSEI_VERSION` from PyPI in a separate
`RUNNER_TEMP` environment. When that exact distribution/version is unavailable,
it installs from the public ReviewSensei repository at the executing workflow
commit SHA;
other PyPI failures remain fatal, including network and authentication errors.
Both paths must prove the intended release identity against a validated
compatibility manifest through `prove_release_identity`, which checks the
executing workflow commit, every manifest artifact digest, and Worker
compatibility range membership. The reusable workflow still performs the
version-string and executing-SHA checks; digest/manifest verification is the
implemented release contract in `review_sensei.release_manifest` and is not
satisfied by fetching a checksum beside an untrusted artifact. Live
disposable-repository canary evidence from #34 is operator-only; fixture
downstream tests bind the exact manifest digest before any `v4` promotion.
It then checks out only trusted base content, validates refs, and computes a
bounded diff without installing or executing the
head branch.

The App creates repository variables for provider mode/model selection, package
version, independent analysis/publication controls, learning proposals and
learning PRs, mention replies, artifact upload, and optional trusted stage and
category directories (`REVIEWSENSEI_STAGES_DIR`, `REVIEWSENSEI_CATEGORIES_DIR`).
Feature switches default to `false`; empty stage/category paths preserve the
packaged defaults. Manual dispatch may override those directories; both the
variable and the input are repository-relative paths loaded from the trusted
base checkout after authoritative PR preflight. Pull-request head edits to
custom stage or category JSON cannot change the instructions used for that
review. A stage whose lenses are all inactive for the diff makes zero provider
calls; a category-less independent-output stage still runs once.
The generated setup exposes nine independent operational controls so analysis,
publication, approval, learning, replies, and artifact retention can be enabled
separately.
The five setup files and generated workflow references remain byte-addressed
and are migrated only when their managed content matches exactly.
The App does not create a placeholder secret or overwrite an existing variable.

To enable automatic same-repository analysis, set
`REVIEWSENSEI_AUTO_REVIEW=true`. Draft pull requests skip that automatic run
until they are marked ready for review. Set `REVIEWSENSEI_GITHUB_WRITES=true`
separately when the validated result may publish to GitHub; analysis remains
provider-only while writes are disabled. Set
`REVIEWSENSEI_LEARNING_PROPOSALS=true` independently when model-generated
learning proposals are desired, and `REVIEWSENSEI_LEARNING_PRS=true` to publish
those proposals as draft PRs.
`REVIEWSENSEI_PROVIDER_MODE=cloud` or `cloud-ollama` runs on `ubuntu-latest`
and requires the customer-owned `OLLAMA_API_KEY` under Repository Settings →
Secrets and variables → Actions. `REVIEWSENSEI_PROVIDER_MODE=local` or
`local-ollama` runs the same review path on the labelled self-hosted runner.
`REVIEWSENSEI_PROVIDER_MODE=openrouter` runs on `ubuntu-latest` with
`OPENROUTER_API_KEY` and `REVIEWSENSEI_MODEL`. All modes reject fork heads before provider
or broker access and can publish one exact-head App review with valid inline
comments and a validated summary. Enable `REVIEWSENSEI_LEARNING_PRS` separately for deterministic draft
learning PRs.

With automatic review and GitHub writes enabled, clean eligible reviews can
satisfy branch-protection approvals automatically by default. Set
`REVIEWSENSEI_AUTO_APPROVE=false` to publish `COMMENT` instead. Blocking
findings are published as `REQUEST_CHANGES`, then a shared idempotent
finalizer may emit `APPROVE` once no unresolved ReviewSensei root is classified
blocking. A later execution on the same head can still request changes after
an earlier approval, and can approve after an earlier change request only when
those blocking roots are resolved. Non-blocking ReviewSensei follow-ups and
human threads may remain open. An unclassified ReviewSensei root,
partial/incomplete/summary-only result, incomplete/error thread response, or
stale/fork/closed/App-authored target remains a comment or fails closed before
writing. All `@sensei` replies remain ordinary comments.

The approval marker deduplicates the `APPROVED` state per exact head. The
finalizer runs immediately after review publication and after an AI reply
successfully resolves a blocking root. A maintainer resolving a blocking root
manually still needs to rerun **ReviewSensei review** (supplying the current
exact PR head and base) or push a new head because GitHub Actions does not start
a run for thread resolution alone. Classification metadata
(blocking, severity, fix effort, and lens) controls the approval boundary:
explicit `false` does not prevent approval, while an omitted flag is
blocking only for case-insensitive `critical` or `high` severity; missing,
lower-severity, and legacy free-form values are non-blocking. See
[ADR 0032](adr/0032-blocking-finding-classification-for-approvals.md)
for the full criteria and rollback procedure.

For local Ollama, install and pull `qwen3.5:4b` on the labelled self-hosted
runner. Automatic reviews, manual reviews, learning proposals, artifact upload,
and authorized `@sensei` replies are available in both provider modes. A cloud
reply sends its bounded thread and review context to Ollama Cloud; local mode
keeps that context on the configured local service.
`review.json` is uploaded only when `REVIEWSENSEI_UPLOAD_ARTIFACTS=true`; there
is no separate version artifact. The generated **Remove ReviewSensei setup**
workflow opens a cleanup PR and leaves learnings, variables, and secrets for
explicit operator review.

If the GitHub App was already installed before a setup version change, do not
remove the generated files manually. Deploy the updated Worker, then accept a
pending App permission update or remove and re-add the repository to generate a
fresh setup delivery. The bootstrap identifies older generated files, including
a byte-exact managed v3 workflow (including its older SHA pin), and opens a
migration PR that updates only those files to setup-v4. It also migrates a
managed v4 caller that follows an older tag. It skips custom or future versions
for manual review and preserves learnings, existing variables, and secrets.

## Setup-v4 and tag-based reusable workflow

The current generated setup is version 4. The caller follows the public
`malsabbagh/review-sensei` reusable workflow at the operator-managed `v4` git
tag and passes only bounded event inputs. It requests `contents: read`,
`pull-requests: read`, `issues: read`, and `id-token: write`; generated write
and artifact switches are all `false`.
The caller passes an explicit provider mode to one provider-neutral reusable
job. Cloud review and reply operations run on `ubuntu-latest`; local review and
reply operations run on `[self-hosted, linux, x64, ollama]`. Both modes support
automatic review, manual review, validated review publication, learning draft
PRs, optional artifacts, and authorized `@sensei` conversations. An authorized
mention receives 👀 while the response is being generated, and the reaction is
removed after the reply or another terminal outcome. The provider may include
`resolve: true` in its validated reply when the current exact-head context
shows that a ReviewSensei-authored inline finding is fully addressed; the
publisher then resolves only that thread through a bounded GraphQL mutation.
Issue comments and human-authored roots remain open. Add another standalone
`@sensei` mention in the same thread to continue the bounded conversation. A
successful AI resolution automatically causes one fresh same-head review pass;
approval still requires the ordinary no-blocker and all-threads-resolved gates.

The generated caller may contain the literal name-only mapping
`OLLAMA_API_KEY: ${{ secrets.OLLAMA_API_KEY }}`. This does not read
secret values during setup and the App never creates, retrieves, logs,
persists, or interpolates a secret value into generated output. Add the
customer-owned `OLLAMA_API_KEY` yourself only when explicitly enabling cloud
mode. `OPENROUTER_API_KEY` is forwarded by generated callers only after the
public `v4` tag includes the reusable-workflow contract.

Setup reconciliation is PR-only and changes only the three generated paths.
`installation.created` (including reinstall),
`installation.new_permissions_accepted`, and
`installation_repositories.added` inspect absent, legacy, and v2 clients.
They reuse an existing open setup PR and produce at most one deterministic
`review-sensei/setup-v4-<base12>-<tag>` PR for the exact base and update
channel. Current v4 is a no-op. A byte-exact
managed v3 workflow or a managed v4 workflow following another valid tag is
stale and is migrated; custom,
malformed, and future versions are skipped without writes. A deployment alone
does not replay old events.

Released pre-marker and setup-v2 clients are recognized from exact historical
generated bytes. Present setup-v3 files must exactly match generated files; a
review workflow that exactly matches its own older valid public workflow SHA is
recognized as managed stale content, while retained markers do not make edited
content migratable. The current setup-v4 workflow contains exactly one valid
tagged reusable-workflow reference; the byte-exact previously released two-job
v4 caller remains recognizable only for managed migration. Setup branches are
create-only and bind the base
and update channel in their names. Existing branches are reused only after App-author, exact-parent,
canonical-content, and generated-path checks. A pre-existing or concurrent
collision reports `skipped_branch_conflict`; the App never force-moves a ref.
The broker resolves the configured tag at capability exchange time and requires
the runtime workflow SHA to match that resolution; a caller using another ref
is rejected.

The release order is: merge the implementation; publish the audited public
snapshot; set Worker `PUBLIC_WORKFLOW_TAG` only; deploy the Worker; move the
public `v4` tag to that same snapshot; update and accept
App permissions or reinstall/re-add the App;
verify setup PR reconciliation; merge desired setup PRs; enable repository
opt-ins; and only then run hosted acceptance. Rollback is a code-only
revert/redeploy that preserves delivery and broker ledger migrations; close or
revert unmerged setup PRs rather than writing the default branch directly.
