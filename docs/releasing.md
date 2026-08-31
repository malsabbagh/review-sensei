# Releasing ReviewSensei

This runbook covers the first public package release and subsequent patch or
minor releases. The release workflow builds once from a version tag, validates
the artifacts, then fans the same bundle out to PyPI and a GitHub release.
Package publication is a maintainer operation; ordinary pull requests must not
push tags or invoke the release workflow.

## One-time maintainer setup

1. Reserve the `review-sensei` project name on [PyPI](https://pypi.org/).
2. Add a PyPI Trusted Publisher for the exact GitHub Actions identity:

   - owner: `malsabbagh`
   - repository: `review-sensei`
   - workflow: `release.yml`
   - environment: `pypi`

   The environment name and workflow filename are part of the identity. Do not
   register a wildcard workflow or a different repository.
3. Create the `pypi` GitHub environment and require the maintainer approval
   policy appropriate for the repository. The workflow requests only a
   short-lived OIDC credential (`id-token: write`) in the publish job; it does
   not read a `PYPI_TOKEN`, `password`, or other long-lived upload secret.
4. Protect changes to `.github/workflows/release.yml`, the package metadata, and
   the release environment with the repository's normal branch/review rules.
5. Configure the release owner's signing key and keep its public verification
   identity documented in the maintainer's secure records. Do not commit a
   private key or paste one into an issue, workflow, or fixture.

PyPI Trusted Publishing exchanges a GitHub OIDC identity for a short-lived,
project-scoped upload credential. It is still critical to protect the trusted
workflow and environment: a contributor who can change and run that workflow
can affect the package identity.

## Version and tag procedure

1. Choose the next immutable three-component version. The first package release
   uses the existing `0.1.0` value in `pyproject.toml`.
2. Update `[project].version` and add a dated heading to `CHANGELOG.md` in the
   same pull request. Keep the heading in the form `## X.Y.Z - YYYY-MM-DD`.
3. From a clean checkout, run the local build and consumer checks below. The
   validator must report exactly one sdist and one wheel, with package defaults
   in the wheel and `docs/`/`examples/` in the sdist.
4. Merge the reviewed change to `main`. Create an annotated, signed tag from
   the release commit and push only that tag:

   ```bash
   git tag -s v0.1.0 -m "ReviewSensei 0.1.0"
   git push origin v0.1.0
   ```

   The tag must match the package version and changelog. The workflow rejects a
   missing `v` prefix, a metadata mismatch, or a missing dated heading.
5. Monitor the `Release` workflow. The build job must pass before either the
   PyPI or GitHub release job can run. The PyPI environment approval, if
   configured, is the final human publication gate.

## Public reusable-workflow tag channel

The customer setup contract follows the separate `v4` tag in the public
`malsabbagh/review-sensei` repository. This tag is not a package-version tag:
it identifies the reviewed reusable workflow snapshot and may be moved only by
the release owner as part of an approved public cutoff. Do not move it from an
unreviewed or unpublished snapshot.

After the audited public snapshot is published, record the commit that the
public `v4` tag will resolve to. Set the Cloudflare Worker variables
`PUBLIC_WORKFLOW_TAG=v4` and `PUBLIC_WORKFLOW_SHA=<resolved commit>` together;
retain older pinned workflow SHAs in `PUBLIC_WORKFLOW_LEGACY_SHAS` until
managed client migration PRs have been merged. Deploy the Worker, move `v4` to
that same commit, and then trigger a fresh App installation/permission event to
reconcile existing repositories. The reusable workflow installs its source
fallback from the executing workflow SHA directly.

A package release still uses an immutable `vX.Y.Z` tag and the `Release`
workflow above. Publishing a package or deploying the Worker does not move the
public `v4` tag or replay historical App deliveries; each is a separate,
operator-owned cutoff step.

## Local build and verification

Run these commands from the repository root in a disposable Python 3.11+
environment. The second environment is deliberately outside the checkout so
the smoke test cannot succeed by importing `src/`.

```bash
python -m pip install --upgrade build
python scripts/check_release_version.py --tag v0.1.0
rm -rf dist release
python -m build --sdist --wheel --outdir dist
python scripts/validate_release.py dist --version 0.1.0

python -m venv /tmp/review-sensei-wheel-check
/tmp/review-sensei-wheel-check/bin/python -m pip install --no-deps dist/*.whl
/tmp/review-sensei-wheel-check/bin/python -m pip check
/tmp/review-sensei-wheel-check/bin/review-sensei --help
```

The release workflow also writes `SHA256SUMS` and an SPDX JSON SBOM. Verify a
downloaded bundle before installing it:

```bash
sha256sum -c SHA256SUMS
gh attestation verify review_sensei-0.1.0-py3-none-any.whl \
  --repo malsabbagh/review-sensei
```

The GitHub attestation command is an operator-side consumer check performed
after downloading a release bundle; it is not run by the `github-release`
workflow job. It verifies the provenance recorded for the workflow-built
subject. PyPI's package attestations are visible from the project/file page and
are produced by the same Trusted Publishing job.

## Rollback and yank

Package versions and release tags are immutable. Never overwrite a published
file or reuse a version for a different build.

- If a release is incorrect but not compromised, stop downstream adoption,
  yank the affected file(s) or version using the PyPI project controls, and
  create a corrected higher version. Keep the original checksums and
  attestations for the audit trail.
- If the GitHub release needs correction, edit its notes/assets through the
  release owner workflow; do not move or force-push the tag. Point consumers at
  the corrected version and document the yank.
- If a build fails before publication, do not retry with a changed commit under
  the same tag. Fix the source/workflow through a pull request and use a new
  tag or version according to the release owner's policy.

## Compromised-release response

1. Pause the `pypi` environment and disable/review the Trusted Publisher before
   investigating. Do not paste OIDC tokens, PyPI credentials, private source,
   or signing keys into logs or tickets.
2. Determine whether the workflow, tag/signing identity, runner, or package
   contents were compromised. Preserve the release bundle, checksums, SBOM,
   workflow run, and attestations as evidence.
3. Yank every affected PyPI file/version and mark the GitHub release as
   withdrawn or otherwise clearly affected. Do not delete evidence or reuse
   the version.
4. Revoke or rotate the affected signing identity and review repository,
   environment, maintainer, and Trusted Publisher access. Because Trusted
   Publishing has no long-lived upload token, there is no repository token to
   rotate; still review any short-lived credential exposure before expiry.
5. Publish a fixed higher version from a reviewed commit, verify its checksums
   and attestations from a clean consumer environment, and communicate the
   affected versions and mitigations through the private security channel in
   [`SECURITY.md`](../SECURITY.md).

Record the incident timeline and final decision in the maintainer's private
security process. Public issue or release notes must contain only sanitized
details.
