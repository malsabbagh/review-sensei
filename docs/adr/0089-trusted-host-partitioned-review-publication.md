# ADR 0089 - Trusted-host partitioned review publication

Status: Proposed
Date: 2026-10-19
Decision owner/reviewers: Maintainers
GitHub Issues: #240, #241, #242, #244, #269
Pull Request: pending
Approved by: not applicable

## Context

The public ordinary `ReviewPublisher.publish` refuses rich 250-finding inventories
before mutation because its legacy inventory/path/growth allocation cannot fit.
Existing A immutable evidence, B complete prose staging and v4 readers can retain
the complete domain, but did not provide an authenticated conditional writer.
An independently owned root ID arrives only after a real remote POST/readback;
it cannot be manufactured to satisfy an allocation-only preflight.

## Decision and scope

Add explicit `ReviewPublisher.publish_partitioned` with a required trusted host.
The ordinary writer and every default CLI/hosted route remain unchanged. The
host supplies independently retained snapshot, repository/numeric producer,
policy/configuration digests, immutable and activation generations, exact result
digest, complete expected visible instance set, actual allocation and the SAME
original `EvidenceReadBudget`. A plan is not a source grant or original-attempt
witness. The host owns original-operation authorization, retention, serialized
generation mutation and reconciliation. Unsupported host callbacks refuse.

The publisher independently recomputes policy/configuration/result identity
after normal publication admission, stages the entire result-bound inventory
and every complete finding explanation, and checks returned manifest content
digests/counts/bytes against the actual submitted documents. It validates the
root's complete future resolution growth before any remote mutation using a
separate allocation-only value. Maximum-width projected storage/review IDs are
only sizing values; they never become reader authority or retained root identity.

The explicit host conditionally activates and readbacks an owned root, returning
actual authenticated numeric root identity and generation. Before root dispatch,
the host holds the original attempt; only exact owned readback and successful
local generation commit clear that hold. Lost POST/readback responses retain
the hold and original reservation, so retry cannot issue a fresh root. Every
PR fence dispatch receives the original budget's remaining timeout. The publisher GETs
that exact object independently, checks exact body/head/PR/Bot numeric ownership,
then scans the latest authority with all required explicit v4 readers and fresh
complete prose readbacks. A changed newest root never falls back to older clean
authority. The final current PR fence remains live. No source, deadline, counter
or mutation allowance is inferred from root/manifest fields or callback success.

Initial supported scope is pending human inventories without blocking findings.
Blocking-to-body placement is an explicit unsupported capability. The stable
check uses existing operator policy; check failure remains visible in the root
and facts. Auto-approval opt-outs and existing provider provenance/qualification
rules persist. A provider label or status does not become qualification. All
pending findings remain blocked and this initial writer emits no APPROVE.


The explicit `BoundPartitionedEligibilityReader` requires independent owned root
body bytes and context from a retained authenticated publication receipt. It is
opt-in on `HumanAssessmentPublisher` and `ReviewApprovalFinalizer`. It shares
that source operation's actual budget/transport and never turns a budget snapshot
into an original attempt. Authenticated content-addressed inventory/receipt
payloads may be copied within this operation; mutable records are never cached.
Each completed paginated review scan supplies its actual freshly fetched records
to the same numeric-ID, Bot owner, head, PR placement, full body byte/hash checks
used by individual prose GETs. Callers cannot provide claimed fresh records to
`load_eligibility`. Duplicate/missing records and changed prose/root bytes refuse.
This removes duplicate GETs while retaining full prose freshness at preparation,
provider dispatch, acceptance, final activation and post-publication verification.
The ordinary/default reader remains unchanged.

For explicit target IDs, evidence acquisition requests the complete required
path closure of those targets, while retaining the entire human inventory and
complete changed-file evidence bundle. It does not raise the 64 required-path
bound or treat unselected concerns as resolved. The same existing queue adapter,
source/PR/latest checks, serialized local reads and provider evidence validator
remain in the actual dispatch/acceptance path; no preparation fence is removed.

Add `HumanAssessmentPublisher.publish_partitioned` for an accepted partial
assessment with findings still pending. It validates the actual work against
original inventory, complete evidence and selected source, then builds a bounded
resolution root. Every resolved full ID is explicitly paired with the same unique RS alias
rendered in the retained prose; the remaining count and rule that every other
visible RS alias remains pending are visible. Navigation links every
retained full-prose part, clearly marked historical, preserving all concerns,
reasons and required paths without duplicating their bodies. Immutable manifests,
result/policy/configuration/producer identity and provider qualification facts
remain unchanged. The ordinary publication API refuses v4 without this explicit
conditional host. This profile never emits APPROVE; all-resolved finalization is
an explicit unsupported capability here.

`PartitionedAssessmentHost` owns the original accepted source/queue packet,
serialized generation, exact accepted decision bytes, declared physical dispatch
liabilities and terminal acknowledgement. Before dispatch, admission checks the
actual complete-scan cost, a possible added-root page and all host/final-source
liabilities against the original budget. This observation only plans capacity;
verification still fetches every page fresh. Unexpected concurrent growth or
transport ambiguity remains held/refused. There is no call-cap increase or
ordinary-read relabeling as a fence.

The local host holds before POST, authenticates the actual numeric owned root
GET, and invokes the publisher's independent complete root/prose and final
source/head verification before terminal local commit. Successful commit closes
the original reservation after readback; a stored pre-publication counter cannot
restart that completed source. Failure retains the original reservation and
unknown hold. Exact in-process replay verifies the committed local record and
fresh complete root/prose/source/head without another inference, root or round.
Fresh-process root-delivery recovery needs an authenticated original attempt and
owned terminal packet; a snapshot or newly loaded manifest is insufficient and
is not qualified by this local fixture.

## Consequences

Full finding prose is visible in retained COMMENT reviews, with navigation from
the owned authority root. Immutable parts and earlier roots are never deleted.
Failure after staging retains nonauthoritative evidence; an unknown root write
retains the original reservation/charges for explicit reconciliation. A fresh
attempt cannot be inferred from an old snapshot. REST-only automatic writer
activation remains disabled: GitHub review writes do not provide the required
authenticated conditional generation boundary.

The serialized local host fixture is a finite trusted library profile. It uses
actual `LocalSessionLedger` reservation/generation mutation and an in-process
serialized writer. It does not establish an atomic distributed GitHub lock,
broker grant consumption, fresh-process unknown-write recovery or deployed
compatibility. Those are separate implementation/qualification requirements.

## Alternatives considered

Embedding the full inventory in the legacy authority exceeds its reviewed
allocation. Raising caps, truncating concerns or inventing a numeric root ID is
rejected. Nonactivating staging alone retains data but does not close publication
liveness. A REST-only writer without authenticated host generation would make
concurrency and unknown outcomes appear safer than its contract supports.

## Validation

`Rich250PublicWriterTests.test_complete_rich250_public_write_readback` exercises
250 independently varied full IDs, 250 distinct paths and every original concern
body with detail=1. Actual local persistence plus captured synthetic GitHub HTTP
finishes at 49 charged calls: 36 HTTP dispatches and 13 local control dispatches.
Four complete prose reviews, four immutable evidence objects and one authority
root retain all 250 pending findings. The check is `action_required`; provider
facts remain fixture/not-required; no APPROVE occurs. All 250 future full-ID
resolutions still fit the original root allocation and preserve original prose.

Other tests exercise wrong independent policy/configuration/result/visible set,
too-small future allocation before HTTP, actual lost activation POST/readback
responses held without retry, remaining-deadline PR fences,
actual competing local generation, two serialized original writers with exactly
one root, changed head, missing evidence and changed complete prose. Operator
opt-outs remain persisted. Existing v4 reader/staging tests retain their bounds.

## Rollout and rollback

No default writer, CLI option, worker deployment, new permission or schema field
is activated. Older/default readers refuse v4; mixed-reader deployment is not
qualified. Roll back by removing the explicit host opt-in while retaining every
owned root, prose object and immutable evidence part for investigation/recovery.

## Late assessment validation and remaining boundaries

`Rich250LateFeedbackTests` exercises the actual initial public writer, terminal
initial ledger acknowledgement, a genuinely new authorized source revision,
public selected-context preparation, complete 250-path evidence, real local queue
persistence, one provider request, conditional root delivery and exact replay.
The later source has one original budget throughout; the earlier initial review
has its own completed operation (49 publication calls plus 5 acknowledgement
calls). No same-event budget or counter is reset.

| Later source state | Accepted | Durable 249-pending root | Exact root replay |
| --- | --- | --- | --- |
| 0 unrelated issue comments | 40 (24 HTTP + 16 local) | 53 (32 HTTP + 21 local) | 57 (35 HTTP + 22 local) |
| 100 unrelated issue comments | 40 | 53 | 57 |
| 250 unrelated issue comments | 40 | 53 | 57 |
| 100 additional non-App review records | 44 (28 HTTP + 16 local) | 59 (38 HTTP + 21 local) | Admission refuses at 59 before more I/O |
| 250 additional non-App review records | 48 (32 HTTP + 16 local) | Admission refuses at 53 before root POST; all 250 remain pending in trusted authority | Unqualified |

At 53 the original budget contains 41 ordinary and 12 existing fence charges;
at 57 it contains 45 ordinary and 12 fence charges. These include the actual
prepaid local activation steps, not just HTTP requests. The original total
ceiling remains 64 and ordinary dispatch still leaves the four reserved final
slots; no read is relabeled for capacity. The finite publication admission
conservatively funds the complete remaining liability before root POST.

Issue comments and review records are distinct. The explicitly selected source
route does not load unrelated generic discussion; it still authenticates the
entire selected source closure. Review records are fully paginated and freshly
validated, including all required prose. The 250-review-record refusal retains
the accepted decision in its original queue without claiming root delivery or
approval. The 100-review-record first delivery does not qualify its replay.
Known insufficient delivery capacity refuses before an authority mutation.

Additional tests cover schema-valid/in-place root edits, exact root byte binding,
prose missing/duplicate/owner/head/placement/body faults before dispatch (zero
provider calls), changes after provider (no accepted receipt), pre-publication
source/head/pause changes, actual concurrent local generation mutation, actual
lost POST/GET responses, changed prose/source/head during root POST, malformed or
unreadable pagination, remaining deadlines and expiry, copied-payload alias
isolation, and two concurrent original writers producing exactly one root at 60
charged calls. Every control uses captured HTTP and real local persistence.

These finite initial/detail=1 and partial-resolution profiles do not qualify
richer detail=3 capacity, initial publication on a busy review history, arbitrary
feedback/reference history, every subsequent resolution through all-resolved
approval, fresh-process terminal root recovery, installed CLI/broker, other
provider/configuration cross-products or deployed behavior. No issue is closed
from these profiles alone. No DecisionProvider or governance policy decision is
introduced.
