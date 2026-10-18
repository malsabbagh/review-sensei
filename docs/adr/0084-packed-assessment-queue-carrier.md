# ADR 0084 - Lossless packed assessment queue carrier

Status: Proposed
Date: 2026-10-18
GitHub Issues: #240, #241, #242, #243, #244, #269
Pull Request: pending
Owners/Reviewers: Maintainers
Approved by: not applicable

## Context

A trusted reusable host can already compose explicit `FeedbackSelector`,
`reassess`, `AssessmentQueueHostAdapter`, and `LocalSessionLedger` prepaid tails.
The complete queue, original execution accounting and accepted decisions must
survive before public publication. A 40-finding varied-identity targeted operation
stored its approximately 11 KiB factored envelope in partition objects. Preparation,
reservation and the two coalesced checkpoint activations consumed 50 original
control reads in the initial prototype. Adding the required current PR/base/head
and latest authority fences raises the fully fenced partition case to 56 reads
after the single-read admission optimization below. Publication then exhausted the ordinary 60 slots of the unchanged
64-call envelope, before any reply or authority record.

## Decision

Provide opt-in `pack_queue_state` and `unpack_queue_state` helpers in the queue
owner. A closed `assessment-queue-state-packed-v1` document contains the existing
canonical `zlib-json-v1` bounded evidence encoding of the **whole** existing queue
envelope. Encoding and decoding independently enforce the existing 2 MiB queue
receipt ceiling, validate the complete legacy or factored journal, and retain all
IDs, pending order, resolutions, original admissions, counters, reservations and
accepted decisions. The helpers confer no authority and create no new store.

The trusted host may use the existing authenticated inline manifest only when
the packed document fits both its existing manifest ceiling and
`assessment_queue_manifest_capacity(record)`. Otherwise it must use the existing
partition path or refuse within the original allowance. Neither data truncation
nor read/deadline renewal is a fallback. A reader that does not understand the
packed queue document must refuse; this is an explicit host opt-in, not an
automatic CLI/Worker writer activation.

The root continues to carry its exact repository/base/head/policy/configuration,
producer, inventory and generation binding. The host authenticates the original
source and every participating source again before each mutation, preserves the
original reservation/witness and source operation binding, and uses the ledger's
sealed generation-fenced activation and owned full readback. Live publication
retains the existing source, current PR/head/base, authority and approval checks.
Digest equality and compressed bytes cannot substitute for any of those checks.

Admission reuses its bounded authenticated receipt once for full decoding;
every actual activation retains the original generation fence. Every accepted
replay still persists original counters and the time high-water before returning.
Suppressing an otherwise identical replay write is unsafe: current usage can be
higher than the stored counters after max-merging, and a later process with a
regressed wall clock must not forget a previously observed age. Receipt expiry
and within-operation clock monotonicity are checked again at persistence. None
of these checks renews original origin/deadline/expiry or permits unknown
provider outcomes to dispatch again.

## Validation and limits

`QueueStateCarrierTests` covers complete 40/100/250 varied-ID queue round trips,
encoded and decoded refusal, changed bytes/digest/size/schema, foreign manifest
binding, semantic corruption, and a high-entropy 250 queue that cannot fit inline.
Queue storage round trips do not establish rich eligibility publication capacity.

`PublicFeedbackContinuationTests` composes the actual public preparation,
assessment and publication APIs with the actual local ledger, synthetic GitHub
HTTP and deterministic provider. A full-ID target after the first 32 findings
resolves only that target, retains the other 39 obligations, withholds approval,
and completes within 56 charged original reads. Reconstructing the accepted
checkpoint with its original live ledger owner performs zero new provider calls
and preserves original execution/counters/deadline, but its mandatory replay
activation consumes the publication allowance. That full replay/publication path
remains budget-blocked. A fresh local ledger without the genuine original
attempt witness refuses activation; queue bytes or a reservation string cannot
supply that proof.
Source edits, current PR/head/base or latest inventory changes, and competing
local generations refuse without unsupported effects.
An early semantic rejection leaves its four obligations unresolved while a late
second batch is durably accepted in the same operation.

This qualifies an explicitly trusted local library host composition. It does not
qualify installed CLI/Worker activation, consuming remote broker grants, crash
restart after an unknown dispatch, concurrent OS writers, or deployed delivery.
The varied 100-finding current full receipt exceeds the inline growth allocation;
partition fallback remains lossless but still cannot finish publication within
the original allowance. Rich 250 eligibility still requires compatible v4 writer
and reader activation, which remains separate work. No cap, approval rule,
governance policy, release or deployment changes here.

## Alternatives and rollback

Increasing read or inline caps, omitting original receipt fields, treating prose
as target authority, or resetting the budget would weaken the existing contract.
Those alternatives are rejected. Follow-on work can qualify references to
unchanged authenticated inventory/facts or a complete budgeted immutable reader
projection; it must preserve original accounting and delayed-reader recovery.
A referenced-inventory prototype fits the varied-100 manifest but does not fund
the unchanged multi-page public report under the same allowance. It is not
shipped as a second carrier contract without end-to-end benefit. Coalescing
actual owned transaction reads and safe immutable page associations remain
implementation options; external access is not their blocker.
For rollback, disable this host opt-in and retain compatible packed readers for
already referenced roots until their retained source receipts/tombstones expire.
