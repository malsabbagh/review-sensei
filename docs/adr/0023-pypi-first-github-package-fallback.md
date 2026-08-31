# ADR 0023 - PyPI-first package installation with executing-SHA GitHub fallback

Status: Proposed
Date: 2026-08-29
GitHub Issue: #37
Pull Request: not configured
Owners/Reviewers: Maintainers
Approved by: not configured

## Context

The setup-v4 ReviewSensei workflow receives an exact `X.Y.Z` package version.
Its normal distribution channel is PyPI, but the first public package release
is temporarily unavailable while the maintainer restores the publishing
account. A fresh customer repository must still be able to run the workflow
without changing its generated caller or installing the reviewed repository
as the review engine.

The source fallback must not turn a missing-release condition into an unverified
dependency. It must preserve the existing trusted-base checkout, local-first
provider behavior, fork restrictions, and exact version validation. A transient
PyPI outage or dependency-resolution failure must remain visible rather than
silently switching sources.

## Decision

The public reusable workflow keeps `review_sensei_version` as the required
semantic version input. Each execution creates its isolated `RUNNER_TEMP`
virtual environment and attempts:

1. `review-sensei==X.Y.Z` from PyPI.
2. `git+https://github.com/malsabbagh/review-sensei.git@<resolved-sha>` only
   when pip reports that the ReviewSensei package/version has no matching
   distribution.

The setup installer validates the operator-managed `v4` workflow tag and writes
that tag into the caller. The reusable workflow validates that its
`job.workflow_ref` is the exact public v4 tag reference and that `job.workflow_sha`
is a valid executing commit. If PyPI is unavailable, it installs directly from
that executing commit SHA. It checks
the installed metadata against the requested version, runs `pip check`, and
logs whether PyPI or GitHub supplied the package. A branch or arbitrary source
URL is never accepted.

Generated setup-v4 callers continue to pass `REVIEWSENSEI_VERSION` and add no
package-source input. Existing callers following another tag are recognized as
stale managed files and receive a migration PR to the configured v4 tag.
Existing v3 callers retain their historical immutable workflow behavior during
the migration. Publishing the package to PyPI remains the preferred long-term
path.

## Scope

In scope:

- `.github/workflows/review-sensei-run.yml` installation behavior in automatic
  and trusted-local jobs.
- Static CI policy coverage for source ordering, tag/SHA verification, version
  verification, and fail-closed handling.
- Workflow examples, installation/data-handling/architecture documentation,
  and the setup-v4 publication contract.

Out of scope:

- Changing the generated setup-v4 caller interface or repository variables.
- Publishing to PyPI, creating a GitHub release, changing the public repository,
  deploying the Worker, or enabling customer opt-ins.
- Falling back on mutable branch refs, network failures, dependency errors,
  or arbitrary user-supplied source URLs.

## Consequences

Positive:

- Existing generated callers can run during the temporary PyPI availability
  gap.
- The normal PyPI path remains first and observable.
- GitHub fallback code is selected by the executing immutable SHA,
  reproducible, and version-checked.
- Unrelated installation failures remain actionable rather than being masked.

Tradeoffs:

- The installation Worker must validate that the public `v4` tag exists before
  creating setup files, and the broker resolves it again for each exchange.
- Source installation depends on Git being available on the runner and still
  resolves package build dependencies from the configured package index.
- Until the package is published, hosted acceptance must record that the run
  used the source fallback rather than a PyPI release artifact.

## Alternatives considered

### Install from `main` or an unverified tag

Rejected because an arbitrary branch or untrusted tag would allow the review
engine to change outside the operator-managed v4 cutoff. The managed `v4` tag
is the intentional release channel and is checked by the broker at runtime.

### Fall back on every PyPI installation error

Rejected because network outages, dependency conflicts, and resolver errors
would be hidden behind a different source and make diagnosis harder.

### Add a source URL input to every generated caller

Rejected because it expands the public setup surface without helping the
current release gap. The workflow-derived tag keeps the caller stable and
limits the source choice to the reviewed public workflow.

## Validation

- Static workflow tests assert PyPI installation precedes the GitHub URL, the
  reusable workflow ref is exactly the managed v4 tag, the executing SHA is
  valid, the
  missing-distribution patterns are specific to `review-sensei`, version
  metadata is checked, and other failures refuse fallback.
- Run the shell syntax check for both install blocks.
- Run the repository's action-pin, lint, typecheck, compile, test, coverage,
  build, and whitespace gates.
- Exercise an isolated direct Git install at the resolved public commit and verify
  `review-sensei --version` reports `0.1.0`.

## Rollout and rollback

Publish the reviewed source snapshot, configure and deploy the Worker with
`PUBLIC_WORKFLOW_TAG=v4`, then move `v4` to that snapshot. Trigger a fresh
installation/permission event so the setup-v4 caller follows the current tag.
After the PyPI release is available,
the same workflow automatically uses it and the fallback remains dormant. Roll
back by moving `v4` to the last approved public commit or redeploying the prior
Worker/workflow pair; no review-content data migration is required.

## Follow-up

- Publish and verify `review-sensei==0.1.0` through the existing Trusted
  Publishing release workflow.
- Move the public `v4` tag whenever a future source snapshot is introduced
  before its PyPI publication; no separate SHA variable is updated.
