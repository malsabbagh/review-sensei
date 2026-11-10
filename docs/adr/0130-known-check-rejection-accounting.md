# ADR 0130: Close fresh publication accounting after a confirmed check rejection

- Status: Proposed
- Date: 2026-11-10
- Decision owner/reviewers: ReviewSensei maintainers; independent source review
- Linked GitHub Issue: pending; user-directed lifecycle gap qualification
- Linked PR: pending; independent prospective correction

## Context

A native review and its approval decision can finish while check publication is
definitively rejected. The check diagnostic currently prevents the ordinary
review owner from entering terminal accounting. This confuses an unsuccessful
merge gate with an uncertain external effect. Conversely, a diagnostic string,
absent check, published review or empty pending array does not prove closure.

## Decision

Introduce invocation-private check-rejection evidence, separate from existing
publication/finalization witnesses. Observe the original physical dispatch after
its original debit and the response after existing deadline validation. Bind the
exact method, request digests, operation, original observation/request ID and
deadline, caller owner/configuration/startup, and source result/transaction.
A single observed 401 or 403 can qualify the corresponding original typed
`CheckFailure`. Copied values, restored documents and unrelated responses confer
no authority. Inspection is bounded to 8,192 existing request-body bytes and adds
no request or allowance. Closed evidence contains digests and typed facts, not
credentials, response bodies, arbitrary exception text or request URLs.

The canonical Application binds its authenticated transaction record and result
before check dispatch. The context separately seals the caller's accepted root
at that point; these are distinct roots and can advance through legitimate
publication/checkpoint writes. It is not a new latest-root or historical
settlement certificate. At completion, the original live result, issued approval
decision, positively returned ordinary native review ID, completed selected
cleanup, durable publication and unchanged business result qualify accounting
entry. Shadow metadata can differ. Fresh attempt one and `published` are the
bounded entry scope. The existing terminal caller still validates all unresolved
mutations, identity, original funding, deadlines and protected tails.

A qualified rejection permits that normal terminal close. The check diagnostic
and exit 1 persist even after successful accounting. No successful check or new
approval eligibility is inferred. `publication-outcome.json` additionally carries
closed `check_failure` code/operation/status facts when this proof exists.

## Scope

Fresh ordinary native review publication using the original caller, broker or
native check rejection, and source-issued cleanup/approval evidence. Existing
finalization witness meanings and ledger/accounting wire formats are unchanged.
Grouped native publication, replay and restored historical runs are outside this
proof path and continue conservatively pending when they lack other completion
proof. No broker, workflow, permissions, provider or deployment change is added.

## Consequences

Known unsuccessful check publication can have honest durable accounting without
making its gate successful. Partial analysis and required human decisions keep
their classifications and withheld approval. Lost/ambiguous responses, expired
requests, unknown earlier effects, unconfirmed cleanup or failed terminal writes
preserve the accounting hold. Existing historical incidents remain unsettled;
this source correction cannot recreate their absent original receipts.

## Alternatives considered

- Adding `check_access_denied` to the diagnostic allowlist loses physical effect
  provenance and can accept reconstructed or stale evidence.
- Reusing publication completion would falsely assert that the native check
  succeeded; use an independent negative-result proof instead.
- Refunding denied or lost calls would discard actual usage. Every original
  debit remains and unchanged caller guards decide closure.

## Validation

Provider-free real entry fixtures cover broker/native denial, partial and normal
publication, lost writes before and after server storage, ambiguous responses,
an earlier unknown mutation followed by definite denial, terminal-write failure,
expired response, unconfirmed cleanup and copied/foreign/changed evidence.
Existing check-diagnostic, finalization and review-run regressions cover nearby
success, replay, funding and liability behavior. Independent review assesses the
fixed source and tests. Local checks qualify the prospective patch, not a deployed
historical recovery path.

## Rollout and rollback

Normal source review and CI precede any release. No activation or release is part
of this change. Rollback restores conservative pending behavior for future
rejections; it supplies no replay permission or historical ledger rewrite.

## Follow-up work

Qualify grouped native final-receipt issuance independently. Historical completion
still needs original authenticated effect/accounting evidence and its supported
recovery authority. Broader writer fencing and late-effect problems remain
separate from this original-process negative-response correction.


## Local qualification amendment: closed proof-hook observations

Date: 2026-11-10. Status remains Proposed. This additive source candidate is
qualified separately from the original PR and is not deployed.

Supplementary exceptions at original dispatch capture, confirmed-response
capture and publication-proof issuance can record `dispatch`, `response` and
`publication` respectively in the existing lifecycle observation. Three bits per
active lifecycle bound storage; repeated reports deduplicate and serialize in
fixed order. The normal and bounded-fallback observations share the same closed
field. Unknown input labels are ignored without formatting opaque objects.

No exception object or identity is accepted by the recorder. No decision reads
its state. Guarded reporting failures preserve the original operation and its
exit, and original cancellation propagates. Tokens neither certify completion
nor invalidate an already established original proof; they report that an
exception handler was reached, without inferring the hook's partial progress.
Original charges, uncertain mutations, funding and terminal guards are unchanged.

Provider-free regressions exercise the three real handlers, leakage canaries,
bounded repetition, foreign labels, nested contexts, unavailable/corrupt state,
broken recorder/output, primary cancellation and an unknown mutation that stays
unsettled even when every token is recorded. The dependency is the original
check-accounting source slice; publication or integration needs separate scope
reconciliation and authorization. No historical record or diagnostic is rebuilt.
