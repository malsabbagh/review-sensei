# ADR 0071 - Generate release notes from exact merged-source history

Status: Proposed
Date: 2026-10-09
Owners/Reviewers: Maintainers
GitHub Issue: None; maintainer-requested Release Marshal correction
Related PR: Pending; fixes the release prerequisite exposed after [PR #232](https://github.com/malsabbagh/review-sensei/pull/232)

## Context

Release Marshal already commits allowlisted version metadata and generated docs
directly to main using the dedicated App. Its empty Unreleased guard still
requires a separate authored-notes PR for each release, which does not match the
requested owner-dispatch workflow. That guard is a product policy choice rather
than a requirement of the App permissions or main protections.

## Decision

Keep authored Unreleased notes when supplied. Otherwise generate a deterministic
changelog entry from the exact first-parent main history between the annotated
tag for the current source version and the selected dispatch SHA. Each entry
contains a commit subject escaped as text and a full immutable commit link.
Record both range endpoints and the previous annotated tag object in the notes.
Do not infer behavior from diffs, use a provider, or execute commit text.

The previous tag must name the current version, point to source with that version,
and lie on the bounded first-parent history. Preparation verifies its exact
GitHub-verified signed object before generation and again before the non-force
App push. Missing or ambiguous ancestry, no new commits, oversized subjects or
a range above 256 commits / 256 KiB fails explicitly. Maintainers can supply
authored notes for an exceptional range. Empty Unreleased alone is routine.

Use the existing CHANGELOG allowlist entry and receipt notes digest. Recompute
automatic notes from the exact preparation parent during commit verification,
so replacing notes and updating receipt hashes cannot invent a release claim.
Prepared CI qualification rechecks the previous signed tag identity. Preserve
legacy authored-note receipts and same-version/date retry behavior.

## Scope

Release preparation helper, regression tests, owner workflow input description
and release runbook. App permissions, main protections, payload allowlist,
receipt schema, immutable versions, signing and publisher approval stay intact.

## Alternatives considered

- Require a notes PR for every release: rejects the requested direct generation.
- Generate a provider summary: introduces unnecessary egress and unverifiable
  behavioral claims. Exact commit titles and source links suffice.
- Accept free-form dispatch notes: loses their binding to selected main history.

## Consequences

The helper/workflow upgrade is reviewed and merged once. Future owner dispatches
generate notes, version metadata and docs together in the existing direct App
commit, then wait for all exact-head CI jobs before signing. No separate release
notes or generated-docs PR is required for routine releases.

Notes inventory merged commit titles rather than claiming a semantic summary
of every internal change. First-parent merge subjects avoid duplicate branch
implementation commits; linked commits retain the complete source detail.

## Validation

Exercise empty Unreleased direct-main preparation, authored-note compatibility,
same-intent retries, first-parent merges, escaped titles, exact provenance,
missing or ambiguous previous tags, forged notes with matching receipt hashes,
and previous-tag races before push. Run the full repository gates and require
terminal exact-head CI for the reviewed upgrade and generated release commit.

## Rollout and rollback

Review and merge this helper/workflow upgrade once. After its main CI passes,
the owner can dispatch Marshal for unused 0.6.18 without merging the separate
notes-only PR #233. Marshal generates the release commit and awaits exact CI
before signing; npm/PyPI approvals retain their owner boundary. Do not dispatch
duplicate releases. Roll back the helper through ordinary reviewed source
changes, preserving published versions and immutable tags.

## Follow-up work

Verify the first actual automatic release receipt and generated changelog after
integration. Pages, channel promotion and hosted runtime review reruns retain
their existing authorization boundaries.
