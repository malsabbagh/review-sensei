# ADR 0088 - Opt-in broker physical dispatch control

Status: Proposed
Date: 2026-10-19
GitHub Issues: #240, #242, #243, #244, #245, #269
Pull Request: pending
Owners/Reviewers: Maintainers
Approved by: not applicable

## Context and source oracle

A caller HTTP request is not the whole broker cost. The actual production
`TokenBroker` fixture performs 13 internal OIDC/GitHub dispatches for a cold
feedback grant with two complete sources; grant verification adds one. Counting
only `BrokerClient.before_request` understates that operation. No deployment or
live credentials are needed to reproduce this with the existing consuming
broker bridge and synthetic external transports.

## Decision and scope

Add an explicit optional `control_budget` envelope to token issuance and grant
verification. Its closed versioned fields bind an operation ID, unique request
ID, original absolute Unix-millisecond deadline and finite maximum internal
dispatch liability. Maximum liability is at most 60, and remaining time is at
most 60 seconds; these are ceilings, never a fresh operation allowance. A v2
feedback mutation's operation ID and read-accounting deadline must match before
network work begins. Existing source/OIDC/actor/runtime/permission/grant checks
remain unchanged. The metadata contains no source bodies or credentials.

`BrokerDispatchControl` is request-owned. Actual OIDC/JWKS and GitHub REST/Git
fetches pass its pre-dispatch counter/deadline gate and receive an AbortSignal
bounded by the remaining original deadline. Public-runtime REST fallback is
also gated. A response received after expiry or a wall-clock regression cannot
complete the request. The existing public JWKS cache remains; a wait on another
request's in-flight JWKS fetch is independently deadline-bounded without
canceling that request's I/O or charging a nonexistent extra fetch.

Controlled fetches use Cloudflare's supported `redirect: "manual"` and refuse
all 3xx responses with a closed `broker_control_redirect_refused` diagnostic.
Automatic redirect hops would otherwise hide physical dispatches and potentially
forward authentication headers. A refused Git advertisement never falls through
to another resolver. Legacy uncontrolled fetch behavior remains unchanged.
The controlled Python default transport also refuses redirects on caller/OIDC
requests; injected trusted transports must preserve the same physical accounting.
An OIDC response returned after the original deadline cannot be accepted.
Cloudflare explicitly does not implement `redirect: "error"`; see its
[Request redirect documentation](https://developers.cloudflare.com/workers/runtime-apis/request/#parameters).

Success includes a closed `control_receipt` echoing the original scope and
observed internal dispatch count. The HTTPS broker transport is the existing
trust boundary; the receipt confers no source, root, admission or grant authority.
`BrokerDispatchAccounting` prepays the complete finite internal liability in the
caller's existing `EvidenceReadBudget`, before its normal HTTP charge. Missing,
changed, malformed or over-liability receipts refuse before returned authority
can be used. Unused or uncertain liability remains charged; no refund occurs.
The original budget callback and deadline cannot be replaced with another
instance. A whole request whose liability plus HTTP call cannot fit refuses
before any transport or partial debit.

## Consequences and alternatives

Unattended analysis admission prepays 30 internal dispatches plus its caller
HTTP request from the original metadata budget. Thirty is the exact enumerated
ceiling, not an estimate, and it counts the maximum, annotated-tag fallback
case: cold JWKS (1), the failed Git advertisement plus its two annotated-tag
REST reads (3), installation and repository checks with the session capability
(4), current head (1), App identity (1), and both enrollment witnesses at the
reader's own 10-request page ladder (20). The fallback's 3 is the one step
that differs by path: the Git path completes the same resolution with a single
successful advertisement, so the deterministic non-witness prefix is ten
dispatches on the fallback path and eight on the Git path, and the reservation
funds the larger fallback case. None of those paths retries, refreshes or
re-reads, so the reservation leaves no spare dispatch: a dispatch added to
this exchange requires rederiving and qualifying the bound within the
unchanged original operation budget. The explicit qualification
would surface an unfunded dispatch as
`broker_control_budget_exhausted` in the funded annotated-tag history instead of
the witness's closed code. A witness restarts its ladder at a smaller page size
when a page exceeds the response byte cap; every failed read is charged to the
witness's own ten-request allowance, and a restart does not reset it, so one
complete inventory can legally cost ten dispatches. With both ladders funded
in full, a byte-cap restart can no longer become an unfunded admission
dispatch: it either completes within the witness's remaining requests or
refuses with the witness's closed `github_session_witness_failed` inventory
code. The reservation therefore bounds the admission budget, not the size of
arbitrary histories. The same factory reserves 12 for the witness-free rescan
authorization exchange, which stops at the capability token. These are bounded
reservations, not a promise that arbitrary paginated histories fit, and a
witness that exhausts its own ten-request cap cannot produce a successful
enrollment from an incomplete inventory. The earlier 12-dispatch reservation
funded the cold annotated-tag path with one page per witness; extra physical
reads could exhaust it. Some smaller or warm fallback histories still fit that
allowance. The retained-data reconstruction of PR374 required 13 internal
dispatches through Git or 15 through annotated-tag fallback; these are offline
measurements of reconstructed inputs, not counters from its failed live request.
Admission and completion own separate original budgets, so the larger
reservation does not reduce the review's completion allowance. Enrollment and
rescan authorization share the admission budget: 30 plus 12 internal units and
two caller POSTs reserve 44 ordinary units before caller OIDC and authority
reads, leaving at most 16 of its 60 ordinary units. This does not prove that a
complete rescan lifecycle fits. The 64 total /
60 ordinary call ceilings, original
deadline, exact receipt binding, and no-refund rule are unchanged. This
accounting correction applies when the containing hosted package is released;
it does not authorize renewing a previously refused live attempt or attribute
any historical failure to this fallback without its original dispatch evidence.

This deliberately charges a conservative admitted liability when a cached or
failed request uses fewer internal calls. Its returned count is evidence, not a
refund. Post-hoc telemetry alone is rejected because it permits unfunded server
work before the caller learns the cost. A mutable global request counter or
canceling another request's shared JWKS fetch is rejected. No second queue store,
new permission, new grant-consumption policy, relaxed live authority check,
workflow-channel movement, provider change or Worker deployment is included.

The accounting class does not persist or authenticate a budget. The application
must retain its admitted liability and observed time through the genuine original
operation/checkpoint before making the external request. This commit does not
wire the selected CLI, establish a broker continuation credential, qualify remote
queue/publication capacity, or repair unknown-dispatch reconciliation. Neither a
component receipt nor a refused CLI closes those remaining engineering gaps.

## Validation and rollout/rollback

`broker-dispatch-control.test.ts` executes actual production signed-grant policy
and external transport fixtures. It proves exact counts, pre-dispatch refusal,
absolute deadline, blocked-fetch cancellation, exact mutation binding, concurrent
request isolation, shared-JWKS wait bounds, clock rollback and closed bounds.
The explicit bridge also executes all three production fetch sites through real
loopback HTTP redirect handling: each 302 is refused after exactly one HTTP
request, with no destination request or REST fallback. A real blocked HTTP fetch
is canceled at the original deadline. These use Node's real transport with the
Worker-supported manual redirect API; no deployed Cloudflare claim is made.
The Python default-transport HTTP oracle observes two requests for the unchanged
legacy redirect and exactly one refused request with control enabled, retaining
its one original debit. Bounded final validation rejects a late OIDC response.
`BrokerDispatchControlTests` covers prepaid caller liability, unchanged deadline,
no unused/uncertain refund, missing/altered receipts, refusal before unfunded
transport, and exact original callback ownership.

The explicit `tests.epic238_broker_control_qualification` runs Python's actual
client against production TypeScript through the consuming bridge. Cold issuance
uses 15 total calls: caller OIDC, caller HTTP and 13 internal calls. Verification
adds exactly two, reaching 17. A one-slot internal liability stops at three total
calls before the next server fetch. A mismatched mutation/control deadline makes
no internal dispatch and burns the admitted caller liability. These are fixture
segments, not a full CLI/queue operation or deployed Worker qualification.

Default callers send no control envelope and retain their previous protocol.
New controlled clients refuse old Workers that omit a receipt. Deploying the
reviewed Worker and explicitly activating an authenticated caller remain separate
steps; rollback disables the opt-in caller or omits the optional control metadata.
Do not activate selected CLI success based on this component proof. It still
needs genuine same-operation physical accounting across bootstrap, every queue
mutation, inference, publication, interruption and replay within original bounds.
