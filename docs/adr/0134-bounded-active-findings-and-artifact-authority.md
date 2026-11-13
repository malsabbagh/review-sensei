# ADR 0134: Bounded active finding window, cross-file drift, and artifact/trust authority

- Status: Accepted
- Date: 2026-11-11
- Decision owner/reviewers: ReviewSensei maintainers; independent source review
- Linked GitHub Issue: pending; user-directed lifecycle gap qualification (G10, G16, G14, G15)
- Linked PR: [#460](https://github.com/malsabbagh/review-sensei/pull/460) supplied the cold-inventory guard; the subsequent durable lifecycle candidate requires independent qualification.
- Source revision of this decision: 2026-11-13; final exact-head qualification pending

## Context

Multi-round pull request reviews face four coupled failure modes when display,
identity, comment persistence, and repository trust are conflated:

1. **Coverage versus obligation collapse (G10):** Complete changed-file coverage
   in an incremental or fallback-full review does not prove an omitted prior
   blocking finding was fixed. Conversely, bounding the number of inline threads
   shown to reviewers must never silently drop non-displayed obligations, leak a
   public deferred-finding queue or count, or trigger unbounded continuation
   loops.
2. **Cross-file location and thread drift (G16):** Renames, copies, symbol
   extractions, line deletions, duplicate code, and whole-file rewrites move or
   invalidate diff anchors and cause GitHub to mark threads outdated or allow
   premature human resolution without resolving the underlying concern.
3. **Human edits and deletion of review artifacts (G14):** Reviewers and bots
   can edit or delete PR comments, review summaries, inline threads, checks, and
   reactions, including immediately after an HTTP response loss.
4. **Repository and privilege transitions (G15):** Repository renames, ownership
   transfers (where numeric `repository_id` remains unchanged while `owner_id`,
   slug, and installation authority change), fork conversions, GitHub App
   reinstalls, repository selection removals, permission revocations, and actor
   privilege downgrades can occur before admission, before a native write, or
   after an unconfirmed dispatch.

## Decision

Retain the complete finding lifecycle under the independently selected session
root, before effects, rather than treating a reduced baseline or publisher cache
as authority. The versioned codec, evidence reference, transport journal and
native readback described below extend the ordinary G10/G14/G16 paths. Current
source includes fresh-process application tests and bounded native-publication
tests; this document records the implementation contract, not a claim that the
final candidate's complete test matrix or required CI has passed.

G15 trust observations and migrations retain their separate admission contracts.
A finding snapshot neither establishes current installation/owner trust nor
solves G02/G08 uncontrolled-writer fencing.

### 1. Four-variable bounded active window (`review_sensei.active_finding_window`)

When a complete lifecycle state is available, the domain evaluator separates
four immutable state variables on each review round:
- `changed_file_coverage`: diff enumeration and per-file coverage state;
- `authoritative_decision_inventory`: complete retained inventory of unresolved,
  omitted-uncertain, ambiguous, verified-fixed, overridden, and rescan-superseded
  obligations regardless of display capacity;
- `display_active_findings`: strict-cap (`max_active_findings`) priority-first
  window that preserves existing active/contingent native slots before promoting
  challengers;
- `approval_eligibility`: final gate requiring both satisfied coverage and zero
  blocking/uncertain/ambiguous obligations in the full authoritative inventory.

Public projections (`to_public_status`) are validated by
`assert_no_deferred_leakage`: they truthfully withhold approval and publish
explicit recovery routes (`verify`, `override`, `accept-risk`, `rescan`,
`accept-coverage-uncertainty`) without ever exposing a deferred-finding list or
count. Opt-in `#427` decision-provider receipts remain bounded proposals used
solely for intra-tier tiebreaking; they never drop obligations, downgrade
blocking status, or prove fixes.

**Relationship to ADR 0042:** ADR 0042 introduced `PublishedSlot` states
(`active`, `contingent`, `uncertain`, `released`) and bounded inline selection.
This ADR refines and extends ADR 0042 across multi-round lifecycles without
changing `PublishedSlot` wire or state semantics: ADR 0042's slot invariants
govern per-target reservation inside `display_active_findings`, while this ADR
separates `changed_file_coverage` and `authoritative_decision_inventory` so
non-displayed obligations remain approval gates while that inventory is
retained. A reduced persisted baseline does not restore those gates or slots.

### 2. Orthogonal criterion, anchor, and thread tracking (`review_sensei.finding_drift`)

Track `OriginalFindingCriterion` independently from `PresentLocationAnchor` and
`NativeThreadBinding` across `file_rename`, `file_copy`, `cross_file_move`,
`line_deletion`, `duplicate_code`, and `whole_file_rewrite`. Outdated anchors,
deleted lines, and host UI thread resolution never resolve an obligation.
Uniquely drifted active obligations update and reuse their existing native
thread in-place (falling back to `BODY_PLACEMENT` via `plan_finding_placement_detail`
when unanchored). Multi-target copies, split moves, and duplicate code hold in
`ambiguous-multi-target` until positive `ConfirmedFixEvidence` covers every
implicated path, an `AuthorizedHumanOverride` is recorded, or a complete
head-bound `AuthorizedRescan(mode="full")` (whose `reviewed_paths` include all
present candidate paths) supersedes prior state. Ambiguity and all prior
candidate paths survive subsequent empty or single-target results; another diff
or deleted path alone cannot collapse that set.

Native success uses actual owned review/comment/thread readback. A review ID
alone cannot confirm an inline binding. New inline receipts match the exact
rendered body, instance, initial anchor, numeric producer, repository/PR and
snapshot. An in-place comment edit preserves GitHub's original review, commit
and anchor; the current lifecycle location and marker are tracked separately.
Its old body digest and native identities must match the retained receipt before
`PATCH`, and exact readback must confirm the edit. Pending creation uses a typed
`intent:` handle with no numeric comment ID, not a manufactured GitHub ID.

An `AuthorizedHumanOverride` on `ambiguous-multi-target` requires authenticated
maintainer/admin authority (`OWNER`, `MEMBER`, or `COLLABORATOR` meeting
`authorized_override` policy) bound to the exact `obligation_id` and `head_sha`
with a non-empty audit reason and `proof_sha256`. Recording an override is an
explicit whole-obligation disposition across the entire `candidate_paths` set;
partial-file fixes without maintainer override must instead supply
`ConfirmedFixEvidence` covering every implicated path.

### 3. Three-tier artifact classification (`review_sensei_github.artifact_authority`)

Classify GitHub review surfaces into:
- `authority`: `session_ledger_marker`, `retained_result_part`,
  `retained_context_part`;
- `evidence`: `owned_readback_receipt`, `archive_chain_part` (each requiring an
  explicit `expected_sha256` so missing expected digests fail closed);
- `projection`: `native_review_summary`, `inline_review_comment`,
  `check_run_text`, `comment_reaction`.

The artifact classifier treats mutations/deletions of `projection` surfaces as
non-authoritative: supplied observations cannot erase retained obligations or
manufacture approval. It refuses supplied tampered, duplicated, stale or foreign
`authority` markers and missing/corrupted archive evidence. Not every projection
observation is retained as authority. Before a native mutation, the lifecycle journal retains its exact intent; after HTTP response
loss the selected durable intent and target slot remain `uncertain` until
readback completes. Exact unedited App-owned evidence reconciles idempotently
at most once (`reconciled-via-owned-readback`),
whereas human edits, duplicates, or deletions preserve conservative
`uncertain-after-response-loss` holds. Restricted operator `reenroll` is
permitted only for authenticated maintainers/admins on open, non-draft PRs when
the marker is `missing` (backed by a cryptographic `enrollment_witness_sha256`
bound into the audit digest from verified prior delivery/check state) or
`expired` and zero active blocking obligations, reservations, staged
publications, in-flight publication phases, uncertain writes, quarantine
lineage, or assessment queues exist.

### 4. Repository and trust-boundary transitions (`review_sensei_github.trust_transitions`)

Bind `repository_id`, `owner_id`, `owner_login`, `repository_slug`, `is_fork`,
`installation_id`, `repository_selected`, `granted_permissions`, monotonic
`permission_epoch`, and actor authority in `AuthenticatedTrustSnapshot`.
Classify all seven transitions separately across `before_admission`,
`before_native_effect`, and `after_unknown_effect`. Both fork directions
(`False -> True` and `True -> False` unfork) fail closed on hosted write
boundaries (`unsupported-fork-boundary`) and are excluded from automated trust
migration. Enforce that unchanged numeric `repository_id` alone never proves
`ownership_transfer` trust: `verify_and_migrate_trust_boundary` binds
`migration.operator_id`, `operator_login`, `operator_association`, and
`operator_permission` to the authenticated `target` snapshot (requiring
override-level authority on `target`), rejects `permission_epoch` regressions,
and for `ownership_transfer` requires a domain-separated
`previous_owner_transfer_attestation_sha256` computed by
`compute_transfer_attestation_sha256` over `(repository_id, previous_owner_id,
previous_owner_login, new_owner_id, new_owner_login, pull_request, head_sha,
permission_epoch, previous_owner_witness)` and verified against expected prior-owner control
plane records. Legitimate renames, reinstalls, permission/selection
restorations, and dual-attested transfers recover while preserving 100% of
session charges, reservations, and obligation history
(`fresh_allowance_minted=False`).

### 5. Versioned lifecycle and session authority

`review_sensei.finding_lifecycle` defines the closed
`finding-lifecycle-payload-v1` codec. It preserves coverage, complete active-window
or drift inventory, original criteria, candidate paths, native slots and proofs,
input identity and concern history. It explicitly encodes all 13 `ReviewComment`
fields, including `effective_blocking` and `needs_human`. Public comment/result
serializers intentionally omit those admission fields and are not this codec.
Dataclass equality is insufficient for fidelity tests because the embedded
comment fields are excluded from equality.

`review_sensei.finding_lifecycle_store` uses the existing bounded evidence
primitives with purpose `finding-lifecycle`. The session's
`finding-lifecycle-reference-v1` selects a `finding-lifecycle-evidence-v2`
document and binds operation, predecessor root and predecessor snapshot. The
reader reselects current session authority and checks numeric repository/PR,
producer, generation, operation, snapshot and lineage. A digest alone is not
producer authentication or current-root selection. Payloads cannot choose their
own authority, allowance or activation permission.

The lifecycle reference survives ordinary transaction completion and unrelated
session transitions. Writers cannot silently remove it or replace it with an
unrelated predecessor. Existing digest/closed-schema checks make an older reader
refuse a lifecycle-bearing root it cannot preserve; downgrading an active session
to a reader that discards the reference is not a rollback procedure.

The carrier is not transaction-only `publication_staging-v2`, whose 4 KiB/eight
part frontier has a different lifetime. It does not add fields to the closed
`HumanInventoryStore` authority schema. Small documents use the audited inline
encoding; larger documents use the existing authenticated evidence parts and
bounded references, without recursively embedding previous snapshots. Canonical
journal packing deduplicates repeated values losslessly, not by truncating
obligations, prose, receipts or history.

### 6. Effect protocol and native unions

The ordinary application restores selected lifecycle evidence before planning,
command inventory construction or approval decisions. Publisher dictionaries
are working projections of that state, not a substitute authority source.
`review_sensei_github.finding_lifecycle` journals exact native requests without
authentication tokens. A transition must preflight its complete retained future,
known/unknown receipt growth, native dispatch/readback and protected completion
tail before effects. Its order is:

1. Authenticate the current root and reconstruct the complete lifecycle.
2. Plan obligations, exact native intent and the bounded confirmation tail.
3. Persist/read back the pre-effect state and intent under session authority.
4. Revalidate the live target and dispatch once.
5. Retain a confirmed/rejected receipt, or leave a pending unknown outcome.
6. Reconcile exact owned native evidence, advance lifecycle authority and finish
   the original transaction/accounting paths.

A process death after dispatch is not proof of absence. A later process cannot
reissue an unknown request merely because its response was lost. Edited,
foreign, duplicated, missing or incompletely read projections preserve a hold.
A known native success followed by persistence/observation failure remains known
success with missing completion proof, not permission for a replacement write.
Original charges, reservations and unresolved holds remain in force.

`native-review-union-v2` represents current inline membership reused from old
native roots as well as newly allocated part comments. Reused records bind
instance, actual thread/comment/review IDs, original native commit and exact
current body digest. The existing complete native census proves those roots,
producer and final-frame digest; merely omitting them from a new-part plan would
not be complete evidence. Replay reconstructs only new allocations and verifies
the retained membership. Unmodified v1 receipts remain readable; an old v1
reader cannot erase v2 reused-root fields.

### 7. Enrollment, legacy state and limits

A provably new admitted lifecycle can create its first reference before effects.
A supported existing reference restores normally. Legacy migration requires a
complete authentic source for every obligation, original criterion, binding,
uncertain intent, receipt and original funding fact. Compatibility with old
session metadata or native-union v1 alone does not supply that missing lifecycle.
A reduced `ReviewBaseline`, complete file coverage, resolved/outdated host UI,
explicit-reanalysis flag or an omitted prior-context argument is not an empty
inventory. An enrolled legacy record without sufficient evidence retains
`durable_finding_inventory_recovery_required`; malformed, corrupt, foreign,
conflicting or unsupported evidence refuses before business effects. No generic
reconstruction from missing historical data is promised.

All existing limits remain independent and cumulative: 256 evidence parts,
200 comment allocations, 8 MiB retained evidence, 512 KiB responses and 8 KiB
queries; `ResultPartStore` additionally retains its 32-part/1 MiB result limit.
The lifecycle payload itself is bounded to 1 MiB. The native union keeps its
4 KiB frame, 50-existing-root and ten-read census limits, so only reused sets
that fit the whole frame are admitted. Protected tails and provider/API budgets
are unchanged. A representable snapshot alone does not prove its next effect,
future receipt and accounting tail fit. No extra census, compressed-lossy
fallback, allowance reset or limit increase is an overflow recovery route.

### 8. Data handling

The internal lifecycle representation retains normalized, validated finding
prose and all admitted comment fields, hidden obligations, original criteria,
exact native intent bodies and bounded receipts. Those are required for faithful
restart; public summaries/status still omit deferred lists/counts and internal
admission flags. They are not raw provider responses, review prompts, raw diffs,
credentials or newly captured provider context.

Storage uses the selected session's existing evidence carrier, retention and
access policy, not a new service or access grant. Local private evidence and
hosted repository-visible evidence are different access boundaries. Hosted
issue-comment carriers remain readable to readers allowed by that repository;
compression/base64 is not encryption. “Internal” means excluded from public
status projections, not an added confidentiality guarantee. See
[data handling](../data-handling.md); enabling this lifecycle must not silently
change an operator's approved evidence-storage destination or visibility.

## Validation and source qualification

The feature's acceptance boundary is ordinary admitted producer → authenticated
persistence → a genuinely fresh process → ordinary publication, verification or
maintainer command → terminal accounting, without duplicate inference/effects
or newly minted funds. Supplied-state domain tests and a safe refusal alone do
not satisfy that boundary.

Relevant suites include:
- `tests/test_active_window_lifecycle_g10.py`
- `tests/test_finding_location_drift_g16.py`
- `tests/test_artifact_mutation_resilience_g14.py`
- `tests/test_finding_lifecycle_codec.py`
- `tests/test_finding_lifecycle_store.py`
- `tests/test_finding_lifecycle_journal.py`
- `tests/test_native_finding_receipts.py`
- `tests/test_native_union_reused_roots.py`
- `tests/test_durable_finding_lifecycle_restart.py`

The restart harness runs ordinary adapters/application paths in distinct
interpreters sharing only persisted synthetic service/ledger bytes. Its cases
cover hidden blockers at cap one, exact replay, native response loss/process
death, projection mutation, drift/ambiguity and cold commands. Codec tests assert
individual comment fields rather than relying on dataclass equality. Grouped
native tests exercise both mixed new/reused and all-reused publication through
the real bounded reader. Capacity and mutation tests qualify their explicit
fixtures, not arbitrary input sizes or missing legacy evidence.

Source-level bounded G10/G14/G16 qualification requires the final exact candidate's
complete acceptance tests, schema/lint/type checks and required remote CI.
Until those results are recorded, this is implemented source under qualification,
not a completed gap-closure or production-readiness claim. G15 trust qualification
remains separate.

## Check receipts, deployment and rollback

Check observations are bound to repository/PR, base/head and result digest.
A confirmed native check response precedes optional observation; observer
failure preserves the native ID and charge without reposting. Its
`authority_evaluated=False` receipt does not authenticate lifecycle inventory,
trust or historical settlement.

This source work does not authorize a release, deployment, reenrollment, live
recovery or historical settlement. Deploy reader support before enabling new
lifecycle/native-union writers. Preserve retained references, receipts and
funding during rollback; an older unsupported reader must remain fail-closed,
not clear the new reference and restart a session.

G02/G08 all-writer containment, G15 authentic durable trust transitions, G17/G34
original-context recovery and historical incidents have separate evidence and
activation requirements. Finding-lifecycle source tests neither settle those
incidents nor establish live cross-process takeover safety outside the existing
session-owner boundary.

### Opt-in current-observation source contract

The v4 run-authority request retains the existing request scope and adds no
caller-supplied trust facts. Its signed session grant includes a closed
`current-observation-v1` document derived from the broker's existing authenticated
GitHub and OIDC observations: repository/owner identity, installation identity,
current repository access and selection mode, installation and effective token
permissions, and actor identity. Source association remains distinct from actor
repository permission. The existing grant MAC covers the whole observation.

V3 remains compatible and is still the ordinary Python request producer. V4 is
restricted to signed `review_session` exchanges with a session scope; token-only,
release-only and other capability routes refuse rather than silently downgrade.
No extra HTTP call, token scope, storage backend or deployed binding is introduced.

This is a qualified observation producer/parser, not durable enrollment or
per-native-effect freshness. It does not supply a permission epoch, continuous
selection history or evidence that a revoke/restore transition did not occur
between observations. Those claims require an authenticated durable event
producer or an explicitly accepted narrower product contract. Neither option is
implicitly selected by this source change.


### Explicit legacy profile and historical lifecycle origins

The local compatibility profile is selected only for the exact source-owned
local ledger/publisher/finalizer combination, with hosted-ledger and lifecycle
write flags disabled, no active-window or drift enrollment, no cached lifecycle,
native or trust uncertainty, and a successfully loaded root without a lifecycle
reference. It preserves the existing ledger, history and finalizer. Enrolled,
custom, malformed and lifecycle-bearing contexts do not take this route. This
corrects producer/consumer profile asymmetry, not a migration or empty-history
fallback. Existing 77/100/250 human-inventory journeys exercise the original
profile without changing their provider-count or approval assertions.

`quarantine-lifecycle-published-assessment-source-v1` keeps the historical source's
closed four-field shape while giving its assessment IDs explicit immutable
semantics: `instance` is the original obligation ID and `concern` is the original
resolution criterion digest, independent of latest display comments. Its
original record is authenticated by the selected root or enclosing predecessor
archive. Fresh charged partition reads validate manifest, length, producer and
scope; the reader accepts self-contained packed-v3 journals, not v4 dependency
references lacking this historical proof contract. It does not synthesize a
current `load` or restore historical state as present authority.

The journal binds confirmed main-review POST or scoped PUT commitments to fresh
native review identity, owner, head, body and finding index. Current-scope fences
surround the operation. Required blocking and human obligations include hidden
findings; complete-origin mode preserves resolved criteria, and distinct origins
preserve duplicate obligations. The existing 100-assessment/24,000-byte per-origin
and 200-assessment aggregate caps remain unchanged. Missing/edited/foreign
carriers, native body/owner/head mutation, duplicate publication or version
conflict hold. `tests/test_quarantine_lifecycle_origin.py` covers an actual
cap-one producer with three obligations and a genuinely archived predecessor,
not a supplied synthetic lifecycle dictionary. This source reader qualification
does not establish a complete successor-quarantine journey or settle its prior
uncertain effects.

The ordinary remaining-review consumer compares native eligibility with the
actual display projection reconstructed from the same selected lifecycle root,
not the unprojected original result. Original retained result identity/content,
transaction scope, journal validation, fresh root/dependency fences and the
existing caller meter still apply. Native edits or a foreign/unprojected digest
refuse before new inference or publication. A historical rescan receipt without
its current transaction does not establish a current lifecycle projection;
that retry remains held pending a separately authenticated historical contract.

### Source-produced original observation enrollment (opt-in)

`review-run`, `command-run` and `reply-run` accept
`--enable-current-trust-enrollment` before their `--` argument separator. This
requires a compatible broker supporting the existing signed v4 session protocol;
normal token-only exchanges and the default no-enrollment v3 route are unchanged.
No deployment or live activation is implied by this source implementation.

The actual validated session exchange issues a process-local observation receipt.
A supplied snapshot/model, copied receipt or substituted source method cannot
issue one. Initial enrollment retains a closed, integrity-bound inline original
observation in the existing native session root, before its first POST. It stores
no token, opaque session grant or OIDC credential. The original observation is
immutable across ordinary session evolution; it cannot be silently replaced with
today's facts or removed to make a migration succeed. Existing root and 8 KiB
component limits apply, with complete serialization checked before the write.

Opted-in admission reuses the ordinary authenticated native root load. Its cold
receipt binds immutable original root/enrollment bytes and the actual Bot owner;
it is not a fresh installation observation or a writer capability. A new v4
session exchange supplies the separately observed current facts for comparison.
A retained enrollment with the flag disabled holds rather than downgrading to v3;
a legacy existing session cannot manufacture original enrollment by enabling the
flag. Both cases require explicit supported recovery/migration, not a reset.

This qualifies original observation retention and current admission comparisons,
not continuous permission/selection history, an invented permission epoch, actor
repository permission, or fresh trust at every native effect. Existing native
source/actor and finalization gates remain in force. A per-effect observation
producer and its complete prepaid cost remain separate work. Reverification of
an old session grant must never be described as a new observation.

### Source-only refreshed observations and explicit original actor role

The broker source now has a separate, explicitly requested observation refresh.
An opted-in v4 session exchange may request `observe_actor_role: true` to retain
`current-observation-v2`: the actual collaborator-permission response must bind
both numeric user ID and login to the authenticated run actor. Its legacy
permission and role name are retained separately; association never supplies a
role. Existing v3 behavior and v4/v1 observation issuance remain unchanged. A v1
original is still readable/verifiable but cannot qualify the stronger refresh:
`broker_original_actor_role_unavailable` is a hold, not an inferred migration.

The existing session-grant endpoint accepts an explicit
`refresh_effect_sha256` request. This source route verifies the unchanged original
signed attestation and reads current installation/selection/permissions,
repository identity, PR head, numeric actor permission, and (for action runs)
exact original command/reply source. Changed facts hold rather than silently
replace the original. The response keeps original attestation and current facts
separate and binds the result to the requested digest. It issues no publication
token, writes no native object, renews no grant, and proves no continuous history.
Distinct action runs retain their own authenticated actors; the original review
actor is not blanket command ownership. Operation-specific authorization still
belongs to the existing command/effect policy, not this observation parser.

Before dispatch, refresh admits its complete bounded internal read cost: eight
for review or nine for command/reply, including the worst-case public Git failure
and annotated REST-tag fallback. A one-short control refuses before any network
request. This stays inside the existing 60-subrequest service ceiling. The one
internal scoped-token issuance requests only existing review-session permissions;
it is not a native review/check/session mutation or new persistent grant.

This backend slice is **not yet a per-effect caller qualification**. No ordinary
caller emits the new role request or refresh request in this slice. Startup v4
and reused capability redemption remain insufficient for effect freshness. Each
supported future native effect will need a prepaid additional caller exchange,
exact source-owned effect binding, and a single-use consumption boundary that
cannot recurse through the journal. Complete success, readback, and terminal
liabilities must be admitted before the first effect under the original total
funding. Unsupported routes and unavailable evidence must remain named holds;
unknown outcomes after a native effect must remain unknown until reconciled.
No deployment or live activation is qualified by local source tests.

A separately tested, non-CLI initialization primitive now applies the refresh
contract to the actual `initialize_and_activate` source path. It requires the
exact prepared initial root, captured native implementation, original issued
session token, caller/run identity, and one additional physical broker request
inside the existing startup slot. Before that request it preserves the complete
existing native write/confirmation tail. The same effect digest, including the
intended physical sequence and a local authentication hash, is refreshed and
consumed once before the root POST. Changed role, substituted source/token,
changed request bytes/headers, and one-short funding refuse before the root;
a lost native response retains its unknown accounting and is not replayed.
This primitive introduces no public effect-mode CLI flag. After initialization,
unpriced business routes still hold before their first native mutation. It does
not qualify review/check/command finalization, partitioned writes, or recovery.

Capability provenance is now separately source-registered from actual v3
`BrokerClient.exchange` completion and its validated physical response. Its
closure-private receipt binds the returned credential hash, exact capability,
request scope, configured broker, and original caller. A supplied token/model,
substituted exchange/transport, mismatched response capability, changed source
profile, or capability beyond the original observed installation permissions
cannot qualify. Legacy/custom exchanges retain their old return behavior but
issue no unsupported provenance. Looking up provenance spends no request and
**does not make a reused token fresh**: business dispatch still needs a complete
prepaid route and a separate exact-effect refresh. This source facility enables
neither a public effect-mode flag nor an unpriced business mutation.

The first stronger checkpoint pricing profile is deliberately plain and inline
only (`plain-trust-inline-v1`). It binds the actual immutable effect profile and
authenticated v2 original root, adds exactly one root verification to each
source-issued checkpoint quote, and carries that addition through both outer
conditional and inner post-mutator reservations. Missing source scope, an old
original without actor role, partitioned prospective writes, quarantine, and
restart are explicit holds rather than generic lower-price fallbacks. This
pricing grants no business dispatch permit. Existing default profiles, total
funding, retention bounds, and unknown histories remain unchanged.


The initial fresh-effect command route is source-qualified only for `status`
on a current plain root with no retained lifecycle, disposition snapshot,
historical lineage, reservation, or review transaction. It captures the actual
ordinary command implementation and pins its transport, ledger, source fence,
and HTTP methods. Before publishing intent it reserves the existing bounded
work requirements: 57 calls (one admission load, two root operations including
one refresh each, two status loads, two source/head fences, and the existing
grant-verification exchange), plus 11 calls from the already-reserved terminal
block. No allowance or operation limit is increased. The three permitted native
writes are exact source-prepared root PATCH transitions: intent, execution entry,
and terminal status result. Each receives its own fresh exact-effect observation.
An unknown write keeps its original debit and only the existing exact-root
readback may settle it; the permit never admits a replay. The invocation-local
permit disappears on success or failure. Other commands and retained-history
routes remain explicit holds, and no public effect-mode switch is exposed by
this source slice. This qualifies neither continuous permission history nor
live deployment, and does not close G15.

The status source seal includes its explicit transitive helper and constructor
bindings, including locally imported disposition/settlement functions. It is
checked before every physical request, not only before mutations, so replacing
a helper after an earlier root cannot introduce unpriced reads. Admission also
requires the real caller budget facade and immutable accounting/configuration
source, reconciles original authenticated block balances with physical debits,
and checks subsequent balance reductions against the actual request trace.
These checks neither replenish work nor move terminal capacity into work.
