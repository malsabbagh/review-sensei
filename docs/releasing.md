# Releasing ReviewSensei

This runbook covers the first public package release and subsequent patch or
minor releases. The tag-triggered workflow builds Python artifacts once from a
version tag, validates them, then fans that bundle out to PyPI and a GitHub
release. npm uses the separate public-repository lane below. Package
publication is a maintainer operation; ordinary pull requests must not push
tags or invoke a release workflow.

## npm release lane (issue #103)

The public `malsabbagh/review-sensei` repository owns the npm package source
and the manual [`.github/workflows/publish-npm.yml`](../.github/workflows/publish-npm.yml)
release lane. Dispatch it only from public `main`; it pins the dispatch SHA,
checks the requested version against the package metadata and changelog, then
builds each native target on a matching runner. It assembles the launcher plus
five platform directories under ignored staging and runs non-dry-run `npm pack`
for every directory. The exact `.tgz` bytes are independently validated, hashed
as SHA-256 and npm SRI, attested, and uploaded as one release bundle.

The workflow's default `publish=false` is a preparation/inspection mode. With
`publish=true`, its protected `npm` job serializes a versioned release and
preflights every package: a registry entry must be absent or already match the
attested tarball's exact npm SRI. It publishes missing platform packages first,
reads back each exact version and `dist.integrity`, then publishes
`@reviewsensei/cli` last. This makes a transient partial run safely retryable
only when already-published bytes exactly match the release bundle. It also
installs the launcher in a clean Linux prefix, verifies that
`node_modules/.bin/review-sensei` resolves to the launcher rather than a
platform package, and runs its help command. The npm lane is deliberately
separate from PyPI and GitHub Release creation: do not use the tag-triggered
`release.yml` workflow for an npm-only release.

The first version of a new npm package cannot use npm Trusted Publishing until
the package already exists. For that one bootstrap run, create a short-lived
granular token restricted to the ReviewSensei npm organization scope with
package write access and 2FA bypass, then store it only as `NPM_TOKEN` in the
protected `npm` environment. Dispatch `0.1.0` with `bootstrap=true`; the workflow refuses to
use that secret for any other version, and also refuses an OIDC run while the
secret remains configured. Publish from this GitHub-hosted workflow with
`--provenance`, revoke the token, and delete the environment secret immediately
after registry verification. Then register this exact public identity for each
of the six packages and use OIDC-only publishing thereafter:

```bash
npm trust github @reviewsensei/cli --repo malsabbagh/review-sensei --file publish-npm.yml --env npm --allow-publish
npm trust github @reviewsensei/cli-darwin-arm64 --repo malsabbagh/review-sensei --file publish-npm.yml --env npm --allow-publish
npm trust github @reviewsensei/cli-darwin-x64 --repo malsabbagh/review-sensei --file publish-npm.yml --env npm --allow-publish
npm trust github @reviewsensei/cli-linux-arm64-gnu --repo malsabbagh/review-sensei --file publish-npm.yml --env npm --allow-publish
npm trust github @reviewsensei/cli-linux-x64-gnu --repo malsabbagh/review-sensei --file publish-npm.yml --env npm --allow-publish
npm trust github @reviewsensei/cli-win32-x64 --repo malsabbagh/review-sensei --file publish-npm.yml --env npm --allow-publish
```

Account 2FA and package write access are required to create these trust records.
Protect the `npm` environment with a maintainer reviewer and a `main` branch
policy before adding the bootstrap secret. A failed or partial npm release is
never repaired by moving a tag or overwriting bytes: retry the same version only
when every existing registry entry matches the attested bundle exactly; otherwise
preserve the evidence, deprecate/remove the affected recommendation when
appropriate, and publish a higher patch version after the source and provenance
checks pass.

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

## Python version and tag procedure

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

After the audited public snapshot is published, configure the Cloudflare
Worker with `PUBLIC_WORKFLOW_TAG=v4` and deploy it. Move `v4` to the reviewed
public snapshot, then trigger a fresh App installation/permission event to
reconcile repositories. The broker resolves the tag at capability exchange
time and checks the runtime workflow SHA against that resolution. The reusable
workflow installs its source fallback from the executing workflow SHA directly.

A Python/PyPI release still uses an immutable `vX.Y.Z` tag and the `Release`
workflow above. Publishing an npm package or deploying the Worker does not move
the public `v4` tag or replay historical App deliveries; each is a separate,
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
