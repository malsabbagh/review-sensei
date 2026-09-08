# ADR 0029 - Structured finding classification and lens presentation

Status: Proposed
Date: 2026-09-07
GitHub Issue: not configured
Pull Request: draft PR to be linked
Owners/Reviewers: Maintainers
Approved by: not configured

## Context

ReviewSensei findings currently expose optional free-form `severity` and
category/lens identifiers, but the GitHub publisher renders only the finding
body. Providers can therefore mix impact labels such as `bug` and
`suggestion`, and users cannot quickly distinguish impact, remediation scope, or
which configured lens produced a finding.

Review comments are provider-neutral public results consumed by multiple
boundaries. The v1 schema compatibility policy allows additive optional fields,
while narrowing the existing free-form severity contract would be breaking.

## Decision drivers

- Keep impact and remediation scope independent and understandable.
- Preserve the existing category identifier as the single lens source of truth.
- Render the same metadata on inline comments and the aggregate review summary.
- Keep the review core independent of GitHub and provider SDKs.
- Preserve v1 compatibility and provide a code-only rollback.

## Decision

Add an optional `fix_effort` field to `ReviewComment` and the v1 review-comment
and review-result schemas. New default review prompts use these preferred
values:

- `severity`: `critical`, `high`, `medium`, or `low`.
- `fix_effort`: `trivial`, `small`, `moderate`, `large`, or `unknown`.
- `category`: the existing configured lens id.

Severity describes likely impact if the issue is shipped. Fix effort describes
remediation scope, not a time estimate. A single composite priority score is
not emitted. Existing arbitrary v1 severity strings remain readable for
compatibility and are not silently mapped to a canonical impact level.

The provider-neutral presentation layer formats available metadata on each
inline body and derives deterministic counts for the top-level summary. Lens
labels are derived from the stable category id at publication time; the model
does not provide a second lens title or provenance value. Classification values
are bounded printable provider text, escaped as Markdown labels, and the
publisher validates the formatted summary and inline bodies against their
configured output ceilings. The complete framed GitHub review body is also
checked against a 65,536-byte transport ceiling before any write.

## Consequences

Positive:

- Maintainers can triage by impact and remediation scope independently.
- Summary counts cannot drift from the validated comment set.
- Existing category/lens selection, context ownership, and publisher identity
  boundaries remain unchanged.
- Legacy v1 result files continue to parse and publish.

Tradeoffs:

- A permissive legacy severity value can still appear until a future breaking
  schema version tightens the enum.
- Publisher-time labels derived from custom category ids may be less polished
  than titles from the original category catalog.
- Summary-only findings remain prose in this first slice; first-class
  unlocated findings would require a later result-model extension.

## Alternatives considered

### One priority score

Rejected because it hides whether a finding is urgent or merely easy to fix.

### Free-form Markdown labels only

Rejected because providers would produce inconsistent labels and summaries could
not be counted or validated deterministically.

### Add a separate `lens` field

Rejected because `category` is already the established lens identifier. A second
field would create conflicting sources of truth.

## Validation, rollout, and rollback

Unit, schema, service, presentation, and GitHub publisher tests cover canonical
classification, legacy compatibility, adversarial label input, clean reviews,
summary aggregation, output ceilings, and unchanged exact-head publication
behavior. The default prompt and bundled examples are updated to use canonical
values. Rollback is code-only: revert the formatter, optional field, prompt/docs,
and tests; no stored review-data migration is required.
