# ADR 0117: Bound native inline publication with confirmed COMMENT groups

- Status: Proposed; implementation and qualification, no activation
- Date: 2026-10-28
- Decision owner/reviewers: repository owner; Astra xhigh
- Linked issue: user-directed complete recovery incident; no separate issue
- Linked PR: https://github.com/malsabbagh/review-sensei/pull/390
- Depends on: ADRs 0115 and 0116

## Context

A valid 1-MiB accepted result with 250 inline findings can exhaust the existing
ten-request adaptive reader after retrieving 141 complete comments. Total bytes
alone do not certify an unindexed mixture of native object sizes. A failed
response prefix is not authority, and a successful publication response does
not supply a durable input-ordinal to native-comment mapping after a crash.

The independent review recommended complete bounded COMMENT review groups,
then one final authority review. This adds visible reviews and a durable native
staging protocol. It does not enable the separate body-prose partition writer.

## Decision

Prepare an immutable source/result/transaction-bound deterministic plan retaining
every full inline explanation, location, original instance and command guidance.
Select at most 8 groups with at most 32 roots per group, using sorted round-robin
encoded cost. Each group is a COMMENT review with a bounded versioned part frame
and complete maintainer guidance. Part frames contain no eligibility marker.

Add a closed provider-neutral `publication_staging` session value bounded to 4 KiB.
It records the plan hash, original transaction/result, attempted prefix and exact
confirmed native receipts. Persist attempt before dispatch. Allow at most one
unconfirmed group. Unknown, absent, cancelled or failed observations never permit
a replacement POST, a subsequent group or the final frame. A later exact positive
read may confirm the same original attempt. Receipts and plan are immutable.
Reserve complete future staging growth alongside baseline, queue and decisions.
Each part and final attempt carries its own durable random claim token. Storage
ACK reconciliation can repeat that exact proposal; a second stale caller mints
a different claim and cannot inherit permission to dispatch the first attempt.

The final review must require a union-aware eligibility version. It can become
authority only after complete exact group reconciliation and a fresh closing
head/base/ownership/root-membership fence. Existing single-review formats keep
their original replay readers. An older or unaware reader must withhold approval
instead of interpreting an optional manifest as complete publication.

Keep numeric review IDs and receipt hashes in the 4-KiB session frontier.
Resolve opaque part node IDs freshly in the opening review metadata census,
separately from root metadata. Reserve their full encoded width in that census;
do not derive opaque IDs from an undocumented global-ID encoding. Root opening
and middle metadata share the two smallest complete body packets. Closing uses
complete numeric review membership and the fresh root/main-body projections.

Use purpose-specific native projections. Complete group reads select full body,
path, original/current line and commit, numeric comment/parent-review identity,
numeric Bot identity, reply parent and update metadata. Omit unbounded diff hunks
and presentation URLs. Source-bound installed provenance must independently
certify old fixed review heads, root/thread/review associations, exact execution
union and mutation targets; fresh native association and ceased-census checks
remain mandatory. Head mismatch alone cannot authorize narrowing. Conversation
and legacy report-conflict readers retain full old reply liability and bodies.

Distinguish explicit review/check heads from API-selected reply-associated review
heads. The complete review allocation reserve must include possible implicit
review objects from native replies. An observed human reply is not qualification
of the historical hosted REST endpoint; its count, encoded bytes and server-side
head selection require independent source/API evidence before registration.

One shared native walk counter includes opening metadata, complete body packets,
middle occupancy metadata when needed and fresh closing metadata. The original
ten requests, 512-KiB responses, 8-KiB requests, item and deadline limits remain.
Physical dispatches, including failed/unknown requests, also consume the caller's
original allowance. No nested reader or later preparation can mint capacity.

## Scope

This is the guarded recovery/default complete-inventory publication route.
The production activation registry and provenance catalog remain empty. Source
component tests do not qualify or activate a live recovery. Native readback,
suppression, approval, cleanup, thread resolution and conversation discovery all
need the composed reader contracts before rollout.

## Consequences

Large reviews can span several nonauthoritative COMMENT reviews followed by one
main review. Durable staging adds bounded ledger CAS operations that require
caller admission before the first effect. Unsupported current occupancy or
historical source/target evidence refuses before effects. Cancellation retains
the original incomplete transaction and every unknown native charge.

## Alternatives considered

- Retrying smaller arbitrary prefixes cannot use a reliable native size index.
- A terminal-only size hint cannot recover the initial unknown publication.
- Pointer-only inline comments would remove confirmed inline guidance.
- Raising limits, truncating explanations or dropping findings violates scope.
- Pending reviews plus GraphQL mutation batches require a different cancellation
  and pending-review protocol; COMMENT groups are the narrower selected path.

## Validation

Qualify source maxima for Unicode label casing/escaping, prose, all finite
formatter branches, full-length finding IDs and selected object metadata.
Serialize actual opening/middle/body/closing envelopes at maximal supported
occupancy. Packet arithmetic alone is not a lifecycle certificate.

Exercise original 250-item mixture in arbitrary API order, duplicate/foreign/
edited/missing objects, every stored/absent/delayed ACK frontier, receipt write
uncertainty and cancellation. Prove all 13 old issue allocations and interrupted
current staging discovery. Complete heavy 14-hold and 80-candidate/23-hold public
journeys, individual decisions, verify and next ordinary review with the physical
guard active. Repeat on direct/rebuilt installed artifacts, supported platforms
and the paired Worker, then obtain independent composed review.

## Rollout and rollback

Reader-first source support does not enable a producer or recovery registration.
Keep fresh production disabled until complete source/effect/capacity and authority
gates pass. No release, live recovery or merge exception follows from this ADR.
After a new-format attempt exists, retain its original format, plan, receipts,
charges and incomplete state on rollback; never reinterpret it as a legacy POST.

## Follow-up work

Finish whole-walk admission and consumers, wire the durable grouped writer and
union-aware final frame, qualify the installed physical journeys, independently
review the final immutable artifact, and publish only after those gates pass.
