# ADR 0103: Bounded hosted media confirmation

- Status: Proposed
- Date: 2026-10-22
- Decision owner / reviewers: ReviewSensei maintainers
- Linked GitHub Issue: No separate issue; user-authorized media command defect repair
- Linked PR: [#343](https://github.com/malsabbagh/review-sensei/pull/343)

## Context

The supported maintainer media sentence reaches the hosted application without
the current retained result, publisher or owned request identity. PR #342 makes
that refusal an honest command failure. Passing the missing values alone is
insufficient: repeated preparation exceeds the original dispatch allowance even
for two files, and the v1 document admits at most 32 changes. A reported target
has 41 media changes among 102 changed files and two retained result parts.

## Decision

Compose the command after broker reconstruction of its live source and actor.
Load the canonical owned session transaction and retained result, preserving the
separate serialized-payload, content and publication digests. Require the exact
current base/head, numeric repository/App identity, latest owned review, source
comment and current maintainer permission before creating a request.

Keep v1 requests, receipts, digests and 32-file limits unchanged. A new bounded
group document contains ordered v1 request parts with one identical snapshot,
review and result. Its complete union must equal the result's human-confirmable
inventory, without duplicate paths or file IDs. Each part retains the 32-file
cap; the group also retains the existing 32 KiB canonical document limit.
Grouped receipts bind one genuine source event and the exact part/root digests.
The blanket sentence selects the full authenticated group. Named selections
retain their existing maximum of 32 literal IDs.

Use one publisher and one original budget throughout each invocation. Reuse
only exact-commit inventory inside that invocation. Recheck mutable request,
receipt, source, permission, latest review and full PR state before approval.
The host supplies fresh thread, draft/fork/App-author and provider qualification
facts. Resolved inline findings require the authenticated exact-result finding
index and positive current thread-resolution evidence; a frozen false flag
alone cannot resolve a finding. Keep the complete raw result and its digest.

Broker-internal dispatches retain prepaid accounting and bounded server
enforcement. The ordinary command profile admits ten internal requests; the
following check capability admits five. Colder caches, fallback or larger
history may refuse before an unfunded dispatch. No unused liability is refunded.
The final check uses only the four already reserved finalization calls, scoped
to its exact repository/head and check routes. All calls retain the original
64-total/60-ordinary allowance and 60-second deadline.

Unknown writes are not automatically retried. A later explicit invocation of
the original supported command reconciles owned requests, receipts and reviews,
then performs fresh authorization and finalization. A missing or invalid
assessment, exhausted budget or unfinished check remains a command failure.

## Scope

The existing hosted issue-comment command, canonical result reader, human-file
adapter and final check. No model call, new credential, broker permission,
publisher, release lane or cross-head carry. Partitioned v4 approval roots
remain unsupported; incomplete or conflicting authority refuses.

## Consequences

The command can record an existing human assessment without repeating visual
analysis. AI coverage and result status remain partial. Receipts remain audit
metadata and cannot replace technical findings, human adjudication, provider
qualification or current thread evidence. Large histories, deep trees,
oversized documents and depleted deadlines can still require an explicit retry
or operator evidence repair; support is bounded rather than unlimited.

## Alternatives considered

1. Wire three arguments into the old composition. Rejected because its repeated
   mutable/immutable reads exceed the original allowance.
2. Increase request counts, timeouts or the v1 file cap. Rejected because it
   changes existing resource and compatibility contracts.
3. Separate unbound v1 requests. Rejected because each v1 request requires the
   whole inventory and one source cannot be rebound to unrelated requests.
4. Publish a manual approval or passing status. Rejected because it loses the
   owned result, source and file provenance.

## Validation

Credential-free tests exercise canonical retained records, the actual supported
CLI/application/publisher/check path, grouped 41-file inventories, 102-file API
pagination and two retained parts. Synthetic production broker qualification
counts actual TypeScript internal dispatches and signed OIDC exchanges. Failure
tests cover source/head/base changes, permissions, tampering, stale roots,
drafts/forks, missing qualification, unknown writes and explicit replay.
Source, clean-package, exact-head review and CI receipts are separate gates.

## Rollout and rollback

Merge only after exact-head validation/review. Coordinate package, Worker and
v5 activation through the existing release owner and Release Marshal. Reader
schemas ship with the writer; old v1 bytes remain readable. Roll back to the
prior qualified release to disable hosted composition. Retain grouped requests
and receipts for audit; do not delete or rewrite them or claim they are AI
coverage. A production command is reverified separately from source tests.

## Follow-up work

Retain exact release and deployed-command receipts. Broader inventory/history
capacity, partitioned v4 roots and stronger remote concurrency primitives need
separate bounded designs. This repair does not create atomic GitHub CAS.
