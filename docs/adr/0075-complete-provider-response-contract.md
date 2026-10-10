# ADR 0075 - Complete provider responses and exact capability endpoints

Status: Proposed
Date: 2026-10-10
Owners/Reviewers: Maintainers
Approved by: not applicable
Related issues: [#238](https://github.com/malsabbagh/review-sensei/issues/238), [#244](https://github.com/malsabbagh/review-sensei/issues/244), [#245](https://github.com/malsabbagh/review-sensei/issues/245)
Related PR: pending; isolated follow-up to [#257](https://github.com/malsabbagh/review-sensei/pull/257)

## Context

Syntactically valid review JSON can arrive with incomplete or length-terminated
provider metadata. Parsing the JSON alone cannot establish a complete response.
A qualified context limit also needs to identify the exact selected endpoint and
adapter response policy; a provider name and mutable model alias are insufficient.

## Decision and scope

The Ollama adapter refuses any present `done` other than primitive `true`, any
present `done_reason` other than primitive `stop`, and malformed or negative
`eval_count`. A reported count above the intersection of the request and
constructor output limits refuses before returning text. These are nontransient,
sanitized provider failures, including when the response contains valid JSON.
Unified execution retains the charged call and leaves affected work pending.
This guard does not certify that an untrusted server reports usage honestly.

Library callers can explicitly construct a fixed-model Ollama adapter with
`require_completion_metadata=True`. This mode requires all four metadata fields
(`done`, `done_reason`, `eval_count`, `model`), the exact requested model and an
effective output limit. It refuses a changed endpoint/model/override policy
before transport. Only this mode advertises `bounded-complete-text-v1`.

Legacy construction defaults to `False` and continues accepting absent metadata;
it does not advertise that complete-response contract. Present unsafe metadata
still refuses. No CLI flag or public runtime factory activates strict mode or a
qualified capability in this change.

A `qualified=True` capability must bind provider, model, exact credential-free
endpoint and `bounded-complete-text-v1`. Discovery, reassessment and every shared
executor dispatch compare the actual adapter identity before admission. The
endpoint uses HTTPS, or HTTP on loopback, and contains no credentials, query,
fragment, control characters or parent path components. Comparison is exact;
there is no alias normalization or live discovery. Capability and strict adapter
work digests bind the policy so earlier receipts cannot silently gain it.
Legacy adapters without the contract retain their prior work identity.
The executor also refences identity after a durable dispatch callback. A conflict
there withholds transport and retains the already charged call and unknown output
reservation; it cannot refund or reopen the original allowance.
Budget wrappers expose the current underlying provider/model rather than copied
identity. Ollama revalidates current constructor and effective output limits at
dispatch, so negative, boolean, noninteger or oversized mutations refuse before
transport even after a durable host callback.

The contract name is a trusted adapter declaration, not process attestation or
independent proof of remote model/tokenizer identity. Offline token bounds and
finite probes do not establish a hosted alias-to-tokenizer mapping. Adoption
still requires reviewed mapping assurance (or an explicit expiring alias policy),
the installed adapter/producer artifact, and actual target runtime qualification.
The investigated candidate stays `qualified=False`. Producer delegation, first
admission authority, original-attempt restoration and richer writers remain
separate pending decisions and gates.

## Consequences and alternatives

Known truncated completions cannot clear obligations even if their JSON parses.
Servers returning unknown termination reasons fail closed. Legacy servers omitting
metadata continue working in their existing unqualified profile; they cannot
expand that profile through a qualified record lacking the new fields.

Requiring metadata globally would break legacy local servers and synthetic
adapters. Accepting valid JSON despite a length termination would confuse syntax
with response completeness. Trusting model-name equality alone would omit the
actual transport boundary. Neither alternative is selected.

## Validation

Synthetic public-adapter and service regressions cover incomplete/length/unknown
metadata, primitive types, exact output boundaries, missing strict metadata,
model/endpoint changes, sanitized failures, pending service coverage and zero
dispatch on capability mismatch. Existing qualified fixtures declare explicit
synthetic endpoints/contracts. Complete credential-free source and installed
package gates and independent bounded review are required before integration.
No live provider prompt is needed for this guard verification.

## Rollout and rollback

Integrate the guard as a reviewed reliability change; do not qualify a model or
enable richer workflows through it. Strict construction remains opt-in. Rollback
must continue withholding qualified capability use if its endpoint or response
contract cannot be established. Existing authoritative receipts are not rewritten.

## Follow-up

Review hosted tokenizer/runtime mapping, explicit producer authority and exact
installed artifact behavior separately. Qualify the full public lifecycle and
conservative response accounting across crashes before any adoption claim.
