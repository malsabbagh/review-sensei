# @reviewsensei/cli

ReviewSensei is an open-source, provider-neutral AI code review engine. Give it
a unified Git diff to get a validated summary and findings tied to changed
files and lines. It selects review lenses by changed path, can attach bounded
context from a trusted checkout, calls your configured model, and validates its
structured response. Review coverage and incomplete results remain visible.

This npm package runs the same Python review engine as the `review-sensei`
Python package through a bundled native executable. A separate Python
installation is unnecessary. It works for local reviews and scripted CI use;
the repository's reusable GitHub workflow is a separate integration.
This guide describes the engine bundled with this package version; CLI
`--help` and the linked engine references are the shared option and contract
reference, rather than an independent npm API.

## Install and run

Use Node.js 22 or newer. Supported targets are macOS arm64/x64, Linux arm64/x64
with glibc 2.36 or newer, and Windows x64. Target selection follows Node's
operating system and architecture. Linux musl and other architectures are
unsupported. Keep npm optional dependencies enabled: the launcher needs its
exact-version platform package and reports an error if it is missing.

```console
npm install --save-dev @reviewsensei/cli@0.6.13
npx review-sensei --version
npx review-sensei --help
```

Or run an exact version without adding a project dependency:

```console
npx --yes @reviewsensei/cli@0.6.13 --help
```

From your repository directory, create a patch and review it with a reachable
local Ollama server and an installed model. `prepare-diff` requires an external
`git` executable; npm itself installs the platform package.

```console
npx review-sensei prepare-diff --repository . --base-ref main --head-ref HEAD --output pr.patch
npx review-sensei --diff pr.patch --provider local-ollama --model qwen3.5:8b
```

The launcher has no install hook or runtime downloader. Model requests are made
by the engine when you run a review using the selected provider.

## Configuration and forwarded arguments

The launcher forwards every CLI argument unchanged, without a shell, to the
bundled Python engine. It inherits the current directory, environment, and
terminal streams. Relative paths, including paths containing spaces, therefore
resolve from your working directory. Quote each such argument in your shell:

```console
npx review-sensei --config "config/review settings.yml" --diff "patches/my change.patch" --format json --output "reports/review result.json"
```

The engine reads `.reviewsensei.yml` in the current directory by default, or the
file supplied with `--config`. There is no separate npm configuration schema.
Without a configuration file, packaged defaults select a local Ollama backend.
For example:

```yaml
schema: 1
inference:
  backend: local-ollama
  model: qwen3.5:8b
advanced:
  resources:
    timeout_seconds: 300
    max_provider_calls: 8
```

Inference precedence is CLI `--provider`/`--model`, then environment
`REVIEWSENSEI_PROVIDER`/`REVIEWSENSEI_MODEL`, then YAML `inference.backend`/
`inference.model`, then packaged defaults. Environment overrides therefore
take precedence over YAML, including in CI. Unset those variables when you
want YAML to select the backend and model, or inspect the effective settings
with `npx review-sensei config show --explain`. This is the engine's explicit
operator-override behavior; npm does not add another precedence layer. The
backends for live reviews are
`local-ollama`, `cloud-ollama`, `openrouter`, and `openai-compatible`.

| Backend | Authentication |
| --- | --- |
| `local-ollama` | No credential by default; requires a reachable local Ollama server |
| `cloud-ollama` | `OLLAMA_API_KEY` |
| `openrouter` | `OPENROUTER_API_KEY` |
| `openai-compatible` | `OPENAI_API_KEY` for its default OpenAI endpoint |

Set credentials in your shell or CI secret store, never in the YAML or patch.
An inherited credential does not select a backend. After setting
`OLLAMA_API_KEY`, for example, a cloud review is:

```console
npx review-sensei --diff pr.patch --provider cloud-ollama --model deepseek-v4.1-flash:cloud
```

These backend names and `--api-key-env`/`--allow-custom-endpoint` are accepted
by the engine's review parser; `fixture` is a separate repository test seam.
`--api-key-env` selects an explicitly named credential variable. Endpoint
overrides use `--base-url` and, for custom OpenAI-compatible endpoints,
`--allow-custom-endpoint`; see the configuration reference for validation and
credential-routing rules. `review-sensei config --help` and
`review-sensei doctor --help` describe configuration inspection and diagnostics.

## Review lenses, context, and output

Packaged stages and categories provide the default lenses. Custom JSON category
and stage directories use `--categories-dir` together with `--stages-dir`;
`--categories-dir` requires `--stages-dir`. See the stage/category reference for
path selectors, focus items, and declared document sources. These flags use
the Python engine's existing formats.

Use `--instructions` for explicit review guidance, `--context-root` for a trusted
target-branch checkout, and `--learning-root` for approved repository learnings.
Optional document selection and verbatim excerpts report loss and provenance;
`--context-selection-output selection.json` writes a metadata-only inventory.
Selection is distinct from complete review coverage.

```console
npx review-sensei --diff pr.patch --title "My change" --context-root ../trusted-main --format markdown --output review.md
```

Text is the default output. `--format markdown` or `--format json` selects a
rendered report, and `--output` writes it to a file. Warnings and diagnostics go
to stderr. The launcher returns the engine's exit code: `0` means a completed
review with no required fixes, `1` means required fixes remain, and `2` means
the review could not complete. Partial coverage does not establish full
approval. `--exit-semantics operational` instead returns `0` for non-failure
engine outcomes, including partial, skipped, and already-published runs, and
`1` for failures or required human action; launcher startup errors also return
`1`. Neither operational code establishes full review coverage or approval.
Exit code `1` alone cannot distinguish an engine failure from a launcher
failure. Inspect stderr and any structured engine outcome requested with
`--outcome outcome.json`
for status and diagnostics. Early failures may not write an outcome, and a
launcher failure cannot produce an engine outcome file. Use a fresh output path
so an old file cannot be mistaken for the current result.

A local review prints or writes its report; GitHub publication is a separate
workflow/command with its own authorization. Selecting a remote provider sends
the supplied review data to that provider. Review the data-handling guidance
before using private source or context.

- [Provider configuration](https://github.com/malsabbagh/review-sensei/blob/main/README.md#providers)
- [Stages and categories](https://github.com/malsabbagh/review-sensei/blob/main/README.md#structured-review-stages)
- [Installation](https://github.com/malsabbagh/review-sensei/blob/main/docs/installation.md)
- [Data handling](https://github.com/malsabbagh/review-sensei/blob/main/docs/data-handling.md)
- [Source and issues](https://github.com/malsabbagh/review-sensei)
