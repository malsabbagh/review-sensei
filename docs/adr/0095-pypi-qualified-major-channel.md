# ADR 0095 - Qualify v5 from verified PyPI publication independently of npm

Status: Proposed
Date: 2026-10-20
Owners/Reviewers: Maintainers
Linked GitHub Issue: User-requested next-release policy; no separate issue
Linked PR: [#330](https://github.com/malsabbagh/review-sensei/pull/330)

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

### Re-exposed publisher execution amendment

GitHub can assign a successful ancestor job a new job ID, record creation time
and `run_attempt` during a finalizer-only retry, preserving its original runner,
execution timestamps and complete step inventory. The current run attempt is
not necessarily the publisher's OIDC invocation attempt. The validator compares
authenticated GitHub API job facts, independently of certificate contents, to
recover that original execution and job ID before constructing the verification
plan. The signed invocation claim is still checked exactly against that plan.

A successful record created after its execution completed needs one unique
earlier original record created before or at execution start. Both records bind
the same run, source, ref, workflow/job names, execution start/end, runner ID,
runner name, runner group, labels and complete numbered step names, conclusions
and timestamps. Observed GitHub-hosted default group IDs `0` and explicit `null`
are equivalent with the exact `GitHub Actions` group name; the remaining runner
facts still match. Missing fields, malformed times or steps, duplicate IDs,
multiple origins and unmatched aliases block qualification. A genuinely new
successful execution keeps its new attempt. A later failed, incomplete or
distinct execution blocks an older success even if GitHub later re-exposes that
success. The existing latest-job prerequisite and fresh-run recheck remain.

This comparison does not trust raw certificate claims, weaken Sigstore policy,
alter artifact/ref/environment bindings or authorize any retry or publication.

Independently read both engine and hosted PyPI wheels and sdists. Require exact
inventory, sizes, digests and unyanked metadata. Verify PyPI publication
attestation signatures and transparency through pinned pypi-attestations and
Sigstore tools in a credential-free child process. Bind the signed claims to
repository, workflow, immutable ref, source digest, GitHub-hosted runner,
publication run/attempt and protected environment. Require both signed Python
tags to bind their own publication sources and the hosted wheel to pin the engine.
The hosted source equals the selected workflow source. A reused older engine
source requires the recognized core build-input equivalence proof in
[ADR 0105](0105-hosted-only-python-release-sources.md); same-source releases
preserve the original path.
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

The execution amendment has public `publication_run` regression fixtures from
the captured engine 0.8.6 run, including actual build re-exposure across
attempts 1 and 2 and publisher re-exposure from attempt 2 under attempt 3.
Tests cover the actual publisher's attempt 2, synthetic new attempt 3 success
and failure, changed runner/timestamp/step facts, missing or ambiguous origins,
intervening executions and an independent `verify_plan` invocation-claim
acceptance/refusal pair. Signature verification stays in the existing verifier;
the policy fixture does not claim a new live retry or cryptographic receipt.

## Rollout and rollback

Review and merge this policy PR through normal exact-head CI. Use it with the
next newly prepared release; do not change or duplicate the active 0.8.4 /
0.3.4 promotion. Retain protected package approvals, signed release preparation,
workflow-reader/writer drain, exact old-object lease and downstream validation.
Tag movement and publishing require their own authorization.

The later execution amendment can ship as a normally reviewed validator repair
on protected main without changing an immutable released package. The major
gate checks out protected default-branch validation code and records
`validator_sha`; installed lanes and final recheck use that exact validator
commit. Its eleven released source-data inputs exclude the verifier scripts.
Keep that validator identity distinct from immutable release `source_sha`,
signed tag objects and package/provenance digests. Publication/finalizer success,
all provenance checks and the four installed lanes still precede channel
acceptance. This local candidate is not a deployed validator or a promotion.

Revert before preparing a later release to restore the npm prerequisites.
Recover any partial distribution using its original run and artifacts, without
retagging, deleting published versions or overwriting signed evidence. An
already-promoted channel requires an explicitly authorized signed rollback.

Reverting the execution amendment restores conservative refusal when a reused
job record disagrees with the original attested invocation. It does not justify
republishing, moving immutable tags, guessing an attempt or waiving a failed
finalizer. Missing execution evidence remains an explicit qualification hold.

## Follow-up work

Observe the next authorized release with npm pending, retain Python admission
receipts and all CLI/downstream results, and finish npm through its independent
recovery process. Do not infer complete npm or Pages availability from v5.
