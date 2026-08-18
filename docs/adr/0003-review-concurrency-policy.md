# ADR 0003: Expose host-enforced review concurrency policy

- Status: accepted (amended 2026-08-09)
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

## Rollback

A host can stop requesting or enforcing the plan and continue calling
`ReviewService.review()` directly. Removing the feature does not change review
result validation, provider transport, or stored learnings.
