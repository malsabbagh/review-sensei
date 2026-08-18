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

The manual workflow example in
[`examples/github-actions/review-sensei-review.yml`](../examples/github-actions/review-sensei-review.yml)
installs an exact released `review-sensei==X.Y.Z` package into a separate
`RUNNER_TEMP` virtual environment. It records `review-sensei --version` before
reviewing, uses the installed `review-sensei prepare-diff` command to validate
refs and compute a bounded diff, and uploads `review.json` plus
`review-sensei-version.txt`.

The workflow defaults to the `REVIEWSENSEI_PROVIDER_MODE` repository variable
(`local`), uses a local Ollama API, and does not require a provider secret.
Because GitHub-hosted runners do not ship
with Ollama, this example requires a maintainer-controlled self-hosted Linux
runner labelled `ollama` with the selected model already available. The
workflow checks out only the repository default branch and rejects a
`base_ref` that does not match it; it never installs or executes the head
branch. If you set `REVIEWSENSEI_PROVIDER_MODE` to `cloud`, the review step
sends the diff and selected context to Ollama Cloud using
`deepseek-v4-flash:cloud` by default and requires `OLLAMA_API_KEY`; review provider
terms and your data-handling requirements before doing so. The workflow does
not accept an arbitrary provider URL input.

The workflow is manually dispatched and intentionally not enabled for fork
pull requests. A fork-triggered automatic run would need a separate security
review of secrets, untrusted head behavior, and provider data egress.

The GitHub App bootstrap creates the three `REVIEWSENSEI_*` repository
variables with the defaults above and does not create a placeholder secret.
After the setup PR is merged, install Ollama and pull `qwen3.5:4b` on the
self-hosted runner for local mode. For cloud mode, add `OLLAMA_API_KEY` under
Repository Settings → Secrets and variables → Actions, then change only
`REVIEWSENSEI_PROVIDER_MODE` to `cloud`. The generated **Remove ReviewSensei
setup** workflow can be dispatched to open a cleanup PR; it leaves learnings,
variables, and secrets for explicit operator review.

If the GitHub App was already installed before a setup version change, do not
remove the generated files manually. Deploy the updated Worker, then accept a
pending App permission update or remove and re-add the repository to generate a
fresh setup delivery. The bootstrap identifies the older generated files and
opens a migration PR that updates only those files. It skips custom or future
versions for manual review and preserves learnings, existing variables, and
secrets.
