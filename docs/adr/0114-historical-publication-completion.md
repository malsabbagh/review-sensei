# ADR 0114: Historical publication completion and current run eligibility

- Status: Proposed
- Date: 2026-10-27
- Decision owner/reviewers: repository owner; Astra High
- Scope: independently reviewed protocol correction; no live recovery activation

## Context

A durable `publication_succeeded` transaction can lack its caller's terminal
binding when a superseded-thread cleanup was unconfirmed. The phase alone must
not close that caller. Releasing corrected code can also advance the PR base;
requiring that historical publication to adopt the new base cannot authenticate
its original evidence. Historical completion and current eligibility are distinct.

## Decision

Keep ordinary rollover strict. Add a distinct transition for a new authenticated
review run, attempt one, whose current base/head differs from an accounting-bearing
historical predecessor's publication. This does not enroll accounting-free legacy
roots, pending provider work, active reservations or operator-paused sessions.

Fresh policy commit P/configuration, current PR identity/base/head and exclusive
Actions owner govern the new run. The original root, transaction, policy/config
digests, base/head and retained result identify historical evidence only. Read the
old hash-selected policy blob without executing old code. Support the canonical
merge-focused historical policy; unsupported formatting and additional media or
explicit-rescan tails refuse.

Historical inventory access is an explicitly process-issued read-only selection.
It validates the exact original root snapshot while independently checking the
fresh current PR; it never substitutes a historical response for a live response.
Default live inventory guards remain unchanged. Historical stores reject writes.
Reconstruct finding prose from the immutable admitted index and placement; current
branch rules do not rewrite historical placement or confer current eligibility.

Authenticate complete native finding/index/prose, existing historical check and
any required approval, complete original cleanup scope, resolved effects, stopped
predecessor, current owner and stable closing observations. Every possible original
target must remain present and unchanged. An unresolved superseded thread refuses;
the authenticator never resolves it, dismisses its replacement or publishes.

Job cessation alone cannot settle an earlier uncertain root write. The initial
qualified executor is the signed v5 object `86017489651cfc6ef7dd493bef255e1edebbe556`,
peeled source `52b5ca47ba3508a526312ce8424ff0c7fa1bb347`, with the exact recognized
workflow and core 0.8.14/hosted 0.3.20 installation contract. Require the latest
original run to remain attempt one, completed successfully at the historical head,
with its owning review step successful, mention reply skipped and other possible
writer jobs skipped. Bind the complete writer inventory and source/step identity
in the receipt. That source propagates unresolved root-write errors to a failed
CLI/wrapper exit. Its post-review artifact step has no ledger capability. Failed,
cancelled, rerun, unknown-source or conflicting histories therefore refuse. This
does not infer non-entry from a missing marker or declare arbitrary requests settled.

Perform authentication inside the existing serialized native mutation after its
latest-root check. Only the resulting in-process completion can produce the closed
`historical-publication-completion-v1` receipt and `caller-run-reference-v2` pointer.
Archive the original predecessor record unchanged in `caller-run-root-v2`, including
unknown charges and an absent terminal marker; confirm complete archive/receipt
readback before conditional root replacement. Recheck old/current Actions and
current PR after archival work, then require native root replacement/readback.
The receipt binds both historical and current identities and verifier version.
Serialized receipts support exact history reading and reconciliation, never a
different enrollment or restored execution allowance.

All requests consume the existing physical caller meter. The new-run startup slot
remains 77 requests; its combined startup liability remains 130 and repository
default remains 512. Protect the existing 34-request archive confirmation tail plus
five post-archive authority checks before the first archive write. Refuse an
unfunded transition before mutation. Lost acknowledgements use exact owned readback
without replay; retained and abandoned capacity stays charged.

## Crash states

After confirmed enrollment but before a new reservation/transaction, the root may
retain the historical transaction. An exact authenticated v2 pointer/receipt and
archive identify this as inherited evidence, not a publication by the new caller.
The one supported same-run successor may start fresh current analysis only from
that narrow state after cessation/current-fence checks, preserving all first-owner
blocks. A new reservation or analysis transaction removes this permission. A hard
kill after transaction attachment or around provider entry cannot establish
non-entry and refuses; retained completed-result recovery keeps its existing path.

## Eligibility and rollout

After confirmed new enrollment, ordinary analysis uses the current base/head and
policy. Historical approval and coverage do not approve that revision. Preserve
decisions, findings, convergence history and human obligations through normal
reassessment; no empty-current-result shortcut clears them.

This correction can be reviewed and released independently from narrative-rendering
changes. Normal CI, native review, package/platform qualification and sole-publisher
release precede any live invocation. A later recovery must reconcile original
effects, authorize exact remaining cleanup, confirm its readback and invoke a new
current run under the released protocol. No old-workflow rerun, workflow-history
alteration, manual terminal marker, credential change or bypass is introduced.

## Validation

Use the public package entry with synthetic external transports. Cover changed
base/head/policy, exact archive/charges, one fresh provider entry after root
confirmation, withholding human obligations, lost archive/root acknowledgements,
foreign or altered evidence, missing effects, changed closing observations,
overlapping owners, funded refusal and process death at each durable boundary.
Include an uncertain original terminal-marker write, failed/cancelled owners,
masked step failures and unknown executor provenance. Qualify the recognized old
source with synthetic stored/unstored response-loss faults at every root-write
position; retain run-bound installation and complete writer-inventory evidence.
