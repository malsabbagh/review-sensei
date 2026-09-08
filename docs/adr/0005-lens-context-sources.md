# ADR 0005: Select bounded supplemental context per review lens

- Status: accepted
- Date: 2026-08-09
- Approved by: Maintainers via issue #10

## Context

Review categories define what a lens examines, but architectural review also
depends on repository-specific facts such as layer rules, ownership boundaries,
architecture guides, and decision records. Sending every repository file to a
model would be costly, noisy, and likely to expose unrelated or secret-bearing
content. Loading documents from the pull-request head would also let a change
rewrite the rules used to review itself.

Different lenses need different context and do not necessarily apply to every
changed path. The selection policy must stay provider-neutral, deterministic,
auditable, and bounded before prompt construction.

## Decision

Extend `ReviewCategory` with:

- `applies_to`, a non-empty list of repository-relative patterns that defaults
  to `**`;
- `context.learnings.categories` and `include_uncategorized`, which select from
  already approved, path-scoped repository learnings;
- `context.documents`, a list of repository-relative files or directories with
  include/exclude patterns and an optional `required` flag.

Resolve active lenses from the old and new changed paths, including deletions and
both sides of renames. Match repository patterns segment by segment: `*` stays
within one segment and `**` spans zero or more segments. Resolve documents only
beneath an explicit trusted target/base checkout supplied as `--context-root`;
the CLI may fall back to the already trusted `--learning-root`. It must not
implicitly scan the current working directory. Operators can intentionally
select a broad, canonical directory by declaring its explicit
repository-relative path (for example `path: "docs"`) with include patterns.
The root shorthand `path: "."` is not canonical and is rejected; root-level
files such as `AGENTS.md` must be listed explicitly.

Represent selected documents as provider-neutral `ReviewDocument` values with
repository-relative path, UTF-8 content, and SHA-256 provenance. Group selected
learnings and documents in `ReviewLensContext` and render them only for their
active lens through `{review_context}`. A stage that configures supplemental
context must include that placeholder.

Treat document contents and learnings as untrusted reference data, not model
instructions. Reject traversal, symlinks, secret-like names, unsupported text
formats, unreadable or empty files, and inputs beyond fixed per-file, file-count,
or aggregate byte limits before a provider call. Reject unknown stage, category,
context, learning-selector, and document-source fields so misspelled safety
configuration cannot silently broaden selection.

The packaged pipeline adds an architecture lens that applies repository-wide,
selects architecture and uncategorized approved learnings, and optionally reads
`AGENTS.md`, `docs/architecture.md`, Markdown files below `docs/architecture/`,
and Markdown ADRs below `docs/adr/`.

## Alternatives considered

### Scan the entire repository by default

Rejected because it sends unrelated data, increases prompt cost, creates a
secret-discovery risk, and makes the review depend on incidental repository
layout. A root scan remains an explicit operator choice through a declared
source.

### Let each provider adapter load context

Rejected because selection, containment, size limits, and provenance are review
business rules. Provider adapters should only translate a validated request to
their transport.

### Load architecture files from the pull-request head

Rejected because the change could modify its own review policy. The target/base
checkout is the trust boundary, consistent with approved repository learnings.

### Put all supplemental context in the global learnings block

Rejected because it loses lens ownership and makes unrelated stages consume the
same material. Per-lens grouping preserves intent and allows inactive lenses to
be omitted.

## Consequences

Positive:

- Architecture review can use repository-specific rules and decisions.
- Each lens receives only its configured, applicable context.
- Paths and digests make supplied documents auditable.
- Explicit trust roots and hard limits reduce prompt-injection and disclosure
  risk.
- The core remains independent of GitHub and provider SDKs.

Tradeoffs:

- Hosts must provide a trusted checkout to use document context.
- Repository operators maintain context paths as documentation moves.
- The same document may be included for more than one lens, though aggregate
  limits count unique documents once.

## Rollback

Remove `context` and `applies_to` from category files and remove
`{review_context}` from stage templates. Reviews then use all configured lenses
and the existing global approved-learning block without loading supplemental
documents. No provider or publisher interface must change.

## Related security contract

Canonical path, quoted Git path, prompt/context, and publisher-output bounds are
defined by [ADR 0007](0007-bound-untrusted-review-inputs-and-publisher-outputs.md).
This ADR remains focused on trusted target-branch context selection; it does
not loosen the shared validation seam.
