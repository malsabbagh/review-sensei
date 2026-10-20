# Shared provider flow conformance

Baseline: `b9bf039c214b4f0ec5938b614eecd955c1e202dd`. This test-only change
qualifies a finite composition matrix using existing interfaces; it adds no
production provider abstraction, runtime refactor, permissions or support claim.
An ADR is unnecessary because no runtime/public/persistence contract changes.

`tests/provider_common_flow_contract.py` supplies one reusable contract.
Six generated configuration classes run the same nineteen methods, including
legacy and unified work-mode subcases. Adding a factory supplies registry
settings, explicit canonical backend/YAML/credential metadata, a constructor
transport seam and protocol envelope functions; the
scenario methods are shared. Generated classes are importable by exact test ID
for the existing sharded checkout runner.

All providers are the real production adapter classes created by the real default
registry. Only captured synthetic HTTP transport is injected. Socket connections
are denied for the shared scenarios. The supported finite configuration matrix is:

| Class prefix | Canonical CLI selection | Endpoint and credential |
| --- | --- | --- |
| `OllamaLocalDefault` | `local-ollama`, packaged `qwen3.5:4b` default | Packaged loopback endpoint; no Authorization header even with ambient provider keys present |
| `OllamaCloud` | `cloud-ollama`, packaged `deepseek-v4.1-flash:cloud` default | Packaged Ollama HTTPS endpoint; selected `OLLAMA_API_KEY` |
| `OllamaCloudGLM` | `cloud-ollama`, explicit `glm-5.3:cloud` YAML | Same cloud endpoint and selected credential |
| `OpenAIHTTPS` | `openai-compatible`, packaged `gpt-4o-mini` default | Packaged OpenAI HTTPS endpoint and selected `OPENAI_API_KEY` |
| `OpenAICustomHTTPS` | `openai-compatible`, packaged model default | Explicit permitted custom HTTPS endpoint and selected `CUSTOM_OPENAI_KEY`, distinct from ambient `OPENAI_API_KEY`; removing endpoint permission refuses before transport |
| `OpenRouterStrictRouting` | `openrouter`, packaged DeepSeek default | Packaged HTTPS endpoint, selected `OPENROUTER_API_KEY`, unchanged strict `morph` policy |

Both approved cloud slugs run every common lifecycle method, not only a settings
probe. The extra existing settings probe remains as a separate small control.
These are configuration conformance observations, not model-quality claims.
Shared engine journeys use explicit supported library settings (five-second
adapter cap and 128 output tokens); the canonical public CLI probe uses its own
actual resolved defaults, rather than inventing corresponding CLI flags.

## Acceptance matrix

Method names below omit the `test_` prefix. Each row applies identically to all
six generated configuration classes. Backend selection is explicit case data,
never inferred from an Ollama-shaped response envelope. The local/default-model
and default HTTPS CLI probes omit model overrides to exercise packaged defaults.

| Boundary | Shared method evidence | Limit |
| --- | --- | --- |
| Actual public CLI configuration reaches actual registry/adapter | `public_cli_canonical_settings_reach_the_real_adapter` | Synthetic clean review per canonical backend; no hosted CLI configuration cross product |
| Clean/blocking results, enforced check and approval facts | `complete_clean_and_blocking_results_agree_with_approval_and_check` | OpenRouter still withholds auto-approval for missing qualification even when the findings check passes; other obligations remain enforced |
| Stage deadline and output ceilings | `stage_calls_share_original_deadline_and_transport_output_cap` | Each adapter observes original 1.0 then 0.75-second allowance and at most configured 128 output tokens; elapsed transport is deterministic clock input |
| Physical transient retry and JSON correction | `real_transport_retry_and_json_correction_share_original_allowance` | Three actual transport attempts receive 1.0/0.75/0.5 seconds, one retry and one correction |
| Call, prompt and elapsed-time exhaustion | `retry_and_correction_cannot_bypass_provider_call_ceiling`, `prompt_budget_exhaustion_refuses_before_real_transport`, `expired_or_exhausted_deadline_has_no_fresh_dispatch` | No extra dispatch or passing/approvable incomplete result |
| Malformed output and cancellation | `invalid_review_output_stays_blocked_and_is_not_echoed_to_correction`, `keyboard_interrupt_does_not_become_clean_review_or_retry` | Correction omits invalid response content; cancellation propagates without another transport call |
| Full fallback retains an omitted prior blocker | `full_fallback_from_real_discovery_retains_omitted_concern` | Actual adapter discovery plus public baseline planner/reconciler and approval/check facts; durable CLI baseline recovery remains the separate baseline suite |
| Accepted hosted computation replay | `accepted_hosted_computation_replays_real_adapter_result_without_dispatch` | Real service output retained through the production comment operation store and reconstructed store; exact result and accounting replay with one transport call. App authority and comments are synthetic, not live GitHub delivery |
| Human reassessment and stale obligations | `human_reassessment_correction_valid_evidence_and_stale_refusal` | Literal evidence validation and correction through actual adapter, resolution/facts round trip, stale base/head rejection; trusted facts projection does not replace GitHub actor authorization/publication tests |
| Ordinary discussion | `ordinary_discussion_uses_same_adapter_and_bounded_correction` | Existing public discussion API, strict reply validation and two-attempt correction. Deadline behavior is qualified by the separate discussion deadline change/suite |
| Unsupported expanded capability | `actual_adapter_does_not_inherit_unverified_expanded_capability` | The default registry adapters do not silently acquire a qualified completion contract; capability matching refuses before transport |

The two `OllamaApprovedSettingsTests` additionally distinguish the public registry
defaults from the explicit library-only `require_completion_metadata=True` mode.
The strict constructor accepts complete exact-model metadata and refuses missing
metadata. This does not expose a new CLI flag or imply the other adapters provide
that strict contract.

## Execution and packaging

The checkout wrapper is `tests/test_provider_common_flow.py`. The guarded installed
wrapper is `tests/dist_safe/test_provider_common_flow_contract.py`, requiring both
engine and adapter imports under the installed prefix. The helper is declared in
`MANIFEST.in` and `tests/fixtures/distribution-contract.json`; the installed wrapper
is selected by `tests/published_cli/lifecycle.py` for post-package qualification.

Focused source execution passed all 86 common-flow methods. Source-loader checks
of the installed wrapper are not installed-artifact qualification. No wheel build,
installation, live provider call or hosted mutation is part of this delegated
change. Integration must run the clean sdist-extracted wheel lanes, post-package
inventory and complete required gates on the combined exact head. Live/deployed
capabilities, provider quality, hosted 64/60 receipts and the full configuration
cross product remain separate evidence requirements.

The original three-configuration suite passed 44 common methods and 135 methods spanning common flows,
distribution lanes, direct provider conformance/completion guards, operation
entry and review work; the published qualification bootstrap passed 27 methods.
The original checkout sharder executed the exact 44-method inventory in two
workers (28/16 assigned and executed, both exit zero), with
`execution_complete: true`. Changed-file Ruff format/lint and diff checks passed.

The expanded six-configuration suite reruns the same methods (86 total including
the two existing Ollama controls). Its exact checkout sharder receipt discovers
86 methods, assigns/executes 44 and 42 in two workers, and reports both exits zero
with `execution_complete: true`. The common/configuration/registry/distribution
focused command passes 193 methods in an environment without inherited provider
settings. Earlier attempts inherited retired shell settings and failed existing
configuration assertions; no product change was made to bypass those errors.
Source-loader execution of the dist-safe wrapper also passes 86 methods; its
deliberate checkout-import opt-out makes this a loader check, not installed-wheel
evidence. Earlier three-configuration receipts are not reinterpreted as broader
coverage. Installed and deployed execution remain integration gates.

## Expanded installed ingress observation

The preceding 86-method observations identify the earlier helper. Frozen source
`28ceeeae9203ca6942fb8fe733f96c0aac82f499` expands it to 116 methods with
the five additional ingress/control methods described in
[the provider-flow matrix](provider-flows.md). The
[exact expanded receipt](provider-ingress-candidate.json) records 3,741 checkout
tests, four complete coverage workers at 82.77% (unchanged 80% floor), and all 16
required local gates. All 871 tracked build inputs matched the Git archive.

The exact installed wheels passed 234 dist-safe tests, 11 downstream tests and 18
OpenAI configuration cases outside checkout. All six post-package suites passed
1,281 cases, including 199 lifecycle methods. The sdist contract checked 48
required entries and 10 checkout-only exclusions; all 119 engine and 48 adapter
runtime files matched the frozen wheels and Git bytes. These observations use
synthetic external authorization/HTTP, not a live broker or provider. They do
not qualify the remaining event cross-product, unknown-effect recovery or the
live/deployed rows in the remaining-work map. This receipt/documentation update
changes none of the tested runtime or harness bytes.
