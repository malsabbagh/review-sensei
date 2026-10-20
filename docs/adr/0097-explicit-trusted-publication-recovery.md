# ADR 0097 - Explicit trusted caller publication recovery

Status: Proposed
Date: 2026-10-20
GitHub Issue: [#246](https://github.com/malsabbagh/review-sensei/issues/246)
Incident: [PR #316](https://github.com/malsabbagh/review-sensei/pull/316)
Owners/Reviewers: Maintainers

## Context

A committed analysis can retain its complete validated result while publication
fails. Ordinary reusable-workflow review retries correctly return
`publication_recovery_required`, clear stale runner output and emit no new result
or trusted admission documents. The package already supports publication-only
`--result` and `--recover-retained`, but the invocation-only caller exposes no
explicit recovery route. Repeating inference, fabricating OIDC/App credentials,
or silently treating an ordinary rescan as recovery is not acceptable.
This extends [ADR 0093](0093-default-publication-retention.md) and the
[ADR 0052 recovery amendment](0052-logical-review-transaction-across-analysis-and-publication.md).

## Decision

Add an optional manual `recover_publication` flag on the existing canonical
`operation: review` surface, with the independently selected original policy
commit and expected original head. Both reusable lanes retain their existing
writer concurrency, current policy and live PR guards. They require manual
dispatch, full immutable selectors, current original head/base and an original
policy commit in trusted default-branch history. They prepare the exact bounded
diff, skip provider credential/inference steps, and invoke the installed existing
publication CLI with `--recover-retained --reconstruct-contexts`.

The package reconstructs the default reusable caller's configuration and admission
key from frozen trusted source using existing canonical configuration, stage,
context and learning builders. It computes configuration/evidence/policy digests
and the original deterministic reservation independently of the result. The
authenticated committed ledger must match them exactly. Its original prior
baseline is reused only while available; an overwritten verification baseline
refuses and directs the operator to the existing original-context document route.
Unknown original overrides/custom configuration fail rather than being inferred.
This adds invocation data and an in-process context projection, not a new
authority envelope, transaction schema, capability or provider qualification.

All reconstruction ledger/partition reads and subsequent retained-result,
accepted-operation, retention and publication reads share the existing single
publication EvidenceReadBudget. The result crosses the existing checkpoint,
accepted content, broker-selected ledger, current-head, ownership and replay
checks. No analysis round is reserved or charged and no learning PR is emitted.
Mutable publication phase/generation reconciliation retains the existing logical
transaction identity rules; changed source/base/head is never rebound.

Canonical Python and Worker setup templates remain byte-equivalent. Freeze the
exact previously released caller for managed recognition, accepting no operator
edits to it. Existing callers need their own normal managed source update; a
reusable-tag move cannot add missing dispatch inputs. Worker template source
parity does not deploy a Worker or change broker issuance policy.

## Alternatives

An automatic fallback from ordinary review/rescan obscures intent and is rejected.
A custom production harness, local OIDC fabrication, PAT-as-App write, new broker
scope, enlarged budget or prefetched result outside the attempt budget is rejected.
Supplying the original result and context documents remains available when those
trusted files exist; reconstruction is a bounded additional route, not permission
to guess missing prior evidence or reanalyze the PR.

## Validation and rollback

Public CLI controls begin with actual checkpointed analysis and retained failed
publication, remove runner artifacts, prove ordinary retry refusal, then recover
without constructing a provider. Negative controls cover changed source, stale
head, malformed diff, missing source, changed accepted bytes and overwritten
original verification baseline. Both reusable lanes and exact managed caller
recognition have offline executable/static controls. Installed artifact and
composed real comment-port/history budget checks remain release gates.

The feature defaults off. Removing the manual caller flag and reconstruction CLI
route restores the prior explicit original-file recovery path without changing
stored ledgers, result parts, broker configuration or ordinary analysis behavior.
