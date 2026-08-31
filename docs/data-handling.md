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
writes only the validated result when `--output` is supplied. Repository
learnings are ordinary files owned and retained by the repository; the core
does not write proposals or create branches. Lens documents are read only for
the current review and are not persisted by the core. Applications embedding
the library are responsible for their own logs, queues, databases, and
retention policies.

Do not log prompts, diffs, API keys, private keys, or raw provider responses.

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
  stays on the configured local service.
- `https://ollama.com/api` is explicit cloud opt-in and sends source data to
  Ollama Cloud.
- Any other private service URL sends source data to that configured service
  instead.

ReviewSensei does not make claims about provider retention, model training,
subprocessors, residency, or deletion. Review the provider's current terms and
configure the endpoint deliberately before using private or regulated code.

## Portable manual GitHub Actions workflow

The example workflow in
[`examples/github-actions/review-sensei-review.yml`](../examples/github-actions/review-sensei-review.yml)
is the primary open-source path. It runs on a maintainer-controlled self-hosted
runner labelled `ollama`, checks out only the repository default branch,
requires `base_ref` to match that default branch,
installs the exact requested `review-sensei==X.Y.Z` package from PyPI first and
falls back to a public GitHub source tag only when that distribution is
unavailable. It resolves the tag, compares it with the executing workflow
SHA, and installs from that verified commit before checking the installed
version and dependencies before computing a bounded diff with
`review-sensei prepare-diff`, without
checking out or executing the head branch. Other PyPI failures remain fatal.
The runner must provide Ollama on
`http://127.0.0.1:11434` for the default local mode.

Provider data egress is explicit. The workflow defaults to the
`REVIEWSENSEI_PROVIDER_MODE=local` repository variable, which uses
`http://127.0.0.1:11434/api` with no API key. If an operator sets that variable
to `cloud`, the review step sends the diff and selected context to Ollama Cloud
using `deepseek-v4-flash:cloud` by default and requires `OLLAMA_API_KEY` from
secrets. The workflow does not accept an arbitrary provider URL input, so a
dispatch-supplied URL cannot redirect the provider credential.

The workflow uploads `review.json` and `review-sensei-version.txt` as GitHub
artifacts with a retention window. Artifact retention is controlled by the
workflow, not by ReviewSensei. Fork pull requests are not automatically
triggered by this example; enabling fork-triggered automation requires a
separate security review of secrets, untrusted head behavior, and provider data
egress.

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
is explicit and limited to the configured provider step; local mode uses the
operator's Ollama runner. Review results are reconstructed and validated,
including exact diff locations and reply bounds, before any App-authored write.
