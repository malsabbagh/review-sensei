# Installation

## From npm (launcher)

Node.js 22+ is required. The launcher introduced by this change is a thin, shell-free
`npx` entry point for the existing ReviewSensei Python engine; it does not
contain a JavaScript engine, system-Python fallback, runtime downloader, or
install lifecycle hook. An external `git` executable is also required for
`prepare-diff`.

```bash
npx --yes @reviewsensei/cli@0.6.8 --version
npx --yes @reviewsensei/cli@0.6.8 prepare-diff \
  --repository . --base-ref main --head-ref feature --output pr.patch
```

The launcher and all optional platform packages use one exact version from
`pyproject.toml` and the signed `vX.Y.Z` tag. Supported targets are macOS
arm64/x64, Linux arm64/x64 with glibc 2.36 or newer, and Windows x64. Linux
musl and unsupported architectures fail before any executable is spawned.
Provider flags and configuration variables are the same as the Python CLI,
including explicit local versus Ollama Cloud mode.

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
| `provider-mode` action | Select `inference.backend` in `.reviewsensei.yml` (`local-ollama` or `cloud-ollama`; Ollama only, not OpenRouter). |
| `endpoint` action | Start a local Ollama runner on loopback, or correct `OLLAMA_BASE_URL`. |
| `model` action | `ollama pull` the configured model. |
| `repository-metadata` action | Use a read-only `GITHUB_TOKEN` you already have; doctor never mints a broker token. |
| `compatibility` action | Supply a validated compatibility manifest path. |

Optional `--network` probes are read-only GETs. Example output from a bare
directory with only the packaged defaults — no repository, session ledger, or
network probe — so the automated-review admission check reports `action` and
doctor exits `2`:

```text
status: action
version: 0.6.8
pass: package — 0.6.8
pass: packaged-assets — default stages and categories available
pass: provider-mode — local (offline check)
pass: review-convergence — mode=merge-focused enforcement=publication compatibility=explicit-opt-in rounds=uncapped failed_attempts=6
action: automation-admission — operator mode cannot report automated-review admission without a session ledger
pass: verification-scope — status=baseline-required round=initial late_admission=False reason=missing-baseline
pass: stages — packaged default stages selected
pass: categories — packaged default categories selected
pass: context — no supplemental context configured
pass: symbol-context — opt-in trusted-base symbol context is disabled by default
pass: review-gate — the merge gate is the check 'ReviewSensei' produced by App 'reviewsensei[bot]'; 'ReviewSensei' must be marked required by a repository administrator (ReviewSensei cannot read or change branch protection), the App needs Checks: write, and GitHub never runs required checks on App-authored pull requests
unknown: network — not checked (offline mode)
```

`status: pass` (exit `0`) appears only when every configured check passes —
including `automation-admission`, which reports `pass` when the session ledger
for the configured repository and pull request admits the round. `review-gate`
reports `pass` as guidance that the gate contract is understood, not as proof
that branch protection is configured. See
[`docs/diagnostics.md`](diagnostics.md) for the exit-code contract.

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

A repository selects its backend and policy in `.reviewsensei.yml` at the
repository root; `--config` selects another path, and a repository without the
file uses the packaged defaults:

| Field | Default | Purpose |
| --- | --- | --- |
| `inference.backend` | `local-ollama` | `local-ollama`, `cloud-ollama`, `openrouter`, or `openai-compatible` |
| `inference.model` | backend default | Model slug; a slug of the wrong shape for the selected backend fails closed |
| `github.automatic_reviews` | `true` | Automatic review of eligible pull requests |
| `github.writes` | `false` | Publication of the validated result to GitHub |
| `github.reviews` | `auto-approve` | Review policy: `auto-approve`, `blocking`, or `advisory` |
| `github.mentions` | `true` | Authorized `@sensei` conversation |
| `github.learning` | `disabled` | Learning mode: `disabled`, `proposals`, or `pull-requests` |
| `github.artifacts` | `none` | Artifact mode: `none` or `diagnostics` |

The packaged model for the selected backend applies unless `inference.model`
names another one. `REVIEWSENSEI_PROVIDER` and `REVIEWSENSEI_MODEL` are the
only supported environment overrides: they map to `inference.backend` and
`inference.model`, an explicit `--provider` or `--model` wins over them, and
`.reviewsensei.yml` is the source of record. The retired
`REVIEWSENSEI_PROVIDER_MODE`, `OLLAMA_MODEL`, `REVIEWSENSEI_LOCAL_MODEL`, and
`REVIEWSENSEI_CLOUD_MODEL` settings have no effect; `review-sensei config`
reports each one with its replacement instead of reading it.
`review-sensei config validate` checks the file and the effective
provider/model combination, and `review-sensei config show [--explain]` prints
the effective configuration with optional provenance.

The packaged backend defaults are:

| `inference.backend` | Where it runs | Credential | Default model |
| --- | --- | --- | --- |
| `local-ollama` (default) | Self-hosted runner labelled `ollama`, loopback API | none required | `qwen3.5:4b` |
| `cloud-ollama` | `ubuntu-latest` + Ollama Cloud | `OLLAMA_API_KEY` | `deepseek-v4.1-flash:cloud` |
| `openrouter` | `ubuntu-latest` + OpenRouter | `OPENROUTER_API_KEY` | `deepseek/deepseek-v4.1-flash` |
| `openai-compatible` | `ubuntu-latest` + a gateway you select | `OPENAI_API_KEY`, or `advanced.endpoint.credential_env` | `gpt-4o-mini` |

The default configuration is local-first and leaves `OLLAMA_API_KEY` empty.
Cloud egress is explicit opt-in: select `cloud-ollama` and provide
`OLLAMA_API_KEY`. A custom gateway is declared in `.reviewsensei.yml` under
`advanced.endpoint` with `allow_custom_endpoint: true`, the gateway `base_url`,
and the name of the environment variable that holds its credential. The model
slug must match the selected backend shape (Ollama slugs for local/cloud,
`vendor/model` slugs for OpenRouter); a cross-backend value fails closed before
any provider call.

Endpoint, timeout, and credential selection come from the command line flags
(`--base-url`, `--allow-custom-endpoint`, `--timeout-seconds`, `--api-key-env`)
or the selected backend's documented defaults; no other environment variable
moves a review's endpoint or timeout. A backend that requires no credential
never receives one implicitly: an exported cloud key does not travel to a local
endpoint unless `--api-key-env` names it.

Optional GitHub App-identity publication uses `GITHUB_APP_PRIVATE_KEY` by
default through `EnvPrivateKeySource`. The App id is passed when constructing
`GitHubAppAuth`. See [`docs/github-app-auth.md`](github-app-auth.md) for
registration and secrets guidance.

Optional CLI `--profile` selects a named preset (`local-private`,
`fast-triage`, `deep-verification`, `openrouter-sonnet`, `openrouter-gpt`)
without changing the configuration file. Installed GitHub workflows never pass
`--profile`. `fast-triage` is an explicit CLI/OpenAI path and requires
`OPENAI_API_KEY`; it is not enabled by the reusable workflow. Hosted OpenRouter
accepts only the published allowlist in
`provider_config.HOSTED_OPENROUTER_DEFAULTS` (default
`deepseek/deepseek-v4.1-flash`, plus `anthropic/claude-3.5-sonnet` and
`openai/gpt-4o-mini`).

OpenRouter credentials and routing (used with `--provider openrouter`, the
`openrouter-sonnet` / `openrouter-gpt` profiles, and hosted workflow lanes):

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | empty | Required bearer credential for OpenRouter modes |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | Allowlisted OpenRouter API root |
| `OPENROUTER_MODEL` | `deepseek/deepseek-v4.1-flash` | Default model for unprofiled OpenRouter CLI runs |
| `OPENROUTER_TIMEOUT_SECONDS` | `120` | Request timeout |
| `REVIEWSENSEI_OPENROUTER_TIMEOUT_SECONDS` | unset | Overrides `OPENROUTER_TIMEOUT_SECONDS` when set |

A local `--provider openrouter` review fixes its API root at
`https://openrouter.ai/api/v1` and its timeout at the documented backend
default: `OPENROUTER_BASE_URL`, `OPENROUTER_MODEL`,
`OPENROUTER_TIMEOUT_SECONDS`, and `REVIEWSENSEI_OPENROUTER_TIMEOUT_SECONDS`
are not consulted by it. Pass `--base-url`, `--model`, or `--timeout-seconds`
explicitly instead.

`doctor` and `plan` report credential presence only; they never print the key.
OpenRouter CLI profiles start `unqualified` and require
`--allow-unqualified-profile` for live review until separate qualification
evidence exists. Hosted workflows reject `allow_unqualified_profile=true` and
do not forward `--allow-unqualified-profile`; use the CLI for unqualified
profile runs. Hosted OpenRouter runs the provider arguments the resolved plan
emitted (`--provider`, `--base-url`, `--model`) and derives the upstream
provider from the hosted OpenRouter model allowlist in `provider_config.py`;
upstream routing is declared in `.reviewsensei.yml` under
`advanced.routing.upstream_provider`, and the retired
`OPENROUTER_UPSTREAM_PROVIDER` variable is reported by `review-sensei config`.
Every OpenRouter request includes `provider.data_collection=deny` (plus
no fallbacks and zero-data-retention) through the typed routing policy.

To roll back from OpenRouter, set `inference.backend` back to `local-ollama` or
`cloud-ollama` in `.reviewsensei.yml`, and remove `OPENROUTER_API_KEY` when it
is no longer needed.

## GitHub workflow example

The setup-v5 caller example in
[`examples/github-actions/review-sensei-review.yml`](../examples/github-actions/review-sensei-review.yml)
uses the operator-managed `v5` git tag directly. The Worker validates that tag
during installation/reconciliation, and the broker resolves the same tag when
authorizing a run (using the public Git ref advertisement before a bounded REST
fallback on GitHub.com). That workflow
installs the exact release its workflow commit belongs to (the version its
`pyproject.toml` declares) from PyPI in a separate `RUNNER_TEMP` environment;
no repository variable selects it. When that exact distribution is unavailable,
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
downstream tests bind the exact manifest digest before any workflow-channel promotion.
It then checks out only trusted base content, validates refs, and computes a
bounded diff without installing or executing the
head branch.

The App creates no repository variables: every behavioral decision is a field
of `.reviewsensei.yml`, and the generated setup contains 3 generated files:
`.github/workflows/review-sensei-review.yml`,
`.github/workflows/review-sensei-uninstall.yml`, and the root
`.reviewsensei.yml` the operator owns from the first install on. The only two
optional Actions overrides the product consumes are `REVIEWSENSEI_PROVIDER`
(`inference.backend`) and `REVIEWSENSEI_MODEL` (`inference.model`); the
reusable workflow maps them once from the trusted policy commit, and no other
variable, boolean, or path reaches a run. A repository that still carries a
retired managed variable (`REVIEWSENSEI_AUTO_REVIEW`,
`REVIEWSENSEI_AUTO_APPROVE`, `REVIEWSENSEI_GITHUB_WRITES`,
`REVIEWSENSEI_MENTION_REPLIES`, `REVIEWSENSEI_LEARNING_PROPOSALS`,
`REVIEWSENSEI_LEARNING_PRS`, `REVIEWSENSEI_UPLOAD_ARTIFACTS`,
`REVIEWSENSEI_REVIEW_MODE`, `REVIEWSENSEI_VERSION`,
`REVIEWSENSEI_STAGES_DIR`, or `REVIEWSENSEI_CATEGORIES_DIR`) sees it reported
with its `.reviewsensei.yml` replacement on each run and otherwise ignored.

Stage and category documents live in the conventional directories
`.reviewsensei/stages/` and `.reviewsensei/categories/` beside the
configuration root of the trusted base commit; the retired
`REVIEWSENSEI_STAGES_DIR` and `REVIEWSENSEI_CATEGORIES_DIR` variables are
reported with that replacement. Pull-request head edits to custom stage or
category JSON cannot change the instructions used for that review. A stage
whose lenses are all inactive for the diff makes zero provider calls; a
category-less independent-output stage still runs once.

The 3 generated files are byte-addressed and are migrated only when their
managed content matches exactly. Editing a generated file makes the
installation custom (`unknown`) and prevents automatic overwrites, in both
historical and current setup versions; reconcile custom files manually rather
than weakening managed-file recognition, and configure behavior in
`.reviewsensei.yml`. The App requests no Variables permission, does not create
a placeholder secret, and never creates the customer-owned provider secrets.

Every hosted review requires a trusted session ledger for admission
and duplicate suppression: it runs through the reusable workflow,
which invokes `review-sensei github review` with the broker-attested
`--github-session-ledger`. A default setup-v5 installation therefore completes
reviews without any additional ledger provisioning. The analysis job and the
publication job of one run resolve the same broker-attested comment ledger, so
rounds, baselines, and dispositions recorded by one job are visible to the next
instead of living on a runner filesystem no later job can read; the first job
of a fresh installation enrolls the marker when the broker reports no prior
session. The ledger requirement is
operator-visible only on the local CLI path: `review-sensei github review`
without `--session-ledger` or `--github-session-ledger` reports `doctor`
`status=action` (exit 2) and `plan` lists `session-ledger-required`, and an
unadmitted round is skipped with zero provider calls rather than silently
downgraded. See [`docs/diagnostics.md`](diagnostics.md) and
[ADR 0055](adr/0055-merge-focused-default-and-legacy-retirement.md).

To enable automatic same-repository analysis, set
`github.automatic_reviews: true` (the default). Draft pull requests skip that
automatic run until they are marked ready for review. Set
`github.writes: true` separately when the validated result may publish to
GitHub; analysis remains provider-only while writes are disabled. Model-
generated learning proposals require `github.learning: proposals`, and
`github.learning: pull-requests` additionally publishes those proposals as
draft PRs. `github.writes: true` also exposes the maintainer command
surface, independently of `github.mentions`. An authorized human
`OWNER`, `MEMBER`, or `COLLABORATOR` may post `@sensei review
status|pause|continue|reenroll`, `@sensei verify`, or `@sensei
dismiss|defer|accept-risk <fingerprint> --reason <text>` on a pull request;
those comments take the command path whenever writes are enabled, while
`github.mentions` continues to control only conversational
replies. The command path is the pull-request conversation: an inline review
comment on a diff line always resolves as a conversational reply and therefore
still requires `github.mentions`. The caller workflow's `@sensei`
and association checks are routing gates, not the authorization decision: every
mutation is re-authorized inside the reusable workflow against the
broker-attested actor and the durable session ledger before any write.
`inference.backend: cloud-ollama` runs on `ubuntu-latest`
and requires the customer-owned `OLLAMA_API_KEY` under Repository Settings →
Secrets and variables → Actions. `inference.backend: local-ollama` runs the
same review path on the labelled self-hosted runner.
`inference.backend: openrouter` runs on `ubuntu-latest` with
`OPENROUTER_API_KEY` and the resolved model. All backends reject fork heads
before provider or broker access and can publish one exact-head App review with
valid inline comments and a validated summary.

With automatic review and GitHub writes enabled, clean eligible reviews can
satisfy branch-protection approvals automatically by default. Set
`github.reviews: blocking` (publish and enforce, never automatically approve)
or `github.reviews: advisory` (no ReviewSensei merge gate and no approval) in
`.reviewsensei.yml` to select another policy instead. Enforcement is
one stable `ReviewSensei` check run bound to the reviewed head and to the App
that produced it; mark it required in branch protection (an administrator
action - ReviewSensei never reads or changes branch protection, and `doctor`
reports the check identity and producing App) for it to gate merges. The
conclusion is `success` for a complete review with no required fixes, `failure`
when required fixes remain, `action_required` for a partial, incomplete, or
unpublished review, `neutral` in `advisory` mode, and `cancelled` for a run that
ended without a conclusion; no persistent `REQUEST_CHANGES` is emitted as a
second gate, and the App needs `Checks: write`. A shared idempotent finalizer may
then emit `APPROVE` once no unresolved ReviewSensei root is classified blocking.
Non-blocking ReviewSensei follow-ups and human threads may remain open. An
unclassified ReviewSensei root, partial/incomplete/summary-only result,
incomplete/error thread response, missing check permission, or
stale/fork/closed/App-authored target withholds approval or fails closed before
writing, with a bounded diagnostic. All `@sensei` replies remain ordinary
comments.

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
`review.json` is uploaded only when `github.artifacts: diagnostics` is set in
`.reviewsensei.yml`; there is no separate version artifact. The generated
**Remove ReviewSensei setup** workflow opens a cleanup PR and leaves learnings
and repository secrets for explicit operator review.

If the GitHub App was already installed before a setup version change, do not
remove the generated files manually. Deploy the updated Worker, then accept a
pending App permission update or remove and re-add the repository to generate a
fresh setup delivery. The bootstrap identifies older generated files, including
a byte-exact managed v3 workflow (including its older SHA pin), and opens a
migration PR that updates only those files to setup-v5. It also migrates a
managed v4/v5 caller that follows an older tag. It skips custom or future
versions for manual review, preserves learnings and repository secrets, and
never creates, reads, or rewrites a repository variable.

## Setup-v5 and tag-based reusable workflow

The current generated setup is version 5. The caller is a thin bootstrap: it
resolves the pull-request trigger from the trusted default branch, passes only
bounded event inputs to the public `malsabbagh/review-sensei` reusable workflow
at the operator-managed `v5` git tag, and reads no repository variables. It
requests `contents: read`, `pull-requests: read`, `issues: read`, and
`id-token: write`; whether anything is published is `github.writes` in
`.reviewsensei.yml`, and each write is authorized by the broker for that
operation rather than by a permission the caller grants.
The reusable workflow resolves the trusted configuration plan once and derives
the backend, endpoint, model, and credential name from it. Cloud review and
reply operations run on `ubuntu-latest`; local review and reply operations run
on `[self-hosted, linux, x64, ollama]`. Both modes support automatic review,
manual review, validated review publication, learning draft PRs, optional
artifacts, and authorized `@sensei` conversations. An authorized
mention receives 👀 while the response is being generated, and the reaction is
removed after the reply or another terminal outcome. The provider may include
`resolve: true` in its validated reply when the current exact-head context
shows that a ReviewSensei-authored inline finding is fully addressed; the
publisher then resolves only that thread through a bounded GraphQL mutation.
Issue comments and human-authored roots remain open. Add another standalone
`@sensei` mention in the same thread to continue the bounded conversation. A
successful AI resolution automatically causes one fresh same-head review pass;
approval still requires the ordinary no-blocker and all-threads-resolved gates.

The generated caller declares the three optional provider secrets by name only:
`OLLAMA_API_KEY: ${{ secrets.OLLAMA_API_KEY }}`,
`OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}`, and
`OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}`. These are literal name-only
mappings: they do not read secret values during setup, and the App never
creates, retrieves, logs, persists, or interpolates a secret value into
generated output. Add the customer-owned secret for the backend you select
(`OLLAMA_API_KEY` for `cloud-ollama`, `OPENROUTER_API_KEY` for `openrouter`,
`OPENAI_API_KEY` for `openai-compatible`) and leave the others unset. A backend
whose credential is required fails closed before any provider call when its
secret is not available to the workflow.

Setup reconciliation is PR-only and changes only the three generated paths.
`installation.created` (including reinstall),
`installation.new_permissions_accepted`, and
`installation_repositories.added` inspect absent, legacy, and v2 clients.
They reuse an existing open setup PR and produce at most one deterministic
`review-sensei/setup-v5-<base12>-<tag>` PR for the exact base and update
channel. Open setup PRs from an earlier setup version are not recognized,
because lookup matches the current v5 branch name; a delivery after a version
change therefore opens the v5 migration PR alongside an unresolved
`review-sensei/setup-v4-...` PR, and operators should close the superseded
one. Current tag-following setup-v5 is a no-op. A byte-exact
managed v3/v4 workflow or a managed v5 workflow following another valid tag is
stale and is migrated; custom,
malformed, and future versions are skipped without writes. A deployment alone
does not replay old events.

Released pre-marker and setup-v2 clients are recognized from exact historical
generated bytes. Present setup-v3 files must exactly match generated files; a
review workflow that exactly matches its own older valid public workflow SHA is
recognized as managed stale content, while retained markers do not make edited
content migratable. The current setup-v5 workflow contains exactly one valid
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
public `v5` tag to that same snapshot; update and accept
App permissions or reinstall/re-add the App;
verify setup PR reconciliation; merge desired setup PRs; enable repository
opt-ins; and only then run hosted acceptance. Rollback is a code-only
revert/redeploy that preserves delivery and broker ledger migrations; close or
revert unmerged setup PRs rather than writing the default branch directly.
