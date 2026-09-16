# ADR 0003: Expose host-enforced review concurrency policy

- Status: accepted (amended 2026-08-09, 2026-09-16)
- Date: 2026-08-03

## Context

The internal Code Sensei workflow learned two concurrency rules. A newer run
for the same repository and pull request must supersede an older run, while
provider-consuming stages for one pull request must use one slot without
cancelling an active stage. Different pull requests must not share that slot.
Ignored or informational triggers must also be isolated so they cannot cancel a
real review.

The public package intentionally has no GitHub workflow runner, durable queue,
or provider-specific scheduling dependency. Embedding scheduling in
`ReviewService` would create process-local behavior that could not coordinate
separate workers and would make cancellation of a synchronous provider call
misleading.

## Decision

Add `ReviewConcurrencyPlan` and `ConcurrencyGroup` to the provider-neutral core.
For a pull-request request, the plan contains:

- a workflow group in the `review` namespace with length-prefixed repository
  and pull-request components, one active run, and `cancel_in_progress=True`;
- a provider group in the `provider` namespace with length-prefixed provider,
  repository, and pull-request components, one active stage, and
  `cancel_in_progress=False`.

Length-prefixed components make the serialization unambiguous even when safe
identifiers contain delimiter characters. Pull-request numbers and numeric
trigger ids are limited to signed 32-bit positive integers so keys remain
bounded before conversion to text.

For non-review triggers, the plan contains an isolated trigger-specific
workflow group and no provider group. `ReviewService.concurrency_plan()` is a
convenience seam that selects the configured provider name without changing
review execution.

The plan is descriptive. A host scheduler is responsible for applying the
groups across processes, workers, or hosted workflows.

## Alternatives considered

### In-process locks or thread pools in `ReviewService`

Rejected because they cannot coordinate multiple workflow runners, cannot
reliably interrupt the existing synchronous provider protocol, and would make
the review business logic own queue state.

### GitHub Actions expressions in the public package

Rejected because they would couple the public engine to GitHub event payloads
and prevent other hosts from reusing the policy.

### One repository-wide provider group

Rejected because a slow review would consume the queue for unrelated pull
requests. The provider group is scoped to repository plus pull request.

## Consequences

Positive:

- Hosts can implement consistent latest-wins and per-pull-request admission
  without importing GitHub or model-provider SDKs into the core.
- The policy is deterministic and testable without network access.
- Different pull requests remain eligible to run concurrently.
- Non-review triggers cannot cancel a pull-request review when the host uses the
  isolated plan.

Tradeoffs:

- The public package does not enforce the plan by itself.
- A host must map `ConcurrencyGroup` values to its own queue and cancellation
  primitives.
- Group keys intentionally include repository and provider identifiers and
  must be treated as orchestration metadata rather than review content.
- Hosts upgrading from the original delimiter-only key format must drain or
  explicitly migrate existing scheduler groups before switching formats. The
  new keys intentionally do not collide with the old namespace.

## Amendment - issue #27 PR-scoped hosted admission and in-process leases

Date: 2026-09-16
GitHub Issue: #27
Status: this additive amendment records the hosted and in-process contracts;
the original decision remains in force. It is not a new ADR and does not
change the Accepted status of other records.

### Decision

GitHub-hosted review admission is the reusable workflow concurrency group:
repository + operation + pull-request number, `max_active=1`, and
cancel-in-progress for reviews. Head SHAs remain exact-head publication
identity and are not cancellation keys. Reply operations use the unique run
id / non-review trigger identity and do not share the review cancel group.
Manual and automatic reviews for the same pull request join the same
provider latest-wins group after authoritative preflight.

`ProviderAdmission` is an optional in-process helper for tests and local
hosts. It enforces `ConcurrencyGroup.max_active`, bounds waiters so they
cannot accumulate without a cap, and releases the lease after success,
cancellation, or failure. It is provider-neutral, has no GitHub SDK, is not
a cross-process scheduler, and must not be used as a global lock across
repositories. GitHub Actions does not use this helper; hosted admission is
native workflow `concurrency`.

A cancelled or stale hosted run cannot publish even if the provider
finished: publish steps require `success() && !cancelled()` and a current
live-head admission output. The read-only live head SHA check retries transient `gh api` failures with
bounded exponential backoff and jitter, fails fast on `401`/`403`/`404`,
then fails closed (job failure, no publish) when the API remains unavailable
or the live SHA is malformed. A SHA mismatch records `skipped_stale` and
skips publish without failing the job.
Exact-head publication preflight remains; the pre-publish check does not
mint write tokens.

Admission and cancellation logs are metadata-only. They record group keys,
counts, and closed-set statuses, never prompts, diffs, or source content.
The Cloudflare Worker still only bootstraps setup and does not invent a
SHA-based concurrency key.

### Consequences

- Local hosts can exercise bounded provider capacity without GitHub SDKs.
- Hosted latest-wins remains PR-scoped and SHA-free.
- Cancelled provider work cannot publish a superseded head.

## Rollback

A host can stop requesting or enforcing the plan and continue calling
`ReviewService.review()` directly. Removing the feature does not change review
result validation, provider transport, or stored learnings.
