# ADR 0098: Same-operation admitted inventory handoff

- Status: Proposed
- Date: 2026-10-20
- Decision owner / reviewers: ReviewSensei maintainers
- Linked GitHub Issues: #238, #246, #267
- Trigger: PR #316 publication exhausts the original shared request allowance while rebuilding a context already authenticated by its owning store

## Context

The application independently authenticates the admitted human inventory before
publication. A later lazy default reader reconstructs that same context through
additional PR, Bot and ledger reads. Those duplicate construction reads can
exhaust the original 60 ordinary dispatches before mandatory completion. The
canonical admitted payload and original staging intent are evidence; current
comment bodies, ledger state, current PR and visible prose remain mutable.

## Decision

An original `HumanInventoryStore` may retain one bounded canonical admitted
payload, its exact reference and canonical original intent after a complete
successful read or stage. The publisher may explicitly attach that exact store
to its default reader. Attachment requires the identical original budget object,
underlying audited transport and credential, plus matching repository, PR, head
and configured App. No reference supplies any expected authority or scope.

The reader avoids reconstructing the already independently selected context.
Before adopting the payload it still performs ordinary fresh store authority
checks and a complete intent/parts enumeration, including duplicate, provenance,
canonical framing and payload checks. The original intent and payload must match
exactly. A failed handoff cannot replace the original snapshot with changed live
bytes. Each root validation still freshly verifies mutable checkpoint/head,
latest owned root, exact original index and every full visible explanation.
Ordinary read/read_available retain their existing corruption detection.

Snapshots belong only to the owning store and original finite operation. They
are not global HTTP response caches, persisted authority decisions, credentials,
or a new budget. Independently selected readers and mismatching transports,
credentials, scopes and budgets cannot adopt them. Existing read fallback remains
available when no authenticated store exists. No limit or deadline increases.

Pagination observations in this operation retain only oversized-request upper
bounds at the exact transport/API/query/item offset. A successful small page does
not prove larger sizes failed and cannot constrain another reader's page ladder.
Metadata expires with the original budget and transport; each scan still reads
its own complete final page.

## Validation

A public publisher/default-reader test leaves six ordinary requests and proves
all 77 admitted findings survive strict eligibility loading with full fresh
storage/root/prose fences. It is red without the explicit handoff. Negative
controls cover changed original intent, parts, duplicate intent, transaction,
current head, root ownership and prose, plus transport/budget/token mismatch.
Dense 100-item 16/20 KiB histories, 250 items, individually oversized items and
cross-ladder metadata poisoning remain in the installed-wheel contract.

The sdist packages those same tests. The existing default-human-inventory
wrapper and post-package lifecycle tier discover the actual test classes;
load errors or skipped tests are not positive qualification. Synthetic public
acceptance does not establish complete actual-history publication or live effects.

## Consequences and rollback

Memory holds at most one admitted payload within the existing 1 MiB part cap per
owning store. Full storage and mutable authority/prose reads remain necessary,
so sufficiently rich history can still exhaust the allowance. Disable attachment
to restore independent reader construction; existing stored formats and readers
remain compatible. This decision does not authorize cleanup ordering, releases,
deployment, permissions or inference retries.
