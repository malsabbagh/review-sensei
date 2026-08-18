# ADR 0016 - Product Identity Rename to ReviewSensei

Status: Proposed
Date: 2026-08-16
GitHub Issue: #44
Pull Request: not configured
Owners/Reviewers: Maintainers
Approved by: not configured

## Context

The project was developed under the internal name "Code Sensei" in a private
repository. Before the first public publication, the product identity needs to
be renamed to "ReviewSensei" so that the public repository
(`malsabbagh/review-sensei`), PyPI package (`review-sensei`), CLI
(`review-sensei`), import namespace (`review_sensei`), and canonical site
(`https://reviewsensei.dev`) all carry the intended public brand. The internal
repository slug remains private; the releasable source tree uses ReviewSensei
so publication does not require a second branding transform.

## Decision

Rename the user-visible product identity, Python distribution, CLI entry
point, import namespace, schema `$id` URLs, env var prefix, concurrency key
prefix, learnings directory, generated workflow names, and all documentation
from Code Sensei to ReviewSensei. Historical changelog entries and prior ADRs
retain the original name for accuracy. The private upstream slug is preserved
where unavoidable.

## Consequences

- The Python package becomes `review-sensei`; users install with
  `pip install review-sensei`.
- The CLI command becomes `review-sensei`.
- The import namespace becomes `review_sensei`; the error class becomes
  `ReviewSenseiError`.
- Schema `$id` URLs point at `https://reviewsensei.dev/schemas/v1/...`.
- Env var prefix becomes `REVIEWSENSEI_*`.
- The learnings directory becomes `.github/review-sensei/learnings`.
- Concurrency key prefix becomes `review-sensei:`.
- Existing tests, fixtures, and validation scripts are updated to match.
- Prior ADRs (0001-0015) and historical changelog entries retain the original
  name as immutable records.
