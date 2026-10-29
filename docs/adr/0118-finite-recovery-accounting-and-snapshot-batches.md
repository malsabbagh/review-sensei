# ADR 0118: Preserve finite recovery accounting and preflight snapshot batches

- Status: Proposed; production paged writers and recovery activation remain gated
- Date: 2026-10-29
- Decision owner/reviewers: repository owner; independent composed review required
- Linked issue: user-directed recovery capacity correction
- Linked PR: separate capacity correction, pending creation
- Depends on: ADRs 0106, 0111, 0115, 0116 and 0117

## Context

The recovery path must retain original accounting, all historical obligations,
late-effect reserves and complete decisions. Repeating whole decision inventories
in immutable carriers consumes finite storage. Increasing terminal reservation
at request 513 also took work away from the previously supported 512 profile.
Neither discarding history nor refusing a valid complete lifecycle constitutes
capacity qualification.

## Decision

New caller accounting uses its versioned compact completed-request ID/deadline
pairs. Completed entries, pending requests, interrupted reservations and unknown
charges retain their original meanings. Existing v1 runs keep their original
format and allocation semantics; upgrades do not rewrite their history.

Each new-version owner funds added terminal liability from that owner's own
allocation growth above the 512-request baseline. With startup liability 130,
limits 512/513/514/548 produce first/successor terminal reservations 50/50,
51/50,51/51 and68/68. Both owners retain 141 ordinary requests until their
additional terminal liability is funded. Integer allocations conserve the frozen
limit and ordinary work is monotone. Frozen v1 runs retain terminal 50, including
an unclaimed successor at a larger frozen limit. Existing blocks are immutable.

A v2 disposition root selects a finite index of closed eight-decision pages and
a tail of zero to seven decisions. Complete reads authenticate every carrier,
result, producer, repository/PR, generation, page position and global uniqueness.
Reuse requires reading the complete previous snapshot. Changed pages produce new
immutable carriers; no old page, root, receipt or unknown effect is removed.
Readers continue to accept v1 snapshots. An unqualified writer refuses to
downgrade existing v2 authority.

Prepare the entire batch before its first immutable write. The local adapter can
derive exact content-addressed IDs for every page and the index, perform one
retention census, and preflight aggregate bytes/counts/readback plus future
baseline, disposition, queue and history reads. Its complete readback precedes
return. Activation plans enumerate baseline parts, disposition index/pages, then
queue/history parts in the reader's actual order. A bounded cache stores only
root-specific authenticated page IDs; activation still validates the selected
carriers under the adapter's existing freshness contract.

GitHub assigns numeric IDs after POST. Its candidate batch measures maximum raw
index size using nineteen-character unknown IDs, then uses the conservative
normal-wrapper zlib bound `L + ceil(L/8) + ceil(L/64) + 11`. It derives all part,
comment, request-wire and final-manifest sizes from the actual framing. It never
estimates compression by compressing repeated placeholder IDs. Sizing tokens
never become stored references, observations or accounting receipts. This bound
is independent of the default compiled memory level; the tighter default-only
bound is insufficient for other supported compressor settings. See the
[zlib general bound](https://github.com/madler/zlib/blob/v1.2.12/deflate.c#L627-L697).

The hosted writer reuses the existing complete authenticated census and reserves
all existing carriers and late-growth liability. It matches proposed evidence
sizes against a copy of already admitted evidence promises. It cannot borrow the
mandatory command-receipt reservation or add new promises. Assigned IDs and
actual page/index sizes are revalidated before index creation and activation;
complete readback authenticates every resulting carrier. A lost create response
retains the possible effect and its charge and stops the original attempt.

The plain adaptive hosted mutation lacks a sealed future scan allocation, so it
cannot use the batch writer. The ApiRun companion enters its existing owned
staging scope and requires the issued exact-checkpoint snapshot for this root.
Its aggregate admission retains two request slots per possible new part and the
existing five-request root confirmation tail. It cannot increase its enclosing
operation's allocation. The existing sealed-tail path also retains complete
future reads and bounded scans.

## Current gates and concrete remaining obligation

Production adapters still select the v1 writer. The local and hosted v2 writers
are explicit qualification candidates. Their support is not a release, registry,
recovery or approval authorization.

The current command allocation admits an archive carrier, one result/evidence
carrier and a separate receipt. After the startup archive, only one evidence
creation remains; the receipt is not spare storage. A fresh 33-decision migration
needs four closed pages, and 64 needs eight, plus an index carrier when the actual
root allocation requires it. At a 2048-byte manifest allocation, the tested batch
requires five and nine carriers respectively. The current command profile must
refuse that aggregate before any snapshot POST. Its remaining capacity decision
must jointly fund the complete lifecycle; safe refusal alone is not acceptance.

The qualification fixture may supply explicitly sufficient existing bounded tail
admission to test assigned-ID 33/64 batches. That fixture is not a production
checkpoint receipt. A real public-command probe exposed the receipt borrowing
bug at the first 20-decision migration; the corrected batch refuses before its
snapshot writes instead of consuming the receipt promise. All old obligations
and accounting remain retained.

No storage or census ceiling changes here: retained parts 256, retained bytes 8 MiB,
part bytes 32768, per-document parts 32, snapshot root 8192 and session record 32768.
The existing 200-comment recovery census, ten-request native walk, 512 KiB response
and 8192-byte query limits remain unchanged. The proposed larger retained-count
and paginated-census changes remain separately blocked pending an exact decision.

## Validation and rollout

Verify source and rebuilt installed artifacts, complete physical public-entry
journeys, caller ownership, Windows behavior and the paired Worker. Exercise
manifest/count/byte/request refusal before writes, assigned ID widths, escaping,
compression boundaries, old-page reuse, corruption, missing/foreign carriers,
unknown writes and receipt reservation. Include both v1 and v2 dispositions in
complete baseline/queue activation tails. The current stable reader-first
snapshot passed 204 focused source tests and rebuilt-installed snapshot/tail
qualification; later hosted-batch changes require their own recorded artifact
and installed verification.

Enabling a producer requires complete supported-profile lifecycle proof and
independent review of the exact artifact. Rollback keeps any v2 roots, original
receipts, unknown charges and obligations readable and preserved. It cannot
reinterpret an unknown POST as permission for another create.
