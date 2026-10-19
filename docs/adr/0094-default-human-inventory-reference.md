# ADR 0094: Default publication of complete admitted human inventories

- Status: Proposed
- Date: 2026-10-19
- Decision owner / reviewers: ReviewSensei maintainers
- Linked GitHub Issue: [#238](https://github.com/malsabbagh/review-sensei/issues/238), [#246](https://github.com/malsabbagh/review-sensei/issues/246), [#267](https://github.com/malsabbagh/review-sensei/issues/267)
- Linked PR: Follow-up to #325; observed default publication failure on #316

## Context

PR #325 makes the complete default index bounded and retains canonical result
bytes. A realistic human inventory can still exceed the legacy 24 KiB future
assessment document, even when its individual inline explanations and review
body fit. Retained ReviewResult bytes intentionally omit runtime human/blocking
admission flags and cannot independently restore these obligations. Activating
the separate v4 host would also impose incompatible qualification and terminal
resolution behavior on ordinary callers.

## Decision

Add a distinct closed eligibility schema 5 containing ordinary approval facts, a
purpose-bound human inventory reference, and the compact full-ID resolution
receipt. Preserve the existing standard approval algorithm, including canonical
provider qualification and legitimately `not-required` providers. A transaction
bound publication retains the complete admitted inventory and exact finding index
in existing owned PR result parts, even when the legacy root
subsequently refuses its capacity. The complete profile may use schema 5 when
its human inventory exceeds legacy capacity. Legacy publication remains unchanged
for inventories within its capacity.

References confer no authority. Each operation independently selects its durable
transaction, repository/PR/base/head, configured numeric Bot, publication identity,
policy/configuration/evidence digests and original finite budget. A fresh reader
reconstructs immutable evidence, then revalidates current authority, the latest
owned root, its original exact index and visible human explanations. Compact
assessment receipts cannot replace original obligations or explanations. Missing,
conflicting, foreign, stale or unreadable evidence withholds decisions.

Only the original independently admitted publication may start storage. A closed
purpose-bound intent precedes the first data POST. Unknown/lost responses and
crashes permit reconciliation of a complete owned set; absent or incomplete
uncertain writes remain held. A reference does not authorize recreating missing
parts. GitHub comments still do not provide distributed compare-and-swap.

Preflight the initial framed root and the largest all-resolved authority receipt
before inventory/check/root writes. Preserve full IDs, complete individual prose,
existing item/review/decoded limits and exact index placement. Do not increase the
legacy human cap or truncate findings. The new immutable inventory is separate
from raw-result retention and diagnostics. One original evidence allowance covers
publication or feedback transport, broker dispatches, storage, readers and fences.

For schema 5, initial inference, decision persistence and final approval require
fresh complete visible-prose validation. Nonauthoritative pending-receipt pages
may use fresh transaction/head/owned-root/index metadata fences between those
full proofs. A metadata fence never permits a decision or approval. Pending
reports retain every full ID and path; repeated common guidance is stated once.
The original explanations remain in their verified body/inline placements.

## Scope and limits

Wire the ordinary application/publisher, fresh readers, conversation assessment,
maintainer decisions, explicit rescan and finalization. The packaged eligibility-v5
structural schema complements, but cannot replace, independently authenticated
reference, resolution, root and visible-prose checks. Explicit rescan shares the
caller allowance when supplied; otherwise one command-local allowance covers all
its reads without attaching a new deadline to later inference/checkpoint work. Keep the separately proposed partitioned
hosts opt-in. POSIX local ledgers may reuse bounded immutable part bytes after
fresh containment and inode/size/mtime/ctime checks; mutable roots remain fresh.
Windows retains full rehashing and may hold this richer repeated-read profile
within its existing budget. Native Windows capacity is not positively qualified.
Reader-first rollout and configured complete emission remain
required. Full prose must fit its existing inline/body placement bounds; this
change does not make arbitrary body-only 250-finding reviews publishable.
Long history or unavailable storage can exhaust the finite allowance and hold.
Missing original incident payloads cannot be reconstructed from baselines.

## Validation

Acceptance tests use actual application/publisher/reader boundaries with a
serialized ledger and synthetic external transport. They must cover rich and
poorly compressible inventories beyond 24 KiB, exact full metadata, authorized
partial and final resolution, current-head approval, preservation of other gates,
mutable prose/identity corruption, unknown writes, interruption and finite budget
exhaustion. Installed wheels and extracted sdist must execute the same supported
journey without checkout fallback. Deterministic evidence does not qualify live
providers, hosted rollout or every concurrent interleaving.

## Rollout and rollback

Ship compatible readers to all supported consumers before enabling emission.
Retain those readers for previously published schema 5 roots if emission is
rolled back to `legacy`. No release, v5 tag movement, deployment, permission or
secret change is part of implementation. ADR remains Proposed pending review.
