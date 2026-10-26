# ADR 0111: Caller API work and independent Worker bounds

- Status: Proposed; implementation and qualification in progress
- Date: 2026-10-25
- Decision owner/reviewers: repository owner; Astra High
- Related issue: user-directed repository API-work redesign
- Pull request: pending

## Decision

One root `.reviewsensei.yml` setting, `api_request_limit`, bounds physical
ReviewSensei caller HTTP attempts for one authenticated repository ID and Actions
`run_id`. The default candidate is 512. The parser accepts integers from 1 through
9,007,199,254,740,991, rejecting booleans, fractions, nonfinite values and larger
integers. This upper bound is the common exact JSON/JavaScript representation,
not a workload recommendation. Reservation arithmetic uses bounded integer inputs.

The allowance includes policy/configuration, authorization, PR/evidence, OIDC,
Worker exchanges, publication and cleanup. Each Worker POST counts once;
Worker-internal fanout is informational to the caller. The Worker independently
limits an exchange to at most 60 external dispatches, a 25-second lifetime and
bounded upstream deadlines. A lower source-selected exchange ceiling may tighten
that limit. Neither dispatch receipts nor grants allocate caller capacity.
Inference retains its separate resource limits. Checkout, package installation
and artifact upload are infrastructure outside this ReviewSensei allowance.

One lifecycle process owns the selected review, command or reply route. Shell
steps do not obtain business credentials, check the live PR head, publish or
clean up reservations in another process. Cancellation forwarding and artifact
steps remain. Trusted policy commit **P**, its exact configuration digest and its
limit are frozen independently of reviewed base **B** and head.

## Finite allocations and recovery

The current review/reply startup policy occupies 130 requests (77 initial and
53 successor); commands occupy 132 because each attempt authenticates its source.
These are conservative source-controlled reservations, not observed shipping
minimums. Remaining capacity is divided between the initial owner and one
successor, each with protected terminal capacity of 50 and minimum work capacity
of 4. Consequently 238/240 are minimum admission funding for review/reply and
commands, respectively. Admission never promises completion.

Ordinary reads and native mutations have finite operation allocations, with
separate confirmation capacity protected before a write. A read may exhaust its
allocation even when the repository allowance has unused capacity. Independent
pagination, retained evidence, output and deadline limits remain enforced.
Unknown or abandoned allocations are never refunded. An uncertain HTTP outcome
remains charged even if exact readback later establishes its business effect.
Uncertain writes are never replayed merely because their response was lost.

A run attempt, job and owner generation identify an execution owner, not another
allowance. Both startup slots remain occupied after crashes and when unused.
Same-slot process restart and a third attempt refuse. Supported attempt 2 can
recover only after authenticated predecessor-job cessation and exact native owner
checks. It retains the predecessor's accounting and cannot consume abandoned
capacity. A retained pending review may publish without repeating inference.
Separate workflow-dispatch publication recovery is retired.

Authoritative PR concurrency serializes writers. Comment PATCH is not CAS;
generation and exact readback detect conflicts but do not provide a distributed
lock. Active, overlapping, ambiguous or changed owners refuse.

A genuinely different Actions run may replace terminal accounting only after
proving predecessor cessation and retaining its exact root in an authenticated
one-part archive. Archives preserve their previous pointer, charges, decisions,
baselines and convergence counters. They transfer no capacity. Archive, root and
transition size boundaries refuse before an unfunded or unrepresentable write.
The native root is bounded to 32 KiB and its rendered comment to 64 KiB; evidence
and pagination ceilings are independent.

## Review and action terminal states

Review completion requires its exact transaction and confirmed publication or
explicitly unnecessary publication. A narrowly typed pre-operation abort closure
is allowed only for the original owner/reservation/transaction when operation
entry never started, native cleanup is confirmed and no unresolved obligation
remains. Entry is marked started before analysis. Missing fields are not proof.
Provider failure, timeout, interruption after entry, unknown reservation and
uncertain cleanup remain conservative blockers. Post-dispatch provider settlement
is future work, outside this change.

Commands and replies admit a durable intent before effects. It binds purpose,
source ID/version/body digest, run, owner, head, configuration, operation key and
antecedent root. Execution entry is durable. Terminal closure requires a typed
outcome bound to the exact retained result; an inherited review transaction
cannot close a newer action. Unknown effects block rollover. Retained outcomes
use the existing bounded compressed evidence codec.

Reply finalizers produce process-issued completion evidence only after all
selected effects are confirmed or explicitly unnecessary. Legitimate withholding
may be terminal while approval remains blocked. Default, missing, serialized or
independently supplied completion is unconfirmed. Masked check/thread failures
cannot become success through a diagnostic string. Conversational replies require
their exact publication identity. Broader reassessment must establish its own
completion and cannot borrow an earlier reply's proof.

## Current authority and retained rescans

The Worker and caller use one closed version-3 authority envelope with purpose
`review`, `command` or `reply`. It binds repository/PR/head, authenticated Actions
run/attempt/workflow, bounded expiry and exact source identity. Purpose-specific
capabilities are not interchangeable. The Worker authenticates these subjects
against GitHub/OIDC; parsing a serialized document confers no authority. The
Worker supplies its trusted numeric App ID; event hints cannot supply it.
Obsolete grants and separate hosted phase entry points refuse before effects.
Historical bytes remain evidence and cannot authorize new work.

Retained rescans use an explicit historical publication reader bound to the
AuthorizedRescan source, exact review/reference/archive/producer and current
run/owner/transaction. Fresh source and head checks remain. Issue-comment rescans
skip the unused Actions-token exchange; workflow-dispatch rescans reuse startup's
Actions capability while independently authenticating their dispatch source.
Typed budget/deadline exhaustion is preserved rather than reported as malformed
coverage. Original decisions and incomplete coverage remain authoritative.

## Qualification and limits

The measured 512-workload sequence uses separate, genuinely different Actions
runs: a 20-finding review, two ten-ID commands and verify. Observed caller counts
were 84/109/114/55; all 20 decisions and one native approval remained. These
measurements do not establish an allowance reset within a run. Current reply
fixtures exercise the actual CLI/provider/journal/reader/check/thread path,
including confirmed withholding. Actual Worker qualification uses fake external
HTTP around production authentication, grant and service-ceiling code; synthetic
receipts alone are insufficient.

The exact 300-comment input with twenty 60-KiB bodies exceeds the ten-attempt
native discovery envelope and refuses before provider entry or root/business
writes (19 physical startup requests). The earlier positive for that input
covered checkpoint phases, not a complete CLI lifecycle. A bounded adaptive
24-comment workload completes the current CLI at 138 caller requests. Raising
the root allowance does not enlarge this independent discovery envelope.

The media check capability uses its existing independent Worker ceiling of 60;
the current cold JWKS/annotated-tag/source-validation path needs 11 internal
dispatches. It still costs one caller POST. A known media receipt followed by
an incomplete approval/check result remains pending and refuses automatic
successor replay; this change does not add general partial-action recovery.

The offline production-code matrix accepts matching versions for review,
command and reply. Released callers refuse against the current Worker before
installation-token issuance. Current v3 sessions refuse against the released
Worker after two internal token requests; its separate Actions read capability
can succeed before that mandatory session barrier. No mixed-version session
grant or enrollment response is returned. These refusal details are part of the
cutover contract, not a compatibility fallback.

Final acceptance requires immutable-head source and installed tests, platform
qualification, production Worker contract tests, exact workflow recognition,
required CI and independent review. Moving-source or earlier checkpoint counts
are scoped evidence only. Four socket-dependent provider tests cannot execute in
the current restricted local environment and still require the platform lane.
Earlier rejected experiments and their measurements remain in Git history and
external evidence; they are not shipping policy.

## Cutover and rollback

The required matrix is released caller/released Worker working; either mixed
version refusing unsupported authority without fallback; new caller/new Worker
qualified for review, command and reply. The new source does not retain an
executable legacy-authority bridge.

Cutover needs a separately approved temporary admission interruption. Because
the released Worker lacks a hold, the publisher first needs a narrowly qualified
old-protocol hold-only bridge. The hold must fence token, grant, webhook and setup
continuation issuance/writes on every reachable surface, including SELF, while
keeping health available. Held requests return distinct temporary unavailability
with no issuance, queued replay, automatic retry or false completed acknowledgement.

The sole publisher must derive a drain from the latest possible issuance after
hold propagation, actual token/grant/cursor lifetimes and bounded in-flight work;
elapsed time alone cannot settle unknown effects. After approval: enable and
verify hold, drain and identify unresolved work, deploy the new Worker with hold,
promote signed v5, verify exact identities, reopen, then run bounded post-release
tests. On failure retain the hold. Do not roll back to old readers after new state
has been written without a separately proven rollback path. Source qualification
and release authorization do not themselves authorize activating the hold.
