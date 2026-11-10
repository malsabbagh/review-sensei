# ADR 0127: Authenticate bounded observed action-result ancestry

- Status: Proposed
- Date: 2026-11-10
- Decision owner/reviewers: ReviewSensei maintainers; independent source review
- Linked GitHub Issue: pending; user-directed recovery qualification
- Linked PR: none; local unconnected reader slice

## Context

[ADR 0126](0126-prospective-action-result-reader.md) authenticates a current
native root's action result. Passing arbitrary archives to that reader would
erase its provenance boundary. Recovery preparation also needs bounded access
to results linked from archived roots, with each original identity intact.
Neither carrier integrity nor a terminal-looking value settles historical work.

## Decision

Add the explicit, unconnected `read_ancestor_action_results` interface. It accepts
the configured native ledger, its identical original read budget/context and a
target run ID. It accepts no starting record or archive reference. It loads the
native root once and follows only each authenticated node's `previous_record`,
through the existing `read_previous_run` ownership, root/run, binding and
historical-receipt checks. The target must be an ancestor of that observed root.

For every terminal action result returned, derive the expected producer, source,
head, configuration, original owner/intent and outcome bindings from that node.
Share a private decoder with the current-root reader. The current-root interface
continues to load its own native root; the legacy runtime reader continues to
refuse V3. V1 dictionary receipts, V2/V3 pair receipts and all V3 runtime write
refusals remain unchanged. Existing one-item/one-part, encoded 8,192-byte and
decoded 2 MiB limits apply independently to each result.

Return canonical immutable root/result bytes in newest-to-oldest order, anchored
to the root initially observed. Anchor result bytes are not resolved or relied
upon. A root without an action has an explicit absent action result; its review
payload is not thereby authenticated. Pending archived actions refuse under the
existing retained-binding contract. Pending native anchors remain observations,
not completion proof. No orphan part supplies a missing result or terminal marker.
Failure returns no prefix and never refunds a read.

Count attempted roots, including failed loads, against the same downward-only
context depth. Reject repeated root hashes and repeated run IDs. Bound the total
canonical anchor, archive and decoded result output to at most 2 MiB, with an
optional lower caller limit. This is a separate output bound, not an assertion
that encoded retained-storage limits also bound expanded memory.

Use the identical budget, existing transport, allocations and protected tails.
Never construct another allowance, consume a fence reservation, release another
phase's hold, refund uncertainty or reconstruct a budget from stored claims.
Base `EvidenceReadBudget` retains its fixed absolute deadline. `CallerRunBudget`
retains caller allocations/holds and each physical request's original deadline;
it has no aggregate traversal deadline. This reader creates none. Lost reads
retain their actual request identity, deadline and charge.

## Authority and integration boundary

This is observed data, not settled ancestry, latest-checkpoint authority, a
writer lease, original source-text or executable authentication, current actor
permission, or permission to replay/close/enroll work. Preserve original claims;
do not replace them with current configuration. An intent's `source_root` need
not equal the nearest archived predecessor. A later root advance does not change
the returned observation's anchor. No final root GET is claimed to prove writer
exclusion. Missing original context/native placements or stronger freshness
requirements still need their independent contracts.

No runtime caller, recovery enrollment, startup plan or writer is connected.
Future integration must separately fund the complete native census/traversal and
result reads from the original allocation, qualify transitive retention and the
appropriate live authority/freshness contract, and preserve all historical
obligations and uncertain effects. A caller can exhaust its allowance during
this read; refusal is not qualification of an end-to-end recovery lifecycle.

## Validation and rollback

Provider-free native fixtures cover mixed accounting versions, each original
binding, archive/reference/result substitutions, pending/actionless nodes, lost
reads, depth/run cycles, exact aggregate bounds, spent allowances, fixed-budget
expiry and root advancement during traversal. Nearby reader/writer refusal tests
remain required. Fixtures grant no production authority. Removing this explicit
reader makes the new preparation unsupported; it does not rewrite any record or
permit replay. No release, historical recovery or activation is included.
