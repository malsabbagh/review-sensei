# ADR 0140: Self-contained lifecycle prefix representation

Status: Proposed

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

## Consequences and pending integration

Runtime integration must authenticate exact immutable prefix bytes independently
at both pre-effect sizing and dispatch. Generic, dictionary and unsupported
multi-effect routes retain their original contracts. Existing committed v3
bytes must not be reconstructed with a different serializer. Runtime integration
must preserve the original 8 KiB reference and 32 KiB root limits and reject
values that do not fit; this representation does not promise universal inline
support at 32 effects. No live route is activated by this proposal.
