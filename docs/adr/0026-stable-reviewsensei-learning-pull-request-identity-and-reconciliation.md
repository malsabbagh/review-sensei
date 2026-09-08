# ADR 0026 - Stable ReviewSensei learning pull-request identity and reconciliation

Status: Proposed
Date: 2026-09-02
GitHub Issue: #97
Pull Request: not configured
Owners/Reviewers: Maintainers
Approved by: not applicable

## Context

The learning publisher currently keys branches and open-PR lookup by proposal-batch digest, so repeated validated runs for one source pull request fan out into multiple draft pull requests. The publisher must retain one safe, descriptive open draft while allowing a new generation after merge.

## Decision Drivers

- Preserve one reviewable draft per source pull request while it is open.
- Keep proposal files content-addressed and deduplicated without trusting branch
  names or mutable PR text alone.
- Refresh from the latest default-branch head without force-moving refs or
  overwriting manual edits.
- Make merge, closed-unmerged, deleted, and concurrent states explicit and
  recoverable.
- Keep source, provider, diff, credential, and private review data out of PR
  metadata.

## Decision

Use a stable source-pull-request identity marker and canonical open-branch identity. Before writes, reread the source PR and default-branch head, prove that any matching draft branch/PR is a same-repository ReviewSensei artifact, parse and validate its generated learning files, union/deduplicate proposals by full canonical proposal digest, and refresh through a non-force Git data commit rooted in the latest default branch. Treat merged generations as closed history and create a fresh deterministic generation; treat closed-unmerged, deleted, tampered, or ambiguous artifacts as explicit fail-closed outcomes.

The v2 marker is the exact final non-empty body line
`<!-- reviewsensei:learning:v2 repo=<decimal-id> pr=<decimal-pr> batch=<64 lowercase hex> head=<40 lowercase hex> -->`.
The stable lookup key is `(repository id, source PR number)`; the batch digest
is computed only after full-digest deduplication and latest-base filtering.
The preferred branch is exactly `review-sensei/learnings/pr-<pr>`. If a merged
generation still occupies it, the publisher tries
`review-sensei/learnings/pr-<pr>-g-<batch[:16]>` and then suffixes `-2` through
`-10`, never force-updating an unproven ref. Legacy v1 digest branches are
recognized only after an exact marker, branch suffix, single-parent commit, and
canonical file/tree proof; a proven open v1 draft is upgraded in place.

The caller base (`C`), source PR base (`S`), and latest default ref (`R`) are
compared explicitly. `S == C == R`, `S == C != R`, and `S == R != C` all use
the freshly read `R`; when `S` disagrees with both snapshots the publisher
returns a stale-base no-write result. A new commit has parent `[R]`; a refresh
has `[H]`, or `[H, R]` when the default advanced, and every ref update uses
`force: false`. Commit provenance records repository/PR, `R`, batch, source
head, and SHA-256 hashes of the exact title and body. Title/body metadata is
bounded (256 UTF-8 bytes for the title and 65,536 for the body), normalized and
Markdown-escaped, and contains only the source link, one sanitized `Source
title: ...` line, reviewed head, proposal summaries, approval warning, and
marker. The source-title line is part of the prior canonical rendering proof,
so a source-title transition can be distinguished from a title-only maintainer
edit. A transition is accepted only when a bounded authoritative GitHub issue
timeline proves a rename chain from the prior rendered title to the current
source title; the rename events must have strictly chronological `created_at`
values after the candidate draft's immutable `created_at` boundary. Historical
events before that boundary, mutable PR text, and recomputed hashes cannot
authenticate themselves.

## Scope

In scope:

- Stable repository/source-pull-request identity markers and canonical branch
  selection for generated learning PRs.
- Default-branch rereads, branch-content validation, canonical proposal union,
  non-force refresh commits, and descriptive draft PR title/body generation.
- Deterministic reconciliation for merged generations and fail-closed handling
  of closed-unmerged, deleted, tampered, and ambiguous artifacts.
- Scripted GitHub HTTP fixture coverage and public contract documentation.

Out of scope:

- Changes to provider-neutral review logic, provider adapters, or proposal
  validation semantics.
- Automatic merging, closing, deleting, or force-updating GitHub branches or
  pull requests.
- Persisting raw provider output, prompts, diffs, credentials, or review data.

## Consequences

Positive:

- Repeated validated runs converge on one understandable open draft.
- Merged learning files are naturally reconciled against the latest default
  branch before a new generation is opened.
- Maintainers can review proposal meaning, provenance, and scope from the PR
  body without opening generated JSON files.
- Unsafe or ambiguous repository state is surfaced instead of overwritten.

Negative or tradeoffs:

- The publisher performs additional bounded GitHub reads and may need to fail
  closed when a maintainer manually edits generated artifacts.
- A closed-unmerged or deleted-unproven artifact requires maintainer recovery
  rather than silently creating another PR. A deleted branch is retired only
  after its merged PR and immutable generated commit have been proven
  canonical; that state permits a fresh generation.
- Existing v1 digest-branch PRs require conservative recognition and marker
  migration; unproven legacy state remains untouched.

## Alternatives Considered

### Keep one branch per proposal batch

- Summary: Continue using the batch digest in the branch and create a new PR for
  every changed batch.
- Why not chosen: It is the current fan-out behavior and cannot provide one
  open draft per source pull request.

### Patch an open branch through file-content endpoints

- Summary: Discover an open PR by a stable marker and update individual files
  with the Contents API.
- Why not chosen: It cannot prove the complete generated tree or safely remove
  stale proposals, and it makes latest-base reconciliation and concurrent
  non-force updates harder to reason about.

### Delete/recreate the generated branch

- Summary: Recreate a branch from the latest default branch whenever proposals
  change.
- Why not chosen: Deletion or force recreation can destroy manual history and
  would weaken the fail-closed ownership boundary.

## Implementation Notes

- New artifacts carry a v2 marker binding repository id, source PR number,
  current proposal-set digest, and latest reviewed source head. The open-PR
  search accepts the stable identity portion while refresh validates the whole
  marker and generated tree.
- New generations use a deterministic source-PR branch when available; a
  merged branch that still exists receives a deterministic generation suffix.
  Branch updates always use `force: false` and an expected current head.
- Generated bodies contain only a bounded source link/title line, reviewed
  head, proposal summaries (title, rule, rationale/category when present,
  scope), approval warning, and machine markers. The source-title line must
  match the descriptive PR-title envelope before a refresh may overwrite it;
  if it differs from the current source title, the bounded issue timeline must
  prove the old-to-current rename chain with strictly chronological event
  timestamps after the candidate creation boundary.
- Closed-unmerged, deleted, tampered, fork-owned, non-draft, and ambiguous
  matches return sanitized explicit errors/statuses; no automatic close, merge,
  delete, or force update occurs.
- If a process stops after a final commit or ref mutation, the next bounded
  invocation may reuse only an exact canonical orphan or an open PR whose old
  title/body hashes prove the pre-update metadata. Unexpected content, branch
  heads, markers, or pagination remains a conflict and is never overwritten.
- Candidate state is reread after open/history discovery and immediately before
  Git-object/ref mutation; a close or merge observed during that revalidation
  returns a no-write lifecycle result.

## Validation And Rollout

- Validation: Unit tests use scripted GitHub HTTP fixtures for first creation,
  identical and changed retries, concurrent races, open/merged/closed/deleted
  lifecycle states, latest-base advancement, existing identical files, title
  bounds, and tampering. Run the repository full validation sequence.
- Rollout: Merge through the normal draft PR review path; the public publisher
  contract changes with the package snapshot. Existing open v1 artifacts are
  migrated only when their ownership and generated content are proven.
- Rollback: Revert the publisher/docs/ADR change through a normal PR. No
  default-branch data migration or destructive branch cleanup is required;
  existing generated PRs remain reviewable history.

## Follow-Up

- Document operator recovery for an explicitly closed or deleted generated PR
  before enabling automated retries against that state.

## Links

- Related issue: #97
- Related PR: not configured
- Supersedes: none
- Superseded by: none
