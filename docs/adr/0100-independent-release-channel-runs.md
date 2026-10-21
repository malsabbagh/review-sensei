# ADR 0100 - Independent signed-source Python and npm workflow runs

Status: Proposed
Date: 2026-10-21
Owners/Reviewers: Maintainers
Linked GitHub Issue: User-requested Release Marshal channel split; no separate issue
Linked PR: [#340](https://github.com/malsabbagh/review-sensei/pull/340)

## Context

ADR0095 removes npm from Python/v5 prerequisites, but separate jobs in one
Release run still share GitHub's retry lifecycle. In the 0.8.7 release, GitHub
refused the failed Python finalizer's retry while npm was publishing. An npm
registry visibility delay can therefore delay Python qualification despite
successful approved PyPI publication. That live release and its immutable tags
are outside this change.

Release Marshal is the established `prepare-release.yml` preparation and
`sign_release.py` dedicated signer. It already checks generated receipts,
reviewed main/CI, unused versions, signing identity and approval environments,
then pushes one signed tag through the narrow App token. It is not an invented
new coordinator application.

## Decision

Use that single signed core `vX.Y.Z` push to start two independent runs:
`release.yml` (Release) retains Python/docs/draft/PyPI/finalizer and all its
publisher/attestation identities. `publish-npm.yml` (Release npm) owns native
builds/assembly/npm. Retire the default-branch manual/bootstrap/cross-run npm
publisher to prevent competing publication paths. Add no workflow dispatch,
Actions write, App permission, key or registry credential.

Both channels validate the same signed tag object, immutable commit, prepared
receipt/CI and core/npm manifest version. The adapter retains its distinct
version and `release-github.yml` publication. Require one original tag-push run
per channel/tag; duplicated/mismatched runs fail closed. Recovery uses attempts
of the original channel run, with immutable original artifacts. No checkouts
follow main after selecting a signed source.

Pin publisher downloads to the same-run upload's exact artifact ID, fail on
archive digest mismatch, and requalify source after protected approval. npm
additionally verifies metadata/checksums, exact inventory and GitHub tarball/
metadata attestations against workflow/ref/source, hosted runner and the
original assembly invocation, independent of the later publishing attempt.
Preserve bounded registry retries, exact SRI, existing-byte refusal and
platform-first/launcher-last publication.

Order npm publication after the matching **published** stable Python release.
Require the original Python run's terminal success, including its result
recorder, before approving npm. Latest successful package jobs and public assets
alone do not admit an active, cancelled or failed Python channel run.
Verify the unique matching Python run's latest source/build/docs/draft/PyPI/
finalizer jobs, exact reviewed notes, signed source/tag/run binding and four
assets. Even visible drafts fail validation. The read-only metadata route is
supported by GitHub's [published release API contract](https://docs.github.com/en/rest/releases/releases#list-releases):
public release metadata is available with Contents read. Draft listings have a
different visibility boundary; no draft access or Contents write is assumed or
added. Reuse the strict existing public `major_tag_gate.release_binding` policy;
remove the earlier optional draft allowance from that helper.

npm does not download Python artifacts, and Python never reads npm state. Both
channels can build concurrently. npm publication holds if Python finalization
has not succeeded; retry that original npm publisher with its original bundle
after Python recovery. This is an explicit publication-order tradeoff rather
than an assertion that both distributions can publish independently. Their run
lifecycles, artifacts and retries remain independent. Each channel records its
own authenticated job/artifact facts and signed contract in a run/attempt-specific
result artifact; those operational receipts do not replace cryptographic
publication proofs.

Preserve ADR0095 and the exact PyPI publisher execution-origin verifier,
including original job evidence for re-exposed ancestor records. v5 remains
Python-only. Pages keeps the complete-publication policy: either channel's
completion can wake it, but both runs must succeed with the same signed
version/source before it selects Python's docs artifact. Legacy combined
runs retain their original validation policy.

## Scope

Future workflow topology, shared channel guards/results, Pages coordination,
regression tests and documentation. No package version, runtime implementation,
immutable tag, active run, deployed validator, live publication, security setting
or merge. Existing tagged workflows cannot be retrofitted.

## Consequences

An npm approval, failed build or registry wait no longer holds a Python run
open. Failed Python finalization can be retried after that channel is terminal,
while npm remains visible and independently recoverable. npm publication now
waits for verified public Python finalization; an early npm publisher holds
without publishing and resumes through an original-job retry after that Python
gate succeeds. Existing npm tarballs and original build invocation stay fixed. A failed
qualifier produces an Actions failure, never a qualified channel result.
Missing/expired/conflicting artifacts, duplicate runs or moved identities hold
recovery for owner review.

## Alternatives considered

- Keep separate jobs in one run: retains the observed GitHub HTTP403 retry block.
- Dispatch downstream workflows with version/source/run inputs: adds write
  authority, caller authentication and cross-run artifact-selection surfaces.
  The existing signed push already provides the common contract.
- Move Python to a new workflow filename: forces PyPI identity and provenance
  migration and risks breaking existing qualification. Keep that identity stable.
- Grant npm Contents write to read drafts: expands authority solely for
  visibility; rejected. No token or persistent permission change is necessary.
- Add a signed cross-run draft receipt/artifact bridge: adds a new transfer and
  attestation-selection contract when the established public release already
  provides the reviewed source/run/asset binding. Deferred.
- Remove the Python release prerequisite: loses the reviewed release and asset
  boundary; requiring publication strengthens that existing boundary.

## Validation

Credential-free tests exercise shared core identity, npm wait/failure isolation,
independent graph/run/result boundaries, dispatch/fork/ref/duplicate rejection,
version/source/tag drift, artifact ID/digest/run/expiry and original attestation
invocation refusal, unpublished release/failed or pending finalizer refusal,
source/notes/run/assets binding, original-bundle retry after Python recovery,
and independent Pages
success/refusal. Existing signed-source, finalizer, npm readback, PyPI/Sigstore
and re-exposed publisher execution tests remain required. Action pinning,
workflow lint, full checkout tests, source typing and package build remain gates.

## Rollout and rollback

Keep the focused PR unmerged until release-owner coordination. Before any
future activation, the owner must verify all six existing `@reviewsensei/cli*`
npm Trusted Publishers bind `malsabbagh/review-sensei`, `publish-npm.yml`,
protected environment `npm`, and publication permission. The filename may
already be registered for the retired manual lane; that is not evidence that
all six registrations are correct. Read-only npm trust inspection of the
launcher returned registry HTTP401 on 2026-10-21; the other five were not read
after that authentication hold. Current registrations and the need for an
actual change remain unknown. The runbook contains the six-row exact owner
checklist; a matching existing registration needs no mutation. Any
missing/different identity is an explicit registry migration hold. This task performs no registry or persistent
permission/security mutation. PyPI stays `release.yml`/`pypi`; hosted stays
`release-github.yml`/`pypi-github`.

Retirement of old npm `release.yml` registrations or legacy dispatch authority
requires separately authorized owner review after all original release
recovery is finished. Old immutable workflow sources retain their old authority
until the owner retires it; merging this PR does not revoke it. A pre-release
rollback reverts topology and re-verifies trust for that topology. Recover
already-created releases using their original runs and signed source; never
retag, republish mismatching bytes or weaken provenance to enable a retry.

## Follow-up work

Record owner verification of six npm identities, coordinate the merge window,
and observe one newly authorized release with npm pending and a Python
finalizer-only retry. Keep independent channel receipts and strict v5/PyPI
qualification evidence; no live release test is authorized by this PR.
