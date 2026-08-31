# ADR 0011 - Portable immutable GitHub Actions workflow

Status: Proposed
Date: 2026-08-12
GitHub Issue: #15
Pull Request: not configured
Owners/Reviewers: Maintainers
Approved by: not configured

## Context

The primary open-source path for Code Sensei is running the package in a
user-owned GitHub Actions workflow. The previous example workflow installed
the checked-out repository with `pip install .`. In a consumer repository that
is not Code Sensei, that command installs the reviewed project instead of Code
Sensei, so the manual workflow is not portable.

The example also passed `inputs.base_ref` and `inputs.head_ref` directly into
`git fetch` without option/ref validation, had no fork head-repository input,
checked out a head branch that might contain untrusted code when users followed
the example, and defaulted to Ollama Cloud, which sends private diffs to an
external provider by default.

## Decision Drivers

- Users must be able to run Code Sensei in repositories that do not contain
  Code Sensei source scripts.
- Exact released package versions must be installable into a separate runtime
  environment so the reviewed repository's own code is never installed or
  executed as the review engine.
- When an exact package version is not yet present on PyPI, the workflow must
  have a reproducible source-install path without weakening the trusted-base
  or provider boundaries.
- Untrusted refs and repository slugs must be validated before any git command
  and must never be interpolated into shell commands.
- Diff preparation must fetch explicit refs, resolve them to commit OIDs,
  verify a merge base, and enforce the public diff limits without checking out
  or executing head code.
- Local Ollama must be the default provider configuration. Cloud egress must
  be explicit opt-in only.

## Decision

Add a portable `code_sensei.workflow` helper and expose it through the
`code-sensei prepare-diff` CLI subcommand. Rewrite the manual example workflow
so it checks out only the base ref with an immutable `actions/checkout` pin,
sets up Python with an immutable `actions/setup-python` pin, validates an exact
`code_sensei_version` with a self-contained stdlib Python bootstrap step, and
installs `code-sensei==X.Y.Z` into a separate `RUNNER_TEMP` virtual
environment.

The workflow then runs `code-sensei --version` and writes the output to
`code-sensei-version.txt`, runs `code-sensei prepare-diff` with env-bound
quoted refs/repository inputs and bounded diff flags, and runs the review with
env-bound quoted provider inputs. Checkout is pinned to
`github.event.repository.default_branch`, and validation rejects a
dispatch-supplied `base_ref` that does not equal that trusted branch. Provider
mode defaults to local Ollama at `http://127.0.0.1:11434/api` with no API key
and the `qwen3.5:4b` model;
the example requires a maintainer-controlled self-hosted Linux runner labelled
`ollama` with the selected model provisioned. Cloud mode is explicit opt-in
through the `REVIEWSENSEI_PROVIDER_MODE=cloud` repository variable, uses the
fixed Ollama Cloud endpoint with `deepseek-v4-flash:cloud` by default, and
requires `OLLAMA_API_KEY`. The workflow does not accept an arbitrary provider
URL input, so a dispatch-supplied URL cannot redirect the provider credential.

## Scope

In scope:

- A new provider-neutral `src/code_sensei/workflow.py` helper.
- `code-sensei --version` and `code-sensei prepare-diff` CLI contracts.
- A rewritten immutable manual workflow example.
- Documentation, public-contract updates, and ADR 0011.
- Tests for ref/repository/version validation, bounded diff preparation,
  fork fetch behavior, shallow behavior, merge-base failures, no-authorization
  local provider requests, and workflow static policy.

Out of scope:

- Automatic `pull_request` fork review triggers.
- GitHub App-identity comments, reviews, or installation-token transport.
- Publishing a package release, pushing a tag, deploying, merging, or closing
  issue #15.
- Changing provider-neutral review business logic or adding provider SDK
  dependencies to the core.

## Consequences

Positive:

- A consumer repository can run the reviewed workflow without having Code
  Sensei source checked out.
- The reviewed repository's head code is never checked out or executed.
- Diff preparation validates untrusted inputs before shelling out to git and
  enforces the existing `ReviewLimits` profile.
- Local-first defaults avoid exporting private diffs by default.
- The workflow records the exact installed package version for auditability.

Negative or tradeoffs:

- PyPI remains the preferred distribution and the fallback is only a temporary
  release-gap path; the pinned public source commit must be maintained whenever
  the default package version changes.
- Fork reviews are supported manually by `head_repository` only after an
  operator supplies the fork slug; there is no automatic fork PR trigger.
- The bootstrap version validation is a short Python heredoc inside the
  workflow. It is self-contained stdlib code, but it still needs static policy
  coverage to prevent drift.

## Alternatives Considered

### Keep installing the checked-out repository

- Summary: Continue `python -m pip install .` after checking out the base
  ref.
- Why not chosen: It installs the reviewed repository's own package in a
  consumer repository instead of Code Sensei, and it does not guarantee an
  exact reviewed release.

### Put workflow logic in repository-local scripts

- Summary: Validate refs and compute diffs with scripts checked out from the
  base branch.
- Why not chosen: A consumer repository will not contain those scripts. The
  bootstrap step must be self-contained in the workflow or in the installed
  Code Sensei package.

### Default to Ollama Cloud

- Summary: Keep the default provider configuration pointed at Ollama Cloud.
- Why not chosen: That silently exports repository diffs and selected context
  to an external service. Local Ollama should be the default and cloud egress
  should require explicit operator configuration.

## Implementation Notes

- `code_sensei.workflow.prepare_diff` validates refs, optional fork slugs,
  optional versions, and diff limits before invoking git. It uses list-based
  argv, resolves refs to full commit OIDs, fetches explicit refs into
  `refs/code-sensei/*`, verifies `git merge-base`, and streams a bounded
  triple-dot diff to a temporary patch file before atomic publication.
- The optional `head_remote` parameter is a test seam only. The workflow does
  not expose it. When omitted, fork fetches default to
  `https://github.com/<head_repository>.git`.
- The workflow's version bootstrap step reads `CODE_SENSEI_VERSION`, requires
  `X.Y.Z` or `vX.Y.Z`, normalizes it, and writes a step output. It does not
  execute a repository-local script.
- The setup-v3 reusable workflow tries `review-sensei==X.Y.Z` from PyPI first.
  It falls back only when pip reports that the ReviewSensei package/version is
  unavailable, then installs a public GitHub source commit pinned to a full
  40-character SHA and verifies the installed version and dependencies. Network,
  dependency, and other PyPI failures remain fatal.
- The review step sets `OLLAMA_API_KEY` from the operator's secret only in
  cloud mode. Cloud egress remains opt-in through
  `REVIEWSENSEI_PROVIDER_MODE=cloud`.
- The local default is operational only on the required self-hosted `ollama`
  runner; a preflight checks the loopback service and selected model before the
  provider call.
- The workflow uploads `review.json` and `code-sensei-version.txt` with an
  immutable `actions/upload-artifact` SHA and explicit retention.

## Validation And Rollout

- Validation: Run the deterministic repository gates, the added workflow
  fixture tests, the fork fetch/diff fixture, and a clean-wheel consumer smoke
  that installs the built wheel outside the checkout and runs
  `code-sensei --version` plus `prepare-diff` against a fixture repository.
- Rollout: Merge the reviewed change through the normal branch/protection
  process. The manual workflow becomes the documented primary path; no hosted
  deployment is required.
- Rollback: Revert the workflow, helper, CLI, tests, docs, and ADR through a
  normal code-only rollback. Users who installed an exact package can switch
  back to the previous workflow version or install a different exact version.

## Follow-Up

- Publish the first Code Sensei package release so operators can supply an
  installable exact version to the workflow.
- If automatic fork pull-request review is needed later, design a separate
  workflow with explicit secrets, head-ref identity, and untrusted-head
  behavior review.
- If GitHub App-identity comments/reviews are needed later, keep them on a
  separate, explicitly configured App identity path.

## Amendment - issue #64 setup-v3

The portable workflow decision now has a generated setup-v3 caller that
invokes the public reusable workflow at an exact configured commit SHA. The
caller keeps automatic cloud pull-request execution on GitHub-hosted compute,
limits local Ollama to manual or trusted-event execution on the labeled
self-hosted runner, and grants only read permissions plus `id-token: write`.
All publication, learning, reply, and artifact switches default to false.
The customer-owned `OLLAMA_API_KEY` may be passed to the reusable workflow by
name only; the setup App never handles its value. This amendment supersedes
the old example-only boundary for generated setup clients while retaining the
trusted-base checkout and exact version validation. The reusable workflow's
PyPI-first, full-SHA-pinned GitHub source fallback is defined in ADR 0023.

## Amendment - issue #64 setup-v4 tag channel

Setup-v4 uses the operator-managed public `v4` git tag only as an install-time
update channel. The Worker resolves that tag, verifies it matches its configured
SHA, and writes the full commit SHA into both reusable-workflow references in
the generated caller. The reusable workflow and Cloudflare broker require that
immutable SHA; the broker may temporarily admit explicitly retained older SHA
pairs during migration. Setup-v3 callers remain supported only through the
explicit migration allowlist described in ADR 0021. Package release tags remain
immutable `vX.Y.Z` tags; the separate `v4` workflow tag is moved only as an
approved public cutoff.

## Links

- Related issue: #15
- Related PR: not configured
- Supersedes: none
- Superseded by: none
