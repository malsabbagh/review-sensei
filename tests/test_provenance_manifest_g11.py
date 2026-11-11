"""G11: Source, installed-version, and runtime provenance manifest tests."""

from __future__ import annotations

import importlib.metadata
import io
import json
import unittest
from contextlib import redirect_stderr
from dataclasses import replace

from review_sensei.errors import ReviewInputError
from review_sensei.provenance_manifest import (
    ATTEMPT_EVIDENCE_SCHEMA_VERSION,
    MISSING_BINDING,
    PROVENANCE_MANIFEST_SCHEMA_VERSION,
    UNKNOWN_PROVENANCE,
    AttemptBudgetSummary,
    AttemptEvidencePacket,
    GateObservations,
    LifecycleReceipts,
    NoDuplicateWorkAssertion,
    OriginalInputBindings,
    ProvenanceCompatibilityReport,
    ProvenanceManifest,
    RecoveryPrescription,
    SourceEventIdentity,
    UncertainDispatchRecord,
    build_attempt_evidence_packet,
    classify_provenance_compatibility,
    inspect_installed_package_versions,
    prescribe_recovery,
)
from review_sensei.release_manifest import Artifact, CompatibilityManifest


def _complete_manifest() -> ProvenanceManifest:
    return ProvenanceManifest(
        repository="malsabbagh/review-sensei",
        repository_id=123456,
        pull_request=421,
        main_sha="e" * 40,
        base_ref="main",
        base_sha="a" * 40,
        head_sha="b" * 40,
        source_event=SourceEventIdentity(
            event_kind="pull_request",
            source_id=987654,
            source_revision="c" * 40,
            source_digest="1" * 64,
        ),
        caller_workflow=".github/workflows/review-sensei-review.yml@refs/heads/main",
        reusable_workflow_ref="malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@v5",
        reusable_workflow_tag_object="a72527097205110337a6841a545f548610c7fbaa",
        reusable_workflow_peeled_commit="d626185fff387dab14aedd48b2b0684e92c84739",
        installed_core_version="0.8.18",
        installed_hosted_version="0.3.24",
        selected_executor="github-actions:a72527097205110337a6841a545f548610c7fbaa",
        broker_route="/github/session-grant",
        broker_revision="12345678-1234-1234-1234-123456789abc",
        frozen_policy_commit="d" * 40,
        frozen_policy_sha256="2" * 64,
        gates=GateObservations(
            routing_preflight="passed",
            authenticated_live_validation="passed",
            broker_enforcement="passed",
        ),
    )


def _compatibility_manifest() -> CompatibilityManifest:
    return CompatibilityManifest(
        release="0.8.18",
        workflow_commit="d626185fff387dab14aedd48b2b0684e92c84739",
        workflow=Artifact("review-sensei-run.yml", "0.8.18", "3" * 64),
        python=Artifact("review-sensei", "0.8.18", "4" * 64),
        npm=(Artifact("@review-sensei/cli", "0.8.18", "5" * 64),),
        schemas_version="1.0.0",
        worker=Artifact("review-sensei-worker", "0.3.24", "6" * 64),
        compatible_worker_range=">=0.3.20 <0.4.0",
        provenance="https://github.com/malsabbagh/review-sensei/attestations/1",
        provenance_kind="github-artifact-attestation",
    )


class ProvenanceManifestG11Tests(unittest.TestCase):
    def test_complete_manifest_distinguishes_all_identities_and_round_trips(
        self,
    ) -> None:
        manifest = _complete_manifest()
        self.assertTrue(manifest.has_complete_runtime_identity)
        self.assertEqual(manifest.unknown_fields, ())
        self.assertEqual(len(manifest.known_fields), 25)
        self.assertNotEqual(
            manifest.reusable_workflow_tag_object,
            manifest.reusable_workflow_peeled_commit,
        )
        self.assertNotEqual(manifest.main_sha, manifest.reusable_workflow_peeled_commit)
        self.assertFalse(manifest.authority)
        self.assertFalse(manifest.grants_execution_authority)
        self.assertFalse(manifest.grants_recovery_authority)

        serialized = manifest.to_dict()
        self.assertEqual(
            serialized["schema_version"], PROVENANCE_MANIFEST_SCHEMA_VERSION
        )
        self.assertFalse(serialized["main_sha_is_runtime_proof"])
        self.assertEqual(ProvenanceManifest.from_dict(serialized), manifest)

        local_manifest = replace(
            manifest, broker_route="none", broker_revision=UNKNOWN_PROVENANCE
        )
        self.assertTrue(local_manifest.has_complete_runtime_identity)

        tampered = dict(serialized)
        tampered["installed_core_version"] = "0.8.19"
        with self.assertRaises(ReviewInputError):
            ProvenanceManifest.from_dict(tampered)

    def test_missing_telemetry_uses_unknown_without_network_or_retries(self) -> None:
        default_manifest = ProvenanceManifest()
        self.assertFalse(default_manifest.has_complete_runtime_identity)
        self.assertEqual(len(default_manifest.unknown_fields), 25)
        self.assertEqual(default_manifest.known_fields, ())
        self.assertEqual(
            ProvenanceManifest.from_dict(default_manifest.to_dict()),
            default_manifest,
        )

        calls: list[str] = []

        def fake_lookup(dist: str) -> str:
            calls.append(dist)
            if dist == "review-sensei":
                return "0.8.20"
            raise importlib.metadata.PackageNotFoundError(dist)

        core_ver, hosted_ver = inspect_installed_package_versions(
            version_lookup=fake_lookup
        )
        self.assertEqual(core_ver, "0.8.20")
        self.assertEqual(hosted_ver, UNKNOWN_PROVENANCE)
        # Exactly one local lookup per package; never retried.
        self.assertEqual(calls, ["review-sensei", "review-sensei-github"])

        bad_core, bad_hosted = inspect_installed_package_versions(
            version_lookup=lambda _dist: "not-a-semver"
        )
        self.assertEqual((bad_core, bad_hosted), ("unknown", "unknown"))

        incomplete_hosted = replace(
            _complete_manifest(), broker_revision=UNKNOWN_PROVENANCE
        )
        self.assertFalse(incomplete_hosted.has_complete_runtime_identity)

    def test_gates_remain_strictly_distinct_and_non_substitutable(self) -> None:
        routing_only = GateObservations(
            routing_preflight="passed",
            authenticated_live_validation="failed",
            broker_enforcement="not_run",
        )
        self.assertFalse(routing_only.routing_proves_live_validation)
        self.assertFalse(routing_only.live_validation_proves_broker_enforcement)
        self.assertFalse(routing_only.broker_proves_live_validation)
        self.assertTrue(routing_only.has_routing_pass_with_live_refusal_or_unknown())
        self.assertFalse(routing_only.all_required_gates_confirmed())

        local_gates = GateObservations(
            routing_preflight="not_run",
            authenticated_live_validation="passed",
            broker_enforcement="skipped",
        )
        self.assertFalse(local_gates.all_required_gates_confirmed(require_broker=True))
        self.assertTrue(local_gates.all_required_gates_confirmed(require_broker=False))

    def test_compatibility_classifier_catches_version_source_policy_and_broker_drift(
        self,
    ) -> None:
        manifest = _complete_manifest()
        compat = _compatibility_manifest()

        clean = classify_provenance_compatibility(
            manifest,
            expected_tag_object="a72527097205110337a6841a545f548610c7fbaa",
            expected_hosted_version="0.3.24",
            expected_policy_commit="d" * 40,
            compatibility_manifest=compat,
        )
        self.assertEqual(clean.status, "compatible")
        self.assertEqual(clean.mismatched_fields, ())
        self.assertEqual(clean.missing_fields, ())
        self.assertIn("main_sha_differs_from_executing_workflow_commit", clean.notes)
        self.assertIn("broker_revision_is_opaque_deployment_uuid_or_sha", clean.notes)
        self.assertEqual(clean.recovery.action, "no_action_needed")
        self.assertFalse(clean.authority)

        # Semver Worker out of range + core/hosted/workflow/policy mismatch.
        drifted = replace(
            manifest,
            reusable_workflow_tag_object="f" * 40,
            reusable_workflow_peeled_commit="f" * 40,
            installed_core_version="0.8.17",
            installed_hosted_version="0.3.23",
            broker_revision="0.2.0",
            frozen_policy_commit="9" * 40,
            gates=GateObservations(
                routing_preflight="passed",
                authenticated_live_validation="failed",
                broker_enforcement="not_run",
            ),
        )
        drift_report = classify_provenance_compatibility(
            drifted,
            expected_hosted_version="0.3.24",
            expected_policy_commit="d" * 40,
            compatibility_manifest=compat,
        )
        self.assertEqual(drift_report.status, "mismatch")
        self.assertEqual(
            drift_report.mismatched_fields,
            (
                "broker_revision",
                "frozen_policy_commit",
                "installed_core_version",
                "installed_hosted_version",
                "reusable_workflow_peeled_commit",
            ),
        )
        self.assertIn(
            "tag_object_equals_peeled_commit_lightweight_or_direct_sha",
            drift_report.notes,
        )
        self.assertIn(
            "routing_preflight_passed_without_live_validation_pass",
            drift_report.notes,
        )
        self.assertEqual(drift_report.recovery.action, "rescan_under_pinned_provenance")
        self.assertEqual(drift_report.recovery.command, "@reviewsensei rescan")
        self.assertTrue(drift_report.recovery.preserves_obligations)

        # Unknown runtime fields yield incomplete status + missing_evidence recovery.
        unknown_runtime = replace(
            manifest,
            reusable_workflow_peeled_commit=UNKNOWN_PROVENANCE,
            installed_core_version=UNKNOWN_PROVENANCE,
            broker_revision=UNKNOWN_PROVENANCE,
        )
        incomplete = classify_provenance_compatibility(
            unknown_runtime,
            expected_hosted_version="0.3.24",
            compatibility_manifest=compat,
        )
        self.assertEqual(incomplete.status, "incomplete")
        self.assertEqual(
            incomplete.missing_fields,
            (
                "broker_revision",
                "installed_core_version",
                "reusable_workflow_peeled_commit",
            ),
        )
        self.assertEqual(
            incomplete.recovery.action, "supply_original_context_or_rescan"
        )
        self.assertFalse(incomplete.to_dict()["authority"])

    def test_authority_elevation_and_tampered_invariants_are_rejected(self) -> None:
        manifest = _complete_manifest()
        for bad_authority in (True, 1, "false", None):
            with self.subTest(bad_authority=bad_authority):
                with self.assertRaises(ReviewInputError):
                    ProvenanceManifest(authority=bad_authority)  # type: ignore[arg-type]
                with self.assertRaises(ReviewInputError):
                    RecoveryPrescription(
                        action="no_action_needed",
                        command="none",
                        requires_fresh_inference=False,
                        steps=("Hold state.",),
                        authority=bad_authority,  # type: ignore[arg-type]
                    )

        for flag in (
            "preserves_obligations",
            "preserves_reservations",
            "preserves_unknown_charges",
        ):
            with self.subTest(flag=flag):
                with self.assertRaises(ReviewInputError):
                    RecoveryPrescription(
                        action="no_action_needed",
                        command="none",
                        requires_fresh_inference=False,
                        steps=("Hold state.",),
                        **{flag: False},
                    )

        with self.assertRaises(ReviewInputError):
            ProvenanceCompatibilityReport(
                status="invalid",
                mismatched_fields=(),
                missing_fields=(),
                notes=(),
                recovery=prescribe_recovery("supported_read_only_observation"),
            )

        raw = manifest.to_dict()
        raw["main_sha_is_runtime_proof"] = True
        with self.assertRaises(ReviewInputError):
            ProvenanceManifest.from_dict(raw)

    def test_sanitization_rejects_secrets_raw_diffs_control_chars_and_traversal(
        self,
    ) -> None:
        secret_samples = (
            "ghp_1234567890abcdefghijklmnopqrstuv",
            "github_pat_11ABCDEFG0123456789",
            "sk-proj-1234567890abcdef",
            "Bearer eyJhbGciOiJIUzI1NiJ9",
            "-----BEGIN RSA PRIVATE KEY-----",
            "https://alice:hunter2@example.com/repo",
            "api_key=supersecretvalue",
            "diff --git a/src/app.py b/src/app.py",
            "@@ -10,4 +10,5 @@",
            "line1\nline2",
        )
        for sample in secret_samples:
            with self.subTest(sample=sample[:20]):
                with self.assertRaises(ReviewInputError):
                    ProvenanceManifest(caller_workflow=sample)
                with self.assertRaises(ReviewInputError):
                    RecoveryPrescription(
                        action="no_action_needed",
                        command="none",
                        requires_fresh_inference=False,
                        steps=(sample,),
                    )

        for bad_ref in ("../outside/workflow.yml", "workflows//double.yml"):
            with self.assertRaises(ReviewInputError):
                ProvenanceManifest(caller_workflow=bad_ref)

        with self.assertRaises(ReviewInputError):
            UncertainDispatchRecord(operation_key="bad..key")

    def test_attempt_evidence_packet_and_every_recovery_route(self) -> None:
        manifest = _complete_manifest()
        full_bindings = OriginalInputBindings(
            configuration_sha256="1" * 64,
            defaults_sha256="2" * 64,
            context_selection_sha256="3" * 64,
            learnings_sha256="4" * 64,
            baseline_sha256="5" * 64,
            accepted_result_sha256="6" * 64,
            decision_policy_sha256="7" * 64,
        )
        self.assertEqual(full_bindings.missing_input_labels, ())

        budget = AttemptBudgetSummary(
            run_id=41261501984,
            attempt=1,
            owner_generation=2,
            original_api_limit=64,
            reserved_requests=4,
            consumed_requests=18,
            unknown_usage_requests=1,
            protected_tail_requests=6,
        )
        self.assertEqual(budget.conservative_committed_requests, 23)

        # Case 1: Retained result ready for zero-inference replay.
        replay_packet = build_attempt_evidence_packet(
            provenance=manifest,
            original_bindings=full_bindings,
            budget=budget,
            receipts=LifecycleReceipts(
                intent="confirmed",
                execution_started="confirmed",
                provider_request_result="confirmed",
                retained_result_parts="confirmed",
                publication="failed",
                check="not_applicable",
                thread_cleanup="not_applicable",
                native_approval="absent",
                terminal_accounting="pending",
            ),
            no_duplicate_work=NoDuplicateWorkAssertion(
                provider_calls=1,
                physical_api_dispatches=18,
                duplicate_business_effects=0,
                unreconciled_retries=0,
            ),
            current_base_ref="main",
            current_base_sha="a" * 40,
        )
        self.assertEqual(
            replay_packet.classifier_outcome,
            "eligible_recovery_awaiting_authorization",
        )
        self.assertEqual(replay_packet.recovery.action, "replay_retained_result")
        self.assertFalse(replay_packet.recovery.requires_fresh_inference)
        self.assertTrue(replay_packet.no_duplicate_work.holds)
        self.assertEqual(
            AttemptEvidencePacket.from_dict(replay_packet.to_dict()), replay_packet
        )

        public_summary = replay_packet.to_public_summary()
        self.assertEqual(
            public_summary["schema_version"], ATTEMPT_EVIDENCE_SCHEMA_VERSION
        )
        self.assertFalse(public_summary["authority"])
        self.assertLessEqual(len(json.dumps(public_summary).encode("utf-8")), 4096)

        # Case 2: Partial coverage -> @reviewsensei review remaining.
        partial_packet = build_attempt_evidence_packet(
            provenance=manifest,
            original_bindings=full_bindings,
            budget=budget,
            receipts=LifecycleReceipts(
                intent="confirmed",
                execution_started="confirmed",
                provider_request_result="confirmed",
                retained_result_parts="confirmed",
                publication="confirmed",
                check="confirmed",
                thread_cleanup="confirmed",
                native_approval="absent",
                terminal_accounting="pending",
            ),
            has_partial_coverage=True,
        )
        self.assertTrue(
            partial_packet.receipts.publication_settled_without_terminal_accounting()
        )
        self.assertTrue(
            partial_packet.receipts.check_green_without_approval_or_accounting()
        )
        self.assertEqual(partial_packet.recovery.action, "review_remaining")
        self.assertEqual(
            partial_packet.recovery.command, "@reviewsensei review remaining"
        )

        # Case 3: Published & terminal accounting settled, check/cleanup pending -> verify.
        verify_packet = build_attempt_evidence_packet(
            provenance=manifest,
            original_bindings=full_bindings,
            budget=budget,
            receipts=LifecycleReceipts(
                intent="confirmed",
                execution_started="confirmed",
                provider_request_result="confirmed",
                retained_result_parts="confirmed",
                publication="confirmed",
                check="failed",
                thread_cleanup="pending",
                native_approval="absent",
                terminal_accounting="confirmed",
            ),
        )
        self.assertEqual(verify_packet.recovery.action, "verify_convergence")
        self.assertEqual(verify_packet.recovery.command, "@reviewsensei verify")
        self.assertFalse(verify_packet.recovery.requires_fresh_inference)

        # Case 4: Missing original baseline (#421 attempt 2 pattern) -> supply context or rescan.
        missing_bindings = replace(full_bindings, baseline_sha256=MISSING_BINDING)
        self.assertEqual(missing_bindings.missing_input_labels, ("baseline",))
        missing_packet = build_attempt_evidence_packet(
            provenance=manifest,
            original_bindings=missing_bindings,
            budget=budget,
            receipts=LifecycleReceipts(publication="pending"),
        )
        self.assertEqual(missing_packet.classifier_outcome, "missing_evidence")
        self.assertEqual(
            missing_packet.recovery.action, "supply_original_context_or_rescan"
        )

        # Case 5: Uncertain dispatch -> owned readback before any mutation.
        uncertain_packet = build_attempt_evidence_packet(
            provenance=manifest,
            original_bindings=full_bindings,
            budget=budget,
            dispatches=(
                UncertainDispatchRecord(
                    operation_key="review_publish:attempt-1",
                    certainty="unknown",
                    owned_readback_sha256=UNKNOWN_PROVENANCE,
                ),
            ),
        )
        self.assertEqual(
            uncertain_packet.classifier_outcome,
            "explicit_human_decision_required",
        )
        self.assertEqual(
            uncertain_packet.recovery.action,
            "reconcile_owned_readback_then_hold_or_dispose",
        )

        # Case 6: Expired ledger marker -> controlled reenrollment.
        expired_packet = build_attempt_evidence_packet(
            provenance=manifest,
            ledger_expired=True,
        )
        self.assertEqual(expired_packet.recovery.action, "controlled_reenrollment")
        self.assertEqual(expired_packet.recovery.command, "controlled-reenroll")

    def test_validation_guards_on_malformed_dicts_and_types(self) -> None:
        with self.assertRaises(ReviewInputError):
            classify_provenance_compatibility("not-a-manifest")  # type: ignore[arg-type]

        with self.assertRaises(ReviewInputError):
            OriginalInputBindings.from_dict(
                {
                    "configuration_sha256": "1" * 64,
                    "defaults_sha256": "2" * 64,
                    "context_selection_sha256": "3" * 64,
                    "learnings_sha256": "4" * 64,
                    "baseline_sha256": "5" * 64,
                    "accepted_result_sha256": "6" * 64,
                    "decision_policy_sha256": "7" * 64,
                    "missing_input_labels": ["baseline"],
                }
            )

        with self.assertRaises(ReviewInputError):
            AttemptBudgetSummary.from_dict(
                {
                    "run_id": 1,
                    "attempt": 1,
                    "owner_generation": 0,
                    "original_api_limit": 10,
                    "reserved_requests": 1,
                    "consumed_requests": 2,
                    "unknown_usage_requests": 0,
                    "protected_tail_requests": 1,
                    "conservative_committed_requests": 99,
                }
            )

        with self.assertRaises(ReviewInputError):
            NoDuplicateWorkAssertion.from_dict(
                {
                    "provider_calls": 1,
                    "physical_api_dispatches": 2,
                    "duplicate_business_effects": 1,
                    "unreconciled_retries": 0,
                    "holds": True,
                }
            )

        packet_dict = build_attempt_evidence_packet(
            provenance=_complete_manifest()
        ).to_dict()
        packet_dict["packet_sha256"] = "0" * 64
        with self.assertRaises(ReviewInputError):
            AttemptEvidencePacket.from_dict(packet_dict)

    def test_coexists_with_lifecycle_diagnostics_g18_boundary(self) -> None:
        from review_sensei_github import review_lifecycle_diagnostics as diagnostic

        stream = io.StringIO()
        with redirect_stderr(stream), diagnostic.lifecycle("review") as current:
            diagnostic.observed_policy("main", "d" * 40, "2" * 64)
            current.returned(0)
        lines = [
            line.removeprefix(diagnostic.PREFIX)
            for line in stream.getvalue().splitlines()
            if line.startswith(diagnostic.PREFIX)
        ]
        self.assertEqual(len(lines), 1)
        observed_line = json.loads(lines[0])
        self.assertFalse(observed_line["authority"])
        observed_policy = observed_line["scope"]["observed_policy"]

        manifest = ProvenanceManifest(
            frozen_policy_commit=observed_policy["policy_commit"],
            frozen_policy_sha256=observed_policy["policy_content_sha256"],
        )
        packet = build_attempt_evidence_packet(provenance=manifest)
        self.assertFalse(packet.authority)
        self.assertEqual(packet.provenance.frozen_policy_commit, "d" * 40)


if __name__ == "__main__":
    unittest.main()
