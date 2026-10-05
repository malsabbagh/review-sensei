# ADR 0059 - Dependency review and partial publication

Status: Proposed
Date: 2026-10-05
Owners/Reviewers: Maintainers
Approved by: not applicable
GitHub Issue: none; reproduces [PR #203](https://github.com/malsabbagh/review-sensei/pull/203)
Pull Request: pending

## Context

PR #203's hosted run 37244564731 failed during analysis with
`identity-bound analysis produced a partial review that cannot be checkpointed or published`.
Publication was skipped. A separate configured local review reproduced one
reviewed manifest hunk and thirteen lockfile hunks excluded as generated.
The hosted run retained no review artifact, so its exact exclusion details are
not inferred from the failure message alone.

Generated lockfiles contain material dependency changes. Excluding them is
truthful incomplete coverage, but prevents useful dependency review. ADR 0044
retains validated findings from partial work; ADR 0052's complete-only
checkpoint guard prevents that work from reaching the identity-bound publisher.

## Decision

Review every original npm `package-lock.json` and `npm-shrinkwrap.json` hunk.
Append optional bounded visible JSON member evidence tied to the record hash,
supplied-diff hunk index, original paths, side and line. Npm's
[format documentation](https://docs.npmjs.com/cli/v11/configuring-npm/package-lock-json/)
identifies version, dependency, resolution, integrity and install-script metadata;
the annotation is only a source aid. It cannot reconstruct a full package graph,
pair unrelated old/new members, prove the owning package or lock version, or
certify registry bytes. Compact, invalid, or unrecognized JSON remains raw
review input. Annotation truncation never truncates the raw patch or creates
coverage. Other recognized lock formats are explicitly unsupported; generated
non-lockfiles retain their policy exclusions.

Permit a validated `partial` result to checkpoint and publish only with explicit
complete enumeration and evidence of reviewed work. Keep exact identity,
configuration, digest and generation fences. Reject missing coverage,
incomplete enumeration, summary-only results and wholly failed analysis.
Legacy complete artifacts without coverage retain checkpoint/publication
compatibility; unknown coverage withholds approval and projects a partial
publication outcome.
Charge a partial checkpoint as one failed attempt; preserve prior complete
baseline/counters and omit completed convergence/no-progress markers. Retry
publication from the same checkpoint without another provider call or charge.

Decide coverage completeness before lifecycle resolution and complete-pass
caching. Publication delivery does not upgrade `partial` to `reviewed`.
Filtered or summary-only pipelines that execute no comment stage receive
`unsupported` / `no-review-stage` coverage; chunk aggregation never relabels
that lack of work as validated partial evidence.
Approval stays withheld; enforcement checks remain `action_required`, while
advisory policy keeps its documented neutral check. Default review exit `2`
and operational workflow semantics remain distinct.

## Scope

Includes planning, evidence prompts, coverage reason, transaction admission,
publication outcomes and synthetic regression tests. Excludes package audit,
registry access, lockfile graph reconstruction, additional format parsers,
release/tag promotion, Worker deployment, App permissions, and PR #203 changes.
Existing publisher input/location bounds remain in place.

## Consequences

Dependency changes receive review without a summary claiming unchecked source
was covered. Useful partial findings can reach users while remaining visibly
partial. Raw locks use more request budget; an oversized hunk, exhausted budget,
unsupported format or failed provider remains explicitly unreviewed. Partial
attempts cannot establish clean convergence, erase the last complete baseline,
or consume completed-round counters. No raw prompts or provider responses are
added to durable session state.

## Alternatives considered

- Skip locks or mark exclusions reviewed: hides material unchecked changes.
- Replace all hunks with a flattened graph: risks losing unknown metadata and
  requires authoritative before/after snapshots and format-specific contracts.
- Publish all partial/incomplete documents: lacks evidence that any material
  was validated and weakens recovery admission.
- Treat publication success as review success: would permit false approval.

## Validation

Synthetic npm v2/v3 fixtures reproduce fourteen total hunks, mixed generated
and unsupported changes, source-bound evidence, rename/deletion handling,
annotation limits, provider/chunk failure and complete-cache protection.
Transaction fixtures cover safe partial checkpoint/publication/recovery,
failure rejection, preserved prior baseline, current-head identity, distinct
operational/review exits, partial outcomes and withheld approval/check status.
Run repository source, coverage, schema, build and clean-wheel gates.

## Rollout and rollback

Ship through the ordinary reviewed package/workflow release process. This PR
does not move any runtime channel. Existing checkpoints retain their schemas.
Rolling back the partial admission change fails closed for pending partial
results; preserve artifacts and regenerate under an appropriate reviewed
runtime. Coverage reason additions require matching runtime/schema versions.

## Follow-up work

Additional lock formats or complete semantic graph summaries need separate
parser/snapshot contracts and regression evidence. Live hosted behavior needs
validation after this code reaches the approved release channel; a draft PR's
skipped hosted review is not evidence of live publication or approval.
