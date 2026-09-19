# ADR 0047 - Durable review-session ledger

Status: Proposed
Date: 2026-09-19
GitHub Issue: #136
Pull Request: [#139](https://github.com/malsabbagh/review-sensei/pull/139)
Owners/Reviewers: Maintainers
Approved by: not applicable

## Context

Issue #136 C1 defined `RoundSessionState` and `evaluate_round_admission`. C2
applies blocker admission before publication. Those counters still live only
in one process. A later GitHub Actions job cannot see completed initial
reviews, verification rounds, or failed attempts, so C5 cannot yet enforce a
PR-wide budget.

ADR 0042 / issue #38 already names a pull request as `repository` +
`pull_request`. Recovery artifacts (ADR 0043) already use expiry and a content
digest. C3 needs durable counters without a hosted database, without storing
source or findings, and without refusing publication.

## Decision

Add a versioned `session-record` document and a `SessionLedger` protocol with
two adapters:

- `LocalSessionLedger` writes one JSON file per pull request under an operator
  supplied directory (`--session-ledger` / `REVIEWSENSEI_SESSION_LEDGER`).
- `GitHubIssueCommentSessionLedger` stores one App-authored issue comment on
  the source pull request, bound by
  `<!-- reviewsensei:session:v1 repo=... pr=... gen=... digest=... -->`.

The GitHub comment has a separate bounded framing allowance (`2 *
MAX_SESSION_RECORD_BYTES`) so the serialized record, JSON fence, intro, and
marker are capped before parsing. Schema field limits remain stricter than that
byte ceiling; a comment that fits the transport bound but violates a field
limit is an integrity failure, not a sizing expansion.

`LocalSessionLedger` is a single-writer adapter per repository/pull-request
identity. Its atomic replacement protects individual files but does not claim
inter-process locking. The GitHub-backed adapter performs bounded
pre-discovery checks and post-update readbacks, but GitHub issue-comment PATCH
does not expose a conditional generation or ETag precondition here. Its
cross-process protection is therefore best-effort; deployments requiring a
strict no-lost-update guarantee must serialize writers for an identity.

Session identity is the ADR 0042 pair `repository` + `pull_request`. Optional
`repository_id` binds GitHub comments. Head SHA, model, and policy digests are
not part of the key and cannot reset the round limit.

Records store only counters, CAS `generation`, a paired reservation, expiry,
and `record_sha256`. They never contain source, prompts, findings, or provider
output. Default TTL is 30 days; the maximum is 90 days. Missing, expired,
tampered, or conflicting state is explicit. GitHub discovery that is truncated
or finds two markers fails closed.

Local and in-memory mutations are compare-and-swap on `generation`; GitHub
mutations use the same generation metadata with the best-effort hosted checks
described above. `reserve` holds
`initial` / `verification` / `failed-attempt`. `commit` applies the increment
and is idempotent for the same `reservation_id`. `abort` drops an uncommitted
hold. Local files migrate `schema_version=0.1` documents that use
`pull_request_number`, carry an explicit `created_at`, and have no digest;
GitHub-backed loads never migrate or rehash. Legacy expiries are bounded both
above and below before the migrated record is accepted; legacy `updated_at` is
validated and retained (or normalized from `created_at` when omitted). A
deterministic
reservation id is an idempotency key for one
repository/PR-head/slot attempt; after that id commits, a retry does not create
another reservation. An abort replay that observes a later generation raises a
CAS conflict, which tells the caller to reload rather than silently dropping a
different writer's reservation.

Initialization is an idempotent ensure operation for an already validated
record. A caller that loses the initial read/create race can continue with the
record discovered from the winning writer; torn creates that leave multiple
markers still fail closed as a conflict.

The visible consumer is doctor/plan display plus GitHub publication
write-through. Operator modes (`advisory`, `merge-focused`, `strict`) reserve
before `ReviewPublisher.publish` and commit only when the publication status is
`published`; every other publication result aborts the hold without counting,
so stale-head and transient retries remain eligible for a later attempt.
`legacy` may initialize a missing record for display, but it does not reserve or
count a round. C3 does not refuse a review when the C1 decision would hand off;
that enforcement remains C5. `REVIEWSENSEI_AUTO_APPROVE` and
#114/#115 gates are unchanged.

## Scope

In scope:

- Session record schema, local and GitHub-backed adapters, CAS/idempotency,
  expiry, missing-state, integrity, and v0.1 local migration tests.
- Doctor/plan display and optional GitHub publication persist.

Out of scope:

- Host enforcement of round budgets (C5), baseline-aware verification (C4),
  maintainer dispositions (C6), evaluation rollout (C7).
- A hosted database, Durable Object, or second review engine.
- Changing `REVIEWSENSEI_AUTO_APPROVE` or recreating #114/#115 gates.

## Consequences

Positive:

- Later jobs can read the same PR-wide counters C5 will enforce.
- Missing ledger state cannot be mistaken for a fresh zero-round session by
  GitHub-backed loads that fail closed on ambiguity.

Tradeoffs:

- A GitHub comment can be deleted. C3 reports missing rather than inventing
  counters.
- Local and in-memory concurrent jobs lose a CAS race instead of
  double-counting. GitHub issue-comment updates have no conditional PATCH
  primitive, so independent hosted writers must be serialized when a strict
  no-lost-update guarantee is required; the adapter's post-update readback
  catches many races but cannot make the remote PATCH atomic.
- GitHub initialization posts once and then rediscoveries the bounded marker.
  If a torn or concurrent create leaves multiple session comments, discovery
  returns `conflict` and initialization does not overwrite either comment. An
  operator must delete the extra or invalid session comment manually, leaving
  at most one valid marker, before retrying.
- Local repository directory names percent-encode the identity separator, so
  repository slugs remain injective on disk. Local writes fsync the temporary
  file and, on POSIX, the containing directory; if the directory fsync fails
  after replacement, the write is treated as durably replaced and the caller
  receives the synchronization error without deleting the new record.
- Local initialization uses an exclusive filesystem create for a missing
  identity, so concurrent initializers lose explicitly instead of replacing a
  first record. Callers with a trusted checkout root may pass it to the path
  resolver for containment; traversal and Windows device/UNC paths are always
  rejected.
- GitHub discovery accepts only terminal, bot-authored session markers. When
  an App slug is available it must match the comment author, and each mutation
  re-discovers the marker immediately before its update and after its update;
  these checks reject observable stale generations but do not replace a
  serialized writer or a remote conditional update.

## Alternatives considered

### Reuse the in-memory `ReviewContextCache`

Rejected because cache keys include head SHA and configuration digests, and
the cache is not durable across jobs.

### Store counters in the recovery artifact

Rejected because recovery artifacts bind one head and a full `ReviewResult`.
Round budgets are PR-wide and must not carry source.

### SQLite or a hosted Durable Object

Rejected by issue #136: no broad database/platform project.

## Validation

Unit tests cover local and GitHub adapters, CAS conflicts, idempotent
commit/reserve, expiry, missing and tampered state, v0.1 migration, doctor/plan
display, and operator-mode publication write-through that still publishes.
Ordinary CI stays offline.

## Rollout and rollback

Opt-in via `--session-ledger` or `--github-session-ledger`. Existing
installations write nothing. Rollback by omitting the flags; leftover comments
or files are inert counters.

## Follow-up work

- C4–C7 as specified in issue #136.
- C5 consumes this ledger to refuse/handoff autonomous rounds.

## Links

- Related issue: [#136](https://github.com/malsabbagh/review-sensei/issues/136)
- Pull request: [#139](https://github.com/malsabbagh/review-sensei/pull/139)
- Related: [ADR 0042](0042-incremental-reviews-and-finding-lifecycle.md),
  [ADR 0043](0043-structured-run-outcomes-budgets-and-publication-recovery.md),
  [ADR 0046](0046-evidence-based-blocker-admission-and-review-loop-convergence.md)
- Supersedes: none
- Superseded by: none
