# Opt-in review decisions

Issue [#161](https://github.com/malsabbagh/review-sensei/issues/161) adds an
experimental decision pass after aggregate review generation. It is disabled by
default. Mocked tests qualify the contracts; neither adapter has been qualified
with live inference by this change. No model-quality, cost or latency improvement
has been demonstrated.

The review provider generates findings. The decision provider answers bounded
closed-choice questions about concern kind, evidence, supplied duplicate matches
and the next step. A requested verification uses the configured review provider
for at most one targeted batch. Original comments, blocking proposals and human
dispositions survive. Model agreement and confidence are not evidence or approval.

## Configuration

Add a `decision` section to the same trusted `.reviewsensei.yml` consumed by the
library, CLI and hosted runner. Omission or an expanded `decision` mapping with
`enabled: false` disables the pass.
There are no decision environment overrides. An API key variable names a secret;
its value is never interpolated into the document or diagnostics.

```yaml
schema: 1
inference:
  backend: local-ollama
  model: qwen3.5:4b
decision:
  enabled: true
  provider: ollama
  model: qwen3.5:4b
  base_url: http://127.0.0.1:11434/api
  max_calls: 4
  max_input_bytes: 65536
  max_output_bytes: 32768
  timeout_ms: 30000
```

The model example is an explicitly selected installed model, not a tested small
model recommendation. No automatic pull occurs. Local endpoints use literal
loopback addresses, disable environment proxies, reject cloud model suffixes and
accept no credential. Operators still need to qualify the local server and model:
a loopback URL cannot prove that a separately managed server never proxies work.

```yaml
decision:
  enabled: true
  provider: openrouter
  model: typesafe/jev-1.13
  api_key_env: DECISION_OPENROUTER_API_KEY
  allow_remote: true
  upstream_provider: typesafe
  max_calls: 4
  max_input_bytes: 65536
  max_output_bytes: 32768
  timeout_ms: 30000
```

This requests the fixed SystemOne endpoint, separately selected upstream, no
fallbacks, required parameters, denied data collection and ZDR. The upstream/model
combination is an unqualified example; an unavailable compliant route fails
without downgrading privacy controls. Unknown configuration fields are rejected.
Requested and returned model revisions are recorded separately.

Core CLI deployments can explicitly configure hybrid review and decision routing.
A named local-only review profile rejects remote decisions. The existing hosted
workflow forwards its selected review credential: its OpenRouter decision route
therefore requires the same explicitly named credential variable, while review
and decision model/upstream selections remain independent. Local hosted decisions
require the local runner route. Other runner/credential combinations are refused
during planning. This change does not add secrets, permissions or workflow inputs.

`plan`, `doctor`, and `host-plan` display the decision settings without inference
or secret values. Installed older runners reject the new configuration; a source
merge does not update a deployed runner.

## Bounds and conservative outcomes

The pass runs once after complete aggregate analysis, including chunked analysis.
Zero candidates cause zero decision calls. Three candidates form a batch; the
whole structured state has a 16 KiB ceiling. Whole relevant diff hunks are supplied
when bounded, and absent evidence is explicitly marked. No source packet is
silently shortened to obtain a successful decision. Unprocessed candidates remain
visible and the review stays partial.

All batches and the single verification batch use the original run tracker and
deadline. The subordinate limits include verification: exhausting decision calls
can leave no allowance for it. Unknown decision output retains its reserved byte
allowance. No retry or provider fallback is available. Decision processing is
withheld after partial analysis or an analysis transport retry, because this
first integration does not import unknown output reservations from that analysis.

Optional suggestions are identified separately in the receipt and summary counts;
they are not deleted. A proposed duplicate remains suspected and both findings
remain present. The pass does not coalesce solely on model similarity or treat
omission as a fix. Contradiction, missing context, malformed responses, exhausted
work or a human-review choice leave an unresolved result. Verification checks
candidate IDs and supplied hunk hashes; it does not execute tests or establish
independent behavioral proof.

The optional `decision_pass` receipt contains bounded choices, model identities,
usage when available, counters and candidate references. It contains no source
packets or raw responses. Its content participates in a separate digest domain;
disabled historical digests are unchanged. If optional presentation does not fit,
the appendix is omitted. If even the receipt cannot fit, original candidates are
returned as partial with an incomplete decision stage observation.

Decision policy participates in cache, continuation and transaction configuration
identity. Enabled analysis does not write the ordinary complete-analysis cache.
Recovery reconstruction binds the same decision policy identity in both review
work modes; changing it cannot reuse an earlier transaction. Matching installed
readers still require qualification before hosted enablement. Historical results
are not rewritten or granted new reuse authority.

## Extension and qualification

`ReviewDecisionProvider.evaluate(request, execution)` is independent of the
inactive comment-intent protocol. Register an explicit factory with
`DecisionRegistry`, or inject an adapter into `ReviewService` together with matching
`DecisionConfiguration`. Extensions use `DecisionExecution.execute` for each
physical attempt and retain closed-choice/request identity validation. There is
no automatic plugin discovery or default adapter for an unknown provider.

See [ADR 0124](adr/0124-review-engine-decision-providers.md) and the
[frozen evaluation plan](review-decisions-evaluation.md). Relevant wire contracts:
[Ollama structured outputs](https://docs.ollama.com/capabilities/structured-outputs),
[Ollama generate](https://docs.ollama.com/api/generate), and
[OpenRouter SystemOne](https://openrouter.ai/docs/api/api-reference/systemone/submit-a-system-one-request).
