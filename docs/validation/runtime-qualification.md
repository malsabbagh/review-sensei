# Runtime qualification binding acceptance (#115)

This candidate extends base `ce18fbb46ba0eae997bbab00e8bd66423f8d7e5d`
(PR #316). It implements [ADR 0083](../adr/0083-trusted-runtime-openrouter-qualification.md).
All reports and HTTP in the controls are synthetic. This is deterministic API
acceptance, not live provider or deployed qualification.

## Reproduction and implementation

The baseline has no positive trusted runtime authority API. Its public
standalone finalizer accepts caller-built clean eligibility without any
App-owned eligibility readback when `require_persisted=False`: a captured run
against the exact baseline adapter emitted `approved` and one `APPROVE`.
The same public-seam reproduction now emits `approval_withheld` and no review
event. The new positive acceptance initially failed because the authority API
was absent, then passed after retained-evidence integration.

`review_sensei_github.qualification.TrustedOpenRouterQualification` snapshots
bounded raw evidence and independent host expectations and reuses
`load_supported_openrouter_qualification` on every use. It explicitly separates
evaluation configuration from actual runtime transaction configuration. The
publisher derives a receipt from checked authority and exact repository/PR/
base/head/result identities. Facts v2 retain provider plus that bounded receipt.
Every finalizer requires matching App-owned persisted provenance, even when a
caller requests the old False flag, and rechecks OpenRouter authority before
thread reads and at the approval write boundary.

## Acceptance matrix

Methods below are in `tests.test_github_trusted_qualification.TrustedQualificationTests`.
Existing accepting-string negatives remain in
`tests.test_github_qualification_boundary.QualificationBoundaryTests`.

| Condition | Exact methods / evidence | Outcome |
| --- | --- | --- |
| Checked independent evidence positive | `test_checked_matching_retained_evidence_can_approve`; `test_named_profile_is_bound_by_effective_context_without_status_trust` | COMMENT + APPROVE; bounded facts v2; no report contents persisted |
| Missing context / status strings | `test_missing_or_mismatched_runtime_context_and_caller_claims_withhold`; existing boundary tests | COMMENT only; no accepting string grants proof |
| Missing/duplicate/fixture/mutated evidence and independent digest/attestation mismatch | `test_authority_rejects_missing_duplicate_fixture_and_mutated_evidence` | Authority construction fails closed |
| Exact target/endpoint/policy/suffix | `test_runtime_target_endpoint_policy_and_suffix_do_not_inherit_support` | Cross-target, custom endpoint, fallback and suffix inheritance rejected |
| Current context snapshot / actual publication mode | `test_authority_snapshots_inputs_and_detects_policy_mismatch`; `test_authority_changed_during_reads_is_rechecked_before_approval_write` | Original immutable context works; changed actual context/mode withholds |
| Application and model-free recovery | `test_application_and_model_free_recovery_use_the_same_trusted_context` | Matching context approves; missing recovery context withholds; check reflects actual review |
| Separate finalization | `test_separate_finalizer_rechecks_matching_retained_evidence`; `test_delayed_finalizer_withholds_missing_changed_and_forged_binding` | Matching App-owned readback + authority approves; changed runtime, receipt, result or PR withholds |
| Replay / interrupted approval delivery | `test_replay_rechecks_proof_and_does_not_upgrade_unbound_publication`; `test_repeated_bound_delivery_reconciles_existing_approval_without_post` | Same bound publication can finish; missing context or old unbound publication cannot upgrade |
| Independent provider provenance | `test_detached_caller_cannot_forge_v2_provider_exemption_without_app_readback`; `test_first_publication_requires_its_actual_app_owned_readback` | Caller v2 provider claim or False flag cannot bypass missing/different App-owned readback |
| Legacy compatibility | `test_legacy_accepting_facts_cannot_authorize_delayed_finalization`; `test_versioned_provider_facts_roundtrip_and_legacy_does_not_gain_exemption` | Legacy v1 readable and withheld; strict v2 provider/binding validation |
| Other gates remain conjunctive | `test_qualified_evidence_never_overrides_partial_or_blocking_obligations`; `test_live_open_thread_and_stale_base_still_withhold_qualified_review` | Partial, blocking, open-thread and stale-base review cannot approve |
| Compatible policies | `test_new_non_openrouter_and_opt_out_behavior_remains_supported`; `test_constructor_rejects_boolean_or_duck_typed_qualification_authority` | New Ollama/compatible approvals and opt-outs retained; unchecked authority objects rejected |
| Authenticated same-head recovery | `tests.test_explicit_rescan.RescanPublicationTests.test_complete_reanalysis_can_pass_existing_approval_checks` and existing rescan suite | Previously incomplete authorized analysis can publish and pass independent readback; no complete-legacy upgrade authority |
| Package-safe positive | `tests/dist_safe/test_openrouter_runtime_qualification_contract.py::InstalledRuntimeQualificationTests.test_checked_packaged_corpus_evidence_binds_publisher_and_delayed_finalizer` | Real packaged corpus, three independent synthetic evaluator invocations, independently computed expected identities, public publication + parsed delayed finalization |

## Validation and limits

Final focused source checks passed: Python 3.14 GitHub discovery (570 tests),
lifecycle/finalizer/human/rescan/default-gate suites (184 tests), Python 3.11
source compatibility (201 tests), Ruff formatting/lint, mypy (112 source files),
JSON contracts and `git diff --check`. The new runtime boundary class has 20
public-seam methods. The package-safe test has also run against source with the
explicit checkout-import opt-out; that is not an installed-wheel receipt.
The [exact-source receipt](runtime-qualification-candidate.json) binds frozen
source `93fcb75653bb6c252e58ba05570f009809ab4dd5`: all 16 local gates,
3,781 normal tests, four complete coverage workers and 82.83% coverage pass.
The exact installed engine/adapter pass 237 distribution tests, 11 downstream
tests, 18 OpenAI configuration tests and all 1,284 post-package cases, including
202 lifecycle cases. The duplicate/unknown recorded-ID regression also passes
against those installed bytes. All 880 tracked inputs and 119/49 runtime files
were checked. This later receipt/documentation commit changes no tested runtime
or harness bytes; CI and hosted review remain separate evidence.

The fake GitHub fixture opts into server state: a successful review POST is
returned by later review-list reads. Its default remains off, so missing or
conflicting readback controls cannot accidentally gain provenance. Detached
positive fixtures explicitly seed App-owned eligibility. Legacy promotion tests
now assert withholding where no persisted provider/result authority exists.

A trusted host must explicitly select the evidence inventory, independently
expected evaluation inputs, actual stages/limits and runtime configuration.
Default CLI/hosted composition has no inferred evidence authority and withholds
OpenRouter approval. The supported receipt is not a report signature and does
not prove producer honesty or immutable provider alias revisions. Single-provider
stages are covered; heterogeneous stage-profile qualification is not claimed.
Complete unbound legacy publications need a new-head bound review; the existing
authenticated incomplete-rescan path keeps its own limited authority. Real live
qualification (#122), operational installed/deployed acceptance (#123), release
and rollout remain separate gates. No external provider, credential, release,
deployment or permission change is part of this candidate.
