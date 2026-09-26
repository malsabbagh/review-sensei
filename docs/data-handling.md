# Data handling

## Data sent to a provider

The selected provider receives:

- The unified pull-request diff.
- Active, path-scoped repository learnings selected from the target branch, when
  supplied by the caller.
- Explicitly configured, lens-specific text documents selected from the trusted
  target-branch checkout, including their repository-relative paths and SHA-256
  digests.
- Optional repository name, pull-request number, title, and reviewer
  instructions.
- The selected model name and provider-specific request options.

The prompt tells the model to treat source code, comments, learnings, and lens
documents as untrusted data. The engine does not execute code from the diff or
from context documents.

## Storage and logging

The library does not persist review prompts or raw provider responses. The CLI
writes the selected result document to stdout, or to `--output` when supplied
(readable terminal text by default; `--format markdown` or `--format json` to
select another form), and the structured `RunOutcome` when `--outcome` or
GitHub Actions summary/output files are present. An identity-bound
`RecoveryArtifact` is written only when `--recovery-artifact` is explicit; it
expires automatically, contains only a publisher-validated result, and is not
uploaded unless the existing `upload_artifacts` opt-in is enabled. Repository
learnings are ordinary files owned and retained by the repository; publication
recovery never writes them.
Lens documents are read only for the current review and are not persisted by
the core. The optional in-memory `ReviewContextCache` stores only bounded
metadata such as coverage mode and generation; it is repository/PR-scoped,
integrity-checked by base/engine/model/profile/stage/context/learning
digests, deletable via `invalidate`/`clear`, and never a hosted service.
Applications embedding the library are responsible for their own logs,
queues, databases, and retention policies.

Do not log prompts, diffs, API keys, private keys, or raw provider responses.
Run outcomes record only closed diagnostic tokens, identities, and counters.

## GitHub App authentication

If you enable optional GitHub App-identity comments or reviews, the
`review_sensei.hosting.github` auth adapter handles App JWTs and installation
access tokens. The private key is loaded through an injectable
`PrivateKeySource` and is never read from committed configuration. JWTs,
installation tokens, authorization headers, and API bodies are redacted from
errors. The adapter does not persist keys or tokens; cache entries are
in-memory and scoped by installation, repository, and permission. See
[`docs/github-app-auth.md`](github-app-auth.md) for registration, key rotation,
and emergency revocation.

## Ollama configurations

- `http://127.0.0.1:11434/api` is the default local Ollama configuration. Data
  stays on the configured local service, and the default local review selects
  it without requiring an Ollama key.
- A backend that requires no credential never receives one implicitly: with the
  local Ollama backend an exported `OLLAMA_API_KEY` or another provider key is
  not attached to requests unless `--api-key-env` names it.
- `https://ollama.com/api` is explicit cloud opt-in and sends source data to
  Ollama Cloud.
- Any other private service URL sends source data to that configured service
  instead.

## OpenRouter configurations

- `https://openrouter.ai/api/v1` is the only allowlisted OpenRouter endpoint.
  Review data and the `OPENROUTER_API_KEY` bearer credential leave the machine
  for remote inference even when the CLI runs locally.
- Named profiles (`openrouter-sonnet`, `openrouter-gpt`) embed an immutable
  `OpenRouterRoutingPolicy` (upstream provider slug, no fallbacks, deny data
  collection, ZDR). Unprofiled `--provider openrouter` uses
  `OPENROUTER_UPSTREAM_PROVIDER` (default `morph`) for the routing policy.
- `doctor` and `plan` report execution location (local CLI process) versus
  inference location (remote for OpenRouter) and credential presence only.
- `inference.backend: cloud-ollama` (or the `REVIEWSENSEI_PROVIDER=cloud-ollama`
  override) means Ollama Cloud only. Hosted OpenRouter is selected with
  `inference.backend: openrouter` and `inference.model` (or the
  `REVIEWSENSEI_PROVIDER`/`REVIEWSENSEI_MODEL` overrides); that backend choice
  is the operator egress acknowledgement.
  The reusable workflow rejects `allow_unqualified_profile=true`, does not
  forward `--allow-unqualified-profile`, and only runs allowlisted models. The
  generated caller forwards `OPENROUTER_API_KEY` by name and never reads its
  value during setup.

ReviewSensei does not make claims about provider retention, model training,
subprocessors, residency, or deletion. Review the provider's current terms and
configure the endpoint deliberately before using private or regulated code.

## Portable GitHub Actions workflow

The generated caller in
[`examples/github-actions/review-sensei-review.yml`](../examples/github-actions/review-sensei-review.yml)
is the primary open-source path. It is a read-only bootstrap: it checks out
only the repository default branch with no persisted credentials, holds only
`contents: read`, `pull-requests: read`, `issues: read`, and `id-token: write`,
and calls the public reusable workflow at the configured tag. The reusable
workflow checks out only the repository
default branch, requires `base_ref` to match that default branch, installs the
exact requested `review-sensei==X.Y.Z` package from PyPI first, and falls back
to the executing workflow commit SHA only when that exact distribution is
unavailable; other PyPI failures remain fatal. It then computes a bounded diff
with `review-sensei prepare-diff`, without checking out or executing the head
branch. Local backends run on the operator's self-hosted runner (labelled
`ollama` in the example) and must have Ollama on
`http://127.0.0.1:11434`.

Provider data egress is explicit. The default local backend uses
`http://127.0.0.1:11434/api` with no API key. If an operator selects a cloud
backend in the trusted configuration (`inference.backend: cloud-ollama` or
`openrouter`, or the `REVIEWSENSEI_PROVIDER` override), review and conversation
steps send their bounded diff, selected context, and authorized thread context
to that backend's fixed allowlisted endpoint — Ollama Cloud by default with
`deepseek-v4.1-flash:cloud`, requiring `OLLAMA_API_KEY` from secrets. The
workflow does not accept an arbitrary provider URL input, so a
dispatch-supplied URL cannot redirect the provider credential. Installed
workflows do not pass `--profile`, and only the credential the selected backend
names is ever attached to provider requests: the generated caller forwards the
three declared secret names, and the reusable workflow fails closed if the
trusted plan names a credential outside them.

The workflow uploads `review.json`, `outcome.json`, and the recovery artifact
as the `review-sensei-diagnostics` GitHub artifact only when the trusted
configuration enables diagnostics uploads, with a seven-day retention window.
Artifact retention is controlled by the workflow, not by ReviewSensei. Fork
pull request heads are rejected by the reusable workflow's preflight before any
provider call; enabling fork-triggered automation would require a separate
security review of secrets, untrusted head behavior, and provider data egress.

## OpenAI-compatible adapter

The `openai-compatible` adapter and `fast-triage` profile send the same
provider-facing review payload to `https://api.openai.com/v1` using a bearer
token from the caller-supplied `OPENAI_API_KEY` value. The adapter rejects
redirects, requires HTTPS, and does not fall back to Ollama. Custom hosts
require an explicit `allow_custom_endpoint` opt-in at the Python boundary and
are rejected for named profiles. Review the current OpenAI retention, training,
residency, and deletion terms before selecting this profile.

## Bounded transfer and failure contract

`ReviewLimits` is the provider-neutral, downward-only profile shared by request,
diff, transport, and result validation. Defaults cap the diff at 1,048,576
bytes/50,000 lines/500 files/5,000 hunks; repository metadata at 512 bytes,
title at 4,096 bytes, instructions at 65,536 bytes, model ids at 256 bytes;
metadata at 64 pairs (128-byte keys and 4,096-byte values); learning entries at
100; active categories and lens contexts at 64; prompts at 4,194,304 bytes;
provider response envelopes at 1,048,576 bytes; summaries at 32,768 bytes;
comments at 250 with 16,384-byte bodies; proposals at 100 (16,384 bytes each,
262,144 bytes for the compact UTF-8 serialization of the complete proposal
array, including brackets and separators); results at 2,097,152 bytes; and line numbers at
2,147,483,647. Consumers may pass a lower profile through `ReviewRequest` but
cannot raise a hard ceiling.

Repository paths are strict NFC UTF-8 and `/`-separated. The core rejects
empty/dot/parent segments, absolute/drive/UNC forms, backslashes,
surrounding whitespace, control/format/surrogate code points, and non-NFC text
without normalizing it. Git C-quoted paths are decoded as strict bytes first.
Exact comment duplicates `(path, line, body)` use stable first-wins ordering;
different bodies remain distinct. Literal glob characters in real Git paths are
preserved without being interpreted. The Ollama adapter reads at most
`max_response_bytes + 1` bytes before strict decoding and envelope validation.
Expected failures never include prompts, diffs, provider bodies, credentials, or
secret markers.

Migration is code-free: replace non-canonical paths and context-source
`path: "."` with explicit canonical files/directories and split oversized
inputs. Rollback is code-only: revert the limits/validation integration and
restore the prior parser, constructors, and provider read boundary. No stored
review or learning data needs migration or rollback.

## Evaluation corpus and reports

The versioned evaluation corpus under `evaluation/v1/` is synthetic and
CC0-1.0. The loader requires a complete regular-file inventory, canonical
paths, bounded UTF-8 files, declared provenance, and rejects private-key,
token, bearer, non-reserved email, symlink, traversal, and size violations.
Reports contain only digests, statuses, aggregate metrics, and non-secret
provider metadata; they never contain diffs, prompts, responses, credentials,
environment values, or monetary cost claims.

Fixture evaluation performs no network access and does not read the Ollama API
key environment variable. Live evaluation requires `--allow-live-model`; any
non-loopback endpoint additionally requires `--allow-data-egress` before a
provider is constructed.

## Future integrations

Every provider adapter must document its endpoint, authentication, retention,
training, residency, and deletion behavior. If ReviewSensei later supports
GitHub App-identity comments or reviews, that adapter must explain installation
scope, repository permissions, uninstall behavior, and the exact GitHub data it
forwards to a model provider. Any future learning PR path must keep proposals
unmerged until maintainer review and load future learnings from the target
branch rather than the PR head.

## Setup-v4 and broker boundary

The issue-64 setup-v4 path keeps review execution in the customer repository.
The Worker receives only bounded webhook metadata and a short-lived Actions
OIDC assertion for capability issuance. Its Durable Object stores hashed `jti`
and scope identities plus bounded replay/rate counters. Pre-auth admission uses
only a hashed assertion digest and a sanitized source-address scope; public JWKS
keys are cached briefly in Worker memory. The signed assertion is claimed
before repository or installation API reads. The Worker never stores source,
diffs, prompts, results, replies, provider credentials, assertions, or
installation-token values. Worker responses are `no-store`, the token route
does not support CORS, and errors are sanitized.

The generated workflow may reference the existing customer-owned secret by
name only (`secrets.OLLAMA_API_KEY`) when invoking the tagged public reusable
workflow. The App and setup bootstrap never create, retrieve, reveal, log,
persist, or interpolate its value into generated files. Cloud provider egress
is explicit and limited to the configured provider steps; local mode uses the
operator's Ollama runner. The App-authored 👀 reaction is transient GitHub
metadata: it is added only after mention authorization and removed after the
reply or another terminal outcome. Review results and replies are reconstructed
and validated,
including exact diff locations and reply bounds, before any App-authored write.
