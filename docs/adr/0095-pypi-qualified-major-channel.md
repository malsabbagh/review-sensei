# ADR 0095 - Qualify v5 from verified PyPI publication independently of npm

Status: Proposed
Date: 2026-10-20
Owners/Reviewers: Maintainers
Linked GitHub Issue: User-requested next-release policy; no separate issue
Linked PR: Pending creation

## Context

The hosted workflow installs engine and hosted Python distributions. npm ships
a Node launcher and five frozen native engine bundles; it does not install the
PyPI package. Previously, npm assembly blocked the GitHub draft and engine PyPI
publisher, npm publication blocked the public GitHub release, and major-channel
validation required the entire Release run to succeed. Merely removing the npm
job from a validator would leave those indirect prerequisites intact.

The active engine 0.8.4 / hosted 0.3.4 promotion is a separate operation using
the existing process. This decision applies to the next release.

## Decision

Build the signed-source Python bundle and docs before preparing the verified
GitHub draft. Keep native/npm assembly on its own branch of the workflow graph.
Finalize the exact GitHub draft after successful protected PyPI publication.
Remove npm job requirements from both GitHub release helper phases.

Major-channel admission retains the signed immutable engine source, reviewed
main ancestry, public GitHub release binding, exact four assets, bounded wheel
metadata, and source/ref/workflow-bound GitHub build-attestation verification.
Replace overall Release success with successful latest executions of the
source, Python build, docs, draft, PyPI publication and GitHub finalizer jobs.
Never mask a newer failed execution with an older success.

Independently read both engine and hosted PyPI wheels and sdists. Require exact
inventory, sizes, digests and unyanked metadata. Verify PyPI publication
attestation signatures and transparency through pinned pypi-attestations and
Sigstore tools in a credential-free child process. Bind the signed claims to
repository, workflow, immutable ref, source digest, GitHub-hosted runner,
publication run/attempt and protected environment. Require both signed Python
tags to resolve to the selected source and the hosted wheel to pin the engine.
The GitHub and PyPI engine wheel digests must agree. Pass both verified wheel
hashes into the unchanged four installed-CLI lanes. Recheck signed channel/tag
identities, GitHub binding/assets and both PyPI artifact identities at the end.

npm retains native smoke equivalence, exact tarball validation/attestations,
protected approval, Trusted Publishing, platform-first ordering, registry
integrity readback and original-bundle recovery. Pending/failed npm work remains
visible and never receives a synthetic success. Pages retains its separate
complete-Release policy, which is outside v5 admission.

## Scope

Release workflow dependencies, GitHub release prerequisite sets, major-channel
PyPI verification, pinned validation tools, regression tests and docs. No
version bump, package publication, workflow dispatch, tag movement, Worker
change, secret, ruleset or environment-approval change. Existing immutable
release workflows remain unchanged.

## Consequences

The next Python/workflow release can qualify while npm is pending or failed.
Unavailable registry/provenance services, unsupported artifacts or conflicting
source identity fail closed. Admission downloads four bounded Python artifacts
and provenance documents without executing package code. External Sigstore
trust metadata may be refreshed; verification is time-bounded and diagnostics
never include raw child output. The additional verifier dependencies are
development/validation tools and do not enter engine or hosted package runtime
requirements. npm distribution and released Pages can lag the Python channel.

## Alternatives considered

- Ignore only the npm publisher result: leaves assembly, finalizer and aggregate
  Release success as indirect npm prerequisites.
- Trust successful PyPI jobs or registry filenames alone: does not bind actual
  public bytes to approved source and publishing identity.
- Replace the signed engine/GitHub release contract entirely with registry
  metadata: discards existing provenance and release-binding protections.

## Validation

Regression tests cover npm waiting/failure acceptance, transitive workflow
dependencies, missing/yanked/duplicate/corrupt Python artifacts, invalid
provenance and signed claims, source/pin mismatch, failed/latest publisher
attempts, bounded reads, final rechecks and both wheel digest outputs. Existing
source/signing/GitHub release, npm recovery and downstream tests remain required.
Read-only qualification against existing public Python artifacts exercises the
real cryptographic verifier; it does not authorize or perform a promotion.

## Rollout and rollback

Review and merge this policy PR through normal exact-head CI. Use it with the
next newly prepared release; do not change or duplicate the active 0.8.4 /
0.3.4 promotion. Retain protected package approvals, signed release preparation,
workflow-reader/writer drain, exact old-object lease and downstream validation.
Tag movement and publishing require their own authorization.

Revert before preparing a later release to restore the npm prerequisites.
Recover any partial distribution using its original run and artifacts, without
retagging, deleting published versions or overwriting signed evidence. An
already-promoted channel requires an explicitly authorized signed rollback.

## Follow-up work

Observe the next authorized release with npm pending, retain Python admission
receipts and all CLI/downstream results, and finish npm through its independent
recovery process. Do not infer complete npm or Pages availability from v5.
