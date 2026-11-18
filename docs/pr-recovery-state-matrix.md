# Pull request recovery: prove the outcome before choosing the next action

There is no universal PR recovery operation. The original attempt may be safe
to resume, held until specific evidence becomes available, or unable to be
settled at all. A separately authorized successor can produce a new result; it
cannot change what is known about the predecessor. Source tests, required CI,
installed applicability, and historical settlement are separate claims.

Start with the [approval recovery procedure](approval-recovery.md), not a new
run. A live `verify` can write a check, resolve a thread, or submit an approval.
It is not a read-only probe just because it uses no inference. Offline evidence
packets, command exit status, and review status text do not grant authority.

## Record the smallest useful evidence packet

Use authenticated retained evidence and read-only observations for the original
attempt. Keep unavailable facts explicitly unknown. Preserve the distinction
between a recorded intention, an attempted request, its observed effect, and
the durable close of that operation.

1. **Scope:** repository numeric ID, PR number, original reviewed base/head and
   frozen policy, exact source event or command revision, installation and
   attested writer/job identity. Record the current PR state separately.
2. **Runtime:** Actions run, attempt, job, caller workflow, reusable-workflow tag
   object and peeled commit, installed core and hosted package identities, and
   broker route/revision if independently observed. See [runtime
   provenance](runtime-provenance.md). Source branch, wheel version label, and
   execution evidence are not substitutes for each other.
3. **Durable work:** ledger generation and digest, transaction versus command
   action, original result and producer, current and retained finding inventory,
   accepted decisions, any pending native effect, and the phase that last
   completed. Record original allowance, consumed and uncertain requests,
   reservations and protected finish work separately.
4. **External effects:** authenticate the selected original review/check/thread
   intent and exact native identity/readback, or record precisely which
   observation is missing or ambiguous. Check owner cessation and whether an
   earlier dispatch can still land. Preserve the original observed charge.
5. **Completion:** verify the matching terminal transaction or authenticated
   terminal action result, accepted work, native approval when applicable, and
   exact-head check independently. A terminal action can be a refusal with zero
   decisions. A missing review terminal marker alone does not disprove an
   independently authenticated terminal command action.

Do not publish raw responses, credentials, private recovery files, or unrelated
source as evidence. [Lifecycle diagnostics](review-lifecycle-diagnostics.md)
can identify a stage to investigate. The absence of a log line, artifact, or
transaction is not evidence that an operation never started or never wrote.

## Decision matrix

| Original evidence and current state | Classification | Safe next step and required proof |
| --- | --- | --- |
| Matching authenticated terminal review transaction, or matching authenticated terminal command result | **Already terminal** | Observe what the result actually says. If it withheld approval or made no decisions, do not describe it as completion of the user's goal. Evaluate further work separately on the current target. |
| Retained accepted result/intent, supported original transport and later attempt, unchanged required context, complete exact native readback, and executable funded tail | **In-place recovery candidate** | Use only the supported original route. Require a verified terminal receipt, zero repeated inference, no duplicate native business write, retained uncertain physical charges, and unchanged obligations. A route that does not establish these witnesses is not qualified. |
| Authenticated no-transaction pre-entry abort, released reservation, zero provider entry, no uncertain caller dispatch, ceased owner, and complete negative late-effect evidence | **Known pre-entry abort** | An ordinary successor may be eligible after its own admission checks. Unavailable, malformed, truncated or ambiguous evidence cannot prove absence. A positive late effect also blocks this route. This is not replay of the predecessor. |
| Unknown native dispatch, missing/malformed readback, conflicting identity, partial inventory, or no proof the old writer has ceased | **Evidence hold** | Preserve the intent, earlier effects, liabilities, and findings. Reconcile against owned exact readback and original evidence if available. No second write or refund simply because no object is visible yet. Readback that proves only one effect cannot settle another. |
| Missing original coverage, complete finding inventory, retained result or POSIX private context | **Evidence hold** | Restore the original authenticated evidence only through the appropriate route. A reduced baseline, current policy, rewritten prose, or a manually resolved thread cannot reconstruct it. Standalone `publish` cannot take ownership of the metered hosted caller. |
| Same original work cannot be priced or represented within the combined policy, storage and physical request limits; zero progress or capacity refusal | **Bounded stop** | Check the actual no-effect/terminal proof. Use smaller work only if already supported and legitimately authorized, or seek human assessment. Do not split an indivisible operation, extend budget, refill uncertain usage, or loop unchanged requests to simulate success. |
| Fresh head/base, changed source, changed policy or trust, missing applicable installation capability, or an unresolved human gate | **Current authority or qualification required** | Re-establish current identity, frozen context, access, provenance, and the required human decision. Old approval/decisions do not apply automatically. Do not retry an unchanged denial or transfer an original hold. |
| Original completion unavailable but an explicitly qualified restart route with safe isolation and separate authorization is available | **Successor only** | Preserve the archived predecessor and all holds. Check the [controlled restart gates](controlled-restart-launch.md); a registry, proposal, or fixture is not authorization. The new result certifies only the supported successor scope. |
| Original authentic evidence is permanently lost or an old effect can never be attributed or ruled out | **Historically unprovable** | Keep the old attempt as unknown, without a cause or settlement claim. Do not rerun a merged PR, reset accounting, or reconstruct original evidence with inference. A legitimately fresh review elsewhere does not close the historical record. |

Account for the [current finite envelope](finding-and-artifact-lifecycle.md#capacity-and-privacy):
256 evidence parts, 200 comment allocations, 8 MiB retained evidence, 512 KiB
responses, 8 KiB queries, and the existing operation-specific limits and frozen
caller funding. A specialized, source-qualified read tranche is not extra
funding. A protected terminal tail is not available for discovery. A successful
formatter, a check with no new findings, a merged PR, and a native approval
each say something different from terminal accounting.

## Engineering and qualification order

| Order | Work and dependency | Acceptance and stop rule |
| --- | --- | --- |
| 1. Establish the baseline | Verify current source, owner, open PRs and exact runtime/CI separately. Reconcile prior fixes before starting another writer. | Preserve the original operation and point to authenticated inputs. Do not reopen historical work or duplicate an in-flight review/release. |
| 2. Qualify pre-entry abort (G29) | Exercise the read-only inspector and the actual ordinary successor entry. The advisory result must not authorize the runtime path. | Complete empty or demonstrably unrelated evidence can pass only with every other predicate satisfied. Attributable effects, unreadable or partial pages, repeated pagination, unknown dispatch, active owner, and one-short funding must hold before archive, provider or native business writes. |
| 3. Qualify exact native settlement (G03/G31) | Use the existing ordinary caller and fresh-process fixtures to prove selected native effects and lost-response outcomes with original funding. | A single exact readback can settle its own effect without refunding an uncertain dispatch. Missing, altered, conflicting or multiply attributable results remain held; no additional native write or inference. Test create/update, lost response, crash, cold resume and terminal close separately. |
| 4. Qualify bounded progress and context (G12/G17/G30/G34) | Reuse the hosted original-context and command-prefix entry points. Exercise actual retained before/after states and original capacities, not just their formatters. | A supported window must strictly advance the same authenticated work, or return a typed hold/refusal without clearing obligations. Include small funded positives, one-short/oversize and zero-decision controls, corrupt/expired/wrong-scope inputs, and matching final accounting. An authentic unknown remains unknown. |
| 5. Qualify identity and convergence (G02/G08/G10/G13/G14/G15/G16/G26/G27/G32) | Keep the existing cold lifecycle, source mutation, drift, contention and actual caller tests. Add targeted scenarios only for missing ordinary evidence. | Losing writers preserve epochs and old charges. A delayed/duplicate event or restored text cannot impersonate the current source. An unstarted cancellation must be positively fenced; a started or uncertain effect remains held. No claim of all-writer containment, durable trust continuity, or live cutover from a source-only proof. |
| 6. Verify the exact code candidate (G11) | Run affected deterministic and ordinary-entry tests and the repository's relevant checks; publish the reviewed branch per repository procedure. | Separately record exact candidate CI, substantive review, installed runtime provenance, and live applicability. Do not rerun or relabel an old failure, or infer deployment from source or CI. |
| 7. Separate operational qualification | For targets with adequate evidence, identify the compatible installed caller and broker and the precise authority still needed. For historical targets, keep read-only evidence custody. | Deployment, changes to access, registration, live canaries, actions with approval effects, and restart are separate gated operations. No source change or green CI supplies those grants. |

Relevant source fixtures include `test_no_transaction_abort_g29`,
`test_review_run`, `test_ordinary_native_recovery_cold`,
`test_ordinary_native_recovery_crash`, `test_historical_recovery_matrix_g03`,
`test_pending_reassessment_g30`, `test_publication_overflow_recovery_g31`,
`test_hosted_disposition_snapshots`, `test_recovery_context_g17`,
`test_durable_finding_lifecycle_restart`, `test_source_lifecycle_process_matrix`,
`test_writer_epoch_contention`, and `test_invocation_provenance_g11`.
Their names indicate qualification scope, not evidence that any particular
candidate or installation has passed. To close a row, retain its exact source,
ordinary entry path, passed and held scenarios, original budget/effect trace,
CI/runtime status, and any still-missing historical witness.

For historical counterexamples, see [ADR 0133](adr/0133-historical-review-recovery-and-nonterminal-boundaries.md).
Its #443, SiteVault #142/#153, #408 and TaskAlong #41 examples are distinct.
Neither fixture success nor fixing a forward-looking guard establishes the
cause or outcome of any old attempt.
