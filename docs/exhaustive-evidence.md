# Exhaustive current evidence

Issue [244](https://github.com/malsabbagh/review-sensei/issues/244), lane E.
Interface direction: evidence-v1, reviewed for isolated implementation by the
Epic238 coordinator on 2026-10-10. Shared persistence and hosted activation remain
integration work. This is source behavior, not installed or deployed qualification.

`GitHubHttp.load_review_evidence` reads a complete changed-file inventory between
base/head and changed-file-count fences, including required-path-only requests.
It retries an oversized response at the same item offset with page sizes
100/50/25/5/1. Responses remain capped at 512 KiB. A known 1,000-file inventory
requires a bounded empty-page probe; a cap without that proof is incomplete.
An inventory above 1,000, an oversized single item, unavailable transport, count
conflict or snapshot change raises a sanitized failure and retains obligations.

Acquisition has at most 64 physical reads and 60 seconds, including both fences,
every retry and the empty-page probe. Integrated callers supply `before_read`,
which charges the original shared host allowance and returns its remaining
positive seconds, at most 60. The earliest deadline is retained. Feedback,
partition and evidence phases must share that guard; each phase cannot create
64 additional reads. The standalone defaults are ceilings, not a supported
whole-operation capacity claim.

`EvidenceGroup` retains exact whole patches, canonical counts, snapshot, required
paths, coverage and digests. Its canonical materialization document is at most
2 MiB and contains at most 64 paths across a group. This does not enlarge the
existing eight-path finding contract or the 8 MiB total diff-work envelope.
Restore verifies the closed complete document byte-for-byte, including framing
and UTF-8 escaping. Missing/invalid/binary patches and incomplete enumeration
have explicit pending reasons. Literal Git glob characters are preserved as
path data and compared by equality; no pattern evaluation occurs.

A materialized group is domain evidence, not an authenticated storage read or
provider receipt. The common partition reader must authenticate ownership and
the exact snapshot before restoration. No shared partition writer is activated
by this module. Provider admission remains atomic: complete evidence can still
be too large for a rendered request. Existing trusted provider qualification may
permit a larger finite single request; model text cannot qualify itself. No
isolated fragment decision resolves a group, and no multi-call aggregate decision
contract is delivered here. Unsupported groups remain pending.

Regression fixtures cover large Unicode and multi-hunk patches, giant atomic
refusal, missing records/count conflicts, 1/4/5/8-path finding packing,
deletions/zero-length adjacent insertions/renames, literal paths, 999/1000/1001
items, exact response-byte boundaries, every late-offset fallback, fences and
shared read/deadline exhaustion. Cross-cutting hosted wiring is supplied in
`handoff/issue-244-conversation.patch` for the coordinator; it is not applied to
the lane's checkout. The coordinator owns the shared ADR/index and activation
decision. Reverting these source changes writes or deletes no stored authority.
