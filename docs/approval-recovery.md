# Reaching approval with bounded recovery

Version applicability: the durable decision snapshots, remaining-coverage windows,
metadata phases and progress diagnostics below were published in **core 0.8.12 /
hosted 0.3.13** from signed source
[`c80d1983`](https://github.com/malsabbagh/review-sensei/commit/c80d19837f7bc3ac90a54fb8184638443628b7b6).
Publication does not update an installed workflow. Confirm both installed Python
versions and the qualified signed `v5` source in the release/activation receipt;
do not infer activation from a checkout or a green workflow. The remaining-work
route also needs a compatible caller, as described below.

Hosted **0.3.14**, paired with the unchanged core **0.8.12**, adds operational
failure observations and a registry-only core installation contract. Its signed
workflow source is
[`41792ec8`](https://github.com/malsabbagh/review-sensei/commit/41792ec89afb092c4b8ad0ad6816fba945ab551f).
These additions do not supply missing review evidence or guarantee automatic
thread cleanup or native approval. Check the activation receipt and installed
versions before expecting these diagnostics from `v5`.

Hosted 0.3.12 / core 0.8.11 still use the embedded decision ledger and can reach
its byte limit. Never reset a session or discard decisions to get past that limit.

## Choose the operation from the retained evidence

- A normal review performs analysis. Resolving a GitHub thread by hand does not
  itself trigger a new workflow or prove the finding was independently fixed.
- `verify` reloads current decisions and reconciles approval and thread cleanup;
  it does not run a model or fill missing text coverage.
- `review continue` unpauses a review or admits the separately authorized bounded
  failed-attempt window. It does not currently mean “review every remaining file
  of a published partial result.”
- `rescan` requests fresh bounded analysis. It deliberately starts from the full
  current snapshot rather than silently trusting a previous partial analysis.
- A retained media receipt follows the original media command's supported replay
  path. Preserve the original human source event and inventory. Do not fabricate
  a new confirmation, treat media as text analysis, or repeatedly rescan it.

```text
@reviewsensei review status
@reviewsensei verify
@reviewsensei review continue
@reviewsensei rescan
```

Check the resulting diagnostic before choosing another operation. A successful
workflow can truthfully report that approval is withheld.

## Continue a published partial text review

The following operation requires core 0.8.12 / hosted 0.3.13 or a compatible later
pair, the qualified activated reusable runner and an updated caller. Updating `v5`
alone cannot update an installed caller's embedded trigger parser. Inspect the
caller with `review-sensei-github setup --help` and its supported migration flow;
preserve custom runner labels, pins and secrets. The migration diagnostic names
`remaining_coverage_routing` when this route is missing. Do not run the command
against an older installed caller and interpret its discussion reply as analysis.

```text
@reviewsensei review remaining
@reviewsensei review remaining commit abc1234
```

Replace `abc1234` with the current PR head or its 7–40-character prefix. Each new
standalone human comment buys one normal bounded analysis window. It reloads the
exact published partial artifact and reviews only pending reviewable text while
retaining prior findings. Check the resulting coverage and publication receipt
before submitting a new window. A retry of the same source event does not buy
more inference; recover its retained publication first. Manual workflow dispatch
may select `review_remaining: true`; do not simultaneously select full rescan.

Productive windows that strictly extend the same finite coverage inventory and
carry trusted bounded-execution evidence do not consume the failed-attempt
allowance. They do not reset counters, extend one request's budget, accept any
finding or grant approval. Failed/unknown dispatches and no-progress windows
still count. Exhausted genuine failures require the separately authorized
`review continue` mechanism; requesting remaining work cannot bypass it.

A changed base/head, policy/configuration, provider/model, budget or source
requires establishing compatible current evidence first. Missing/corrupt parent
artifacts refuse instead of silently reviewing from the beginning. If a single
atomic unit cannot fit or progress stops, repair the identified configuration or
evidence issue. Fresh rescan remains a separate deliberate operation. Text
coverage does not replace media/human assessment or independent approval gates.

Complete file and hunk counts can coexist with a partial review. For example,
incremental reconciliation keeps the result partial when older findings cannot
be uniquely matched to current evidence. Finding overrides do not establish that
the older concerns were fixed, and remaining-text analysis has no work to schedule
if every text unit is already reviewed. Inspect the retained result and baseline
before choosing a new operation. For confirmed prior-finding uncertainty, the
existing full rescan starts a fresh bounded review without that incremental
baseline:

```text
@reviewsensei rescan commit abc1234
```

Replace the prefix with the current head. Reconcile that one operation before
requesting another. A full rescan can discover new findings or remain incomplete;
it does not waive evidence requirements or guarantee approval. This full-rescan
route is already available in core 0.8.11 / hosted 0.3.12.

## Record legitimate maintainer decisions in batches

Assess every finding against its source and evidence before accepting its risk.
The command below records a human disposition, not an independently verified fix.
It requires the existing maintainer/admin authority and current finding identity.
Up to **10 finding IDs per comment** keeps authorization, storage writes and
readback bounded. Spaces or commas separate IDs. Post another assessed batch
when more findings remain; the limit applies to each request, not a total of ten
findings in the review.

```text
@reviewsensei override RS-ABC123 RS-DEF456 acceptable risk
@reviewsensei accept-risk abc123abc123abcd --reason tracked mitigation
@reviewsensei verify
```

The compatible snapshot writer preserves all audit fields in authenticated,
immutable parts and commits one complete inventory reference in the session.
It does not remove the ten-ID command limit, increase the request budget, or
drop earlier decisions. A fresh process validates the complete referenced
inventory. Missing or corrupt parts withhold approval; there is no inline or
empty-inventory fallback. Small legacy records remain readable.

Storage is finite: at most 4,096 snapshot entries, 32 parts per snapshot,
2 MiB decoded evidence, and the existing 256-part / 8 MiB retained-part limit.
The default reviewer inventory has its own smaller admission limits. Replacing
snapshots retains prior immutable parts, so storage capacity can be exhausted
before the entry ceiling. A capacity diagnostic requires evidence-preserving
operator repair; do not split requests indefinitely, remove audit fields,
rotate actors, or change the head just to evade it.

## Finish thread cleanup and approval

After recording decisions, run `verify`. If it reports unconfirmed thread
resolution or a retained decision with failed gate re-evaluation, first check
whether the operation committed. On the unchanged source/head, a fresh supported
`verify` request resumes unresolved work under a new request's own allowance.
Already resolved matching threads are observations; reopened threads must be
reconciled again. Every actual mutation and final approval retains current
identity, finding, permission and eligibility checks. No new inference is needed
for this cleanup.

If a bounded retry makes no progress, retain its diagnostic and inspect the
specific unresolved conversations and authority checks before another retry.
An authorized maintainer may use GitHub's normal **Resolve conversation** control
after independently assessing that conversation. This records a native thread
resolution; it does not record a finding-risk decision or supply missing analysis
or human evidence. Run supported finalization afterward and verify its actual
outcome. Manual resolution does not establish that automatic cleanup is fixed.

A lost response is an uncertain outcome, not proof the write failed. Reconcile
retained decisions, the original native review and the current check before
retrying. The existing approval marker binds the exact result/head; sequential
replay must reuse that approval rather than post another one. Serialize commands
for one PR; GitHub comment updates do not provide a distributed CAS lock.

For a new head or changed policy/base/source, re-establish the current review
and finding authority. Old decisions and receipts cannot grant approval to a
stale or different review. Missing coverage, an unresolved human decision,
revoked authority, invalid evidence or required qualification still withholds
approval. An explicit coverage-risk decision is a separate elevated policy path,
not implied by finding overrides.

## Verify the actual outcome

Completion means an App-authored native **APPROVED** review for the intended
head/result, the current ReviewSensei check is successful, and required native
threads are confirmed resolved. Record those IDs and the installed versions.
A green workflow, a comment saying decisions were stored, or a merged PR alone
is not that receipt.

### Operational failure observation (hosted 0.3.14+)

The separate `approval-operation-failure` schema 1.0 observation was introduced
in hosted 0.3.14 for `verify` and finding-disposition re-finalization. It is not
available in hosted 0.3.13. When one of those operations observes a failure, the
line identifies the first failure in each phase of eligibility readback, check
publication, thread cleanup and approval, plus bounded failure counts. It reports closed stage/reason names and allowlisted
HTTP/GraphQL categories; raw messages, responses, tokens and source are excluded.
Later call exhaustion does not replace an earlier failure in another phase.

The original budget's observed calls and 64-call ceiling are separate from its
60-call ordinary-dispatch limit: four calls remain reserved for explicit final
work. No counter or deadline is refreshed. Transport request attempts include
invocations rejected locally by an inner allowance; they are not network-call
counts. `approval_request_attempted` means the finalizer invoked the approval
HTTP adapter, not that the POST reached GitHub. `network_dispatch_observed` is
null because this observation cannot establish that boundary. A failure before
that adapter invocation records false; a lost dispatched response remains
uncertain until the native review is read back.

The observation adds no requests and never controls eligibility. Inspect its
first failing stage before another unchanged retry. A REST check can succeed
before a later GraphQL read or approval request fails. Preserve both facts; do
not expand permissions, reset resource budgets, repeat inference, or claim
native approval from that check. The original `approval-progress` 1.0 document
is unchanged and remains the final machine-readable line.

A cleanup `FORBIDDEN`, `UNAUTHORIZED`, HTTP 401 or HTTP 403 is an authorization
hold. Retain recorded decisions and confirmed thread state. Do not follow a
generic `retry_verify` suggestion until the denial has been investigated and
materially relevant authority evidence or a reviewed supported recovery path
allows it. Do not switch actors, substitute a broader token or change App,
installation or workflow permissions as a retry. Any needed access expansion
requires an explicit scoped approval. An uncertain lost response is different:
read back the original target before deciding whether any write remains needed.

When blocked, retain the source event, exact head, installed versions, reason,
recorded/unrecorded decision counts, thread state and next supported command.
Do not include tokens, raw provider output or private source in diagnostics.

## Read finalization progress

The compatible command diagnostics can emit an `approval-progress` JSON line
with schema version 1.0. Its head, result digest and source review ID identify
the observation. `honored_findings` counts authenticated current index entries,
not IDs merely requested in a comment. Unknown coverage counts, human counts
and original media source IDs are null. Recorded qualification describes the
retained fact; it does not claim this command freshly qualified a provider.

`decided_thread_reconciliation` concerns decided findings, not every native
thread. `requested_check_conclusion` is distinct from confirmed check publication.
A missing check token is `not-attempted`; a failed or permission-denied request
is `unconfirmed`. Only a successful create/update with an observed positive
check ID is confirmed. An unreadable approval response is `not-observed`, since
the write may have committed; it is not evidence that approval was withheld.
Approval can be submitted even when a later check update fails, so preserve
`approval_state` and reconcile that review before retrying. The observation adds
no request, authority or budget and never feeds eligibility.

For `next_action: inspect_partial_source`, inspect the referenced retained
review. A verify command has not established reusable text scope or media
confirmation authority. Choose validated remaining-text work, a fresh full rescan
for confirmed prior-finding uncertainty, or the original media source replay as
appropriate; do not repeat the same operation
without checking its actual outcome. Missing evidence requires repair, not
clearing a flag or relabeling partial analysis complete.

## Explicitly accept an assessed coverage uncertainty

A maintainer/admin may make the separately audited coverage-risk decision when
the current supported policy permits it. Assess the exact missing evidence
first and explain the accepted risk. This is a human policy exception, not proof
that AI reviewed the missing work, and it cannot waive independent authority,
source-integrity, required finding, thread or provider-qualification gates.

```text
@reviewsensei accept-coverage-uncertainty --reason Independently assessed remaining coverage risk and tracked mitigation
@reviewsensei verify
```

The decision binds the current head and exact incomplete-evidence inventory.
Inspect the resulting exception outcome and actual native review; a new source,
head or policy needs fresh assessment. Do not use this command to hide a failed
publication, corrupt receipt or unverified security requirement.
