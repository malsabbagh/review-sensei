# ADR 0110: Separate repository-bound thread cleanup authority

Status: Proposed; implementation candidate, not published or enabled.

GitHub can reject `ResolveReviewThread` for a Pull requests: write installation
token even though ordinary review publication succeeds. GitHub's issue
[35726](https://github.com/github/gh-aw/issues/35726) reports the additional
Contents: write requirement. This corroborates the observed rejection; it does
not reconstruct the historical token's effective permissions.

Add the fixed App capability `thread_resolve`, requesting exactly Contents:
write and Pull requests: write, plus GitHub's implicit Metadata: read. Keep
ordinary review, reply, session and check grants unchanged. Retain exact
returned-permission validation, including rejection of unsolicited Contents:
write on an ordinary review token. No PAT or `learning_write` fallback exists.

The broker derives repository identity from verified Actions OIDC claims,
independently checks its numeric identity and fork status, and looks up that
repository's installation. GitHub issuance selects only that repository. The
operation is generic across existing authorized installations and selected
repositories; it contains no repository-specific exception. Public immutable
workflow admission, issuer/audience/signature checks and original dispatch
controls remain mandatory. The broker authenticates this execution context;
it does not establish that a particular finding has been independently assessed.

Initially wire the capability only into `verify` and disposition command
re-finalization. Complete discovery, independently owned root readback and
current decision/head/base/result fences use the ordinary review token. Only
an eligible unresolved candidate can lazily request cleanup authority. The
operation caches it locally; acquisition uses the same controlled broker client,
original budget and deadline. Refresh root, decision and current-source fences
after acquisition. Only resolution and bounded lost-response readback receive
the cleanup token. Empty, foreign, unassessed, resolved or stale candidates
acquire nothing. Failure leaves cleanup unconfirmed and decisions retained;
there is no fallback or automatic permission escalation. Denial stops the batch.

A subsequent supported request can observe prior progress under its own bounded
allowance. Neither issuance nor a retry resets an existing request's budget.
Command finalization performs cleanup before lazily acquiring the separate
check token. Otherwise retained hosted-ledger reads plus eager check issuance
can consume the entire allowance before the first cleanup token fits, making
every retry repeat the same non-progress. Cleanup therefore runs under an
ordinary hold derived as `1 + exchange liability + 2 + 5`: one request, the
`check_publish` exchange's internal dispatches, check publication's lookup and
create/update, and finalization's five approval reads (up to three pull
preflights, one review-thread scan and one persisted-reviews readback). The
packaged default is 18, from
`COMMAND_EXCHANGE_DISPATCH_LIABILITY = 10` in `broker_control.py`;
`COMMAND_FINALIZATION_RESERVE` in `publication.py` is that packaged default.
A caller-provided broker reports its configured liability on its control
object, and the application reads `exchange_liability` from whatever control
the broker exposes, not only the packaged accounting type, so a wrapper that
forwards that value cannot be under-reserved. Derivation does not depend on the
accounting being bound to this request's budget: a foreign envelope cannot mint
cleanup authority (the token provider refuses it), but the packaged default is
never silently substituted for a broker whose control reports a larger
liability. The hold spends nothing;
it only narrows cleanup, which remains unconfirmed and can resume under a later
supported request's own allowance. Broker admission reads the same narrowed
ceiling while the hold is outstanding, so cleanup's own capability exchange
may use only the unheld room; the check exchange runs after the release. The
hold stays unconditional when less than the reserve remains: that allowance
cannot reliably fund discovery plus the separate `thread_resolve` authority,
while withholding it leaves the check and approval as much of their liability
as the request can still provide. The hold is non-reentrant and is released
only by the exact count reserved, so a nested or mismatched release cannot
discard another phase's reservation. Acquisition is all-or-nothing: a refused
hold leaves nothing reserved and is validated with the same strict integer
test as the release. The hold is phase-local and is deliberately excluded from
the budget snapshot, so a restored budget starts unheld and a resumed operation
re-establishes whatever reservation it still needs. A refused release is
observed at
`cleanup.hold` instead of replacing the cleanup outcome. A prepaid
activation tail or an existing hold refuses the reservation without spending
allowance; because an outstanding tail also refuses every ordinary dispatch,
cleanup cannot proceed and the request records the distinct `cleanup.hold`
stage with reason `hold-conflict` before leaving the threads unconfirmed for a
later request. Keep the
intended check conclusion independent of acquisition. An unfunded or failed
deferred check retains cleanup progress and defers a new native approval; an
exact-head approval already observed remains a truthful observation. The next
supported request can observe resolved threads without another cleanup token
and finish check/approval work.
Explicit-check-token callers retain their existing check-first behavior; this
is not a new universal claim that native approval always depended on check
confirmation. Token liabilities, call ceilings and final authority fences stay
unchanged.
Normal review supersession and conversational reply entry points do not
implicitly acquire this capability; supported command finalization remains the
explicit recovery path. Source, inventory and approval gates are unchanged.

GitHub does **not** restrict this credential to thread resolution: Contents:
write permits repository-content writes in its selected repository. Lazy
acquisition and application fences limit intended use, not GitHub's inherent
grant. Broadening `review_publish` would expose that authority during every
review; a PAT would introduce a separate user principal and credential lifecycle.
A broker-executed fixed mutation would reduce token exposure but would be a
different execution boundary. The dedicated App capability is selected.

Implementation qualification includes exact grants, generic repository and
installation binding, admission rejection, token separation, post-acquisition
authority races, original accounting and deadline refusal, denial/lost-response
recovery, and durable progress across more than twenty legitimate synthetic
decisions with the production broker and public command seam. Synthetic proof
does not establish live installation grants or actual native approval.

Deployment needs a qualified Worker revision, hosted package and coordinated
signed channel activation. Core/npm need no release unless their actual inputs
change. Before live activation, present the concrete generic issuance population
and repository-content-write authority for approval. Read existing App and
installation grants first. Missing registration permission or installation
acceptance requires an explicitly identified, separately approved change; never
expand repository selection silently. Read-only setup inspection must not invoke
helpers that internally mint installation tokens.

Rollback withdraws the broker capability or hosted acquisition path. Unsupported
issuance fails closed without fallback. Withdrawing issuance does not revoke
already-issued GitHub credentials; account for their remaining lifetime or an
explicitly authorized revocation before treating that authority as withdrawn.
Keep immutable releases, durable
decisions, truthful uncertain outcomes and all approval evidence.
