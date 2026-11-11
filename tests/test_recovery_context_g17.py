"""Tests for bounded original recovery-context retention and fresh-process handoff (G17)."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from review_sensei_github.publication_context_recovery import (
    bundle_from_reconstructed_contexts,
    reconstruct_or_prescribe_publication_contexts,
    reconstruct_publication_contexts,
)
from test_publication_recovery import BASE, HEAD, IDENTITY
from test_trusted_publication_recovery import (
    TrustedPublicationRecoveryTests,
    _FailedPublication,
)

from review_sensei.api_work import (
    ACCOUNTING_VERSION,
    LEGACY_ACCOUNTING_VERSION,
    PARTITIONED_ACTION_ACCOUNTING_VERSION,
)
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewResult
from review_sensei.recovery_context import (
    MAX_DECODED_RESULT_BYTES,
    MAX_RECOVERY_ANCESTOR_DEPTH,
    MAX_RECOVERY_HANDOFF_FILES,
    MAX_RECOVERY_RESULT_PARTS,
    MAX_SESSION_ROOT_BYTES,
    RECOVERY_CONTEXT_SCHEMA_VERSION,
    OriginalRecoveryContextBundle,
    RecoveryContextStore,
    build_bundle_from_context_documents,
    canonical_recovery_digest,
    classify_recovery_input_origins,
    ensure_not_observation_only_evidence,
    price_recovery_context_admission,
)
from review_sensei.session import LocalSessionLedger

POLICY_COMMIT = "e" * 40
HANDOFF_KEY = b"k" * 32


def _sample_bundle(**overrides: object) -> OriginalRecoveryContextBundle:
    defaults: dict[str, object] = {
        "repository": "octo/repo",
        "repository_id": 1,
        "pull_request": 1,
        "base_ref": "main",
        "base_sha": BASE,
        "head_sha": HEAD,
        "policy_commit": POLICY_COMMIT,
        "transaction_id": "1" * 64,
        "generation": 1,
        "reservation_id": "resv:publish:01",
        "accepted_result_sha256": "2" * 64,
        "configuration_digest": "3" * 64,
        "evidence_digest": "4" * 64,
        "convergence_policy_digest": "5" * 64,
        "work_policy_digest": "6" * 64,
        "decision_policy_digest": "7" * 64,
        "provider_identity": "ollama:qwen3.5:4b",
        "selected_context_digest": "8" * 64,
        "learnings_digest": "9" * 64,
        "baseline_digest": None,
        "diff_sha256": "a" * 64,
        "placement_mode": "complete",
        "accounting_version": ACCOUNTING_VERSION,
        "producer": "local-cli",
        "run_id": 101,
        "attempt": 1,
        "reconstructible_from_defaults": True,
        "runtime_only_fields": (),
        "authority": False,
    }
    defaults.update(overrides)
    return OriginalRecoveryContextBundle(**defaults)  # type: ignore[arg-type]


class OriginalRecoveryContextBundleTests(unittest.TestCase):
    def test_census_round_trip_and_strict_sanitization(self) -> None:
        bundle = _sample_bundle(baseline_digest="b" * 64)
        payload = bundle.to_dict()
        self.assertEqual(payload["schema_version"], RECOVERY_CONTEXT_SCHEMA_VERSION)
        self.assertIs(payload["contains_raw_diff_or_prompt"], False)
        self.assertIs(payload["authority"], False)
        self.assertEqual(len(bundle.bundle_sha256), 64)
        restored = OriginalRecoveryContextBundle.from_dict(payload)
        self.assertEqual(restored, bundle)

        tampered = dict(payload)
        tampered["bundle_sha256"] = "f" * 64
        with self.assertRaisesRegex(
            ReviewInputError, "tampered_authentication_or_digest"
        ):
            OriginalRecoveryContextBundle.from_dict(tampered)

        with self.assertRaisesRegex(ReviewInputError, "authority"):
            _sample_bundle(authority=True)

        with self.assertRaisesRegex(ReviewInputError, "secret-like"):
            _sample_bundle(provider_identity="ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")

        with self.assertRaisesRegex(ReviewInputError, "placement_mode"):
            _sample_bundle(placement_mode="invalid")

        with self.assertRaisesRegex(ReviewInputError, "attempt"):
            _sample_bundle(attempt=3)

        for bad_dict in (
            "not-a-dict",
            {k: v for k, v in payload.items() if k != "diff_sha256"},
            {**payload, "unexpected": 1},
            {**payload, "schema_version": "wrong"},
            {**payload, "contains_raw_diff_or_prompt": True},
            {**payload, "runtime_only_fields": "not-a-list"},
        ):
            with self.assertRaises(ReviewInputError):
                OriginalRecoveryContextBundle.from_dict(bad_dict)

    def test_reconstructible_defaults_separated_from_runtime_only_inputs(self) -> None:
        reconstructible, fields = classify_recovery_input_origins()
        self.assertTrue(reconstructible)
        self.assertEqual(fields, ())

        reconstructible, fields = classify_recovery_input_origins(
            has_stage_provider_routing=True,
            has_overwritten_verification_baseline=True,
        )
        self.assertFalse(reconstructible)
        self.assertEqual(
            fields,
            ("overwritten_verification_baseline", "stage_provider_routing"),
        )
        bundle = _sample_bundle(
            reconstructible_from_defaults=reconstructible,
            runtime_only_fields=fields,
        )
        self.assertFalse(bundle.reconstructible_from_defaults)
        self.assertEqual(bundle.runtime_only_fields, fields)

        with self.assertRaisesRegex(ReviewInputError, "reconstructible_from_defaults"):
            _sample_bundle(
                reconstructible_from_defaults=True,
                runtime_only_fields=("cli_overrides",),
            )
        with self.assertRaisesRegex(ReviewInputError, "duplicates"):
            _sample_bundle(
                reconstructible_from_defaults=False,
                runtime_only_fields=("cli_overrides", "cli_overrides"),
            )
        with self.assertRaisesRegex(ReviewInputError, "unknown runtime_only_fields"):
            _sample_bundle(
                reconstructible_from_defaults=False,
                runtime_only_fields=("unknown_flag",),
            )

    def test_v1_v2_preserved_and_v3_plus_observation_only_refused(self) -> None:
        for version in (LEGACY_ACCOUNTING_VERSION, ACCOUNTING_VERSION):
            bundle = _sample_bundle(accounting_version=version)
            self.assertEqual(bundle.accounting_version, version)

        with self.assertRaisesRegex(ReviewInputError, "unsupported_v3_writer"):
            _sample_bundle(accounting_version=PARTITIONED_ACTION_ACCOUNTING_VERSION)

        with self.assertRaisesRegex(ReviewInputError, "unsupported accounting_version"):
            _sample_bundle(accounting_version="caller-requests-v99")

        ancestry_observation = SimpleNamespace(anchor_document={}, ancestors=())
        with self.assertRaisesRegex(ReviewInputError, "observation_only_not_authority"):
            ensure_not_observation_only_evidence(ancestry_observation)

        action_result_doc = {
            "schema_version": "caller-action-result-v1",
            "accounting_version": ACCOUNTING_VERSION,
        }
        with self.assertRaisesRegex(ReviewInputError, "observation_only_not_authority"):
            OriginalRecoveryContextBundle.from_dict(action_result_doc)

        v3_action_doc = {
            "schema_version": "caller-action-result-v1",
            "accounting_version": PARTITIONED_ACTION_ACCOUNTING_VERSION,
        }
        with self.assertRaisesRegex(ReviewInputError, "unsupported_v3_writer"):
            OriginalRecoveryContextBundle.from_dict(v3_action_doc)


class PreEffectBudgetAndHandoffStoreTests(unittest.TestCase):
    def test_cycle_depth_storage_and_request_budget_refusals_before_effects(
        self,
    ) -> None:
        bundle = _sample_bundle()
        ok = price_recovery_context_admission(
            bundle,
            current_run_id=200,
            current_root_sha256="a" * 64,
            ancestor_record_sha256s=("b" * 64, "c" * 64),
            ancestor_run_ids=(199, 198),
            already_consumed_requests=100,
            unknown_usage_requests=20,
            active_reserved_requests=10,
            original_api_limit=512,
        )
        self.assertTrue(ok.admitted)
        assert ok.pricing is not None
        self.assertTrue(ok.pricing.fits())
        self.assertGreater(ok.pricing.remaining_headroom_requests, 0)
        self.assertEqual(ok.recovery.action, "replay_retained_result")
        self.assertFalse(ok.recovery.requires_fresh_inference)

        hash_cycle = price_recovery_context_admission(
            bundle,
            current_run_id=200,
            current_root_sha256="a" * 64,
            ancestor_record_sha256s=("b" * 64, "a" * 64),
        )
        self.assertFalse(hash_cycle.admitted)
        self.assertEqual(hash_cycle.refusal_code, "circular_ancestor_evidence")

        run_cycle = price_recovery_context_admission(
            bundle,
            current_run_id=200,
            ancestor_run_ids=(199, 200),
        )
        self.assertFalse(run_cycle.admitted)
        self.assertEqual(run_cycle.refusal_code, "circular_ancestor_evidence")

        too_deep = price_recovery_context_admission(
            bundle,
            current_run_id=200,
            ancestor_run_ids=tuple(range(1, MAX_RECOVERY_ANCESTOR_DEPTH + 2)),
        )
        self.assertFalse(too_deep.admitted)
        self.assertEqual(too_deep.refusal_code, "ancestor_depth_exceeded")

        for kwargs in (
            {"result_part_count": MAX_RECOVERY_RESULT_PARTS + 1},
            {"current_root_bytes": MAX_SESSION_ROOT_BYTES + 1},
            {"intermediate_root_bytes": MAX_SESSION_ROOT_BYTES + 1},
            {"total_decoded_bytes": MAX_DECODED_RESULT_BYTES + 1},
        ):
            exceeded = price_recovery_context_admission(
                bundle, current_run_id=200, **kwargs
            )
            self.assertFalse(exceeded.admitted)
            self.assertEqual(exceeded.refusal_code, "root_capacity_exceeded")
            assert exceeded.pricing is not None
            self.assertFalse(exceeded.pricing.fits())

        exhausted = price_recovery_context_admission(
            bundle,
            current_run_id=200,
            already_consumed_requests=490,
            unknown_usage_requests=10,
            active_reserved_requests=5,
            original_api_limit=512,
        )
        self.assertFalse(exhausted.admitted)
        self.assertEqual(exhausted.refusal_code, "budget_exhausted_before_effect")
        self.assertEqual(
            exhausted.recovery.action, "reconcile_owned_readback_then_hold_or_dispose"
        )

    def test_handoff_store_authenticates_and_refuses_tampering_truncation_expiry(
        self,
    ) -> None:
        now = datetime(2026, 11, 11, 4, 0, tzinfo=timezone.utc)
        clock = [now]
        with tempfile.TemporaryDirectory() as directory:
            handoff_dir = Path(directory) / "handoff"
            store = RecoveryContextStore(
                handoff_dir,
                key=HANDOFF_KEY,
                ttl_seconds=600,
                now=lambda: clock[0],
            )
            bundle = _sample_bundle(baseline_digest="c" * 64)

            absent = store.load_for_fresh_process(
                bundle.transaction_id,
                expected_repository=bundle.repository,
                expected_pull_request=bundle.pull_request,
                expected_base_sha=bundle.base_sha,
                expected_head_sha=bundle.head_sha,
                expected_policy_commit=bundle.policy_commit,
                expected_result_sha256=bundle.accepted_result_sha256,
                ledger_expired=True,
            )
            self.assertFalse(absent.admitted)
            self.assertEqual(absent.refusal_code, "absent_context")
            self.assertEqual(absent.recovery.action, "controlled_reenrollment")
            self.assertEqual(absent.recovery.command, "controlled-reenroll")

            saved_path = store.save(bundle)
            self.assertTrue(saved_path.is_file())

            fresh = store.load_for_fresh_process(
                bundle.transaction_id,
                expected_repository=bundle.repository,
                expected_pull_request=bundle.pull_request,
                expected_base_sha=bundle.base_sha,
                expected_head_sha=bundle.head_sha,
                expected_policy_commit=bundle.policy_commit,
                expected_result_sha256=bundle.accepted_result_sha256,
                expected_baseline_digest="c" * 64,
                verify_baseline_match=True,
                current_run_id=102,
            )
            self.assertTrue(fresh.admitted)
            self.assertEqual(fresh.bundle, bundle)

            overwritten = store.load_for_fresh_process(
                bundle.transaction_id,
                expected_repository=bundle.repository,
                expected_pull_request=bundle.pull_request,
                expected_base_sha=bundle.base_sha,
                expected_head_sha=bundle.head_sha,
                expected_policy_commit=bundle.policy_commit,
                expected_result_sha256=bundle.accepted_result_sha256,
                expected_baseline_digest="d" * 64,
                verify_baseline_match=True,
            )
            self.assertFalse(overwritten.admitted)
            self.assertEqual(
                overwritten.refusal_code, "overwritten_baseline_or_context"
            )

            mismatch = store.load_for_fresh_process(
                bundle.transaction_id,
                expected_repository=bundle.repository,
                expected_pull_request=bundle.pull_request,
                expected_base_sha=bundle.base_sha,
                expected_head_sha="f" * 40,
                expected_policy_commit=bundle.policy_commit,
                expected_result_sha256=bundle.accepted_result_sha256,
            )
            self.assertFalse(mismatch.admitted)
            self.assertEqual(mismatch.refusal_code, "snapshot_or_transaction_mismatch")
            self.assertIn("head_sha", mismatch.missing_or_invalid)

            clock[0] = now + timedelta(seconds=601)
            expired = store.load_for_fresh_process(
                bundle.transaction_id,
                expected_repository=bundle.repository,
                expected_pull_request=bundle.pull_request,
                expected_base_sha=bundle.base_sha,
                expected_head_sha=bundle.head_sha,
                expected_policy_commit=bundle.policy_commit,
                expected_result_sha256=bundle.accepted_result_sha256,
            )
            self.assertFalse(expired.admitted)
            self.assertEqual(expired.refusal_code, "expired_context")
            clock[0] = now

            wrong_key_store = RecoveryContextStore(
                handoff_dir, key=b"z" * 32, ttl_seconds=600, now=lambda: clock[0]
            )
            tampered = wrong_key_store.load_for_fresh_process(
                bundle.transaction_id,
                expected_repository=bundle.repository,
                expected_pull_request=bundle.pull_request,
                expected_base_sha=bundle.base_sha,
                expected_head_sha=bundle.head_sha,
                expected_policy_commit=bundle.policy_commit,
                expected_result_sha256=bundle.accepted_result_sha256,
            )
            self.assertFalse(tampered.admitted)
            self.assertEqual(tampered.refusal_code, "tampered_authentication_or_digest")

            saved_path.write_bytes(saved_path.read_bytes()[:40])
            truncated = store.load_for_fresh_process(
                bundle.transaction_id,
                expected_repository=bundle.repository,
                expected_pull_request=bundle.pull_request,
                expected_base_sha=bundle.base_sha,
                expected_head_sha=bundle.head_sha,
                expected_policy_commit=bundle.policy_commit,
                expected_result_sha256=bundle.accepted_result_sha256,
            )
            self.assertFalse(truncated.admitted)
            self.assertEqual(truncated.refusal_code, "truncated_or_corrupt_context")

    def test_handoff_store_enforces_file_cap_and_rejects_bad_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ReviewInputError, "key"):
                RecoveryContextStore(Path(directory) / "bad", key=b"short")

            store = RecoveryContextStore(Path(directory) / "capped", key=HANDOFF_KEY)
            for idx in range(MAX_RECOVERY_HANDOFF_FILES):
                tx_id = f"{idx:064x}"
                store.save(_sample_bundle(transaction_id=tx_id))
            with self.assertRaisesRegex(
                ReviewInputError, "aggregate storage budget exhausted"
            ):
                store.save(_sample_bundle(transaction_id="f" * 64))

    def test_signed_corrupt_envelopes_and_scalar_validators(self) -> None:
        for bad_kwargs in (
            {"repository": 123},
            {"repository": "bad repo"},
            {"base_ref": 123},
            {"base_ref": "../bad"},
            {"base_sha": 123},
            {"base_sha": "xyz"},
            {"transaction_id": 123},
            {"transaction_id": "xyz"},
            {"pull_request": 0},
            {"reservation_id": 123},
            {"reservation_id": "bad..token"},
            {"runtime_only_fields": "not-a-seq"},
        ):
            with self.assertRaises(ReviewInputError):
                _sample_bundle(**bad_kwargs)

        ensure_not_observation_only_evidence(None)

        now = datetime(2026, 11, 11, 4, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            handoff_dir = Path(directory) / "handoff"
            handoff_dir.mkdir(mode=0o700)
            store = RecoveryContextStore(handoff_dir, key=HANDOFF_KEY, now=lambda: now)
            bundle = _sample_bundle()
            target = handoff_dir / f"{bundle.transaction_id}.json"

            def write_signed(doc: dict[str, object]) -> None:
                sig = store._signature(doc)
                target.write_text(
                    json.dumps({"document": doc, "authentication": sig}),
                    encoding="utf-8",
                )

            target.write_bytes(b"")
            self.assertEqual(
                store.load_for_fresh_process(
                    bundle.transaction_id,
                    expected_repository=bundle.repository,
                    expected_pull_request=1,
                    expected_base_sha=BASE,
                    expected_head_sha=HEAD,
                    expected_policy_commit=POLICY_COMMIT,
                    expected_result_sha256=bundle.accepted_result_sha256,
                ).refusal_code,
                "truncated_or_corrupt_context",
            )

            target.write_text(json.dumps({"wrong": 1}), encoding="utf-8")
            self.assertEqual(
                store.load_for_fresh_process(
                    bundle.transaction_id,
                    expected_repository=bundle.repository,
                    expected_pull_request=1,
                    expected_base_sha=BASE,
                    expected_head_sha=HEAD,
                    expected_policy_commit=POLICY_COMMIT,
                    expected_result_sha256=bundle.accepted_result_sha256,
                ).refusal_code,
                "truncated_or_corrupt_context",
            )

            base_doc = {
                "envelope_version": "original-recovery-handoff-v1",
                "saved_at": now.isoformat(),
                "expires_at": (now + timedelta(seconds=60)).isoformat(),
                "bundle": bundle.to_dict(),
            }
            for doc, expected_code in (
                (
                    {**base_doc, "envelope_version": "bad"},
                    "truncated_or_corrupt_context",
                ),
                ({**base_doc, "saved_at": "not-iso"}, "truncated_or_corrupt_context"),
                (
                    {
                        **base_doc,
                        "bundle": {
                            **bundle.to_dict(),
                            "accounting_version": PARTITIONED_ACTION_ACCOUNTING_VERSION,
                        },
                    },
                    "unsupported_v3_writer",
                ),
                (
                    {
                        **base_doc,
                        "bundle": {"schema_version": "caller-action-result-v1"},
                    },
                    "observation_only_not_authority",
                ),
                (
                    {
                        **base_doc,
                        "bundle": {**bundle.to_dict(), "bundle_sha256": "0" * 64},
                    },
                    "tampered_authentication_or_digest",
                ),
                (
                    {**base_doc, "bundle": {"bad": 1}},
                    "truncated_or_corrupt_context",
                ),
            ):
                write_signed(doc)
                res = store.load_for_fresh_process(
                    bundle.transaction_id,
                    expected_repository=bundle.repository,
                    expected_pull_request=1,
                    expected_base_sha=BASE,
                    expected_head_sha=HEAD,
                    expected_policy_commit=POLICY_COMMIT,
                    expected_result_sha256=bundle.accepted_result_sha256,
                )
                self.assertEqual(res.refusal_code, expected_code)

            write_signed(base_doc)
            all_mismatch = store.load_for_fresh_process(
                bundle.transaction_id,
                expected_repository="other/repo",
                expected_pull_request=99,
                expected_base_sha="0" * 40,
                expected_head_sha="f" * 40,
                expected_policy_commit="d" * 40,
                expected_result_sha256="e" * 64,
            )
            self.assertEqual(
                all_mismatch.refusal_code, "snapshot_or_transaction_mismatch"
            )
            self.assertEqual(len(all_mismatch.missing_or_invalid), 6)


class EndToEndFreshProcessRecoveryG17Tests(TrustedPublicationRecoveryTests):
    def test_fresh_process_zero_inference_handoff_and_decision_drift_recovery(
        self,
    ) -> None:
        # 1. Default context: delete all local files (result, config, admission)
        #    and recover from remote retained parts + fresh-process handoff.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.original_analysis(root)
            diff_text = (root / "review.patch").read_text(encoding="utf-8")
            result = ReviewResult.from_dict(
                json.loads((root / "result.json").read_text(encoding="utf-8"))
            )
            assert result.transaction is not None
            state = _FailedPublication()
            self.assertEqual(self.invoke(root, state)[0], 1)
            ledger = LocalSessionLedger(root / "ledger")
            _, admitted_default = reconstruct_or_prescribe_publication_contexts(
                source_root=root,
                diff=diff_text,
                ledger=ledger,
                identity=IDENTITY,
                base_ref="main",
                base_sha=BASE,
                head_sha=HEAD,
                policy_commit=POLICY_COMMIT,
                policy=ReviewConvergencePolicy(),
            )
            self.assertTrue(admitted_default.admitted)
            assert admitted_default.bundle is not None
            handoff_store = RecoveryContextStore(
                root / "handoff",
                key=HANDOFF_KEY,
                now=lambda: datetime.now(timezone.utc),
            )
            handoff_store.save(admitted_default.bundle)
            for artifact in ("result.json", "configuration.json", "admission.json"):
                (root / artifact).unlink()
            loaded_default = handoff_store.load_for_fresh_process(
                result.transaction.transaction_id,
                expected_repository=IDENTITY.repository,
                expected_pull_request=IDENTITY.pull_request,
                expected_base_sha=BASE,
                expected_head_sha=HEAD,
                expected_policy_commit=POLICY_COMMIT,
                expected_result_sha256=result.content_digest(),
                current_run_id=2,
            )
            self.assertTrue(loaded_default.admitted)
            state.fail_review = False
            code, output, error = self.invoke(root, state, reconstruct=True)
            self.assertEqual(code, 0, error)
            self.assertIn("published", output)

        # 2. #427 Decision-enabled context: drift refuses without ledger mutation;
        #    restoring pinned .reviewsensei.yml recovers to published with zero inference.
        decision = {"enabled": True, "provider": "ollama", "model": "fixture-decision"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.original_analysis(root, decision=decision, work_mode="unified")
            original_config_text = (root / ".reviewsensei.yml").read_text(
                encoding="utf-8"
            )
            diff_text = (root / "review.patch").read_text(encoding="utf-8")
            result = ReviewResult.from_dict(
                json.loads((root / "result.json").read_text(encoding="utf-8"))
            )
            assert result.transaction is not None

            state = _FailedPublication()
            self.assertEqual(self.invoke(root, state)[0], 1)
            ledger = LocalSessionLedger(root / "ledger")

            reconstructed, admitted = reconstruct_or_prescribe_publication_contexts(
                source_root=root,
                diff=diff_text,
                ledger=ledger,
                identity=IDENTITY,
                base_ref="main",
                base_sha=BASE,
                head_sha=HEAD,
                policy_commit=POLICY_COMMIT,
                policy=ReviewConvergencePolicy(),
            )
            self.assertTrue(admitted.admitted)
            assert reconstructed is not None
            assert admitted.bundle is not None
            self.assertNotIn("diff --git", json.dumps(admitted.bundle.to_dict()))

            handoff_store = RecoveryContextStore(
                root / "handoff",
                key=HANDOFF_KEY,
                now=lambda: datetime.now(timezone.utc),
            )
            handoff_store.save(admitted.bundle)

            for artifact in ("result.json", "configuration.json", "admission.json"):
                (root / artifact).unlink()

            # Drift #427 decision policy -> must refuse without mutating ledger
            (root / ".reviewsensei.yml").write_text(
                original_config_text.replace("fixture-decision", "drifted-decision"),
                encoding="utf-8",
            )
            before_ledger = self.ledger_bytes(root)
            drifted_ctx, drifted_decision = (
                reconstruct_or_prescribe_publication_contexts(
                    source_root=root,
                    diff=diff_text,
                    ledger=ledger,
                    identity=IDENTITY,
                    base_ref="main",
                    base_sha=BASE,
                    head_sha=HEAD,
                    policy_commit=POLICY_COMMIT,
                    policy=ReviewConvergencePolicy(),
                )
            )
            self.assertIsNone(drifted_ctx)
            self.assertFalse(drifted_decision.admitted)
            self.assertEqual(
                drifted_decision.refusal_code, "snapshot_or_transaction_mismatch"
            )
            self.assertEqual(
                drifted_decision.recovery.action, "rescan_under_pinned_provenance"
            )
            self.assertEqual(drifted_decision.recovery.command, "@reviewsensei rescan")
            self.assertEqual(self.ledger_bytes(root), before_ledger)

            # Restore pinned config & verify fresh-process handoff + zero-inference publish
            (root / ".reviewsensei.yml").write_text(
                original_config_text, encoding="utf-8"
            )
            loaded_handoff = handoff_store.load_for_fresh_process(
                result.transaction.transaction_id,
                expected_repository=IDENTITY.repository,
                expected_pull_request=IDENTITY.pull_request,
                expected_base_sha=BASE,
                expected_head_sha=HEAD,
                expected_policy_commit=POLICY_COMMIT,
                expected_result_sha256=result.content_digest(),
                current_run_id=2,
            )
            self.assertTrue(loaded_handoff.admitted)
            self.assertEqual(loaded_handoff.bundle, admitted.bundle)

            state.fail_review = False
            code, output, error = self.invoke(root, state, reconstruct=True)
            self.assertEqual(code, 0, error)
            self.assertIn("published", output)
            final_record = ledger.load(IDENTITY).record
            assert final_record is not None and final_record.transaction is not None
            self.assertEqual(final_record.transaction.phase, "publication_succeeded")
            self.assertEqual(
                final_record.transaction.result_sha256, result.content_digest()
            )

    def test_overwritten_baseline_refuses_default_reconstruction_and_recovers_with_original_documents(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.original_analysis(root, head="c" * 40)
            self.original_analysis(root)
            diff_text = (root / "review.patch").read_text(encoding="utf-8")
            result = ReviewResult.from_dict(
                json.loads((root / "result.json").read_text(encoding="utf-8"))
            )
            config_doc = json.loads(
                (root / "configuration.json").read_text(encoding="utf-8")
            )
            admission_doc = json.loads(
                (root / "admission.json").read_text(encoding="utf-8")
            )
            assert result.transaction is not None

            for forbidden_key in ("diff", "prompt"):
                with self.assertRaisesRegex(ReviewInputError, "raw diff or prompt"):
                    build_bundle_from_context_documents(
                        transaction=result.transaction,
                        configuration_context=config_doc,
                        admission_context={**admission_doc, forbidden_key: "secret"},
                        decision_policy_digest="7" * 64,
                        diff_sha256="a" * 64,
                        policy_commit=POLICY_COMMIT,
                    )

            with self.assertRaisesRegex(
                ReviewInputError, "configuration_context digest"
            ):
                build_bundle_from_context_documents(
                    transaction=result.transaction,
                    configuration_context={**config_doc, "model": "wrong"},
                    admission_context=admission_doc,
                    decision_policy_digest="7" * 64,
                    diff_sha256="a" * 64,
                    policy_commit=POLICY_COMMIT,
                )

            with self.assertRaisesRegex(ReviewInputError, "evidence digest"):
                build_bundle_from_context_documents(
                    transaction=result.transaction,
                    configuration_context=config_doc,
                    admission_context=admission_doc,
                    evidence_context={
                        "evidence_policy": "confirmed",
                        "snapshot_sha256": "f" * 64,
                    },
                    decision_policy_digest="7" * 64,
                    diff_sha256="a" * 64,
                    policy_commit=POLICY_COMMIT,
                )

            explicit_bundle = build_bundle_from_context_documents(
                transaction=result.transaction,
                configuration_context=config_doc,
                admission_context=admission_doc,
                decision_policy_digest="7" * 64,
                diff_sha256=hashlib.sha256(diff_text.encode("utf-8")).hexdigest(),
                policy_commit=POLICY_COMMIT,
                reconstructible_from_defaults=False,
                runtime_only_fields=("overwritten_verification_baseline",),
            )
            self.assertFalse(explicit_bundle.reconstructible_from_defaults)
            self.assertIsNotNone(explicit_bundle.baseline_digest)
            self.assertEqual(
                explicit_bundle.baseline_digest,
                canonical_recovery_digest(admission_doc["baseline"]),
            )

            state = _FailedPublication()
            self.assertEqual(self.invoke(root, state)[0], 1)
            ledger = LocalSessionLedger(root / "ledger")

            reconstructed, decision = reconstruct_or_prescribe_publication_contexts(
                source_root=root,
                diff=diff_text,
                ledger=ledger,
                identity=IDENTITY,
                base_ref="main",
                base_sha=BASE,
                head_sha=HEAD,
                policy_commit=POLICY_COMMIT,
                policy=ReviewConvergencePolicy(),
            )
            self.assertIsNone(reconstructed)
            self.assertFalse(decision.admitted)
            self.assertEqual(decision.refusal_code, "overwritten_baseline_or_context")
            self.assertEqual(
                decision.recovery.action, "supply_original_context_or_rescan"
            )
            self.assertEqual(
                decision.recovery.command, "supply-original-recovery-context"
            )

            # Universal Review Recovery Guarantee: supplying original context documents
            # recovers the review cleanly to published completion with zero inference!
            state.fail_review = False
            code, output, error = self.invoke(root, state, reconstruct=False)
            self.assertEqual(code, 0, error)
            self.assertIn("published", output)
            after = ledger.load(IDENTITY).record
            assert after is not None and after.transaction is not None
            self.assertEqual(after.transaction.phase, "publication_succeeded")

    def test_v3_accounting_on_ledger_refuses_bundle_enrollment_and_prescribes_recovery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.original_analysis(root)
            diff_text = (root / "review.patch").read_text(encoding="utf-8")
            state = _FailedPublication()
            self.assertEqual(self.invoke(root, state)[0], 1)
            ledger = LocalSessionLedger(root / "ledger")
            reconstructed = reconstruct_publication_contexts(
                source_root=root,
                diff=diff_text,
                ledger=ledger,
                identity=IDENTITY,
                base_sha=BASE,
                head_sha=HEAD,
                policy=ReviewConvergencePolicy(),
            )

            loaded = ledger.load(IDENTITY)
            assert loaded.record is not None
            v3_record = SimpleNamespace(
                transaction=loaded.record.transaction,
                api_run_accounting={
                    "schema_version": PARTITIONED_ACTION_ACCOUNTING_VERSION
                },
            )
            real_load = ledger.load
            calls = [0]

            def load_v3_on_bundle(identity_arg):
                calls[0] += 1
                if calls[0] % 2 == 1:
                    return SimpleNamespace(status="ok", record=v3_record)
                return real_load(identity_arg)

            with patch.object(
                ledger,
                "load",
                return_value=SimpleNamespace(status="ok", record=v3_record),
            ):
                with self.assertRaisesRegex(ReviewInputError, "unsupported_v3_writer"):
                    bundle_from_reconstructed_contexts(
                        reconstructed,
                        source_root=root,
                        diff=diff_text,
                        ledger=ledger,
                        identity=IDENTITY,
                        base_ref="main",
                        policy_commit=POLICY_COMMIT,
                    )

            calls[0] = 1
            with patch.object(ledger, "load", side_effect=load_v3_on_bundle):
                _, refusal = reconstruct_or_prescribe_publication_contexts(
                    source_root=root,
                    diff=diff_text,
                    ledger=ledger,
                    identity=IDENTITY,
                    base_ref="main",
                    base_sha=BASE,
                    head_sha=HEAD,
                    policy_commit=POLICY_COMMIT,
                    policy=ReviewConvergencePolicy(),
                )
            self.assertFalse(refusal.admitted)
            self.assertEqual(refusal.refusal_code, "unsupported_v3_writer")
            self.assertEqual(
                refusal.recovery.action, "reconcile_owned_readback_then_hold_or_dispose"
            )


if __name__ == "__main__":
    unittest.main()
