# Complete-evidence runtime and unified review rollout

The repository selects `advanced.review_work.mode: unified` as its lasting
full-review and human-reassessment route. Preserve this selection after
qualification. It uses the existing complete-evidence planner and bounded
executor; it does not expand provider qualification, result, ledger, transport,
publication or approval bounds. Remove the redundant legacy
`advanced.large_changes.orchestrate: false` override. Repository selection is
distinct from the packaged default and becomes live only on trusted `main`.

The released legacy reassessment route refused PR #236's complete 15,603-byte
baseline patch because its diff context ceiling is 12,288 bytes. Released 0.6.18's
unified planner admits that exact required patch and the authorized factual reply
as one 21,714-byte prompt under its unchanged 49,152-byte admission bound, with
zero unprocessed requirements. This is an admission proof, not a model decision
or approval. Missing, incomplete or oversized required evidence remains pending.
These measurements describe the PR #236 evidence captured on 2026-10-09; they
are not a capacity guarantee for later heads. Rebuild the exact required patches
and factual reply through that runtime's work planner and measure the UTF-8
request before using this admission result for a different source identity.

## Candidate and prerequisites

Prepared against the identities verified on 2026-10-09:

These are dated observations. Refresh them against remote tags and registries
before any operation; stop if the version, source or channel identity changed.

- Latest stable library: `0.6.18`, immutable version tag `v0.6.18`, source
  `9e955975c64c293f80d005da1137494eaffd59b3`.
- Signed movable `v5` tag object:
  `ef5cf9f7cf6f5875263bd61606dedd1a1c90c5c8`, targeting that same source.
- Next candidate: **0.6.19**, subject to fresh absence checks for the version
  tag, GitHub release, PyPI and all six npm package versions. A failed lookup
  does not prove absence. Never reuse a published or partially published version.

Merge and qualify the intended reviewed source before preparation, including
[PR #236](https://github.com/malsabbagh/review-sensei/pull/236)'s complete
persistence/readers and this repository configuration. Use the ordinary branch
review policy; admin bypass needs distinct authorization. Do not bump only a
package pin, point `v5` at a pre-release main commit or publish the candidate
while its source is unmerged. Other source changes join this cutoff only when
explicitly selected and merged.

Run full source checks, installed-wheel/distribution lanes and the immutable
reader qualification. The complete-evidence qualification must cover 12+
findings, malformed and oversized evidence, all-resolution/state growth, local
restart, publication retries and GitHub reconstruction. Old readers reject the
new encoded baseline and rich eligibility contract; they must not recover
approval from older markers. Qualify every shared-ledger reader and delayed
reply/approval finalizer before activating new writers. Do not delete an
obligation or reset a ledger to simplify this rollout.

Before merging this configuration into trusted `main`, record the executing
workflow/package identities for every active or delayed reader of this
repository's authority and require compatible-reader qualification. A green
source CI run or this document does not prove that no legacy deployment remains.
If the reader inventory is incomplete or any reader is incompatible, keep the
policy change pending; holding package publication alone does not prevent the
configuration from becoming active after a merge. Preserve the release hold.

## Prepare, build, verify and publish

1. Record the final reviewed `main` SHA and require all fifteen terminal-success
   push/main CI jobs on that exact source. Verify existing Release Marshal/App
   readiness and signing metadata with the documented read-only checks. Keep
   current signing credentials and npm/PyPI environment reviewers unchanged.
2. Recheck candidate absence for all seven package versions and both GitHub identities.
   Dispatch the existing `prepare-release.yml` from fresh `main`, selecting
   `version=0.6.19`, the actual release date and `sign_tag=false` until readers
   and compatibility are qualified. It generates the receipt-bound version,
   six npm manifests/dependencies, current install pins, changelog and source
   docs. Do not hand-edit those generated release payloads into this config PR.
   For a release actually prepared on 2026-10-09, the first dispatch is:

   ```bash
   gh workflow run prepare-release.yml --repo malsabbagh/review-sensei --ref main \
     -f version=0.6.19 -f release_date=2026-10-09 -f sign_tag=false
   ```

   Use the actual date if preparation starts later; retries retain that date.
3. Qualify the emitted prepared SHA with all fifteen exact-head main jobs and
   credential-free installed-wheel/reader checks. If additional native inspection
   is needed before signing, the existing manual npm workflow's `publish=false`
   path builds without publication. Those inspection bytes are not a substitute
   for verifying the final tag-triggered Release artifacts. This configuration
   adds no broker capability or credential requirement.
4. After qualification, use the existing dedicated-key signing handoff for a
   **new** signed annotated `v0.6.19` on the qualified prepared SHA. The existing
   `sign_tag=true` workflow rechecks main, CI, the preparation receipt, signing
   identity and unused versions. Inspect GitHub's verified tag object and exact
   source. Never replace `v0.6.18` or an existing `v0.6.19`.
   The signing dispatch uses the same version/date with `sign_tag=true` only
   after the emitted prepared SHA and reader qualification are verified.
5. Let that immutable tag trigger `Release`. Before the existing separate npm
   and PyPI environment approvals, inspect source gates, Python wheel/sdist,
   five native executables and six npm tarballs, exact checksums/SRI,
   attestations and installed CLI/schema parity. Verify Intel static OpenSSL and
   native/direct error behavior. Build the compatibility manifest from these
   actual artifact bytes and the reviewed workflow, schema and compatible Worker
   identity; bind complete canary evidence. Keep publication pending until this
   qualification succeeds. Publish platform packages before the npm launcher. Verify every
   registry version against its original artifact bytes and attestations before
   promotion. Recover partial publication through the original failed job/bundle;
   do not rebuild, move a version tag or mix artifacts from another source.
6. Verify successful Release completion, GitHub assets and the released docs
   snapshot/Pages provenance. Generated snapshot commits are documentation
   outputs, not replacement package/workflow source commits.

## Promote the signed v5 channel and verify consumers

Quiesce in-flight old-channel writer/finalizer jobs and record all consumer reader
identities. Keep writers pending until compatible readers are ready. The
broker resolves `v5` at exchange time: a channel move during an old workflow
fails closed and requires a fresh run. Preserve that behavior.

Re-read the remote old `v5` tag object immediately before promotion. Create and
locally verify a signed annotated replacement `v5` targeting the **published,
qualified v0.6.19 source**, using the existing authorized channel signer. Push
only `refs/tags/v5`, with an exact force-with-lease against the recorded old tag
object; a concurrent change stops promotion. Record old/new signed objects,
source SHAs and signer verification. Do not run the version-tag signer with a
fabricated version to move the channel. Channel signing is the separate existing
owner operation described in [the release runbook](releasing.md#public-reusable-workflow-tag-channel).

After recording the freshly verified `old_v5_object` and
`qualified_release_sha`, the owner's channel operation is:

```bash
git verify-tag v0.6.19
test "$(git rev-parse 'v0.6.19^{commit}')" = "$qualified_release_sha"
reviewed_old_v5_object=$old_v5_object
old_v5_object=$(git ls-remote --exit-code origin refs/tags/v5 | awk '$2 == "refs/tags/v5" {print $1}')
[[ "$old_v5_object" =~ ^[a-f0-9]{40}$ ]]
test "$old_v5_object" = "$reviewed_old_v5_object"
git tag -s -f v5 "$qualified_release_sha" -m "ReviewSensei setup v5, library 0.6.19"
git verify-tag v5
git push origin refs/tags/v5 --force-with-lease="refs/tags/v5:$old_v5_object"
```

The lease is the exact old annotated tag object, not its peeled commit. Read
back the new remote object, GitHub signature verification and peeled source
before starting fresh writers. These commands replace only the movable channel.

The public caller stays at `@v5`; the promoted reusable workflow installs its
source commit's exact `0.6.19` package from PyPI, with the existing exact-commit
fallback only for an unavailable distribution. Do not replace this contract
with `pip install --upgrade` or a floating source branch. Verify installed
version, executing workflow SHA, broker acceptance and registry provenance on
fresh consumers. npm/native consumers must use the same qualified 0.6.19 bytes.

On a specifically authorized, open qualification PR, establish fresh complete
current-base/head review authority, then submit a new authorized factual mention.
Verify full pending evidence, bounded decisions, retained unresolved obligations,
precise capacity status and actual final approval state. A merged PR #236 needs
no reassessment of its historical message. PR #231's branch remains untouched;
fresh review/mention on it requires confirmed target scope. Preserve all head,
ownership, source, generation, thread, blocker and qualification fences.

Keep unified mode active after qualification. A rollback to an old reader while
rich records exist fails closed and requires compatible reader restoration;
it is not repaired by restoring legacy routing or clearing findings. Any
emergency channel rollback needs a reviewed compatible cutoff and its own
recorded authorization.
