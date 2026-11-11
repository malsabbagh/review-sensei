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

import test_trusted_publication_recovery as trusted_recovery
from review_sensei_github.publication_context_recovery import (
    bundle_from_reconstructed_contexts,
    reconstruct_or_prescribe_publication_contexts,
    reconstruct_publication_contexts,
    verify_stored_recovery_context,
)
from test_publication_recovery import BASE, HEAD, IDENTITY

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
    resolve_recovery_context_store,
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
        with self.assertRaisesRegex(ReviewInputError, "observation_only_not_authority"):
            OriginalRecoveryContextBundle.from_dict(v3_action_doc)

        v3_writer_doc = {
            "accounting_version": PARTITIONED_ACTION_ACCOUNTING_VERSION,
        }
        ensure_not_observation_only_evidence(
            v3_writer_doc, check_writer_enrollment=False
        )
        with self.assertRaisesRegex(ReviewInputError, "unsupported_v3_writer"):
            OriginalRecoveryContextBundle.from_dict(v3_writer_doc)


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


_FailedPublication = trusted_recovery._FailedPublication


class EndToEndFreshProcessRecoveryG17Tests(
    trusted_recovery.TrustedPublicationRecoveryTests
):
    @staticmethod
    def _enable_diagnostics_and_handoff_args(root: Path) -> tuple[str, ...]:
        config_path = root / ".reviewsensei.yml"
        text = config_path.read_text(encoding="utf-8")
        if "artifacts: diagnostics" not in text:
            text = text.replace(
                "github:\n",
                "github:\n  artifacts: diagnostics\n",
                1,
            )
            config_path.write_text(text, encoding="utf-8")
        key_file = root / "handoff.key"
        key_file.write_bytes(HANDOFF_KEY)
        key_file.chmod(0o600)
        return (
            "--recovery-context-dir",
            str(root / "handoff"),
            "--recovery-context-key-file",
            str(key_file),
            "--policy-commit",
            POLICY_COMMIT,
        )

    def test_fresh_process_zero_inference_handoff_and_decision_drift_recovery(
        self,
    ) -> None:
        # 1. Default context: ordinary CLI publish captures handoff automatically
        #    before publication fails; delete all local files (result, config, admission)
        #    and recover from remote retained parts + authenticated fresh-process handoff
        #    with zero test-side store.save(...) shortcuts.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.original_analysis(root)
            handoff_args = self._enable_diagnostics_and_handoff_args(root)
            result = ReviewResult.from_dict(
                json.loads((root / "result.json").read_text(encoding="utf-8"))
            )
            assert result.transaction is not None
            state = _FailedPublication()
            self.assertEqual(self.invoke(root, state, extra=handoff_args)[0], 1)
            handoff_file = (
                root / "handoff" / f"{result.transaction.transaction_id}.json"
            )
            self.assertTrue(handoff_file.is_file())
            for artifact in ("result.json", "configuration.json", "admission.json"):
                (root / artifact).unlink()
            state.fail_review = False
            code, output, error = self.invoke(
                root, state, reconstruct=True, extra=handoff_args
            )
            self.assertEqual(code, 0, error)
            self.assertIn("published", output)

        # 2. #427 Decision-enabled context: ordinary entry captures handoff;
        #    drift refuses closed without ledger mutation; restoring pinned
        #    .reviewsensei.yml recovers to published with zero inference.
        decision = {"enabled": True, "provider": "ollama", "model": "fixture-decision"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.original_analysis(root, decision=decision, work_mode="unified")
            handoff_args = self._enable_diagnostics_and_handoff_args(root)
            original_config_text = (root / ".reviewsensei.yml").read_text(
                encoding="utf-8"
            )
            diff_text = (root / "review.patch").read_text(encoding="utf-8")
            result = ReviewResult.from_dict(
                json.loads((root / "result.json").read_text(encoding="utf-8"))
            )
            assert result.transaction is not None

            state = _FailedPublication()
            self.assertEqual(self.invoke(root, state, extra=handoff_args)[0], 1)
            ledger = LocalSessionLedger(root / "ledger")

            handoff_file = (
                root / "handoff" / f"{result.transaction.transaction_id}.json"
            )
            self.assertTrue(handoff_file.is_file())
            self.assertNotIn("diff --git", handoff_file.read_text(encoding="utf-8"))

            for artifact in ("result.json", "configuration.json", "admission.json"):
                (root / artifact).unlink()

            # Drift #427 decision policy -> CLI recovery must refuse closed without mutating ledger
            (root / ".reviewsensei.yml").write_text(
                original_config_text.replace("fixture-decision", "drifted-decision"),
                encoding="utf-8",
            )
            before_ledger = self.ledger_bytes(root)
            state.fail_review = False
            drift_code, _, drift_err = self.invoke(
                root, state, reconstruct=True, extra=handoff_args
            )
            self.assertEqual(drift_code, 1)
            self.assertIn("original committed transaction", drift_err)
            self.assertEqual(self.ledger_bytes(root), before_ledger)

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

            # Restore pinned config & verify fresh-process CLI recovery with zero inference
            (root / ".reviewsensei.yml").write_text(
                original_config_text, encoding="utf-8"
            )
            code, output, error = self.invoke(
                root, state, reconstruct=True, extra=handoff_args
            )
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

    def test_review_findings_regressions_runtime_flags_refusal_codes_and_receipt_copy(
        self,
    ) -> None:
        from review_sensei.models import ReviewResult
        from review_sensei.review_decision_receipt import canonical_decision_receipt

        # RS-467CD5, RS-05A3ED, RS-5405A3: canonical_decision_receipt returns an unaliased deep copy.
        nested_candidate = {
            "candidate_id": "cand_1",
            "fingerprint": "f" * 64,
            "action": "continue",
            "status": "assessed",
            "assessments": {
                "kind": "behavioral_defect",
                "evidence": "supported",
                "next": "continue",
                "duplicate": "different_concern",
            },
            "suspected_duplicate": False,
            "verification": "supported",
        }
        nested_model = {
            "provider": "openai",
            "requested_model": "gpt-5",
            "actual_model": "gpt-5",
            "actual_provider": "openai",
            "input_tokens": 120,
            "output_tokens": 45,
        }
        raw_receipt: dict[str, object] = {
            "verification_called": True,
            "status": "complete",
            "schema_version": "1.0",
            "provider": "openai",
            "policy_digest": "1" * 64,
            "output_reserved": 256,
            "output_bytes": 128,
            "models": [nested_model],
            "model": "gpt-5",
            "inventory_sha256": "2" * 64,
            "input_sha256": "3" * 64,
            "input_bytes": 512,
            "elapsed_ms": 42,
            "candidates": [nested_candidate],
            "calls": 1,
        }
        copied_receipt = canonical_decision_receipt(raw_receipt)
        self.assertEqual(list(copied_receipt.keys()), sorted(copied_receipt.keys()))
        result = ReviewResult(
            summary="Reviewed",
            comments=(),
            provider="openai",
            decision_pass=raw_receipt,
        )
        digest_before = result.content_digest()
        nested_candidate["action"] = "optional_suggestion"
        nested_candidate["assessments"]["kind"] = "clarification"  # type: ignore[index]
        nested_model["input_tokens"] = 999999
        raw_receipt["calls"] = 99
        assert result.decision_pass is not None
        self.assertEqual(result.decision_pass["calls"], 1)
        self.assertEqual(result.decision_pass["candidates"][0]["action"], "continue")
        self.assertEqual(result.content_digest(), digest_before)

        for bad_receipt, expected_msg in (
            ({**copied_receipt, "schema_version": "2.0"}, "identity is invalid"),
            ({**copied_receipt, "calls": True}, "accounting is invalid"),
            ({**copied_receipt, "candidates": "bad"}, "inventory is invalid"),
            (
                {**copied_receipt, "candidates": [{"candidate_id": "BAD"}]},
                "candidate is invalid",
            ),
            (
                {
                    **copied_receipt,
                    "candidates": [
                        {
                            **copied_receipt["candidates"][0],
                            "assessments": {"kind": "bad"},
                        }
                    ],
                },
                "assessment is invalid",
            ),
            (
                {
                    **copied_receipt,
                    "candidates": [
                        {**copied_receipt["candidates"][0], "action": "human_review"}
                    ],
                },
                "completion is invalid",
            ),
            ({**copied_receipt, "models": "bad"}, "model inventory is invalid"),
            (
                {**copied_receipt, "models": [{"provider": ""}]},
                "model metadata is invalid",
            ),
            (
                {
                    **copied_receipt,
                    "candidates": [
                        {
                            **copied_receipt["candidates"][0],
                            "candidate_id": f"cand_{idx}",
                            "fingerprint": f"{idx:064x}_" + "a" * 60,
                        }
                        for idx in range(220)
                    ],
                },
                "exceeds its byte bound",
            ),
        ):
            with self.assertRaisesRegex(ReviewInputError, expected_msg):
                canonical_decision_receipt(bad_receipt)

        # RS-C43336: ancestor depth & cycle refusals attach non-None pricing.
        sample = _sample_bundle()
        depth_refusal = price_recovery_context_admission(
            sample,
            current_run_id=10,
            ancestor_run_ids=tuple(range(100, 134)),
        )
        self.assertEqual(depth_refusal.refusal_code, "ancestor_depth_exceeded")
        self.assertIsNotNone(depth_refusal.pricing)
        cycle_refusal = price_recovery_context_admission(
            sample,
            current_run_id=10,
            ancestor_run_ids=(11, 10),
        )
        self.assertEqual(cycle_refusal.refusal_code, "circular_ancestor_evidence")
        self.assertIsNotNone(cycle_refusal.pricing)

        # RS-8DC918, RS-431704, RS-D2EF08, RS-6944DE:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.original_analysis(root)
            diff_text = (root / "review.patch").read_text(encoding="utf-8")
            state = _FailedPublication()
            self.assertEqual(self.invoke(root, state)[0], 1)
            ledger = LocalSessionLedger(root / "ledger")

            reconstructed, flagged_decision = (
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
                    runtime_only_flags={
                        "cli_overrides": True,
                        "has_custom_stages": True,
                    },
                )
            )
            self.assertIsNotNone(reconstructed)
            self.assertTrue(flagged_decision.admitted)
            assert flagged_decision.bundle is not None
            self.assertFalse(flagged_decision.bundle.reconstructible_from_defaults)
            self.assertEqual(
                flagged_decision.bundle.runtime_only_fields,
                ("cli_overrides", "custom_stages"),
            )

            # Plain ReviewInputError with misleading "unavailable" / "does not match" words
            # must classify as truncated_or_corrupt_context rather than matching substrings:
            with patch(
                "review_sensei_github.publication_context_recovery.load_configuration",
                side_effect=ReviewInputError(
                    "configuration key is unavailable and does not match schema"
                ),
            ):
                _, config_err_decision = reconstruct_or_prescribe_publication_contexts(
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
            self.assertFalse(config_err_decision.admitted)
            self.assertEqual(
                config_err_decision.refusal_code, "truncated_or_corrupt_context"
            )

            # Malformed api_run_accounting (missing/non-string version) is rejected instead of defaulting:
            loaded = ledger.load(IDENTITY)
            assert loaded.record is not None and reconstructed is not None
            bad_acc_record = SimpleNamespace(
                transaction=loaded.record.transaction,
                api_run_accounting={"version": 123},
            )
            with patch.object(
                ledger,
                "load",
                return_value=SimpleNamespace(status="ok", record=bad_acc_record),
            ):
                with self.assertRaisesRegex(
                    ReviewInputError, "missing a valid version"
                ):
                    bundle_from_reconstructed_contexts(
                        reconstructed,
                        source_root=root,
                        diff=diff_text,
                        ledger=ledger,
                        identity=IDENTITY,
                        base_ref="main",
                        policy_commit=POLICY_COMMIT,
                    )

            for bad_flags, flag_err in (
                ("not-mapping", "must be a mapping"),
                ({1: True}, "keys must be strings"),
                ({"unknown_flag": True}, "unknown runtime_only_flags key"),
                (
                    {"cli_overrides": True, "has_cli_overrides": False},
                    "conflicting runtime_only_flags",
                ),
            ):
                with self.assertRaisesRegex(ReviewInputError, flag_err):
                    bundle_from_reconstructed_contexts(
                        reconstructed,
                        source_root=root,
                        diff=diff_text,
                        ledger=ledger,
                        identity=IDENTITY,
                        base_ref="main",
                        policy_commit=POLICY_COMMIT,
                        runtime_only_flags=bad_flags,  # type: ignore[arg-type]
                    )

            with self.assertRaisesRegex(ReviewInputError, "source is unavailable"):
                bundle_from_reconstructed_contexts(
                    reconstructed,
                    source_root=root / "missing-dir",
                    diff=diff_text,
                    ledger=ledger,
                    identity=IDENTITY,
                    base_ref="main",
                    policy_commit=POLICY_COMMIT,
                )
            with patch.object(
                ledger,
                "load",
                return_value=SimpleNamespace(status="missing", record=None),
            ):
                with self.assertRaisesRegex(ReviewInputError, "ledger is unavailable"):
                    bundle_from_reconstructed_contexts(
                        reconstructed,
                        source_root=root,
                        diff=diff_text,
                        ledger=ledger,
                        identity=IDENTITY,
                        base_ref="main",
                        policy_commit=POLICY_COMMIT,
                    )
            with patch.object(
                ledger,
                "load",
                return_value=SimpleNamespace(
                    status="ok",
                    record=SimpleNamespace(transaction=None, api_run_accounting=None),
                ),
            ):
                with self.assertRaisesRegex(
                    ReviewInputError, "transaction is unavailable"
                ):
                    bundle_from_reconstructed_contexts(
                        reconstructed,
                        source_root=root,
                        diff=diff_text,
                        ledger=ledger,
                        identity=IDENTITY,
                        base_ref="main",
                        policy_commit=POLICY_COMMIT,
                    )
            with patch.object(
                ledger,
                "load",
                return_value=SimpleNamespace(
                    status="ok",
                    record=SimpleNamespace(
                        transaction=loaded.record.transaction,
                        api_run_accounting="not-mapping",
                    ),
                ),
            ):
                with self.assertRaisesRegex(ReviewInputError, "must be a mapping"):
                    bundle_from_reconstructed_contexts(
                        reconstructed,
                        source_root=root,
                        diff=diff_text,
                        ledger=ledger,
                        identity=IDENTITY,
                        base_ref="main",
                        policy_commit=POLICY_COMMIT,
                    )
            with self.assertRaisesRegex(
                ReviewInputError, "admission_context is unavailable"
            ):
                bundle_from_reconstructed_contexts(
                    SimpleNamespace(admission_context=None),  # type: ignore[arg-type]
                    source_root=root,
                    diff=diff_text,
                    ledger=ledger,
                    identity=IDENTITY,
                    base_ref="main",
                    policy_commit=POLICY_COMMIT,
                )

            unfunded_ctx, unfunded_dec = reconstruct_or_prescribe_publication_contexts(
                source_root=root,
                diff=diff_text,
                ledger=ledger,
                identity=IDENTITY,
                base_ref="main",
                base_sha=BASE,
                head_sha=HEAD,
                policy_commit=POLICY_COMMIT,
                policy=ReviewConvergencePolicy(),
                original_api_limit=8,
                already_consumed_requests=8,
            )
            self.assertIsNone(unfunded_ctx)
            self.assertFalse(unfunded_dec.admitted)
            self.assertEqual(
                unfunded_dec.refusal_code, "budget_exhausted_before_effect"
            )


class OrdinaryEntryHostedAndCliRecoveryG17Tests(
    trusted_recovery.TrustedPublicationRecoveryTests
):
    """End-to-end G17 proofs: ordinary entry capture to fresh-process recovery with zero manual save."""

    @staticmethod
    def _make_hosted_fixture(
        directory: str,
        *,
        artifacts: str = "diagnostics",
        limit: int = 512,
    ):
        import subprocess

        from test_review_run import EntryFixture

        class _RecoveryRunEntryFixture(EntryFixture):
            recovery_context_dir: Path | None = None
            recovery_context_key_file: Path | None = None
            recovery_context_ttl_seconds: int | None = None
            override_run_directory: Path | None = None

            def invoke(self, *, human: bool = False) -> int:
                from review_sensei_github import cli as package_cli

                original_main = package_cli.main

                def _inject(argv: list[str] | None = None) -> int:
                    assert argv is not None
                    mutated = list(argv)
                    if (
                        self.override_run_directory is not None
                        and "--run-directory" in mutated
                    ):
                        run_idx = mutated.index("--run-directory")
                        mutated[run_idx + 1] = str(self.override_run_directory)
                    extra: list[str] = []
                    if self.recovery_context_dir is not None:
                        extra.extend(
                            ["--recovery-context-dir", str(self.recovery_context_dir)]
                        )
                    if self.recovery_context_key_file is not None:
                        extra.extend(
                            [
                                "--recovery-context-key-file",
                                str(self.recovery_context_key_file),
                            ]
                        )
                    if self.recovery_context_ttl_seconds is not None:
                        extra.extend(
                            [
                                "--recovery-context-ttl-seconds",
                                str(self.recovery_context_ttl_seconds),
                            ]
                        )
                    if mutated and mutated[0] == "review-run" and extra:
                        mutated = ["review-run", *extra, *mutated[1:]]
                    return original_main(mutated)

                with patch.object(package_cli, "main", side_effect=_inject):
                    return int(super().invoke(human=human))

        fx = _RecoveryRunEntryFixture(directory, limit=limit)
        if artifacts != "none":
            updated = fx.config_bytes.decode("utf-8").replace(
                "github:\n",
                f"github:\n  artifacts: {artifacts}\n",
                1,
            )
            fx.config_bytes = updated.encode("utf-8")
            (fx.root / ".reviewsensei.yml").write_bytes(fx.config_bytes)
            subprocess.run(
                ["git", "-C", str(fx.root), "add", ".reviewsensei.yml"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(fx.root),
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "commit",
                    "--amend",
                    "-m",
                    "fixture",
                ],
                check=True,
                capture_output=True,
            )
            fx.policy = subprocess.check_output(
                ["git", "-C", str(fx.root), "rev-parse", "HEAD"],
                text=True,
                stderr=subprocess.PIPE,
            ).strip()
            fx.policy_ref = fx.policy
        return fx

    def test_default_unconfigured_and_opt_in_policy_gates_write_nothing(self) -> None:
        # 1. Default unconfigured hosted run (even under artifacts: diagnostics) writes nothing.
        with tempfile.TemporaryDirectory() as directory:
            fx = self._make_hosted_fixture(directory, artifacts="diagnostics")
            self.assertEqual(fx.invoke(), 0, fx.error)
            self.assertFalse((fx.root / "handoff").exists())
            self.assertEqual(
                list(fx.root.glob("*recovery*")),
                [],
            )

        # 2. Opting in without artifacts: diagnostics refuses before provider execution.
        with (
            tempfile.TemporaryDirectory() as directory,
            tempfile.TemporaryDirectory() as secret_dir,
        ):
            fx = self._make_hosted_fixture(directory, artifacts="none")
            handoff_dir = Path(secret_dir) / "handoff"
            key_file = Path(secret_dir) / "handoff.key"
            key_file.write_bytes(HANDOFF_KEY)
            key_file.chmod(0o600)
            fx.recovery_context_dir = handoff_dir
            fx.recovery_context_key_file = key_file
            self.assertEqual(fx.invoke(), 1)
            self.assertIn("diagnostics artifact policy", fx.error)
            self.assertEqual(fx.provider_calls, 0)
            self.assertFalse(handoff_dir.exists())

        # 3. Unpaired option, non-private key file, short key file, and symlink key file refuse.
        with tempfile.TemporaryDirectory() as secret_dir:
            sroot = Path(secret_dir)
            self.assertIsNone(
                resolve_recovery_context_store(
                    None, None, artifacts_policy="diagnostics"
                )
            )
            key_file = sroot / "valid.key"
            key_file.write_bytes(HANDOFF_KEY)
            key_file.chmod(0o600)
            with self.assertRaisesRegex(ReviewInputError, "paired directory"):
                resolve_recovery_context_store(
                    sroot / "handoff", None, artifacts_policy="diagnostics"
                )
            open_key = sroot / "open.key"
            open_key.write_bytes(HANDOFF_KEY)
            open_key.chmod(0o644)
            with self.assertRaisesRegex(ReviewInputError, "must be private"):
                resolve_recovery_context_store(
                    sroot / "handoff", open_key, artifacts_policy="diagnostics"
                )
            short_key = sroot / "short.key"
            short_key.write_bytes(b"short")
            short_key.chmod(0o600)
            with self.assertRaisesRegex(ReviewInputError, "unavailable or invalid"):
                resolve_recovery_context_store(
                    sroot / "handoff", short_key, artifacts_policy="diagnostics"
                )
            symlink_key = sroot / "link.key"
            symlink_key.symlink_to(key_file)
            with self.assertRaisesRegex(ReviewInputError, "must be private"):
                resolve_recovery_context_store(
                    sroot / "handoff", symlink_key, artifacts_policy="diagnostics"
                )

    def test_hosted_review_run_ordinary_capture_fresh_successor_and_fail_closed_controls(
        self,
    ) -> None:
        from review_sensei_github.session_ledger import parse_session_comment
        from test_api_work_persistence import IDENTITY as RUN_IDENTITY

        with (
            tempfile.TemporaryDirectory() as directory,
            tempfile.TemporaryDirectory() as secret_dir,
        ):
            fx = self._make_hosted_fixture(directory, artifacts="diagnostics")
            sroot = Path(secret_dir)
            handoff_dir = sroot / "handoff"
            key_file = sroot / "handoff.key"
            key_file.write_bytes(HANDOFF_KEY)
            key_file.chmod(0o600)
            fx.recovery_context_dir = handoff_dir
            fx.recovery_context_key_file = key_file
            fx.recovery_context_ttl_seconds = 600
            fx.provider_summary = "Sensitive model summary must never enter handoff."

            def crash_before_publish() -> None:
                raise OSError("simulated process crash before publication")

            fx.before_publication = crash_before_publish
            self.assertEqual(fx.invoke(), 1)
            self.assertEqual(fx.provider_calls, 1)
            self.assertEqual(len(fx.reviews), 0)

            # Ordinary review-run entry automatically captured the handoff file (no store.save in test!)
            self.assertTrue(handoff_dir.is_dir())
            self.assertEqual(handoff_dir.stat().st_mode & 0o077, 0)
            handoff_files = list(handoff_dir.glob("*.json"))
            self.assertEqual(len(handoff_files), 1)
            handoff_path = handoff_files[0]
            raw_handoff_bytes = handoff_path.read_bytes()
            raw_handoff_text = raw_handoff_bytes.decode("utf-8")

            # Privacy redaction proof: zero raw diffs, prompts, model prose, keys, or tokens stored.
            for forbidden_secret in (
                "diff --git",
                "Sensitive model summary",
                "synthetic-only",
                "synthetic-workflow-read",
                HANDOFF_KEY.decode("ascii"),
            ):
                self.assertNotIn(forbidden_secret, raw_handoff_text)
            parsed_envelope = json.loads(raw_handoff_text)
            self.assertIs(
                parsed_envelope["document"]["bundle"]["contains_raw_diff_or_prompt"],
                False,
            )
            self.assertIs(parsed_envelope["document"]["bundle"]["authority"], False)

            # Remove all local analysis diagnostic files before fresh-process attempt=2 recovery.
            for local_diag in ("configuration.json", "admission.json", "review.json"):
                (fx.root / local_diag).unlink(missing_ok=True)

            assert fx.comment is not None
            saved_ledger_comment = fx.comment["body"]
            fx.before_publication = None
            fx.attempt = 2
            run_counter = [0]

            def reset_for_attempt_2() -> None:
                assert fx.comment is not None
                fx.comment["body"] = saved_ledger_comment
                fx.trace = [entry for entry in fx.trace if entry[0] != 2]
                run_counter[0] += 1
                fresh_run_dir = sroot / f"fresh-runner-{run_counter[0]}"
                fresh_run_dir.mkdir(parents=True, exist_ok=True)
                fx.override_run_directory = fresh_run_dir

            # Control 1: Missing handoff context -> fails closed (absent_context), zero inference/publish.
            reset_for_attempt_2()
            handoff_path.unlink()
            self.assertEqual(fx.invoke(), 1)
            self.assertIn("absent_context", fx.error)
            self.assertEqual(fx.provider_calls, 1)
            self.assertEqual(len(fx.reviews), 0)

            # Control 2: Tampered handoff HMAC -> fails closed (tampered_authentication_or_digest).
            reset_for_attempt_2()
            tampered_env = json.loads(raw_handoff_text)
            tampered_env["authentication"] = "0" * 64
            handoff_path.write_text(json.dumps(tampered_env), encoding="utf-8")
            self.assertEqual(fx.invoke(), 1)
            self.assertIn("tampered_authentication_or_digest", fx.error)
            self.assertEqual(fx.provider_calls, 1)
            self.assertEqual(len(fx.reviews), 0)

            # Control 3: Truncated handoff JSON -> fails closed (truncated_or_corrupt_context).
            reset_for_attempt_2()
            handoff_path.write_bytes(raw_handoff_bytes[:32])
            self.assertEqual(fx.invoke(), 1)
            self.assertIn("truncated_or_corrupt_context", fx.error)
            self.assertEqual(fx.provider_calls, 1)
            self.assertEqual(len(fx.reviews), 0)

            # Control 4: Expired handoff TTL -> fails closed (expired_context) without auto-deleting file.
            reset_for_attempt_2()
            handoff_path.write_bytes(raw_handoff_bytes)
            future_now = datetime.now(timezone.utc) + timedelta(seconds=601)
            original_resolve = resolve_recovery_context_store

            def expired_resolve(*r_args, **r_kwargs):  # type: ignore[no-untyped-def]
                r_kwargs["now"] = lambda: future_now
                return original_resolve(*r_args, **r_kwargs)

            with patch(
                "review_sensei_github.review_run.resolve_recovery_context_store",
                side_effect=expired_resolve,
            ):
                self.assertEqual(fx.invoke(), 1)
            self.assertIn("expired_context", fx.error)
            self.assertTrue(handoff_path.is_file())
            self.assertEqual(fx.provider_calls, 1)
            self.assertEqual(len(fx.reviews), 0)

            # Control 5: Original caller budget exhausted before effect -> fails closed.
            reset_for_attempt_2()
            original_reconstruct = reconstruct_publication_contexts

            def budget_starved_reconstruct(**kwargs):  # type: ignore[no-untyped-def]
                kwargs["already_consumed_requests"] = kwargs["original_api_limit"]
                return original_reconstruct(**kwargs)

            with patch(
                "review_sensei_github.publication_context_recovery.reconstruct_publication_contexts",
                side_effect=budget_starved_reconstruct,
            ):
                self.assertEqual(fx.invoke(), 1)
            self.assertIn("budget_exhausted_before_effect", fx.error)
            self.assertEqual(fx.provider_calls, 1)
            self.assertEqual(len(fx.reviews), 0)

            # Valid fresh-process attempt=2 recovery: authenticates stored bundle and publishes with 0 new inference.
            reset_for_attempt_2()
            self.assertEqual(fx.invoke(), 0, fx.error)
            self.assertEqual(fx.provider_calls, 1)
            self.assertEqual(
                [r["state"] for r in fx.reviews],
                ["COMMENTED", "APPROVED"],
            )
            pub_outcome = json.loads(
                (fx.root / "publication.json").read_text(encoding="utf-8")
            )
            self.assertEqual(pub_outcome["status"], "published")
            final_record = parse_session_comment(
                fx.comment["body"], identity=RUN_IDENTITY
            )
            assert final_record.transaction is not None
            self.assertEqual(final_record.transaction.phase, "publication_succeeded")

    def test_cli_publish_refusal_controls_explicit_context_mode_and_retention_cleanup(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.original_analysis(root)
            config_path = root / ".reviewsensei.yml"
            config_path.write_text(
                config_path.read_text(encoding="utf-8").replace(
                    "github:\n",
                    "github:\n  artifacts: diagnostics\n",
                    1,
                ),
                encoding="utf-8",
            )
            key_file = root / "handoff.key"
            key_file.write_bytes(HANDOFF_KEY)
            key_file.chmod(0o600)
            handoff_dir = root / "handoff"
            handoff_args = (
                "--recovery-context-dir",
                str(handoff_dir),
                "--recovery-context-key-file",
                str(key_file),
                "--policy-commit",
                POLICY_COMMIT,
            )

            # Ordinary CLI publish fails on HTTP 403, but automatically captures handoff first.
            state = _FailedPublication()
            self.assertEqual(self.invoke(root, state, extra=handoff_args)[0], 1)
            handoff_files = list(handoff_dir.glob("*.json"))
            self.assertEqual(len(handoff_files), 1)
            handoff_path = handoff_files[0]
            saved_bytes = handoff_path.read_bytes()

            saved_config_doc = (root / "configuration.json").read_bytes()
            saved_admission_doc = (root / "admission.json").read_bytes()
            for artifact in ("result.json", "configuration.json", "admission.json"):
                (root / artifact).unlink()
            state.fail_review = False

            # CLI missing / tampered / budget-exhausted / placement-drift refusal controls:
            handoff_path.unlink()
            code, _, err = self.invoke(
                root, state, reconstruct=True, extra=handoff_args
            )
            self.assertEqual(code, 1)
            self.assertIn("absent_context", err)

            tampered = json.loads(saved_bytes.decode("utf-8"))
            tampered["authentication"] = "f" * 64
            handoff_path.write_text(json.dumps(tampered), encoding="utf-8")
            code, _, err = self.invoke(
                root, state, reconstruct=True, extra=handoff_args
            )
            self.assertEqual(code, 1)
            self.assertIn("tampered_authentication_or_digest", err)

            handoff_path.write_bytes(saved_bytes)
            code, _, err = self.invoke(
                root,
                state,
                reconstruct=True,
                extra=(
                    *handoff_args,
                    "--original-api-limit",
                    "20",
                    "--prior-consumed-requests",
                    "10",
                ),
            )
            self.assertEqual(code, 1)
            self.assertIn("budget_exhausted_before_effect", err)

            code, _, err = self.invoke(
                root,
                state,
                reconstruct=True,
                extra=(*handoff_args, "--publication-inventory", "legacy"),
            )
            self.assertEqual(code, 1)
            self.assertIn("snapshot_or_transaction_mismatch", err)

            # Explicit-document --recover-retained mode also enforces stored recovery context:
            (root / "configuration.json").write_bytes(saved_config_doc)
            (root / "admission.json").write_bytes(saved_admission_doc)
            explicit_recover_args = [
                "publish",
                "--allow-write",
                "--enable-review",
                "--no-auto-approve",
                "--oidc-token",
                "synthetic",
                "--repository",
                IDENTITY.repository,
                "--repository-id",
                "1",
                "--pull-request",
                "1",
                "--head-sha",
                HEAD,
                "--base-sha",
                BASE,
                "--base-branch",
                "main",
                "--app-slug",
                trusted_recovery.APP,
                "--diff",
                str(root / "review.patch"),
                "--config",
                str(root / ".reviewsensei.yml"),
                "--outcome",
                str(root / "publication-outcome.json"),
                "--session-ledger",
                str(root / "ledger"),
                "--recover-retained",
                "--configuration-context",
                str(root / "configuration.json"),
                "--admission-context",
                str(root / "admission.json"),
                *handoff_args,
                "--original-api-limit",
                "20",
                "--prior-consumed-requests",
                "10",
            ]
            import io
            import os
            from contextlib import redirect_stderr, redirect_stdout

            import review_sensei_github as github
            from review_sensei_github.cli import main as github_main

            err_buf = io.StringIO()
            with (
                patch.dict(os.environ, {"GITHUB_TOKEN": "synthetic-read"}, clear=True),
                patch.multiple(
                    github,
                    GitHubHttp=lambda: state.http,
                    BrokerClient=trusted_recovery._Broker,
                ),
                redirect_stdout(io.StringIO()),
                redirect_stderr(err_buf),
            ):
                self.assertEqual(github_main(explicit_recover_args), 1)
            self.assertIn("budget_exhausted_before_effect", err_buf.getvalue())
            (root / "configuration.json").unlink()
            (root / "admission.json").unlink()

            # Valid fresh-process CLI recovery succeeds with zero inference:
            code, out, err = self.invoke(
                root, state, reconstruct=True, extra=handoff_args
            )
            self.assertEqual(code, 0, err)
            self.assertIn("published", out)

        # Bounded retention cleanup proof: prune_expired removes expired envelopes while keeping active ones.
        base_now = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
        clock = [base_now]
        with tempfile.TemporaryDirectory() as cleanup_dir:
            cpath = Path(cleanup_dir) / "handoff"
            store = RecoveryContextStore(
                cpath, key=HANDOFF_KEY, ttl_seconds=300, now=lambda: clock[0]
            )
            self.assertEqual(store.prune_expired(), ())
            for idx in range(MAX_RECOVERY_HANDOFF_FILES - 1):
                store.save(_sample_bundle(transaction_id=f"{idx:064x}"))
            clock[0] = base_now + timedelta(seconds=200)
            active_path = store.save(_sample_bundle(transaction_id="e" * 64))
            clock[0] = base_now + timedelta(seconds=301)
            removed = store.prune_expired()
            self.assertEqual(len(removed), MAX_RECOVERY_HANDOFF_FILES - 1)
            self.assertTrue(active_path.is_file())
            # Slot capacity is reclaimed for subsequent captures without enlarging the 8-file cap.
            new_path = store.save(_sample_bundle(transaction_id="f" * 64))
            self.assertTrue(new_path.is_file())

    def test_cli_result_retry_does_not_overwrite_handoff_recover_from_rejection_repo_id_none_and_corrupt_accounting(
        self,
    ) -> None:
        import io
        import os
        from contextlib import redirect_stderr, redirect_stdout
        from dataclasses import replace

        import review_sensei_github as github
        from review_sensei_github.cli import main as github_main

        from review_sensei.recovery_context import RecoveryRefusalError

        # 1. `verify_stored_recovery_context` rejects `repository_id=None` mismatch in BOTH directions.
        with tempfile.TemporaryDirectory() as rdir:
            rstore = RecoveryContextStore(Path(rdir) / "handoff", key=HANDOFF_KEY)
            bundle_with_id = _sample_bundle(transaction_id="1" * 64, repository_id=123)
            rstore.save(bundle_with_id)
            with self.assertRaises(RecoveryRefusalError) as err_to_none:
                verify_stored_recovery_context(
                    rstore,
                    expected_bundle=replace(bundle_with_id, repository_id=None),
                    current_run_id=2,
                )
            self.assertEqual(
                err_to_none.exception.refusal_code, "snapshot_or_transaction_mismatch"
            )
            self.assertIn("repository_id", str(err_to_none.exception))

            bundle_without_id = _sample_bundle(
                transaction_id="2" * 64, repository_id=None
            )
            rstore.save(bundle_without_id)
            with self.assertRaises(RecoveryRefusalError) as err_from_none:
                verify_stored_recovery_context(
                    rstore,
                    expected_bundle=replace(bundle_without_id, repository_id=123),
                    current_run_id=2,
                )
            self.assertEqual(
                err_from_none.exception.refusal_code,
                "snapshot_or_transaction_mismatch",
            )
            self.assertIn("repository_id", str(err_from_none.exception))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.original_analysis(root)
            config_path = root / ".reviewsensei.yml"
            config_path.write_text(
                config_path.read_text(encoding="utf-8").replace(
                    "github:\n",
                    "github:\n  artifacts: diagnostics\n",
                    1,
                ),
                encoding="utf-8",
            )
            key_file = root / "handoff.key"
            key_file.write_bytes(HANDOFF_KEY)
            key_file.chmod(0o600)
            handoff_dir = root / "handoff"
            handoff_args = (
                "--recovery-context-dir",
                str(handoff_dir),
                "--recovery-context-key-file",
                str(key_file),
                "--policy-commit",
                POLICY_COMMIT,
            )

            # 2. `--recover-from` rejects `--recovery-context-dir` / `--recovery-context-key-file`
            # BEFORE reading legacy artifact or executing `recover_review` (even with world-readable key).
            insecure_key = root / "insecure.key"
            insecure_key.write_bytes(HANDOFF_KEY)
            insecure_key.chmod(0o644)
            state = _FailedPublication()
            state.fail_review = False
            legacy_args = [
                "publish",
                "--allow-write",
                "--enable-review",
                "--no-auto-approve",
                "--oidc-token",
                "synthetic",
                "--repository",
                IDENTITY.repository,
                "--repository-id",
                "1",
                "--pull-request",
                "1",
                "--head-sha",
                HEAD,
                "--base-sha",
                BASE,
                "--base-branch",
                "main",
                "--app-slug",
                trusted_recovery.APP,
                "--diff",
                str(root / "review.patch"),
                "--outcome",
                str(root / "publication-outcome.json"),
                "--recover-from",
                str(root / "nonexistent-legacy-artifact.json"),
                "--recovery-context-dir",
                str(handoff_dir),
                "--recovery-context-key-file",
                str(insecure_key),
            ]
            legacy_err = io.StringIO()
            with (
                patch.dict(os.environ, {"GITHUB_TOKEN": "synthetic-read"}, clear=True),
                patch.multiple(
                    github,
                    GitHubHttp=lambda: state.http,
                    BrokerClient=trusted_recovery._Broker,
                ),
                redirect_stdout(io.StringIO()),
                redirect_stderr(legacy_err),
            ):
                self.assertEqual(github_main(legacy_args), 1)
            self.assertIn(
                "legacy --recover-from does not accept recovery-context handoff settings",
                legacy_err.getvalue(),
            )
            self.assertEqual(len(state.reviews), 0)

            # 3. Retried `--result` verifies existing handoff envelope rather than overwriting it!
            state.fail_review = True
            self.assertEqual(self.invoke(root, state, extra=handoff_args)[0], 1)
            handoff_files = list(handoff_dir.glob("*.json"))
            self.assertEqual(len(handoff_files), 1)
            handoff_path = handoff_files[0]
            original_handoff_bytes = handoff_path.read_bytes()
            state.fail_review = False

            # 3a. Tampered handoff on `--result` retry fails closed and is NOT overwritten.
            tampered_doc = json.loads(original_handoff_bytes.decode("utf-8"))
            tampered_doc["authentication"] = "0" * 64
            tampered_bytes = json.dumps(tampered_doc).encode("utf-8")
            handoff_path.write_bytes(tampered_bytes)
            code, _, err = self.invoke(root, state, extra=handoff_args)
            self.assertEqual(code, 1)
            self.assertIn("tampered_authentication_or_digest", err)
            self.assertEqual(handoff_path.read_bytes(), tampered_bytes)
            self.assertEqual(len(state.reviews), 0)

            # 3b. Symlink at handoff target on `--result` retry fails closed and is NOT overwritten.
            decoy_target = root / "decoy.json"
            decoy_target.write_bytes(original_handoff_bytes)
            handoff_path.unlink()
            handoff_path.symlink_to(decoy_target)
            code, _, err = self.invoke(root, state, extra=handoff_args)
            self.assertEqual(code, 1)
            self.assertIn("tampered_authentication_or_digest", err)
            self.assertIn("symlink_handoff_rejected", err)
            self.assertTrue(handoff_path.is_symlink())
            self.assertEqual(len(state.reviews), 0)
            handoff_path.unlink()

            # 3c. Expired handoff on `--result` retry fails closed (never mints fresh TTL).
            handoff_path.write_bytes(original_handoff_bytes)
            future_now = datetime.now(timezone.utc) + timedelta(days=365)
            orig_resolve = resolve_recovery_context_store

            def expired_cli_resolve(*r_args, **r_kwargs):  # type: ignore[no-untyped-def]
                r_kwargs["now"] = lambda: future_now
                return orig_resolve(*r_args, **r_kwargs)

            with patch(
                "review_sensei_github.command_bodies.resolve_recovery_context_store",
                side_effect=expired_cli_resolve,
            ):
                code, _, err = self.invoke(root, state, extra=handoff_args)
            self.assertEqual(code, 1)
            self.assertIn("expired_context", err)
            self.assertEqual(handoff_path.read_bytes(), original_handoff_bytes)
            self.assertEqual(len(state.reviews), 0)

            # 3d. Budget-exhausted `--result` retry fails closed (honors --original-api-limit).
            code, _, err = self.invoke(
                root,
                state,
                extra=(
                    *handoff_args,
                    "--original-api-limit",
                    "20",
                    "--prior-consumed-requests",
                    "15",
                ),
            )
            self.assertEqual(code, 1)
            self.assertIn("budget_exhausted_before_effect", err)
            self.assertEqual(len(state.reviews), 0)

            # 4. Malformed `record.api_run_accounting` fails closed (`truncated_or_corrupt_context`)
            # on BOTH `--result` and explicit `--recover-retained` (no silent ACCOUNTING_VERSION fallback).
            orig_load = LocalSessionLedger.load
            for corrupt_accounting in (["not-a-mapping"], {"version": "   "}):

                def corrupt_load(ledger_self, ident, *, _bad=corrupt_accounting):  # type: ignore[no-untyped-def]
                    loaded = orig_load(ledger_self, ident)
                    if loaded.record is not None:
                        object.__setattr__(loaded.record, "api_run_accounting", _bad)
                    return loaded

                with patch.object(LocalSessionLedger, "load", corrupt_load):
                    code_res, _, err_res = self.invoke(root, state, extra=handoff_args)
                self.assertEqual(code_res, 1)
                self.assertIn("truncated_or_corrupt_context", err_res)

                explicit_args = [
                    "publish",
                    "--allow-write",
                    "--enable-review",
                    "--no-auto-approve",
                    "--oidc-token",
                    "synthetic",
                    "--repository",
                    IDENTITY.repository,
                    "--repository-id",
                    "1",
                    "--pull-request",
                    "1",
                    "--head-sha",
                    HEAD,
                    "--base-sha",
                    BASE,
                    "--base-branch",
                    "main",
                    "--app-slug",
                    trusted_recovery.APP,
                    "--diff",
                    str(root / "review.patch"),
                    "--config",
                    str(root / ".reviewsensei.yml"),
                    "--outcome",
                    str(root / "publication-outcome.json"),
                    "--session-ledger",
                    str(root / "ledger"),
                    "--recover-retained",
                    "--configuration-context",
                    str(root / "configuration.json"),
                    "--admission-context",
                    str(root / "admission.json"),
                    *handoff_args,
                ]
                exp_err = io.StringIO()
                with (
                    patch.dict(
                        os.environ, {"GITHUB_TOKEN": "synthetic-read"}, clear=True
                    ),
                    patch.multiple(
                        github,
                        GitHubHttp=lambda: state.http,
                        BrokerClient=trusted_recovery._Broker,
                    ),
                    patch.object(LocalSessionLedger, "load", corrupt_load),
                    redirect_stdout(io.StringIO()),
                    redirect_stderr(exp_err),
                ):
                    self.assertEqual(github_main(explicit_args), 1)
                self.assertIn("truncated_or_corrupt_context", exp_err.getvalue())

            # 5. Valid `--result` retry verifies existing envelope WITHOUT overwriting it and publishes!
            code_ok, out_ok, err_ok = self.invoke(root, state, extra=handoff_args)
            self.assertEqual(code_ok, 0, err_ok)
            self.assertIn("published", out_ok)
            self.assertEqual(handoff_path.read_bytes(), original_handoff_bytes)


if __name__ == "__main__":
    unittest.main()
