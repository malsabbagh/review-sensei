# ADR 0113: Archive-backed provider narrative excerpts

- Status: Proposed
- Date: 2026-10-27
- Decision owner/reviewers: repository owner; Astra High
- Related issue/PR: [TaskAlong PR33](https://github.com/malsabbagh/taskalong/pull/33)
- Implementation: [source branch](https://github.com/malsabbagh/review-sensei/compare/main...fix/archive-backed-review-narrative)

## Context

A validated partial result can fit every input limit while its complete review
frame exceeds GitHub's 65,536-byte ceiling. Finding references, explanations,
eligibility, discussion instructions and future disposition reserves all count.
Existing compact rows and the default inventory reference do not always suffice.
Increasing the caller API allowance cannot solve this independent byte ceiling.

## Decision

After existing complete-profile fitting fails, the ordinary application may
confirm its already retained original result and render only the explicitly
non-authoritative provider narrative as an excerpt. This fallback is unavailable
to a publisher lacking independently selected transaction and retention context.

Confirmation reads complete App-owned result parts under the original shared
caller budget. It validates canonical bytes, raw payload digest, content digest,
transaction, repository, PR, base/head and original narrative. The ledger digest
must remain unchanged across confirmation. Ordered carrier identities are bound
into the confirmation receipt. Missing, foreign, changed or incomplete evidence
refuses; no replacement parts or new event identity are created.

The excerpt contains at most 1,024 original UTF-8 bytes, ends at a valid character
boundary, is labelled as an excerpt, and links every authenticated immutable
carrier with the exact content and payload digests. The complete original result
and narrative remain unchanged. Every finding explanation, classification,
coverage statement, index and authority marker keeps its existing representation.
The byte budget includes all labels, digests and links so the rendered overview
strictly shrinks. Mandatory archive framing that cannot shrink it refuses.

The complete new frame, links and all disposition reserves must still fit the
unchanged ceiling. If they cannot, publication refuses. This fallback grants no
approval, completes no partial coverage and authorizes no historical replay.

## Scope

Ordinary complete-profile presentation and read-only result confirmation only.
No schema, provider admission, budget, broker, workflow, deployment or legacy-root
enrollment change. The explicit trusted-host partitioned writer remains opt-in.

## Consequences

Large non-authoritative overviews no longer crowd out otherwise fitting findings.
Only overflowing publications incur confirmation reads. Lost or partial retention
acknowledgements remain uncertain and are never repaired by reissuing writes.
Small overviews, excessive finding prose and unavailable archives can still refuse.

## Alternatives considered

- Raise the frame or API allowance: rejected; they are independent limits.
- Omit finding prose or metadata: rejected; obligations must remain inspectable.
- Truncate the original result: rejected; retention and result identities remain exact.
- Activate the prototype partitioned writer: broader authority and rollout than needed.

## Validation

Synthetic public application and actual review-run regressions cover complete
finding retention, unchanged incomplete eligibility, one physical caller meter,
Unicode, missing/foreign/changed parts, altered confirmation fields, concurrent
ledger changes, exhausted budget, lost acknowledgement and remaining overflow.
Exact recovered public-incident payloads are exercised only in local evidence;
generated review output is not committed as a fixture. Source, clean-wheel and
exact-head CI/review evidence must be recorded before release.

## Rollout and rollback

Normal reviewed source PR and package qualification precede the sole publisher's
release. Existing result parts remain readable by old readers. Reverting restores
the previous fail-closed sizing behavior without deleting or migrating evidence.
No live TaskAlong recovery is implied by source or offline acceptance.

## Follow-up work

Reconcile any pending original execution before proposing recovery. Historical
roots without API accounting require a distinct guarded enrollment design and
positive terminal-effect/cessation evidence; this change does not bypass that gap.
