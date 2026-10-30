# ADR 0078 - Split the hosted GitHub runtime into review-sensei-github

Status: Proposed
Date: 2026-10-12
Owners/Reviewers: Maintainers
Approved by: not applicable
Related issue: not created; user-authorized split of the hosted runtime.
Related PR: not created yet.

This record is ADR 0078. It does not restate the OIDC broker (ADR 0015) or the
session ledger (ADR 0047). Those contracts stay. Only the package that speaks
them moves.

## Context

`review-sensei` is the local review engine: diff, providers, configuration, and
a local session ledger. The same distribution also contains the GitHub Actions
runtime: OIDC exchange, the token broker client, session grants, GitHub
publication, and maintainer commands. A hosted-only defect therefore forces a
full engine release, and a local install ships broker and GitHub-write code it
never runs.

The Cloudflare Worker at `github.reviewsensei.dev` stays a separate TypeScript
deployment. This ADR splits the runner-side client, not the Worker.

## Decision

Ship a second distribution, `review-sensei-github`, from this repository.

- Import package: `review_sensei_github`.
- Console script: `review-sensei-github`.
- Source: `packages/python/review-sensei-github`.
- Dependency: `review-sensei` pinned to the exact engine version the hosted
  package was tested with, plus `cryptography` for GitHub App JWT signing and
  OIDC verification. The engine distribution does not depend on
  `cryptography`.
- Distribution: PyPI only. There is no npm package. The tool runs on Actions
  runners, not on developer machines.
- Versions are independent. A hosted-only fix releases `review-sensei-github`
  and moves `v5`. An engine fix releases `review-sensei` and bumps the hosted
  pin.

`review_sensei_github` may import `review_sensei`. `review_sensei` must not
import `review_sensei_github` or `review_sensei.hosting`. An AST test enforces
that. The engine wheel and the npm standalone binary do not contain the broker
client, OIDC verification, or GitHub publication.

### What moves

The modules that were under `review_sensei.hosting.github`, plus
`openrouter_workflow`. The hosted CLI commands are `review`,
`publish`, `reply`, `command`, `host-plan`, `resolve-hosted-openrouter`,
`evaluate-observed`, and `doctor`.

`review` runs the engine review entry point with hosted hooks: a GitHub
session ledger and the Actions operation-entry provider runner. `publish` is
the former `github review` subcommand.

### What stays in the engine

The review engine, providers, configuration (including the `github:` section,
which the hosted package reads through the engine API), the `SessionLedger`
protocol, `LocalSessionLedger`, diff preparation, and local CLI commands.
`OperationHandle` and `operation_handle_witness` live in the engine because
attempt accounting has to recognize a witness without importing the host.
`broker_diagnostics` and its schema stay in the engine because the outcome
renderer reads that catalog. The hosted client imports the catalog from the
engine, and the Worker keeps its existing schema path.

`hosted.py` planning stays in the engine as library code. Only the
`host-plan` command moves.

The command implementations live in the normally imported `command_bodies.py`;
`commands.py` explicitly re-exports the existing command names. Finite engine CLI
helpers and late-bound callables are qualified through `review_sensei.cli` at
invocation time, preserving existing patches without an engine-to-hosted import.
Both command modules participate in ordinary Ruff and mypy checking. The existing
private helper dependency remains a maintainability boundary, not a new public
core API or a second runtime.

### Review seam

`review_sensei.cli.main` accepts hosted hooks supplied by the caller. The
engine CLI passes none and does not advertise `--github-session-ledger`,
`--oidc-token`, or the hosted `--repository-id`. The hosted `review` command
passes hooks that resolve the broker ledger and run the operation-entry
provider. Broker failures are reported by those hooks. The engine does not
import the broker diagnostic catalog.

### Compatibility

- The reusable workflow path stays
  `.github/workflows/review-sensei-run.yml`. Callers, the broker, and setup
  generation all bind that path.
- `src/review_sensei/hosting/github/trigger.py` remains a self-contained
  script at that path. Generated callers execute it with `PYTHONPATH=src`
  before the package install. It is a byte copy of
  `review_sensei_github.trigger` and is not imported by the engine.
- For one release, `review-sensei github`, `host-plan`, and
  `resolve-hosted-openrouter` print a moved-command notice and run
  `review-sensei-github` in a subprocess. They do not import the hosted
  package.
- The broker HTTP protocol, session-grant format, and ledger comment format
  do not change.

### Release

`release-github.yml` publishes `review-sensei-github` from a signed
`github-vX.Y.Z` tag through the `pypi-github` environment. The reusable
workflow installs `review-sensei==<pin>` from PyPI when that exact version
exists, and uses the workflow commit for the engine only when PyPI has no
such version. It then installs `review-sensei-github==<version declared at
the workflow commit>` from PyPI, or, when that version is missing, from the
same commit's `packages/python/review-sensei-github` subdirectory with
`--no-deps`, then installs that package's requirements other than
`review-sensei`. The fallback does not replace the engine already installed,
and it still installs `cryptography` from the hosted package. Any
other PyPI failure refuses the fallback. The published `0.6.22` engine has
no `hosted_hooks` parameter, so the first hosted release has to be prepared
with a newer engine version. `prepare-diff` stays on the engine binary.

Do not move `v5` until the engine pin and the hosted version in that commit
are the versions you intend runners to install.

## Consequences

Local `pip install review-sensei` no longer installs GitHub publication,
the broker client, or `cryptography`. Hosted runs install one extra
distribution, and that distribution installs `cryptography` itself. After the
first engine that provides `hosted_hooks` is published, a hosted-only fix
does not require another engine release. The workflow commit must name both
versions, and the hosted pin must match the engine version in the same commit.

## Scope

In scope: the package split, the CLI seam, the workflow install, the Worker
schema import path, release metadata, and the import boundary test.

Out of scope: changing the Worker protocol, renaming
`review-sensei-run.yml`, moving `v5` in this change, and registering the PyPI
project or Trusted Publisher. Those remain operator steps.


## Amendment: ordinary typed command imports (2026-10-30, proposed)

Remove command-source reading, `exec`/`compile`, the missing-global dictionary and
unchecked callable casts. `command_bodies.py` imports standard/core types normally;
`commands.py` re-exports the same ten entrypoints. Hosted package version resolution
moves unchanged into the implementation module to avoid a circular import. Runtime
GitHub constructors stay local imports so the established export-patching seam works.

The finite CLI dependency is explicit: stage binding, provider registry, host outcome
emission, configuration/inference resolution, bounded context loading and exit codes;
plus the existing custom-endpoint/work-recovery parser helpers, configured budget,
Ollama defaults, positive-number parser, offline errors and live-profile gates.
`ReviewService` construction remains a late engine-CLI lookup. This creates no new
public core helper API. Internal TypedDicts describe existing provider-run and
checkpoint keywords; the two hook forwarding signatures propagate those annotations
without changing execution, admission, recovery or accounting behavior.

Only the loader-specific F821/F401 and mypy exclusions are removed. No blanket
replacement ignores, Any/callable casts, service locator or helper registry is added.
Validation retains CLI grammar/defaults/prog/version/exits, retirement refusals,
late patches, broker diagnostics, lifecycle recovery and the engine-only package
boundary. A self-contained installed-wheel contract proves normal imports,
entrypoint identity, version behavior and invocation-time helper patches. Rollback
would restore the loader and its old exclusions together; no persisted protocol or
schema migration is involved.


## Amendment: native core bundle qualification (2026-10-30, proposed)

The standalone oracle installs only the core package on every platform. The old
Intel Homebrew static-OpenSSL prerequisite and cryptography source-build flags
belong to the pre-split dependency graph and are removed. The historical
`check_standalone_crypto.py` command remains, with engine-boundary receipt semantics:
it requires the actual bundle/executable, scans collected files, and inspects the
embedded CArchive/PYZ module inventory using pinned PyInstaller tooling. Hosted
`review_sensei_github` and `cryptography` packages fail qualification; Python's
ordinary TLS libraries and the trigger compatibility shim remain allowed. Missing
or malformed inventory fails closed.

Intel retains Python 3.11.9, actual native build/parity, candidate archiving and
installed npm consumer checks. All five native targets remain required. Synthetic
offline tests prove installer and checker behavior; actual Intel and platform CI
must qualify the resulting executable. Rollback restores the installer/checker
and their native check wiring together; no public runtime or schema changes occur.
