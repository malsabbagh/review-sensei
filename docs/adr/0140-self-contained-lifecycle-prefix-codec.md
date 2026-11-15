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
retains the original 32-effect and 1 MiB limits. Legacy serializers and decoders
are unchanged. A separate bounded DATA assembly seam supports hypothetical
suffix sizing without accepting its semantics. No prefix data proves execution
custody, and no codec call grants dispatch, funding, smaller quotes or reads.

## Scope

This proposal covers only the pure representation and its strict decoder. It
excludes runtime selection, custody issuance, pricing, funding, publication and
live activation. The original v3 encoding remains byte-for-byte unchanged.

## Consequences

Runtime integration must authenticate exact immutable prefix bytes independently
at both pre-effect sizing and dispatch. Generic, dictionary and unsupported
multi-effect routes retain their original contracts. Existing committed v3
bytes must not be reconstructed with a different serializer. Runtime integration
must preserve the original 8 KiB reference and 32 KiB root limits and reject
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
producer replacement refusal. These checks do not qualify original runtime
prefix custody or a complete hosted journey.

## Rollout and rollback

The pure module is not selected by any runtime caller. Runtime integration needs
separate source review, original-prefix custody and exact-head qualification.
Before any v5 write, existing readers must support its explicit version. Rollback
must retain v5 decoding for already-written records; stop new v5 selection rather
than rewrite or discard committed history. No migration is performed here.

## Follow-up work

Authenticate the immutable prefix independently during sizing and execution;
retain the legacy serializer for committed dictionary-base reconstruction;
qualify all supported stop/loss outcomes and unchanged funding, root/reference
limits and reader contracts before enabling any writer.
