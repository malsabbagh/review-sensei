# ADR 0004: Structure review categories as reusable catalog entries

- Status: accepted
- Date: 2026-08-09
- Approved by: Maintainers via issue #10

## Context

Configurable review stages can choose a prompt and output sections, but a free-
form prompt alone does not give maintainers a machine-validated way to define
the review lenses or the specific concerns each lens should examine. Category
names can drift between prompts and provider responses, and a configured focus
can be silently omitted from a template.

Stage prompts also interpolate diffs, source comments, repository learnings,
and caller instructions. These values are untrusted input and must not be
rescanned as template syntax after insertion.

## Decision

Add provider-neutral `ReviewCategory` values. Each category contains:

- a stable lowercase `id` used by model-supplied comment classifications;
- a human-readable `title`;
- one or more concrete `focus` items.

Support one category per JSON file and load those files into a validated
`ReviewCategoryCatalog`. A stage can reference reusable definitions through an
ordered `category_ids` array. Unknown or duplicate references fail closed. For
compatibility and small self-contained configurations, a stage may instead use
an inline `categories` array, but it cannot combine both forms.

The stage template must include `{review_categories}` when categories are
configured. The service serializes the resolved category list as JSON and
substitutes all allowlisted template placeholders in one regex pass. Inserted
values are never interpreted as more template syntax.

When a stage declares categories, a non-null category returned on a comment
must match one of the declared ids. Category classification remains optional so
an otherwise valid finding is not rejected merely because a provider omitted
the label. Multiple stages may reuse a category id only with an identical title
and focus list, so aggregated comments retain one unambiguous vocabulary.

Stage and category configuration is trusted operator input and must be loaded
from a reviewed target-branch checkout or deployment bundle, never from the
change being reviewed. The loaders bound file counts and sizes and reject empty
directories, symlinks, duplicate names or ids, malformed categories, unknown
configuration fields or category references, and unknown placeholders before
provider execution.

## Alternatives considered

### Put one free-form focus string directly on `Stage`

Rejected because it conflates multiple review lenses, provides no stable id for
comment classification, and cannot ensure that distinct concerns are covered.

### Keep every category inline in each stage

Retained as a compatibility option, but rejected as the only representation
because definitions drift when several stages repeat the same category. Stable
catalog ids make review lenses independently reusable and validate references
without giving stage files arbitrary path-resolution behavior.

### Let stages reference category file paths directly

Rejected because it makes each stage responsible for filesystem layout and
introduces traversal, containment, and duplicate-definition behavior at every
reference. A catalog centralizes file loading and leaves stages dependent only
on stable ids.

### Infer categories from provider output

Rejected because provider output is untrusted and would let the model define
the validation vocabulary that is supposed to constrain it.

## Consequences

Positive:

- Maintainers can state both what a category is called and exactly what it must
  examine.
- Category files can be reused by multiple stages without copying focus text.
- Prompts and model classifications share stable ids.
- Invalid or silently unused category configuration fails before model usage.
- Category behavior remains independent of provider and GitHub SDKs.
- Single-pass rendering prevents placeholder-like text in a diff or instruction
  from changing another prompt section.

Tradeoffs:

- Operators provide both a category directory and a stage directory when using
  reusable category ids.
- Existing custom templates must add `{review_categories}` before declaring
  categories.
- The service validates declared labels but does not require every returned
  comment to have a category.

## Rollback

Replace `category_ids` with the equivalent inline `categories` definitions, or
remove categories and omit `{review_categories}`. The stage continues to use
its free-form prompt and existing output validation. A full code rollback also
removes `ReviewCategoryCatalog` and `ReviewCategory` without changing provider
or publisher interfaces.
