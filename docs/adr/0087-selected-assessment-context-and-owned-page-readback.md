# ADR 0087 - Selected assessment context and owned page readback

Status: Proposed
Date: 2026-10-19
GitHub Issues: #240, #242, #244, #269
Pull Request: pending
Owners/Reviewers: Maintainers
Approved by: not applicable

## Problem and failing acceptance

ADR 0085's 57-call positive library fixture supplied an already prepared
conversation. Charging ordinary `prepare_context` to the same original budget
made cold selected assessment of 100 varied findings with 0/100/250 historical
comments fail at 60 calls. Durable accepted decisions remained intact, but a
provisional report did not advance authority. The desired completion oracle was
red for all three histories; the prior safe-refusal control did not close liveness.

Ordinary discussion preparation loads generic thread history, prior findings,
diff context and repository learnings. Existing unified human reassessment does
not pass those generic conversation fields to inference: `prepare_feedback`
loads the retained full finding inventory, exact selected-source closure and
complete current required evidence separately. Its source citations and bundle,
not historical conversation excerpts, determine the assessment prompt.

## Explicit selected preparation

Add `ConversationPublisher.prepare_selected_assessment_context(token, selector,
app_slug, root_comment_id=None)` as an opt-in public library seam. Require an
explicit selector with nonempty full target IDs. Read the actual trigger and PR;
validate source ID, association, authorization, revision, root and current exact
base/head. Refuse missing or changed scope. Do not load unrelated generic
discussion context. Ordinary `prepare_context` retains its existing behavior.

The caller must next call `HumanAssessmentPublisher.prepare_feedback` using the
same selector. It still reads the full retained inventory, validates every exact
selected source and then revalidates their complete bodies/metadata, and loads
the full current authoritative review evidence bundle. No evidence comes from
caller prose, truncated context, a cached PR observation or unselected history.
Tests compare the complete provider request (excluding only its remaining
timeout) and all inventory/bundle/feedback/authority inputs byte-for-byte with
ordinary preparation. A multiple-source control retains the full Unicode body
of each selected source. This route does not authorize ordinary discussion
inference, source grants, new attempts or consuming broker mutations.

## Bounded publication changes

For explicitly selected `shared-guidance-v1` reports, perform one initial live
source/latest-authority/PR fence in `publish_pending` instead of duplicating that
same triple around pure rendering in its caller. No mutable observation is
cached. Every subsequent page and final fence still performs fresh reads.

After a confirmed successful numeric-ID page POST in the compact report
transaction, GET that exact comment and require exact ID/body/App Bot owner and
PR association. The transaction then performs its mandatory final whole-report
scan, detecting missing, changed, duplicate and conflicting pages before the
last live source/latest-authority/PR fence and the caller's separate authority
publication fence. Default and standalone publication retain their full scan
readback. Negative lookup and unknown/lost-ack POST reconciliation are unchanged;
a direct lookup never establishes absence or a complete report.

## Exact evidence and remaining bounds

`PublicFeedbackContinuationTests.test_selected_cold_100_with_0_100_250_history_completes_original_budget`
uses actual local durable receipts and synthetic GitHub/provider transports.
All three accept at 34 original calls and finish at 56/59/60, respectively.
Exactly one selected full ID is resolved; all 99 other findings/prose remain
pending and approval is withheld. No new read/provider allowance, timeout,
reserved fence slot, enrollment authority or original attempt witness is minted.

`test_selected_context_keeps_identical_complete_provider_inputs` and
`test_selected_context_retains_complete_multiple_source_closure` guard inference
semantics and complete evidence. Scope, source/current PR/base/head/latest
authority races, competing generations, uncertain POST, direct owned readback
faults and final whole-report faults refuse without advancing authority.

The unchanged ordinary cold conversation route with 100/250 historical comments
still exhausts the original allowance. The explicit selected route's zero-history
prepared-context control now finishes at 54. Accepted replay still persists its
original charges/time and lacks enough allowance for complete delivery. Rich
250 authority publication, indexed feedback-history composition, actual
CLI/default remote store/broker activation, installed and deployed qualification
remain separate. No DecisionProvider or governance-policy change is proposed.
