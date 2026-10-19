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

## Follow-up work

The full late selected-source flow must authenticate the same retained complete
inventory and exact source closure, preserve original attempt accounting and
perform fresh full prose checks at dispatch, acceptance and final activation.
Fresh root metadata fences may reuse immutable evidence only inside its actual
original operation; they must never cache mutable source/PR/prose authority.
An explicit conditional resolution-root report may link every retained full prose
part only if pending/resolved IDs, status, counts and individual reasons remain
unambiguous. Older prose must be clearly historical and every resolved full ID
explicit. The ordinary pending report remains unchanged.

Initial root publication is not proof of that later 249-pending authority update,
accepted replay delivery, richer detail=3 capacity, complete retained feedback
history, installed CLI/broker or live provider/deployed behavior. No issue is
closed from initial publication alone. No DecisionProvider or governance policy
decision is introduced.
