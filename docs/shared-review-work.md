# Shared review work (experimental)

See [Review pipelines and customization](review-pipelines.md) for normal and
reply sequence diagrams, current storage bounds, lens behavior and configuration
examples. Unified evidence budgets do not expand those storage bounds.

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
    batch_prompt_bytes: 262144  # Preferred target; unqualified CLI admission is 48 KiB.
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
The CLI does not accept a self-declared qualification from YAML or a PR: larger
prompt admission requires trusted library-supplied capabilities for the exact
provider/model. This change qualifies no production model or hosted routing
profile.

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
64 HTTP reads, including snapshot fences, within 60 seconds and the remaining work deadline. A missing, truncated,
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

Richer eligibility uses payload version `2.0` within the existing
`reviewsensei:eligibility:v1` envelope. The facts document remains version `1`.
Both versions are read strictly; richer requirements cannot be downgraded by
changing the version. Malformed or unsupported latest authority never exposes
an earlier clean result. Structural JSON/schema failures permit one bounded
correction per batch within the shared retry allowance; citation, rationale and
reserved-marker failures keep the batch pending without a semantic retry.

Unified batches may return up to four closed `context_requests`: an assigned
fingerprint (reply) or supplied path (discovery), `kind: joint|discovery`, up to
eight canonical changed paths and a concrete reason bounded to 512 bytes. The
host validates references and paths; prompt text cannot authorize arbitrary
file access. Joint work uses one expansion wave, the same planner/executor and
the original tracker. Compatible completed batches retain their evidence and
request identities. A second wave, unknown path or oversized group remains
pending. Newly widened pending requirements are persisted in v2 authority.

A broader discovery request remains a separate mode. A trusted library host may
provide `broader_service` to `reassess` and receive its full `ReviewRun` in
`HumanAssessmentWork.discovery`; this consumes the same remaining envelope and
cannot recursively expand. Omission from that result does not clear the old
human inventory. The hosted controller publishes its new full result through the normal review
publisher only when trusted automatic-review policy is enabled. It retains the
exact prior finding identities and resolutions, unions newly discovered findings,
and preserves blockers, incomplete coverage and missing qualification. The new
result marker includes the source assessment authority digest; replays reconcile
that exact continuation and consult latest persisted eligibility. Current reply,
base/head and authority are checked again before mutations. Oversized combined
inventory or review framing fails before publication; incomplete discovery leaves
the request pending. A clean broader result cannot clear an omitted old finding.
The CLI requires explicit `github-reply --enable-broader-review` plus unified
mode, trusted automatic-review policy and writes. It uses configured stages,
categories and provider routing; no live hosted policy is enabled by this change.

`WorkRecoveryStore` is an optional host-owned v2 diagnostics store. Pass it via
`ReviewService.run(work_recovery=...)`, `reassess(recovery=...)`, or the hosted
composition seam. It requires an explicit private directory, a host-supplied
HMAC key of at least 32 bytes, and `artifacts="diagnostics"`. Its default is
`artifacts="none"`, which creates no directory and performs no reads or writes;
hosted use also requires the existing artifact flag. Both CLI entry points accept paired `--work-recovery-dir` and
`--work-recovery-key-file` options only in unified mode with diagnostics enabled.
The key file and directory must be private to the current user on POSIX; symlinks
are refused. No automatic key, new environment setting, artifact upload or
deployment is introduced.

The authenticated artifact retains exact evidence and normalized validated
receipts, not prompts or raw provider responses. It is sensitive diagnostics,
bounded to 2 MiB per artifact, eight artifacts/8 MiB per private run directory,
32 directory entries, six-hour default/24-hour maximum
expiry, atomic private-file writes, and the existing explicit seven-day hosted
artifact-retention policy when an operator separately uploads it. Nothing is
added to the session ledger. The host owns cleanup and should allocate one
private directory per run; expiry prevents reuse and does not delete files.
Oversized artifacts fail rather than truncate.
Authentication, full plan/evidence/source/policy identity, current semantic
validation, provider/routing request identity and expiry are required for reuse.
Consumed calls are saved before dispatch; cumulative bytes/retries and elapsed
wall time survive restart, so interruptions cannot mint a new deadline or call
allowance. Terminal rejected batches remain pending on restart; only interrupted
dispatches may resume. Restart also preserves the original retention expiry. The host supplies a
trustworthy, nondecreasing aware wall clock for age/expiry. Saved elapsed duration
plus wall-clock checkpoint age is rebased onto one local monotonic tick, preserving
the larger current duration; clocks need no common origin. A clock before the
saved timestamp rejects reuse. Persisting a prior monotonic origin would not be
portable across restarts.
Retained completed work may be republished after inference time is
exhausted only through the existing current-source/head/eligibility gates.

Deploy compatible readers before opting in. Unified transaction contexts add a
hashed work-policy identity; old readers reject that extension. Keep v2 writers
disabled until every reply/finalization reader supports them. Rollback stops
new writers and selects existing routing on compatible readers; preserve any
reader needed for durable records. Never reset a ledger, erase pending findings
or fall back to older eligibility to make an incompatible record usable.

Run `python scripts/check_review_reader_compatibility.py` from a checkout with
the immutable v0.6.16 commit `5a121dfe39fb978c7d71def0d08dc570aad83f1d`
available locally. CI fetches that exact commit before running the gate; a shallow
local checkout needs `git fetch --no-tags --depth=1 origin 5a121dfe39fb978c7d71def0d08dc570aad83f1d`
first. The gate fails when that source is unavailable; it never skips or
substitutes a newer reader. It compares that exact source and
the upgraded reader without network or worktree mutations. The old reader treats
trusted session comments over 16 KiB as missing; the upgraded reader recognizes
oversized terminal markers as integrity failures and refuses initialization.
Quiesce older readers/writers before expanded records exist. Old-reader rollback
is unsafe after such writes, independently of unified prompt evidence budgets.

The reusable hosted workflow uses one PR-scoped provider lane for full reviews,
replies and commands, including hosted/local backends, with cancellation disabled.
Replies keep distinct workflow-level identities. GitHub may replace queued jobs
because its concurrency group has one pending slot; replaced work grants no
assessment decision and stays pending. This is serialization, not a durable FIFO.

When upgrading or rolling back the reusable workflow, pause admission and let
queued and running provider jobs finish before switching workflow revisions.
The old `reviewsensei-provider-reply-<repo>-<pr>` group becomes the shared
`reviewsensei-provider-review-<repo>-<pr>` group. GitHub does not serialize work
across those two keys. Include delayed jobs, command/review/reply entry points
and both hosted/local backends in the drain; a consumer still pinned to the old
workflow can otherwise overlap a new run. Resume admission only when the active
entry points use the same group key. Keep current-head/source/eligibility fences
enabled throughout. This is a rollout prerequisite, not a migration performed
by this implementation.

Provider quality/capacity qualification, hosted pilots and any default or
consumer rollout remain separate phases. Broader publication and recovery need
the explicit trusted opt-ins described above. See [ADR 0067](adr/0067-shared-review-work-mechanics.md).
