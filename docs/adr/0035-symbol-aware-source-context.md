# ADR 0035 - Deterministic bounded symbol-aware source context

Status: Proposed
Date: 2026-09-16
GitHub Issue: [#40](https://github.com/malsabbagh/review-sensei/issues/40)
Pull Request: draft PR to be linked
Owners/Reviewers: Maintainers
Approved by: not applicable

## Context

ReviewSensei already selects bounded documents and approved learnings from a
trusted target/base checkout. Cross-file reasoning still needs related source:
enclosing functions or classes, local imports and interfaces, callers, and
associated tests. Loading the pull-request head, executing files, or walking
the repository without budgets would expand the trusted context surface and
let untrusted source rewrite the review.

Issue #40 asks for a provider-neutral selector on immutable snapshots, with
explicit provenance and visible coverage when parsing or budgets are
incomplete. Evaluation under [#33](https://github.com/malsabbagh/review-sensei/issues/33)
must demonstrate precision, recall, and usage impact on cross-file cases
before this selector can become the default.

## Decision

Keep document and learning context as the default review path. Operators opt
in to symbol-aware selection with `--enable-symbol-context` (or
`REVIEWSENSEI_ENABLE_SYMBOL_CONTEXT`) plus a trusted `--base-sha` and the
existing trusted `--context-root` / `--learning-root`.

The live review CLI and `build_review_context_selection()` call
`SymbolAwareContextSelector` only when that policy is enabled. The selector
reads the immutable trusted-base snapshot, never the untrusted head tree.
Head identity, when supplied as `--head-sha`, is coverage metadata only.

The documented language set is Python (`.py` / `.pyi`) with deterministic AST
parsing. JavaScript and TypeScript files may be admitted as a bounded
whole-file fallback and are recorded as `unsupported-language`. The selector
never executes source, imports, hooks, tests, package managers, or language
servers.

Every excerpt records repository-relative path, base commit SHA, Git blob
object id, line range, selection reason, and SHA-256 digest. Canonical path,
traversal, symlink, file-type, and secret-sensitive-file protections apply.
Identical snapshot and policy inputs produce identical excerpts and outcome
ordering. Ambiguous parses, unsupported languages, excluded paths, and
exhausted budgets appear in coverage/outcomes; exhausted budgets fail closed
when the opt-in policy is enabled.

## Scope

In scope:

- Opt-in trusted-base policy and CLI wiring
- Python import, interface, enclosing-symbol, caller, and associated-test
  relationships within configured depth and size
- Coverage on the review result without changing approval, write, or egress
  defaults

Out of scope:

- Default enablement before evaluation under #33
- Vector indexes, embeddings, execution sandboxes, or LSP plugins
- Expanding GitHub writes, approvals, or network egress

## Consequences

Positive:

- Cross-file review can use related Python source without ingesting the
  repository.
- Default reviews keep the existing document/learning trust boundary.
- Incomplete understanding is explicit for operators and evaluators.

Tradeoffs:

- Only Python has symbol analysis; other languages are fallback or
  unsupported.
- Opt-in enablement with exhausted budgets aborts the review rather than
  sending a silently truncated graph.
- Prompt contents grow when the selector is enabled.

## Alternatives considered

### Enable symbol-aware context by default

Rejected. Silently expanding trusted source would change every review and
needs evaluation under #33 first.

### Read related files from the pull-request head

Rejected. Head source is untrusted data and must not become trusted
configuration or the snapshot used for excerpts.

### Let the model request arbitrary files

Rejected. Selection must stay deterministic, bounded, and free of
model-controlled filesystem or network access.

## Validation

- Unit tests cover imports, interfaces, callers, associated tests,
  enclosing symbols, renames/deletions, malicious paths/configuration,
  graph fan-out, static parsing without execution, and both default-off and
  opt-in CLI paths.
- Coverage documents omit source text and validate against the review-result
  schema.

## Rollout and rollback

Rollout is opt-in. Operators pass `--enable-symbol-context --base-sha <sha>`
against a trusted base checkout. Rollback is code-only: omit the flag or
revert this change. No stored state or migration is created. Default
enablement is blocked until evaluation under #33 is complete.

## Follow-up work

- Run the #33 evaluation corpus against cross-file Python cases before any
  default-on proposal.
- Consider additional languages only with the same static, bounded, fail-closed
  contract.
