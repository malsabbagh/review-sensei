# ADR 0050 - Maintainer disposition and handoff status

Status: Proposed
Date: 2026-09-19
Last amended: 2026-09-20
GitHub Issue: #136
Pull Request: [#145](https://github.com/malsabbagh/review-sensei/pull/145)
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
- `@sensei review reenroll`
- `@sensei dismiss|defer|accept-risk <fingerprint> --reason <text>`

`@sensei review reenroll` is the authenticated recovery command for an expired
or witness-only session (see the amendment below). It is the only command that
retires durable state and resets round counters, it is refused for a live,
present-but-unreadable, or ambiguous record, and like every other mutating
command it requires GitHub writes to be enabled (`--allow-write` on the CLI and
`github_writes` on the hosted path).

Unauthorized actors, bots, and the App itself are ignored. Dismiss,
defer, and accept-risk require a reason and bind to a finding
fingerprint plus optional head SHA. The bounded disposition object is persisted
on the C3 session record. A recorded disposition is a human decision
(`honored-disposition`), never an independently verified fix.
Accepted exceptions do not waive coverage, current-head identity, model
qualification, or other repository rules.

Operator pause is stored on the C3 session record as `operator_paused`.
Legacy C3 documents without the field still load (digest compatibility)
and default to not paused. The next mutate rehashes with the new field.

C5 `handoff` publication now projects to run outcome `action_required`,
which is a failing CLI/check status. `skipped_policy` remains for
disabled writes, forks, and other non-handoff skips. With GitHub writes
disabled, `GitHubApplication.apply_maintainer_command` returns
`writes_disabled` for mutating commands without exchanging a broker capability.
Status uses the read-only `review_status` capability for a hosted ledger and
does not create a missing session comment.

Exact-head binding uses the command's optional head SHA against the
current pull-request head at trigger time. Advisory mode still does not
dismiss existing `REQUEST_CHANGES`. The original C6 implementation added no
new GitHub App scopes; the F4 amendment below defines its separately scoped
session capability.

## Amendment 2026-09-20: authenticated session authority

Issue #146 F2/F4 requires a missing session marker not to reset an established
PR's budget. The GitHub comment alone cannot distinguish deletion from first
enrollment. The existing OIDC broker therefore owns a separate, hashed
enrollment witness and a closed `review_session` capability. Before the
application creates or mutates a GitHub session comment, it supplies the
numeric repository/PR identity and exact current head to the broker. The
broker validates those values against the authenticated OIDC repository and a
live App-authenticated PR read, then records or finds the witness in its
existing Durable Object. A witness combined with a missing comment is an
authenticated recovery requirement, never a new zero-budget session.

`review_session` requests only `pull_requests: write`; it is separate from
`review_publish`, which remains the only capability able to publish a
pull-request review. The durable session lives in one App-authored issue
comment, so the session capability needs no further grant and the broker never
requests `checks: write`, which ADR 0022/0006 do not register for the App. The
witness is keyed by the verified current head and retained for 90 days from its
most recent use, matching ADR 0047's maximum session lifetime, so both a new
head and a long-idle pull request still enroll cleanly. The subsequent F4
session-boundary work binds this capability to serialized workflow execution
and an opaque, short-lived grant before every hosted mutation.
Once a record adopts the continuation-grant protocol, the transition is
one-way for that record: legacy caller-supplied `--continue-rounds` remains
disabled until authenticated `reenroll` creates a fresh record.
Consumed continuation command IDs remain in the bounded session record as
replay tombstones. A newly authenticated command may supersede only the one
unconsumed grant whose head or policy scope is stale; it never clears consumed
tombstones or resets a live grant-bearing record. The record permits at most
four continuation grants; a fifth authenticated continuation is refused rather
than pruning a consumed command ID and reviving its one-use authority. The
bound is a storage and replay-safety limit, not an approval or review-round
budget. Once a live record reaches that bound, the operator must wait for the
record to expire (or recover a witness-only/missing marker) and then issue the
authenticated `reenroll` command, which establishes a fresh record and resets
the round counters.

## Scope

In scope:

- Command parser, authorization, pause/continue/status on the session
  ledger, finding disposition objects, `action_required` run outcome,
  trigger `operation=command`, CLI `github command`, and disabled-write
  tests.

Out of scope:

- Sequential evaluation, shadowing, and default-mode promotion (C7).
- Changing `REVIEWSENSEI_AUTO_APPROVE` or recreating #114/#115 gates.
- Reusing `review_publish` as authority to create a replacement session after
  an authenticated broker witness reports that its marker is missing.
- A blanket `approve everything` command.

## Consequences

Positive:

- Exhausted or paused heads fail a required check instead of skipping.
- Maintainers can pause or continue without resetting round counters.
- Human exceptions stay visible as human.

Negative:

- The generated setup-v4 inline trigger script routes command-shaped
  comments without the packaged parser, so its prefilter is deliberately
  looser: bodies the parser rejects still resolve to `operation=command`
  when they differ only in the axes the regex accepts beyond the parser —
  the mention token's casing (the regex is case-insensitive, the parser's
  mention is case-sensitive), a second mention on a later line (the regex
  matches any mention at a line boundary while the parser reads only the
  first mention of the body), an empty or non-printable `--reason` value, a
  reason beyond the 512-byte bound, or separators inside the action words
  that the regex `\s` accepts but the parser's ASCII-only bound does not.
  The reusable workflow re-parses the body and the hosted handler returns
  `not-a-command` without a write, so the residual cost is a wasted run
  rather than an unauthorized mutation.
- On a caller that has not adopted the packaged trigger module path, that
  inline prefilter is the only command gate, so `REVIEWSENSEI_GITHUB_WRITES=true`
  alone enables the command surface even while `REVIEWSENSEI_MENTION_REPLIES`
  is false: the prefilter does not consult the reply switch, and the switch
  still governs conversational replies only. This widening from the previous
  release is deliberate; a comment the caller does not classify as a command
  still resolves as a conversational reply. The command path is the
  pull-request conversation (`issue_comment`); an inline review comment always
  resolves as a conversational reply in every copy of the resolver, so
  review-comment bodies remain governed by the reply switch.
- The caller workflow's `@sensei`, association, and user-type checks are a
  routing gate, not an authorization decision. Authority is re-derived
  inside the reusable workflow from the broker attestation and the durable
  session ledger, so editing the caller cannot widen it. The resolver's own
  job condition already requires the mention, an OWNER/MEMBER/COLLABORATOR
  association, and a non-bot author, so `operation=command` is only produced
  for those comments; the price is that an authorized actor the broker later
  refuses still starts a run that fails closed.
- `@sensei review status` is not a looser read path: the hosted handler
  still requires a caller-supplied OIDC token and exchanges the read-only
  `review_status` capability before it reads any ledger state, so status is
  bound to the same broker-attested actor and association as every
  mutation, and only its broker scope is narrower.

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
- Retire the inline trigger fallback once every supported caller resolves
  the packaged trigger module path, so the command grammar has one
  implementation instead of four byte-locked copies.

## Links

- Related issue: [#136](https://github.com/malsabbagh/review-sensei/issues/136)
- Pull request: [#145](https://github.com/malsabbagh/review-sensei/pull/145)
- Related: [ADR 0046](0046-evidence-based-blocker-admission-and-review-loop-convergence.md),
  [ADR 0047](0047-durable-review-session-ledger.md),
  [ADR 0049](0049-automation-admission-and-handoff.md)
- Supersedes: none
- Superseded by: none (C7 sequential evaluation is a follow-up, not a replacement.)
