# ADR 0021 - Versioned GitHub App Setup Migrations

Status: Proposed
Date: 2026-08-18
GitHub Issue: #37
Pull Request: not configured
Owners/Reviewers: Maintainers
Approved by: not configured

## Context

The GitHub App setup bootstrap writes generated workflow and configuration
files through a reviewable pull request. Existing installations can receive a
new Worker or package version after an earlier setup pull request has already
been merged. Requiring operators to uninstall and manually remove those files
would lose the safe, reviewable update path and can leave stale setup branches.

The bootstrap must distinguish its own older files from a repository-owned
custom workflow. It must also avoid reading unbounded repository content or
overwriting a future setup version that this Worker does not understand.

## Decision

Generated setup files carry a comment marker, `ReviewSensei setup version: 2`.
On a setup-triggering installation delivery, the bootstrap reads only these
three paths from the repository default branch:

- `.github/workflows/review-sensei-review.yml`
- `.github/workflows/review-sensei-uninstall.yml`
- `.github/review-sensei/config.yml`

Each file is bounded to 128 KiB, decoded as strict UTF-8, and inspected in
memory. The bootstrap classifies the known paths as:

- **absent**: no known files exist; create the initial setup PR;
- **current**: all three files have the current marker; do nothing;
- **migration**: an older generated file (including the pre-marker legacy
  format) or a partial current setup is present; refresh the setup branch from
  the current default branch and open a migration PR;
- **unknown**: a custom file, malformed marker, or future version is present;
  skip without writing so an operator can review it manually.

The migration branch is always based on the current default branch. Its tree
changes only the generated paths, so repository learnings and all other files
remain untouched. Existing repository variable values are still preserved and
secrets are never read or created. An existing open setup PR remains the
idempotency boundary and is reported without creating a second PR.

## Consequences

Positive:

- Existing installations can be upgraded by merging one migration PR; no
  uninstall/reinstall cycle is required.
- Future or customized setup files fail closed instead of being overwritten.
- File reads and generated writes stay bounded, reviewable, and secret-free.

Tradeoffs:

- A new generated-file contract requires a version marker and a deliberate
  migration decision for each future version.
- The setup token needs Contents read access in addition to the existing setup
  write permissions so the Worker can inspect the default branch.
- A deployment alone does not trigger a migration; an installation event (or
  permission re-acceptance/repository re-add) is still required.

## Rollback

Close the migration PR and redeploy the previous Worker if the generated
changes are not wanted. Because the default branch is never written directly,
the repository can roll back by reverting or closing the PR. A future setup
version is skipped by older Workers rather than overwritten.

## Validation

- Unit tests cover legacy migration, current no-op, unknown no-write behavior,
  bounded file decoding, and branch refresh when the setup branch already
  exists.
- Typecheck the Cloudflare Worker and run the repository's Python quality and
  package tests before deployment.
