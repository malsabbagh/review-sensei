# OpenRouter contract acceptance evidence

Source baseline: `7a641b8b6402292f213159853400aff4f2e9a3d9`.
This matrix covers deterministic protocol/configuration behavior only. It is
not live provider qualification, a support label, or an approval eligibility
record. Proposed ADR 0081 records the public contract and rollback constraints.

| Issue / acceptance | Code seam | Exact test evidence | Remaining condition |
| --- | --- | --- | --- |
| #119 independent requested/observed model, upstream and reported fingerprint | `ProviderResponse`; `OpenRouterProvider.complete` | `OpenRouterMetadataTests.test_requested_observed_fingerprint_and_upstream_are_independent`; `test_missing_identity_is_unknown_and_model_is_never_revision` | Observations are provider claims; no immutable-build or endpoint attestation |
| #119 bounded malformed/absent metadata and adapter compatibility | Typed response validation and allowlisted parser | `OpenRouterMetadataTests.test_malformed_optional_identity_is_discarded_without_echoing`; `test_typed_observations_validate_bounds_and_provenance`; `ProviderConformanceTests.test_successful_complete_returns_provider_and_model` | Unavailable metadata remains unknown; no enrichment request |
| #120 complete/partial/absent/zero/malformed usage | `ProviderTokenUsage`; OpenRouter response parser | `OpenRouterMetadataTests.test_usage_complete_partial_absent_zero_and_malformed`; `test_typed_usage_rejects_invalid_counters`; `test_unparseable_oversized_integer_is_a_sanitized_provider_failure` | Counters cover successful responses only; failed response usage stays unknown |
| #120 failed/truncated/interrupted/correction accounting; honest aggregates | `MeasuredProvider.complete`; `provider_reported_usage` | `ProviderUsageAccountingTests.test_failed_cancelled_and_correction_attempts_are_counted_once`; `test_complete_usage_sums_and_partial_usage_does_not_fabricate_totals`; `test_aggregate_overflow_is_unknown` | Cancellation propagates; no fabricated usage, refunds or monetary totals |
| #119/#120 strict schema compatibility and live CLI serialization | Optional v1 `metrics.provider_reported_usage` | `HostedQualificationConfigurationTests.test_documented_evaluate_command_captures_the_effective_target_and_schema`; existing golden/negative evaluation-report fixtures | Old closed schema rejects new telemetry; update reader schema. Old report bytes still validate and retained evidence must not be rewritten |
| #121 bounded normalized family/variant/region/tier grammar; no fallback on rejection | `OpenRouterRoutingPolicy`; OpenRouter transport | `OpenRouterMetadataTests.test_endpoint_grammar_rejects_unsupported_syntax`; `test_specific_endpoint_is_preserved_with_strict_policy`; existing OpenRouter TLS, redirect, HTTP and bound tests | Syntax acceptance does not prove provider existence/capability; prefix matching may span eligible deployments |
| #118 selected hosted target, unknown-combination rejection, request/policy/configuration agreement | Explicit DeepSeek/Morph eligibility triple and existing hosted resolver | `HostedQualificationConfigurationTests.test_current_hosted_target_is_eligible_without_promoting_other_combinations`; `test_documented_evaluate_command_captures_the_effective_target_and_schema` | Morph is a family constraint. Current capabilities and three independent live passing runs remain pending #122. #115 runtime evidence loading remains separate |
| Installed wheel, public imports, packaged schemas | Wheel installed outside checkout; extracted sdist suite | `InstalledOpenRouterMetadataContractTests.test_installed_protocol_metadata_routing_and_eligibility` in `tests/dist_safe/test_openrouter_metadata_contract.py` | Full repository/CI gates and independent review remain integration-owner responsibilities |

The current candidate is `deepseek/deepseek-v4.1-flash` / `morph` at
`https://openrouter.ai/api/v1`. Its strict policy digest is
`27fbcad31a801f636838de7fe087bdd9bb73b5318416cd473a92b72eb2efe400`.
Other hosted combinations cannot inherit its eligibility or evidence. Historical
Haiku/Anthropic eligibility is preserved, without claiming passing qualification.

The fake CLI command uses a custom single clean case and now passes its stated
oracle after exact fixture checks are separated from live quality. It captures
one actual adapter request and validates model, strict policy, configuration
digest, operator-attested version, optional telemetry and fixture control.
This synthetic evaluator result is not live provider qualification.

Red evidence against the baseline adapter: the updated fingerprint regression
returned the model slug; endpoint tests rejected all three documented variants;
and usage was absent. Restoring the fix makes those tests pass. No raw provider
output, credentials, private prompts or source were used.

Focused command (credential-free; use an environment installed from this checkout):

```bash
python -m unittest tests.test_openrouter_metadata tests.test_openrouter \
  tests.test_provider_conformance tests.test_evaluation \
  tests.test_openrouter_qualification tests.test_schemas tests.test_cli \
  tests.test_registry tests.test_diagnostics_and_patches tests.test_provider_config -q
```

The initial response/routing run had 351 passing focused tests. Formatting,
Ruff, mypy, public-schema validation, JSON configuration validation, action pins,
compileall and sdist contract checks pass. The installed test runs with `-I`
from `/tmp` against the built wheel and extracted sdist. These are local
candidate artifacts, not a published release or proof that existing published
packages contain the change.


## Canonical routing ingress repair (#121)

The acceptance matrix also covers the public configuration ingress, rather than
only direct policy construction. `OpenRouterConfigurationTests` exercises the
actual YAML parser, resolver, `config validate`, offline `plan`, hosted
`host-plan` (including workflow output), registry and real OpenRouter adapter
with socket-free captured transport. Four synthetic suffixes across the three
published family mappings survive unchanged with the strict privacy/no-fallback
policy. Malformed and wrong-family values fail before adapter construction.
A retired environment value cannot replace canonical YAML routing. The original
source failed all four valid suffix cases at YAML parsing; subsequent acceptance
also exposed policy loss in the CLI and environment equality in the registry.

`InstalledOpenRouterMetadataContractTests.test_installed_canonical_configuration_preserves_endpoint_in_real_adapter`
adds the same canonical YAML → CLI → registry → real adapter path to the existing
installed/post-package dist-safe inventory. This adds three checkout test methods
and one installed contract method. Existing unlisted-model configuration remains
unchanged; suffixes do not become qualification targets. These test identifiers
are synthetic protocol cases, not claims about live endpoint availability.

The ingress repair's broader focused command passed 417 tests across
configuration, hosted planning, provider settings/registry, canonical CLI,
OpenRouter metadata, adapter/conformance, evaluation, qualification and schemas.
Ruff, formatting, mypy and `git diff --check` also passed on that source diff.

## Qualification routing evidence repair

Spec review of `19db65dded84bf53bd965a711a5f8409e3667eb5` reproduced a
family target accepting retained reports with a configuration digest computed
for `morph/variant`. Eligibility-constructor rejection alone did not bind report
routing. `QualificationRoutingEvidenceTests` now covers matching historical and
current synthetic controls; missing/mutated/suffix/old-upstream policy binding;
changed endpoint; and a loader supplied with altered artifacts whose retained
byte references were also updated. The live report producer emits the actual
policy digest/base URL; the mint and checked loader require their target match.
Old unbound reports remain schema-valid but cannot newly authorize support.

`InstalledOpenRouterMetadataContractTests.test_installed_live_report_binds_actual_routing_and_endpoint`
uses the packaged corpus and real CLI/adapter with fake transport. This exposed
and repaired a schema mismatch: live contract-rejection cases already emit
`skipped`, which the case-status enum omitted. Source execution of the three
installed-contract methods passes; final wheel execution remains with the
integration owner.

## Live evaluator applicability repair

`LiveEvaluationTests` uses the actual packaged corpus. Before the repair, an
oracle-perfect synthetic live provider achieved precision/recall/location 1 but
failed exact fixture identity, deterministic rejection and category coverage.
The new protocol runs the complete exact fixture inventory as a distinct local
control and scores actual live findings with the unchanged one-to-one oracle.
An adjacent denominator repair retains the full expected finding inventory;
empty findings now have recall 0 instead of 1.

Six acceptance methods prove three independently invoked synthetic perfect runs
can pass/check promotion; bad live quality cannot be rescued by a passing or
later fixture run; failed local conformance blocks perfect live quality; missing,
partial and contradictory controls fail closed; non-fixture sidecar identity
fails before live calls; and original thresholds/fixture output remain intact.
The report-bound promotion gate checks the composite controls. Source/installed
regressions do not qualify a provider or close #122's authorized live evidence.
Proposed ADR 0082 records schema migration, digests, assurance limits and rollback.
