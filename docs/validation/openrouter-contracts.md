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

The fake CLI command intentionally returns an evaluation failure because the
synthetic corpus pins fixture identity. It still captures one actual adapter
request and validates its effective model, strict policy, configuration digest,
operator-attested version and optional telemetry. A serialized request is not
passing quality evidence.

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

The current deterministic matrix has 351 passing focused tests. Formatting,
Ruff, mypy, public-schema validation, JSON configuration validation, action pins,
compileall and sdist contract checks pass. The installed test runs with `-I`
from `/tmp` against the built wheel and extracted sdist. These are local
candidate artifacts, not a published release or proof that existing published
packages contain the change.
