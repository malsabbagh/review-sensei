# ADR 0085 - Bounded complete selected feedback reports

Status: Proposed
Date: 2026-10-18
GitHub Issues: #240, #242, #243, #244, #269
Pull Request: pending
Owners/Reviewers: Maintainers
Approved by: not applicable

## Problem and executable oracle

The public trusted-library 100-finding fixture retains independently varied
SHA-256 identities and original Unicode concern prose. The original receipt
carrier exceeds its 7,168-byte future-growth allocation and uses immutable
parts. Even an exact-inventory-reference carrier that fits inline consumes
32 original reads before publication. The pending report repeats the same
generic explanation and next-step guidance for every unresolved ID. It needs
multiple pages and exhausts the unchanged original 60 ordinary /64 total calls
before complete delivery or authority publication. No approval is issued.

`PublicFeedbackContinuationTests.test_varied_100_falls_back_to_parts_and_does_not_claim_publication_capacity`
retains the original failure oracle.
`test_referenced_varied_100_complete_publication_inside_original_budget`
exercises the complete public preparation, actual local ledger, assessment and
publication boundaries. It uses the unchanged independently varied fixture,
resolves the exact selected target, and retains 99 pending obligations at 57
charged original reads. Queue reconstruction never supplies an attempt witness.

## Proposed bounded interfaces

Use an explicit `shared-guidance-v1` pending-report layout, retaining the
existing byte-identical legacy layout as the default. The shared layout moves
only repeated generic explanation/next-step text to a common heading. Every
exact identity, display identity, path and per-finding validated reason remains
present under the existing display bounds. All page byte/count limits, source/
PR/base/head/latest-authority fences, per-page ownership/body readback remain unchanged. The opt-in layout additionally
reads back the complete report before the final live fence: two red regressions
showed that an earlier page edited after a later POST, or a failed final whole
readback, could otherwise return a successful receipt. Exact bodies, hashes,
page counts and ownership must all match before the report is complete. Rendering does not grant authority.

Provide the opt-in
`assessment-queue-state-packed-v2` carrier. It uses the already required exact
authenticated complete inventory as its reference table. It binds snapshot,
immutable inventory digest and ordered identity digest, plus the original full
envelope SHA-256 and decoded size. Decoding requires that exact original queue;
missing, changed or unretained original authority refuses. Independent encoded,
projected and reconstructed bounds and closed shapes apply. Older readers
refuse the new carrier; v1 and uncompressed readers retain existing behavior.

The fresh reader must reconstruct its reference queue from an actual retained
Bot eligibility read inside the existing charged authority fence. A queue is
neither an original-attempt witness nor a source grant. Every replay persists
counter/time high-water and needs the existing live owner or genuine original
attempt witness. No deployment, CLI/Worker activation, cap increase or new
allowance is enabled.

## Alternatives and tests

Reusing negative comment lookups could duplicate concurrent deliveries and is
rejected. A new source/PR observation-sharing seam or deferred page readbacks
would expand authority/delivery contracts unnecessarily if presentation reuse
solves the bounded case. Keep their current behavior instead.

The green oracle retains 99 pending findings and withholds approval after
one exact target is assessed, with original provider/read budgets and all
original concern data intact. Test missing/changed retained authority, unknown
carrier/layout fields, malformed indices, decoded bounds, original full hash,
oversized individual items, source/head/latest-authority races, ambiguous/
concurrent delivery and incomplete owned readback. Replay budget and witness
refusals remain explicit limitations. Rich 250 eligibility activation and joint
long-history capacity remain separate work.

## Executable evidence and remaining bounds

`QueueStateCarrierTests` covers v1 compatibility and explicit v2 round trips for
100/250 queues, exact snapshot/inventory/order requirements, malformed indices,
unknown fields and versions, independent decoded/reconstructed bounds, changed
whole-envelope hash and adversarial recomputed frame hashes with a different
inner inventory. Default readers without the exact reference refuse v2.

`PendingPresentationTests` proves the legacy default has identical pages and
report digest to explicit legacy; shared reports retain every full/display ID,
path and validated per-finding reason under existing byte bounds. Unknown layouts
and oversized individual discussion bodies refuse.

`PublicFeedbackContinuationTests` checks source/head/base/latest-authority races
between two naturally sized pages of a 104-finding stress fixture (one valid
resolution and three unresolved decisions) and concurrent, changed-body, lost/partial POST,
individual and whole readback faults. Every failed delivery leaves authority
unadvanced and approval withheld. Original-owner replay persists at 44 reads with
zero inference; its later publication still exhausts the original allowance.
A fresh owner obtains the actual retained inventory but refuses mutation without
the original attempt witness. Missing retained authority refuses before inference.

Neither a compact presentation nor a queue reference activates rich 250
eligibility, a consuming broker grant, CLI/Worker selection/queue scheduling,
unknown-provider reconciliation or a fresh process's original-attempt witness.
The 100 profile has no historical discussion; joint long-history capacity remains
unqualified. These are implementation and qualification limits, not a permission
request or a claim that the remaining work is impossible.
