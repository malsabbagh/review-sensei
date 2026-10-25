# ADR 0112: Read retained published inventory during explicit rescan admission

- Status: Proposed
- Date: 2026-10-25
- Decision owner/reviewers: repository owner; Astra High
- Related issue: [TaskAlong PR29](https://github.com/malsabbagh/taskalong/pull/29)
- Pull request: pending source review

## Context

After an aborted continuation, the ledger can retain its published source in a
consumed receipt while releasing the current transaction and reservation. The
core rescan selector accepts that source. The hosted default inventory reader
requires a current transaction, so a valid explicit full rescan fails before new
analysis with `explicit rescan published coverage is unverifiable`.

The frozen remaining-only repair does not cover full rescan admission. This fix
is isolated from the repository API-work redesign and changes no allowances.

## Decision

Extract a read-only retained published-source reference. Bind the unchanged
authenticated ledger root, the exact latest receipt applicable to the head, its
published transaction and source review ID. Require no current transaction or
reservation. Context construction and every inventory authority fence freshly
reselect the same reference from the ledger.

Only the authenticated explicit-rescan resolver opts the reader into this path.
Both full and remaining intent keep their existing permission, event, current PR,
publication and incomplete-coverage gates. Inventory scope still binds repository,
PR, base/head, result, policy, configuration, evidence and numeric Bot producer.
The receipt's source review must occur among the independently verified original
publication roots. Existing complete explanation validation remains in force.

Retained inventory contexts cannot stage writes, including reconciliation through
the staging API. An ordinary reader cannot attach their authenticated store.
No transaction is restored, no receipt is edited and no counter is reset.

## Scope

Hosted source admission and default inventory authority only. Existing ordinary
disposition/finalization readers retain their current-transaction requirement.
No provider, broker, budget, retry, release, deployment or live TaskAlong operation.

## Consequences

A legitimate full-rescan event can authenticate the retained source instead of
failing on a missing current transaction. It still cannot claim coverage, ignore
human obligations or establish approval. Later analysis/publication gates may
refuse independently.

## Alternatives considered

- Cherry-pick the remaining-only fix: full rescan would still not opt in.
- Restore an old transaction: rejected; historical evidence is not live authority.
- Let every reader fall back to receipts: rejected; ordinary decision and
  finalization authority must not expand.
- Ignore the reference or assume its hash authenticates scope: rejected; retain
  independent root, transaction, producer, parts and explanation validation.

## Validation

The regression drives the actual full-rescan resolver and default reader against
a generation16 retained-only checkpoint with20 unchanged synthetic dispositions.
A valid schema5 reference to already staged exact inventory makes the reader
mandatory on all platforms. Negative cases cover root changes, receipt/review
identity changes, stale digest, wrong producer, missing parts, active reservation,
latest-per-head selection, ordinary reader refusal and retained write refusal.
The installed-distribution lane includes the same regression.

Source, installed package, CI and independent review results must be recorded
before publication.

## Accounting-path preservation

The retained reference supplies source selection only. Reads still pass through
the existing publication HTTP and session-ledger wrappers with the original
`EvidenceReadBudget`, including fresh authority fences and cached-read checks.
Authentication adds no grant, refund or replacement budget. The generation,
dispositions, receipts, failed-attempt counts and completed-round counts stay
unchanged during admission. The ordinary downstream admission, consumption and
publication paths still account for any newly authorized analysis.

The later API-work redesign can retain this selector and route these same read
interfaces through its authenticated cumulative counter. It must cover retained
admission in combined resolver/reader/accounting tests, debit every actual read
and preserve refusal when capacity is exhausted. The retained reference must
never become owner, reservation or write authority, or a path around accounting.

## Rollout and rollback

Review the isolated patch before any PR/release operation. Package/channel
qualification and publication remain with the release owner. No live TaskAlong
retry is authorized by this source patch. Roll back the source normally if needed;
historical receipts and decisions are preserved.

## Follow-up work

After source review and qualification, coordinate release through the single
publisher. Reassess the live repository only under separately authorized recovery
steps. The repository-knob redesign remains on its independent branch.
