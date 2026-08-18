# ADR 0012: Privacy-safe review quality evaluation

- Status: Accepted
- Date: 2026-08-12
- Related issue: #9

## Context

Maintainers need a reproducible end-to-end example and review-quality signal
without uploading private repositories or treating model variance as a
deterministic contract failure. CI must remain credential-free and offline.

## Decision

Add a versioned synthetic CC0-1.0 corpus, closed v1 corpus/report schemas, a
bounded semantic/privacy loader, a file-backed `FixtureProvider`, and a
provider-neutral evaluator that reuses `ReviewService`. Fixture mode is the
default and performs no network or API-key lookup. Live mode requires explicit
model acknowledgement and requires a separate data-egress acknowledgement for
non-loopback endpoints before provider construction.

Reports contain stable corpus/configuration digests, privacy inventory status,
separate deterministic and quality metrics, and non-monetary performance
proxies. They never retain raw diffs, prompts, responses, credentials, or
environment values. Exact finding matching is transparent and one-to-one.

## Alternatives considered

1. A hosted evaluator or dashboard would require storage, retention, auth, and
   private-data policy that are outside the open-source-first boundary.
2. A model-graded benchmark would make regressions opaque and conflate
   contract failures with model variance.
3. An unbounded fixture directory would weaken provenance and traversal/privacy
   guarantees.

The selected local corpus and explicit provider seam preserve the existing
provider-neutral architecture and can be removed without migration.

## Security and privacy boundary

Corpus paths are canonical, contained, symlink-free, bounded, fully inventoried,
UTF-8 scanned, and subject to synthetic/provenance assertions. Secret-like
markers, private keys, cloud tokens, bearer tokens, non-reserved email
addresses, traversal, and oversized files fail closed with sanitized errors.
CI runs fixture mode only. Live acknowledgements are CLI authority, not model
output.

## Validation, rollout, and rollback

The validator, golden/negative schema fixtures, focused tests, full repository
gates, deterministic fixture report, negative authority paths, and reverse-
apply rollback check are required before publication. Rollout is a draft PR;
there is no hosted service, release, deployment, merge, or issue closure. Revert
the additive files and edits to restore the prior review CLI and contracts.
