# Frozen decision evaluation and proposed smoke

No live call is authorized or recorded by this source change. Fixture success is
contract evidence, not evidence of improved model quality.

## Paired baseline

Freeze the repository commit, `evaluation/v1/corpus.json`, every asset digest in
that corpus, review configuration, decision configuration, requested model versions,
actual returned identities and evaluation code before any run. Preserve the
existing `evaluate_fixture()` exact-conformance control and thresholds unchanged.
Reuse `evaluation.run_case(..., fixture_exact=False)` and the existing corpus
matching/scoring functions for paired disabled/enabled services. Do not remove
decision receipts to manufacture equality with an old exact oracle.

Use the existing off-by-one, shell-injection, layer-violation, clean-guard and
missing-regression-test examples as adjudicated synthetic expectations. Add an
explicit annotation sheet for optional preference, missing evidence, exact and
lookalike duplicate pairs, and a multi-commit sequence (concern persists, prose
changes, independently demonstrated fix, authorized human disposition). Freeze
that sheet and the candidate-to-expectation mapping before inference. Human review
of ambiguous labels remains pending; these cases cannot certify quality until the
labels are settled. Use identical input snapshots and review model/settings for
each pair. Retain generation variability rather than attributing it to triage.

Report actionable precision, known-defect recall (separately for serious defects),
false blocking findings, duplicate count, reopened concerns, unresolved cases,
latency, total calls and bytes. Use the original run counters and decision receipt:
`MeasuredProvider` alone counts only the review provider. Unknown usage remains
unknown. Report paired sample counts and every regression; do not average away a
lost serious defect. Promotion requires preserved serious-defect recall, no false
approval from incomplete processing, and a documented improvement in at least one
target measure. Otherwise record insufficient evidence and keep recommendations
withheld. Existing evaluation/promotion policy remains authoritative.

## One concrete live-test proposal

First qualify local Ollama only, on an operator-identified machine with an already
installed explicitly versioned small model. Proposed model: `qwen3.5:4b`, subject
to the operator confirming the exact installed digest and hardware. Endpoint:
`http://127.0.0.1:11434/api/generate`; proxies disabled; no credential or model pull.
Use synthetic public corpus excerpts only. Proposed cap: six physical inference
calls in total, 64 KiB aggregate input, 32 KiB aggregate output and 120 seconds
wall time; zero remote spend. Run one empty-inventory control and a bounded subset
covering a real defect, optional concern, missing evidence and duplicate ambiguity.
Stop on the first contract, privacy or budget discrepancy. Retain sanitized model
identity, choices, usage, counters and timing; retain no raw prompts/responses.

This is a proposal, not an executed or scheduled test. Approval must identify the
actual runtime/model digest and confirm the six-call cap. OpenRouter qualification
follows separately with a provisioned Jev route, explicit upstream/ZDR policy and
an additional approved monetary cap; the local proposal does not authorize it.

## What this can and cannot close

Classification can help candidate relevance, optional-versus-defect presentation,
duplicate suspicion and choosing focused verification. Measure those effects.
It does not close caller lifetime, unknown-effects accounting, durable retention,
authenticated authority, recovery source provenance, title/comment freshness or
terminal settlement gaps. No new matrix gap is closed merely because this feature
is present or a PR merges.
