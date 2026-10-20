# Ordinary discussion deadline acceptance

Baseline: `b9bf039`. Scope: the public `ConversationService.reply` and its
ordinary GitHub discussion caller, without model inference or remote writes.

The baseline service constructed requests with no remaining timeout. Its ordinary
GitHub branch also ignored the supplied resource budget: an expired context
preparation could still generate and publish a reply. Two public acceptance
cases reproduced these failures against the baseline, then passed with this fix.

The service now uses the existing generic executor and an optional original
`ResourceBudgetTracker`, with the documented default profile when omitted.
The GitHub caller creates its tracker before context preparation and passes the
same instance through operation entry and generation. Existing provider transport
settings remain ceilings; no provider-specific branch or qualification is added.

| Acceptance | Exact tests | Evidence |
| --- | --- | --- |
| Public default request has finite deadline | `ConversationDeadlineContractTests.test_default_public_reply_receives_a_finite_request_deadline` | Default requests carry a positive timeout no greater than the 120s default profile |
| Three actual adapters share original allowance and configured output cap | `test_three_real_adapters_share_deadline_and_limits_across_correction` | Ollama, OpenAI-compatible and OpenRouter captured transports receive 1s then 0.6s and the same 256-token configured ceiling |
| Expired, call/prompt/output-exhausted work refuses before dispatch | `test_expired_and_exhausted_budgets_never_dispatch`; `test_correction_cannot_refresh_call_or_output_allowance`; `test_correction_cannot_refresh_aggregate_prompt_allowance` | Closed diagnostic, charged attempts retained, no new dispatch or reply |
| Retry/backoff and correction share one tracker | `test_transport_retry_and_correction_share_one_original_deadline` | Three dispatches receive 1s, 0.5s, 0.3s; one transport retry and one correction counted |
| Late response, cancellation, malformed or semantic failure cannot become a reply | `test_response_after_deadline_cannot_publish_or_retry`; `test_cancellation_propagates_without_retry_and_preserves_attempt`; `test_provider_failure_is_sanitized`; `test_parser_safety_failure_is_sanitized_and_bounded` | No late result, cancellation propagates, provider/parser errors stay bounded and omit raw text |
| Existing correction behavior remains narrow | `test_semantic_reply_failure_never_requests_structural_correction`; `test_correction_without_append_space_keeps_original_prompt_bounded`; existing `tests.test_conversation` | Semantic failure makes one call and zero corrections; JSON/non-object correction remains at most one; a full prompt retains the prior bounded fallback |
| GitHub preflight and operation latency consume original deadline | `GitHubApplicationTests.test_ordinary_discussion_context_and_operation_consume_original_deadline`; `test_ordinary_discussion_expired_during_context_never_generates_or_publishes` | Context plus operation latency reduces requests to 0.5s/0.3s; expiry during context produces no provider call or final reply |
| Caller cleanup and failed generation remain safe | `test_ordinary_discussion_call_exhaustion_and_cancellation_never_publish`; existing reaction/error and operation-replay tests | No generated publication after failure; processing reaction removed; operation authority is unchanged |

The shared 12-method engine acceptance helper is included in the sdist contract,
wrapped by checkout and installed dist-safe tests, and included in the existing
post-package lifecycle inventory. The three additional GitHub caller methods
exercise the public application with synthetic authenticated-context, broker and
publisher edges; they are not evidence of deployed GitHub delivery.

Focused source validation passed 197 tests covering conversation, caller,
inference wiring, human reassessment, package/CI contracts and schemas, plus the
27 post-package bootstrap tests. Formatting, Ruff and mypy passed for both engine
and hosted sources. The full combined source/coverage and frozen artifact gates
remain integration-owner checks. No live provider or hosted qualification is
claimed, and no approval, replay, grant, permission or persistence contract is
expanded.
