# ADR 0050 - Maintainer disposition and handoff status

Status: Proposed
Date: 2026-09-19
Last amended: 2026-09-19
GitHub Issue: #136
Pull Request: not yet linked
Owners/Reviewers: Maintainers
Approved by: not applicable

## Context

Issue #136 C6 requires authenticated maintainer control after C5 has
stopped autonomous rounds. C5 maps handoff onto `skipped_policy`. GitHub
required checks can treat skipped/neutral as passing, which would hide
an unreviewed or exhausted head. Maintainers also need bounded pause,
status, continuation, and finding-scoped disposition without a blanket
approve command or a fake verified-fix.

Existing mention replies already authorize human OWNER/MEMBER/COLLABORATOR
actors and reject the App. C6 reuses that association set. `@sensei`
command spellings in the strategy issue were proposals; this slice makes
a closed subset supported.

## Decision

Support these comment commands, case-sensitive `@sensei` gate:

- `@sensei review status`
- `@sensei review pause`
- `@sensei verify`
- `@sensei review continue --rounds 1`
- `@sensei dismiss|defer|accept-risk <fingerprint> --reason <text>`

Unauthorized actors, bots, and the App itself are ignored. Dismiss,
defer, and accept-risk require a reason and bind to a finding
fingerprint plus optional head SHA. A recorded disposition is a human
decision (`honored-disposition`), never an independently verified fix.
Accepted exceptions do not waive coverage, current-head identity, model
qualification, or other repository rules.

Operator pause is stored on the C3 session record as `operator_paused`.
Legacy C3 documents without the field still load (digest compatibility)
and default to not paused. The next mutate rehashes with the new field.

C5 `handoff` publication now projects to run outcome `action_required`,
which is a failing CLI/check status. `skipped_policy` remains for
disabled writes, forks, and other non-handoff skips. With GitHub writes
disabled, `GitHubApplication.apply_maintainer_command` returns
`writes_disabled` without exchanging a broker capability.

Exact-head binding uses the command's optional head SHA against the
current pull-request head at trigger time. Advisory mode still does not
dismiss existing `REQUEST_CHANGES`. No new GitHub App scopes are added.

## Scope

In scope:

- Command parser, authorization, pause/continue/status on the session
  ledger, finding disposition objects, `action_required` run outcome,
  trigger `operation=command`, CLI `github command`, and disabled-write
  tests.

Out of scope:

- Sequential evaluation, shadowing, and default-mode promotion (C7).
- Changing `REVIEWSENSEI_AUTO_APPROVE` or recreating #114/#115 gates.
- A blanket `approve everything` command.

## Consequences

Positive:

- Exhausted or paused heads fail a required check instead of skipping.
- Maintainers can pause or continue without resetting round counters.
- Human exceptions stay visible as human.

Negative:

- Generated setup-v4 inline trigger script still classifies unknown
  issue comments as reply until operators upgrade to the packaged
  trigger module path. New CLI/trigger entry points understand commands.

## Alternatives considered

### Keep skipped_policy for handoff

Rejected: GitHub can treat skipped required checks as passing.

### Store pause only in a sidecar file

Rejected: C3 already has CAS adapters; pause belongs with the PR-wide
session so GitHub-backed loads see it.

## Validation

Run maintainer-command, session integrity, trigger, GitHub disabled-write,
CLI `github command`, and C5 handoff `action_required` tests. Ordinary CI
stays offline and credential-free.

## Rollout and rollback

Operator modes plus a session ledger remain opt-in. Rollback by returning
to `legacy` or omitting the ledger. Unresolved findings are not discarded.

## Follow-up work

- C7: sequential evaluation and opt-in rollout.
- Setup-v4 inline trigger parity for `operation=command` on older caller
  workflows after packaged trigger adoption.

## Links

- Related issue: [#136](https://github.com/malsabbagh/review-sensei/issues/136)
- Related: [ADR 0046](0046-evidence-based-blocker-admission-and-review-loop-convergence.md),
  [ADR 0047](0047-durable-review-session-ledger.md),
  [ADR 0049](0049-automation-admission-and-handoff.md)
- Supersedes: none
- Superseded by: none
