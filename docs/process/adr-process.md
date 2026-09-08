# ADR Process

Project: ReviewSensei
Last updated: 2026-08-10

This document defines how architecture decisions are proposed, reviewed, accepted, superseded, and linked.

## ADR States

- `Proposed`: drafted for review.
- `Accepted`: approved and used by implementation.
- `Rejected`: recorded option that should not be implemented.
- `Superseded`: replaced by a newer ADR.

## When An ADR Is Required

Create or update an ADR for changes that affect:

- public APIs, events, schemas, or cross-module contracts
- data model, persistence, migrations, retention, or backfills
- authentication, authorization, secrets, privacy, or security posture
- deployment topology, infrastructure, queues, caching, or operational ownership
- dependency direction, package boundaries, or long-lived architectural patterns
- irreversible or expensive-to-reverse decisions
- meaningful tradeoffs future maintainers need to understand

ADR usually is not required for:

- small bug fixes inside an established pattern
- isolated refactors with no contract or behavior change
- documentation-only changes
- test-only changes
- dependency bumps with no architecture impact

When uncertain, record a short no-ADR rationale in the task context.

## Required ADR Sections

Every new ADR and every material amendment adopted after this process must
include:

- status
- explicit approver when status is `Accepted`
- date
- decision owner or reviewers
- linked GitHub Issue
- linked PR when known
- context
- decision
- scope
- consequences
- alternatives considered
- validation
- rollout and rollback
- follow-up work

ADRs 0001 through 0005 predate this process and are grandfathered as historical
records. They need not be rewritten solely for formatting compliance. If one
is materially amended or superseded, the amendment or replacement must satisfy
the current sections and approval rules while preserving the original record.

## Review Rules

- Do not mark an ADR `Accepted` without explicit maintainer approval or an existing repo policy that allows it.
- The ADR body and index status must match. `create_adr.py --status Accepted` requires `--approved-by <identity>` and records that identity in the body.
- Do not delete superseded ADRs.
- Keep accepted ADRs immutable except typo fixes, link updates, or explicit amendment sections.
- Link superseded ADRs forward to replacements.
- Link PRs and GitHub Issues back to the ADR.

## Templates And Helpers

- ADR template: `.project-ai/templates/adr.md.tmpl`
- ADR review template: `.project-ai/templates/adr-review.md.tmpl`
- ADR index: `docs/adr/README.md`

Create a numbered ADR with:

```bash
python3 <plugin-root>/skills/project-development/scripts/create_adr.py --root . "Decision title"
```

Create an already approved ADR only when approval has actually been given:

```bash
python3 <plugin-root>/skills/project-development/scripts/create_adr.py \
  --root . \
  --status Accepted \
  --approved-by "Maintainer name or handle" \
  "Decision title"
```

## v2 Approval Enforcement

The ADR body and index status must match. Creating an ADR with `--status Accepted` requires explicit maintainer approval through `--approved-by <identity>`; the helper records that approver in the ADR body. Superseded ADRs remain in place and link forward.
