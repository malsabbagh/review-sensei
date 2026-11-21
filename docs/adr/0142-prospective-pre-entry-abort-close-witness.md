# ADR 0142: Prospective pre-entry abort close witness

- Status: Accepted (direct pre-entry abort writer and admission wired; charged-failure tail remains out of scope)
- Date: 2026-11-18
- Decision owner and approver: repository maintainer
- Related work: G29, [PR #468](https://github.com/malsabbagh/review-sensei/pull/468), and [PR #469](https://github.com/malsabbagh/review-sensei/pull/469)
- Implementation: live direct-abort writer & admission wiring in `session_ledger.py`, `api_run_requests.py`, `pre_entry_abort_admission.py`, and `cli.py` (`charge_failed_attempt=False` only)

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

Direct pre-entry abort finality wiring (`charge_failed_attempt=False` on a plain
hosted first-attempt cleanup root with zero prior uncertainty) prepares and
dispatches the single canonical `abort-close-v1` PATCH in
`ApiRunRequestTransport.close_analysis_abort` and enforces a sticky
post-abort finality lock so no subsequent mutation or `reconcile_checkpoint`
can follow on that caller. Ordinary pre-entry abort admission
(`pre_entry_abort_admission.py`) accepts `old.uncertain == 1` only via
`observe_exact_abort_close` on the freshly fetched exact native comment row
inside the active native CAS mutation, followed by all existing abort-receipt,
negative native reviews/comments census, Actions cessation, and archive-capacity
checks, archiving the predecessor root unchanged (retaining `uncertain == 1`
and `abort-close-v1` without refunding or converting it to completed).

It cannot mint a writer identity, refund the original charge, clear any other
uncertainty, settle cleanup, or authorize a successor on its own. Legacy
opaque `c-0-...` roots, `uncertain > 1`, non-plain roots, and the
charged-failure tail (`record_session_failed_attempt` /
`charge_failed_attempt=True`) remain unchanged and explicitly out of scope for
prospective witness emission and `uncertain > 0` admission. No new capacity,
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

1. Source-qualified candidate (`PR #469`) established canonical encoding and
   observer checks without changing readers or writers.
2. Direct-abort writer qualification (wired): qualify only a source-owned final
   direct abort (`charge_failed_attempt=False` on a plain hosted first-attempt
   root with zero prior uncertainty), binding one exact `abort-close-v1`
   request and deadline at the actual native PATCH boundary, enforcing sticky
   post-abort finality on the caller, and never replaying or reconciling a lost
   close response.
3. Direct-abort admission qualification (wired): require `observe_exact_abort_close`
   on the freshly fetched native row inside active CAS alongside finality,
   original authority, Actions cessation, complete negative late-effect
   inventory, archive/write capacity, and unchanged retention of the `uncertain == 1`
   pending receipt (`caller-run-root-v1`). Failed or unaffordable proofs hold
   before archive, provider execution, or native writes; unknown responses are
   never converted to completed.
4. Out of scope (future work): for the charged-failure path
   (`record_session_failed_attempt`), first design and test custody of the
   later reservation/commit, or a qualified equivalent that ends at the proven
   closure and keeps failed-attempt charges. Cleanup needs its own prospective
   proof. Neither step retrofits evidence for historical runs.
