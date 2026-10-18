# Provider interface and flow matrix

The common interface already exists on main `7a641b8`; PR315/316 repair behavior
and extend acceptance rather than introduce another provider interface.
`ReviewProvider` in `src/review_sensei/providers/base.py` requires `name`, `model`
and `complete(ProviderRequest) -> ProviderResponse`. The registry constructs and
validates that protocol. `ReviewService`, `ConversationService` and
`HumanAssessmentService` consume it. The shared engine owns obligations,
coverage, recovery, deadlines, retries and budgets; the GitHub application and
publisher own exact-head authorization, delivery and approval. Adapters own
authentication, endpoint/TLS rules, protocol mapping, bounded reads and normalized
transport errors.

`tests/provider_common_flow_contract.py` runs the same 14 methods for each
configuration below, with two additional Ollama settings/completion controls.
Only factory settings, captured transport and protocol envelopes vary. The
checkout wrapper, guarded dist-safe wrapper and default post-package inventory
reuse that implementation; the final exact-byte receipt records execution.

| Configuration factory | CLI/registry | Review, learning and recovery | Discussion and human assessment | Approval/delivery policy |
| --- | --- | --- | --- | --- |
| Local Ollama: packaged qwen model, loopback `/api` | Packaged defaults; no Authorization despite ambient cloud/API keys | Same legacy/unified service and bounded execution | Same services | Same exact-head and obligation gates |
| Ollama cloud: DeepSeek4.1Flash | Canonical cloud configuration, selected key | Same common methods | Same common methods | Same gates; no separate cloud lifecycle |
| Ollama cloud: GLM5.3 | Explicit model YAML, selected key | Same common methods | Same common methods | Same gates |
| OpenAI-compatible: default HTTPS API | Packaged endpoint/model, selected key | Same common methods | Same common methods | Same gates |
| OpenAI-compatible: custom HTTPS | Explicit permission and selected custom credential; missing permission refuses before transport | Same common methods | Same common methods | Same gates; no credential borrowing |
| OpenRouter: canonical HTTPS, strict Morph routing | Dedicated key and immutable privacy/no-fallback policy | Same common methods | Same common methods | Same gates plus independent qualification; unchecked approval stays withheld |

Common methods exercise the actual registry and adapters through canonical CLI,
legacy/unified review, blocking/clean result classification, corrections,
transport retries, original shrinking deadlines, prompt/call ceilings,
cancellation, baseline omission retention, accepted hosted-computation replay
with a reconstructed comment store, current/stale human evidence, and ordinary
discussion. They do not prove every hosted event/control combination.
`conversation_deadline_contract.py` additionally exercises the original caller
tracker through context preparation, operation latency and discussion generation;
all three real adapters receive its remaining timeout and shared limits.

The audit found ordinary discussion had no request deadline/tracker even though
normal review and human reassessment used shared budgets. The correction uses
the existing executor and original tracker, including correction/output
accounting, without a provider-name branch or a second retry policy. Replay,
head fencing and approval remain independent requirements.

Capabilities and qualification remain explicit. `ProviderCapabilities` in
`budgets.py` binds qualified capacity to provider, model, endpoint and completion
contract; an unknown or spoofed claim cannot expand admission. A profile label
does not create that capability. Strict Ollama completion is an explicit library
opt-in, distinct from CLI defaults. OpenAI-compatible requires HTTPS: HTTP
localhost is unsupported by its adapter. OpenRouter accepts its canonical host
and strict policy; a routing suffix is not endpoint capability or inherited
qualification. Unsupported permission, capability and credential combinations
must fail clearly before transport.

To add a provider, implement the existing protocol and normalized errors,
register its settings factory, and expose its supported configuration,
credential and endpoint rules explicitly. Supply its protocol envelope and
transport fixtures to the reusable common-flow cases and direct transport
conformance tests. Run the same checkout, installed and post-package acceptance.
Declare truthful completion/capability metadata. No adapter implements approval,
head fencing, obligation resolution, replay or a fresh resource allowance.
CLI/workflow backend allowlists still need explicit updates; this is a shared
engine, not unrestricted automatic discovery. DecisionProvider #161 is separate.

## Evidence boundaries

Captured transports and loopback fixtures are deterministic evidence. Existing
reply-inference tests that replace factories/application prove ingress wiring,
not a complete remote journey. Installed acceptance must use the exact built
wheels outside checkout; source execution is not a substitute. Synthetic ports
and signed synthetic OIDC do not establish deployed App/broker/Actions behavior.

The authorized DeepSeek4.1Flash CLI review of public diffs is limited live
evidence. The GLM5.3 review timed out and supplies no passing live result. This
task performed no new local-model qualification and has no supplied live
OpenAI/OpenRouter authorization/configuration or passing run evidence. Three
independent synthetic evaluation invocations prove control/quality and retained
report gates; they are not the three real provider reports required by #122.
Positive runtime OpenRouter report binding remains separate under #115.

Rich inventories, public fair queues, joint retention/deployed readers, complete
original physical-service budget accounting, all final native artifacts and
named deployed upgrade/drain/rollback remain the separate limits in
[the lifecycle matrix](remaining-lifecycle.md). No threshold, coverage cap or
approval requirement is weakened to qualify a provider.

## Integrated candidate observation

The [exact-source receipt](combined-lifecycle-provider-candidate.json) records
unpublished source `b2bf03400788ad2a507cecded65b2b912c3cd2e3`, including the
PR315 storage and duplicate replay fixes. All 3,710 checkout tests passed in
normal discovery and four disjoint coverage workers, with 82.71% coverage and
the unchanged 80% floor. All required static gates passed. The frozen archive
matched 868 tracked inputs; both installed packages matched every wheel/Git
runtime byte outside checkout. The sdist checker found 47 required entries and
excluded all 10 declared checkout-only modules.

Installed discovery passed 204 tests, downstream consumers 11, and the separate
OpenAI configuration suite 18. All six post-package suites passed 1,251 cases,
including the nine-file lifecycle union's 169 methods. The same real-codec
duplicate/unknown identity regression additionally passed against both exact
installed packages, with live network disabled. It is recorded separately from
the default lifecycle count. Later receipt/documentation commits do not change
these tested runtime or test bytes. These observations retain every evidence
boundary above; they do not grant provider support or hosted approval.
