# ADR 0007 - Bound untrusted review inputs and publisher outputs

Status: Accepted
Date: 2026-08-10
GitHub Issue: #7
Pull Request: not configured
Owners/Reviewers: Maintainers
Approved by: Maintainers via issue #10

## Context

The provider-neutral review core must bound untrusted diffs, paths, provider responses, and publisher-facing results before it can be safely embedded behind a multi-tenant GitHub App.

## Decision Drivers

- Oversized or malformed untrusted input must fail before a provider call.
- A provider adapter must not fully buffer a response beyond the allowed size.
- A future publisher must receive only a structurally and quantitatively safe
  `ReviewResult`.
- The business policy must remain independent of GitHub and provider SDKs.
- Error messages must not echo private diffs, prompts, provider bodies, or
  credentials.

## Decision

Adopt one provider-neutral ReviewLimits and canonical repository-path validation seam; propagate response limits through ProviderRequest, enforce transport buffering in provider adapters, validate aggregate ReviewResult output, and keep exact duplicate comments first-wins in stable order.

## Scope

In scope:

- Canonical repository-relative validation for diff markers, review comments,
  lens documents, category patterns, and learning scopes.
- Public hard ceilings for diff, rendered prompt, provider response, and
  publisher-facing result sizes.
- Stable first-wins deduplication for exact path, line, and body comment
  duplicates across stages.
- A configuration seam that permits embedders to tighten, but not silently
  exceed, the public safe profile.

Out of scope:

- GitHub App authentication, webhooks, persistence, and publisher transport.
- Provider-specific policy in the review service.
- Automatic normalization of ambiguous or unsafe paths.

## Consequences

Positive:

- Every supported entry point shares the same path semantics.
- Diff and provider resource consumption has a deterministic upper bound.
- Direct and service-produced review results obey the same publisher contract.
- Smaller deployment-specific limits can be selected without forking business
  logic.

Negative or tradeoffs:

- Reviews above the hard ceilings must be split or rejected.
- Existing callers using dot segments, decomposed Unicode, Windows separators,
  or root shorthand `.` must migrate to canonical repository-relative paths.
- Every future provider adapter must honor `ProviderRequest.max_response_bytes`
  at its transport read boundary.

## Alternatives Considered

### Keep module-local constants and validators

- Summary: Add independent checks in the diff parser, models, stages, and Ollama
  adapter.
- Why not chosen: Duplicate policy would continue to drift and embedders would
  have no authoritative configuration seam.

### Enforce limits only in a future publisher

- Summary: Allow the review core and provider to remain unbounded, then reject
  unsafe values at GitHub publication time.
- Why not chosen: Oversized diffs and responses would already have consumed
  memory and provider capacity, and non-GitHub callers would remain unsafe.

### Normalize unsafe paths automatically

- Summary: Collapse dot segments, separators, or Unicode forms into a canonical
  path.
- Why not chosen: Rewriting changes identity and can turn attacker-controlled
  ambiguity into a different valid publisher target. The boundary fails closed
  instead.

## Implementation Notes

- `ReviewLimits` exposes these default hard ceilings: 1 MiB/50,000 lines/500
  files/5,000 hunks per diff; 4 MiB per rendered prompt; 1 MiB per provider
  response envelope; 32 KiB summary; 250 comments; 16 KiB per comment body;
  100 learning proposals; 16 KiB per serialized proposal; 256 KiB for the
  compact UTF-8 serialization of the complete proposal array, including
  brackets and separators; and 2 MiB aggregate publisher-facing output.
- Request metadata is independently bounded before prompt construction:
  repository 512 bytes, title 4 KiB, instructions 64 KiB, model 256 bytes, 64
  metadata pairs with 128-byte keys and 4 KiB values, 100 learning entries, 64
  active categories, and 64 lens contexts.
- Limit values are positive integers no greater than the public safe defaults.
  `ReviewRequest` carries the selected profile, the service propagates the
  response ceiling through `ProviderRequest`, and `ReviewResult` validates
  against the same profile.
- Repository-relative values are already-NFC UTF-8 strings using `/`, with a
  maximum of 4,096 bytes total and 255 bytes per segment. Empty, `.`, `..`,
  absolute, drive-qualified, UNC, backslash-containing, control-character, lone
  surrogate, and non-NFC values are rejected. Pattern mode permits glob tokens
  only inside otherwise canonical segments. Real Git paths may contain literal
  glob characters and are preserved without being interpreted.
- Git C-quoted diff paths are decoded as bytes using only supported escapes,
  decoded as strict UTF-8, and then passed through the same canonical validator.
  Malformed or non-UTF-8 escapes fail closed.
- Diff parsing counts and validates in one bounded pass. CLI file reads check
  file size and read at most the configured byte ceiling plus one.
- Ollama reads at most `max_response_bytes + 1` bytes before decoding,
  continuing through short reads until EOF or the ceiling is exceeded. Oversize,
  invalid encoding, transport, envelope, and output failures use stable messages
  that omit untrusted content.
- Aggregate checks run after each stage, before another provider call when the
  accumulated result is already full, and again in `ReviewResult` so direct
  construction cannot bypass the boundary.

Consumers select a profile explicitly in Python, for example
`ReviewRequest(diff=diff, limits=ReviewLimits(max_comments=40))`. Every value
is an integer count or a UTF-8 byte ceiling as named above; no environment or
provider-specific override can increase a hard default. `ProviderRequest`
exposes `max_prompt_bytes` and `max_response_bytes`, and adapters must pass the
latter to their transport read before decoding. `ReviewResult` stores the same
profile so direct construction and service aggregation use identical checks.

## Validation And Rollout

- Validation: Unit tests cover traversal and normalization variants, controls,
  invalid Unicode encodings, excessive diff dimensions, bounded response reads,
  every publisher output dimension, duplicate ordering, sanitized failures,
  and multi-stage aggregation. The full suite and source compilation must pass.
- Rollout: This is a library contract change. Release notes and README/security
  docs publish the limits before any hosted GitHub App depends on them.
- Migration: Replace unsafe path forms with canonical NFC `/`-separated paths;
  replace a context source `path: "."` with explicit files or directories; split
  reviews that exceed a hard ceiling. No stored data migration is required.
- Rollback: Revert the limits/validation module and its integrations, restore the
  previous constructors and parser, and restore the prior Ollama unbounded read.
  No persistent data rollback is required.

## Follow-Up

- Require future provider and publisher adapters to demonstrate the same
  request/response limit contract in their adapter tests.

## Links

- Related issue: #7
- Related PR: not configured
- Supersedes: none
- Superseded by: none
