# ADR 0093: Bounded default publication and transaction-bound retention

- Status: Proposed
- Date: 2026-10-19
- Decision owner / reviewers: ReviewSensei maintainers
- Linked GitHub Issue: [#238](https://github.com/malsabbagh/review-sensei/issues/238), [#246](https://github.com/malsabbagh/review-sensei/issues/246), [#267](https://github.com/malsabbagh/review-sensei/issues/267)
- Linked PR: pending; repairs the default publication failure observed on [#316](https://github.com/malsabbagh/review-sensei/pull/316)

## Context

The deployed default publisher refuses a valid 77-record review at the v1 index
limit of 64. Analysis succeeded, but the diagnostics opt-out and digest-only
session baseline left no complete result to recover. Separate explicit partitioned
hosts do not change this default writer. Merely raising the count also risks
prose loss, incompatible readers and approval based on an incomplete inventory.

## Decision

Keep the existing v1 default review format and independently bounded reader.
Readers accept at most 250 entries with unchanged identity/flag/duplicate
validation. Writers remain limited to 64 by default. Trusted
`github.publication_inventory: complete` activates larger emission after the
reader-first rollout. The complete profile preflights and preserves every finding
identity and full explanation or refuses before review/check mutation. Existing
body, individual-item, future-human-assessment, path and time/read bounds stay.
Exact retry reconciliation verifies authenticated full retained prose/placement.

Reuse ADR 0076 D-A's existing App-owned PR result-part storage for the final
transaction-bound canonical ReviewResult, before review/check publication.
Retention follows `github.operation_entry`, remains separate from diagnostics,
and stores no raw diff, prompt, credential or caller settings. Recovery requires
complete strict-owned parts, current PR/base/head and repository identity, a
matching independent durable transaction, and caller-supplied trusted contexts.
The accepted-operation guard still compares accepted bytes. Independently
validated checkpoint normalization removes only the transaction envelope; a
content change or incompatible accepted operation refuses.

Transaction-bound publication identity includes the logical transaction ID and
content digest, excluding mutable phase and generation. Unbound legacy identity
is unchanged. A complete writer cannot silently adopt an old transaction-hash
root or an identity-only legacy root with omitted prose.

One publication-attempt budget covers acceptance, retention, ledger reads and
publication. Before a potentially unconfirmed first retention write the durable
transaction enters the existing failed/unconfirmed publication phase. A fresh
owner may reconcile a complete retained set but cannot recreate missing parts
blindly. This conservatively holds ambiguous cases. It does not provide an atomic
multi-writer GitHub-comment CAS or promise distributed exclusivity.

## Scope

Default GitHub ReviewPublisher, GitHubApplication, reusable hosted and local
workflow publication lanes, installed `github review` CLI, strict configuration,
and existing transaction/result-part recovery. No new hosted service, broker
permission, provider inference, DecisionProvider or architecture-governance policy.

## Consequences

Compact 65/77/100/250 inventories can publish without losing prose after
activation. Rich inventories exceeding existing independent limits retain their
validated result and fail with an actionable capacity message. No arbitrary
250-finding capacity is claimed. Valid retained results recover without inference;
missing/corrupt/foreign/stale parts and uncertain missing writes remain held.
Existing `operation_entry: disabled` and writes-disabled callers preserve opt-out.
Old failed runs without full result parts cannot be reconstructed from a baseline.

## Alternatives considered

Blindly raising the count permits silent optional-prose loss. Truncating findings
breaks completeness and identity. Making the separate partitioned v4 host default
requires additional authority/rollout guarantees and expands this repair. Enabling
diagnostics globally changes source retention policy. Repeating inference loses
the exact accepted result and consumes another allowance. New per-part durable
intent receipts could improve ambiguous-write liveness, but require a broader
persistence contract and are deferred.

## Validation

Public default publisher tests cover gated compact inventories, codec corruption,
full individual prose, oversize refusal before mutation and exact full-body/inline
replay. Public application tests use a serialized ledger, final canonical result
retention, failure and fresh-owner recovery, current identity/context rejection,
strict ownership/association, unknown writes, shared dispatch/deadline exhaustion,
opt-outs and crash fences. CLI tests verify configuration forwarding, unchanged
accepted-operation enforcement and recovery without provider construction.
Installed wheel and extracted-sdist validation qualifies the supported matrix;
component or synthetic tests do not establish deployed provider qualification.

## Rollout and rollback

See [the runbook](../validation/default-publication-recovery.md). Release updated
readers and install them for every publication, command, reassessment, delayed
verify and recovery consumer before activating larger emission. Keep repository
configuration on `legacy` until exact installed compatibility is verified and the
maintainer authorizes activation. Package release, v5 promotion and deployment
are separate approved operations. Roll back emission to `legacy` while retaining
expanded readers for already-published reviews; do not downgrade readers after
large inventories exist. `operation_entry: disabled` remains the existing
retention rollback and does not authorize inference or bypass validation.

## Follow-up work

Distributed conditional comment storage, richer partitioned-host rollout and
provider/configuration qualification remain separately tracked. The lost full
result from PR #316 at f59edcc cannot be recovered by this patch. Any future
provider run needs separate authorization after the repaired installed path is
verified. ADR status remains Proposed pending maintainer review.
