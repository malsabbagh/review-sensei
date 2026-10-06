# ADR 0065 - Complete human inventory and retryable approval

Status: Proposed
Date: 2026-10-06
Owners/Reviewers: Maintainers
Approved by: not applicable
GitHub Issue: follow-up to the human reassessment state demonstrated on PR #216

## Context

Lifecycle concern fingerprints omit prose and line locations so a concern can
survive movement across review rounds. Distinct human findings can share one
fingerprint. ADR 0061's unique-inventory validator correctly rejects duplicates,
but construction swallowed that rejection and omitted the complete inventory.
The hosted reply path then generated ordinary chat, which could not change the
persisted human-review flag. A separate interruption after durable reassessment
but before approval also left no pending findings to trigger another assessment.

PR #216's published review has the human flag without an inventory. Duplicate
short finding identifiers suggest the collision path, but its raw provider
artifact was not retained. Synthetic regression evidence establishes the code
path; it does not reconstruct that historical artifact.

## Decision

Keep the existing lifecycle identity for unambiguous human concerns. For each
collision group, hash a domain-separated canonical complete validated comment
with its concern fingerprint to produce a separate assessment identity. Provider
ordering does not change identities. Only identical validated comments coalesce;
distinct text or locations remain independently assessable. Persisted inventories
and provider decisions still reject duplicate, unknown and conflicting identities.
Existing inventories keep their original identities and need no migration.

The complete inventory must satisfy the existing limits: 20 distinct findings,
2 KiB per finding, 24 KiB total and an exact base SHA. Construction failures are
explicit, and the original review publisher validates before any check/review
mutation. No truncation or silently omitted inventory represents a complete
human review. Repeated initial publication also carries the exact base into its
eligibility construction.

An authenticated mention with a latest App review declaring pending human
findings but no inventory fails with an actionable request to rerun a full review
for the current head. Malformed or contradictory latest records also fail
explicitly; they cannot reveal an older clean record. Preparation rejects stale
base/head, changed or unauthorized source, and missing current diff rather than
falling through to ordinary chat. Legacy documents remain readable by other
approval paths and continue withholding.

No available hosted artifact binds the full legacy finding set and original base
to its result digest. Review-body prose, truncated RS IDs, inference responses
and `resolved:true` chat bits are insufficient authority. This implementation
does not infer or repair legacy inventory from them. A fresh review is the safe
recovery path; direct live-ledger edits are outside this change.

Persisting a supported assessment still changes only the human flag and inventory
resolutions. On a retry, an exact persisted refresh can re-enter the existing
finalizer. A newly prepared mention with a fully resolved inventory skips model
inference and retries finalization directly. Both paths require the existing
separate review capability, current source authorization, exact base/head and
latest persisted eligibility. Finalization checks all existing static facts and
live threads, rechecks PR identity and persisted state, and reconciles the same
approval marker. A newer same-head result or stale base/head wins. Retry creates
neither another reassessment COMMENT nor a second approval in sequential replay.
The source reply is reconciled through the existing conversation publisher
before finalization, so retries reuse the reply marker and new mentions receive
an acknowledgment rather than a false `already_replied` result.

## Consequences and limits

New human reviews remain reassessable even when lifecycle identities collide.
Inventory validation failures prevent publication and require a bounded complete
review. Legacy missing state remains pending until a fresh review establishes a
complete inventory. Successful reassessment and approval converge across runner
interruptions without another model decision.

No permissions, provider selection, evidence checks, coverage policy, security
policy, budgets, session baselines, publication fences or release configuration
change. Literal excerpts still establish provenance; semantic reassessment still
depends on the configured model. GitHub has no atomic compare-and-publish API;
freshness checks and marker reconciliation preserve the existing race limits.

## Validation and rollout

Synthetic tests cover collision identity, bounds, actual original review
publication through authenticated reply/reassessment/one approval, partial
collision decisions, malformed/contradictory/legacy authority, capability
separation, interruption/replay and stale/newer-result races. Existing approval,
coverage, qualification, blocking-thread and authorization tests remain required.

This is a source change only. Package publication, released workflow/v5 promotion,
deployment and live verification require separate authorization. After that
rollout, legacy missing-inventory cases require a fresh exact-head full review
before reassessment. Rollback withholds on colliding or missing inventories.
