# Process Index

Project: ReviewSensei
Last updated: 2026-08-10

This document defines the durable delivery process for this repository. Use it as the default process unless a more specific repo policy overrides it.

## Process Goals

- Keep work tied to a GitHub Issue or local story artifact.
- Make architecture decisions explicit through ADRs.
- Capture validation evidence before review or ship decisions.
- Prove each required acceptance criterion with fresh, criterion-linked evidence.
- Scale assurance and verification to impact, uncertainty, reversibility, and validation difficulty.
- Keep generated and temporary agent output under `.project-ai/output/`.
- Keep durable project knowledge under `docs/`.
- Bound discovery context and verify structured retrieval hints against current source.

## Stages And Gates

The identifiers below are canonical and ordered. State and automation must use them exactly.

| Identifier | Stage | Purpose | Required artifact or evidence | Gate |
| --- | --- | --- | --- | --- |
| `intake` | Intake | Define the smallest useful work item | GitHub Issue or `.project-ai/output/stories/<work-id>.md` | Outcome contract has stable criterion IDs, observable oracles, constraints, and approval gates |
| `discover` | Discover | Understand affected areas and constraints with bounded retrieval | `.project-ai/output/context/<work-id>.md` | Backend, freshness, impact, risk, assurance, exact-source checks, and open questions are recorded |
| `design` | Design | Choose an approach and ADR need | Design notes and ADR or no-ADR rationale | Approach, tradeoffs, validation strategy, and rollback are documented |
| `plan` | Plan | Order verifiable slices and ownership | `.project-ai/output/plans/<work-id>.md` | Tasks map to criteria, evidence, dependencies, and budgets |
| `implement` | Implement | Make scoped changes | Code, docs, and tests | Planned slices are complete or scope changes are recorded |
| `quality` | Quality | Validate behavior and maintainability | Structured evidence under `.project-ai/output/runs/<work-id>/evidence/` | Required checks are fresh and pass; skips and residual risk are explicit |
| `review` | Review | Check correctness, security, architecture, and tests | Review findings and verifier packet | Blocking findings are resolved and required independent verification agrees |
| `ship` | Ship | Compute readiness and prepare the handoff | Final evaluation and report | Verdict is `ready` or explicitly accepted `ready_with_risk` |

## Outcome Contract And Evidence

Each non-trivial task records a goal, scope, non-goals, constraints, approval gates, and stable criteria such as `AC-01`. Every required criterion names an observable oracle and evidence type. Evidence records include the method, timestamp, result, input or diff fingerprint, and supported criterion IDs. A narrative claim without matching fresh evidence cannot produce `ready`.

Run truth lives under `.project-ai/output/runs/<work-id>/`. `.project-ai/state.json` is only a compatibility pointer or summary.

## Assurance And Verification

- `fast`: reversible, low-impact work with strong deterministic oracles; targeted gates and self-review.
- `standard`: normal product and engineering work; full configured gates plus a fresh-context acceptance verifier.
- `high`: security, privacy, migrations, public contracts, protected files, irreversible actions, or weak-oracle work; negative-path and rollback evidence plus acceptance and specialist verification.

Use one executor by default. Parallel workers need a central owner, non-overlapping objectives, bounded outputs, and isolated workspaces when edits could conflict. Worker output is evidence to validate, not truth to adopt automatically.

## Bounded Recovery

Normalize and classify failures before retrying. Retry a transient failure once. Repair an implementation defect only with a changed hypothesis or inputs. Replan after the same fingerprint occurs twice. Escalate specification gaps and missing authority immediately. Exhausted budgets end with `not_ready` or `blocked`, preserving the full run record.

## Required Artifacts By Change Type

| Change type | Required artifacts |
| --- | --- |
| Feature | Issue/story, delivery plan, validation report, review report |
| Bug fix | Issue/story, reproduction or characterization, validation report |
| Architecture-sensitive | Issue/story, delivery plan, ADR or no-ADR rationale, architecture review |
| Data or migration | Issue/story, ADR when policy requires, rollout plan, rollback plan, validation report |
| Release | Ship report, CI status, release notes, rollback plan |

## Roles

| Role | Responsibility |
| --- | --- |
| Requester | Defines outcome and acceptance criteria |
| Implementer | Plans, changes, validates, and records evidence |
| Reviewer | Reviews correctness, maintainability, security, and process compliance |
| Architecture reviewer | Reviews ADRs and boundary-impacting changes |
| Release owner | Confirms CI, rollout, rollback, and release notes |

## Artifact Locations

- Project contract: `.project-ai/repo.md`
- Agentic and tool-risk policy: `.project-ai/policies/`
- Machine-readable schemas: `.project-ai/schemas/`
- Per-work-item run truth: `.project-ai/output/runs/`
- Task context: `.project-ai/output/context/`
- Plans: `.project-ai/output/plans/`
- Reports: `.project-ai/output/reports/`
- Reviews: `.project-ai/output/reviews/`
- Stories: `.project-ai/output/stories/`
- Durable process docs: `docs/process/`
- Durable architecture docs: `docs/architecture/`
- ADRs: `docs/adr/`

## Exception Handling

If a gate cannot be satisfied, record:

- what is missing
- why it is missing
- risk created by the gap
- who can resolve it
- whether work should continue, pause, or ship with known risk

Do not silently skip required artifacts.
