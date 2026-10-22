# ADR 0104: Verify published npm consumers before channel completion

- Status: Proposed
- Date: 2026-10-22
- Decision owner / reviewers: ReviewSensei maintainers
- Linked GitHub Issue: No separate issue; user-authorized npm integration follow-up
- Linked PR: Follow-up draft PR pending

## Context

Native build smoke tests and tarball qualification prove the staged release
bundle. They do not prove that a consumer can install the published launcher and
its optional native platform package from the public npm registry. The existing
Linux consumer check in the protected publisher exercises one target, while the
package contract supports five. A successful earlier release or one local
platform receipt cannot qualify another version or the complete target matrix.

Publication is durable before these consumer checks finish. A published package
and a completed npm release channel therefore need separate observable states.
Verification retries must not require publication again or a replacement tag.

## Decision

Keep the native build matrix, all prepublication smoke tests, six-package
tarball attestations, protected publisher and exact registry integrity readback.
Add a separate read-only `verify-published` matrix after `qualify-source` and
`publish-npm`. Use the signed qualifier outputs for the exact release version
and source SHA; do not select a floating npm tag or independently supplied source.

Run on the five supported native targets: `linux-x64-gnu`, `linux-arm64-gnu`,
`darwin-x64`, `darwin-arm64` and `win32-x64`. Each job installs the exact public
launcher with optional dependencies and lifecycle scripts disabled, using
verification-only npm 11.16.0, a fresh prefix/cache and empty user/global
configurations. The publisher's npm 11.5.1 pin remains unchanged. The
installed launcher and native package must be the qualified version. Their
registry integrity, signatures and provenance must identify the expected source.
No checkout engine, build artifact or system Python fallback supplies the CLI.
Unix lanes execute the owned npm bin shim. The Windows lane validates the npm
cmd shim's launcher target, then invokes that installed JavaScript target through
Node without a command shell; the receipt identifies this entrypoint choice.

Execute the actual installed command for version/help, deterministic fixture
evaluation, a blocking review with review exit code 1 and operational exit code
0, and an invalid diff with exit code 2. The CLI subprocess environment uses a
strict allowlist that excludes inherited npm, GitHub and provider credentials
and unrelated configuration. These synthetic checks do not authorize live
provider inference. `npm audit signatures` remains required.

Use stable check names `Verify published npm consumer (TARGET)`. Every target
attempt always runs an artifact upload step for its receipt, command
stdout/stderr, package lock, audit output and produced evaluation/review
artifacts, including on failure, under `npm-consumer-TARGET-RUN-ATTEMPT`.
Missing receipt files fail that upload; an initialization failure before receipt
creation cannot stand in for a successful check. These receipts supplement the
original signed source and publication attestations; they do not replace those
proofs.

The authenticated npm channel result waits for all five latest verification job
conclusions as well as its existing required jobs. Failed, skipped, cancelled,
missing or ambiguous checks leave the channel incomplete even if all six npm
packages are already published. Python publication and major-channel promotion
retain their independent result. Pages retains its existing requirement that
both release channels complete successfully.

The trusted default-branch Pages helper reads the exact signed source's npm
workflow as bounded data and verifies its Git blob identity. A closed
`# reviewsensei:published-npm-consumer-verification:v1` declaration requires all
five latest terminal-success verifier jobs during selection and fresh rechecks.
Historical combined runs, and split sources with neither the consumer job nor
its declaration, retain their original prerequisite set. Unknown, malformed or
duplicate declarations, or an undeclared `verify-published` job, refuse
admission; downloaded workflow content never executes in the Pages job.

## Scope

Future signed releases containing this workflow/helper, npm channel acceptance,
their deterministic tests and maintainer documentation. No package version is
published by this change. No new registry writer, signing credential, protected
environment, trust relationship or tag movement is introduced. Existing
immutable release sources retain their original workflows and evidence.

## Consequences

The release gains five native registry-consumer observations after publication.
Registry availability, runner availability or a real consumer failure can keep
the channel incomplete after publication succeeds. The evidence records which
version, source, target and commands were actually tested.

A verification retry runs the failed verification jobs in the original release
run against the same published version and source. It does not republish, rebuild
different package bytes or mint another tag merely to retry a check. If the
published artifact itself is defective, recovery requires a reviewed source
repair and a higher version through the ordinary release process.

The verifier has Contents read permission and does not enter the npm publishing
environment or receive its write authority. Original publication approval,
integrity and provenance gates remain mandatory.

## Alternatives

- Keep only prepublication native smoke tests: retains bundle checks but leaves
  registry resolution and installed package integration untested on four targets.
- Keep only the publisher's Linux consumer check: provides useful existing
  evidence but cannot qualify every supported native package.
- Use a floating `npx` command or shared cache: cannot establish that each job
  installed the exact qualified public artifact in an isolated consumer prefix.
- Fold verification retries into publication or create a new tag for them:
  couples read-only recovery to registry writes and obscures already-published
  state. The separate job retains immutable publication and honest channel state.

## Validation

Deterministic tests cover the helper's subprocess isolation, exact package/source
checks, command exit semantics, evidence retention and the five required channel
job names. Workflow contract tests retain the original prepublication matrix,
publication protections and result dependencies. Pages regressions cover exact
source policy, historical admission and refusal of each missing or unsuccessful
required verifier. These tests prove orchestration rules; they are not evidence
that a public registry install passed on all five native targets.

The first eligible release must retain actual receipts from all five jobs before
the npm channel is complete. An earlier version's local macOS receipt remains
evidence only for that version and target. This ADR makes no future-version or
complete-platform pass claim.

## Rollout and rollback

Merge and qualify this change before preparing a future signed release. Do not
retrofit old immutable tags, move tags or replace signed bundles. Existing npm
Trusted Publisher setup and protected approval remain separate owner operations.

On a failed verifier, retain its evidence and retry only verification in the
original run. Keep the channel incomplete until all required checks succeed.
Source rollback affects future releases through normal review; it must not mark
an unverified published channel complete or waive existing integrity gates.

## Follow-ups

- Retain the first eligible release's five real consumer receipts and exact
  channel result alongside its original publication evidence.
- Keep owner npm trust setup separate from this read-only verification change.
- Record maintainer review through the normal ADR process. This record remains
  Proposed until explicitly accepted.
