# ADR 0070 - Preserve full review findings when baseline metadata overflows

Status: Proposed
Date: 2026-10-09
Owners/Reviewers: Maintainers
Related work: user-authorized recovery of [PR #231](https://github.com/malsabbagh/review-sensei/pull/231) hosted review [run 37883954080](https://github.com/malsabbagh/review-sensei/actions/runs/37883954080)
Implementation PR: pending

## Context

The 0.6.17 hosted review failed after inference with `complete review evidence
exceeds the persisted bound`. That message conflates incomplete coverage,
finding-count overflow and encoded-byte overflow. The original result was not
retained, so the actual overflowing dimension cannot be reconstructed from
that run. Its reservation was cleared and one failed attempt was recorded.

The writer permits two findings, an 11,264-byte baseline, a 12,288-byte history
and a 20,480-byte session record. The actual baseline allocation may be smaller
because the record reserves room for publication and subsequent lifecycle
growth. The schema also bounds identity/path/symbol fields to 256 characters
and defect kinds to 128 characters. The three-finding reader allowance supports
legacy history; it is not spare writer capacity. These limits protect bounded
comment-ledger storage and future transitions.

## Decision

Measure the complete canonical JSON baseline before checkpointing, including
all findings. Report every exceeded capacity dimension through a closed
sanitized diagnostic document: fixed reason labels, counts, lengths and byte
limits only. Never log paths, configuration values, finding prose, raw provider
output or credentials in this diagnostic.

When otherwise complete validated analysis cannot fit, retain every comment
and lifecycle in the result, explicitly change its status to `partial` and
use the existing partial transaction checkpoint. This withholds approval,
charges one failed attempt for the current head, clears the reservation,
retains the exact result digest and keeps the prior complete convergence
history unchanged. It does not complete an initial/verification round. The
publisher may deliver the full finding inventory through its existing partial
publication path; all head, generation, identity and configuration checks
remain required. Invalid/incomplete evidence is not a capacity exception and
does not gain authority through this fallback.

Optional baseline/cache projections that omit findings now clear both
`complete` and `coverage_complete`, just as projections omitting paths already
do. A narrowed projection cannot masquerade as complete evidence in current
or legacy readers.

Opt-in checkpoint evidence retains the complete pre-checkpoint validated
result in a distinct `publishable: false` diagnostic envelope. It is not a
recovery artifact or trusted publication input. After overflow, its original
complete-analysis digest intentionally differs from the ledger-bound partial
publication result digest. Publication/recovery
uses `review.json` and its trusted contexts, never this evidence envelope.
Diagnostics uploads retain this file even on checkpoint failure, only when
`github.artifacts: diagnostics`
is already enabled. `artifacts: none` uploads no content. Stale runner files
are cleared before inference. Evidence and numeric diagnostic schemas are
additive; no durable session fields or storage limits change.
Failure uploads additionally require a fresh per-step cleanup marker, so an
early provider-step failure cannot retain stale files from a reused runner.

## Scope and consequences

This change covers the provider-neutral baseline serializer, transactional
review CLI and both reusable workflow review lanes. It leaves PR #231's release
automation independent. Overflow no longer discards all finding publication
merely because a metadata cache is too small, but it still cannot approve or
establish a reusable complete baseline. Existing partial-review admission and
per-head failure budgets apply; publication retry uses the bound partial result
and does not rerun inference or charge a second attempt. Operators may need to
split a large change or request authorized continuation after its budget is
exhausted. The original failed run's exact dimension remains unknown.

## Alternatives considered

Raising the two-finding limit or byte limits lacks a lifecycle capacity proof
and may strand sessions in later transitions. Truncating findings while
claiming completeness loses obligations. Treating a complete provider pass as
approval authority despite missing durable evidence violates convergence
guarantees. External full-baseline storage would require new authentication,
retention, schema, reader migration and availability contracts; it remains
separate future work. Retaining only the old operational error prevents any
finding publication and hides the dimension needed to diagnose it.

## Validation

Exercise exact byte boundary and one-byte-over, two versus three findings,
field-width overflow, incomplete-evidence refusal and incomplete projection
flags. Through the CLI, verify the full finding inventory survives, approval
is withheld, prior history is unchanged, failure budget is charged once,
publication can finish, same-head replay does not infer again, and persistence
or evidence-write errors clear the reservation. Verify both workflow lanes
retain opted-in diagnostics after checkpoint failures without relaxing their
publication guards. Run the complete repository gate, clean-wheel suites,
legacy-reader qualification and independent source review before rollout.

## Rollout and rollback

Merge only after review and exact-head CI. Publish a separately approved patch
release through the normal package/tag process, then promote the approved
runtime channel before any authorized hosted rerun. This PR does not publish,
merge, tag, promote channels, deploy the Worker or rerun the hosted review.
Existing durable schemas and partial result semantics remain readable by the
legacy reader. Rolling back removes improved handling/diagnostics but leaves
valid partial transactions and unchanged complete history.

## Follow-up work

After the approved runtime is published, rerun the specific hosted review and
inspect its exact diagnostic dimensions and publication gate. If complete
convergence for larger finding inventories is required, design a separately
reviewed full-evidence persistence contract with readers-first rollout and
bounded lifecycle capacity evidence.
