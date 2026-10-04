# ADR 0058: Rank and budget supplemental documents across active lenses

- Status: Proposed
- Date: 2026-10-04
- Decision owner/reviewers: ReviewSensei maintainers
- Linked GitHub issue: Pending maintainer triage of the requested document-selection improvement
- Linked PR: [#204](https://github.com/malsabbagh/review-sensei/pull/204)

## Context

ADR 0005's broad optional architecture sources can discover more than the
64-document limit before any document reaches a provider. Alphabetical clipping
or raising the limit would send noise and could discard relevant guidance.
Model-generated summaries introduce cost, latency, nondeterminism, and the risk
of invented or weakened requirements.

## Decision

Resolve declared sources for active lenses, deduplicate paths, inspect each
unique document once, and reserve every present explicit-file source and every
required-source match in full. Missing optional sources remain optional.
Applicable ancestor `AGENTS.md` guidance is also reserved when discovered inside
declared sources. Do not discover extra guidance outside those sources.
Mandatory count, per-file, or byte overflow fails before any provider request.

Rank optional directory documents deterministically using each owning lens's
applicable changed paths (including old/new rename and deletion paths supplied
by the CLI): exact path references first, then changed file-stem overlap in the
document filename, first Markdown heading, and body. Use the maximum owning
lens score for global admission and canonical paths only to break ties. Retain
fitting configured foundational documents even when their lexical score is zero.
Lenses receive only documents from their own configured sources. Preserve
approved learning selectors and explicit review instructions independently.

Keep the existing 64 unique document, 128 KiB supplied-document, and 512 KiB
unique-content limits. Separately bound discovery to 4,096 candidate documents
and 16,384 visited entries per source collection; bound inspection to 1 MiB per
optional source and 16 MiB aggregate. Discovery or aggregate-inspection overflow
fails with a request to narrow sources, never an arbitrary discovery prefix.
Containment, symlink, secret-name, unsupported-format, and permission rules apply
before ranking. Read content using bounded root-relative reads.

When a relevant optional document cannot fit the remaining bytes or per-file
limit, inspect at most 65,536 source lines and select at most eight complete line windows, with two surrounding lines,
up to 16 KiB total. Keep the exact source text. Never cut a line or paraphrase a
requirement. If no window fits, omit the document with `extract-unavailable`.
Optional sources above the inspection cap, unreadable, invalid UTF-8, or empty
are explicitly omitted. Fitting originals need no extraction. Required/explicit
files cannot fall back to extracts. This is extractive summarization, with zero
extra requests and zero additional provider cost.

Per-lens metadata distinguishes discovered, selected (full plus excerpted),
summarized (verbatim excerpts), and omitted documents. Selected payloads keep
their existing content SHA-256; provenance separately records source SHA-256,
source bytes, and exact source line ranges. Report policy/method, loss warning,
omission reasons, at most sixteen omission samples, and the full inventory's
digest in prompts. The optional local `--context-selection-output` sidecar
contains the complete bounded metadata inventory without document contents.
Stderr reports loss counts. Cache identity includes the report, so omission or
provenance changes invalidate prior context state.

Bare or quoted canonical path references are recognized in one text pass;
scoring and extraction do not scan every line once per changed file. Source
byte/digest fields remain unknown (`null`/empty) when content could not be read.

## Scope

Shared Python document selection used by local CLI and reusable hosted workflow
execution. Leave direct `RepositoryContextStore.for_sources()` strict. No new
provider adapters, custom-category migration, implicit scans, authorization,
publication, review-coverage schema, or approval-policy changes.

## Consequences

Large optional catalogs reach review with useful bounded context. Guidance and
explicit files remain complete. Lexical ranking is approximate: generic rules
may lack path terms, and omitted text may contain relevant requirements. The
metadata explicitly prevents treating selection or extracts as complete review
coverage. Operators can pin important files or use required/narrow sources.
Full-path inventories and digests are retained only in memory or the explicitly
requested local sidecar; no new persistence or model requests are introduced.

## Alternatives considered

- Raise limits: rejected because noise, exposure, and request size still grow.
- Alphabetical clipping: rejected because ordering is unrelated to scope.
- Generated model summaries: deferred; additional calls and semantic loss are
  unnecessary for the first bounded fix and cannot guarantee requirements.
- Always drop zero-score documents: rejected to retain fitting foundational
  guidance; relevance determines priority when budgets bind.

## Validation

Synthetic tests cover more than 64 documents, relevant documents behind noise,
mandatory overflow, duplicate paths and lens ownership, oversized documents,
exact excerpt provenance, failed extraction, global bytes, determinism, cache
identity, discovery/inspection exhaustion, containment, and the packaged default
CLI path. Run focused tests and the CONTRIBUTING aggregate before proposing PR.

## Rollout and rollback

Review and accept the candidate before publication. A Python engine change
requires a new package version; moving `v5` while it declares an already
published version still installs that existing engine. Publish and verify the
new CLI artifacts, then separately move the reviewed workflow channel. No
Worker deployment or App permission change is required by this selection policy.
Rollback to the prior engine/tag restores strict overflow behavior. No stored
state migration is needed.

## Follow-up work

Maintainers must link the issue/PR and decide whether to accept this policy.
Broader semantic ranking or generated summarization needs separate evidence,
egress authorization, cost bounds, and validation of semantic fidelity.
