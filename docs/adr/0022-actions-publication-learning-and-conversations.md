# ADR 0022 - Tagged setup-v4 publication and authorized conversations

Status: Proposed
Date: 2026-08-19
GitHub Issue: #64
Pull Request: not configured
Owners/Reviewers: Maintainers
Approved by: not configured

Revised by: ADR 0025 removes the provider-specific feature restrictions while
preserving this ADR's authorization, idempotency, and setup-v4 boundaries.

## Context

ReviewSensei needs an opt-in GitHub integration without moving model
execution, provider credentials, or review content into a hosted service.
Generated customer workflows must remain reviewable, with immutable third-party
Action pins, while
App-authored review comments, deterministic learning draft PRs, and explicit
mention replies need exact-head, idempotent, fork-safe publication. Existing
installations also need a safe migration path that never overwrites custom or
future setup files.

## Decision

Setup version 4 is a thin customer caller that follows the public reusable
workflow at the operator-managed `v4` git tag during installation:
`malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@v4`. The
Worker validates that the tag resolves, and the broker resolves the same tag at
capability exchange time before requiring the runtime workflow SHA to match.
The tag is the only operator-facing workflow channel; moving it is an
intentional trust change.
The reusable workflow checks out only trusted base content and computes bounded
diffs. When the exact package distribution is unavailable, it installs from
the executing workflow commit SHA directly. It validates the installed version and typed results before
any write. Automatic
cloud pull-request review uses GitHub-hosted compute and
same-repository heads only. Local Ollama is restricted to manual or trusted
event execution on the labeled self-hosted runner.

Generated switches for automatic review, GitHub writes, learning PRs,
mention replies, and artifacts default to `false`. The caller can reference
the existing customer-owned `OLLAMA_API_KEY` by name only when passing it to
the reusable workflow; setup and the App never create, read, log, persist, or
reveal its value.

The Worker exposes an issuance-only `POST /github/token` route. It verifies
the GitHub Actions OIDC issuer, audience, time, repository identity, actor and
run claims, the current `v4` workflow ref plus its runtime-resolved SHA, allowed
event/runner policy,
server-side repository/fork state, and server-side installation mapping. It
performs bounded pre-auth admission before cached JWKS verification, claims
the verified assertion before GitHub API lookups, and stores hashed replay and
rate identities in a SQLite Durable Object. It then issues one disjoint
capability: `review_publish`, `inline_reply`,
`issue_reply`, or `learning_write`. The ledger stores no assertions, tokens,
source, diff, prompt, result, reply, or provider credential.

Review publication deduplicates by repository ID, PR number, exact head SHA,
and App author identity. Its stable marker retains the canonical result digest
as metadata, but nondeterministic result changes do not create a second review
for the same head. It submits one review containing the summary and validated
inline comments. Learning proposals use
canonical content digests, one allowed path, a deterministic branch, an
exact-base Git data commit, and a draft PR. Existing identical content skips;
different content conflicts. Conversation replies accept only standalone
case-insensitive `@sensei` mentions from human OWNER, MEMBER, or COLLABORATOR
authors; context and replies are bounded and source/head/root state is
rechecked before writing.

Setup reconciliation handles `installation.created` (including reinstall),
`installation.new_permissions_accepted`, and
`installation_repositories.added`. Absent, legacy, and v2 clients receive at
most one deterministic setup-v4 PR. Current v4 is a no-op, while a byte-exact
current or previously released v3 workflow, or a managed v4 workflow generated
for another valid public tag, is treated as managed stale content and migrated.
A previously released v3 uninstall template is also recognized as managed. Custom,
malformed, and future files are no-write cases. Released pre-marker Worker and
Python clients and setup-v2
clients are recognized only by byte-exact historical digests. Present v3 files
must exactly match a generated artifact; a v4 workflow's two public references
must agree on the embedded tag. Migration uses a create-only branch whose name
binds the base and update channel. Reuse requires the exact base parent,
ReviewSensei App author, canonical generated content, and a comparison limited
to generated paths. A pre-existing or concurrent collision is a no-write
conflict; the App never force-moves a ref. Only the three generated paths are
changed and the default branch is never written directly.

Mention replies remain on the trusted local self-hosted runner. Cloud mode is
an explicit source-data egress option for automatic review only; it does not
schedule comment content for cloud-provider processing.

## Consequences

Positive:

- Customer workflows own provider execution and secret values.
- Public workflow changes are auditable through a tag plus broker-verified
  resolved commit SHA.
- Exact-head markers and reconciliation close duplicate and stale-write races.
- Capability scopes keep review, reply, and learning writes separate.
- Installation lifecycle events can repair absent or legacy clients without
  overwriting repository-owned customizations.

Tradeoffs:

- Hosted acceptance requires a later public snapshot, an installable package
  release or the executing-SHA source fallback, Worker deployment, App
  permission acceptance, and customer setup-PR review.
- Moving the public `v4` tag changes the workflow trusted by tag-following
  callers immediately; protect the tag and publish the reviewed snapshot before
  moving it.
- The Python and Worker setup builders must remain structurally equivalent.
- The broker holds short-lived opaque installation tokens in request memory in
  order to call GitHub, but never persists or returns them outside issuance.
- Local comment-event execution still requires a trusted runner policy and
  must not be expanded to automatic fork PR execution without a new design.

## Rollback

Do not mutate customer default branches. Stop or redeploy the previous Worker
version with its existing Durable Object migration history, and leave current
ledger state intact. Close or revert unmerged setup-v4 PRs if the generated
contract is not desired. Revert the package/workflow/docs code through the
normal review process; no review-content data migration is required.

## Validation and release order

Deterministic checks cover invalid OIDC claims, wrong audience/event/repository
or workflow-SHA pairing, replay/rate limits, forks, missing installations, permission
scope, redaction, disabled writes, stale/duplicate/ambiguous publication,
learning conflicts, unauthorized mentions, and setup lifecycle no-write cases.
Release order is implementation merge, audited public snapshot, Worker
`PUBLIC_WORKFLOW_TAG` deployment, public `v4` tag creation or move to that
snapshot, App permission update/acceptance or reinstall/re-add,
setup reconciliation, setup PR review/merge, repository opt-in, and hosted
fixture acceptance. The source fallback installs from the executing SHA when a
PyPI package is not yet available.

## Links

- Related private tracker: #64
- [Setup installation guidance](../installation.md)
- [GitHub App registration](../github-app-registration.md)
- [Public contracts](../public-contracts.md)
- [Cloudflare Worker package](../../deploy/cloudflare/README.md)
- [ADR 0025](0025-cloud-local-feature-parity-and-conversation-reactions.md)
