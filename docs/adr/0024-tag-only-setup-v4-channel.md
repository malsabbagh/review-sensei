# ADR 0024 - Tag-only setup-v4 workflow channel

Status: Proposed
Date: 2026-08-30
GitHub Issue: #64
Pull Request: #79
Owners/Reviewers: Maintainers
Approved by: not configured

## Context

Setup-v4 previously combined a public workflow tag with an install-time SHA
pin and a legacy SHA allowlist. That split release authority between multiple
Worker variables, required a new customer PR for every public cutoff, and made
the generated caller differ from the public release channel. The requested
contract is one operator-managed channel: the protected public `v4` git tag.

## Decision

- `PUBLIC_WORKFLOW_TAG` is the only Worker setup-v4 workflow configuration.
- The setup Worker validates that the configured tag resolves before creating a
  setup PR. Generated callers reference
  `malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@v4` directly.
- The reusable workflow accepts only the canonical public repository tag-ref
  form. The broker binds the configured tag to its runtime `job_workflow_sha`;
  the workflow still uses that SHA for the immutable source fallback and
  validates that it is a full lowercase commit SHA.
- The OIDC broker resolves `PUBLIC_WORKFLOW_TAG` on every capability exchange and
  requires both the exact `@refs/tags/<configured-tag>` `job_workflow_ref` and a
  matching `job_workflow_sha`. No `PUBLIC_WORKFLOW_SHA` or
  `PUBLIC_WORKFLOW_LEGACY_SHAS` configuration is supported.
- Historical setup-v3 and SHA-pinned setup-v4 files remain recognizable for
  migration, but new setup output and current-v4 recognition are tag-based.

## Consequences

Positive:

- One visible release control updates every tag-following client after the
  normal setup lifecycle trigger.
- Installation, generated files, reusable-workflow checks, and broker policy
  describe the same public channel.
- The broker retains an immutable runtime check by resolving the tag and
  comparing the Actions OIDC SHA.

Tradeoffs:

- Moving `v4` changes trusted workflow code immediately for existing callers;
  the tag must be protected and moved only after the audited public snapshot is
  published.
- The broker performs a GitHub tag-resolution call for each token exchange.
- Existing SHA-pinned callers no longer obtain a v4 capability until the App
  generates and the repository merges a tag-following migration PR.

## Rollout and rollback

Deploy the reviewed Worker with `PUBLIC_WORKFLOW_TAG=v4`, publish the reviewed
public snapshot, move `v4` to that snapshot, and trigger a fresh installation or
permission-acceptance delivery. Merge the generated setup PR, then enable
customer opt-ins. Roll back by moving `v4` to the last approved public commit or
redeploying the prior Worker; no customer default branch is written directly.

## Alternatives considered

- A configured SHA pin was rejected because it duplicates the public cutoff and
  requires separate Worker configuration updates.
- Ref-only broker authorization was rejected because it would not detect a tag
  move during a running workflow; the runtime SHA check remains required.
- An arbitrary branch or caller-supplied ref was rejected because it expands the
  trust boundary beyond the protected public release channel.
