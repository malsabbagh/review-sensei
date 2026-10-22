# Releasing ReviewSensei

The existing Release Marshal (`prepare-release.yml` and its dedicated
`sign_release.py` signer) prepares one reviewed core version and immutable
signed `vX.Y.Z` tag. Its existing App-token tag push starts **two workflow
runs** from that exact source:

- `release.yml` / **Release**: Python build, documentation, reviewed GitHub
  draft, protected PyPI publication and GitHub finalization.
- `publish-npm.yml` / **Release npm**: five native builds, six attested npm
  tarballs and protected npm publication.

Both qualify the prepared receipt, signed tag object, exact source/CI and
matching core/npm version. The hosted adapter retains its independent version
and `release-github.yml` / `pypi-github` identity. There is no new dispatcher,
App permission or signing credential. See [ADR 0100](adr/0100-independent-release-channel-runs.md).

This process applies only to newly prepared releases containing the split.
Existing signed tags retain their original workflows. Package publication and
workflow retries remain maintainer operations; an ordinary PR grants neither.

For the next complete-evidence library and lasting unified repository pipeline,
follow [the runtime rollout plan](review-runtime-rollout.md). Its next-version
candidate must be rechecked against all registries before preparation; `v5`
promotion follows verified engine and hosted PyPI publication rather than a
floating install.

After a separately authorized signed major-channel move, follow
[major-channel validation](major-tag-validation.md). The validator binds the
exact tag object/source and attested published wheel, reuses the installed CLI
harness, and documents the default-branch dispatch needed for historical targets.

## GitHub Release draft and publication

Starting with the next release containing this policy, successful Python and
docs builds prepare a **draft** GitHub Release before protected publication
approval. Native/npm assembly runs in the separate **Release npm** run. Notes
come from that version's reviewed, dated changelog
section, with links to its exact source commit and Release run. The four assets
are the validated wheel, source archive, `SHA256SUMS` and SPDX SBOM. Checksums
cover all three payloads; their GitHub build attestations must bind the signed
tag ref, source digest and Release workflow on GitHub-hosted runners.

Both existing `npm` and `pypi` environment approvals remain required for their
own distribution channels. After the PyPI publisher succeeds, the final job
publishes the verified draft without waiting for npm assembly or publication.
A pending or failed npm lane stays visible and must be recovered independently;
it is never marked successful to qualify the Python release.
Pages requires both complete successful channel runs for the same signed
tag/source.
A draft or failed Python or npm channel cannot authorize released-docs Pages.
No additional GitHub App, private key, rules bypass or registry credential is
needed: the two GitHub Release jobs use their short-lived workflow token with
Contents write, Actions read and Attestations read. They cannot approve the
publisher environments or sign tags.

For an interrupted draft job, rerun that job in the original Release run with
its retained original bundle. An exact matching draft resumes only missing
assets. A dropped create/upload/publish response is reconciled through readback;
an already published exact release with successful publisher proofs is a no-op.
The finalizer never creates a missing draft or repairs missing assets: retry
draft preparation first. Finalizer-only retries reuse prior successful build
and publisher attempts, while a newer failed attempt blocks publication.

Existing notes, tag/source/run identity and asset SHA-256/size must match.
Unexpected assets, duplicate releases, missing remote digests, edits or moved
tags hold publication without overwriting or deleting anything. Inspect the
mismatch with the owner instead of forcing a retry. Retain Actions artifacts
until release recovery is complete. GitHub has no transaction spanning tag,
job and release state; final fresh checks and per-tag job serialization limit
but cannot remove changes made concurrently by a repository administrator.
`make_latest=legacy` asks GitHub to select the latest stable version rather than
unconditionally promoting an older retry.

Existing immutable tags keep the workflow at their original commit. This change
does not update or backfill their releases. Review any older-release recovery
separately; never move a tag to opt it into the new process.

## PyPI publication and v5 promotion

The next-release policy in [ADR 0095](adr/0095-pypi-qualified-major-channel.md)
requires verified publication of both `review-sensei` and `review-sensei-github`
before v5 qualifies. It does not change the active 0.8.4 / 0.3.4 release or its
promotion procedure. Merge and exact-head CI precede the next prepared release;
no existing immutable tag is moved or retrofitted.

The validator keeps the signed immutable engine release, reviewed source,
GitHub release binding, exact assets and GitHub build provenance checks. It also
reads back both PyPI wheels and sdists and verifies their digest, publication
attestation, repository, workflow, signed source/ref, publishing run/attempt,
GitHub-hosted runner and protected environment. The hosted wheel must pin the
exact engine version. All four installed CLI lanes consume both verified wheel
hashes, and the final recheck freezes both Python tag and registry identities.

Overall Release success is not an admission signal for v5: npm can still be
pending or failed. Required Python/source/docs/GitHub release jobs must each
have a successful latest execution. Protected approvals, signed source
preparation, workflow drain, downstream tests and the old-object lease still
apply; this policy grants no authority to promote a channel.

npm is a Node launcher with optional platform packages containing frozen native
engine binaries, rather than an installer of the PyPI package. Its exact
six-tarball inventory, attestations, registry integrity readback and protected
publication remain separately verified. Released Pages documentation requires both successful Python and npm runs;
that separate docs policy is not a prerequisite for v5 validation.

## Independent retry and npm publication (issue #103)

npm publishes only from the Marshal's signed `vX.Y.Z` push in
`publish-npm.yml`. The old default-branch manual/bootstrap/cross-run resume
publisher is retired for future releases. No user-supplied version, source,
run ID or artifact can start a second publisher. Each channel requires exactly
one original tag-push run; recover that run, never dispatch another workflow
or delete/recreate a tag. Duplicate runs fail closed, even when one failed.

The `npm` environment keeps Mo's approval and its existing `v*.*.*` tag rule.
After approval, npm requalifies the signed source/version and the exact artifact
ID/digest emitted by its own assembly job. Downloads fail on digest mismatch.
All six tarballs plus source metadata must have verified GitHub attestations
for this workflow, signed ref/source, GitHub-hosted runner and original build
run/attempt. Publisher-only retries reuse the original successful assembly
attempt, rather than guessing the current publishing attempt. npm publication
requires the matching **published** stable Python release, a terminal-success
Python channel run, successful latest source/build/docs/draft/PyPI/finalizer
jobs, exact reviewed notes/source/run
binding and four assets. npm never downloads Python's build artifact.

Approve npm only after the original Python run reaches terminal success,
including its result recorder. This intentionally orders npm publication after
Python finalization and recording. Native/npm
builds can run concurrently with Python. If npm reaches verification first,
missing publication or failed/pending finalization holds that publisher; after
Python recovery succeeds, retry the original npm publisher with its retained
attested bundle. No polling loop, rebuild or new run is introduced. Python
finalization and qualification still do not wait for npm, and its run can become
terminal and retry while the npm run is pending or failed.

The existing `contents: read` grant is sufficient for public release metadata
under GitHub's [published release API contract](https://docs.github.com/en/rest/releases/releases#list-releases).
No draft-read ability is assumed: even a visible draft fails the strict
published-release validator. The job does not gain Contents write, a new token
or a cross-run artifact bridge to read unpublished state.

The unchanged npm helper preflights all six packages, publishes missing
platforms first, reads back exact SRI with bounded retry/backoff, then publishes
the launcher last. Registry HTTP404 can be eventual visibility after successful
publication: retain the original bundle and let bounded readback finish.
Existing exact registry entries are skipped; different bytes hold publication.
The clean Linux consumer installation and `npm audit signatures` remain.
No rebuild, overwrite, replacement artifact or moved tag repairs partial npm.

### Published npm consumer verification

Future signed releases containing [Proposed ADR 0104](adr/0104-published-npm-consumer-verification.md)
add a separate read-only `verify-published` matrix after `qualify-source` and
`publish-npm`. Its checks are named `Verify published npm consumer (TARGET)` for
`linux-x64-gnu`, `linux-arm64-gnu`, `darwin-x64`, `darwin-arm64` and `win32-x64`.
Native builds, prepublication smoke tests, tarball/source attestations and the
protected publisher remain required. The verifier adds no publishing authority.

Each target takes the exact version and source SHA from the signed qualifier
outputs. Verification-only npm 11.16.0 installs the exact launcher and optional
native dependency from the public registry with `--ignore-scripts`, a fresh
prefix/cache and empty user/global configurations. The helper verifies installed
version, native package, registry integrity and provenance against that source,
and runs `npm audit signatures`. Its CLI subprocesses use a strict environment
allowlist without inherited npm, GitHub or provider credentials. No checkout
engine, staged bundle or system Python fallback supplies the command.
The existing publisher retains npm 11.5.1; its publication toolchain is unchanged.
Unix lanes execute the owned npm bin shim. Windows validates the cmd shim's
target and invokes that installed JavaScript launcher through Node without a
command shell; the receipt records the entrypoint used.

The installed command must pass `--version`, `--help` and deterministic fixture
evaluation, produce the expected blocking review with review exit code 1 and
operational exit code 0, and reject an invalid diff with exit code 2. Fixtures
use synthetic data and require no live provider credentials. Each target
attempt always runs the upload step for its receipt directory as
`npm-consumer-TARGET-RUN-ATTEMPT`, including `receipt.json`, command stdout/stderr,
`package-lock.json`, `audit-signatures.json` and any produced evaluation/review
artifacts. Missing receipt files fail the upload, including when initialization
failed before receipt creation. Inspect failure evidence before retrying;
absence is not a pass.
The lock hash covers the final retained bounded lock file, when available.
Completed or timed-out commands retain at most 4 MiB per stdout/stderr stream;
the receipt records original sizes and truncation. Oversized completed output
still fails verification, and timeout remains a failure. This cap applies after
execution and does not impose a streaming or peak-disk quota.

The npm channel result waits for all five latest authenticated verification job
conclusions alongside its existing required jobs. A failed, skipped, cancelled,
missing or ambiguous check leaves the channel incomplete even when publication
has already succeeded. These consumer receipts supplement the original
publication proofs. Python/v5 remains independent, and Pages retains the
requirement that both release channels complete successfully.
Pages reads the bounded npm workflow from the exact signed source as data and
verifies its Git blob identity. The closed
`# reviewsensei:published-npm-consumer-verification:v1` declaration requires all
five latest successful consumer jobs at selection and fresh rechecks. Historical
combined runs and split sources with neither the consumer job nor its declaration
keep their original prerequisites. Unknown, malformed or duplicate declarations, or
an undeclared `verify-published` job, refuse admission.

Retry failed verification jobs in the original **Release npm** run against the
same published version and qualified source. Do not republish, rebuild different
bytes or create a tag merely to retry verification. Preserve the original bundle
and receipts. A defect in the published package requires a reviewed repair and
higher version through the usual release process. Existing signed tags keep
their original workflow; evidence from an earlier version or one target does not
qualify a future release's five-platform matrix.

For a failed Python draft/finalizer, retry that job in the original **Release**
run once that run is terminal, even if **Release npm** is still waiting or
publishing. For a failed npm publisher, retry only that job in its original
**Release npm** run, with its retained bundle. The two runs share no workflow
concurrency group, pending approval or lifecycle. Their version-specific
publisher groups never cancel active work. A newer failed execution cannot be
hidden by an older successful job. Published versions and signed tags remain
immutable; expired/missing/conflicting evidence holds recovery for owner review.

Each qualified channel runs an `always()` result job after its own dependencies
finish and uploads `release-python-result-RUN-ATTEMPT` or
`release-npm-result-RUN-ATTEMPT`. The JSON records signed tag object, source,
version, workflow, run/attempt, authenticated latest job conclusions and
artifact IDs/digests. Failed/skipped jobs stay failed/skipped and the channel
run remains non-successful. A failed source qualifier cannot produce a qualified
channel receipt; its Actions failure is retained. Inspect both run URLs/results
independently. These operational receipts grant no publication or promotion
permission and do not replace PyPI/Sigstore verification under ADR0095.

### Owner migration hold before activation

This PR does **not** change registry trust, credentials, persistent permissions,
reviewers, environment rules, signing setup or security settings. Before the
first future release, the owner must verify each of these six existing npm
packages has Trusted Publishing with repository `malsabbagh/review-sensei`,
workflow filename **`publish-npm.yml`**, environment **`npm`**, and publish
permission:

| Package | Current registration inspection | Desired workflow / environment | Change required? |
| --- | --- | --- | --- |
| `@reviewsensei/cli` | Read denied: registry HTTP401 on 2026-10-21 | `publish-npm.yml` / `npm` | Unknown until owner reads trust records |
| `@reviewsensei/cli-darwin-arm64` | Not read after shared npm authentication failure | `publish-npm.yml` / `npm` | Unknown |
| `@reviewsensei/cli-darwin-x64` | Not read after shared npm authentication failure | `publish-npm.yml` / `npm` | Unknown |
| `@reviewsensei/cli-linux-arm64-gnu` | Not read after shared npm authentication failure | `publish-npm.yml` / `npm` | Unknown |
| `@reviewsensei/cli-linux-x64-gnu` | Not read after shared npm authentication failure | `publish-npm.yml` / `npm` | Unknown |
| `@reviewsensei/cli-win32-x64` | Not read after shared npm authentication failure | `publish-npm.yml` / `npm` | Unknown |

The read-only `npm trust list @reviewsensei/cli --json` call (npm 11.16.0)
reached `GET /-/package/@reviewsensei%2fcli/trust` and returned HTTP401
("You must be logged in to publish packages"). This is an npm authentication
hold, not evidence that any registration is missing. No login, credential,
trust, permission or security-setting mutation was attempted. The successful
old `release.yml` publication proves that identity worked for that execution;
it does not enumerate today's registrations or prove `publish-npm.yml` exists.

Owner checklist, using an existing authorized npm session:

1. Read `npm trust list PACKAGE --json` for **each** row; this operation is
   [documented as listing trust relationships](https://docs.npmjs.com/cli/v11/commands/npm-trust/).
2. Compare repository `malsabbagh/review-sensei`, workflow `publish-npm.yml`,
   protected environment `npm`, GitHub provider and publication permission.
   Record the existing trust identity and the comparison for every package.
3. If all six already match, record **no registry change required**. Merely
   reusing the existing manual workflow's filename is not a security mutation.
4. If any is absent or mismatched, report the exact current/desired difference,
   stop activation and obtain specific action-time owner approval before
   changing that registration. This PR grants no such authority.
5. Coordinate retirement of the old workflow identities separately, after all
   old release recovery is complete. Keep the merge/activation hold until the
   release owner coordinates it.

The existing manual filename may already have a registration; never assume
that registration is present, complete or grants publication. If a record is
missing/different, **stop activation** and report that exact package and required
identity to the owner for a separately authorized registry migration. Do not
weaken provenance, substitute an npm token, change an environment or grant an
App more authority. Core PyPI stays `release.yml` / `pypi`; hosted PyPI stays
`release-github.yml` / `pypi-github`.

Old `release.yml` npm trust and any legacy dispatch authority require an
owner-reviewed retirement plan only after all old immutable release recovery
is complete. Merging a workflow does not revoke trust in old signed sources.
Keep this PR unmerged until the current release owner coordinates that window.
A rollback before a future release must also restore/reverify the original
publisher identities; never rerun signing to retrofit an existing release.

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

## review-sensei-github

The hosted GitHub runtime is a second PyPI distribution,
`review-sensei-github`, in `packages/python/review-sensei-github`. Its version
is independent of the engine. The published wheel's `review-sensei==` pin is
immutable, so the pin moves only when that same preparation also publishes a
new hosted version.

`prepare-release.yml` prepares whichever package receives a new version. Pass
a package's current version to leave it unchanged and untagged. `sign_tag=true`
signs `vX.Y.Z` only when the engine version is new, and `github-vX.Y.Z` only
when the hosted version is new.

```bash
gh workflow run prepare-release.yml --ref main \
  -f version=0.6.23 -f github_version=0.1.1 \
  -f release_date=2026-10-12 -f sign_tag=true
```

- Engine `0.6.23` and hosted `0.1.1` prepare both. The hosted pin becomes
  `review-sensei==0.6.23`. Signing pushes `v0.6.23` and `github-v0.1.1`.
- Engine `0.6.23` and hosted `0.1.0` (the current hosted version) prepare the
  engine only. The pin stays `review-sensei==0.6.22`. Do not move `v5` onto
  that commit: runners would install the already-published `0.6.22` wheel,
  which has no `hosted_hooks` parameter.
- A hosted release cannot pin `0.6.22`. That wheel is already on PyPI and
  predates the seam, so `review-sensei-github review` would crash. The first
  hosted tag ships in the same preparation as the new engine, where the pin
  becomes `review-sensei==0.6.23`. After that engine is published, a later
  hosted-only release can keep the pin on that engine.

`prepare-github` still prints that identity and creates no tag. The first
hosted publish needs the PyPI project `review-sensei-github` and a Trusted
Publisher for workflow `release-github.yml` and environment `pypi-github`.
Create that protected environment before signing a hosted tag; publication
waits for Mo's approval there. Move `v5` last, only onto a commit whose
declared engine and hosted versions are already published and whose pin
matches the engine. `review-sensei github`, `host-plan`, and
`resolve-hosted-openrouter` remain subprocess stubs for one engine release.
Remove them in the following release.

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
     -f version=0.6.17 -f github_version=0.1.0 -f release_date=2026-10-08
   ```

   `github_version` is the hosted package version. Pass its current version,
   as above, to prepare the engine only. Pass the engine's current version to
   prepare the hosted package only. See [review-sensei-github](#review-sensei-github).

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
   notes PR.    Generated notes are recomputed from source during commit validation;
   their digest remains bound to the preparation receipt. The receipt also
   records `engine_tag` and `github_tag`; an empty tag means that package is
   unchanged and will not be signed. App permissions are unchanged. See [ADR 0071](adr/0071-generated-release-notes.md).
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

For the next release, apply the [PyPI publication policy](#pypi-publication-and-v5-promotion)
before the channel operation. npm/native builds or publication may remain
pending or failed; they do not hold v5. Signed immutable engine source, both
verified Python publications, workflow drainage, compatible readers, downstream
tests and the exact old-object lease remain required. This does not change the
separately authorized active 0.8.4 / 0.3.4 promotion.

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
Linux's shared build helper admits digest-pinned Docker Official Images from
`public.ecr.aws/docker/library` first, then the official `docker.io/library`
upstream if ECR fails. Each origin gets one attempt: a depleted anonymous data
allowance cannot be repaired by immediate repeated pulls. The fallback preserves
the entire image suffix, including the exact SHA-256 digest, and is logged before
use. No third-party registry, mutable tag, credential or new cache is introduced.
The pinned manifest contents and Debian 12 glibc baseline are unchanged. Both
build and consumer images are admitted before any container starts; each smoke
test uses its admitted reference with `--pull never`, and the consumer remains
offline. If both origins fail for either image, the build stops before execution.
Manifest-only verification against Docker Hub confirmed the existing Python
`b99029c95d3d37fb1e4e76d287f7984373dca77c665885986e31b2c95260c13c` and Debian
`3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251` index
digests, including their amd64 and arm64 descriptors. Actual native CI still
qualifies the pulled images and resulting executables before release.
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

This full-distribution manifest and legacy `v4` serialization contract still
requires all five publication lanes. It does not represent next-release v5
admission: use the independently verified Python publication receipt from
[major-channel validation](major-tag-validation.md) for that channel. Never mark
an incomplete npm lane complete or fabricate a full-distribution manifest to
promote v5; preserve its actual partial state for independent recovery.

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
