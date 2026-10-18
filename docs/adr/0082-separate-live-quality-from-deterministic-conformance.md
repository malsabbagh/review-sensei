# ADR 0082 - Separate live quality from deterministic conformance

Status: Proposed
Date: 2026-10-18
GitHub Issues: #118, #119, #120, #122
Pull Request: pending (#316 stack)
Owners/Reviewers: Maintainers
Approved by: not applicable

## Context

Accepted ADR 0012 separates deterministic engine checks from model variance.
The packaged corpus freezes exact fixture JSON, a malformed-output rejection,
and five numerical live quality thresholds. Live evaluation instead compared
the fixture provider/model and full JSON to live output, then skipped the
rejection case while requiring its threshold. Even exact finding-oracle output
could not pass. The reported recall denominator also counted matched
expectations instead of the full expected inventory, making empty findings look
like perfect recall.

## Decision and scope

Each live invocation executes the existing local fixture evaluation on the same
validated corpus and packaged engine/prompts. Its full case inventory supplies
the two deterministic rates and their unchanged thresholds. Actual live quality
cases use the existing one-to-one path/line/category/body-term oracle and clean
case rule. Their expected count is the full oracle inventory. Summary wording
and provider/model identity are not exact fixture assertions on live quality.

The bounded, closed optional v1 `evaluation_control` object records protocol
`fixture-conformance-and-live-quality-v1`, fixture provider/model, engine,
prompt, corpus and fixture configuration digests, review configuration id,
complete case results, and all original corpus thresholds.
The sidecar also retains its own quality summary, failures and overall verdict;
any existing fixture gate failure adds a `fixture_conformance` blocker.
Fixture configuration identity remains distinct from live configuration. Live quality
rows add `execution_complete`; failures or incomplete results cannot satisfy the
live inventory requirement. Fixture calls/bytes do not enter live usage metrics.
Live contract rows remain explicitly skipped; the local sidecar executes their
rejection checks. The protocol participates in new live configuration digests.

The overall verdict is the conjunction of actual live quality and local
deterministic conformance against every unchanged threshold, plus a nonempty,
complete live quality inventory with provider attempts. Readers and promotion
gates cross-check identities, complete inventories, expected counts, metrics,
rates and verdicts. Contradictory controls fail closed. Missing historical
controls remain structurally valid v1 reports but cannot newly mint or validate
supported promotion. Three independent live invocations remain required.
Passing fixture checks, including a later rerun, cannot repair bad live evidence.

This is a consistency contract for operator-retained reports, not a signature
or independent attestation that a report producer was truthful. OpenRouter's
retained routing/endpoint binding remains in ADR 0081. Runtime approval binding
under #115, real model qualification, credentials, publication and deployment
remain outside this repair.

## Alternatives considered

Ignoring deterministic thresholds in live mode would omit required checks.
Sending the malformed fixture's diff to a real model cannot force the malformed
response that the engine rejection test requires. Normalizing provider/model
inside full fixture equality would still mistake harmless model wording for a
deterministic engine failure. The separate local control preserves exact engine
assertions while live quality retains its existing numerical oracle.

## Validation

`LiveEvaluationTests` uses the actual packaged corpus and synthetic providers.
It reproduces oracle-perfect live failure on the prior evaluator, then proves
three independent synthetic live invocations can pass all checks. Poor live
quality with a passing sidecar remains failed; a failed sidecar blocks perfect
live quality. Missing, partial, mismatched and contradictory controls fail at
the report-bound promotion gate. Existing fixture, promotion, OpenRouter,
schema, CLI and installed-source contracts remain regression coverage. No
live provider call or paid qualification is part of this validation.

## Consequences, rollout and rollback

Corpus bytes, numerical thresholds and matching terms are unchanged. Fixture
output remains compatible. Strict closed v1 readers need the current schema
for the optional control and case field. Retained historical evidence must not
be rewritten; collect new evidence for the new protocol. Rollback is code-only
but must not enable unsupported promotion or restore fabricated recall.

## Follow-up

The integration owner runs the full source, Python compatibility, build and
clean-wheel suites on final bytes. Maintainer review accepts this proposed
contract through the normal PR process. #122 still requires authorized live
qualification and retained independent reports; synthetic success is not that
evidence. #115's runtime approval evidence path remains separate.
