# Capacity-safe recorded-decision reconciliation

## Failure and selected repair

On PR #326 at `c8c809ee12c8331584ca1223fa8c58af8a52859f`, all 21 explicitly
authorized decisions persisted in the session ledger. The original legacy review
was 64,971 UTF-8 bytes. Its resolved eligibility needed 66,847 bytes even without
new command provenance, exceeding the unchanged 65,536-byte review bound. Gate
reconciliation failed after recording; the decisions were not lost.

Reuse the existing compact generated references rather than introduce another
root, storage protocol, eligibility version or authority source. For an existing
oversized legacy root, the command finalizer may compact only a complete canonical
generated Required-decisions section, reconstructed from the original human
inventory and complete index. It independently verifies every original human
explanation at its body or same-review owned inline placement. Ambiguous framing,
missing or changed prose, ownership conflicts and insufficient capacity hold.
Every byte outside the verified reference rows, every full index identity,
historical marker and coverage fact remains intact. Quoted references in provider
narrative are not generated rows and must remain unchanged.

For new publication, both legacy and complete human-inventory callers preflight
the whole future all-resolved frame and reserve one additional maximum ten-ID command marker.
If needed, the ordinary structured renderer selects compact references before
the first inventory/check/review write. Reserving room must not cause a human
inventory to omit optional explanations that previously fit. Complete publication
continues to preserve every explanation. No review, item, inventory, read or
deadline bound is raised.

An unbounded history of additional provenance markers is not promised to fit.
Existing history is retained. An exhausted frame reports
`command_body_capacity_exceeded`; an unverifiable compaction reports
`command_body_capacity_unverifiable`. Both preserve decisions and withhold gate
updates. The command result directs the operator to retain the original review
and ledger for capacity/evidence repair instead of suggesting an identical retry.

## Compatibility, migration and authority

There is no schema or storage migration. The same owned review and same eligibility
schema remain authoritative. Existing readers already consume the compact
reference presentation. The repair requires installing a compatible command
runtime; publishing a source PR does not repair the deployed runtime.

Use existing independent ledger, numeric App owner, repository/PR/base/head,
latest-root, complete index, original prose, finite-budget and final readback
validation. A reference or a copied decision never authorizes a changed head.
Lost-write acknowledgement can reconcile only exact owned readback. A changed
root, head, base or original explanation invalidates the attempt.

The live PR326 decisions stay bound to c8. This implementation uses a separate
branch and does not replay them onto its new head. Compatible release/deployment
and a subsequent exact-c8 verification need their normal authority. No new full
analysis follows from the 21-decision approval. Hosted coverage remains separately
partial at 24/51, and no successful reconciliation may turn that into approval.

This is a bounded presentation repair within the proposed ADR 0093/0094 contracts,
not a new persistence or approval decision. Those ADRs remain Proposed. Writer
activation, release, merge and deployment are separate operations.

## Acceptance matrix

| Condition | Public seam and executable evidence | Required outcome |
| --- | --- | --- |
| Whole future frame, legacy and complete | `test_publication_future_capacity.FutureDispositionFrameTests` | Before: published body grows to 66,031 bytes. After: all decisions and maximum command marker fit, with all original prose/IDs retained. |
| Existing recorded 10+10+1 union | `test_command_capacity_recovery` | Serialized ledger preserves 21 decisions; finalizer updates the original owned review within the existing cap, with no inference. |
| Partial and eligible controls | Same public recovery suite | Partial coverage keeps a non-passing check and cannot approve; an eligible control obeys all ordinary gates. |
| Replay and ambiguous PUT | Same public recovery suite | Exact owned readback is idempotent; uncertain differing bytes withhold authority. |
| Source, snapshot and ownership changes | Same public recovery suite | Changed head/base/newest root/prose and foreign records refuse without stale writes. |
| Ambiguous or indivisible body | Same public recovery suite | Preserve original data and durable decisions, return actionable capacity hold. |
| Persistent failure reporting | `test_github_application.GitHubApplicationTests.test_persistent_command_capacity_failure_does_not_request_identical_retry` | Failure remains visible and does not request an identical verification retry. |
| Installed distributions | `dist_safe/test_command_capacity_contract.py` | Run public recovery and future-frame cases against installed engine/adapter from extracted sdist, without checkout imports. |

Synthetic external HTTP is used with real codecs, publishers, command handling,
durable local ledger and finite budgets. This qualifies the deterministic supported
paths; it does not qualify live rollout, all concurrent interleavings, arbitrary
history capacity or missing hosted analysis. Relevant tracking: #238, #241, #246,
#267 and #269. No broad issue is closed solely by these tests.
