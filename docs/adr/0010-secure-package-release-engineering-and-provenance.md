# ADR 0010 - Secure package release engineering and provenance

Status: Proposed
Date: 2026-08-11
GitHub Issue: #4
Pull Request: #28
Owners/Reviewers: Maintainers
Approved by: not applicable

## Context

Code Sensei needs a repeatable first-release path that keeps package metadata, artifact contents, trusted publishing, and provenance checks auditable.

## Decision Drivers

- Package metadata must have one authoritative source and produce deterministic
  sdist and wheel contents.
- PyPI publishing must not depend on a long-lived credential stored in GitHub.
- A release must be inspectable and verifiable after the build through checksums,
  an SBOM, and signed provenance attestations.
- A release workflow must be narrowly triggerable, least-privilege, and safe to
  change only through normal pull-request review.
- Operators need a documented way to bump, verify, roll back, or respond to a
  compromised release without reusing an immutable version.

## Decision

Use pyproject.toml as the package metadata source of truth and a tag-driven, OIDC-authenticated GitHub Actions release workflow that validates artifacts before publishing, emits checksums/SBOM/attestations, and creates release assets.

## Scope

In scope:

- `pyproject.toml` project metadata, SPDX licensing, project URLs, and package
  data configuration.
- A metadata-free `setup.py` compatibility shim and an explicit source manifest
  for docs and examples.
- Offline artifact validation, clean-wheel smoke validation, and release
  workflow regression checks.
- A tag-driven GitHub Actions workflow that builds before publishing, uses
  PyPI Trusted Publishing/OIDC, creates checksums and an SBOM, emits GitHub
  artifact attestations, and attaches artifacts to a GitHub release.
- Release operation, verification, rollback/yank, and compromised-release
  response documentation.

Out of scope:

- Reserving or configuring the `review-sensei` project on PyPI.
- Pushing a release tag, publishing a package, creating a GitHub release, or
  yanking an existing version as part of this change.
- General CI quality expansion, dependency update automation, or CodeQL work
  tracked by issue #3.
- Hosted review service, provider behavior, or GitHub App runtime code.

## Consequences

Positive:

- Build metadata and package contents can be reproduced from a versioned tag.
- OIDC gives the release workflow short-lived PyPI credentials without a
  repository secret, while the dedicated environment can add human approval.
- Checksums, SBOM output, and attestations make release artifacts auditable and
  support post-publication verification.
- The workflow fails before publishing when the tag, metadata, changelog, or
  artifact contents do not agree.

Negative or tradeoffs:

- Maintainers must register the exact workflow/environment as a PyPI Trusted
  Publisher and protect changes to that workflow.
- GitHub artifact attestations and PyPI attestations depend on the external
  platforms being available; local validation remains necessary.
- Release assets are immutable by version, so a bad release is yanked and
  replaced with a new version rather than overwritten.

## Alternatives Considered

### Store a long-lived PyPI upload token in GitHub secrets

- Summary: Authenticate the publish step with a repository or environment
  secret.
- Why not chosen: A stolen token remains usable until manual revocation and
  violates the issue's no-long-lived-upload-token requirement.

### Publish directly from a developer workstation

- Summary: Build and upload artifacts manually without a release workflow.
- Why not chosen: It is not reproducible, cannot provide workflow-bound
  provenance, and makes least-privilege review and audit difficult.

### Use floating action tags

- Summary: Reference `@v4`, `@v1`, or another mutable action tag.
- Why not chosen: A tag can change after review; full commit-SHA pins keep the
  reviewed action implementation immutable until a deliberate update.

## Implementation Notes

- `pyproject.toml` is the only metadata source; `setup.py` calls `setuptools.setup`
  without project fields for legacy tooling.
- `MANIFEST.in` includes `docs/` and `examples/` in the sdist while package-data
  settings include the packaged default JSON assets in the wheel.
- `scripts/validate_release.py` checks archive safety, metadata, required files,
  and version consistency without importing from the checkout.
- `.github/workflows/release.yml` runs only for `vX.Y.Z` tags, validates the tag
  against `pyproject.toml` and `CHANGELOG.md`, builds before any publish job,
  and grants `id-token: write` only to the jobs that need OIDC.
- The PyPI job uses the PyPA publish action with Trusted Publishing and no
  `password`/upload-token input. A dedicated `pypi` environment is the place
  for any maintainer approval required by repository policy.

## Validation And Rollout

- Validation: Run the repository unit tests and compile check, build an sdist and
  wheel, validate both archives, install the wheel in a fresh Python 3.11+
  virtual environment outside the checkout, run `review-sensei --help` and
  `pip check`, and inspect the workflow for SHA pins, permissions, and the
  no-token invariant.
- Rollout: Merge the reviewed change, reserve `review-sensei` on PyPI, register
  the exact `release.yml` + `pypi` environment as a Trusted Publisher, protect
  the environment, then create and push a signed `v0.1.0` tag. The workflow
  creates the GitHub release and publishes only after all build checks pass.
- Rollback: Do not overwrite or reuse a published version. Yank the affected
  PyPI file/version, mark the GitHub release appropriately, preserve its
  checksums and attestations for investigation, disable/review the Trusted
  Publisher if needed, and ship a corrected higher version. For pre-publication
  failures, delete only the uncreated draft/tag according to maintainer policy
  and fix the workflow through a normal pull request.

## Follow-Up

- Reserve and configure the PyPI project and `pypi` environment.
- Confirm branch protection requires CI and release-workflow review before a
  release tag can be pushed.
- Publish the first package release and verify its PyPI/GitHub attestations from
  a clean consumer environment.

## Links

- Related issue: #4
- Related PR: #28
- Supersedes: none
- Superseded by: none
