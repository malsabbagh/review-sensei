# Shared review work (experimental)

`advanced.review_work.mode: unified` selects one deterministic evidence planner
and bounded executor for full discovery and tagged human reassessment. Existing
work routing is still the packaged default. This setting is separate from
convergence-policy selection and does not change provider qualification or any
approval gate.

```yaml
schema: 1
advanced:
  review_work:
    mode: unified
    batch_diff_bytes: 131072
    batch_prompt_bytes: 262144
    max_total_prompt_bytes: 2097152
    max_total_output_bytes: 1048576
  resources:
    max_provider_calls: 8
```

The YAML values are preferred targets intersected with public, resource and
provider bounds. 128 KiB of diff is not a 128k-token model context window.
Unknown providers retain at most 48 KiB of serialized work prompt; reassessment
responses remain at most 16 KiB. JSON escaping, finding text, instructions and
correction reserve count toward admission. A trusted library integration may
pass `ProviderCapabilities` after qualifying an exact provider/model and the
UTF-8-byte token upper bound, framing, output reservation and safety margin.
This change qualifies no production model or hosted routing profile.

The shared run envelope permits at most eight actual provider dispatches, two
transport retries, two structural retries and 120 seconds. Failed dispatches
consume calls. `advanced.resources.max_provider_calls` tightens both opt-in CLI
routes. Adapter output limits and remaining deadline are tightened per request;
adapter settings never expand. Total prompt/output accounting is separate from
per-batch admission, GitHub transport and durable ledger capacity.

Discovery prefers complete file records and may split oversized files into
exhaustive authoritative hunks. One oversized hunk stays unprocessed.
Reassessment requires complete current file patches for every finding path and
does not select guessed hunk fragments. Patch counts, syntax, conflicts and
enumeration are checked. The GitHub loader retains its 512 KiB response and
1,000-item bounds, tries pages of 100/50/25/5/1 entries, and permits no more than
32 requests within the remaining work deadline. A missing, truncated,
oversized or failed requirement remains pending.

Aggregate reply evidence stays outside the 16 KiB conversation field. Each
accepted batch and finding is mapped to immutable exact-snapshot evidence;
publication revalidates citations, the source reply, base/head and latest
eligibility. Partial decisions clear only those exact known findings. Ordinary
chat limits, authorization, coverage, thread, blocker and qualification gates
remain in force.

V1 inventory serialization and finding identities remain intact. V2 readers
also accept explicit complete `required_paths` groups, bounded to eight paths
per finding, 64 unique paths, 20 findings and 24 KiB of inventory. Cross-file
decisions require citations for every related path. Normal publication still
writes v1; required paths are never inferred from prose. Existing work routing
refuses pending v2 groups rather than treating the primary file as sufficient.

The library continuation seam can add complete evidence and revalidate/reuse
unchanged completed batches with the original `ResourceBudgetTracker`. It
rejects a fresh tracker, changed source authority, snapshot or resource policy.
There is no automatic hosted broader-review controller or new cross-process
batch store in this implementation slice. Existing persisted inventory
resolutions and publication-only transaction recovery remain authoritative.

Deploy compatible readers before opting in. Unified transaction contexts add a
hashed work-policy identity; old readers reject that extension. Keep v2 writers
disabled until every reply/finalization reader supports them. Rollback stops
new writers and selects existing routing on compatible readers; preserve any
reader needed for durable records. Never reset a ledger, erase pending findings
or fall back to older eligibility to make an incompatible record usable.

Provider quality/capacity qualification, hosted pilots, automatic justified
broader-context requests, persistent batch recovery and any default or consumer
rollout remain separate phases. See [ADR 0067](adr/0067-shared-review-work-mechanics.md).
