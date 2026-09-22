# Product and source direction

Updated: 2026-09-22. Roadmap: [#167](https://github.com/malsabbagh/review-sensei/issues/167).

## Product promise

**Evidence-backed reviews that converge, with your models, policy and control.**

ReviewSensei should help authors and maintainers find material defects, verify
fixes, distinguish optional improvements, and reach an explicit next action.
Human- and agent-authored changes use the same review contract. GitHub pull
requests are the first host integration, not the definition of the domain.

The immediate product is an open review engine, CLI and controlled GitHub
integration. It is not a hosted development agent, autonomous editor, universal
model benchmark or a guarantee that a pull request will merge. A trustworthy
human handoff is a successful outcome when automation lacks evidence or budget.

This document records direction, not a feature-availability or qualification
manifest. Existing implementation and release contracts remain authoritative.
The [dated audit](audits/2026-09-22-review-sensei.md) separates source, observed
test behavior, release-channel read-back and unverified deployment claims.

## What is current, what is next

At audited main `1d1f9389c09a1e575b292029c013ea3dbc9af516`, the source includes
review providers, staged review, bounded context, finding validation/lifecycle,
convergence policy, durable session APIs, human dispositions, GitHub publication
and an observed sequence harness. Reuse these rather than recreating them.

The omitted review policy still resolves to `legacy` at that snapshot. The owner
decision in [#146](https://github.com/malsabbagh/review-sensei/issues/146) requires
`merge-focused` to replace it as the supported default. Advisory and strict remain
choices inside the same architecture. Legacy may survive in historical fixtures
and bounded migration readers, not as a permanent fallback or second live engine.
[#155](https://github.com/malsabbagh/review-sensei/pull/155) is the pending cutover
implementation, not proof that the replacement is already released or deployed.

A pluggable DecisionProvider is **planned** in
[#161](https://github.com/malsabbagh/review-sensei/issues/161). It is not implemented
by this direction document. The new default and DecisionProvider are different
changes: one governs the review loop; the other supplies optional typed model
assessments within that loop.

## Priority and finite ownership

| Priority | Deliverable | Existing owner |
| --- | --- | --- |
| P0 / now | Completeness and required qualification at every actual approval emission | [#114](https://github.com/malsabbagh/review-sensei/issues/114), [#115](https://github.com/malsabbagh/review-sensei/issues/115) |
| P1 / now | Finish integrated convergence, required default replacement, migration and rollback | [#136](https://github.com/malsabbagh/review-sensei/issues/136), [#146](https://github.com/malsabbagh/review-sensei/issues/146) |
| P1 / next; development can overlap | One typed DecisionProvider seam, local and hosted adapters, actual triage/verification integration | [#161](https://github.com/malsabbagh/review-sensei/issues/161) |
| Parallel | Exact caller/runner compatibility, one qualified OpenRouter review configuration and operator evidence | [#92](https://github.com/malsabbagh/review-sensei/issues/92), [#99](https://github.com/malsabbagh/review-sensei/issues/99) |
| Parallel; false claims first | Truthful capability states, executable first review, diagnostics and accessibility | [#91](https://github.com/malsabbagh/review-sensei/issues/91) |

Do not hold local implementation, safety fixes or truthful documentation behind
paid qualification. A specific support claim, hosted capability or approval
eligibility still needs its relevant evidence. Website acceptance can finish
with a provider accurately described as implemented but unqualified. None of
these owners acquires a new closure dependency merely because this roadmap exists.

## Source-code responsibilities

The intended logical flow is:

```text
trusted configuration + bounded change/context + retained session history
  -> existing review generation and candidate/evidence validation
  -> optional DecisionProvider assessments and one bounded follow-up batch
  -> deterministic blocker admission and convergence policy
  -> validated outcome + identity-bound durable transaction/checkpoint
  -> independently authorized host publication/finalization
```

This diagram describes responsibilities, not a proposal for a second orchestrator
or mandatory execution of every phase. Observation-only/local reviews and
publication-only recovery must retain their existing distinct semantics.

| Responsibility | Existing starting points | Development rule |
| --- | --- | --- |
| Requests, coverage and review generation | `models.py`, `service.py`, `stages.py`, `coverage.py`, `planning.py` | One domain service and aggregate work budget; no GitHub or vendor SDK dependency in the core. |
| Finding identity, evidence and blocker admission | `verifier.py`, `baseline.py`, `context.py`, `convergence.py` | Model assessments are proposals. Reuse deterministic evidence/lifecycle rules; omission and confidence are not proof. |
| Decision triage, planned | #161 contract and adapter work beside current provider construction | Closed-choice, bounded packets; no vendor response types or executable model actions in the domain contract. |
| Durable review transaction and human controls | `session.py`, `disposition.py` | Preserve original resolution criteria, dispositions and consumed work across jobs; expiry or missing established history cannot reset authority. |
| Model transport/configuration | `providers/`, `provider_config.py` | Own wire formats, credentials, privacy policy and bounded parsing, not merge decisions. Review and decision models are independently selected. |
| Publication and delayed finalization | `hosting/github/{application,publication,approval}.py` | Consume trustworthy exact-head eligibility; never infer completeness/qualification from the absence of blockers or resolved threads. |
| CLI and host composition | `cli.py`, `workflow.py`, `hosting/github/` | Reuse the same domain outcomes. Extract a shared application use case only for an actual consumer, with parity tests. |
| Evaluation and public claims | `evaluation.py`, `openrouter_qualification.py`, `sequence.py`, `hosting/github/observed.py`, site tooling | Structural validity, observed behavior, model qualification and release availability are separate facts. |

Large modules are a maintenance signal, not an independent reason for a rewrite.
Refactor a seam as part of a focused feature or bug fix; retain behavior tests and
avoid mixing unrelated file moves with policy changes. Do not add a second
provider registry, qualification system, session store or JSON source of truth.

### DecisionProvider boundary

Start with local Ollama and Jev through OpenRouter, preserving the full #161
contract. Candidate generation investigates; decision assessment helps triage;
deterministic policy owns the effective action. A model's agreement is not an
independent behavioral test, and no decision backend can waive #114 or #115.

Use local Ollama's schema-constrained, non-streaming request format and validate
answers again. Jev uses the documented native System One question/answer route,
not the existing review adapter's Chat Completions wire format. Keep requested
and observed identities separate and verify endpoint-specific privacy controls.
The official contracts were checked on 2026-09-22:
[Ollama structured outputs](https://docs.ollama.com/capabilities/structured-outputs)
and [OpenRouter TypeSafe integration](https://openrouter.ai/docs/guides/community/typesafe-sdk).
Do not extrapolate local structured-output support to Ollama Cloud.

No candidates means no decision call. Limits cover all chunks, batches, retries
and at most one targeted verification follow-up batch per review. Unavailable or
inconclusive assessment preserves explicit unresolved work. Local-only operation
cannot trigger a remote decision call or automatic cloud fallback. Live adapter
compatibility and quality recommendations need separately authorized evidence;
ordinary CI uses fixtures and fake external APIs.

## Invariants and success measures

Keep review scope, blocker admission and automation budgets distinct. The initial
one-review/two-verification budget is a product hypothesis, not a scientifically
optimal limit. A cap never approves, erases a concern or suppresses a substantiated
new regression. A qualifying last permitted round can approve only if all
independent gates pass. Authenticated human continuation grants bounded work,
not authority to bypass completeness, current-head or qualification checks.

Evaluate actionable blocker precision, material-regression recall, unjustified
late blockers, duplicate/reopened concerns, verification rounds, human handoffs
and additional calls/latency. Preserve serious-defect recall on the adjudicated
set and improve at least one target measure before recommending a decision
configuration. Freeze comparison conditions and thresholds; retain failures and
unknowns. Fewer comments, fewer rounds or more approvals alone is not success.

Source existence, schema validation, a passing helper test, a passing full suite,
model qualification, package publication and deployed compatibility are different
levels of evidence. Show the level actually established. A mutable tag or website
label cannot manufacture a supported configuration.

## Not in the immediate roadmap

No autonomous source edits or privileged execution of PR code; no hosted review
service, account/billing platform, inference resale, broad model catalog,
fine-tuning, mandatory shadow platform, IDE suite, MCP server, multi-host expansion
or unrelated dashboard. Preserve MIT/community and trademark boundaries and
independent analysis, publication, approval, learning, reply and artifact controls.

Tickets close against their own finite acceptance criteria, not age or merge
keywords. Credit implemented foundations, keep historical failures as regressions,
and update exact current evidence. An advisory suggestion is not a reason to
reopen completed work or spawn another release blocker. See the
[architecture index](architecture/index.md) for source/ADR ownership and the
[release process](releasing.md) for operator-controlled publication and rollback.
