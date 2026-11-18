# ADR 0142: Prospective pre-entry abort close witness

- Status: Proposed
- Date: 2026-11-18
- Decision owner and approver: repository maintainer; not yet approved
- Related work: G29 and [PR #468](https://github.com/malsabbagh/review-sensei/pull/468)
- Implementation: unconnected source candidate; no rollout or writer qualification

## Context

An abort closure can be stored on the App's canonical session comment even
though the caller never receives the PATCH response. The existing writer
serializes the comment before allocating the PATCH's own request identity.
A later checkpoint can then durably contain the unknown dispatch, but the
stored request ID and deadline do not identify its effect. Matching that ID by
order, count, timestamps or the existence of an abort receipt would be
guesswork. Do not use this proposal to reinterpret older roots.

A normal analysis failure does not stop at the abort closure. The failed-attempt
reservation and commit can each issue another PATCH. A visible close does not
prove those later writes were unstarted, drained or no-effect. GitHub comment
PATCH is not a conditional compare-and-swap.

## Decision

Qualify the representation of **one final close PATCH after an independently
confirmed cleanup**, without connecting it to a producer or admission. Use the
existing caller-requests-v2 pending row and one slot from the original hosted
first-attempt terminal block. The projected debit moves one reserved slot to
pending without changing confirmed or remaining capacity. Preserve the
original deadline and do not replenish the terminal block. A native echo is
not the original HTTP response.

Use an `abort-close-v1` request ID with the physical sequence and a SHA-256
commitment. Bind the configured HTTPS API destination, PATCH comment ID and
configured numeric Bot/slug along with the complete canonical root, including
the run, owner, policy, original analysis transaction, source and cleanup
digests, deadline and accounting. Normalize only the root's recursive digest
and that exact request ID's recursive suffix. The normal renderer and parser
still apply. Accept a match only on that exact generation and canonical body
from the selected PR's exact Bot-owned native comment; a later root carrying
the same pending ID is not a close echo. The pure observer cannot establish
freshness, transport issuance, or writer cessation on its own. Only a plain
unadorned cleanup root is in scope; for example, a consumed grant in the
native comment cannot be dropped during rendering.

Keep the 32 KiB session record, 64 KiB rendered comment, one-part predecessor
archive, 8 MiB retained inventory, 512 KiB responses, 8 KiB queries and existing
request/confirmation limits independent and unchanged. Preflight the exact
body and one-part archive representation. That does not prove the live
inventory has room to archive it or that its full scan/readback fits.

## Scope and consequences

The prototype prepares detached bytes and describes one observed effect. It has
no HTTP client, caller registration, CLI switch or ordinary rollover consumer.
It cannot mint a writer identity, refund the original charge, clear any other
uncertainty, settle cleanup, transfer obligations, or authorize a successor.
Default writers and old schema readers are unchanged. No new capacity,
deployment, version/channel or permissions are proposed.

The candidate binding is prepared **before** dispatch; it becomes durable
only if the same PATCH stores it. A death before storage does not create
evidence that the dispatch did not start. If a later proof is added, the
metered writer must consume the exact prepared slot once, after the original
root and target fences. It must not alter the request after canonical
rendering, spend a reserved confirmation slot on discovery, or replay when
the known storage echo is unavailable.

## Alternatives considered

- A prose or HTML marker is not bound by the session root and is dropped by
  its retained archive. An opaque past ID cannot be associated retrospectively.
- Adding a separate request allotment, replaying a PATCH or moving a pending
  item to completed after readback would misstate or enlarge the original
  accounting. Do not do that.
- Changing the public accounting schema or adding a dedicated durable intent
  write is unnecessary for this representation. The self-carried value does
  not prove no dispatch where the comment was never stored.
- Proving a generic later generation from a matching ID would miss the
  subsequent failed-attempt reservations. Begin with the direct abort path,
  where finality can be proven separately, rather than generalizing this
  candidate across the whole error tail.

## Validation

Offline tests use the real bounded GitHub HTTP and session codecs with a
synthetic store. They exercise lost-response storage, absent storage, exact
root and Bot identity, altered or noncanonical bodies, legacy opaque rows,
additional uncertainty, later generations and the existing confirmation and
one-part carrier limits. Native provenance, interprocess claim/dispatch
custody and cold admission are explicitly not established by these tests.

## Rollout, rollback and follow-up

1. Review this no-authority candidate and keep readers/writers unchanged. It
   can be removed without migration; no production code emits its marker.
2. Qualify only a source-owned final direct abort. Bind one exact request and
   its unchanged deadline at the actual native PATCH boundary. In cold
   subprocess fixtures, kill immediately before and after storage and exercise
   an exact, fresh, already-funded readback with every existing fence.
3. Before any ordinary admission consumer, prove finality, original authority,
   cessation, complete negative late-effect inventory, full archive/write
   capacity and retention of the pending receipt. A failed or unaffordable
   proof must stop before archive. Do not convert unknown response to completed.
4. For the charged-failure path, first design and test custody of the later
   reservation/commit, or a qualified equivalent that ends at the proven
   closure and keeps failed-attempt charges. Cleanup needs its own prospective
   proof. Neither step retrofits evidence for historical runs.
