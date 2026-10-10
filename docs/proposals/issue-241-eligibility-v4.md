# Compact eligibility-v4 reader proposal

Status: Conditionally reviewed reader-first prototype. No new writer, root,
shared schema registration or rollout is enabled. Depends on A v1.1 and B
instance-v1 / visible-prose-v1 / D feedback-v1. Consumer integration and complete
consuming-grant/resource/fence qualification remain prerequisites for activation.

## Closed root shape

The exact JSON fields are:

```json
{
  "schema_version": "4",
  "head_sha": "40 lowercase hex",
  "result_digest": "64 lowercase hex",
  "facts": "existing closed ApprovalFacts schema 1",
  "immutable_generation": "integer 0..2147483647",
  "activation_generation": "integer 0..2147483647",
  "human_inventory": "closed A evidence manifest",
  "visible_prose": "closed A evidence manifest",
  "human_resolution": {
    "schema_version": "1",
    "inventory_digest": "64 lowercase hex",
    "resolved": ["unique complete 64-hex instance IDs, at most 250"]
  }
}
```

This example describes types, not literal schema-valid values. No extra fields,
unknown versions, implicit defaults, alternate placements or partial objects
are accepted. Mutable queue generation remains in A's queue/session slot and
C's operation callbacks; it does not rename immutable inventory generation.
Activation generation advances on root replacement and never appears in a
part's immutable binding. Generation authority is supplied by the authenticated
conditional host, not inferred from the root alone.

Both manifests have the exact same trusted A binding: repository, repository_id,
PR, base/head, policy/configuration digests, immutable generation and producer.
Only purpose differs: human-inventory versus evidence. Document schema is A's
closed `1.0`. Expected values come from trusted host/root context, never from the
manifest under examination. The root head/result must equal that context.

## Closed complete evidence payloads

The human-inventory payload has exactly:

```text
schema_version: "1.0"
kind: "human-inventory"
result_digest: 64 lowercase hex
inventory_digest: 64 lowercase hex
inventory: complete immutable PendingHumanReview.inventory_document schema 3
```

The inventory digest is SHA-256 of canonical bytes of the nested inventory.
The A manifest digest binds the entire envelope, including the exact result.
Its item_count equals the complete nested human finding count. Pending/resolved
state is excluded from immutable parts; restore only through the matching
compact resolution object. Existing IDs and original obligation metadata remain
unchanged. Rediscovery cannot implicitly attach historical resolutions to new
metadata. Newly assembled result generations stage new result-bound envelopes
even if a retained inventory's content digest remains unchanged.

The visible evidence payload has exactly:

```text
schema_version: "1.0"
kind: "visible-prose-receipts"
result_digest: 64 lowercase hex
inventory_digest: matching nested human inventory digest
finding_count: integer 0..250
parts: ordered array of closed visible-prose-v1 receipt documents, at most 32
```

Each receipt has exactly interface, placement, index, review_id, head_sha,
producer_id, sha256, bytes, instances and paths, as defined by B's strict
VisibleProseReceipt parser. Placement is only review-body. Indexes are contiguous;
numeric IDs are unique; heads/producers equal trusted context; aggregate full
body bytes are <=1 MiB; each is <=65,536 bytes and <=64 unique literal paths.
Instance IDs are unique across all parts. Their count equals finding_count and
the visible manifest item_count. Every inventory ID must occur in the visible
set; optional/blocking explanations also remain in the visible set. Empty prose
requires zero findings and zero receipts. The trusted result supplies the exact
complete expected visible instance set/count, rather than trusting a count alone.

A digest is integrity only. Each part is an ordinary COMMENT review with no
eligibility markers. Authenticated fresh GET of **every** numeric review checks
configured Bot login and numeric account, exact PR URL/head, COMMENTED state,
full UTF-8 bytes/hash and review-body placement. Both manifest reads, metadata,
writes and final readbacks/fences share A's original 64-dispatch/60s budget.
Charged adapters are not charged twice. Missing/edit/move/foreign/ambiguous
objects refuse the complete root; a subset never authorizes resolution.

## Parser/resolver proposal

Extend the owned parser through keyword-only explicit seams:

```text
ReviewApprovalEligibility.from_dict(
    value,
    *,
    partition_reader=None,
    expected_context=None,
    visible_reader=None,
) -> ReviewApprovalEligibility
```

Legacy calls remain byte-compatible and preserve stored IDs. Version 4 requires
all three supplied seams. `expected_context` is authenticated host context with
the complete A snapshot/binding, exact result digest, immutable/activation
generation, actual independently owned numeric root ID, complete expected
visible instance set, actual framing/future allocation and original shared
EvidenceReadBudget. `partition_reader` is A's already charged authenticated
resolver receiving manifest, expected_binding and budget as keyword-only values.
`visible_reader` receives the complete receipts and that same budget. Neither
can be selected by untrusted content. The finalizer checks context budget object
identity before scanning and exact numeric root/App identity on the selected
latest candidate. Parser callback success must return None, not a false status.

The parser validates closed root shape/finite bytes first, validates both closed
manifests and exact bindings/counts, reconstructs both complete envelopes,
validates nested inventory/result/digest, restores compact resolution, proves
complete visible identity coverage and fresh bodies, then checks persisted human
facts against actual pending state. Only then does it return an eligibility
object. Evaluation still requires live blocking-thread, PR/head/base and source
fences. Source/operation receipts and conditional root replacement remain C/D/A
requirements. No helper infers authority from metadata or source prose.

`approval_eligibility_from_body` forwards these explicit trusted seams. Every
latest-root scanner must select the newest owned authority candidate **before**
parsing; malformed, missing-part, unsupported or unresolvable newest authority
withholds. It never searches backwards for a valid older clean root. Legacy
immutable readers must reject a newer v4 root in mixed history.

## Root growth and finite admission

Total framed v4 root ceiling: 32 KiB; A retains its 8 KiB ceiling for each
manifest. This is an additional domain bound, not host support. Preflight
uses maximum-width part IDs, all 250 possible full resolutions, complete
navigation/summary/marker base64 framing and the actual host/root allocation,
including retained transaction/attestation/progress/queue growth where applicable.
If a shape does not fit all future resolutions and actual allocation, refuse
before mutation. Whole visible receipt documents (measured 27,824 bytes for 250)
are partitioned rather than directly embedded. Existing complete prose stays
visible and old authority stays retained after partial failures.

The initial a754527 stage/read seam retains its raw schema-3 inventory format.
The discrete amendment adds inventory_result_document,
inventory_from_result_document and stage_result_human_inventory, leaving raw
prototype reads unchanged. Eligibility v4 accepts only the closed result-bound
envelope. PartitionedEligibilityRoot stores immutable read-backed references;
its serializer reserves all resolutions and maximum activation-counter width.
V4 finalization explicitly withholds in this prototype, even when all static
facts and inventory resolutions are clean. The coordinator owns
shared public schema registration and reader/call-site rollout. No writer
activation until current and immutable-reader checks, fresh source/visible/root
fences, grant composition, retention/resource R12 and fault/concurrency gates pass.
