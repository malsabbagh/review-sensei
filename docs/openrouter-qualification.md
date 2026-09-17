# OpenRouter qualification

This runbook describes how to qualify an initial OpenRouter configuration for
ReviewSensei support claims. It reuses the existing promotion-record contract
and `require_supported_promotion`; it does not create a second approval process.

## Completion boundary

Harnesses, schemas, and validation in this repository can prove that fixture-only
or incomplete evidence cannot mint `supported` status. **Live qualification and
canary acceptance remain operator-only.** Do not fabricate live evidence, add CI
credentials, or enable website support labels solely because the adapter merged.

## Qualification target

The first supported slice is intentionally small:

| Field | Initial value |
| --- | --- |
| Provider | `openrouter` |
| Model | `anthropic/claude-3.5-haiku` |
| Upstream provider | `anthropic` |
| Base URL | `https://openrouter.ai/api/v1` |
| Endpoint scope | `remote` |
| Routing policy | No fallbacks, `require_parameters`, `data_collection=deny`, `zdr=true` |

Record the routing policy digest with
`review_sensei.openrouter_qualification.routing_policy_digest`. Mutable model
aliases and unavailable revision metadata must be recorded honestly in
`observed_revision`.

Unsupported or unobserved capabilities (broader catalog access, alternate
upstream providers, custom base URLs, or unreviewed stage models) stay outside
the qualified slice until a new target and evidence set are published.

## Thresholds and evidence collection

Use the packaged synthetic corpus (`evaluation/v1/corpus.json`) and the same
quality thresholds documented in [evaluation.md](evaluation.md):

- actionable precision, false-positive rate, expected-finding recall
- location validity and category coverage
- deterministic exact-fixture and contract-rejection rates where applicable

Collect **at least three independent live invocations** per promoted
configuration. Independence is the unique `run.invocation_id` stamped by each
`evaluate` invocation; copies that only change elapsed-time metrics are not
independent.

Run live evaluation only outside ordinary CI, with reviewed synthetic or
explicitly authorized inputs, `--allow-live-model`, `--allow-data-egress`, and
a non-empty `--provider-version`:

```bash
review-sensei evaluate --mode live \
  --corpus evaluation/v1/corpus.json \
  --provider openrouter \
  --base-url https://openrouter.ai/api/v1 \
  --model anthropic/claude-3.5-haiku \
  --provider-version <observed-openrouter-revision> \
  --allow-live-model --allow-data-egress \
  --output openrouter-live-1.json
```

Repeat until three independent passing reports exist. Retain exact report files
and provider terms/egress approval separately from the public support record.

## Support record

Publish a machine-readable `openrouter-qualification` document (schema id
`/v1/openrouter-qualification.schema.json`) that embeds the promotion-record
fields plus:

- `qualification_target` (model, upstream provider, base URL, routing policy digest)
- `evidence_references` (SHA-256 digests of retained live reports)
- `limitations` (honest scope bounds; the small corpus is not broad superiority)
- `status` (`supported`, `insufficient`, or `unsupported`)
- `rollback_decision` (`revert-to-baseline`, `hold`, or `none`)

Emit from Python:

```python
from review_sensei.openrouter_qualification import (
    INITIAL_OPENROUTER_QUALIFICATION_TARGET,
    qualification_record_from_reports,
    require_supported_openrouter_qualification,
)

target = INITIAL_OPENROUTER_QUALIFICATION_TARGET
record = qualification_record_from_reports(
    reports,
    target=target,
    observed_revision="<observed-openrouter-revision>",
    reproducibility={"temperature": 0},
    evaluated_at="2026-09-16T00:00:00Z",
)
require_supported_openrouter_qualification(record, reports)
```

Only `status=supported` with at least three evidence references and matching
live reports can authorize support labels or approval eligibility.

## Negative gates (harness coverage)

The qualification harness proves these cases fail closed:

- fixture-mode or fixture-provider reports, even when copied three times
- fewer than three independent live runs
- loopback `endpoint_scope` instead of `remote`
- model or routing-policy digest mismatch against the declared target
- failing quality thresholds (`unsupported`)
- duplicate `invocation_id` values
- explicit `status=supported` when evidence is insufficient
- supported records with fewer than three `evidence_references`

Ordinary CI runs `tests/test_openrouter_qualification.py` and
`tests/test_promotion_release.py` without live credentials.

## Release compatibility and canary

Before moving the public `v4` channel or enabling website support labels:

1. Build a compatibility manifest from exact Python, npm, workflow, schema, and
   Worker artifacts ([releasing.md](releasing.md)).
2. Bind fixture-downstream acceptance with `bind_canary_evidence(..., "fixture-downstream")`.
3. Complete operator-live canary evidence on a pinned disposable repository
   (#98) before treating GitHub installation as qualified.
4. Record channel promotion or rollback with `channel-promotion` metadata.

Unsupported platform/workflow combinations must remain clearly marked in release
metadata.

## Rollback and migration

Rollback is evidence-driven and must not overwrite immutable package tags or
silently move `v4`.

1. **Hold** (`rollback_decision=hold`): keep the previous supported
   OpenRouter qualification record and website label unchanged while
   investigating partial or ambiguous evidence.
2. **Revert to baseline** (`rollback_decision=revert-to-baseline`): publish an
   updated `openrouter-qualification` record with `status=insufficient` or
   `unsupported`, remove the support label, and retain prior evidence digests
   for audit.
3. **Channel rollback**: use `record_channel_rollback` with exact previous and
   candidate workflow SHAs; do not replace immutable `vX.Y.Z` tags.
4. **In-flight reviews**: stale heads, missing credentials, and unqualified
   models must not approve or publish unauthorized changes; approval eligibility
   follows the existing protection gates.

When migrating to a new model or routing policy, treat it as a new qualification
target with a fresh routing policy digest, new live evidence, and a new support
record. Never reuse `supported` status across model or policy changes without
new reports.

## Website and documentation consumption

Website support-matrix work (#91) must consume the published
`openrouter-qualification` record. OpenRouter support is scoped to qualified
combinations, not the gateway's full catalog. Merging the adapter (#96) or
configuration work (#97) does not, by itself, enable a support label.
