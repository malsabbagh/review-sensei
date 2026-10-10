# ADR 0076 - PR253 gap-closure contracts

Status: Proposed
Date: 2026-10-10
Owners/Reviewers: Maintainers
Approved by: not applicable
Related issues: [#238](https://github.com/malsabbagh/review-sensei/issues/238), [#253](https://github.com/malsabbagh/review-sensei/pull/253)
Related PR: this change

## Context

PR 253 revalidated composition gaps that remain after the merged storage,
evidence and provider-guard primitives on main `f8f6a639`. ADR 0074 stores
original-attempt metadata and is not an authorizer. ADR 0075 refuses unsafe
provider completion metadata and does not qualify a model. Public reply
construction still uses the legacy assessor path. A separate human-media
recording implementation exists outside this tree and does not yet approve.

The implementation request for this change selects the recommended defaults
from the PR253 gap plan. Decisions D-A, D-B, D-C and D-D are accepted.
Maintainer acceptance of the remaining contracts is still by review. This
record freezes the contracts implementers must follow. It does not claim a
measured 64/60 public profile, process attestation, or deployed qualification.

## Decision

### Accepted operation-entry decisions

**D-A.** Accepted. The validated review result, not prompts or transcripts, is
stored as App-authored part comments on the same pull request.

**D-B.** Accepted. `github.operation_entry` defaults to enabled. The 64/60
budget is not yet measured
([#267](https://github.com/malsabbagh/review-sensei/issues/267)). Caps are
never raised.

**D-C.** Accepted. Verify reassesses the existing inventory on the current
head with at most one provider request, then runs the approval finalizer,
including mixed APPROVE when media coverage is the only blocker.

**D-D.** Accepted. The worker has no rate-limit counter. Abuse protection is
GitHub's own limits plus a Cloudflare WAF rule configured outside the
repository.

### D1 mandatory context

Keep unqualified admission at 49,152 UTF-8 bytes. Do not raise that cap.
Governing rules stay verbatim in an explicitly required source small enough to
admit with complete evidence. Whole reference material, including the long
`docs/architecture.md` guide, is supplemental and may be omitted only through
the reviewed target-branch document policy, with provenance and omissions
preserved. No model-written replacement and no inferred optionality. A larger
provider envelope is not selected.

### D2 producer trust

Deterministic admission for the new capability is **delegated execution
assurance**, not hardware or process attestation. GitHub-hosted OIDC
authenticates the caller, workflow, run and actor. An approved immutable
workflow attests the entrypoint commit, wheel/artifact SHA256, dependency
lock, renderer/context/admission schema versions and canonical declaration
bytes. The broker independently checks live source, root, inventory, trusted
policy and ordered liabilities. It does not claim it executed the materializer.

Two contracts stay distinct:

- existing-inventory reassessment
- first full-review inventory derivation

Unapproved producers, mutated materializers, caller-controlled execution
paths, self-hosted runners and declaration replay under the other contract
refuse. Legacy capabilities keep their current behavior.

### D3 missing published check

Missing, failed and unpublished checks stay diagnostic. They are not a new
approval blocker in this change. Branch protection is a separate administrator
control. Documentation and tests must say approval is not merge authorization.
Do not silently invert this under a bug fix.

### D4 unchanged files

Permitted regular unchanged files may be cited only with a closed
exact-source record: repository, pull request, base, head, path, regular mode,
Git object SHA at both snapshots, exact required range, content SHA and byte
length. Absent diff hunks are not evidence. Symlinks, submodules, secrets,
excluded paths, foreign objects and truncated ranges refuse. Until a caller
uses this record, precise pending or refusal remains the supported result.

### D5 mixed AI and human coverage

A fresh valid human confirmation of otherwise unsupported regular binary files
may remove only the binary coverage/status blocker. AI coverage stays partial.
Blocking findings, failed text or context review, permissions, unresolved
threads and every other existing fact still gate approval. The first profile
records confirmation, then requires a separately authorized verification
event. One invocation must not hide a second budget.

Host-derived partial causes are closed. `coverage_only_partial` is true only
when completed text review and the complete binary inventory show that
unsupported binaries are the entire remaining coverage cause. Contradictory
reason metadata refuses. Old readers reject the new mixed schema.

### G0 bootstrap

The first supported profile is **uninterrupted bootstrap only**.

| Record | Rule |
| --- | --- |
| Unauthenticated ingress | No victim event or root ownership. Abuse protection is outside the worker (D-D). Invalid traffic is not charged to another event. |
| Interrupted pre-admission | If the authoritative operation comment write did not commit, refuse that attempt. Do not reserve the victim event. Do not reconstruct the cost from a fresh grant or an old GitHub snapshot. |
| Committed original admission | Sticky for the stable event. A lost response, fresh OIDC token, or inventory edit returns the same origin or a binding conflict. It does not create another allowance. |
| Bootstrap carrier | The server creates it inside the authenticated admission transaction. A caller nonce or unsigned event claim is not proof. Absent proof refuses original mutation. |
| Unknown attempt | Retain the debit and possible external effect. Expiry, an older root, or a new token does not clear it. |
| Capacity | Keep 1,024 scopes, 256 guards per scope, 64 transitions per source and 16,384 retained transitions. Refuse before work. Do not evict obligations or tombstones. |

This profile does **not** close artifacts-none recovery of an interrupted
admission. Post-commit authenticated restore is in scope and must not mint a
grant.

### Acceptance stores

The public record is App-authored GitHub issue comments: one index comment
plus one comment per event, and separate result-part comments.
`run_review_trigger` is the entry for full review, reply, reassessment, and
verify. The index and event comments store digests, counters, the trigger
name, and the accepted packet. Result-part comments store the validated
review result. These comments do not store prompts, transcripts, or file
contents. The worker verifies GitHub identity and issues credentials. It is
not the operation store.

The exact App-owned activated acceptance packet owns normalized decisions and
measured known output bytes. Eligibility is derived. Acknowledgement is a
projection, never the acceptance record or the resource origin.

Order: operation comment charge, then provider and validation, then accepted
packet comment, then result parts, then publication, then acknowledgement.
While the stores disagree, liability stays conservative and inference cannot
redispatch. Staged orphan parts are not acceptance. A broker rollback after
an observed external effect must not reopen dispatch.

### Unknown remote effects

Exact unique owned target, marker, head and operation observation can confirm
a commit. Seeing no target is not cancellation. Root PATCH, COMMENT, check,
APPROVE and source acknowledgement each reconcile on their own contract. If no
independently sufficient release condition exists, writer exclusion stays in
terminal pending quarantine. This change does not promise eventual completion
or exactly-once remote writes.

### Post-deadline observation

Read-only reconciliation after the original deadline is a separately
authenticated observation event with its own bounded bootstrap, transport and
deadline. It issues no old-operation permit and does not resume inference.
If it cannot authenticate the exact outcome, the original attempt stays
pending.

### P1 profile

The first useful public proof is one pull request, one exact source, four
distinct pending instances with complete evidence, one provider request and
two durable checkpoints, then accepted readback before acknowledgement.
Ordinary dispatches stay 60 and total dispatches stay 64. The control deadline
stays 60 seconds. Provider calls after a kill that follows accepted authority
must stay 1 then 1 on replay. The 64/60 budget is not yet measured
([#267](https://github.com/malsabbagh/review-sensei/issues/267)). If that
profile does not fit, set `github.operation_entry` to disabled. Caps are
never raised.

### Shared host

One operation host owns admission, restore, transport accounting, checkpoint
activation and publication reconciliation for full review, reply,
reassessment and verify. Those triggers enter through `run_review_trigger`.
The record is the App-authored GitHub comments above. Domain modules do not
gain broker authority. `github.operation_entry` defaults to enabled.

## Scope

In scope: these contracts, gated authorizer and restore behavior, the
operation host, unchanged-source citations, mixed-coverage evaluation, and
tests that show the negative cases above.

Out of scope for this change: publishing packages, deploying the Worker,
canary activation, live provider calls, qualifying a larger model envelope,
and recovery of an interrupted admission that never committed.

## Consequences

Callers of the new host must present an authenticated original handle. A raw
snapshot, a fresh budget, or a consumed token cannot restore work. Mixed
approval can succeed while the AI result remains partial. Missing checks still
do not block approval. Unchanged-file claims without the exact-source record
stay unresolved.

Interrupted bootstrap stays a terminal refusal. That is a product limitation,
not a silent success.

## Alternatives considered

Independent broker reconstruction of admission was rejected for this change
because it duplicates renderer semantics. Confirm-and-approve in one media
invocation was rejected because the measured confirmation plus evaluation
already exceeds the ordinary dispatch budget before broker overhead. Raising
the 49,152 byte or 64/60 ceilings was rejected. Treating a missing check as a
blocker was rejected because that would change acceptance policy without an
explicit product decision. Promising exactly-once GitHub writes was rejected
because a comment write and a later review write are still separate GitHub
requests. The public record does not add a private database beside the
repository.

## Validation

Tests must cover the G0 traces that this profile can express: lost response
after commit, token rotation, concurrent authentic admits, unauthenticated
victim-key spraying, capacity refusal, and failure before commit leaving no
event. They must cover producer negatives, store disagreement, a delayed
remote write after a newer read, the P1 crash replay, unchanged-source
negatives, and mixed approval with unrelated blockers still withholding.
A fixed structural clock is not elapsed-time proof.

## Rollout and rollback

`github.operation_entry` defaults to enabled. Rollback sets that switch to
disabled and keeps compatible readers, original guards, unknown transitions,
receipts and tombstones. Do not delete unknown in-flight state or reset an
event budget.

## Follow-up

These items stay open until their own evidence exists:

- Maintainer acceptance of this ADR.
- Interrupted-admission cost recovery ([#268](https://github.com/malsabbagh/review-sensei/issues/268)). This profile refuses interrupted admission. The worker is not the operation store.
- A passing public P1 trace inside the original 64 dispatches and 60 seconds, including real elapsed time ([#267](https://github.com/malsabbagh/review-sensei/issues/267)). The in-memory host is not that trace.
- Installed, published and deployed artifact identity, plus joint 100/250 history ([#269](https://github.com/malsabbagh/review-sensei/issues/269)).
- A normative re-audit of the required rules file. Keyword extraction and a one-file fixture do not prove every configured review fits ([#270](https://github.com/malsabbagh/review-sensei/issues/270)).
- Human-file recording is ADR 0077. Verify can post mixed APPROVE when media coverage is the only blocker. AI coverage stays partial.
