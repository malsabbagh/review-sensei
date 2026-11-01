# ADR 0121: Proposal-only comment intent and closed-choice provider seam

- Status: Proposed
- Date: 2026-11-01
- Decision owner/reviewers: ReviewSensei maintainer and independent reviewer
- Linked GitHub Issue: [#161](https://github.com/malsabbagh/review-sensei/issues/161)
- Linked PR: [#403](https://github.com/malsabbagh/review-sensei/pull/403)

## Context

Comment replies currently combine prose and fix signals. A bounded decision
provider could extract intent without acquiring source authority or proving a
finding fixed. An inline reply and a global PR comment have different target
scope. The current broker authenticates literal commands; generated command
text cannot inherit that authority. Historical nonterminal operations and
missing review evidence remain separate concerns.

## Decision

Introduce a provider-neutral closed-choice `DecisionProvider` protocol and pure
proposal API. The host supplies authenticated source/revision/thread/actor,
current PR snapshot, actual permissions, admitted extraction, findings and Git
ancestry facts. None of these fields comes from a model response. The response
must match exact request/provider/model/question choices; unknown usage stays
unknown. There is no configured adapter, dispatch wrapper, automatic retry,
provider fallback, command invocation or native write in this contribution.

The first consumer supports a single unquoted English clause, at most 4096
original comment bytes, ten host-lexed references and 13 closed questions.
Unsupported quotations, multiline input, conjunctions/clause punctuation,
incompatible claim/action choices, language/reference
confusables and unresolved identities abstain. Original UTF-8 byte offsets and
literal candidates are passed as data. Models select candidates; they cannot
invent IDs, commits or permission. This is an intentionally narrower first
slice of the proposed richer claim/span extraction contract.
The provisional clause stance and original UTF-8 span remain attributed to the
source. The host retains observation/request/response together; this pure API
does not introduce persistent authority or an execution receipt store.

Exact standalone commands are recognized first using existing parsers and
head selection. A malformed sharp command cannot fall through to NL. Their
returned `explicit` proposal is still not an executable command or admission.
Explicit proposals finish during preparation. Completing a typed extraction
response against an explicit source refuses rather than ignoring the response
and switching to that source operation. Current source/state vetoes still win.
Global claims retain the complete current PR scope. Inline claims inherit one
exact current finding instance and cannot target another concern. Commit
references must resolve uniquely in PR ancestry, including full object IDs.

Fixed and negative claims produce only an assessment proposal with pending
criterion, current diff, full inventory, context-policy and claimed-change
bindings. These bindings are requirements, not examined evidence. General
criterion assessment is not implemented here and cannot be replaced by verify.
Other low-impact intents describe existing status/verify/continue/rescan/
remaining semantics. Approval prose proposes status only. Risk dispositions,
coverage/media attestation, stop and recovery remain explicit-only. All
proposals are non-executable even for a maintainer.

## Scope

This phase adds an offline library seam, schema and synthetic conformance tests.
It changes no caller routing, broker protocol, event subscriptions, default
configuration, command handler, review budget, findings, accounting, approval,
release version, deployed Worker or activation registry. Existing review flows
do not import or invoke this seam. Issue #161's candidate-triage consumer remains
separate; only the small closed-choice protocol can be shared later.

## Consequences

Source/head/actor/revision/policy/inventory/model changes invalidate decisions.
Bot and external observations can remain evidence during independently admitted
work; they do not fund work. The proposal-only policy conservatively requires
live maintain/admin plus a separately authorized invocation for action
proposals. Broader collaborator intake needs its own policy review. A retained
nonterminal operation refuses an action proposal. No flag here creates a
broker grant or terminal proof.

The host still needs a capability/privacy/usage-qualified provider, original
shared allowance and durable attempt semantics before dispatch can be wired.
These are not supplied by a Python Protocol or passing synthetic fixtures.
Semantic precision and latency are unmeasured; literal provenance is not proof
of interpretation correctness.

## Alternatives considered

Generating command strings would misrepresent human authority and is rejected.
Expanding a small deterministic alias parser remains a comparison baseline.
Giving a model repository tools or making it decide finding validity is outside
scope. No model, paid routing, installation or license choice is implicit.

## Validation

Public proposal-entry tests cover global and inline identity, negative claims,
explicit compatibility, malformed commands, stale snapshots, roles/funding,
closed PRs, nonterminal history, ancestry, UTF-8 spans, bounded references,
closed complete responses, unknown usage and replay keys. Installed-wheel tests
exercise the schema and explicit-route boundary. Ordinary required checkout,
format/type/schema/build/distribution gates apply. These are synthetic
conformance tests, not a held-out model evaluation or live activation proof.

## Rollout and rollback

No live rollout is included. Removing the unused proposal seam leaves literal
commands operational. Before any consumer wiring, separately qualify the typed
original-source action envelope, fresh host fences, provider capabilities,
funding, durable attempts, installed caller/Worker parity and effect readback.
Preserve future receipts and unknown liabilities through any later rollback.

## Follow-up work

Independently label and freeze the held-out comment corpus; choose a model and
privacy/budget policy only through explicitly authorized evaluation. Implement
the general criterion-bound assessment adapter separately. Executable NL,
all-comment intake, edits/deletes and source-bound action grants require later
reviewed phases. The sub-line design and Vault historical recovery remain
separate work items.

## Amendment: bounded per-item batch proposals

User acceptance includes multiple finding IDs with distinct explanations and
actions in a single original comment. A single aggregate stance/request cannot
represent that intent safely. Add a separate normalized per-item core boundary,
without changing the original one-clause facade or enabling execution.

The host builds a versioned item manifest with exact original UTF-8 ranges,
reference candidates and quote/fence context. The first grammar uses explicitly
delimited lines/bullets and semicolon units, with one finding target per semantic
item. Ambiguous indivisible multi-target explanations require clarification.
All meaningful source regions remain represented; source is not rewritten into
fabricated human comments. Explicit parsing sees the complete original only.

Use distinct source, item, request/manifest and interpreted-proposal identities.
Item identity is independent of extraction array order, model interpretation and
sibling results. Repeated or conflicting claims remain separate observations;
a shared proposed-operation key does not discard them, grant work, refund cost
or prove replay safety. Persistent admission/idempotency remains a later stage.

The native producer prepares its entire finite question inventory before packing
complete item question sets: at most ten items, ten references, forty questions,
three requests, sixteen questions per request and 4096 input bytes. All requests
bind the same original source and complete manifest. Actual dispatch needs one
original shared allowance covering the full liability; no per-item fresh window,
transport, retry, fallback or provider selection is supplied here.

Failure boundaries remain distinct: invalid shared source/state/manifest rejects
the shared interpretation; an invalid/incomplete native group leaves dependent
items unextracted; semantic ambiguity, negation and inline-target conflict affect
their own items and any source-linked qualification dependencies. Independent
valid siblings keep their own result. Missing extraction is not no-action.
Risk requests stay explicit-command-required; verify cannot satisfy sibling
assessment obligations. No group success clears findings or supplies approval.

Validation adds original-source public-entry tests for mixed actions and
explanations, ambiguity isolation, opposite/repeated claims, stable identities,
quote/fence and UTF-8 ownership, shared-integrity/group/semantic failure scopes,
exact question/input bounds, role/funding/history gates, and installed-wheel
batch imports and results. Existing command and single-clause tests remain
compatibility oracles. The APIs are inactive and have no recovery, renderer,
ledger, caller/broker, qualification or deployed-provider integration.

Alternative substring observations were rejected because they misrepresent
source authority. One aggregate intent was rejected because it loses per-item
meaning. Arbitrary generative span extraction remains an explicitly selected,
separately validated alternative; this stage normalizes the existing closed
native decision-response contract. Rollback removes the unused batch API; it
changes no active state, history, permissions or source authority.
