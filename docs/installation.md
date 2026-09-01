# Installation

## From source

```bash
git clone https://github.com/malsabbagh/review-sensei.git
cd review-sensei
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

## Configuration

Copy the documented variables from [`.env.example`](../.env.example) into the
environment used to run ReviewSensei. The CLI reads:

| Variable | Default | Purpose |
| --- | --- | --- |
| `REVIEWSENSEI_PROVIDER` | `ollama` | Provider registry key |
| `REVIEWSENSEI_PROVIDER_MODE` | `local` | `local` keeps requests on loopback; `cloud` selects Ollama Cloud |
| `REVIEWSENSEI_LOCAL_MODEL` | `qwen3.5:4b` | Local Ollama model |
| `REVIEWSENSEI_CLOUD_MODEL` | `deepseek-v4-flash:cloud` | Ollama Cloud model |
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
is `deepseek-v4-flash:cloud`. `OLLAMA_BASE_URL` and `OLLAMA_MODEL` remain
available as explicit overrides.

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
other PyPI failures remain fatal. It then checks out only trusted base content,
validates refs, and computes a bounded diff without installing or executing the
head branch.

The App creates nine repository variables: provider mode, local model, cloud
model, package version, automatic review, GitHub writes, learning PRs, mention
replies, and artifact upload. The five feature switches default to `false`.
The App does not create a placeholder secret or overwrite an existing variable.

To enable automatic same-repository review, set
`REVIEWSENSEI_AUTO_REVIEW=true` and `REVIEWSENSEI_GITHUB_WRITES=true`.
`REVIEWSENSEI_PROVIDER_MODE=cloud` runs on `ubuntu-latest` and requires the
customer-owned `OLLAMA_API_KEY` under Repository Settings → Secrets and
variables → Actions. `REVIEWSENSEI_PROVIDER_MODE=local` runs the same review
path on the labelled self-hosted runner. Both reject fork heads before provider
or broker access and can publish one exact-head App review with valid inline
comments and a validated summary. Enable `REVIEWSENSEI_LEARNING_PRS` separately for deterministic draft
learning PRs.

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
removed after the reply or another terminal outcome. Add another standalone
`@sensei` mention in the same thread to continue the bounded conversation.

The generated caller may contain the literal name-only mapping
`OLLAMA_API_KEY: ${{ secrets.OLLAMA_API_KEY }}`. This does not read the value
during setup and the App never creates, retrieves, logs, persists, or
interpolates a secret value into generated output. Add the customer-owned
secret yourself only when explicitly enabling cloud mode.

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
