# ADR 0128: Qualify an unconnected recovery authority candidate

- Status: Proposed
- Date: 2026-11-10
- Decision owner/reviewers: ReviewSensei maintainers; independent source review
- Linked GitHub Issue: user-directed recovery-gap work; issue pending
- Linked PR: pending

## Context

A published review and empty pending arrays cannot distinguish a terminal PATCH
that never entered the server from one whose response was lost and can arrive
later. Complete analysis does not prove accounting closure; partial analysis can
close normally. ADRs 0126/0127 authenticate observed data, not request cessation.
The existing comment adapter provides no cross-process conditional revision fence.
The broker currently returns installation write credentials to callers.

## Decision

Select a canonical per-repository-ID/PR authority with credential-contained
forwarding as the prospective design direction. A SQLite-backed Durable Object
behind the existing broker is the preferred eventual hosting candidate, reusing
existing OIDC/App authentication. This ADR does not add that binding, accept a
backend deployment, or establish any account entitlement/cost.

Add only `recovery_authority.SqliteAuthorityCandidate`: a local stdlib SQLite
adapter implementing a closed conditional transition contract. No CLI, broker,
session reader, publisher or recovery classifier imports it. Its snapshot always
reports `native_authority=False`. Neither dataclasses nor snapshot bytes are
portable authorization. There is no reset, takeover, migration, cancellation,
funding refresh, automatic repair or historical settlement API.

The service-owned `AuthorityHost` is an explicit, **unimplemented production
boundary**. It must independently authenticate current caller/source/actor,
workflow/policy/configuration and the same original funded allocation. Its
`authorize` checks the exact operation, request, effect and original reservation,
including current permissions and complete physical pricing. Its `forward`
rechecks authority at the actual effect boundary, contains credentials and uses
the SAME transport dispatch ID, method, target and canonical request bytes.
The LOCAL observation operation never authorizes native HTTP or code execution.
Caller-selected callbacks or reconstructed Access values cannot stand in for this
host. Test ports are synthetic issuers and forwarding models only.

## Scope and state transitions

One fixed, non-reused host epoch binds store identity, repository/PR, caller,
actor, source, workflow, policy, configuration, run/attempt and original funding.
Revisions increase for every committed state transition. Conditional comparison
uses the entire immutable expected checkpoint, preventing local lost updates and
ABA when earlier business values reappear. Epoch change requires a separately
qualified cutover; this candidate refuses it.

Preparation retains the exact request/effect identity and original reservation;
one reservation cannot fund two dispatch IDs. Identical preparation returns the
existing observation without new funding or execution. Forwarding irreversibly
commits `prepared -> forwarded` before calling the host. Death before callback,
lost response and absent readback all leave it consumed. No second POST/PATCH is
authorized. Readback attempts are also consumed before host I/O and capped at two.
Exhaustion leaves the request held; this finite profile is not a promise of
eventual settlement or permission to refresh an operation's budget.

Independent exact receipts distinguish effect confirmation, complete response
and evidenced usage. Effect confirmation alone preserves unknown response/usage.
Known response usage does not refund the original conservative liability, settle
other requests or clear legacy uncertainty. Conflicting or incomplete receipts
refuse. Actual original response-byte, dispatch and deadline accounting remains
the host/caller integration's responsibility; these candidate values cannot mint
it from matching native effect bytes.

## Nonrollback frontier and crash ordering

SQLite alone cannot detect restoration of its whole database. The host must own
a protected, linearizable, nonrollback frontier retaining each exact checkpoint
and irreversible consumption. Missing/divergent frontier state refuses.

Within a SQLite write transaction, construct and size the complete candidate,
then conditionally advance the frontier **before** committing SQLite. A lost
frontier acknowledgement or later SQLite failure rolls back SQLite but leaves a
possible advanced frontier. That identity is deliberately unusable. There is no
automatic copy-back, retry, root recreation or inference that nothing was sent.
The opposite divergence, including restored local state after forwarding, also
refuses. A real backend must qualify the frontier guarantee and a separately
funded reconciliation/cutover protocol before activation. The two-database
test frontier is not proof of an available production primitive.

## Legacy, funding and capacity

Store original legacy bytes unchanged, separately from the bounded current root.
Do not infer missing original inventory/classifications/placements. Any retained
legacy envelope blocks native publication, approval, checks, replies and cleanup;
only a non-native observation can be prepared. A late legacy projection cannot
replace this separate candidate state, but its effect and liability remain open.
No existing transaction or obligation is recovered by this candidate.

The downward profile retains the original maximum 512 calls and a nonzero
protected tail, unchanged absolute deadlines, 32,768-byte roots, 8 MiB retained
bytes, 256 parts, 200 comment allocations, 8 KiB request/query payloads and
512 KiB response bounds. Preparation holds the full dispatch liability plus
20 candidate control units from that allocation; it does not create another
allowance. This models a finite handshake, **not** a certified price for a real
host's internal authentication, census, storage or pagination. Missing authentic
funding or a higher actual price must refuse before dispatch, never borrow a
protected tail or add a census.

Admission counts old retained liability, candidate/frontier payload copies,
the distinct current-root row, append-only history and projected future result
checkpoints, including larger effect-only states and maximum revision width. It uses
the existing part payload/framing ceilings conservatively. These logical payload
measurements exclude SQLite files, journals, indexes, platform replication and
billing. Actual storage/retention and every physical request still need original
funding and hosted qualification. No denied capacity expansion is granted.

## Alternatives considered

- Another reader or prewrite checkpoint duplicates existing functionality and
  leaves the late-request ambiguity intact.
- Request-drain evidence could support the other contract, but current retained
  traces and supported comment APIs supply no such irreversible host receipt.
- Git-backed state still needs all-writer protection against other contents
  writers, forced rollback and ref deletion. Unrelated contents-write capability
  is not authority to create a canonical branch or change protections.
- A separate SQL service adds an unconfigured hosting/authentication boundary.
  The local adapter is a contract implementation, not selection of that service.

## Validation

Provider-free tests use real SQLite transactions, independent connections and a
separate synthetic frontier. Cover exact binding changes, concurrent claims,
stale checkpoints/epochs, process death, both commit divergences, database restore,
finite readbacks, original deadline/tail/capacity refusal, effect versus response
usage and the 57,243-byte generated legacy envelope. Its seven-required/ten-optional
instances remain unchanged alongside a generated LOCAL payload declaring complete
15-file/46-hunk analysis. This test preserves the declaration; it does not execute
analysis or validate a ReviewResult, qualify original inventory or grant coverage.
Independent review and affected deterministic checks remain required.

## Rollout and rollback

No deployed route, credential, resource, binding, storage-ban test, budget or
consumer changes. Existing recovery refusals and all historical holds remain.
Removing this unconnected module removes the candidate only.

Any future activation needs a concrete reviewed resource/cost/retention plan,
all-writer credential containment, original caller/funding/source issuers, hosted
frontier and rollback guarantees, installation provenance, every consumer gate
and a non-destructive cutover. Stopping issuance, expiry or job completion does
not drain legacy requests. Native publication/approval remains held wherever old
effects can race. Restoring a database never silently restores an authority epoch.

## Follow-up work

Qualify the real frontier/backend and physical pricing before connecting any
caller. Review complete legacy exposure or retain a permanently non-authorizing
legacy domain. Add source-qualified individual obligation decisions and actual
transport/effect reconciliation only through independently qualified issuers.
Deployment, permission/access changes, spending and historical recovery remain
separate decisions; source qualification authorizes none of them.
