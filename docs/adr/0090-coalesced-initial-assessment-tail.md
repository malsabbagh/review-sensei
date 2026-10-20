# ADR 0090 - Coalesce original reservation and assessment admission

Status: Proposed
Date: 2026-10-19
GitHub Issues: #240, #242, #243, #244, #245, #269
Pull Request: pending
Owners/Reviewers: Maintainers
Approved by: not applicable

## Context and red oracle

The current GitHub ledger separately consumes a fresh grant and writes an
original reservation, then consumes another fresh grant and writes complete
assessment admission/dispatch liability. These two roots precede the first
provider dispatch. The existing hosted-tail fixture measures 25 caller dispatches
for reserve plus two checkpoints (19 GitHub HTTP and six broker HTTP). The
production two-source broker additionally performs 13 internal requests on cold
issuance and one on verification. Those component segments already exceed the
original allowance when composed naively, before context, inference or report
publication. This is an engineering gap, not an external permission gate.

The public `InitialAssessmentTailTests` oracle starts a genuine `OperationHost`,
admits and prepays the original full finite liability, restores its real handle,
and calls the currently missing `GitHubIssueCommentSessionLedger.admit_with_tail`.
It requires exactly one consumed grant and one owned root mutation containing
both reservation and the complete supplied checkpoint candidate. This storage
oracle is separate from complete C journal semantics, source fencing and CLI
qualification; those require the composed acceptance below.

## Decision and interface

Add an opt-in `admit_with_tail` to the existing GitHub ledger. It accepts an
original handle, slot, exact expected generation, complete operation binding and
candidate preparation callback. The callback receives an uncommitted reservation
candidate and adds the complete admission/dispatch journal without incrementing
generation again. One fresh consuming grant is bound to the actual old root;
one sealed existing activation tail writes reservation plus complete checkpoint
and reads that owned root back before any provider dispatch.

The original handle binds the same operation, reservation, original deadline
and already admitted liability. Raw mappings, fresh allowances, changed scope,
held reservations, previous committed reservations and stale generations refuse.
Existing source/PR/base/head/latest-inventory fences remain the caller's required
composition before mutation. The existing adapter's final live head fence,
current root comparisons, exact consuming grant, full retention validation,
part ownership and complete readback remain mandatory. No cached mutable
observation supplies a later mutation's authority.

Uncertain consumption, preparation failure, interference, expiry or ambiguous
commit burns the original liability and keeps inference unauthorized. A lost
response is inspected through owned readback; it never causes a second grant
consumption or a new allowance. The initial path completes its pre-admitted owned
scan after a lost/partial PATCH response; an exact complete root confirms the
original write without another PATCH or grant. Missing, conflicting or partial
whole-root readback, including the exact consumed-grant marker, refuses when missing
and keeps inference unauthorized. `replace_with_tail`
keeps its original behavior.
The Worker remains stateless, no source-grant scope changes and no second store
are introduced. No governance #270 or DecisionProvider #161 choice is changed.

## Validation and consequences

Required green oracles include actual retained complete C admission before a
provider dispatch, one consumed grant/root write, exact physical accounting,
wrong/missing handle/deadline/operation, stale/competing root, source/base/head
change, refused consume, interruption, lost/partial readback and expired original
allowance. Existing tail, witness, queue and replay counter/time suites remain
required. Production broker cost savings must be measured with the real bridge;
subtracting a fake broker call is not sufficient evidence of 40-item CLI capacity.

The explicit `tests.epic238_initial_tail_qualification` compares independently
funded first-checkpoint segments with actual Python client, production TypeScript
broker and GitHub comment adapter. Complete one-source metadata remains exact.
Separate reservation/checkpoint uses 42 physical calls, 43 prepaid liability and
two PATCHes; coalescing uses 21 physical calls/liability and one PATCH. The unused
cached-JWKS liability remains burned. Thus 21 actual physical calls are removed,
not relabeled or refunded. This excludes full CLI/context/publication and original
operation-store bootstrap; it does not assert whole-operation capacity.

`InitialAssessmentTailTests` additionally runs the real queued reassessment
executor: a fresh owning reader observes the exact complete normalized admission,
original request digests, attempted call, response reservation and four-item
pending inventory before the provider callback. KeyboardInterrupt leaves that
receipt retained. Independent oracle reads use the same original control budget.
Storage race/failure tests do not substitute for the caller's required live
source/base/latest-inventory fences. The existing requirement to serialize
identity writers remains: GitHub PATCH is not distributed compare-and-swap,
and this API does not introduce a scheduler or a lock service.

This interface alone does not enable the CLI. The fresh CLI must still use the
real default operation store, complete immutable bindings, actual provider
observer, original counters/deadline and fresh source grant for every later
checkpoint, then publish under the original 64/60 allowance. Unknown dispatch
reconciliation and hosted/live qualification remain independent conditions.
Rollback keeps callers on the existing separate reservation path; old readers
already understand the resulting single incremented session record.
