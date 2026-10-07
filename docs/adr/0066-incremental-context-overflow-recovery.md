# ADR 0066 - Incremental context overflow recovery

Status: Proposed
Date: 2026-10-07
Owners/Reviewers: Maintainers
Approved by: not applicable

## Context

[SiteVault PR 76 run 37562830019](https://github.com/malsabbagh/site-vault/actions/runs/37562830019)
used ReviewSensei 0.6.16 and failed before inference or publication with
`related paths exceed MAX_RELATED_PATHS`. Its valid stored 16-path baseline
and current derived 32-path context have 41 unique paths. Neither individual
input is malformed; their union exceeds the 32-path incremental limit.

## Decision

Validate each input normally, then detect union overflow before incremental
plan construction. Emit the closed `related-context-overflow` reason with
`fallback-full` coverage. Run the existing full current-diff review once with
no incremental plan, cached results, prior findings in discovery, or enlarged
resource limits. Preserve the prior baseline and exact current key for
publication admission, which keeps fallback findings human-adjudicated under
ADR 0060. The normal incremental path, including the 32-path boundary, is
unchanged. Invalid paths, oversized explicit lists and invalid confirmation
tokens remain input errors.

After validated full analysis, reconcile every prior finding through the
existing unique identity matcher. Omitted or ambiguous priors become
`uncertain` lifecycle records and force a partial result. Do not invent a
comment, evidence of resolution, or provider retry. Existing partial
checkpointing preserves the exact old history, charges one failed attempt and
withholds approval. Publication replay retains existing digest, generation,
head/base, ownership and idempotency fences without another inference call.

A complete refresh requires every prior to be uniquely represented and the
entire finding set to fit the existing two-finding durable bound. Preserve
each rediscovered prior's original resolution criterion and historical
blocking bit. Multiple comments claiming one prior, missing priors, or too
many findings produce partial results. A model downgrade of a historical
blocker also remains partial: rediscovery alone does not authorize retiring
its blocking obligation. Lifecycle overflow fails validation;
never truncate unresolved identities. A complete refresh whose path/evidence
projection cannot fit the durable envelope fails closed before checkpointing,
preserving old history and releasing the reservation with one failed attempt.
Partial provider results and provider failures cannot replace the baseline.

This deliberately provides no automatic evidence-confirmed retirement on the
single-pass CLI: omission requires evidence or operator adjudication under
the existing contracts. It does not reset round/attempt counters or change
approval, coverage, security, permissions, budget or exact-snapshot gates.

## Validation

Offline synthetic tests pin the exact 16 + 32 = 41 union, 32/33 boundaries,
duplicates, malformed inputs, normal incremental planning, full 47-file CLI
coverage, omission retention, original criterion/blocking preservation,
durable and lifecycle bounds, provider failure/partial output, human
adjudication, exact retry suppression and publication replay accounting.
Run the complete source/coverage, quality, schema, build, clean-wheel and
exact-head CI gates before rollout.

## Rollout and rollback

This draft changes source only. An ordinary approved package/workflow release
and consumer pin update are required before installed 0.6.16 or pinned `v5`
consumers can use it. No ledger migration or reset is required. Live consumer
reruns need separate authorization after rollout. Revert the change to restore
the original overflow refusal while preserving ledger history.
