# ADR 0025 - Cloud/local feature parity and conversation reactions

Status: Proposed
Date: 2026-09-01
GitHub Issue: #64
Pull Request: not configured
Owners/Reviewers: Maintainers
Approved by: not configured

## Context

Setup-v4 originally divided features by provider: automatic review ran only in
cloud mode on GitHub-hosted compute, while manual review and `@sensei` replies
ran only in local mode on a labelled self-hosted runner. That split caused valid
comment events to create workflow runs whose jobs were skipped, prevented local
automatic review, and prevented cloud conversations even though the provider-
neutral review and conversation services already support both endpoints.

Users also need visible acknowledgement that an authorized conversation turn is
being processed and a natural way to continue discussing a review finding in
the same GitHub thread.

## Decision Drivers

- Provider choice must change compute and data egress, not product capability.
- Trigger authorization, exact-head checks, output validation, and least-
  privilege capability tokens must remain fail-closed.
- Existing setup-v4 installations must migrate through a reviewable PR without
  overwriting customized workflows.
- Processing state must not remain stale after a reply or failed turn.

## Decision

The generated setup-v4 caller uses one provider-neutral reusable-workflow job.
It passes an explicit `provider_mode` plus an operation (`review` or `reply`).
The reusable workflow selects one of two runtime jobs:

- `cloud` runs on GitHub-hosted compute and uses Ollama Cloud.
- `local` runs on `[self-hosted, linux, x64, ollama]` and uses loopback Ollama.

Both jobs support automatic and manual review, validated review publication,
learning draft PRs, optional review artifacts, and generated mention replies.
Automatic fork review remains disabled. Older callers that omit
`provider_mode` retain their historical automatic-cloud/manual-local mapping,
so moving the protected `v4` tag does not break an installed caller before its
migration PR is merged.

The OIDC broker accepts the supported events on either GitHub-hosted or self-
hosted runners and rejects unknown runner environments. Repository identity,
fork state, workflow ref/SHA, replay, rate, installation, and disjoint
capability checks remain unchanged.

After a mention and its bounded thread context are fully authorized, and a
reply capability token is issued, ReviewSensei adds the GitHub `eyes` reaction
to the source comment. It then invokes the selected provider, validates and
publishes the reply, and removes that exact App-authored reaction in a `finally`
cleanup. Cleanup also runs for provider, validation, reconciliation, and
publication failures. A cleanup failure fails the workflow so a retry can
reconcile the marker and remove the reaction.

Each new standalone, case-insensitive `@sensei` mention by an OWNER, MEMBER, or
COLLABORATOR is a new conversation turn. Inline replies retain their resolved
root thread; PR-level comments use the bounded PR conversation. Both include
the current exact head, bounded diff context, prior App findings, trusted-base
learnings, and at most the configured number of thread messages.

## Scope

In scope:

- Setup-v4 caller generation and managed migration recognition in Python and
  the Worker.
- Cloud/local review, reply, learning, and artifact parity.
- `eyes` reaction creation and deletion around generated replies.
- Documentation and ReviewSensei.dev feature descriptions.

Out of scope:

- Automatic review of fork pull requests.
- Arbitrary provider URLs or new model-provider adapters.
- Persisted chat sessions outside GitHub comments.
- Worker deployment, public tag movement, App permission acceptance, or
  customer setup-PR merge as part of this code change.

## Consequences

Positive:

- Operators can select cloud or local without losing features.
- Comment-triggered runs no longer skip solely because cloud mode is selected.
- Users see an immediate, temporary processing acknowledgement and can continue
  a bounded discussion in the same thread.
- One caller authorization expression reduces cloud/local drift.

Negative or tradeoffs:

- The public reusable workflow still duplicates runtime steps between cloud and
  local jobs because GitHub Actions cannot dynamically select `runs-on` from an
  untrusted event without weakening the boundary.
- Cloud conversations intentionally send bounded comment and review context to
  Ollama Cloud; operators must treat cloud mode as explicit egress consent.
- Reaction cleanup adds two GitHub API writes to each processed turn.

## Alternatives Considered

### Four caller jobs for cloud/local review/reply

- Summary: Generate separate cloud-review, cloud-reply, local-review, and local-
  reply reusable-workflow calls.
- Why not chosen: It duplicates trigger authorization and input mapping four
  times, which recreates the drift that caused the feature mismatch.

### One job with a dynamically selected runner

- Summary: Use one reusable job and choose `runs-on` from `provider_mode`.
- Why not chosen: Runner selection and provider secrets would share one larger
  expression surface, and self-hosted runner labels cannot be represented as a
  safe scalar equivalent of `ubuntu-latest` without extra indirection.

### Post a temporary comment instead of a reaction

- Summary: Publish a “working” comment and delete or edit it later.
- Why not chosen: It creates more conversation noise, needs broader comment
  reconciliation, and can leave a misleading durable artifact after crashes.

## Implementation Notes

- The protected `v4` tag remains the only public setup-v4 update channel.
- The previous two-job setup-v4 caller is reconstructed byte-for-byte for
  managed migration recognition; customized or future files remain no-write.
- `inline_reply` and `issue_reply` tokens already carry the least privilege
  needed to create/delete reactions and publish the corresponding reply.
- Reply markers continue to bind source comment, source update digest, PR, and
  exact head SHA.

## Validation And Rollout

- Validation: Python publisher/application tests cover reaction endpoints,
  ordering, and failure cleanup; workflow policy tests cover the symmetric
  operation matrix; Worker tests cover both runner environments and setup
  migration; the normal full validation sequence remains required.
- Rollout: Merge and publish an audited public snapshot, deploy the Worker, move
  protected `v4` to the public commit, trigger a fresh installation lifecycle
  delivery, review/merge the generated setup migration PR, then verify one
  cloud and one local review plus multi-turn inline and PR-level replies.
- Rollback: Move `v4` back to the previous public workflow commit and redeploy
  the previous Worker. Do not force-update customer branches; close or revert
  unmerged setup PRs through normal review.

## Follow-Up

- Record hosted acceptance evidence for cloud/local review, reaction cleanup,
  and at least two consecutive conversation turns after rollout authorization.

## Links

- Related issue: #64
- Related PR: not configured
- Supersedes: provider-specific feature restrictions in ADR 0022
- Superseded by: not configured
