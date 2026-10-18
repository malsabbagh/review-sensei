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

The current hosted candidate eligible for qualification is intentionally small:

| Field | Candidate value |
| --- | --- |
| Provider | `openrouter` |
| Model | `deepseek/deepseek-v4.1-flash` |
| Upstream provider | `morph` (family constraint) |
| Base URL | `https://openrouter.ai/api/v1` |
| Endpoint scope | `remote` |
| Routing policy | No fallbacks, `require_parameters`, `data_collection=deny`, `zdr=true` |

Hosted selectability and target eligibility do not grant `supported` status.
The historical Haiku/Anthropic eligibility is retained; other hosted models and
unknown combinations cannot inherit this candidate's evidence. The `morph` slug
selects eligible endpoints in a provider family, not one fixed region/deployment.
Current strict-policy capability and three independent live passing runs remain
pending #122. The [official routing contract](https://openrouter.ai/docs/guides/routing/provider-selection#targeting-specific-provider-endpoints)
allows family and endpoint prefixes; full slugs are preserved in requests and
policy/configuration digests. Endpoint variants shown in protocol tests are not
qualified targets. Check the [candidate catalog](https://openrouter.ai/deepseek/deepseek-v4.1-flash/providers)
and actual strict-policy behavior during authorized qualification; no live catalog
lookup occurs in ordinary execution or CI.

The candidate policy digest is
`27fbcad31a801f636838de7fe087bdd9bb73b5318416cd473a92b72eb2efe400`.
A changed upstream suffix produces a different digest and requires new evidence.

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
OPENROUTER_UPSTREAM_PROVIDER=morph review-sensei evaluate --mode live \
  --corpus evaluation/v1/corpus.json \
  --provider openrouter \
  --base-url https://openrouter.ai/api/v1 \
  --model deepseek/deepseek-v4.1-flash \
  --provider-version <observed-openrouter-revision> \
  --allow-live-model --allow-data-egress \
  --output openrouter-live-1.json
```

`--provider-version` remains an operator attestation, not a response probe.
`ProviderResponse.revision` contains only a reported fingerprint when available;
model aliases never substitute for it. Record unavailable identity honestly and
retain provenance separately. Optional provider-reported usage is telemetry and
cannot establish qualification or approve a review.

Repeat until three independent passing reports exist. Retain exact report files
and provider terms/egress approval separately from the public support record.

Each live invocation now runs exact local fixture conformance separately from
actual live finding quality. Both must satisfy every original corpus threshold;
missing/incomplete live work or a failed control withholds passing evidence.
The optional `evaluation_control` records both provenance and the complete local
inventory (proposed ADR 0082). A passing fixture run cannot repair bad live
quality. Historical reports without this control still parse, but cannot newly
mint or validate supported promotion; preserve their original bytes and collect
new evidence. No real provider has been qualified by the synthetic regressions.

Live OpenRouter reports bind their actual requested policy and endpoint in
`run.openrouter_routing_policy_digest` and `run.openrouter_base_url`. Every live
report must match the qualification target on both fields before a support
record can be minted or checked. A `morph/variant` policy cannot inherit `morph`
family evidence. Old reports without these optional schema fields still parse,
but cannot newly authorize support; preserve their original bytes and collect
new bound evidence. Historical Haiku/Anthropic remains an eligible target.

## Support record

Publish a machine-readable `openrouter-qualification` document (schema id
`/v1/openrouter-qualification.schema.json`) that embeds the promotion-record
fields plus:

- `qualification_target` (model, upstream provider, base URL, routing policy digest)
- `evidence_references` (SHA-256 digests of the exact retained live report
  bytes, reproducible with `sha256sum openrouter-live-1.json`)
- `limitations` (honest scope bounds; the small corpus is not broad superiority)
- `status` (`supported`, `insufficient`, or `unsupported`)
- `rollback_decision` (`revert-to-baseline`, `hold`, or `none`)

Emit from Python:

```python
import json

from review_sensei.openrouter_qualification import (
    CURRENT_HOSTED_OPENROUTER_QUALIFICATION_TARGET,
    qualification_record_from_reports,
    require_supported_openrouter_qualification,
)

target = CURRENT_HOSTED_OPENROUTER_QUALIFICATION_TARGET
artifacts = [path.read_bytes() for path in retained_report_paths]
reports = [json.loads(artifact) for artifact in artifacts]

# The UTC instant the live runs were observed. The gate rejects a value dated
# in the future, so this must be a real observation time, not a literal copied
# from this runbook.
evaluated_at = "<observed-utc-instant>"  # e.g. "2026-09-16T00:00:00Z"

record = qualification_record_from_reports(
    reports,
    target=target,
    observed_revision="<observed-openrouter-revision>",
    reproducibility={"temperature": 0},
    evaluated_at=evaluated_at,
    report_artifacts=artifacts,
)
require_supported_openrouter_qualification(
    record,
    reports,
    report_artifacts=artifacts,
    expected_target=target,
    expected_evaluated_at=evaluated_at,
    expected_reproducibility={"temperature": 0},
)
```

`report_artifacts` holds the exact retained bytes of each report, in the same
order as `reports`. The harness hashes those bytes itself, so it refuses to mint
or accept `supported` status without them, and rejects `evidence_references`
that the operator supplies but the bytes do not produce. This applies at every
status, not only `supported`: a record may omit `evidence_references`
entirely, but it may not carry digests without the artifacts behind them, so a
historical record cannot be regenerated from a published document alone.

A published record that carries digests also carries
`evidence_reference_kind: retained-bytes-v1`, and the schema requires that
marker on any `supported` document. A consumer can therefore tell from the
record itself what the digests are digests of, rather than inferring it from
this runbook. Digests of
re-serialized in-memory documents are not interchangeable with these values.

`OpenRouterQualificationRecord` tracks where its `evidence_references` came
from, and refuses direct construction of a `supported` record: only
`qualification_record_from_reports` (which hashed the retained bytes) and
`validate_openrouter_qualification_record` (which is reading an
already-published document for verification) can produce one. Constructing the
dataclass by hand with a `supported` promotion and fabricated digests raises
`ReviewInputError` instead of yielding a publishable record.

Only `status=supported` with at least three evidence references and matching
live reports can authorize support labels or approval eligibility.

### Operator attestations

`evaluated_at` and `reproducibility` describe the observation window, and
evaluation reports do not carry either value. The gate always rejects an
`evaluated_at` that is not an RFC 3339 UTC instant or that is dated in the
future, but it cannot derive the window from the reports themselves.

There is a limit to what the gate can check about `evaluated_at`. Evaluation
reports carry no timestamp of their own, so the gate cannot verify that
`evaluated_at` falls at or after the runs it describes. It rejects a value that
is malformed or dated in the future, and it rejects one that disagrees with the
caller's attested `expected_evaluated_at`, but a record bearing a real past
instant earlier than the runs is accepted. Treat `evaluated_at` as an
operator attestation bounded by those checks, not as a derived observation
time.

The gate therefore requires `expected_target`, `expected_evaluated_at`, and
`expected_reproducibility` by default, so the comparison record is minted from
the caller's own mint inputs instead of from the record under test. A consumer
that holds only the published record and the reports must opt into the weaker
guarantee with `allow_self_attested_inputs=True`, which makes at the call site
the fact that the record is attesting to its own observation window. Pass `now`
to inject the clock used for the future-dating check when a caller needs
deterministic gate decisions.

Downstream consumers should prefer `load_supported_openrouter_qualification`,
which parses a published document and runs the gate in one step, over calling
`validate_openrouter_qualification_record` directly: the latter is a structural
parse, so a record it returns can report `status=supported` without any
artifact check having run.

### Qualification slice

`OpenRouterQualificationTarget` accepts only the published
`(model, upstream_provider)` pairs in `PUBLISHED_QUALIFICATION_SLICES`. A new
model or upstream provider requires publishing a new target alongside its
evidence set; the harness will not mint a record for an unlisted combination
even when the base URL is allowlisted.

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
- `supported` status minted without the retained report artifacts
- `evidence_references` that the retained bytes do not produce, or that repeat
  the same digest
- an artifact whose bytes do not parse to the report it accompanies
- `evaluated_at` that is malformed, future-dated, or stale against the
  attested mint inputs
- targets outside the published model and upstream-provider slice

Ordinary CI runs `tests/test_openrouter_qualification.py` and
`tests/test_promotion_release.py` without live credentials.

## Runtime approval boundary (#115)

The GitHub adapter does not yet bind retained qualification reports to the
independently expected effective configuration of a review run. Until that
integration exists, OpenRouter reviews withhold automatic approval. A named
profile's `qualification_status`, a caller's `qualification="qualified"`, and
the publisher's historical `not-required` default cannot mint eligible
OpenRouter facts. A published target identifies which configurations the
harness can evaluate; it does not grant support or approval.

`tests/test_github_qualification_boundary.py` captures the real publisher's
GitHub requests with external HTTP replaced. It checks omitted/asserted status,
application publication, recovery, replay, and separate finalization of the
newly persisted withheld facts. Permitted comments and requests for changes,
other providers, and approval/write opt-outs keep their existing policy.

This repair does **not** complete #115. A supported positive runtime control
still needs retained report-byte validation through
`load_supported_openrouter_qualification`, independent effective engine,
prompt, configuration and routing identities, and durable exact-head binding.
The evaluation configuration digest and transaction configuration digest have
different contracts and must not be treated as interchangeable. Existing
standalone finalizer eligibility documents carry no provider/evidence identity;
this test cannot prove that an older `qualified` document was backed by reports.
Live qualification (#122) and installed/deployed acceptance (#123) remain
separate operator evidence.

## Release compatibility and canary

Before moving the public `v5` channel or enabling website support labels:

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
