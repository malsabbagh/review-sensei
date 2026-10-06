# ADR 0065 - Complete human inventory and retryable approval

Status: Proposed
Date: 2026-10-06
Owners/Reviewers: Maintainers
Approved by: not applicable
GitHub Issue: follow-up to the human reassessment state demonstrated on [PR #216](https://github.com/malsabbagh/review-sensei/pull/216)
GitHub PR: [#222](https://github.com/malsabbagh/review-sensei/pull/222)

## Context

Lifecycle concern fingerprints omit prose and line locations so a concern can
survive movement across review rounds. Distinct human findings can share one
fingerprint. ADR 0061's unique-inventory validator correctly rejects duplicates,
but construction swallowed that rejection and omitted the complete inventory.
The hosted reply path then generated ordinary chat, which could not change the
persisted human-review flag. A separate interruption after durable reassessment
but before approval also left no pending findings to trigger another assessment.

PR #216's published review has the human flag without an inventory. Duplicate
short finding identifiers suggest the collision path, but its raw provider
artifact was not retained. Synthetic regression evidence establishes the code
path; it does not reconstruct that historical artifact.

## Decision

Keep the existing lifecycle identity for unambiguous human concerns. For each
collision group, hash a domain-separated canonical validated v1 comment
with its concern fingerprint to produce a separate assessment identity. Provider
ordering does not change identities. Only identical validated v1 comments coalesce;
distinct text or locations remain independently assessable. Persisted inventories
and provider decisions still reject duplicate, unknown and conflicting identities.
Runtime-only admission fields do not change assessment identity; approval facts
still retain their authority. Existing inventories keep their original identities
and need no migration.

The complete inventory must satisfy the existing limits: 20 distinct findings,
2 KiB per finding, 24 KiB total and an exact base SHA. Construction failures are
explicit, and the original review publisher validates before any check/review
mutation. No truncation or silently omitted inventory represents a complete
human review. Repeated initial publication also carries the exact base into its
eligibility construction.

An authenticated mention with a latest App review declaring pending human
findings but no inventory fails with an actionable request to rerun a full review
for the current head. Malformed or contradictory latest records also fail
explicitly; they cannot reveal an older clean record. Preparation rejects stale
base/head and changed or unauthorized source rather than falling through to
ordinary chat. Missing current evidence produces the bounded outcome below. Legacy documents remain readable by other
approval paths and continue withholding.

No available hosted artifact binds the full legacy finding set and original base
to its result digest. Review-body prose, truncated RS IDs, inference responses
and `resolved:true` chat bits are insufficient authority. This implementation
does not infer or repair legacy inventory from them. A fresh review is the safe
recovery path; direct live-ledger edits are outside this change.

Persisting a supported assessment still changes only the human flag and inventory
resolutions. On a retry, an exact persisted refresh can re-enter the existing
finalizer. A newly prepared mention with a fully resolved inventory skips model
inference and retries finalization directly. Both paths require the existing
separate review capability, current source authorization, exact base/head and
latest persisted eligibility. Finalization checks all existing static facts and
live threads, rechecks PR identity and persisted state, and reconciles the same
approval marker. A newer same-head result or stale base/head wins. Retry creates
neither another reassessment COMMENT nor a second approval in sequential replay.
The source reply is reconciled through the existing conversation publisher
before finalization, so retries reuse the reply marker and new mentions receive
an acknowledgment rather than a false `already_replied` result. `ReplyResult`
retains the source receipt status and separately reports finalizer status and
diagnostic for initial and resumed assessment. The generated-reply CLI keeps the
receipt on stdout and emits the approval outcome on stderr. A withheld approval
or finalizer exception is never described as successful approval.

## Bounded reassessment evidence and diagnostics

The released 0.6.14 reply on this PR exhausted its 12 KiB diff context on earlier
files and supplied no patch for the pending finding's path. This makes every
accepted decision fail the strict evidence check; without retained raw output,
it does not establish the actual rejected field of that provider response.

Human reassessment uses a separate current-files selector. Validated pending
paths are deduplicated and sorted, and their entire supplied API patches are
admitted first within the existing 12 KiB UTF-8 budget, including headers and
separators. Missing/blank, conflicting or individually/collectively oversized
required patches produce an insufficient-evidence result. Inventory stores no
authoritative hunk locations; selecting a fragment would misrepresent the
available evidence. Optional current patches may use remaining budget only when
their path header fits completely. Historical inline hunks are never substituted.
Ordinary conversation selection remains unchanged.

On insufficient evidence, the application skips inference and review-write
capability exchange. It reconciles a deterministic source acknowledgment with no
decisions through the same authorization, latest-authority and freshness fences.
No eligibility or approval is changed. `ReplyResult.assessment_status` and
`assessment_diagnostic` report this outcome separately from source delivery, and
the CLI emits it on stderr. Direct service callers also reject missing pending
file evidence before inference. Missing evidence never authorizes accepted
provider decisions, including callers that bypass service inference.

Provider response rejection reports only a closed, source-free reason code:
invalid JSON, reply/decision fields or values, text bounds, reserved marker,
unknown/duplicate identity, short rationale, or human/diff evidence mismatch.
The public error message suppresses payload-bearing exception chains. Provider
JSON remains the closed body/assessments shape; model content cannot set a reason
code. Evidence requirements and all context/provider budgets remain unchanged.
There is no corrective provider retry in this decision.

## Scope

This decision covers construction and publication of complete human inventories,
authenticated exact-head reassessment and retry of the existing approval
finalizer. It does not authorize live state repair or alter release configuration.

## Alternatives considered

Coalescing every lifecycle collision would lose distinct findings. Weakening the
persisted uniqueness validator would make decisions ambiguous. Truncating an
oversized inventory would misrepresent completeness. Reconstructing legacy state
from public prose lacks the original full findings, base and digest authority.
These alternatives are rejected in favor of complete validated identities and
explicit recovery through a fresh review.

Extending the ordinary conversation loader with priority/completeness flags would
couple general replies to reassessment admission. The separate selector keeps
complete required-patch admission local. Heuristic hunk selection is rejected
because the inventory lacks an authoritative location, and enlarging budgets or
weakening quote validation would change existing privacy/evidence fences.

## Consequences and limits

New human reviews remain reassessable even when lifecycle identities collide.
Inventory validation failures prevent publication and require a bounded complete
review. Legacy missing state remains pending until a fresh review establishes a
complete inventory. Successful reassessment and approval converge across runner
interruptions without another model decision.

No permissions, provider selection, evidence checks, coverage policy, security
policy, budgets, session baselines, publication fences or release configuration
change. Literal excerpts still establish provenance; semantic reassessment still
depends on the configured model. GitHub has no atomic compare-and-publish API;
freshness checks and marker reconciliation preserve the existing race limits.

## Validation and rollout

Synthetic tests cover collision identity, bounds, actual original review
publication through authenticated reply/reassessment/one approval, partial
collision decisions, malformed/contradictory/legacy authority, capability
separation, interruption/replay and stale/newer-result races. Existing approval,
coverage, qualification, blocking-thread and authorization tests remain required.
Additional regressions cover late pending files behind overflowing docs, multiple
required paths, UTF-8/header/separator boundaries, absent/binary/conflicting and
collectively oversized patches, no-inference/no-mutation insufficient outcomes,
accepted-decision bypass rejection, and safe diagnostics with synthetic payload
sentinels.

This is a source change only. Package publication, released workflow/v5 promotion,
deployment and live verification require separate authorization. After that
rollout, legacy missing-inventory cases require a fresh exact-head full review
before reassessment. Rollback withholds on colliding or missing inventories.

## Follow-up work

Maintainers review this proposed decision and authorize any package/workflow
rollout separately. After rollout, verify reassessment on a fresh exact-head
review and rerun full reviews for legacy missing-inventory cases. The failed
0.6.14 mention must not be replayed against the old runtime. After package/channel
rollout, a fresh exact-head full review and new authorized mention establish
current inventory/evidence before verifying the repaired path.
