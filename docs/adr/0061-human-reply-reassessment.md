# ADR 0061 - Reassess human-review findings from authorized replies

Status: Proposed
Date: 2026-10-06
Owners/Reviewers: Maintainers
Approved by: not applicable
GitHub Issue: release request tracked in PR #212
GitHub PR: [#212](https://github.com/malsabbagh/review-sensei/pull/212)

## Context

A complete review can carry `needs_human` findings without proven blocking
findings. Placement keeps those uncertain findings in the review body, and
approval eligibility preserves the pending human-assessment flag. The old
conversation resolver considers only blocking inline roots; resolving a thread
does not reassess the pending human findings or refresh their eligibility.

## Decision

A generated `@sensei` reply from an authenticated OWNER, MEMBER or COLLABORATOR
can reassess the pending human findings in the latest App-authored review on
that exact head. The normal `github.writes` and `github.mentions` opt-ins apply.
A human explanation requests reassessment; it never grants approval directly.

The original publisher adds a bounded complete human-finding inventory to its
hidden eligibility document: exact base SHA, stable concern fingerprints, paths
and already-public finding text. The additive `human_review` field retains
schema version 1 and strict parsing. Existing documents without an inventory
remain readable but cannot clear pending human flags. Old readers reject the
new field and withhold approval rather than infer eligibility. More than 20
findings, oversized entries or an inventory over 24 KiB omit the inventory and
preserve withholding; no truncated list can stand for the complete result.

The omission behavior and concern-identity assumption above are superseded by
[ADR 0065](0065-complete-human-inventory-and-approval-retry.md): construction now
fails visibly before publication, colliding concerns retain separate assessment
identities, and a legacy missing inventory returns an actionable full-review
error instead of ordinary chat. Durable reassessment can also resume approval
after an interrupted runner.

The configured provider returns one strict bounded assessment per addressed
concern, including its fingerprint, addressed/dismissed/unresolved decision,
concrete rationale and literal excerpts from both the authenticated human reply
and the current diff for that finding's path. Unknown, duplicate or unsupported
citations fail closed. Bare dismissals, thanks, approval demands, unrelated
explanations and missing context stay unresolved. Diff evidence is read from
current PR files, never trusted from a historical inline comment hunk. The model
performs semantic reassessment; literal citation and identity checks establish
provenance, not a deterministic proof of semantic correctness.

After successful inference, the adapter rechecks source actor, membership,
association, source timestamp/body, exact base/head, same-repository non-fork
open/non-draft state and latest eligibility. It records a new App COMMENT review
with the refreshed inventory and source identity/evidence digests. Only the
human-assessment flag changes; the original result digest, blocking findings,
coverage, review status, qualification and auto-approval policy remain intact.
Partial assessments accumulate only their supported fingerprints, and every
other concern remains pending. The adapter uses the existing separate
`review_publish` broker capability; the issue-reply grant is never used to
submit an approval. No broker scopes or App permissions are broadened.

Finalization checks live unresolved blocking threads, exact PR identity and the
latest persisted eligibility again after the thread scan before APPROVE. A
newer same-head result or malformed newer record withholds rather than revealing
an older clean record. Duplicate eligibility markers fail closed. The existing
approval marker reconciles repeated APPROVE operations. The refreshed record
must be read back successfully; ambiguous publication does not grant approval.
No thread-resolution state, arbitrary reply text or `resolve=true` bit can clear
a human-review flag by itself.

The existing check policy remains intact. A complete auto-approve review uses
review events as its decision and already publishes a successful ReviewSensei
check; partial or incomplete evidence remains ineligible. The feature does not
recharge a full-review session round, alter its baseline, grant dispositions to
another head or bypass pause/review budgets. A later source review is a new
eligibility record and can reopen uncertainty; explicit maintainer dispositions
retain their existing separate session contract.

## Scope

This change covers bounded finding inventory, provider assessment validation,
authenticated conversation publication and approval finalization. It does not
change review checks, session baselines, dispositions, provider selection or
broker permissions.

## Consequences

New reviews expose already-public human findings in a hidden complete inventory.
Supported human explanations can remove uncertainty without another full review;
large or legacy inventories continue withholding. Model judgment can still be
wrong, so conservative prompts, provenance validation and the preserved gates
remain necessary. GitHub has no atomic compare-and-publish operation; bounded
freshness checks reduce races, and a later review can restore withholding.

## Alternatives considered

Clearing a flag from arbitrary text or thread resolution was rejected because
neither proves assessment. Always requiring another full review preserves the
old behavior but discards a useful human explanation. Persisting session-ledger
dispositions would grant broader cross-head authority than this feature needs.

## Validation and rollout

Synthetic tests begin with a real needs_human result and original publication,
then cover supported explanation to refreshed eligibility/APPROVE, partial
assessments, unrelated/bare replies, unauthorized/bot sources, source/head/base
and newer-result races, existing blockers, coverage, qualification, disabled
policy, malformed/duplicate markers and capability separation. No live reply,
approval, package/tag publication or deployment is performed by these tests.

Ship through the reviewed 0.6.14 package/workflow cutoff. Existing pending reviews
without inventories need a fresh review before the new flow can reassess them.
Rollback to the prior package withholds on the additive inventory. Publication,
Worker deployment, v5 promotion and live end-to-end verification remain separate
maintainer operations documented in the release runbook.

## Follow-up work

### Proposed JSON-syntax recovery amendment (2026-10-10)

The legacy `HumanAssessmentService.reply` permits exactly one fresh strict-JSON
correction after `json.loads` syntax rejection. The original application resource
tracker is required through preparation and both attempts; direct callers may
supply the same tracker. Calls, aggregate input/output bytes, configured output
tokens and remaining absolute timeout bound both attempts. The service retains
the existing 48 KiB request and 16 KiB response ceilings. The correction contains
only a fixed instruction after the unchanged original prompt, never malformed
provider output. Schema or citation failures do not qualify for correction.
Transport and explicit incomplete completion failures remain provider failures.

Diagnostics retain numeric parser geometry, response bytes and closed
correction/budget status only. They do not retain JSON parser documents or
messages, response excerpts, source text or exception chains. All decisions
continue through the original evidence and publication fences. The existing
batched executor's broader structural correction and transport retry policy is
unchanged; this narrower legacy bug fix reuses its resource tracker contract
without adopting that retry policy. No broker, persistence or approval authority
changes are introduced.

Validation uses synthetic public service/application and real adapter fake-HTTP
tests for one-call valid output, malformed then valid, repeated malformed,
lower/exhausted budgets, schema/citation rejection, incomplete envelopes and
length termination, safe CLI errors and zero writes on failure. Local source and
package qualification precede any separately authorized publication. Rollback
reverts the syntax loop and tracker call-site plumbing; there is no wire-state
migration. ADR status remains Proposed, and installed/deployed qualification is
follow-up work.

After separately approved publication, verify a fresh real needs_human review,
a supported explanation and a partial/unrelated reply against the released
workflow and package. Keep ADR status Proposed until maintainer review.
