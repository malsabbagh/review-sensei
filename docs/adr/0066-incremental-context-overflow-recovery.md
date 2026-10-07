# ADR 0066 - Incremental context capacity and overflow recovery

Status: Proposed
Date: 2026-10-07
Owners/Reviewers: Maintainers
Approved by: not applicable

## Context

[SiteVault PR 76 run 37562830019](https://github.com/malsabbagh/site-vault/actions/runs/37562830019)
used ReviewSensei 0.6.16 and failed before inference or publication with
`related paths exceed MAX_RELATED_PATHS`. Its valid stored 16-path baseline
and current derived 32-path context have 41 unique paths. Neither input is
malformed; their union exceeds the former incremental ceiling.

The former 4096-byte history and 8192-byte record budgets were internal sizing
policies, not GitHub or model context-window limits. ADR 0053 measured a
3202-byte checkpoint; the 1024-byte history-framing reserve rounded a measured
846-byte shape. Those measurements establish fit, not globally optimal sizes.

## Decision

Use the existing per-request metadata item ceiling (64) for related path names.
This is a bounded policy choice, not a mathematical optimum or a guarantee that
64 arbitrary paths fit. It also stays within the existing 64-path coverage
appendix, so the newly admitted related set is visible there. Source-document,
instruction (65536 bytes), prompt, provider-call and total-work limits do not
change. Runtime path syntax remains strict; durable paths remain limited to
256 code points. Byte and field checks further restrict incremental admission.

Choose a 12288-byte history ceiling, with the existing 1024-byte framing reserve
and an 11264-byte maximum baseline allocation. The record ceiling becomes
20480 bytes: history plus the former 8192-byte record allowance for surrounding
state. Retain the named comment allowance at twice the record ceiling (40960).
These are coordinated internal policies; they are not derived from an external
quota. Expanded records remain small bounded metadata, never source, prompts,
provider output or rendered finding text. Individual field and finding limits
are unchanged, including two written findings and three progress markers.

Compare 8 and 12 KiB using `python scripts/measure_incremental_capacity.py`.
All six fixtures start from complete baselines that fit the old allocation.
They carry two prior obligations and count path evidence separately in reviewed
and related arrays. The 52-character path shape represents the longest path in
the existing repository-style checkpoint fixture.

| Related union / path shape | Complete baseline bytes | Previous policy | 8 KiB history | 12 KiB history |
| --- | ---: | --- | --- | --- |
| 32 / short ASCII | 1935 | incremental | incremental | incremental |
| 41 / short ASCII | 2097 | fallback required | incremental | incremental |
| 41 / 52-character ASCII | 5889 | fallback required | incremental | incremental |
| 55 / 52-character ASCII | 7429 | fallback required | fallback required | incremental |
| 64 / 52-character ASCII | 8419 | fallback required | fallback required | incremental |
| 41 / mixed short and 100-é names | does not fit | fallback required | fallback required | fallback required |

Thus fallback/incomplete-evidence requirements fall from 5/6 fixture cases to
3/6 at 8 KiB and 1/6 at 12 KiB. This is offline sizing qualification, not a
production fallback rate, recall measurement or cost forecast. Legacy writers
can narrow evidence and require fallback on the following round; count overflow
requires immediate fallback. New writers detect both before incremental work.
The largest ASCII chunk-name note measures 3506 UTF-8 bytes; the unchanged
instruction/prompt checks still include caller instructions and other context.
Direct CLI incremental and full routes each make one provider call; exact
retries make none. No tokenizer-derived token or currency estimate is claimed.

## Actual record allocation and lifecycle safety

Before planning and checkpointing, `checkpoint_baseline_capacity` serializes
a conservative superset of future analysis/publication record shells. It
preserves actual dispositions, retained grants, identities, created/expiry
timestamps and long legacy timestamp spellings. It reserves maximum-width
counters, per-head attempts, held/committed reservation ids, broker ownership,
transaction identities/result digest/longest phase, normalized update timestamp,
record digest and the nested history key/framing. Including mutually exclusive
fields deliberately over-reserves rather than admitting an unsafe phase.

History capacity is `min(12288, 20480 - serialized_nonhistory_bytes)`; baseline
capacity additionally subtracts the 1024-byte history framing reserve. Canonical
JSON uses the actual escaping policy; history nests as an object, not an escaped
string. The same path in two evidence arrays costs bytes twice. Unicode, retained
grants and dispositions may reduce the allocation. A later operator command
still validates its entire resulting record before CAS; it cannot force a write
past the bound. The 512 KiB GitHub response cap is unchanged, and the broker
receives bounded attestations/digests rather than this history object.

Every incremental candidate must preserve its prior findings and all reviewed
and related evidence within that allocation. A valid union exceeding the count,
byte or durable-field limits emits `related-context-overflow` and `fallback-full`
with no incremental plan. Invalid paths, oversized explicit lists and malformed
confirmation tokens remain input errors. Derived sibling context retains its
existing explicitly bounded heuristic; it is not source coverage.

Run the existing full current-diff review once without cache reuse or injected
prior discovery findings. Keep the exact old baseline/current key for admission,
preserving ADR 0060 human adjudication and head/base/ownership/budget fences.

After incremental or overflow analysis, reconcile every prior through the
existing unique matcher. Omitted, ambiguous, multiply claimed or downgraded
historical blockers become uncertain/partial and hold approval. Partial
checkpointing preserves exact old history and charges one failed attempt;
publication replay cannot reinfer. Rediscovered priors retain original criteria
and historical blocking bits. A complete checkpoint requires all findings and
all evidence to fit; no unresolved identity or evidence path is silently removed.
If even full evidence cannot fit, fail closed before replacement/publication,
release the owned reservation and preserve the old history. The single-pass CLI
does not manufacture evidence-confirmed retirement from omission.

## Compatibility, rollout and rollback

The closed version-1 field shape is unchanged; newer readers accept existing
records verbatim. Expanded count/byte bounds are a reader compatibility change:
old installed/pinned readers reject records above 32 related paths, 4096 history
bytes or 8192 record bytes. Rejection is authoritative unreadable state, never
permission to reset counters, reinitialize, duplicate a marker or truncate it.

Quiesce shared-ledger writes, upgrade every reader (analysis, publication,
commands and recovery, including installed/standalone/hosted entry points), then
enable writers producing expanded records. A separately approved package and
workflow release plus consumer pin updates are required. Mixed old/new readers
are unsupported after expanded writes. Rollback must retain an expanded-capacity
reader for those ledgers; reverting source alone can strand them. No migration,
ledger reset, tag movement, release, deployment or live consumer rerun is part
of this draft.

## Validation

Tests pin the exact 16 + 32 = 41 incremental case, count and canonical byte
boundaries, malformed versus valid overflow, Unicode/escaping and duplicated
evidence, normal incremental behavior, retained findings/dispositions/grants,
owned reservation/publication transitions, old-reader refusal without reset,
new-reader schema/storage round trips, complete-evidence refusal, approval
holds and provider/publication replay accounting. Run full source/coverage,
quality, schemas, build, clean installed-wheel and exact-head CI gates.
