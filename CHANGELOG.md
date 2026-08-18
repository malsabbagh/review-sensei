# Changelog

## Unreleased

- Renamed the product identity from Code Sensei to ReviewSensei before public
  publication. The Python distribution is now `review-sensei`, the CLI is now
  `review-sensei`, and the import namespace is now `review_sensei`. The
  internal repository remains private; public URLs point at `reviewsensei.dev`
  and `malsabbagh/review-sensei`. Historical changelog entries and ADRs retain
  the original name for accuracy.
- Added a commit-oriented, fail-closed publication-boundary audit
  (`scripts/audit_publication.py`) that resolves an exact source commit and
  tree, applies the publication exclusion manifest, scans publishable blobs
  for credentials, private repository references, private-network endpoints,
  and unallowlisted identity metadata, and writes a deterministic redacted
  report. The publication workflow now runs the audit before dry-run and sync.
- Added an OIDC-protected GitHub App installation-token broker for issue
  [#38](https://github.com/malsabbagh/review-sensei/issues/38), with strict
  workflow policy, repository scoping, and fail-closed auditing.
- Added a narrow GitHub App JWT and installation-token authentication adapter
  for optional App-identity comments or reviews.
- Added GitHub App registration/branding, key rotation, revocation, and error
  contract documentation plus ADR 0013.

- Added community health files, issue forms, contributor onboarding, and
  maintainer governance guidance.
- Added a synthetic, privacy-scanned v1 evaluation corpus and report schema.
- Added credential-free fixture reviews and deterministic fixture evaluation.
- Added explicit live-model and remote-egress acknowledgements with offline CI.
- Added evaluation documentation, architecture boundaries, and ADR 0012.

## Unreleased

- Prepared reproducible package release metadata, archive validation, and a
  tag-driven OIDC/provenance/SBOM workflow for the first `0.1.0` publication.
- Added the provider-neutral, downward-only `ReviewLimits` contract for
  untrusted diff/request/provider data and publisher-facing results.
- Added strict NFC repository-path and Git C-quoted path validation, bounded
  one-pass diff analysis, bounded CLI/Ollama reads, sanitized failures, and
  stable first-wins exact-comment deduplication.
- Added migration and code-only rollback guidance; context root shorthand
  `path: "."` is replaced by explicit canonical sources.
- Replaced the hosted GitHub App architecture direction with an
  open-source-first product direction: Code Sensei runs in user repositories,
  primarily through GitHub Actions, without a hosted backend.
- Marked ADR 0006 as superseded for historical context.
- Added a portable immutable manual GitHub Actions workflow boundary:
  `code-sensei --version`, `code-sensei prepare-diff`, exact released package
  installation into a separate `RUNNER_TEMP` environment, bounded diff
  preparation without head checkout/execution, and a recorded installed-version
  artifact.
- Made local Ollama the default provider configuration
  (`http://127.0.0.1:11434/api`, no API key) and documented cloud provider
  egress as explicit opt-in.
- Added ADR 0011 for the portable immutable workflow decision.

## 0.1.0 - 2026-08-02

- Added repository-local learning entries and validated learning proposals for
  future GitHub draft-PR persistence.
- Added provider-neutral review domain models and validation.
- Added unified-diff changed-line parsing.
- Added Ollama local and cloud adapter.
- Added explicit provider registry for future adapters.
- Added host-independent review concurrency plans for latest-wins runs,
  per-pull-request provider serialization, and isolated non-review triggers.
- Added configurable multi-stage reviews with reusable category configuration
  files containing stable ids, titles, and focus items.
- Added a default architecture lens plus per-lens path applicability, approved
  learning selection, and bounded target-branch document context with SHA-256
  provenance.
- Added single-pass prompt rendering and fail-closed validation for empty,
  oversized, symlinked, duplicate, malformed, or unknown stage/category fields.
- Added segment-aware recursive lens globs and deletion/rename-aware changed-path
  selection so context is loaded only for applicable lenses.
- Added CLI, documentation, security policy, and CI.
