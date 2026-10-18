# ADR 0083 - Bind runtime approval to independently checked OpenRouter evidence

Status: Proposed
Date: 2026-10-18
GitHub Issues: #115
Pull Request: pending (stacked on #316)
Owners/Reviewers: Maintainers
Approved by: not applicable

## Context

An accepting caller/profile string is not evidence. The initial fail-closed
repair withholds all OpenRouter approvals, but cannot demonstrate a checked
positive control. A standalone finalizer can also receive old eligibility with
an accepting string and no provider identity. It cannot safely infer that such
an old review was exempt from OpenRouter qualification.

ADR 0081 binds reports to exact routing/endpoint identities; ADR 0082 preserves
live quality and deterministic controls. The existing qualification loader
checks retained report bytes, independent invocations, supported status,
thresholds and separately expected operator attestations. Reusing that loader
avoids a parallel promotion framework.

## Decision and supported scope

A trusted host may inject one immutable `TrustedOpenRouterQualification` into
`ReviewPublisher`, `ReviewApprovalFinalizer`, `ConversationPublisher` and
`HumanAssessmentPublisher`. Construction snapshots bounded retained record and
report bytes, separately supplied expected target, engine, prompt, evaluation
configuration and corpus digests, observation date and reproducibility inputs.
It also snapshots the actual effective runtime configuration, stages and
limits. Stages must use the single configured provider rather than independently
routed per-stage profiles. The actual installed engine/prompt identities and
runtime model, endpoint and complete strict routing policy must match.

These inputs belong to the host composition boundary, outside PR-controlled
configuration and model output. They must be selected independently of the
record being tested. Runtime transaction configuration and evaluation
configuration have different digest contracts. The host explicitly attests the
applicability of the evaluated configuration to its selected runtime context;
the implementation never equates those digests or derives expectations from
the record. This is not cryptographic attestation of report producer honesty.

Publication rechecks the existing supported-record loader against exact bytes
and independently expected identities, then checks actual analyzed context,
provider/model, limits, publication mode and any result transaction digest. It
persists only a SHA-256 receipt binding that verified authority to repository,
PR, base, head and result. Caller strings alone still cannot qualify facts.
The receipt does not contain reports, prompts, findings or credentials.

New facts use schema version `2`, adding a required normalized provider and an
optional bounded qualification receipt. Eligibility envelope versions stay
unchanged, including the reader-first v4 envelope. Legacy facts version `1`
remain parseable, but accepting legacy facts cannot authorize finalization:
the missing provider cannot prove exemption. Old strict readers reject facts
v2 rather than guessing their meaning. This requires the matching adapter and
engine schema update; it is not a transparent downgrade format.

Every public finalization requires App-owned exact-head persisted eligibility
readback, including provider exemption. The compatibility `require_persisted`
argument cannot disable it: caller-constructed facts v2 claiming `ollama` cannot
exempt an unbound or different persisted review. First publication and recovery
use the same readback requirement.

The separate finalizer requires configured current authority for OpenRouter,
rechecks retained evidence and the receipt against live exact base/head before
thread reads and again at the approval write boundary. Missing, changed or
legacy-unbound authority withholds. Existing persistence ownership, current-head
checks, human decisions, coverage, blockers and open-thread gates remain
conjunctive. Other providers' newly published facts retain their previous policy;
this introduces no qualification requirement for Ollama or compatible backends.
Disabled approval and informational/request-changes behavior remain supported.

## Alternatives

Embedding all 3–32 reports in eligibility would amplify body/read budgets and
expose unnecessarily large evidence. Persisting only `qualified`, a profile
allowlist, a caller boolean or a parsed record's status would repeat the original
unverified-authority gap. Revalidating injected evidence plus a bounded receipt
keeps the retained bytes outside GitHub while making delayed approval fail
closed when independently expected deployment context changes.

## Migration and recovery

A retry with new evidence never upgrades an old unbound publication silently.
An interrupted bound publication can replay with identical current authority
and context; separate finalization can resume after authorized obligations
resolve. Model-free recovery forwards trusted context and rechecks the same
binding. Missing context withholds approval while publishing eligible comments.

The existing authenticated incomplete-rescan path remains available when the
prior review is incomplete and a consumed rescan transaction authorizes a new
analysis/result. Qualification is checked independently for that fresh result.
That path does not authorize replacing a complete legacy review, and ordinary
per-head reconciliation does not rewrite its eligibility. For complete legacy
records the supported route is a fresh bound review on a new head. No new
boolean or silent metadata migration grants same-head analysis authority.

CLI/default hosted composition does not infer or load authority from repository
files, ambient profile labels or the result. Operators wiring the reusable host
API must configure the evidence inventory and expected inputs explicitly. Without
that wiring, OpenRouter automatic approval remains withheld. Real model/provider
qualification (#122), deployed/installed operational acceptance (#123), and
heterogeneous stage-provider qualification remain separate conditions.

## Validation and rollback

Offline public-seam controls cover a checked positive, application publication,
model-free recovery, replay and delayed finalization. Negative controls cover
missing/changed/duplicate/fixture evidence, independent identity mismatches,
unsupported suffixes, endpoint/policy changes, forged or moved receipts,
legacy facts, incomplete review, blockers, unresolved threads and opt-outs.
The installed-safe control uses the actual packaged corpus and synthetic
provider output to exercise publisher and parsed delayed eligibility. Synthetic
reports never become real provider qualification evidence.

The integration owner runs complete source and clean-wheel lanes on frozen
bytes. Rollback may withhold more approvals; it must not reinterpret facts v2
or restore accepting strings as evidence. This proposal changes no permissions,
credentials, deployment, release or channel pointer.
