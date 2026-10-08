# ADR 0067 - Shared discovery and human reassessment work mechanics

Status: Proposed
Date: 2026-10-07
Owners/Reviewers: Maintainers
Approved by: not applicable
Related issue: none; user-authorized implementation of the unified review plan
Related PR: pending draft creation

## Context

Full review has optional file/hunk orchestration. Human reassessment instead
loads all pending finding patches into a 12 KiB selector and makes one call.
Neither the 16 KiB conversation diff field nor the 48 KiB reassessment prompt
can represent an arbitrary complete multi-file inventory. Enlarging a selector
alone does not account for JSON escaping, instructions, output, model context,
transport, storage, retry or deadline limits.

ADR 0065 requires complete file patches because v1 findings have no authoritative
hunk locations. It prohibits substituting guessed fragments for required
evidence; batching complete file groups satisfies that requirement.

## Decision

Add one opt-in provider-neutral work mechanism. `EvidenceSnapshot`,
`EvidenceRecord` and `EvidenceBundle` bind immutable evidence to exact snapshots.
`WorkRequirement` identifies an atomic obligation, `plan_work` packs rendered
requests deterministically, and `execute_plan`/`execute_call` enforce admission,
transport and structural retries, total prompt/output bytes and deadlines.
Full discovery and human reassessment supply different prompts and validators
through `discovery_work` and `reassessment_work`. They use the same packing and
execution loops. Legacy routing remains available for the qualification period.

Discovery prefers complete file records and may fall back to exhaustive parsed
hunks. A single oversized hunk remains unprocessed. Reassessment requires every
complete file in an obligation to fit one batch; oversized groups remain
pending. The GitHub loader verifies patch syntax and additions/deletions counts,
never truncates a patch, and uses bounded adaptive pagination.

`advanced.review_work.mode: unified` opts into the mechanism for both entry
points. The packaged default is `legacy`, meaning the existing work routing;
it is unrelated to the retired legacy convergence-policy selection. No consumer configuration, release, hosted channel, permission or default
changes are made here. The reusable workflow source serializes provider work
for each PR across modes and backends without cancelling an active sibling.

128 KiB diff / 256 KiB prompt are configurable preferred batch targets, not
model-capacity claims. Unknown providers keep a 48 KiB serialized work prompt;
reassessment output remains at most 16 KiB. Qualified capabilities require a
trusted exact provider/model identity and a qualified UTF-8-byte upper-bound
token estimator. Context admission reserves model output, framing and a safety
margin. No production provider is newly qualified by this change. Adapter
output caps reduce reassessment packing and per-request output/deadline controls
can only tighten adapter settings. Existing public/run ceilings still intersect
all targets. Total work defaults to eight actual dispatches, two transport and
two structural retries, 120 seconds, 2 MiB prompt bytes and 1 MiB output bytes.
Every failed dispatch consumes a call. The existing
`advanced.resources.max_provider_calls` tightens both opt-in CLI routes.

Every requirement is present exactly once as completed or pending. Missing,
failed, malformed, skipped or unprocessed work cannot clear a finding or make
discovery complete. Aggregation validates each response against its batch and
the publisher revalidates normalized decisions against the original evidence
map. Evidence is not concatenated back into `ConversationContext`.

Continuation reuses only matching batches and rendered request identities after
fresh mode-specific validation, with the same snapshot, source authority,
provider/model, resource policy and original tracker. It cannot mint a fresh
call/deadline allowance. A controller may supply additional complete evidence;
changing an obligation invalidates its old assessment while unchanged valid
batches can be reused. Closed `context_requests` permit one host-validated joint or broader-discovery
wave. Joint work uses authoritative complete changed-file groups; discovery has
its own prompts and result. A configured trusted `broader_service` consumes the
same tracker and publishes through the normal full-review publisher while
retaining prior findings, blockers, coverage and qualification. Hosted CLI use
requires an explicit flag and existing automatic-review/write opt-ins. Source
reply, exact snapshot and latest persisted authority fence the new publication.
Its continuation marker binds the original authority; normal result replay
cannot reconstruct eligibility in a way that drops retained obligations.

An optional HMAC-authenticated v2 work receipt store supports cross-process
continuation. It saves consumed admission before dispatch and retains normalized
receipts and exact evidence, not prompts or raw responses. Reuse requires the
current exact plan, evidence, provider/routing/request identity, budgets, expiry
and semantic validation. Calls, retries, bytes and elapsed wall time survive
restart. It requires explicit diagnostics, a private host directory and key;
limits are 2 MiB/artifact, eight artifacts/8 MiB total, 32 directory entries and
six-hour default/24-hour maximum expiry. The host owns cleanup. No artifact is
automatically uploaded or placed in the public ledger.

## Scope and contracts

V1 finding fingerprints, inventory fields, published eligibility prefix and
ordinary conversation limits remain intact. Reader support for a v2 human
inventory adds explicit `required_paths` to findings. Such groups require cited
evidence from every related path, bounded to eight paths per finding, 64 unique
paths and the existing 20 findings / 24 KiB inventory. Trusted controllers may
construct v2 inventories; normal `from_result` publication still writes v1.
Required paths are never inferred from finding prose or a guessed hunk.

The richer eligibility document uses payload `schema_version: "2.0"` inside
the recognized `reviewsensei:eligibility:v1` envelope. Result-side facts retain
version `1`, and the nested human inventory has version `2`. Old readers see
the newest envelope and reject its payload, withholding approval instead of
revealing an older clean record. New readers require the payload version to
agree with the presence of explicit requirements; no v1 downgrade is accepted.

Unified work policy is hashed into cache identity and, when transactions are
used, the existing orchestration context. New readers accept old contexts and
the optional `work_policy_digest`. Old readers reject that extension rather
than silently reusing a transaction with different semantics. Ledger limits
from ADR 0066 are a separate compatibility concern and are not expanded here.

Only bounded identity hashes and the existing inventory/resolution authority
are published. Evidence patches and normalized batch replies remain in memory by default.
The explicit private diagnostics store may retain them; no raw provider
responses, prompts, credentials or source text are added to session history or
public diagnostics. All existing App/human authorization,
freshness, idempotency, coverage, qualification, thread and blocker gates remain
authoritative. The finalizer still decides approval.

## Consequences

Multi-file reassessment no longer depends on fitting every required patch into
one small conversation field. Provider failures can retain validated decisions
without clearing unfinished findings. Both modes have one mechanical admission
and execution contract and bounded, testable failure behavior.

Small batches may repeat the human explanation, instructions and shared files;
this trades additional bounded calls for complete evidence and validation.
Unqualified providers, very large individual hunks/files and cross-file groups
may still produce partial work. Full discovery may miss relationships spanning
separate batches; paths or prior findings are never silently treated as fixed.
The typed continuation controller widens scope without adding a second batching
engine; a second wave stays pending.

## Alternatives considered

- Raising the 12 KiB selector or 16 KiB context field: insufficient prompt,
  output, transport and provider accounting; rejected.
- Heuristic finding/hunk selection: no authoritative v1 location; rejected.
- Separate reply batching engine: duplicates admission and retry semantics;
  rejected.
- Always rerunning full discovery for a reply: unnecessary work and different
  semantics; reassessment remains scoped to authoritative known findings.
- Enabling the mechanism or a purported 128k context window by default: lacks
  provider qualification and hosted/reader rollout evidence; deferred.

## Validation

Offline regressions cover ~77 KiB and >128 KiB complete-file evidence, an
oversized file, exhaustive hunk splitting, atomic cross-file groups and citations,
missing/truncated/conflicting evidence, malformed outputs, all-dispatch budgets,
adaptive pagination bounds, deadline/output controls, cache/budget identities,
continuation without reset, stale source/base/head and aggregate publication.
Run the complete lint/type/schema/unit/coverage and installed wheel/sdist gates
before promotion. Synthetic fixtures contain no private evidence or live tokens.

`scripts/check_review_reader_compatibility.py` archives the immutable v0.6.16
commit from local objects and runs its real readers in a separate process. It
checks unchanged v1 identity/wire documents, v2 rejection without older-authority
fallback, opt-in transaction rejection and session reader behavior. The pinned
reader skips trusted comments over its 16 KiB size limit and reports missing
state. The upgraded reader instead blocks initialization for oversized trusted
terminal authority. This historical gap requires quiescing old readers/writers
before expanded writes and retaining upgraded readers during rollback.

## Rollout and rollback

1. Land compatible readers and keep existing work routing as the default.
2. Qualify provider/model/routing/token estimates and assess finding recall on
   both modes, including cross-file and oversized fixtures.
3. Pilot explicit unified mode only after every participating analysis,
   publication and recovery reader supports the work-policy extension.
4. Keep v2 inventory writers disabled until every delayed finalizer and reply
   reader supports v2 and the source of required paths is authoritative.
5. A rollout decision, released package, consumer/channel migration and any
   default change require separate operator review.

Before v2 writes, rollback selects legacy work routing on compatible readers.
After opt-in transaction or v2 writes, preserve readers capable of interpreting
those records, finish/reconcile in-flight work and stop new writers. Do not roll
back to a reader that rejects durable records, erase history, reset counters or
fall back to older eligibility to recover. Ambiguity remains action-required.

## Follow-up work

Provider capacity/quality qualification and controlled hosted pilots remain
promotion gates. Reader rollout must precede v2 writers, including delayed
finalizers. Explicit receipt retention and key management remain host policy.
GitHub's one-pending-slot concurrency behavior is not a durable queue; a cancelled
queued reply remains pending and can be explicitly retried. No consumer rollout,
production qualification, release or deployment is included in this change.
