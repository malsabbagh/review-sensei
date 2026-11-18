# ADR 0140: Self-contained lifecycle prefix representation

- Status: Proposed
- Date: 2026-11-15
- Decision owner/reviewers: ReviewSensei maintainers; independent source review
- Linked GitHub Issue: pending; user-directed bounded lifecycle qualification
- Linked PR: [#462](https://github.com/malsabbagh/review-sensei/pull/462)

## Context

A bounded journal retains prior confirmed effects as well as one new effect.
Treating every old scalar as independently variable increases its inline wire
size. An external dictionary would introduce separate reads and custody.

## Decision

Propose `finding-lifecycle-packed-v5`, containing a compressed exact confirmed
prefix and its guarded publication pair descriptor, plus an independently
legacy-v3-packed remainder. The initial boundary is all effects except the last;
the first three confirmed pair effects must be in the prefix. Prefix request
hashes are omitted only when exact canonical recomputation agrees. The four pair
hashes are reconstructed from the validated prefix. All other logical fields,
including lifecycle, receipts, history and uncertainty, remain in the remainder.

The public pure codec validates full logical journals on pack and unpack and
retains the original 32-effect and 1 MiB limits. Legacy serializer bytes and
legacy decoder branches are unchanged. A bounded DATA assembly supports hypothetical
suffix sizing without accepting its semantics. No prefix data proves execution
custody, and no codec call grants dispatch, funding, smaller quotes or reads.

## Scope

The current serializer selects the smaller of the unchanged legacy v3 encoding
and eligible self-contained v5 encoding. Selected-root reconstruction recognizes
only an exact committed hash of one of those representations; it never replaces
old v3 commitments with v5 bytes. Dictionary bases and historical readers accept
the explicit self-contained version through the same full semantic validation.

Generic capacity bounds remain v3. The narrower prefix bound is available only
inside the original one-effect preflight: either the existing detached finalizer
observation proves the retained document, or the existing historical execution
lease independently decodes its exact selected inline carrier. Exact old effects
and the complete pair must match; synthetic later branch effects do not qualify.
The suffix retains the original maximum-allocation projection. Source seals are
checked before invoking codec exports. This issues no new execution lease,
changes no funding or read allowance, and performs no live activation.

## Consequences

Runtime integration authenticates exact immutable prefix bytes independently
at detached sizing and actual request preflight. Generic, dictionary and unsupported
multi-effect routes retain their original contracts. Existing committed v3
bytes must not be reconstructed with a different serializer. Runtime integration
preserves the original 8 KiB reference and 32 KiB root limits and rejects
values that do not fit; this representation does not promise universal inline
support at 32 effects. No live route is activated by this proposal.


## Alternatives considered

Increasing limits or dropping history is excluded. Compact scalar tables alone
leave repeated history growth. External dictionary snapshots require additional
reads and custody. An exact self-contained prefix avoids those dependencies,
without asserting that every maximum-size journal fits inline.

## Validation

New synthetic pure tests cover full reconstruction at 26 and 32 effects,
original v3 byte stability, pending/rejected/confirmed suffixes, digest and pair
conflicts, malformed shapes, bounded canonical compression and callback-free
producer replacement refusal. Integration controls cover exact legacy/current
manifest matching, dictionary bases, no-growth representation selection, generic
maximum fallback and zero-callback substituted exports. Callback-free custody
controls do not fabricate an execution lease. These checks do not qualify
original runtime prefix custody or a complete hosted journey.

## Rollout and rollback

Runtime integration still needs exact-head journey qualification. Readers accept
the explicit version before the current writer selects it. Rollback
must retain v5 decoding for already-written records; stop new v5 selection rather
than rewrite or discard committed history. No migration is performed here.

## Follow-up work

Qualify all supported stop/loss outcomes and unchanged funding, root/reference
limits and reader contracts on the combined candidate before publication. Keep
the live cutover disabled and preserve all existing recovery authorization holds.
