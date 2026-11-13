# ADR 0138: Lossless lifecycle checkpoints with an immutable shared base

- Status: Proposed; implementation and exact-head qualification pending
- Date: 2026-11-13
- Decision owner/reviewers: ReviewSensei maintainers; independent source review
- Linked GitHub Issue: pending; user-directed G10/G14/G16 lifecycle qualification
- Linked PR: pending

## Context

A complete command must price every durable intent, possible stop, native effect,
confirmation and terminal checkpoint before its first business write. Pricing
each effect independently can admit a prefix without admitting its complete tail.
Repeating the entire finding journal in every immutable checkpoint can exceed the
existing single-page response bound even when the logical inventory is bounded.

Increasing capacities, deleting old checkpoints, dropping hidden obligations or
assuming that unknown native identifiers compress well are not acceptable fixes.
The ordinary full snapshot already retained by the authenticated session can
instead supply compression bytes to subsequent checkpoints in the same command.

## Decision

Introduce a separately versioned, closed representation for source-qualified
pending checkpoints. It contains the complete immutable base manifest and a
losslessly compressed complete target journal, with explicit target length and
digest. The dictionary derivation is versioned and deterministic: canonical
sorted unique string keys and values from the decoded base journal, bounded to
the final 32,768 bytes. The target remains the full journal, not a patch or a
partial inventory.

Only an independently authenticated full legacy snapshot may supply the base.
Dictionary checkpoints cannot be bases for other dictionary checkpoints. The
base's scope, content, producer and generation are checked; its generation may
be older than the current root. The current authenticated root selects the
checkpoint and commits its base manifest. The base contributes compression data,
not current authority, lineage, semantic progress or permission to dispatch.

Reconstruction checks encoded and decoded bounds, complete stream consumption,
canonical target JSON, target digest and length, followed by all existing journal,
native receipt and lifecycle validators. Missing, edited, foreign, recursive or
unreadable bases fail closed. A fresh current-root fence follows all dependent
reads, including when the outer checkpoint is inline. Existing v1 through v3
readers remain available; ordinary final snapshots remain self-contained.

The source-owned command plan commits the exact base manifest and pending target
documents. Only those exact documents receive exact compressed-size pricing.
Unknown outcomes retain conservative full-snapshot pricing. Each mutually
exclusive branch includes its own complete stop or success tail; admission uses
the maximum whole-branch resource liabilities, not the sum of incompatible
branches or an optimistic successful prefix.

## Capacity, funding and privacy

The existing base is retained rather than copied into new dictionary parts. All
old evidence and orphan allocations remain counted. Every additional dependent
read, validation, fresh fence, write and final checkpoint is priced under the
original operation budget before business effects. An inline outer carrier is
not evidence that its dependencies are read-free. Process-local authenticated
bytes may be reused only through the existing source-qualified custody proofs;
cold restoration must not assume that cache exists.

The 256-part, 200-allocation, 8 MiB retention, 512 KiB response and 8 KiB query
bounds do not change. Neither the lifecycle effect/byte limits nor protected
staging and terminal reserves change. There is no larger-history census, new
storage service, activation, permission change or automatic retry loop.

Compression adds no data to the journal and is not encryption. Existing data
handling rules still exclude credentials, prompts and raw provider captures.

## Qualification and consequences

Required coverage includes full original-budget command completion, one-short
funding refusal before business effects, cold restart from a pending checkpoint,
missing or changed base parts, foreign scope/producer, changed current root,
recursive base rejection, decompression overflow, trailing streams and target
tampering. The complete logical inventory, historical liabilities and native
receipts must survive every supported representation and crash boundary.

The representation adds a bounded dependency and decoding complexity. A final
self-contained snapshot limits ordinary future restoration cost but does not
erase earlier retained checkpoints. Offline size measurements establish design
feasibility only; they are not runtime qualification, a deployed rollout or full
closure of the residual-gap plan.
