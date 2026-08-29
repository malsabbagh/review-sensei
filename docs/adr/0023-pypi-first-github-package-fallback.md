# ADR 0023 - PyPI-first package installation with pinned GitHub fallback

Status: Proposed
Date: 2026-08-29
GitHub Issue: #37
Pull Request: not configured
Owners/Reviewers: Maintainers
Approved by: not configured

## Context

The setup-v3 ReviewSensei workflow receives an exact `X.Y.Z` package version.
Its normal distribution channel is PyPI, but the first public package release
is temporarily unavailable while the maintainer restores the publishing
account. A fresh customer repository must still be able to run the workflow
without changing its generated caller or installing the reviewed repository
as the review engine.

The source fallback must not turn a missing-release condition into an unpinned
dependency. It must preserve the existing trusted-base checkout, local-first
provider behavior, fork restrictions, and exact version validation. A transient
PyPI outage or dependency-resolution failure must remain visible rather than
silently switching sources.

## Decision

The public reusable workflow keeps `review_sensei_version` as the required
semantic version input. Each execution creates its isolated `RUNNER_TEMP`
virtual environment and attempts:

1. `review-sensei==X.Y.Z` from PyPI.
2. `git+https://github.com/malsabbagh/review-sensei.git@<40-hex-sha>` only when
   pip reports that the ReviewSensei package/version has no matching
   distribution.

The current fallback source is pinned to
`ff53bbadf6bce78fe0421ec2f07844feec976095`, a public source commit that
contains `review-sensei==0.1.0`. The workflow validates that the fallback ref is
a full lowercase commit SHA, checks the installed metadata against the
requested version, runs `pip check`, and logs whether PyPI or GitHub supplied
the package. The fallback ref is updated with the default package version and
public source snapshot; mutable branch names are never accepted.

Generated setup-v3 callers do not gain a new input. Existing callers continue
to pass `REVIEWSENSEI_VERSION`, so the setup migration and Worker's exact
public-workflow-SHA checks remain unchanged. Publishing the package to PyPI
remains the preferred long-term path.

## Scope

In scope:

- `.github/workflows/review-sensei-run.yml` installation behavior in automatic
  and trusted-local jobs.
- Static CI policy coverage for source ordering, full-SHA pinning, version
  verification, and fail-closed handling.
- Workflow examples, installation/data-handling/architecture documentation,
  and the setup-v3 publication contract.

Out of scope:

- Changing the generated setup-v3 caller interface or repository variables.
- Publishing to PyPI, creating a GitHub release, changing the public repository,
  deploying the Worker, or enabling customer opt-ins.
- Falling back on mutable `main`/tag refs, network failures, dependency errors,
  or arbitrary user-supplied source URLs.

## Consequences

Positive:

- Existing generated callers can run during the temporary PyPI availability
  gap.
- The normal PyPI path remains first and observable.
- GitHub fallback code is reproducible and version-checked.
- Unrelated installation failures remain actionable rather than being masked.

Tradeoffs:

- The public source SHA must be maintained when the default package version or
  public snapshot changes.
- Source installation depends on Git being available on the runner and still
  resolves package build dependencies from the configured package index.
- Until the package is published, hosted acceptance must record that the run
  used the source fallback rather than a PyPI release artifact.

## Alternatives considered

### Install from `main` unpinned

Rejected because a moving branch would allow the review engine to change
between runs and would weaken the immutable workflow contract.

### Fall back on every PyPI installation error

Rejected because network outages, dependency conflicts, and resolver errors
would be hidden behind a different source and make diagnosis harder.

### Add a source URL input to every generated caller

Rejected because it expands the public setup surface without helping the
current release gap. A single workflow-maintained full-SHA fallback keeps the
caller stable and limits the source choice to the reviewed public workflow.

## Validation

- Static workflow tests assert PyPI installation precedes the GitHub URL, the
  fallback is a full 40-character SHA, the missing-distribution patterns are
  specific to `review-sensei`, version metadata is checked, and other failures
  refuse fallback.
- Run the shell syntax check for both install blocks.
- Run the repository's action-pin, lint, typecheck, compile, test, coverage,
  build, and whitespace gates.
- Exercise an isolated direct Git install at the pinned public commit and verify
  `review-sensei --version` reports `0.1.0`.

## Rollout and rollback

Publish the reviewed source snapshot, update the configured public workflow SHA,
and let new setup-v3 callers consume the immutable workflow revision. Existing
callers remain on their prior public workflow until migrated. After the PyPI
release is available, the same workflow automatically uses it and the fallback
remains dormant. Roll back by reverting the workflow/docs commit and, if
necessary, restoring the prior public workflow SHA; no customer data migration
is required.

## Follow-up

- Publish and verify `review-sensei==0.1.0` through the existing Trusted
  Publishing release workflow.
- Update the pinned fallback SHA whenever a future default package version is
  introduced before its PyPI publication.
