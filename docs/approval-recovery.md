# Reaching approval with bounded recovery

Version applicability: the durable decision snapshots, remaining-coverage windows
and metadata phases below
are **unreleased**. They are implemented by the approval-liveness repair; do not
assume a checkout or a green workflow has updated the installed runtime. The
release receipt must identify both Python versions and the signed `v5` source.
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

The following operation is **unreleased**. It requires compatible core/hosted
packages, the activated reusable runner and an updated caller. Updating `v5`
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

The unreleased snapshot writer preserves all audit fields in authenticated,
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

When blocked, retain the source event, exact head, installed versions, reason,
recorded/unrecorded decision counts, thread state and next supported command.
Do not include tokens, raw provider output or private source in diagnostics.
