# ADR 0069 - Verified draft GitHub Release before package approval

Status: Proposed
Date: 2026-10-09
Owners/Reviewers: Maintainers
Linked GitHub Issue: User-requested release process improvement; no separate issue
Linked PR: Pending creation

## Context

The signed-tag Release workflow already creates a public GitHub Release after
approved npm/PyPI publication. Its single create command cannot reconcile
retries and uses GitHub-generated notes rather than the reviewed changelog.
The owner needs a concrete draft before approving publication.

## Decision

Prepare one draft after successful source qualification, Python, docs and npm
assembly jobs. Bind notes to the receipt-qualified source changelog and assets
to exact bytes from the run's retained Python bundle. Verify GitHub attestations
for the wheel, archive and SBOM against the signed tag ref, source digest and
Release workflow; include the SBOM in the attested checksum subjects.

Retain separate npm/PyPI environment approvals and Trusted Publishing. Publish
the exact draft only after both jobs succeed, checking each prerequisite's
latest execution across retries. Keep the final job name for existing Pages
qualification. GitHub release jobs use Contents write, Actions read and
Attestations read on the workflow token, with no signing key or new App scope.

## Scope

Only future signed-tag Release runs and their four existing GitHub assets.
No version bump, old tag modification, registry approval, channel promotion,
Worker release or credentials/rules change.

## Consequences

The owner sees reviewed notes/assets before package approval. Exact matching
retries resume missing draft uploads and reconcile dropped responses without
clobbering. Changed notes, source/run bindings or asset digests stop. Missing
GitHub asset digest metadata stops rather than trusting names/sizes. Retained
artifacts are needed for recovery. Rebuilt different bytes cannot replace a
partially uploaded draft, and human edits must be assessed separately.

Per-tag serialization and fresh tag/job/release reads narrow concurrency races;
GitHub offers no atomic transaction with external administrator writes. Failed
package publication leaves a draft, so Pages still requires full Release
success and a public stable release.

## Alternatives considered

Keep one public create command: no pre-approval preview or retry recovery.
Publish on tag push before registries: falsely advertises incomplete releases.
Overwrite an existing release on retry: risks replacing human edits/foreign
assets. Create a new App: unnecessary for existing workflow-token authority.

## Validation

Offline regression tests cover source/run/job/artifact binding, attestation
arguments, unsafe/corrupt bundles, draft/public transitions, failed prerequisite
jobs, retry attempt selection, dropped responses, foreign content, partial
uploads and non-mutating published retries. Full repository validation and
independent public-diff review qualify the PR; no live release is created by
tests or this PR.

## Rollout and rollback

Merge only after review and exact-head CI. The next newly prepared signed tag
uses this workflow. Existing immutable tags keep their original workflow and
receive no automatic backfill. Revert before a new tag to restore the old path;
for a retained draft, recover its original run/assets with owner assessment
rather than moving tags or overwriting content.

## Follow-up work

Observe the first newly authorized release's draft and approval transitions.
Keep npm/PyPI recovery procedures separate and monitor artifact retention.
