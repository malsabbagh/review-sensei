# ADR 0107: Explicit remaining coverage windows

Status: Proposed. Unreleased; activation requires qualified package and workflow releases.

A published partial review could be rescanned from the start, but that did not
provide a public route to accumulate its remaining text coverage. Charging every
productive partial window as a failed attempt also imposed a lifetime progress
limit on a larger finite inventory.

A standalone `review remaining` event authorizes one bounded window against the
exact authenticated published source. It is distinct from fresh full rescan and
from elevated continuation after failed-attempt exhaustion. The caller routes
it as review. Hosted admission independently checks the original unedited human
event, current permission, PR identity, base/head and exact published result.
Missing or incompatible retained evidence refuses; it never falls back to full
analysis. Provider/model, policy, configuration, evidence and full continuation
scope remain unchanged. Event watermarks are shared with full rescan so changing
mode cannot buy a second window from one event.

Only trusted engine execution emits the versioned bounded-stop receipt, binding
execution identity, provider call count, complete inventory, reviewed unit set
and scope (which includes the original resource budget). Provider output cannot
supply it. The new result field has an explicit content-digest domain; legacy
artifacts retain their digests and cannot earn an exemption. Unified execution
requires complete attempt accounting, zero unknown output liability and no
failed/unknown pending dispatch. Recovery formats that lose that accounting are
ineligible. Deadline diagnostics alone never prove a clean unified stop.
Legacy chunk execution tracks failure before flattening it into coverage reasons.

One core checkpoint predicate verifies a consumed remaining receipt, exact
retained parent transaction/digest, identical finite inventory and scope, strict
reviewed-unit growth and preservation of all prior findings/human obligations.
A productive bounded partial neither completes a round nor increments failed
attempts. It resets no counter and authorizes no additional request. Other
partial or failed attempts keep existing charges, failure ceilings and elevated
one-use successor rules. Complete results retain normal round accounting.

The checkpoint CAS retains a fixed-shape progress receipt beside its consumed
event. Receipt history remains bounded at six and replay watermarks remain
monotonic; there is no lifetime productive-window counter. Crash recovery uses
the same predicate. Local retention preserves transaction-addressed source
artifacts before replacing the compatible latest-result file, so retaining a
child cannot destroy its parent authority. Retained artifacts remain subject to
filesystem/storage limits; failures preserve the ledger and withhold approval.

This does not complete media obligations, accept findings, resolve stale threads
or grant approval. Native finalization retains independent exact-head evidence,
authorization, required qualification and finding/thread gates. No-progress or
an atomic unit that cannot fit requires a specific configuration/evidence repair
or an explicitly authorized policy decision, rather than repeated inference.
