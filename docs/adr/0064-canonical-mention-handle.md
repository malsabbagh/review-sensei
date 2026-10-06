# ADR 0064: Canonical mention handle

- Status: Proposed
- Date: 2026-10-06
- Decision owner/reviewers: ReviewSensei maintainers
- Linked GitHub Issue: User-requested feature; no issue created
- Linked PR: Pending draft creation

## Context

The product name is ReviewSensei, while existing callers, commands and published
findings direct users to `@sensei`. A runtime-only alias cannot pass the installed
caller's old event filter or the broker's maintainer-command grammar.

## Decision and scope

Make `@reviewsensei` canonical and accept ASCII case variants such as
`@ReviewSensei`. Retain `@sensei` with its historical casing rules. Update the
parser, trigger resolver, command broker, current installer templates, current
caller/example and user-facing prompts/docs together. Preserve command separator
and reason bounds, first-mention behavior, actor authorization, bot rejection,
exact-head checks, mention/write policy and bounded conversation contexts.

## Consequences and alternatives considered

Replacing the old alias would break installed callers and existing conversations.
A runtime-only alias would leave commands and caller event routing incompatible.
Supporting both requires a coordinated caller/runtime/broker rollout. Frozen
previous caller bytes allow safe managed upgrades; operator-edited callers still
require a focused workflow patch.

## Validation

Shared Python/Worker parity fixtures cover every command, alias/casing, boundary
nonmatches, invalid reasons and rejected legacy spellings. Caller fallback tests
exercise both handles through real embedded resolvers. Conversation tests exercise
top-level and inline contexts, and installer tests reject modified old callers.
Run full Python coverage, quality/contract/package gates and Worker tests/types,
then monitor the draft PR's complete CI matrix.

## Rollout and rollback

See [the rollout guide](../mention-handle-rollout.md). Promotion and consumer PRs
are separate actions. No immutable release, permission or App-name changes are
part of this PR. Roll back the product and consumer alias changes together if
needed; `@sensei` remains available throughout.

## Follow-up work

Promote the reviewed product/runtime/broker version and update each inventoried
consumer caller after separate authorization. Prove real hosted commands and
conversations before claiming production compatibility.
