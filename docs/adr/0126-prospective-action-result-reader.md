# ADR 0126: Read prospective one-part action results without enabling writes

- Status: Proposed
- Date: 2026-11-10
- Decision owner/reviewers: ReviewSensei maintainers; independent source review
- Linked GitHub Issue: pending; user-directed G34 qualification
- Linked PR: none; local reader-only slice

## Context

G34 requires complete command results to fit both their carrier and the enclosing
session root. The prospective carrier design uses one authenticated immutable
evidence part. Codec validity alone establishes neither root authority nor
settlement. Writer capacity, startup pricing and recovery integration require
separate qualification; this reader does not resolve those requirements.

## Decision

Recognize `caller-requests-v3` as a prospective readable value. Keep
`ACCOUNTING_VERSION` at V2. Select receipt encoding independently: V1 retains
dictionary receipts; V2 and V3 use pairs. Pending receipts, identities, deadlines
and liabilities remain intact. Within-run version changes remain forbidden.
V3 reserve, dispatch, confirmation, owner-claim, activation and rollover paths
refuse as reader-only.

`read_current_action_result` is an explicit, unconnected read interface. It loads
the current owned root through the native ledger rather than accepting an
arbitrary caller-provided root. It derives expected bindings from that root and
the independently authenticated producer, then validates a terminal action's
exact intent and outcome. V3 accepts either the existing inline representation or
exactly one partition and one result item. The part contains the complete action
envelope, not another reference or an inferred completion statement.

The partition reader authenticates native producer and PR ownership, complete
part framing, order, lengths, binding and digests. Before decompression it retains
the existing 8,192-byte encoded-payload ceiling; decoded expansion remains capped
at 2 MiB. No storage, payload, HTTP or request allowance increases.

The caller supplies `ActionResultReadContext` with the ledger's original budget.
Its identity and maximum attempted-root count are fixed. Failed root reads consume
an attempt; repeated authenticated roots and exhausted depth refuse. The context
does not create or reset an HTTP budget, deadline, protected tail or reservation.
It does not follow ancestor references. The existing runtime result reader still
refuses V3, so this interface cannot silently activate an unpriced recovery path.

## Scope

Pure accounting/schema read compatibility, the explicit current-result reader,
writer refusal guards and provider-free tests. No result-part serializer or POST,
CLI activation, startup-plan change, ancestor adapter, provider or live recovery
integration is added. Original G34 writer candidates remain separate.

## Consequences

New code can inspect a deliberately supplied prospective current-root fixture
without authorizing another action. Missing, foreign, malformed or altered parts
refuse; a pending root with an orphan part cannot supply terminal closure. Lost
read responses keep their original pending charge. Reading result data never
certifies settled ancestry, released uncertainty or latest-checkpoint authority.

## Alternatives considered

- Replacing the runtime reader immediately would add unpriced startup reads and
  recovery behavior. Keep the prospective interface explicit instead.
- Accepting an arbitrary `SessionRecord` argument would delegate root provenance
  to an unconstrained caller. This slice authenticates the current native root.
- A new immutable read-plan boundary would affect mutable-history freshness and
  publication readers. It is broader than this isolated carrier-reader seam.

## Validation

Focused tests cover native root/part reads, full summary retention, V1/V2 wire
stability, V3 write refusal, substitutions, missing/corrupt/foreign evidence,
encoded and decoded limits, context depth/cycles, failed reads and original-budget
exhaustion. Nearby caller-accounting and action tests remain required. These
checks qualify only the reader slice, not a production V3 writer or recovery path.

## Rollout and rollback

Reader-first source preparation only. Existing writers continue emitting V2 and
existing V1/V2 records retain their exact interpretation. An older reader rejects
V3 rather than guessing. No stored record is upgraded, rewritten or deleted.
Rolling back this reader makes prospective fixtures unsupported; it creates no
permission to replay their work or erase their retained evidence.

## Follow-up work

Qualify pre-effect root/storage capacity, one-part creation and confirmation,
startup/read pricing, complete retention accounting and the unchanged bulk-success
and overflow-refusal oracles before any writer integration. The recovery owner
must separately authenticate each archived root and each result relied upon by a
bounded settled-ancestor adapter. Do not pass an archive or orphan directly to
this current-root reader or infer settlement from its data.
