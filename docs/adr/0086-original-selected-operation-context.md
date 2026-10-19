# ADR 0086 - Original selected-operation context

Status: Proposed
Date: 2026-10-19
GitHub Issues: #240, #242, #243, #244, #245, #269
Pull Request: pending
Owners/Reviewers: Maintainers
Approved by: not applicable

## Context and bounded prerequisite

Selected CLI feedback needs the actual server-admitted operation handle before
it can bind a queue reservation. A source-derived reservation string, a retained
JSON snapshot, or a freshly created budget cannot supply that witness. The
existing adapter hides the actual handle, substitutes scope-only inventory and
source hashes, and uses a fixed structural clock around the whole computation.
Those defaults are not evidence of a physical 64-dispatch/60-second lifecycle.

`OperationHost` additionally rejected present-day Unix milliseconds because its
clock guard used the 32-bit cursor-generation ceiling. Stored timestamp fields
already have the separate safe JSON-integer domain. Four executable wall-clock
oracles failed before correcting that domain. Timestamp arithmetic now retains
room for the original 60-second deadline, without changing generation bounds,
expiry, rollback or original allowance semantics. Remote root observations and
write-start observations use the same timestamp domain, permitting owned Unix-time
reconciliation and lost-ack recovery without expanding generation bounds.

## Prerequisite interface

The optional `HostedComputationIdentity` carries the actual immutable inventory,
complete selected-source digest and complete operation ID. The existing adapter
still owns its store cursor. `execute_with_operation` receives a real handle
from `restore_operation` after durable charged admission; the callback cannot
receive a handle reconstructed from a dictionary or nonce. The final result
also returns the actual restored operation state after owned result acceptance.
An accepted retry performs no callback or inference. Changed identity refuses.
Unknown/unaccepted handle-aware results never escape as fresh usable work.

`call_operation_trigger` requires explicit original clock, origin, identity and
bootstrap count for this mode. Its unselected structural defaults remain
unchanged. This does not assert that a declared bootstrap or transition liability
measures every physical request. A callback surrounding an entire queue is not
an actual provider-dispatch observer, and must not be activated as the selected
CLI implementation on that basis.

## Required selected CLI composition, still unimplemented

Keep the selected CLI profile disabled until a complete public oracle composes:

1. Closed selector validation before credentials; actual complete source and
   current PR/base/head/latest-authority admission; complete execution identity.
2. The default owned operation store and actual GitHub session ledger, with a
   genuine handle from the same operation and a fresh consumed v2 mutation grant
   before every root mutation.
3. Actual provider-dispatch observation around inference, rather than labeling
   queue preparation as provider dispatch. Unknown outcomes remain held until
   authenticated retained-result reconciliation; queue preparation failure must
   not silently reopen a charged attempt.
4. One original physical/control allowance including OIDC, capability issuance,
   broker subrequests, queue parts/root/readbacks, publication and retry. Persist
   usage and observed time before returning an accepted replay.
5. The actual fresh CLI/installed command: one explicit last full ID in the
   original 40-item fixture reaches inference, is durably accepted, publishes
   exactly that resolution and retains 39 pending without approval. Refusal alone
   does not close CLI wiring.

The current broker response has no dispatch-cost receipt or remaining-budget
contract for its internal OIDC/runtime/installation/token/enrollment/source
requests. The minimal sound addition must enforce an original deadline and a
finite caller-funded dispatch liability before each broker subrequest, return
an exact scoped cost receipt, and fail closed on absent/unsupported receipts.
Post-hoc counts without pre-dispatch admission would not bound physical work.
Bootstrap and a failed/uncertain response cannot refund that liability. A
short-lived authenticated bootstrap context may avoid repeating the expensive
immutable OIDC/runtime/installation checks; live source/head authorization still
occurs for each root mutation. That optimization needs its own exact protocol
and executable public acceptance, not an assumption that a token is a grant.

## Validation, alternatives and rollback

`OperationWallClockTests` exercises the real default wall clock, owned-comment
round-trip, unchanged charges/deadline, finite timestamp bounds, generation bounds,
expiry and clock regression. `HandleAwareOperationEntryTests` exercises actual
owned store/parts, exact request binding, zero-callback accepted retry, required
clock/bootstrap inputs and unknown-result refusal. These are component proofs;
they do not qualify public CLI selection, broker consumption or physical capacity.

A second queue store and a fabricated handle are rejected alternatives. Keep the
existing queue/session/operation owners and their exact authority fences. No
permissions, grant-consumption policy, default writer, workflow channel, deployed
Worker or provider configuration is changed. Rollback omits the additive optional
context; existing unselected callers retain their original behavior.
