# Independent Epic238 architecture review

Status: Proposed review recommendations, not an accepted runtime contract.
Date: 2026-10-10 UTC. Decision owner: maintainer and Epic238 integration coordinator.
Related issues: [#238](https://github.com/malsabbagh/review-sensei/issues/238)
and existing #239–246. Implementation remains with their six lanes; this review
changes no runtime code, interfaces, ownership, release, or deployment.

## Decision and readiness

Keep the documented provider-neutral pipeline and existing storage adapters:
**review → complete findings → fix or explain → reassess → explicit decision**.
Use one reviewed authenticated partition/reader contract and bounded durable
operation receipts. Do not introduce another evidence service, competing queue,
or an approval shortcut. Freeze integration only after the contract decisions
below have an owner and an executable combined acceptance case.

**Not ready to close Epic238 or qualify the complete-evidence release.** Current
source implements valuable fail-closed behavior and the cross-file packing fix,
but does not supply the complete advertised lifecycle. Shared context can prevent
all discovery, default full-publication recovery can lose its required input,
and the epic contradicts the existing missing-check approval policy. Partition,
feedback, queue, source, publication, and finalizer seams need combined tests.
Passing source tests is evidence about that source, not about installed or
published wheel/native/npm bytes or the deployed action v5.

This decision record recommends completing those seams rather than repeatedly
redesigning the engine. Maintainer approval belongs in the coordinator's canonical
ADR; this document neither registers another numbered ADR nor marks ADR 0072
Accepted. Its implementation is merged while its recorded status remains Proposed.

## Inputs and evidence status

Latest inspected main is `e8fdcd18f8256b1ceaab323701a2570b208d4e75`, including
PR231. The runtime audit and synthetic probes pin its unchanged runtime ancestor
`70305207647c22595660e5ebb9e4a33991489ffe`; local ancestry checks include merged
PRs #235/#236/#237/#247. PR231 changes the release workflow/helper/tests/docs,
not `src/review_sensei` or the review workflow. The fresh diff was inspected and
the complete local gates rerun on latest main. PR235's diagrams pin historical
`a598d4ae47892d243ce67333bcc5c182da95637a`; its separate PR236 proposal pins
`9fb5ad6bf74c8cd169b730df4bd5232f59ef2d7d`. Historical diagrams are preserved,
not silently relabeled current. Runtime code links below pin the unchanged
`7030520` source; release/prototype links identify their separate immutable SHA.

“Implemented” below means inspected source at that SHA. “Proposed” means an
issue/interface recommendation. “Contradicted” means source/tests disagree with
the stated claim. “Unverified” means the requisite observation was not retained.
None means “shipped” by implication. The main workflow reads a version from its
workflow commit and installs that exact PyPI version first; only an unavailable
distribution allows exact-workflow-commit fallback. A development checkout with
the same version string does not prove its bytes are in PyPI, npm, or action v5.
([workflow][workflow], [rollout](review-runtime-rollout.md))

Emerging design was reviewed before interface lock: A's proposal **v1.1** at
`docs/proposals/issue-240-authenticated-partitions.md` in its isolated lane,
SHA256 `b090f9379a3032449ce51a7ae33335c4c9f9d326f58c1c051d15438e768bc937`.
It specifies 32 KiB framed parts, 32 codec parts, 1 MiB framed / 2 MiB decoded,
8 KiB manifest, 256 retained parts / 8 MiB, one 64-dispatch / 60-second absolute
budget with four final fences, and existing adapter ownership. These are proposed
codec envelopes, **not measured end-to-end capacity**. Actual root allocation,
enumeration, staging/readback, receipts and finalization can admit less.
The coordinator confirms that timing, persistence, concurrency, and retention
are not frozen. B's proposed instance identity hashes exact path/line/side/body,
with a separate lifecycle hash and metadata-bound inventory digest; C/D supply
trusted explicit target/source APIs. Their integrated behavior is unverified.

A's codec prototype was subsequently frozen at
`ba89418d21898d14e6d1e535a5409c47c7cff3b2` in draft PR250. Its queue/root-slot
amendment remains separate. C's inspected working journal raised retained
operations from eight to **32**; this addresses the minimum-run count, but is not
a measured 250-finding lifecycle guarantee under the 2 MiB receipt aggregate.
Neither prototype is merged by this review.

## Claim-to-code-and-test matrix

| Claim or handoff | Current source evidence and test oracle | Status / consequence |
| --- | --- | --- |
| Bare library review produces an outcome, not a GitHub transaction | [ReviewService][service]; `test_review_work`, `test_stages` | Implemented. Host owns authorization, ledger and publication. |
| Normal CLI supplies trusted lens selection, learnings and context | [CLI context construction][cli-context], [selector][context]; `test_context`, `test_context_capacity` | Implemented. Required documents and optional excerpts are different obligations. |
| All configured/custom lenses execute identically on every route | [normal CLI][cli], [broader service construction][cli-broader], [bare broader request][reassessment-broader] | Contradicted if asserted. Targeted assessment has no lens stages; broader discovery lacks normal lens documents/learnings/applicability. Hosted full review does not pass custom stage-directory flags. Historical guide discloses this. |
| Splitting files/hunks makes any legally selected context reviewable | [discovery render][discovery-render], [formatter][formatter], [planner admission][packing]; synthetic shared-context probe | Contradicted. A legal 50,000 B document plus a 93 B diff gives zero calls and partial coverage. Shared context is repeated in each batch. |
| Category agreement proves a defect | [stage aggregation][service], [candidate verifier][verifier] | Unsupported. Deduplication and schema validation are not independent semantic proof. Confirmed verification requires exact supplied evidence; default analysis is legacy evidence policy. |
| Current main still writes only two baseline findings / counts duplicate paths before deduplication | [baseline writer][baseline], [path admission][baseline-paths]; capacity and duplicate-path probes | Contradicted as a current claim; correct historical snapshot. Inline complete metadata supports larger inventories within finite byte/lifecycle bounds. |
| A legal 250-comment ReviewResult necessarily persists and publishes | [validation][validation], [baseline][baseline], [human inventory][human], [publication][publication]; varied capacity probe | Contradicted. Count ceilings, immutable inventory, resolution growth and framed review bytes are separate. Varied 100/250 profiles refuse. |
| One finding needing five/eight small complete patches is rejected for reference count | [packing][packing]; `test_review_work` and 1/4/5/8-path probe | Fixed in current main; each group gets one call/decision. Genuine rendered-prompt/diff overflow still refuses. #239 needs final selected-source qualification, not another implementation. |
| Naming a late finding in ordinary prose targets it | [requirements][reassessment], [sorted capped planner][planning]; 50-finding late-target probe | Contradicted. Eight calls give 32 decisions; an empty-decision reply repeats the same first 32. C's trusted target/queue API is Proposed. |
| Existing default hosted runs durably resume every accepted batch | [executor][execution], [optional recovery][recovery], [reply orchestration][application] | Unverified/absent as a default product claim. Current private diagnostic recovery is opt-in. C's authenticated checkpoint is Proposed. |
| A baseline and result digest reconstruct full pending publication | [pending CLI branch][cli-recovery], [transaction tests][transaction-tests], [architecture](architecture.md) | Contradicted. Retry requires original validated result and trusted admission contexts; default lost-runner recovery needs an owner. |
| Conversation history is complete human evidence | [bounded conversation][conversation], [source reload][human-host] | Contradicted. Chat selects 20 messages and clips each to 1 KiB. Separate admitted human source is complete only up to 4 KiB. |
| Long selected sources always fit a provider request | [4 KiB source admission][human], [48 KiB effective prompt][budgets], [render][reassessment] | Proposed, not implemented. A 64 KiB source or 256 KiB aggregate cannot be inserted whole into a 48 KiB prompt. D's full prompt preflight is required. |
| A current-head explanation can assess unchanged source | [changed-file acquisition][acquisition], [required path lookup][reassessment], [citation validation][human] | Absent for unchanged paths. Missing exact path patch gives zero calls/pending. #244 must decide a trusted complete source-evidence seam or document precise supported refusal. Historical inline fragments cannot supply it. |
| Broader review may retire old findings by omission | [eligibility union][approval-union], `test_work_continuation` | Contradicted; old obligations and stricter groups remain. New result is separate publication authority. |
| Large atomic groups can resolve from independent fragment decisions | [atomic planner][packing], [by-path evidence][evidence] | Unsupported. Discovery hunk partitioning differs from complete reassessment groups. E proposes exhaustive materialization and safe refusal, not multipiece aggregate-resolution authority. |
| Adaptive evidence pagination is exhaustive at every resource limit | [HTTP pagination][http], [snapshot fences][acquisition]; `test_github_http`, 999/1000/1001 probe | Conditional. Same-offset shrink exists. Exact 1,000 full-page items still refuse without EOF proof at current main; E proposes a count-bound EOF probe. |
| A complete part codec makes latest authority discoverable | [fixed-page root discovery][ledger], [fixed-page review reader][eligibility-reader]; 20 × 32 KiB history probe | Contradicted as a general claim. Current fixed-100 scan exceeds 512 KiB. A proposes bounded five-item metadata scans; all review/eligibility readers and repeated-operation pressure remain qualification work. |
| Digests authenticate parts or source authors | [ledger ownership][ledger], [source authorization][human-host], A v1.1 | Contradicted. Adapter-observed producer/association authenticates; hash/length detects alteration. No part-selected arbitrary URL/path or credential is allowed. |
| GitHub generation checks are atomic CAS / workflow concurrency is FIFO | [ledger race boundary][ledger], [workflow groups][workflow], historical guide | Contradicted. GitHub PATCH remains non-atomic; local independent processes require serialization; a queued GitHub job can be replaced. |
| Retry/cancel/restart grants fresh resources automatically | [executor][execution], [tracker][outcomes], `test_work_recovery`, `test_review_transaction` | Contradicted. Dispatches/retries are charged and recovery retains expiry. Proposed default receipts must preserve this across process boundaries. |
| Remaining human concern, required fix, incomplete coverage or stale latest authority can approve | [facts][approval-facts], [finalizer][finalizer], `test_human_assessment`, `test_github_approval` | Withheld by current tested gates. New parts/receipts must retain the same negatives. |
| Missing published check always prevents APPROVE | [facts contract][approval-facts], [approval evaluator][approval-evaluator], [positive contrary test][missing-check-test], [ADR 0057](adr/0057-one-check-run-as-the-single-merge-authority.md) | **Contradicted by deliberate existing policy.** Epic238's negative matrix requires disposition before qualification; do not change policy incidentally. |
| Successful reply, check, CI or manual thread resolution proves assessment/approval | [separate outcomes][application], [finalizer][finalizer], `test_gate_and_default_approval` | Unsupported. Source delivery, decision receipts, pending inventory, enforcement availability and final review state are separate observations. |
| Current source parity proves wheel/npm/action v5/old-reader deployment parity | [distribution contract][distribution], [reader harness][readers], [workflow][workflow], #246 | Unverified. Each actual artifact and each active/delayed reader requires its own exact-byte evidence. |
| PR231's verified draft establishes complete-evidence runtime qualification | [release workflow][release-workflow], [draft helper][release-helper], `test_github_release`; ADR 0069 | Contradicted if asserted. Latest main binds four GitHub assets/notes to source/run/tag/checksums/attestations and checks registry-job success before draft publication. First authorized live observation and registry-installed/runtime/v5 qualification remain separate. |

## Invariants and contract handoffs

The following diagram is a **proposed integration contract**, not a new claim
about current storage or universal completion. Existing gates are preserved;
durable payload/queue/source wiring is qualification work.

```mermaid
sequenceDiagram
    actor Human
    participant Host as Trusted CLI or hosted adapter
    participant Work as Bounded planner and executor
    participant Store as Owned root and immutable parts
    participant Pub as Review publisher
    participant Gate as Exact-head finalizer
    Human->>Host: Review or explicit fix/explain and target/source selection
    Host->>Store: Read latest owned complete authority
    Host->>Host: Fence snapshot, source authorization, context and rendered budgets
    alt Missing, stale, unreadable or unsupported complete evidence
        Host-->>Human: Precise refusal and retained pending obligations
    else Admitted operation
        Host->>Work: Immutable inputs and original operation accounting
        loop Bounded admitted dispatches
            Work->>Store: Charge dispatch and checkpoint receipt
            Work->>Work: Validate assigned decisions and exact evidence
            Work->>Store: Persist accepted receipt and remaining queue
        end
        Work-->>Host: Accepted, rejected and unprocessed identities
        Host->>Store: Stage complete publication inputs and parts
        Store-->>Host: Owner-checked readback and whole-inventory equality
        Host->>Store: Activate fenced root, preserving old obligations
        Host->>Pub: Reconcile exact original payload and remote mutation
        Pub-->>Host: Delivery and enforcement diagnostics
        Host->>Gate: Latest durable eligibility for exact head
        Gate->>Gate: Static facts, live threads, snapshot and latest-authority fences
        alt Eligible under explicitly selected approval policy
            Gate->>Pub: Reconcile or emit exact-head APPROVE
        else Any remaining blocker or unavailable proof
            Gate-->>Human: Withheld decision and explicit remaining work
        end
    end
```

| ID | Required invariant | Acceptance oracle |
| --- | --- | --- |
| I1 Snapshot and authority | Repository/PR/base/head, policy/configuration/provider/model and producer are bound; stale receipts never transfer. | Push/rebase/base advance or same-head newer result at every read/write fence withholds stale work. |
| I2 Complete coverage | Enumeration, selected lens/source coverage, stage execution, durable inventory and human resolution are distinct; skipped work cannot become reviewed. | Exact file/hunk/context/identity set equality; omissions and reasons survive publication. |
| I3 Activation | Parts and original publication inputs are staged and fully read back before their root becomes complete authority. | Kill after every write/readback/root boundary: old valid root or complete new root, never partial authority. |
| I4 Authentication | Root and each part/source require observed owner and association; self-asserted hashes grant no authority. | Foreign numeric author, wrong PR/producer/purpose/schema and tampered parts refuse. |
| I5 Obligation identity | Targeting limits dispatch, not inventory; resolution applies only to exact instance; broader discovery unions obligations. | Similar findings with one old display/lifecycle ID remain individually addressable; metadata/evidence changes do not misapply a receipt. |
| I6 Original operation | Immutable operation identity is separate from mutable cursor/root/receipt revision. Resume retains calls/bytes/retries/deadline/expiry. | Own checkpoint/root changes cannot mint another allowance; kill after charged dispatch cannot erase charge. |
| I7 Fair bounded visits | Every admitted pending item is visited under continued authorized runs; semantic rejection rotates without false resolution. | Late target reaches provider; repeated early rejection does not starve later IDs; no promise of successful semantic resolution. |
| I8 Feedback completeness | Explicit ordered authenticated sources remain verbatim, edit/delete/role change invalidates reuse; context clipping cannot certify resolution. | Exact source body/digest/author/time before reply and activation; required selected feedback and provenance fit real rendered prompts. |
| I9 Publication recovery | Source acknowledgment, decision activation, COMMENT readback and APPROVE are ordered and replayable; lost response reconciles exact mutation. | New process with artifacts none preserves accepted receipts and original publication input; no extra inference/round charge or duplicate sequential mutation. |
| I10 Approval | Complete eligibility, no open required/human obligations, reviewed coverage/qualification, current PR and complete live blocking scan precede exact-head decision. | Full/reply/delayed-finalizer negative suite; missing-check policy explicitly reconciled rather than assumed. |
| I11 Resource composition | Unit/phase/aggregate/read/write/retention bounds include JSON, UTF-8, framing, correction and lifecycle growth. | Combined maximal supported profile completes; above it refuses whole unsupported work with prior authority retained. |
| I12 Concurrency | Same-PR hosted lanes serialize; stale writers refuse; ambiguous remote outcomes reconcile. | Deterministic disjoint replies/full-vs-reply barriers plus process tests; document residual non-atomic GitHub/local races. |
| I13 Readers and privacy | Every authority reader supports active schema; rollback retains required readers and obligations. Defaults do not require raw diagnostics. | Immutable old readers fail closed; exact installed/current/deployed consumers qualify; no new secret or raw-response store. |
| I14 Grant and generation composition | One-attempt hosted mutation authority is separate from operation allowance and immutable inventory identity. Each authorized mutation retains fresh head/role checks without replaying a consumed grant. | Broker-backed multi-batch checkpoint/restart exercises every charge, acceptance, queue and publication root write; no fake unlimited grant, generation-driven allowance reset or unguarded part write. |

## Prioritized corrections and owners

| Priority / ID | Classification and concrete gap | Existing owner / minimum disposition |
| --- | --- | --- |
| P0 R1 | Confirmed source defect in reachable review workflow: legal shared context can admit zero discovery batches, with patch-oriented diagnostics obscuring the cause. | E/#244 plus coordinator's normal/broader context wiring. Preflight fixed context before packing; preserve mandatory guidance, select optional context with provenance/coverage, or return an exact unsupported-context outcome. Never merely lift unqualified limits. |
| P0 R2 | Confirmed specification contradiction: epic says missing check never approves; source/test/ADR deliberately allow independent approval. | Coordinator #238/#246. Retain existing policy and correct acceptance, or obtain a reviewed policy change with ADR/tests across every finalizer. Independent review does not select that policy unilaterally. |
| P1 R3, release blocker | Confirmed lost-runner full-publication recovery input gap: complete baseline/digest cannot reconstruct original result and trusted admission contexts; pending retry requests unavailable original input. | Coordinator with A/B, #240/#241/#245/#246. Choose an owned durable payload/admission-input seam before checkpoint, then prove artifacts-none restart and exact mutation reconciliation. |
| P1 R4, release blocker | Confirmed current authority-scan response pressure; proposed five-item scans also consume the shared read budget as history grows. | A/B/E + coordinator #240/#241/#245. Qualify root discovery and latest eligibility/review scans, not only part resolution; include repeated operations, unrelated comments and retention ceilings. |
| P1 R5 | Proposed resource composition risk: one absolute 60 s transport guard can expire while an allowed 120 s provider call runs; four calls may not cover all latest-root/source/check/part/final fences. | Coordinator and A/C/D/F. Specify when each deadline starts and what provider time counts; test 59/60/61 s and nearly exhausted dispatches with a fake clock through real adapters. Never reset a resumed operation or skip fences. |
| P1 R6 | Confirmed current targeting/starvation/feedback limits; proposed trusted APIs alone do not complete a human-facing workflow. | C/D/B + coordinator #242/#243. Publish discoverable full instance IDs and explicit ordered source/target/continuation entrypoint; test actual CLI/hosted invocation, rejected IDs and multi-source edits. No ambient prose scope parser is assumed. |
| P1 R7 | Proposed aggregation and scope handoff risk: larger admitted sources/parts need the real prompt/evidence contract; checkpoint mode must not turn context/broader requests into permanent unserviceable pending state. | C/D/E + coordinator #242/#243/#244. Whole supported feedback/group evidence must reach a validated decision, or have an explicit supported next operation. No independently accepted fragment resolution or undocumented scope disabling. |
| P1 R8 | Confirmed unchanged-path limitation and route context differences. | E/#244 and coordinator. Decide trusted exact-source materialization/citations and normal-vs-broader lens contract; omission, historical hunks or chat excerpts cannot substitute. |
| P1 R9 | Confirmed main replay gap corroborated by independent injected source-ack failure before COMMENT; F separately reports an OS-kill reproduction. Lost response-arrival accounting is another proposed recovery risk. | F/C + coordinator #245. Real kill/restart and barriers; source ack cannot prevent activating/replaying accepted authority. Reserve the trusted maximum response budget before dispatch, retain unknown reservation after loss, convert only on durable capture; do not claim uncaptured bytes restored. Preserve race limitations rather than promise exactly-once universally. |
| P0 qualification R10 | Unverified exact combined artifact and reader-first rollout. | Coordinator #246. Final combined source and installed candidate bytes, then separately authorized published registry/action/deployed observations. Source-only closure is prohibited. |
| P1 R11 | Proposed grant/root-generation composition risk: [A's hosted replace][a-ledger] verifies a one-attempt broker grant on every replace; stage/readback is enclosed within one mutation callback. C requires multiple durable charge/acceptance writes. Codec success does not establish authorization for the entire journal. | Coordinator A/C/#240/#242/#245. Specify grant issuance per allowed mutation, exact expected root digest/generation, and latest immutable inventory authority. Run with a real consuming fake broker and full hosted root writes; never reuse the old grant or change original operation identity after its own root mutation. |
| P1 R12 | Proposed bounded-history/retention risk: C's 32-operation journal plus cumulative accepted receipts, live queue, baseline, feedback/evidence parts, disposition/grant and tombstone retention can hit root/aggregate/scan ceilings before all 250 concerns complete. | A/B/C + coordinator #240/#241/#242. Qualify 250 varied concerns over at least eight runs, repeated rejected visits and same-source restart. Preflight whole root lifecycle and retained objects; retain no-reset tombstones through expiry and refuse capacity before inference. A documented maintainer recovery path must preserve obligations, not silently drop old receipts or start clean. |

R1 has an independent synthetic reproduction. Separately, the PR231 owner retained
[current readiness evidence](https://github.com/malsabbagh/review-sensei/pull/231)
for head `bf408b0c070e7404fefcf50eebc104346228662b`: tagged/current source
singletons 587,077 / 582,298 / 585,246 B against 49,152 B, 73 documents discovered,
64 selected, nine omissions. Hosted runs
[38011224719](https://github.com/malsabbagh/review-sensei/actions/runs/38011224719)
and [38011579322](https://github.com/malsabbagh/review-sensei/actions/runs/38011579322)
failed before publication on released 0.6.18. These are owner-retained
observations, not a claim that hosted diagnostics prove the precise source cause:
artifacts none retained no downloadable result. This review reproduced the
shared-context mechanism offline without dispatching a live provider.

The independent coverage audit at `e8fdcd1` also reports a legal 70,000 B document
rendering a 71,993 B request (legacy one call/complete, unified zero calls/partial),
and four lenses repeating one 20,000 B unique document rendering 83,231 B (zero
unified calls); four lenses with 10,000 B render 42,343 B and complete. Unique
selection deduplication in [models][models] does not deduplicate per-lens rendered
serialization in [formatter][formatter]. It also reports one provider call and
durable source reply before injected source-ack failure, followed by a new call
on fresh-process retry before COMMENT/APPROVE. These are separately retained
synthetic observations, not live-provider or OS-kill observations by this review.

## Executable journey and failure coverage

Existing passing suites establish current behavior; the acceptance column
describes required **combined** evidence, not a claim that proposals passed it.

| Journey / branch | Current oracle | Required combined qualification |
| --- | --- | --- |
| Full library/CLI/hosted review with legal small inputs | `test_review_work`, `test_stages`, `test_review_transaction` | Exact source/config/stage/provider identity and whole coverage survive storage/readback/publication. |
| Full context / custom lenses / mandatory and optional docs | `test_context`, `test_context_capacity`; shared-context probe | Tiny diff plus legal large shared context; required context never dropped, optional omissions explicit; real hosted and broader routes covered. |
| Large diff, multi-hunk, giant atomic unit | `test_review_work`, `test_work_continuation` | Complete supported files/groups; genuine unsupported units remain named pending. |
| 12/21/50/100/250 varied findings and mixed Unicode/deep paths | `test_evidence_capacity`; capacity probe | Whole lifecycle, all-resolution growth, broader union and restart, including aggregate/part/root/retention boundaries. |
| Reply chat versus human assessment | `test_human_assessment`, `test_github_conversation` | Delivery never stands in for assessment; short/long selected sources, inline/issue/root paths each exercised. |
| Late target / repeated rejected early concerns | late-target probe; C acceptance | Public exact-ID selection and durable fair visits; unknown/short/foreign/resolved/stale IDs reject before inference. |
| Feedback 4,095/4,096/4,097 B; 1 KiB clipping; >20 messages | feedback probe; `test_human_assessment` | Selected multi-source content and exact rendered prompt; edits/deletes/role changes at every fence. |
| Explain unchanged source; deletion/rename/binary/missing or truncated patch | unchanged-path probe; `test_review_work` | Exact snapshot/group completeness and supported source citation, or precise refusal with next action. |
| Broader discovery, second-wave requests and old omission | `test_work_continuation` | Default durable mode preserves expansion/union or exposes a supported continuation; checkpoint and broader publication compose. |
| Pagination >100; response ±1 byte; same-offset shrink; 999/1000/1001 | `test_github_http`; history/pagination probes | EOF/count proof, no skips/duplicates, shared deadline and latest-authority scans across retained part pressure. |
| Correction, semantic rejection, timeout/429/5xx and cancel | `test_review_work`, `test_work_recovery`, `test_review_transaction` | Every dispatch charged; only repairable errors retried; queued/rejected items and original expiry survive new process. |
| Kill at reservation, charge, accepted receipt, part readback, root activation, source ack, COMMENT and APPROVE | `test_work_recovery`, `test_human_assessment` cover subsets | F's actual child-process seams across the combined implementation, artifacts none, zero replay inference/lost resolutions. |
| Two replies/full-vs-reply/local-vs-hosted, old/new group keys | ledger/transaction/human suites | Serialized consumer migration and barriers; stale/ambiguous writers reconcile without old-clean fallback or lost updates. |
| Broker-bound multi-batch roots, grants and journal history | `test_session_ledger`; A/C prototypes | Consuming grant verifier, reserve/charge/acceptance/activation/restart, original operation identity after own generation updates, 250-finding lifecycle and no-reset tombstones. |
| Head/base/policy/source change during inference, scans or finalization | `test_human_assessment`, `test_github_publication` | All part/feedback/queue receipts revalidate; same-head newer malformed/unsupported authority wins. |
| Finalizer negatives and already-resolved retry | `test_github_approval`, `test_gate_and_default_approval`, `test_human_assessment` | Exact selected policy; remaining required/human/coverage/qualification/capability/PR/thread blockers; no inference for finalization-only retry. |
| Wheel/sdist, five native targets, six npm packages, v5 readers | distribution and immutable reader harnesses | Install actual built bytes outside checkout; official registry bytes/SRI/checksums/attestations only after authorized publication; actual caller/workflow/broker and delayed readers separately verified. |

## Limit composition at the inspected source

KiB/MiB mean 1,024/1,048,576 bytes. Serialized counts include JSON escaping and
framing; they are not token estimates. Limits below intersect at each real
entrypoint, and public profiles move downward. A field that parses is not proof
that a given CLI/workflow route consumes it. Historical configuration wiring
limitations remain documented in [the guide](review-pipelines.md).

| Boundary / owner | Unit, scope and current ceiling | Interaction and refusal/recovery |
| --- | --- | --- |
| Ordinary diff / public ReviewLimits | UTF-8 1 MiB, 50,000 lines, 500 files, 5,000 hunks | Each rendered request must fit; oversized atomic evidence is named unprocessed. |
| Orchestrated total / TotalWorkBudget | 8 MiB, 400,000 lines, 4,000 files, 40,000 hunks, eight chunks/calls | Whole enumeration ceiling, not durable baseline capacity or eight calls per stage. |
| Request/model / ReviewLimits | Prompt 4 MiB, response 1 MiB | Wider model-constructor ceilings do not supersede actual run or work admission. |
| Run / ResourceBudget | Eight actual calls, two shared structural retries, 120,000 ms; 1 MiB prompt/output per call by default | Transport retries also consume calls; in-flight calls are not interrupted. Deadline is checked before/after. Resume retains accounting. |
| Unified rendered work / ReviewWorkBudgets | Preferred diff 128 KiB, prompt 256 KiB; unqualified effective prompt 48 KiB; totals 2 MiB prompt / 1 MiB output | Actual JSON/context/correction is rendered before admission. Qualified exact provider/model can only intersect reviewed bounds, not self-certify them. |
| Reassessment output / human service | 16 KiB response; 20 decisions/request public ceiling; effective packing normally four | Output-token reserve can reduce packing further. Eight calls imply at most 32 normal decisions before retries/expansion. |
| Result / ReviewLimits | 250 comments, 16 KiB UTF-8 body each, 32 KiB summary, 2 MiB serialized result | Aggregate prevents 250 maximal bodies; publication/marker/all-resolution bounds independently intersect. |
| Lens context / models and selection | 64 documents, 128 KiB UTF-8 each, 512 KiB unique supplied content; 64 contexts/categories | Optional selections carry provenance/omissions; mandatory overflow refuses. Legal selection still needs real prompt admission (R1). |
| Stage/category loader | 32 stage files × 128 KiB; 64 category files × 64 KiB; 16 categories/stage, 16 focus items/category | Configuration is trusted bounded data. Route flags and applicability determine actual execution. |
| Whole baseline / metadata and session | 512 metadata identities/unique paths, 2 MiB decoded; at most 11,264 B encoded baseline, 12,288 B history, 20,480 B record, 40,960 B framed comment | Actual lifecycle reserve reduces allocation; 4,000 analyzed paths cannot imply a reusable complete 512-path baseline. Capacity refusal preserves prior authority and accurate coverage. |
| Human inventory / current marker | 250 whole findings, 16 KiB body, eight paths/finding, 64 unique paths/inventory; 24 KiB serialized marker including all resolutions | Provider batching cannot enlarge whole inventory. Mutable resolution growth is preflighted. A/B partitions remain Proposed. |
| Human feedback / current service | 4 KiB complete UTF-8 source; legacy required diff 12 KiB including framing | >4 KiB refuses without inference; chat clipping never supplies resolution authority. D's larger source contract requires new reviewed rendered-prompt wiring. |
| Hosted chat / conversation | 20 messages, 1 KiB/message; 12 KiB diff, 512 B finding excerpt, 6 KiB learnings; prompt 64 KiB, reply 16 KiB | Deliberate bounded selection; it is not a complete feedback archive or approval input. |
| REST/authority and graph threads | 512 KiB response, 1,000 collected list items; ten × 100 graph thread pages | Cap/oversized item/incomplete scan refuses authority; no older-clean fallback. Exact-cap EOF and part-history pressure need qualification. |
| Evidence acquisition / GitHub adapter | Same-offset pages 100/50/25/5/1, 64 requests including fences, 60 s or remaining deadline | Transport/read budget exhaustion remains pending. It cannot reconstruct unchanged source or truncated patches. |
| Framed GitHub review / publisher | 65,536 UTF-8 B including markers/inventory/prose | A legal 2 MiB result may not publish. New multipart publication requires complete readback and original payload recovery. |
| Retained session / ledger | TTL 30 days default / 90 maximum; three progress entries, four dispositions, four continuation grants | Expiry/record growth cannot reset obligations or manufacture a new clean session. |
| Opt-in work diagnostics / WorkRecoveryStore | 2 MiB/artifact, eight artifacts / 8 MiB store; six-hour default / 24-hour maximum expiry, private host-owned key/directory | Off by default, not default hosted durability; retained artifacts grant no independent approval authority. |
| Proposed A/D aggregate contracts | A v1.1 limits above; D ordered source selection has separately reviewed finite per-source/aggregate counts/bytes | Neither changes public analysis limits nor proves all legal codec objects can complete within one combined read/provider/publication envelope. |

Sources: [public limits][validation], [work budgets][budgets], [run resource
contract][outcomes], [models][models], [stage loader][stage-loader],
[baseline][baseline], [session][session], [human][human], [conversation][conversation],
[HTTP][http], [acquisition][acquisition], [publication][publication], [recovery][recovery].

## Measured boundaries and validation record

Credential-free synthetic probes against the pinned main, using existing public
test fixtures, produced:

| Probe | Observed result | Unit / implication |
| --- | --- | --- |
| Varied 12/21/50 baseline | 2,354 / 3,658 / 7,643 B; admitted | Compact encoded JSON; actual session allocation can be lower. |
| Corresponding all-resolved human inventory | 9,123 / 7,297 / 15,132 B; admitted | Serialized marker including every resolution. Nonmonotonicity comes from plain/encoded format switch. |
| Varied 100/250 | Baseline and human inventory refused | In-memory count ceiling is not durable support. |
| Atomic 1/4/5/8-path concern | 1 call, 1 decision each; 64/256/320/512 raw patch B | #239 packing correction verified; framing/prompt capacity still independent. |
| 50 findings / empty-decision late-target repeat | 8 calls, 32 decisions / same first 32 repeated | Eight dispatches and four decisions/batch; late ID not selected by prose. |
| Source 4,095 / 4,096 / 4,097 B | 1 call / 1 call / zero-call refusal | UTF-8 source admission, not chat clipping or model tokens. |
| Unchanged required path absent from changed-file bundle | zero calls, required-evidence-missing | No source lookup authority is invented. |
| 600 duplicate / 513 distinct baseline paths | admitted / refused | Current path ceiling counts unique validated paths; historical guide describes an older rule. |
| Twenty history bodies × 32,768 B | fixed scan refuses; adaptive 100/50/25/5/1 retrieves all in 8 requests | 512 KiB response includes JSON/API overhead; aggregate and per-object limits differ. |
| 999 / 1000 / 1001 list entries | admitted / refused / refused at 10 requests | Current exact-cap EOF limitation; safe refusal, not complete enumeration. |
| 100 / 50,000 / 100,000 B selected document with 93 B diff | reviewed / partial zero calls / partial zero calls | Document model admits up to 128 KiB each, selected unique content up to 512 KiB; shared rendered context must also fit 48 KiB unqualified batches. |

The standalone probe and numeric results are retained in the review workspace
(`architecture_probe.py` SHA256
`d9ff5e209c280fcaa2ae374c1341ff489a69d31d3c16ad0ea29414569c9296d6`;
`architecture-probe-results.json` SHA256
`8b462bd09d36950d2a004fd448fbd45cb010436b7a20ecae73b1bad7d1a7fe65`).
The rerun at latest main `e8fdcd1` produced identical observations after comparing
all fields except the source SHA; `architecture-probe-results-latest.json`
SHA256 is `43a83c8132f992f21499068b606b2473d6cb2eb4dd0848078669f4f14d3b5aa0`.
They are resource scenarios using synthetic providers/transports, not production
defect claims or provider semantic qualification. The 393-test focused suite
passed. The original runtime coverage suite passed 2,685 tests. On latest main,
both clean full suites passed **2,714 tests** (coverage: 94.365 s; ordinary:
88.392 s), with **83.02%** branch coverage above the unchanged 80% floor.
Action pins, JSON contracts, Ruff formatting/lint, mypy, compileall, build, sdist
contract and diff checks passed. All five sequence diagrams parsed with
Mermaid 11.12.0; 95 immutable source links/line anchors and local references
resolved. Immutable v0.6.16/current reader checks
passed for legacy and encoded inputs, while explicitly reporting the historical
oversized-comment-as-missing limitation; this does not qualify proposed manifests
or permit rolling back required readers. Full source/doc validation is recorded
in the draft PR; no live provider,
historical PR reassessment, release or deployment is part of this review.

## Consequences, alternatives, rollout and rollback

Prefer a common owned manifest/reader plus separate immutable operation identity
over expanding single-comment limits or adding an external object service.
The first preserves adapter authentication and existing privacy; the second
retains the measured lifecycle bottleneck; the third adds unapproved ownership,
credentials and recovery semantics. Likewise, explicit bounded source/target
selection is reviewable; automatically scraping all discussion or parsing scope
from model prose is neither an authorized completeness proof nor a dependable
user workflow.

Storage compression or partitioning preserves bytes; it does not qualify a model's
context window, verify a defect, make large required source/feedback fragments
semantically complete, or guarantee eventual successful resolution. Provenance,
strict schemas/citations, exact receipts and withholding contain unsupported
claims at handoffs; they cannot eliminate model hallucinations or regressions.

Reader-first rollout must inventory full, reply, command, recovery, verification,
CLI, hosted/local and delayed finalizer consumers, then drain old/new concurrency
groups before rich writers activate. Rollback retains readers needed by written
authority, all active parts and unresolved obligations. No ledger deletion,
older-clean fallback, version-tag replacement or rebuilding under a published
version is a recovery strategy. Finite history/retention exhaustion has an honest
terminal outcome; garbage collection and new infrastructure require their own
reviewed contract.

Epic done requires one selected combined SHA with meaningful end-to-end fault,
fairness, context, feedback, readback and approval tests; a clean installed wheel
and every supported native/npm artifact; immutable legacy/current readers; and
separate authorized published/deployed observations. Unavailable lanes remain
pending. A capacity profile states finite supported workloads and recovery
actions, never infinite capacity or a zero-regression promise.

[service]: https://github.com/malsabbagh/review-sensei/blob/70305207647c22595660e5ebb9e4a33991489ffe/src/review_sensei/service.py
[cli]: https://github.com/malsabbagh/review-sensei/blob/70305207647c22595660e5ebb9e4a33991489ffe/src/review_sensei/cli.py
[cli-context]: https://github.com/malsabbagh/review-sensei/blob/70305207647c22595660e5ebb9e4a33991489ffe/src/review_sensei/cli.py#L3700
[cli-broader]: https://github.com/malsabbagh/review-sensei/blob/70305207647c22595660e5ebb9e4a33991489ffe/src/review_sensei/cli.py#L2506
[cli-recovery]: https://github.com/malsabbagh/review-sensei/blob/70305207647c22595660e5ebb9e4a33991489ffe/src/review_sensei/cli.py#L3897
[context]: https://github.com/malsabbagh/review-sensei/blob/70305207647c22595660e5ebb9e4a33991489ffe/src/review_sensei/context.py
[discovery-render]: https://github.com/malsabbagh/review-sensei/blob/70305207647c22595660e5ebb9e4a33991489ffe/src/review_sensei/discovery_work.py#L148
[formatter]: https://github.com/malsabbagh/review-sensei/blob/70305207647c22595660e5ebb9e4a33991489ffe/src/review_sensei/service.py#L1510
[planning]: https://github.com/malsabbagh/review-sensei/blob/70305207647c22595660e5ebb9e4a33991489ffe/src/review_sensei/planning.py#L212
[packing]: https://github.com/malsabbagh/review-sensei/blob/70305207647c22595660e5ebb9e4a33991489ffe/src/review_sensei/planning.py#L272
[verifier]: https://github.com/malsabbagh/review-sensei/blob/70305207647c22595660e5ebb9e4a33991489ffe/src/review_sensei/verifier.py
[baseline]: https://github.com/malsabbagh/review-sensei/blob/70305207647c22595660e5ebb9e4a33991489ffe/src/review_sensei/baseline.py#L367
[baseline-paths]: https://github.com/malsabbagh/review-sensei/blob/70305207647c22595660e5ebb9e4a33991489ffe/src/review_sensei/baseline.py#L178
[validation]: https://github.com/malsabbagh/review-sensei/blob/70305207647c22595660e5ebb9e4a33991489ffe/src/review_sensei/validation.py
[models]: https://github.com/malsabbagh/review-sensei/blob/70305207647c22595660e5ebb9e4a33991489ffe/src/review_sensei/models.py
[stage-loader]: https://github.com/malsabbagh/review-sensei/blob/70305207647c22595660e5ebb9e4a33991489ffe/src/review_sensei/stages.py
[session]: https://github.com/malsabbagh/review-sensei/blob/70305207647c22595660e5ebb9e4a33991489ffe/src/review_sensei/session.py
[human]: https://github.com/malsabbagh/review-sensei/blob/70305207647c22595660e5ebb9e4a33991489ffe/src/review_sensei/human_assessment.py
[reassessment]: https://github.com/malsabbagh/review-sensei/blob/70305207647c22595660e5ebb9e4a33991489ffe/src/review_sensei/reassessment_work.py#L55
[reassessment-broader]: https://github.com/malsabbagh/review-sensei/blob/70305207647c22595660e5ebb9e4a33991489ffe/src/review_sensei/reassessment_work.py#L290
[budgets]: https://github.com/malsabbagh/review-sensei/blob/70305207647c22595660e5ebb9e4a33991489ffe/src/review_sensei/budgets.py
[execution]: https://github.com/malsabbagh/review-sensei/blob/70305207647c22595660e5ebb9e4a33991489ffe/src/review_sensei/execution.py
[recovery]: https://github.com/malsabbagh/review-sensei/blob/70305207647c22595660e5ebb9e4a33991489ffe/src/review_sensei/work_recovery.py
[outcomes]: https://github.com/malsabbagh/review-sensei/blob/70305207647c22595660e5ebb9e4a33991489ffe/src/review_sensei/outcomes.py
[conversation]: https://github.com/malsabbagh/review-sensei/blob/70305207647c22595660e5ebb9e4a33991489ffe/src/review_sensei/hosting/github/conversation.py#L38
[acquisition]: https://github.com/malsabbagh/review-sensei/blob/70305207647c22595660e5ebb9e4a33991489ffe/src/review_sensei/hosting/github/conversation.py#L821
[human-host]: https://github.com/malsabbagh/review-sensei/blob/70305207647c22595660e5ebb9e4a33991489ffe/src/review_sensei/hosting/github/human_assessment.py#L90
[evidence]: https://github.com/malsabbagh/review-sensei/blob/70305207647c22595660e5ebb9e4a33991489ffe/src/review_sensei/evidence.py#L207
[application]: https://github.com/malsabbagh/review-sensei/blob/70305207647c22595660e5ebb9e4a33991489ffe/src/review_sensei/hosting/github/application.py#L1400
[approval-union]: https://github.com/malsabbagh/review-sensei/blob/70305207647c22595660e5ebb9e4a33991489ffe/src/review_sensei/hosting/github/approval.py#L288
[approval-facts]: https://github.com/malsabbagh/review-sensei/blob/70305207647c22595660e5ebb9e4a33991489ffe/src/review_sensei/hosting/github/approval.py#L41
[approval-evaluator]: https://github.com/malsabbagh/review-sensei/blob/70305207647c22595660e5ebb9e4a33991489ffe/src/review_sensei/hosting/github/approval.py#L374
[finalizer]: https://github.com/malsabbagh/review-sensei/blob/70305207647c22595660e5ebb9e4a33991489ffe/src/review_sensei/hosting/github/publication.py#L671
[eligibility-reader]: https://github.com/malsabbagh/review-sensei/blob/70305207647c22595660e5ebb9e4a33991489ffe/src/review_sensei/hosting/github/publication.py#L1065
[publication]: https://github.com/malsabbagh/review-sensei/blob/70305207647c22595660e5ebb9e4a33991489ffe/src/review_sensei/hosting/github/publication.py
[ledger]: https://github.com/malsabbagh/review-sensei/blob/70305207647c22595660e5ebb9e4a33991489ffe/src/review_sensei/hosting/github/session_ledger.py
[http]: https://github.com/malsabbagh/review-sensei/blob/70305207647c22595660e5ebb9e4a33991489ffe/src/review_sensei/hosting/github/http.py#L171
[workflow]: https://github.com/malsabbagh/review-sensei/blob/70305207647c22595660e5ebb9e4a33991489ffe/.github/workflows/review-sensei-run.yml
[transaction-tests]: https://github.com/malsabbagh/review-sensei/blob/70305207647c22595660e5ebb9e4a33991489ffe/tests/test_review_transaction.py#L1047
[missing-check-test]: https://github.com/malsabbagh/review-sensei/blob/70305207647c22595660e5ebb9e4a33991489ffe/tests/test_gate_and_default_approval.py#L334
[distribution]: https://github.com/malsabbagh/review-sensei/blob/70305207647c22595660e5ebb9e4a33991489ffe/tests/fixtures/distribution-contract.json
[readers]: https://github.com/malsabbagh/review-sensei/blob/70305207647c22595660e5ebb9e4a33991489ffe/scripts/check_review_reader_compatibility.py
[a-ledger]: https://github.com/malsabbagh/review-sensei/blob/ba89418d21898d14e6d1e535a5409c47c7cff3b2/src/review_sensei/hosting/github/session_ledger.py#L813
[release-workflow]: https://github.com/malsabbagh/review-sensei/blob/e8fdcd18f8256b1ceaab323701a2570b208d4e75/.github/workflows/release.yml
[release-helper]: https://github.com/malsabbagh/review-sensei/blob/e8fdcd18f8256b1ceaab323701a2570b208d4e75/scripts/github_release.py
