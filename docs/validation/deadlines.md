# Provider deadline acceptance (#314)

Scope: source based on `7a641b8b6402292f213159853400aff4f2e9a3d9`, with the
focused deadline repair. The reusable contract is
`tests/provider_deadline_contract.py`; checkout discovery uses
`tests/test_provider_deadlines.py`, and the sdist-installed lane uses
`tests/dist_safe/test_provider_deadline_contract.py` with the distribution import
guard. These tests use synthetic inputs, injected clocks/transports, and a real
localhost HTTP transport. They send no credentials and perform no model inference.

| Criterion | Exact contract methods / existing evidence | Expected observation |
| --- | --- | --- |
| Positive finite dispatch timeout, intersected ceilings | `test_adapters_bound_current_timeout_before_transport`; `test_remaining_allowance_does_not_round_up_submillisecond_time` | Ollama, OpenAI-compatible and OpenRouter receive 0.125s for a 0.125s request within a 0.25s configured limit, and 0.25s for a 1s request. Invalid mutable timeouts refuse before transport. Fractional milliseconds are not rounded up. |
| Original deadline across stages, batches, retries and corrections | `test_stages_share_original_deadline_in_both_work_modes`; `test_multiple_discovery_batches_share_original_deadline`; `test_transport_retry_and_correction_share_original_deadline` | Both work modes propagate 1s then 0.75s; transport failure plus 0.125s backoff and syntax correction receive 1s, 0.75s, 0.5s. Unified batches share the tracker. |
| Expiry before dispatch, including durable callback latency | `test_expired_review_never_dispatches`; `test_durable_dispatch_callback_cannot_refresh_or_outlive_deadline` | No dispatch at expiry. A callback consuming 0.25s reduces actual dispatch to 0.75s; a 1s callback prevents dispatch while retaining the saved reservation and charge conservatively. |
| Distinct deadline / shorter transport failure | `test_timeout_after_original_deadline_has_deadline_diagnostic`; `test_shorter_transport_timeout_keeps_retry_diagnostic`; `test_human_timeout_distinguishes_original_deadline_from_transport_limit` | Expired original allowance reports `deadline_exceeded`, including zero retries and nontransient errors. Earlier retry exhaustion stays `transport_retry_exhausted`. Human assessment preserves transport failure before expiry and deadline diagnosis at expiry. |
| Actual blocking transport | `test_real_ollama_socket_obeys_remaining_review_deadline` | A withheld localhost response with a 10s adapter setting returns within 1.5s under a 150ms total budget; no clean result is returned. |
| Outside-checkout installed public CLI | `test_fresh_cli_uses_total_deadline_with_longer_transport_limit` | Isolated fresh CLI uses the tested installed package, clean environment and temporary working directory; 1s total versus 10s transport exits 2 within 2.5s, emitting partial coverage and a deadline reason. |
| Interruption | `test_keyboard_interrupt_propagates_without_clean_result_or_retry`; `test_fresh_cli_interrupt_and_termination_exit_cleanly` | No fabricated success or retry. Fresh POSIX CLI SIGINT and SIGTERM exit 130 with a sanitized cancellation message and no traceback/output result. Existing durable cleanup remains owned by its lifecycle tests. |

The original legacy service omitted the request timeout: the synthetic localhost
reproduction took 3.008s with a 150ms budget before this repair. Its deterministic
stage requests recorded `None` instead of 1s and 0.75s. The unified executor
already propagated a remaining timeout, but its durable callback could consume
the allowance after that value was computed; the new regression reproduced both
an inflated timeout and a call after expiry. Tests were made green by passing the
remaining allowance at legacy dispatch, fencing unified dispatch again after its
callback, preserving fractional-clock precision, and consistently classifying
expired provider errors. The adapters now validate their mutable timeout before
applying the request minimum.

Run the checkout contract with:

```sh
PYTHONPATH=src:packages/python/review-sensei-github/src python -m unittest discover -s tests -p test_provider_deadlines.py -v
```

Build/install the candidate wheel as described in `CONTRIBUTING.md`, extract its
sdist tests, change outside the checkout, and run:

```sh
REVIEWSENSEI_CHECKOUT_ROOT=/absolute/checkout /isolated/venv/bin/python -I -m unittest discover -s /unpacked/sdist/tests/dist_safe -p test_provider_deadline_contract.py -v
```

The focused contract has 14 methods (with additional mode/provider/signal
subcases). A candidate wheel and extracted sdist passed the installed lane on
macOS/Python 3.14; retain the final artifact SHA-256 values with the integration
receipt. The integration owner runs the complete required gate sequence and
post-package inventory on its final combined head. Candidate success does not
qualify previously published same-version bytes, native packages, Windows signal
handling, live providers, GitHub deployment, or rollout/rollback.

The deadline bounds transport dispatch and prevents accepting late results. It
is not a promise to preempt arbitrary Python adapters, a DNS resolver, or all
operating-system scheduling delays. urllib socket timeout semantics still apply
to individual blocking operations. This repair enforces the existing provider
request/resource contracts without a new schema, public persistence contract,
resource ceiling or approval policy; no ADR is needed for that correction.
