# ADR 0039 - Verify candidate findings before publication

Status: Proposed
Date: 2026-09-15
GitHub Issue: #37
Pull Request: draft PR to be linked
Owners/Reviewers: Maintainers
Approved by: not configured

## Context

Stage aggregation is not independent verification. A provider can emit an
inline comment that looks publishable while citing missing, mismatched, or
out-of-snapshot evidence. Publishing that comment as a finding creates
unsupported review noise and can make an incomplete pass look clean enough to
approve.

The library already has a provider-neutral `CandidateFinding` contract and
deterministic `verify_candidate` / `verify_candidates` helpers. Those helpers
were not on the publication path, so unverified comments could still be
written as GitHub findings.

## Decision

Keep a bounded candidate-finding contract (claim, triggering conditions,
impacted path, evidence locations, assumptions, and severity rationale) and
require a deterministic verifier pass before a publisher may treat a candidate
as a finding.

`prepare_publishable_review` is the publication gate:

- `legacy` is the compatible single-pass mode. Existing comments publish
  unchanged and are identified by `evidence_policy="legacy"`.
- `confirmed` publishes only candidates whose evidence exists in the exact
  reviewed snapshot, with matching paths, in-range lines, and excerpt
  substrings. Rejected, duplicate, malformed, and insufficient-evidence
  candidates are unpublished.
- Incomplete verification downgrades `complete` / `summary-only` coverage to
  `partial` and records a deterministic coverage note. It is never a clean
  review.
- Candidate text, excerpts, and snapshot contents are data only. They cannot
  bypass validation or expand write, tool, or egress permissions.
- Published confirmed findings render the claim, triggering conditions,
  severity rationale, and bounded evidence locations. They do not include
  hidden reasoning transcripts or raw model responses.

GitHub `ReviewPublisher` and `GitHubApplication.publish_review` call this gate
before location validation and any review POST. Automatic approval is not
expanded: an incompletely verified `confirmed` review cannot be approved
because issue #31 already rejects non-complete results.

## Scope

In scope:

- Candidate and verification-result schemas, Python exports, and documentation.
- Publication-time filtering of unverified candidates.
- Tests for true defects, false positives, missing context, conflicting
  evidence, malformed output, duplicates, legacy mode, and approval blocking.

Out of scope:

- Optional second-model verification.
- Changing write permissions, cloud egress, trusted context, or default
  automatic approval.
- Evaluation thresholds from issue #33 and per-pass/run budget enforcement
  from issue #36.
- Expanding staged provider output to emit candidates automatically.

## Alternatives considered

### Treat multi-stage aggregation as verification

Rejected because later stages can repeat or decorate earlier claims without
checking evidence.

### Require a second model to confirm every finding

Rejected as the first slice. Model agreement is not proof; deterministic
snapshot checks come first.

### Delete legacy single-pass comments in this change

Rejected to preserve compatible publication. Legacy mode remains default and
is identified as `legacy`.

### Auto-enable `confirmed` whenever candidates are supplied

Rejected because it would change default GitHub publication behavior without
an explicit policy.

## Consequences

Positive:

- Unsupported candidates cannot be published as findings under `confirmed`.
- Incomplete verification is visible and cannot approve.
- Legacy single-pass installations keep their current comment publication.

Tradeoffs:

- `confirmed` requires an explicit snapshot and digest from the orchestrator.
- Confirmed findings do not yet carry independent blocking/severity enums;
  those remain optional comment classification fields.
- Production promotion still needs evaluation (#33) and budget (#36) evidence.

## Validation

Unit tests cover candidate schema round-trips, true defects, plausible false
positives, missing context, conflicting evidence, malformed output,
duplicates, untrusted candidate text, GitHub publication filtering, and
approval rejection for incomplete verification. Rollback is code-only: revert
the package and publishers resume legacy comments.

## Rollout and rollback

Default evidence policy remains `legacy`. Orchestrators opt into `confirmed`
by passing candidates, the reviewed snapshot, and its digest. Reverting the
package restores prior publication behavior with no stored-data migration.

## Follow-up work

- Optional model verification after deterministic checks.
- Evaluation comparison against issue #33 thresholds before promoting
  `confirmed` as a production default.
- Enforce issue #36 run/budget limits on the verification pass.
- Consider teaching review stages to emit candidate findings directly.
