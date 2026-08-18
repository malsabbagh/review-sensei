# ADR 0002: Keep durable review learnings in the reviewed repository

- Status: accepted
- Date: 2026-08-02

## Context

Code Sensei needs to retain important repository-specific details so future
reviews can apply them. A hosted memory database would separate that knowledge
from the code it describes, complicate ownership and deletion, and make review
context difficult to audit. Model output also must not become future context
without maintainer approval.

## Decision

Store approved learnings as one JSON object per file under
`.github/code-sensei/learnings/` in the reviewed repository. `LearningEntry`
values are provider-neutral and may be scoped to changed paths. The caller must
load them from the target branch or target commit before constructing a
`ReviewRequest`.

The provider may return validated `LearningProposal` values in
`ReviewResult.learning_proposals`. The core does not write files, create
branches, or merge changes. A future GitHub publisher may create a draft PR in
the base repository from those proposals. Only merged, active entries are
eligible for later review context.

The review service must not load learnings from the pull-request head before
reviewing it. This prevents a change from modifying its own review context.

## Alternatives considered

### Hosted cross-repository memory

Rejected for the initial design because ownership, retention, export, and
repository-specific authorization would be separate from the codebase. A later
hosted index can still derive from merged repository entries.

### Let the model write learning files directly

Rejected because provider output is untrusted and direct writes bypass
maintainer review. The proposal/result seam keeps persistence in a publisher
adapter and makes draft-PR review explicit.

### Append all review comments as learnings

Rejected because most findings are transient, duplicate, or specific to one
change. The prompt asks for durable facts only, and maintainers choose what to
merge.

## Consequences

Positive:

- Learning history is versioned, reviewable, exportable, and removable with
  ordinary Git operations.
- Repository maintainers control what future reviews treat as durable context.
- The review engine remains independent of GitHub and model providers.
- Path scoping keeps unrelated repository knowledge out of a prompt.

Tradeoffs:

- A future GitHub publisher needs write permission to create a branch and draft
  PR, while the Ollama credential remains a separate model-provider concern.
- Repository learnings are sent to the selected provider along with the diff.
- Large or malformed learning sets must fail closed or be corrected before a
  review can use them.

## Rollback

Close or decline a learning PR before merge, or revert an already merged
learning file. The next review then uses the target branch's remaining active
entries.
