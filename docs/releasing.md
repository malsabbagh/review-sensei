# Releasing ReviewSensei

This runbook covers the first public package release and subsequent patch or
minor releases. The tag-triggered workflow builds Python and npm artifacts
from a version tag, validates and attests them, then publishes to PyPI, npm,
and a GitHub release. The manual npm workflow is available for independent
npm releases and recovery. Package publication is a maintainer operation;
ordinary pull requests must not push
tags or invoke a release workflow.

## npm release paths (issue #103)

The `Release` workflow's `publish-npm` job is the normal npm path for a
`vX.Y.Z` tag. It publishes the attested platform tarballs before the launcher
from the same tagged source. Its `npm` environment must allow a **tag** rule
matching `v*.*.*`; a `main` **branch** rule does not admit a tag, even when
that tag points to a commit on `main`. Keep the maintainer review gate. Register
`.github/workflows/release.yml` as an npm Trusted Publisher with `npm publish`
permission for all six packages, in addition to the manual workflow identity
below. The environment and npm trust settings are external repository/registry
configuration; merging this runbook does not change them.

If the tag's npm job fails while the build and assembly jobs succeeded, re-run
only that job (`gh run rerun --job <npm-job-id> --repo malsabbagh/review-sensei`).
GitHub retains the original tag ref and commit for a job re-run, and the job
downloads the original run's validated npm bundle. Do not move or recreate the
tag to recover a publication failure. If any package reached npm, the preflight
must find the exact attested integrity before publishing missing packages.

The public `malsabbagh/review-sensei` repository owns the npm package source
and the manual [`.github/workflows/publish-npm.yml`](../.github/workflows/publish-npm.yml)
release path. Dispatch it only from public `main`; it pins the dispatch SHA,
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
when already-published bytes exactly match the release bundle. When every
package for the requested version is already published, the version is complete:
preflight records each package as `already-published`, publishes nothing, exits
successfully, and reports that it did not verify provenance. A rebuilt bundle
cannot reproduce bytes that are already on the registry, so a completed version
is a no-op rather than a byte mismatch. A partially published version is
different: if any existing registry entry holds different bytes while another
package for the same version is still missing, the publish is refused because it
would mix bytes from two builds.

If a publish job fails after some packages reach the registry, do not dispatch a
fresh rebuild for the same version. Either re-run the failed workflow job from
GitHub Actions, or dispatch `publish-npm.yml` again with `resume_bundle_run_id`
set to the failed run ID so the workflow reuses that run's attested
`review-sensei-npm-release-bundle` artifact instead of rebuilding new tarballs.
Resume requires a bundle that includes `bundle-metadata.json` (produced by
workflows after this metadata recording landed). Older bundles without that file
are not eligible for resume. The resumed run must be a failed or cancelled
manual `publish-npm` dispatch from the default branch of this repository, and
its attested source commit and bundle metadata version must match the requested
version. A failed tag-triggered `Release` run is not eligible for manual resume;
re-run its failed npm job instead. The manual run title must be exactly
`publish-npm X.Y.Z`. Attestation verification binds each tarball and
`bundle-metadata.json` to that run's source
commit ref and digest via `--source-ref` and `--source-digest`; the metadata
file is listed in `SHA256SUMS` and attested with the tarballs.
`bundle-metadata.json` also provides a secondary check. The workflow's `verify-bundle` helper checks bundle structure and
metadata only; attestation verification runs separately in the resume path.
Before resuming, confirm the referenced run's head SHA is the commit
you intended to release and matches the resuming dispatch's default-branch
`GITHUB_SHA`; a newer default-branch commit cannot resume an older run's bundle.
The resume path trusts that run's attested source and
does not publish bytes built from a different dispatch commit. The resumed
dispatch reuses the prior run's attested tarballs and `source_sha`; it does not
publish bytes built from the resuming dispatch commit. Do not combine resume
with a fresh rebuild for the same version. Non-resume publishes still accept
legacy bundles that lack `bundle-metadata.json`; resume requires metadata and
attestation. Packages already verified on the
registry are skipped and only missing packages are published.
It also installs the launcher in a clean Linux prefix, verifies that
`node_modules/.bin/review-sensei` resolves to the launcher rather than a
platform package, and runs its help command. Use this manual path for an
npm-only release; a version-tag release also publishes PyPI and GitHub assets.

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
Also register the tag-triggered workflow for each package before relying on
tag publication:

```bash
npm trust github @reviewsensei/cli --repo malsabbagh/review-sensei --file release.yml --env npm --allow-publish
npm trust github @reviewsensei/cli-darwin-arm64 --repo malsabbagh/review-sensei --file release.yml --env npm --allow-publish
npm trust github @reviewsensei/cli-darwin-x64 --repo malsabbagh/review-sensei --file release.yml --env npm --allow-publish
npm trust github @reviewsensei/cli-linux-arm64-gnu --repo malsabbagh/review-sensei --file release.yml --env npm --allow-publish
npm trust github @reviewsensei/cli-linux-x64-gnu --repo malsabbagh/review-sensei --file release.yml --env npm --allow-publish
npm trust github @reviewsensei/cli-win32-x64 --repo malsabbagh/review-sensei --file release.yml --env npm --allow-publish
```

Protect the `npm` environment with a maintainer reviewer, a `main` branch
rule for manual dispatches, and a `v*.*.*` tag rule for version-tag releases.
Protect who can create or change release tags; the environment reviewer is the
final publication gate. A failed or partial npm release is never repaired by
moving a tag or overwriting bytes: retry the same version only
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

## Prepare main before signing and building

The normal complete Python/npm release uses `prepare-release.yml` before the
signed tag. Merge and qualify this reviewed workflow implementation first.
**Do not merge PR #226**: retain it as the 0.6.17 comparison reference. Preparation
changes the main metadata through the dedicated App; tagging an unchanged
0.6.16 source as 0.6.17 still fails the release contract.

1. Complete [the dedicated App setup and real readiness check](release-docs-bot.md).
   The existing main-only environment and isolated PR-rule exception are required.
   A broad exception on a mixed PR/core ruleset is rejected. Repository rules
   setup still requires the owner's exclusive editing window and explicit setup
   authority; neither workflow changes rules or App permissions.
2. Review and merge all intended source under the ordinary source review policy.
   Authored `CHANGELOG.md` Unreleased notes are optional. If that section is
   empty, Marshal generates notes from the exact first-parent main commits since
   the annotated previous-version tag. Choose an unpublished `X.Y.Z` version
   and explicit `YYYY-MM-DD` release date. The owner dispatches from fresh main:

   ```bash
   gh workflow run prepare-release.yml --ref main \
     -f version=0.6.17 -f release_date=2026-10-08
   ```

3. Preparation requires full terminal-success **push/main CI on the dispatch SHA**
   before writes. It executes trusted dispatch code and integrates a clean
   disposable worktree at that exact main source. The existing short-lived App
   token is repository-only Contents write/Actions read/Metadata read and is
   revoked when its twenty-minute generation job finishes. The later CI-wait job
   has no App key or write token.
4. The deterministic renderer updates `[project].version`, all six npm versions
   and exact optional dependencies, paired marked current installation pins in
   packaged READMEs/source docs, site version/tag/date and generated provider/release
   pages. It moves authored Unreleased notes under the dated release heading when
   present; otherwise it lists exact merged-source commit subjects with full SHA
   links, the selected source range and the previous annotated tag object.
   Subjects are escaped as text, without a provider call or executable Markdown.
   The previous tag must agree with its GitHub-verified signed identity and
   previous version, and occur on the selected first-parent main history. Missing
   or ambiguous ancestry, an unchanged source or history beyond the bounded
   256-commit / 256 KiB allowance stops preparation. Maintainers can supply authored
   notes for an exceptional range. Routine empty Unreleased sections require no
   notes PR. Generated notes are recomputed from source during commit validation;
   their digest remains bound to the existing preparation receipt. No receipt
   schema or App permission changes are needed. See [ADR 0071](adr/0071-generated-release-notes.md).
   Historical examples outside markers and the `@v5` channel stay unchanged.
   `PAYLOAD_PATHS` in `scripts/prepare_release.py` lists the complete allowlist.
   Runtime source, configs, tests, workflows, credentials and protection policy
   are outside it. The generated `.publication/release-preparation.json` records
   the reviewed parent SHA, immutable version/date intent, notes digest and hashes
   of every payload file. Index/regular-file/filter checks prove the actual commit
   contains those bytes; symlinks, ignored payload, extra staged paths and storage
   transformations stop the write. The single DCO bot commit is pushed normally,
   never with force. A concurrent main update requires a fresh dispatch.
   Integrate the reviewed helper/workflow upgrade once; subsequent owner dispatches
   generate notes, version metadata and source docs in this direct App commit,
   without a separate release-notes or generated-docs PR.
5. Ordinary main CI runs because the push uses the App rather than GITHUB_TOKEN.
   The preparation workflow waits for **all fifteen exact-head CI jobs**, including
   native ABI/parity, npm/clean-wheel contracts, Worker and CodeQL. It checks the
   latest run and newest execution of each job (failed-job retries may retain
   earlier successful jobs). Missing, skipped, failed, cancelled, racing or timed
   out checks withhold signing. It re-verifies the parent/file receipt, main SHA,
   unused tag and all seven registry versions before the handoff. A successful
   preparation does not publish packages or make new source docs live on Pages.
6. Complete the existing readers-first, compatibility/canary and protected
   publication qualification below. To automate the signed tag, complete the
   [dedicated release-key user-run setup](release-signing.md) and dispatch with
   `-f sign_tag=true`. A separate main-only job waits for generated CI, rechecks
   the receipt, source, all unused versions, registered key/email, narrow App
   identity and Mo's publication reviewer gates, then creates and verifies a
   dedicated-key signed annotated tag on the exact qualified SHA. It pushes
   only the new immutable tag and requires GitHub's verified-object readback.
   No personal SSH key is placed in Actions and no tag/ruleset bypass is added.
   `sign_tag=false` (default) retains the local owner signing handoff; use only
   the SHA emitted by the successful preparation, and stop if main/CI changed
   or the tag exists.

   The Release source gate verifies the signed remote tag/object, event/local
   SHA, main ancestry, receipt-bound single-parent allowlisted commit, all
   versions/docs and terminal exact-head main CI before any docs/Python/native
   build. It also rechecks Mo's sole-reviewer npm/PyPI environments. Python/npm
   build from that tagged commit and await **separate publication approvals**.
   Approval is authorization, not a cryptographic signature. GitHub release
   assets wait for both package publications; final Pages/snapshot deployment
   remains bound to successful Release completion. Later snapshot commits do
   not become a replacement package source.

For a transient CI issue, rerun the failed CI job, then retry the failed
`qualify-prepared` job. A new dispatch from unchanged prepared main with the same
version/date is an idempotent no-op followed by exact-head CI qualification. If
main advanced, select and qualify the new reviewed source; refreshing an unpublished
same-version preparation retains the existing date and reviewed release notes,
incorporates any new reviewed Unreleased notes, and generates a new receipt/commit
and full CI. An all-jobs rerun of an old dispatch stops on stale main; it must not
silently incorporate another source. Any existing tag or PyPI/npm version stops
preparation, including partial publications. Recover a tagged release by retrying
its existing jobs/artifacts; never regenerate that version or move the tag.

For offline inspection, use `prepare_release.py render` only in a disposable
checkout with explicit version, date and base SHA. It performs no network or
publication. Validate the generated candidate, then compare version/entry points,
all six npm manifests, provider facts/current installation pins and legacy fixture
behavior with PR #226. Complete bytes may differ because main includes later
reviewed source, marker metadata and immutable tag/run provenance.

## Public reusable-workflow tag channel

The customer setup contract follows the separate `v5` tag in the public
`malsabbagh/review-sensei` repository. This tag is not a package-version tag:
it identifies the reviewed reusable workflow snapshot and may be moved only by
the release owner as part of an approved public cutoff. Do not move it from an
unreviewed or unpublished snapshot.

Keep these names distinct:

- **The generated caller format** is versioned by `SETUP_VERSION` (currently
  `5`), the setup marker version written into generated files. That number is
  not a tag: it currently matches the workflow tag below only because the
  cutover promoted both, and a later tag move must not renumber the caller
  format.
- **`v5`** is the movable public workflow channel. Generated callers use
  `@v5`. Configure the Worker with `PUBLIC_WORKFLOW_TAG=v5`.
- **`0.6.14` / `v0.6.14`** is the immutable Python and npm package cutoff.
  `release.yml` publishes PyPI from `v*.*.*` tags. Do not create a package tag
  named `v5.0.0`; that would collide with the `v*.*.*` release trigger and
  confuse the workflow channel with a library version.

The broker still accepts both the `v4` and `v5` workflow tags during
migration. New generated callers pin the `v5` workflow tag.

After the audited public snapshot is published, configure the Cloudflare
Worker with `PUBLIC_WORKFLOW_TAG=v5` and deploy it. Move `v5` to the reviewed
public snapshot, then trigger a fresh App installation/permission event to
reconcile repositories. The broker resolves the tag at capability exchange
time and checks the runtime workflow SHA against that resolution. The reusable
workflow installs its source fallback from the executing workflow SHA directly.

A Python/PyPI release still uses an immutable `vX.Y.Z` tag and the `Release`
workflow above. Publishing an npm package or deploying the Worker does not move
the public `v5` tag or replay historical App deliveries; each is a separate,
operator-owned cutoff step.

## 0.6.16 recovery and human-reassessment rollout

This cutoff includes authenticated abandoned-analysis recovery (#215), the
canonical `@reviewsensei` handle with the retained `@sensei` alias (#216), CI
dependency updates (#217–#221), and complete human inventories, bounded pending
evidence, safe rejection diagnostics and retryable exact-head approval (#222).
It also fixes the release PR's reproduced retry failure: completed analysis with
unfinished publication requires the original identity-bound result and trusted
contexts, rather than claiming `already_published` and opening a missing result
file. Opted-in diagnostics survive publication failure, and review-create errors
report a bounded HTTP status or transport category. The original PR223 failure's
exact network/API cause was not retained; this change does not assert a fix for
that unknown upstream error. With `artifacts: none`, the discarded result cannot
be recreated from the ledger, so a same-context retry stops for recovery. Do not
reset its budget or apply abandoned-analysis recovery to it.
Preparing or merging this release PR does not update installed consumers. Keep
`v0.6.14`, `v0.6.15`, all published bytes and the separately controlled `v5`
channel intact while preparing the cutoff.

The release owner must approve and record each rollout operation separately:

1. Merge the reviewed release PR after terminal CI on its exact head. From the
   clean, reviewed merge commit, run
   `python scripts/check_release_version.py --tag v0.6.16`, create the signed
   immutable `v0.6.16` tag, and push only that tag. Verify the workflow's staged
   Python files and all six npm tarballs against source-bound attestations and
   clean installed consumers. Retain the static Intel OpenSSL audit, frozen RSA
   sign/verify result and strict native CLI parity. Keep the protected PyPI/npm
   gates pending until their checks and the compatibility binding below pass.
2. Prepare the reviewed broker and App permission rollout from this cutoff,
   preserving its installation identity, secret bindings and deployment
   configuration. The broker must support the lazy `review_actions` capability, scoped
   only to Actions read, OIDC reservation ownership and both mention handles.
   Record the prior Worker identity and the verified candidate bundle. The new
   capability requires **Actions: read** in the GitHub App's repository
   permissions and acceptance by installation owners. This is an App
   installation permission; do not add an Actions permission requirement to
   consumer reusable-workflow callers.
3. Bind the reviewed workflow SHA, verified Python/npm artifacts, current schema
   identities, candidate Worker and canary evidence in the compatibility manifest
   below, preserving its build → verify → canary bind → publish/promote sequence.
   With the release owner's explicit approval bundle, approve package publication,
   deploy the compatible broker, and update/accept the App permission. Verify
   published registry bytes, the deployed Worker identity and effective Actions
   read on each recovery installation. If npm is partially published, rerun only
   its failed job with the original attested bundle; do not rebuild or move the
   immutable tag. After all compatible lanes are verified and channel promotion
   is authorized, move the signed `v5` channel to the reviewed cutoff and record
   its old/new tag objects and commit SHAs. Moving `v5` while retaining the
   `0.6.15` package version cannot deliver this engine fix: the workflow prefers
   its exact PyPI version. Package publication alone does not promote `v5`.
4. Upgrade opted-in consumer caller filters/resolvers through reviewed,
   workflow-only PRs using [the mention-handle rollout](mention-handle-rollout.md).
   Preserve runner selections, operator edits, permissions and concurrency.
   Older callers can continue using `@sensei`; promoting `v5` alone does not let
   their filters route `@reviewsensei`. No App rename or setup-format renumbering
   is required.
5. Verify a fresh end-to-end review, mention reply and human reassessment on the
   promoted workflow and installed CLI `0.6.16`, retaining exact source/head,
   installed version, provider/model, source receipt and final approval outcome.
   A historical review declaring human findings without the complete inventory
   must receive a newly authorized full review of its current head first; do
   not reconstruct the inventory from review prose or old model output. Verify
   that colliding findings stay distinct, insufficient evidence acknowledges
   without inference or eligibility mutation, and a finalization retry preserves
   all existing gates and emits no duplicate approval.
6. For a specifically authorized abandoned session such as PR54, issue
   authenticated `@sensei review continue` (or the canonical handle after its
   caller upgrade) only after the recovery runtime, broker and accepted App
   permission are live. Retain the completed-owner or constrained legacy-origin
   proof, current-head and ledger-generation checks, one failed-attempt charge
   and the subsequent fresh review/publication evidence. Age alone never proves
   abandonment; active, ambiguous or unavailable evidence remains blocked.

Existing records without `reservation_owner` retain their digest and remain
readable. Legacy PR-triggered holds use bounded exact-head/PR-associated origin
proof, so they do not need owner metadata retrofitted. They still require the
new Actions-read broker capability and accepted App permission. New owner-bearing
holds fail closed in older engines; recover or complete them with an owner-aware
engine before rolling the runtime back. Publication checkpoints, completed
counters, expiry and review budgets are preserved. Existing valid human
inventories keep their identities and need no migration; missing inventories
remain pending until a fresh full review establishes complete evidence.

No remote state is migrated by this release PR. Publication, deployment,
permission acceptance, channel promotion, consumer caller changes and live
continuation/review are deferred to the release owner's explicit approval bundle.

## 0.6.15 Intel native ABI repair

The `v0.6.14` cutoff published Python successfully, but its Intel macOS npm
bundle failed strict native/direct error-output parity. The retained
[diagnostic run](https://github.com/malsabbagh/review-sensei/actions/runs/37404551425)
verified the unchanged source and showed `_rust.abi3.so` expecting
`_SSL_get0_group_name` from a bundled `libssl.3.dylib` that lacks that symbol.
Direct Python returned the documented invalid-input exit 2; the frozen CLI
crashed with exit 1. Preserve that immutable tag and all already-published bytes.

Cryptography removed Intel macOS wheels in version 49. The native installer
now builds its declared cryptography dependency with `OPENSSL_STATIC=1` and
architecture-matched Homebrew static archives, bypassing cached/shared wheels.
Linux's shared build helper pulls the digest-pinned Docker Official Images from
`public.ecr.aws/docker/library`, avoiding Docker Hub's shared-runner pull quota.
The pinned manifest contents and Debian 12 glibc baseline are unchanged; both
amd64 and arm64 images are admitted before building, and the consumer remains
offline. Registry failures get at most three pull attempts with jittered backoff
on the same platform/digest, then fail the build with no mutable-tag fallback.
This is the [upstream macOS static-build procedure](https://cryptography.io/en/latest/installation/#building-cryptography-on-macos);
it does not downgrade the dependency. Both `Release` and manual npm builds use
`scripts/install_standalone_oracle.py` for the same installation contract.

Before release, required PR CI reproduces the actual Intel / Python 3.11.9
framework environment. `scripts/check_standalone_crypto.py` rejects shared
OpenSSL dependencies in the frozen Rust binding, then loads that exact frozen
extension and signs/verifies a temporary in-memory RSA message. The original
standalone smoke still requires exact exits and output. A lazy import alone
would hide the error-path trigger without fixing native hosted authentication.

Prepare and review the `0.6.15` package cut. After human merge and separate
release-owner authorization, sign `v0.6.15` from that reviewed commit and publish
through the normal workflow, verifying the Python files and all six npm
packages against their attestations. Do not rewrite or overwrite `0.6.14`.
The current `v5` promotion remains at the published `0.6.14` commit until a
separately authorized channel move. This repair changes no Worker runtime,
capability, secret, or ledger contract and performs no Worker deployment.

## 0.6.14 rollout verification

This cutoff includes bounded stale-baseline recovery (#209), canonical backend
resolution for mention replies (#210), safe broker rejection diagnostics
(#211), evidence-backed human-reply reassessment (ADR 0061), and the validated
`hosted_runner` reusable-workflow input. Preparing or merging the release PR
does not update installed consumers.
The maintainer rollout must complete these separately authorized steps:

1. Build and verify the immutable 0.6.14 artifact lanes, including the Worker,
   and bind the exact compatibility manifest and canary evidence as described
   below before publication. Publish the `v0.6.14` cutoff through `Release` and
   verify the exact PyPI distribution and all six npm packages and attestations.
   Moving `v5` while the package metadata still says `0.6.13` is insufficient:
   the reusable workflow prefers the matching PyPI package and can install the
   old CLI even when the workflow source contains #210.
2. Build and deploy the reviewed Worker with #211 for detailed broker reasons.
   The new CLI retains the generic safe fallback against older Workers. No
   ledger schema migration is required for these fixes; preserve the current
   deployment configuration, secret bindings and operator rollback record.
3. Verify that the compatibility manifest and canary binding match the completed
   published/deployed lanes, then promote `v5` to the reviewed 0.6.14 workflow commit
   under the existing publication and channel gates. Record old and new SHAs.
4. Only after promoted `v5` declares `hosted_runner`, update opted-in downstream
   callers to pass their trusted runner label. Older tag-pinned workflows reject
   this unknown input. Verify actual Ubicloud routing for an opted-in caller,
   the GitHub default for an unchanged caller, and preserved local Ollama
   routing; source-level runner assertions alone are insufficient.
5. Trigger a fresh end-to-end mention reply on the promoted channel. Retain
   sanitized evidence bound to the new workflow SHA, installed CLI `0.6.14`,
   configured backend/model, successful inference and posted reply. Historical
   failed runs are not proof that the new cutoff works. Separately verify
   a fresh human-review finding followed by an authorized supported explanation,
   checking that only eligible exact-head approval follows. Also verify a
   valid stale-baseline full recovery and a synthetic broker rejection after
   Worker deployment, preserving finding adjudication and approval gates.

Use the rollback and yank policy below if verification fails. Never overwrite a
published version; workflow rollback uses the previous immutable target, and
Worker rollback uses the prior recorded deployment.

## Compatibility manifest and release sequencing

A supported cutoff binds workflow commit, Python distribution, npm artifacts,
public schema version, and Worker identity in one compatibility manifest.
Build the document from the exact artifact files after those lanes produce
immutable bytes:

```bash
python scripts/build_compatibility_manifest.py \
  --release 0.1.1 \
  --workflow-commit <40-character-sha> \
  --workflow .github/workflows/review-sensei-run.yml \
  --python dist/review_sensei-0.1.1-py3-none-any.whl \
  --npm @reviewsensei/cli=dist/reviewsensei-cli-0.1.1.tgz \
  --worker dist/review-sensei-worker \
  --schemas-version 1.0 \
  --compatible-worker-range '>=0.1.1 <0.2.0' \
  --provenance-kind github-artifact-attestation \
  --output release/compatibility-manifest.json
python scripts/validate_compatibility_manifest.py release/compatibility-manifest.json
```

The builder hashes file bytes. Missing files, duplicate npm names, untrusted
sidecar provenance, and combinations that do not share one release version
fail closed. GitHub artifact attestation remains the trust mechanism for the
bundle; do not authenticate a release by fetching a digest beside an untrusted
artifact.

Sequencing is build → verify → canary bind → publish/promote. Fixture
downstream evidence from #34 can bind the exact manifest digest. A live
disposable-repository canary is operator-only and cannot authorize workflow-channel
promotion in this contract. Promotion is serialized, records previous and new
channel targets, and is refused when publication is partial or a package version
would be replaced. A `partial` publication state means at least one lane is
published but not all five; finish the remaining lanes (for example the npm
launcher after platform packages per ADR 0031) before retrying promotion with
the same manifest. A `failed` state means every lane is still unpublished and
also blocks promotion until a fresh manifest is built from new artifact bytes.
Rollback uses the previous immutable channel target and records both SHAs. If
the write-channel tag moves during a run, the default is fail-closed retry; bounded grace exists
only as an explicit in-memory authorized record for that run.

Implemented by this change: the manifest schema, digest builder, identity
proof APIs, canary-binding record, and promotion/rollback records. Operator-only:
assembling every lane's bytes, live canary, attestation verification on a
consumer runner, and moving or protecting `v5` in GitHub (keep `v4` protected
while the broker still accepts it; see #26).

## Local build and verification

Run these commands from the repository root in a disposable Python 3.11+
environment. The second environment is deliberately outside the checkout so
the smoke test cannot succeed by importing `src/`.

```bash
python -m pip install --upgrade build
python scripts/check_release_version.py --tag v0.1.0
rm -rf dist release
python -m build --sdist --wheel --outdir dist
sha256sum dist/*.tar.gz dist/*.whl
python scripts/validate_release.py dist --version 0.1.0

python -m venv /tmp/review-sensei-wheel-check
/tmp/review-sensei-wheel-check/bin/python -m pip install dist/*.whl
/tmp/review-sensei-wheel-check/bin/python -m pip check
/tmp/review-sensei-wheel-check/bin/review-sensei --help
sdist_root=/tmp/review-sensei-sdist
rm -rf "$sdist_root"
mkdir -p "$sdist_root"
tar -xzf dist/*.tar.gz -C "$sdist_root"
suite="$(printf '%s\n' "$sdist_root"/review_sensei-*/tests | head -n 1)"
export REVIEWSENSEI_CHECKOUT_ROOT="$PWD"
export REVIEWSENSEI_DIST_SAFE_LANE=1
/tmp/review-sensei-wheel-check/bin/python -I -m unittest discover -s "$suite/dist_safe" -v
/tmp/review-sensei-wheel-check/bin/python -I -m unittest discover -s "$suite/downstream" -v
```

The dist-safe command, SHA-256 archive identification, and checkout-only lane
are recorded in
[`tests/fixtures/distribution-contract.json`](../tests/fixtures/distribution-contract.json).
Do not run `unittest discover` against the developer `tests/` tree from the
wheel environment: that lane can import checkout helpers and `src/`.

The optional [`.github/workflows/downstream-canary.yml`](../.github/workflows/downstream-canary.yml)
workflow is a protected-environment placeholder. It is not a required CI gate,
contains no secrets, and does not call live providers. Remaining operator-only
steps before any live canary: create the `downstream-canary` GitHub Environment
with required reviewers; dispatch only against a disposable repository that
uses reviewed synthetic data; bound privileges to that repository; retain
sanitized outcomes bound to the exact workflow/package digest; and clean up.
Flaky live canaries must not weaken deterministic required gates.

The optional [`.github/workflows/local-review-contract.yml`](../.github/workflows/local-review-contract.yml)
workflow exercises the documented local review contract on a hosted runner. It
is manual-only and not a required CI gate: dispatching it installs the package
from the selected ref and runs the standalone smoke, which asserts the default
three-tier review exits (`0` clean, `1` required fixes, `2` could not complete),
the output formats, the clean-host local review, and the operational contract.
The hosted review lanes select `--exit-semantics operational`, so this lane is
what keeps the default review contract exercised end to end at the workflow
level.

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

## Tag-driven documentation starting with 0.6.17

**0.6.17 is the first tag using this automation.** Merge and qualify the reviewed
preparation/diagnostic workflow before creating it, then follow
[prepare main before signing and building](#prepare-main-before-signing-and-building).
PR #226 remains unmerged as a comparison reference. No tag is eligible from
this implementation PR's unchanged 0.6.16 metadata. The owner-only preparation
commits the aligned version/changelog/current-installation/source docs first;
full CI on that exact generated commit precedes the owner's signed tag.

The tag build retains staged immutable provenance and SHA-pinned docs/example
links. It does not mutate the signed source, invent new prose/provider claims or
replace pre-tag package metadata. The successful-release snapshot remains a
separate machine-owned main update. This means source docs are generated before
package builds, while final release/run provenance is generated after the signed
source exists. Ordinary content changes still need review and source generation
checks; historical versions outside current markers retain their values.

The read-only `Release` documentation job checks the exact event SHA and clean
checkout against the signed annotated tag object as verified by GitHub. It
validates Python/npm/changelog/version and the manifest/schema/evidence,
checks reviewed source generation, then derives the tagged version in the staged
manifest/snippets and builds the entire static site under runner staging. It pins repository docs and example links to the immutable source SHA,
pins previously unversioned current-installation pip snippets, and records tag, version, tag object,
source SHA, release run and build attempt in public provenance. A file inventory
covers the site's exact bytes. The static bundle is retained as
`review-sensei-release-docs` for 90 days (subject to repository retention limits).
It is a source-bound build record, not a package attestation or a registry
verification receipt.

Pages wakes only after `Release` completes successfully, not after main CI or
on a PR. Its helper executes from the trusted default-branch SHA and has only
Contents/Actions read. It requires successful documentation, PyPI, npm and
GitHub-release jobs, a published non-draft/non-prerelease GitHub release, a
verified signed annotated tag still pointing to the run SHA, source ancestry
on main, and exactly one retained artifact from that run. It downloads that
artifact by ID, rechecks the remote cutoff, and validates all file hashes and
metadata. After `github-pages` environment approval, a separate deploy job rechecks the
selected tag/run/artifact identity and high-water mark immediately before Pages
deployment. It grants Pages/OIDC write plus the Contents/Actions read needed
for that check, executing only the pinned trusted default-branch helper. It
never downloads or executes released code. The separate docs writeback job uses
a dedicated release docs App installed only on this repository. Its private key
lives only in the main-only `release-docs-main` environment. The pinned token
Action requests Contents write, Actions read and mandatory Metadata read for
this repository, then revokes the short-lived token after the job. The default
GITHUB_TOKEN remains read-only. Existing Pages environment rules/reviewers
remain the deployment gate.

After the same successful Release qualification, a separate trusted-main job
verifies the exact artifact and commits only `docs/releases/latest/` (the complete
static site snapshot, provenance and file inventory) and
`docs/releases/latest.md` (release/version/source and example links). It does not
rewrite runtime source, package versions, workflows, reviewed source docs, tags
or the operator-managed `v5` channel. The index and snapshot are machine-owned;
hand edits, symlinks, byte tampering and incomplete prior snapshots fail closed.
The reviewed `docs/releases/.gitattributes` policy preserves the artifact's exact
bytes during Git checkout, including on systems with line-ending conversion.
Nested `.gitattributes` files are rejected, and the complete indexed snapshot
must match the verified bytes before commit; ignored or transformed files stop
writeback without a push.
Current main source docs may include unreleased work; the generated snapshot is
explicitly bound to the released SHA.

The job integrates with freshly fetched main using a disposable worktree and a
non-force push. It rechecks tag/run/artifact identity and the newer successful
release high-water mark before pushing. A genuine concurrent main advance is
retried at most three times; permission/policy failures on an unchanged branch
are terminal. Same-tag/source retries retain the first verified snapshot and
make no additional commit, even if a docs rebuild changes its attempt/artifact
ID. Workflow runs are serialized without cancellation. Only a completed Release
or explicit run-ID retry triggers writeback; neither the docs commit nor main CI
starts another release or docs cycle. App pushes do trigger ordinary main CI;
that CI does not start another Release or Pages writeback. Artifact hashes and
staged-path checks run before the push.

**Dedicated App exception and bootstrap:** ordinary main changes still require
one independent approval, code-owner review and last-push-independent approval.
Owner setup isolates that unchanged PR rule in a main-only ruleset whose only
new bypass actor is the dedicated docs App. Main's existing deletion, non-force
and linear-history rules stay in the original ruleset, without an App bypass.
The helper verifies repository-scoped App identity/permissions and GitHub's
effective bypass decision: `always` only for the isolated PR rule, `never` for
applicable non-PR protections. Omitted decisions fail closed; no Administration
permission is added to expose them. This is the explicitly approved App
exception, not an admin bypass or per-release docs PR.

Follow [the secure App setup and read-only readiness procedure](release-docs-bot.md).
Owner setup requires an exclusive policy-editing window and the explicit
`--exclusive-owner-setup` declaration before applying changes. Ruleset updates
are not atomic: a writer that violates that window can have its edit overwritten
between the final read and PUT. If exclusivity is unavailable, keep the tag held.
Keep v0.6.17 held until the implementation is reviewed and merged, owner setup
is complete, and the `Check release documentation bot` workflow succeeds on
main using the real narrow App token. Code CI alone does not establish readiness.
The key is entered by the owner directly in GitHub; never send it through chat
or commit it. Runtime never changes protections, installs credentials or moves a
tag. Pages remains separately qualified and gated. A main-writeback failure
cannot undo published packages or the separately deployed site. After repairing
configuration, retry Pages from main with the same successful `release_run_id`.

A failed, cancelled or partial release leaves both the previous generated main
snapshot and published site in place, even if one registry or GitHub release
already contains the new version.
Recover the original release jobs/bundles under the existing package procedures;
never retag. Re-running a failed publisher may reuse docs built on an earlier
attempt of the same release run. If the docs build itself failed, rerun that job;
a successful rebuild replaces the named artifact with a new artifact ID. The
site is deterministic for the same tag, source and build attempt. Concurrent
Pages runs are serialized without cancelling the active deployment. GitHub may
replace a pending run; if the desired deployment was not delivered, dispatch
Pages from main with `release_run_id` set to the successful release run ID.
The same full qualification applies to dispatches. This does not rebuild or
republish packages.

Automatic deployment and manual retry accept only the latest numerically ordered
successful stable release with the required docs job and retained artifact.
There is no minimum version in the script: its tag input selects the version,
and signed source, completed release jobs and artifact provenance determine
eligibility. A higher version whose entire
Release did not succeed does not suppress the prior good cutoff. A successfully
published newer release is a high-water mark even after its artifact expires or
its tag is moved/deleted: rerunning an older release cannot roll back the site.
The selector paginates release/run history and fails closed on incomplete
history, missing artifacts, ambiguous runs/jobs or changed tag identity. Public
Pages therefore describes the released cutoff; current main documentation may
include unreleased features and remains available through GitHub.

Historical docs are the immutable GitHub source/docs/examples links, release
notes and retained per-run static bundles; this workflow does not promise a
permanent `/versions/` archive on Pages. Preserve any historical bundle needed
past artifact expiry in an operator-controlled archive before it expires. To
repair published docs, prefer a reviewed higher patch release. An emergency
rollback requires a separately reviewed change to this explicit policy and
maintainer authorization; this dispatch path has no rollback bypass. Never move
an immutable tag or silently publish main as released docs. Before the 0.6.17
first-use cutoff, the existing live site remains unchanged. Historical release
runs lack the required documentation job/artifact and fail qualification on
that evidence, rather than on their version number.

Local qualification includes:

```bash
python scripts/check_release_version.py --tag v0.6.17
python scripts/check_action_pins.py
python scripts/validate_json_contracts.py
python scripts/validate_public_schemas.py
python scripts/validate_site_manifest.py
python scripts/build_site_pages.py --check
python -m unittest tests.test_release_docs tests.test_release tests.test_site_manifest \
  tests.test_site_examples tests.test_site_getting_started tests.test_site_claims -v
```

The credential-free release-docs tests execute the real site builder on a
synthetic 0.6.17 tree and verify schema, immutable source/example links,
versioned installation snippets, deterministic bytes and unchanged source.
They also cover fork/PR/dispatch rejection, failed/skipped publication lanes,
unsigned/lightweight/moved tags, unmerged source, missing/expired/duplicate
artifacts, byte tampering/symlinks, retries and newer-release supersession.
The first live tag/deployment remains a release-owner gate after this PR merges;
a PR test run must not dispatch Pages or create a release as a smoke test.
