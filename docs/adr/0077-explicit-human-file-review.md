# ADR 0077 - Explicit human review of unsupported binary changes

Status: Proposed
Date: 2026-10-10
Owners/Reviewers: Maintainers
Approved by: not applicable
Related issue: [#238](https://github.com/malsabbagh/review-sensei/issues/238) provides coverage/authority context; this separately user-authorized local feature does not close it. No new issue created.
Related PR: not created; local implementation only. [#257](https://github.com/malsabbagh/review-sensei/pull/257), [#265](https://github.com/malsabbagh/review-sensei/pull/265) and [#266](https://github.com/malsabbagh/review-sensei/pull/266) are source dependencies, not activation approval.

This record is ADR 0077. ADR 0076 is the PR253 gap-closure contract and is not restated here. Recording a human file confirmation is not approval.

## Context

An AI cannot assess binary media in a text diff. Treating absence of findings as
review would lose coverage. A maintainer may nevertheless have reviewed the
exact media and needs to record explicit selected-file confirmation without
clearing text findings or implying an AI review.

## Decision

Recording is not approval. A receipt is audit metadata for selected binary files. It does not complete the AI result, baseline, or cache, and it does not publish GitHub APPROVE.

Add bounded closed request/receipt reference documents and an opt-in GitHub
library adapter. Trusted host policy defaults to report-only. Confirmation uses
a whole explicit `media-reviewed` command containing the full request digest
and full selected file IDs. Live numeric human identity, source location,
association and collaborator permission are mandatory. Owned request/receipt
readback and the latest owned review bind the exact validated result and complete
changed-file inventory. Immutable Git trees bind old/new regular blobs, modes
and rename paths; no media bytes enter a model.

Do not carry confirmation across base/head/result/review changes, including
unchanged blobs. Edited/deleted/revoked sources lose current coverage while
owned receipts remain audit history. Serial replay is idempotent; ambiguous
writes have no automatic retry. Missing/corrupt evidence or concurrent duplicate
source receipts refuse evaluation.

Preserve AI coverage and the partial result. Introduce optional host-derived
`coverage_only_partial` provenance only after complete reviewable text analysis
with exclusively binary remaining obligations. Absent provenance preserves the
historic content digest; present provenance uses an explicit human-files-v1
projection and the complete publication result digest. A fresh separate
assessment may satisfy only this coverage/status blocker after all selected
binary obligations are confirmed; every other frozen/current fact still gates
approval. No existing marker, cache/baseline, passing check or APPROVE is written.

## Scope

Regular unsupported binary blobs only, at most 32 changes; added, modified,
removed and renamed changes are supported. Canonical literal Unicode/glob paths
retain equality. Text, symlinks, submodules, arbitrary policy exclusions and
unknown coverage remain outside this exemption. Issue-comment commands only.
Trusted configuration is the library policy object; no repository YAML flag,
CLI command, mention workflow, v4 writer or broker capability is enabled.

## Consequences

Humans can record exact selected-file review without model inference. Existing
partial readers continue to withhold; a new host must deliberately compose the
fresh assessment. No automatic carry is conservative and may require humans to
repeat review after unrelated changes. Current permission/source checks can
invalidate an older receipt. Every GET/POST shares the caller's original IO
guard; finite response/page/body limits can refuse large histories. GitHub has
no remote atomic compare-and-swap; this feature does not claim one.

## Alternatives considered

A broad `ignore` pattern, model-inferred waiver, any-maintainer prose, extension
allowlist, or head-independent blob confirmation would hide context or allow
ambient authority. Marking the AI result complete would confuse human and AI
coverage and reopen baseline/cache/closure paths. A new default hosted command
would require an independently reviewed broker/finalizer/resource composition.

## Validation

Public-domain and GitHub-adapter tests cover selected/all confirmations,
permissions/numeric ownership, body/source edits and deletion, old/new blobs and
modes, rename/delete/new-file inventory, unchanged-blob head/base invalidation,
latest malformed roots, audit replay/lost POST response, missing evidence,
text/context/qualification blockers and original count/deadline exhaustion.
Service tests verify host provenance across legacy, chunked and unified review,
failed/budgeted text and model extra-field attempts. Public schemas, source
static gates, full clean suites and clean installed wheel are required. Tests
are credential-free; actual provider/deployed hosted qualification is separate.

## Rollout and rollback

Keep the default policy false and public routing unchanged. Review the local
patch, schemas and digest compatibility before merge. Publish compatible readers
and qualify live hosted authority before enabling any writer or approval path.
Rollback by leaving confirmations disabled; original partial coverage continues
to withhold and historical metadata receipts remain readable audit reference.
No remote cleanup, release, channel move or approval is performed here.

## Follow-up work

Public APPROVE integration is still pending. `evaluate_mixed_coverage` exports
the closed host-derived decision a later finalizer can call. This change does
not edit the shared publication or approval finalizers and does not post APPROVE.

Review the no-carry product policy, hosted command and scoped broker/finalizer
composition, concurrent receipt reconciliation, actual shared dispatch/deadline
profiles, deployment reader inventory and end-to-end publication. The original
attempt/bootstrap/complete-evidence Epic238 blockers are not solved by a human
file receipt.
