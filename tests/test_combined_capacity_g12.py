"""G12 qualification: combined capacity inventory, multibyte UTF-8, and cross-stage replay."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import test_trusted_publication_recovery as trusted_recovery
from review_sensei_github.api_run_history import (
    ActionResultAncestryObservation,
    ArchivedActionResultObservation,
)
from review_sensei_github.publication import parse_finding_index
from review_sensei_github.publication_context_recovery import (
    reconstruct_or_prescribe_publication_contexts,
)
from review_sensei_github.session_ledger import (
    parse_session_comment,
    render_session_comment,
)
from test_api_work_persistence import HEAD, IDENTITY
from test_publication_recovery import BASE as PUB_BASE
from test_publication_recovery import HEAD as PUB_HEAD
from test_publication_recovery import IDENTITY as PUB_IDENTITY
from test_terminal_representability_g34 import _G34CommandFixture

from review_sensei.api_work import (
    ACCOUNTING_VERSION,
    DEFAULT_API_REQUEST_LIMIT,
    PARTITIONED_ACTION_ACCOUNTING_VERSION,
    ApiRunAccounting,
)
from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.combined_capacity import (
    INTERRUPTION_MODES,
    INTERRUPTION_STAGES,
    MAX_PUBLISHED_REVIEW_BODY_BYTES,
    ORDINARY_EVIDENCE_READ_LIMIT,
    REPRESENTATION_LIMIT_INVENTORY,
    RESERVED_FENCE_READS,
    CombinedCapacityError,
    CombinedCapacityEvaluation,
    CombinedComponentFootprint,
    InterruptionReplayResult,
    InterruptionScenario,
    RepresentationLimit,
    build_supported_scenario_matrix,
    evaluate_combined_capacity,
    evaluate_live_session_envelope,
    lookup_representation_limit,
    measure_utf8_text,
    require_combined_capacity,
    simulate_interruption_and_fresh_replay,
    verify_inventory_against_runtime_constants,
)
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewResult
from review_sensei.provenance_manifest import prescribe_recovery
from review_sensei.recovery_context import (
    OriginalRecoveryContextBundle,
    RecoveryContextStore,
)
from review_sensei.session import (
    MAX_SESSION_COMMENT_BYTES,
    MAX_SESSION_RECORD_BYTES,
    LocalSessionLedger,
    SessionRecord,
)


def _make_recovery_bundle(index: int = 1) -> OriginalRecoveryContextBundle:
    tx_hex = f"{index:064x}"
    return OriginalRecoveryContextBundle(
        repository="owner/repo",
        repository_id=123,
        pull_request=7,
        base_ref="main",
        base_sha="a" * 40,
        head_sha="b" * 40,
        policy_commit="e" * 40,
        transaction_id=tx_hex,
        generation=2,
        reservation_id="resv:publish:g12",
        accepted_result_sha256="2" * 64,
        configuration_digest="3" * 64,
        evidence_digest="4" * 64,
        convergence_policy_digest="5" * 64,
        work_policy_digest="6" * 64,
        decision_policy_digest="7" * 64,
        provider_identity="ollama:deepseek-v4.1-flash:cloud",
        selected_context_digest="8" * 64,
        learnings_digest="9" * 64,
        baseline_digest=None,
        diff_sha256="c" * 64,
        placement_mode="complete",
        accounting_version=ACCOUNTING_VERSION,
        producer="github-bot:999",
        run_id=500 + index,
        attempt=1,
        reconstructible_from_defaults=True,
        runtime_only_fields=(),
        authority=False,
    )


class CombinedCapacityG12Tests(unittest.TestCase):
    """Verify G12 limit inventory, multibyte UTF-8, combined overflows, and cross-stage replay."""

    def setUp(self) -> None:
        for target in ("socket.create_connection", "socket.socket.connect"):
            guard = patch(target, side_effect=AssertionError("unexpected network"))
            guard.start()
            self.addCleanup(guard.stop)

    def test_representation_limit_inventory_matches_runtime_constants(self) -> None:
        audit = verify_inventory_against_runtime_constants()
        self.assertIs(audit["combined_guard_required"], True)
        self.assertIs(audit["authority"], False)
        self.assertEqual(audit["ordinary_read_limit"], 60)
        self.assertEqual(audit["reserved_fence_reads"], 4)
        self.assertGreater(
            int(audit["sum_individual_root_subcaps"]),  # type: ignore[arg-type]
            MAX_SESSION_RECORD_BYTES,
        )

        layers = {item.layer for item in REPRESENTATION_LIMIT_INVENTORY}
        self.assertEqual(
            layers,
            {
                "session_root",
                "bounded_evidence",
                "recovery_context",
                "terminal_action",
                "publication_and_api",
            },
        )
        root_lim = lookup_representation_limit("session_record_bytes")
        self.assertEqual(root_lim.ceiling, 32_768)
        self.assertEqual(root_lim.to_dict()["ceiling"], 32_768)
        self.assertEqual(
            lookup_representation_limit("manifest_bytes").ceiling,
            8_192,
        )
        self.assertEqual(
            lookup_representation_limit("published_review_body_bytes").ceiling,
            65_536,
        )

        with self.assertRaises(ReviewInputError):
            lookup_representation_limit("nonexistent_limit")
        with self.assertRaises(ReviewInputError):
            RepresentationLimit("", "x", "bytes", 10, "mod", "why")
        with self.assertRaises(ReviewInputError):
            RepresentationLimit("layer", "x", "bytes", 0, "mod", "why")

    def test_multibyte_utf8_byte_vs_codepoint_accounting_and_surrogate_rejection(
        self,
    ) -> None:
        ascii_m = measure_utf8_text("ReviewSensei")
        self.assertEqual(ascii_m.codepoint_count, 12)
        self.assertEqual(ascii_m.utf8_bytes, 12)
        self.assertFalse(ascii_m.has_multibyte)
        self.assertEqual(ascii_m.max_bytes_per_codepoint, 1)

        latin_m = measure_utf8_text("é" * 300)
        self.assertEqual(latin_m.codepoint_count, 300)
        self.assertEqual(latin_m.utf8_bytes, 600)
        self.assertTrue(latin_m.has_multibyte)
        self.assertEqual(latin_m.max_bytes_per_codepoint, 2)

        cjk_m = measure_utf8_text("審査検証" * 100)
        self.assertEqual(cjk_m.codepoint_count, 400)
        self.assertEqual(cjk_m.utf8_bytes, 1_200)
        self.assertEqual(cjk_m.max_bytes_per_codepoint, 3)
        self.assertGreater(
            cjk_m.ascii_escaped_json_bytes, cjk_m.canonical_json_utf8_bytes
        )

        emoji_m = measure_utf8_text("🧪" * 200)
        self.assertEqual(emoji_m.codepoint_count, 200)
        self.assertEqual(emoji_m.utf8_bytes, 800)
        self.assertEqual(emoji_m.max_bytes_per_codepoint, 4)
        self.assertEqual(emoji_m.to_dict()["utf8_bytes"], 800)

        # Lone surrogates, NULs, and non-strings fail closed:
        with self.assertRaises(ReviewInputError):
            measure_utf8_text("bad\ud800surrogate")
        with self.assertRaises(ReviewInputError):
            measure_utf8_text("bad\x00nul")
        with self.assertRaises(ReviewInputError):
            measure_utf8_text(123)  # type: ignore[arg-type]

    def test_each_component_fits_individually_yet_combined_envelope_fails_all_axes(
        self,
    ) -> None:
        # Axis 1: SessionRecord 32,768B overflow when every sub-component <= its own cap
        fp_record = CombinedComponentFootprint(
            base_shell_bytes=1_600,
            dispositions_bytes=10_000,  # <= 10,240
            convergence_history_bytes=12_000,  # <= 12,288
            assessment_queue_bytes=8_000,  # <= 8,192
            publication_staging_bytes=4_000,  # <= 4,096
            accounting_manifest_bytes=6_000,  # <= 8,192
        )
        ev_record = evaluate_combined_capacity(fp_record)
        self.assertTrue(ev_record.individual_components_fit)
        self.assertFalse(ev_record.combined_envelope_fits)
        self.assertTrue(ev_record.each_component_fits_individually_yet_combined_fails)
        self.assertEqual(
            ev_record.refusal_code, "combined_session_record_bytes_exceeded"
        )
        self.assertEqual(ev_record.recovery.command, "@reviewsensei rescan")
        self.assertFalse(ev_record.authority)
        with self.assertRaises(CombinedCapacityError) as ctx:
            require_combined_capacity(ev_record)
        self.assertIn("combined_session_record_bytes_exceeded", str(ctx.exception))

        # Axis 2: SessionRecord fits (31,000B <= 32,768B), yet 320 consumed grants push comment > 65,536B
        fp_comment = CombinedComponentFootprint(
            base_shell_bytes=1_000,
            dispositions_bytes=9_000,
            convergence_history_bytes=11_000,
            assessment_queue_bytes=6_000,
            accounting_manifest_bytes=4_000,
            consumed_grant_count=320,
        )
        ev_comment = evaluate_combined_capacity(fp_comment)
        self.assertLessEqual(
            ev_comment.projected_record_bytes, MAX_SESSION_RECORD_BYTES
        )
        self.assertTrue(ev_comment.each_component_fits_individually_yet_combined_fails)
        self.assertEqual(
            ev_comment.refusal_code, "combined_session_comment_bytes_exceeded"
        )

        # Axis 3: 16,200 4-byte emoji codepoints (64,800 UTF-8 bytes) + finding index > 65,536B review body
        emoji_summary = "🧪" * 16_200
        self.assertLess(len(emoji_summary), MAX_PUBLISHED_REVIEW_BODY_BYTES)
        fp_review = CombinedComponentFootprint(
            published_review_summary_utf8_bytes=measure_utf8_text(
                emoji_summary
            ).utf8_bytes,
            published_finding_count=10,
        )
        ev_review = evaluate_combined_capacity(fp_review)
        self.assertTrue(ev_review.each_component_fits_individually_yet_combined_fails)
        self.assertEqual(ev_review.refusal_code, "combined_review_body_bytes_exceeded")
        self.assertFalse(ev_review.recovery.requires_fresh_inference)

        # Axis 4: Cumulative stored parts > 256 across session store even though each manifest <= 32 parts
        fp_parts = CombinedComponentFootprint(
            retained_result_part_reads=16,
            stored_parts_count=257,
            stored_parts_total_bytes=1_000_000,
        )
        ev_parts = evaluate_combined_capacity(fp_parts)
        self.assertTrue(ev_parts.each_component_fits_individually_yet_combined_fails)
        self.assertEqual(ev_parts.refusal_code, "combined_stored_parts_exceeded")

        # Axis 5: Ordinary evidence reads (62 > 60) encroach on the 4-call final fence reserve
        fp_reads = CombinedComponentFootprint(
            retained_result_part_reads=18,
            disposition_snapshot_part_reads=16,
            recovery_context_part_reads=8,
            ancestry_part_reads=12,
            publication_reconciliation_reads=8,
        )
        ev_reads = evaluate_combined_capacity(fp_reads)
        self.assertEqual(ev_reads.total_ordinary_part_reads, 62)
        self.assertGreater(
            ev_reads.total_ordinary_part_reads, ORDINARY_EVIDENCE_READ_LIMIT
        )
        self.assertTrue(ev_reads.each_component_fits_individually_yet_combined_fails)
        self.assertEqual(
            ev_reads.refusal_code, "combined_evidence_read_budget_exceeded"
        )

        # Axis 6: Combined physical API requests + unknown usage + protected tail > 512 limit
        fp_api = CombinedComponentFootprint(
            committed_requests=460,
            unknown_usage_requests=12,
            active_reserved_requests=10,
            retained_stage_requests=8,
            publication_requests=12,
            check_requests=4,
            cleanup_requests=4,
            approval_requests=2,
            receipt_requests=2,
            terminal_close_requests=1,
            protected_tail_requests=RESERVED_FENCE_READS,
            original_api_limit=DEFAULT_API_REQUEST_LIMIT,
        )
        ev_api = evaluate_combined_capacity(fp_api)
        self.assertTrue(ev_api.each_component_fits_individually_yet_combined_fails)
        self.assertEqual(ev_api.refusal_code, "combined_api_request_budget_exceeded")
        self.assertLess(ev_api.remaining_api_headroom, 0)

        # Individual component violation vs observation-only & v3 writer non-authority:
        ev_indiv = evaluate_combined_capacity(
            CombinedComponentFootprint(dispositions_bytes=12_000)
        )
        self.assertFalse(ev_indiv.individual_components_fit)
        self.assertFalse(ev_indiv.each_component_fits_individually_yet_combined_fails)
        self.assertEqual(ev_indiv.refusal_code, "individual_component_limit_exceeded")

        ev_v3 = evaluate_combined_capacity(
            CombinedComponentFootprint(
                accounting_version=PARTITIONED_ACTION_ACCOUNTING_VERSION,
                allow_v3_writer=False,
            )
        )
        self.assertEqual(ev_v3.refusal_code, "unsupported_v3_writer")

        obs = ArchivedActionResultObservation(b"{}", b"{}")
        anc_obs = ActionResultAncestryObservation(b"{}", (obs,))
        for candidate in (
            obs,
            anc_obs,
            {"schema_version": "caller-action-result-v1"},
            {"accounting_version": PARTITIONED_ACTION_ACCOUNTING_VERSION},
        ):
            ev_obs = evaluate_combined_capacity(
                CombinedComponentFootprint(), evidence_candidate=candidate
            )
            self.assertFalse(ev_obs.combined_envelope_fits)
            self.assertIn(
                ev_obs.refusal_code,
                {"observation_only_not_authority", "unsupported_v3_writer"},
            )

    def test_live_session_record_multibyte_and_grant_history_envelope(self) -> None:
        record = SessionRecord.create(IDENTITY)
        grants_ok = tuple(f"{i:064x}" for i in range(10))
        ev_ok = evaluate_live_session_envelope(
            record,
            repository_id=123,
            pull_request=7,
            consumed_grants=grants_ok,
            review_summary_text="審査完了 🧪 Clean convergence summary",
            published_finding_count=4,
        )
        self.assertTrue(ev_ok.combined_envelope_fits)
        self.assertIs(require_combined_capacity(ev_ok), ev_ok)
        rendered = render_session_comment(
            repository_id=123,
            pull_request=7,
            record=record,
            consumed_grants=grants_ok,
        )
        self.assertEqual(ev_ok.projected_comment_bytes, len(rendered.encode("utf-8")))
        self.assertFalse(ev_ok.to_dict()["authority"])

        # Exact comment boundary where projected_record_bytes <= 32,768 but exact comment > 65,536:
        grants_580 = tuple(f"{i:064x}" for i in range(580))
        ev_overflow = evaluate_live_session_envelope(
            record,
            repository_id=123,
            pull_request=7,
            consumed_grants=grants_580,
        )
        self.assertLessEqual(
            ev_overflow.projected_record_bytes, MAX_SESSION_RECORD_BYTES
        )
        self.assertGreater(
            ev_overflow.projected_comment_bytes, MAX_SESSION_COMMENT_BYTES
        )
        self.assertFalse(ev_overflow.combined_envelope_fits)
        self.assertEqual(
            ev_overflow.refusal_code, "combined_session_comment_bytes_exceeded"
        )

    def test_finite_scenario_matrix_and_fresh_process_replay_across_all_nine_stages(
        self,
    ) -> None:
        matrix = build_supported_scenario_matrix()
        self.assertEqual(
            len(matrix),
            len(INTERRUPTION_STAGES) * len(INTERRUPTION_MODES) + 12,
        )
        stages_covered = {s.interruption_stage for s in matrix}
        modes_covered = {s.interruption_mode for s in matrix}
        self.assertEqual(stages_covered, set(INTERRUPTION_STAGES))
        self.assertEqual(modes_covered, set(INTERRUPTION_MODES))

        handoff_key = b"g" * 32
        with tempfile.TemporaryDirectory() as tmpdir:
            for idx, scenario in enumerate(matrix, start=1):
                sub_dir = Path(tmpdir) / f"case_{idx:03d}"
                sub_dir.mkdir(mode=0o700)
                bundle = _make_recovery_bundle(idx)
                replay = simulate_interruption_and_fresh_replay(
                    scenario,
                    handoff_directory=sub_dir,
                    handoff_key=handoff_key,
                    recovery_bundle=bundle,
                )
                self.assertFalse(replay.authority)
                self.assertFalse(replay.orphan_parts_trusted)
                self.assertEqual(replay.duplicate_side_effects, 0)
                self.assertEqual(replay.fresh_inference_calls_on_replay, 0)
                self.assertTrue(replay.converged_after_recovery)
                self.assertTrue(replay.recovery.preserves_obligations)
                self.assertTrue(replay.recovery.preserves_reservations)
                self.assertTrue(replay.recovery.preserves_unknown_charges)
                as_dict = replay.to_dict()
                self.assertIs(as_dict["authority"], False)

                if scenario.payload_profile == "combined_overflow":
                    self.assertTrue(
                        replay.envelope_evaluation.each_component_fits_individually_yet_combined_fails
                    )
                    self.assertFalse(replay.fresh_process_handoff_verified)
                else:
                    self.assertTrue(replay.envelope_evaluation.combined_envelope_fits)
                    self.assertTrue(replay.fresh_process_handoff_verified)
                    if scenario.interruption_mode in {
                        "in_flight_not_stored",
                        "in_flight_stored_response_lost",
                    }:
                        self.assertGreater(
                            replay.interrupted_unknown_charges_retained, 0
                        )
                    if (
                        scenario.interruption_stage == "check_publication"
                        and scenario.interruption_mode == "definite_rejection"
                    ):
                        self.assertTrue(replay.accounting_closed_after_replay)
                        self.assertFalse(replay.gate_succeeded_after_replay)

    def test_end_to_end_hosted_interruption_fresh_process_recovery_and_convergence(
        self,
    ) -> None:
        """Hosted multibyte UTF-8 interruptions + fresh-process publication replay."""

        # 1. Lost response BEFORE server storage (business-absent):
        #    proves conservative pending hold and fail-closed writer containment.
        with tempfile.TemporaryDirectory() as directory:
            f = _G34CommandFixture(directory, limit=512)
            subprocess.run(
                ["git", "config", "core.autocrlf", "false"],
                cwd=f.root,
                check=True,
                capture_output=True,
            )
            f.provider_summary = (
                "審査サマリー 🧪 Multibyte UTF-8 summary under envelope"
            )
            f.provider_comments = [
                {
                    "path": "src/app.py",
                    "line": 2,
                    "side": "RIGHT",
                    "body": "外部契約を確認してください 🧪 Confirm caller boundary.",
                    "severity": "high",
                    "blocking": False,
                    "needs_human": True,
                }
            ]
            self.assertEqual(f.invoke(human=True), 0, f.error)
            initial_record = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert initial_record is not None
            live_env = evaluate_live_session_envelope(
                initial_record,
                repository_id=123,
                pull_request=7,
                review_summary_text=f.provider_summary,
                published_finding_count=1,
            )
            self.assertTrue(live_env.combined_envelope_fits)

            index = parse_finding_index(
                f.reviews[-1]["body"], repository_id=123, pull_request=7, head_sha=HEAD
            )
            assert index is not None
            finding_ref = "RS-" + index.entries[0].instance[:6].upper()

            f.SOURCE_ID = 401
            f.select_command(
                f"@reviewsensei override {finding_ref} acceptable risk",
                new_run=True,
            )
            f.action_fault = "business-absent"
            self.assertNotEqual(f.invoke_command(), 0)
            interrupted_record = parse_session_comment(
                f.comment["body"], identity=IDENTITY
            )
            assert interrupted_record is not None
            interrupted_state = ApiRunAccounting.from_document(
                interrupted_record.api_run_accounting
            )
            self.assertEqual(interrupted_state.action["phase"], "pending")
            self.assertEqual(interrupted_record.dispositions, ())
            self.assertEqual(f.business_writes, 1)
            self.assertFalse(any(r["state"] == "APPROVED" for r in f.reviews))

            # Uncoordinated follow-up command refuses fail-closed without duplicate writes:
            f.past_jobs[f.run_id] = f.owner_job
            f.SOURCE_ID = 402
            f.select_command(
                f"@reviewsensei override {finding_ref} second try",
                new_run=True,
            )
            self.assertNotEqual(f.invoke_command(), 0)
            self.assertIn("previous caller run is not terminal", f.error)
            self.assertEqual(f.business_writes, 1)

        # 2. Lost response AFTER server storage (business-stored):
        #    proves bounded owned readback reconciles without duplicate writes and converges!
        with tempfile.TemporaryDirectory() as directory_stored:
            f2 = _G34CommandFixture(directory_stored, limit=512)
            subprocess.run(
                ["git", "config", "core.autocrlf", "false"],
                cwd=f2.root,
                check=True,
                capture_output=True,
            )
            f2.provider_summary = (
                "審査サマリー 🧪 Multibyte UTF-8 summary under envelope"
            )
            f2.provider_comments = [
                {
                    "path": "src/app.py",
                    "line": 2,
                    "side": "RIGHT",
                    "body": "外部契約を確認してください 🧪 Confirm caller boundary.",
                    "severity": "high",
                    "blocking": False,
                    "needs_human": True,
                }
            ]
            self.assertEqual(f2.invoke(human=True), 0, f2.error)
            idx2 = parse_finding_index(
                f2.reviews[-1]["body"], repository_id=123, pull_request=7, head_sha=HEAD
            )
            assert idx2 is not None
            ref2 = "RS-" + idx2.entries[0].instance[:6].upper()

            f2.SOURCE_ID = 501
            f2.select_command(
                f"@reviewsensei override {ref2} acceptable risk",
                new_run=True,
            )
            f2.action_fault = "business-stored"
            self.assertEqual(f2.invoke_command(), 0, f2.error)
            self.assertEqual(f2.business_writes, 1)

            converged_record = parse_session_comment(
                f2.comment["body"], identity=IDENTITY
            )
            assert converged_record is not None
            converged_state = ApiRunAccounting.from_document(
                converged_record.api_run_accounting
            )
            self.assertEqual(converged_state.action["phase"], "terminal")
            self.assertEqual(len(converged_record.dispositions), 1)
            self.assertLessEqual(
                len(canonical_bytes(converged_record.to_dict())),
                MAX_SESSION_RECORD_BYTES,
            )
            self.assertLessEqual(
                len(f2.comment["body"].encode("utf-8")),
                MAX_SESSION_COMMENT_BYTES,
            )
            self.assertEqual(
                sum(r["state"] == "APPROVED" for r in f2.reviews),
                1,
            )
            self.assertEqual(f2.check_runs[-1]["conclusion"], "success")

        # 3. Fresh-process publication interruption & zero-inference CLI replay:
        pub_helper = trusted_recovery.TrustedPublicationRecoveryTests("run")
        with tempfile.TemporaryDirectory() as pub_dir:
            root = Path(pub_dir)
            pub_helper.original_analysis(root)
            diff_text = (root / "review.patch").read_text(encoding="utf-8")
            result = ReviewResult.from_dict(
                json.loads((root / "result.json").read_text(encoding="utf-8"))
            )
            assert result.transaction is not None
            state = trusted_recovery._FailedPublication()
            self.assertEqual(pub_helper.invoke(root, state)[0], 1)

            ledger = LocalSessionLedger(root / "ledger")
            failed_rec = ledger.load(PUB_IDENTITY).record
            assert failed_rec is not None and failed_rec.transaction is not None
            self.assertEqual(failed_rec.transaction.phase, "publication_failed")

            _, admitted = reconstruct_or_prescribe_publication_contexts(
                source_root=root,
                diff=diff_text,
                ledger=ledger,
                identity=PUB_IDENTITY,
                base_ref="main",
                base_sha=PUB_BASE,
                head_sha=PUB_HEAD,
                policy_commit="e" * 40,
                policy=ReviewConvergencePolicy(),
            )
            self.assertTrue(admitted.admitted)
            assert admitted.bundle is not None
            handoff_store = RecoveryContextStore(
                root / "handoff",
                key=b"z" * 32,
                now=lambda: datetime.now(timezone.utc),
            )
            if os.name != "posix":
                # The preceding hosted/convergence subjourneys remain portable.
                # This private-store replay cannot claim Windows ACL qualification.
                retained = {
                    name: (root / name).read_bytes()
                    for name in ("result.json", "configuration.json", "admission.json")
                }
                with self.assertRaisesRegex(
                    ReviewInputError, "unsupported on this platform"
                ):
                    handoff_store.save(admitted.bundle)
                self.assertFalse((root / "handoff").exists())
                for name, content in retained.items():
                    self.assertEqual((root / name).read_bytes(), content)
                self.assertEqual(ledger.load(PUB_IDENTITY).record, failed_rec)
                return
            handoff_store.save(admitted.bundle)
            for artifact in ("result.json", "configuration.json", "admission.json"):
                (root / artifact).unlink()

            fresh_store = RecoveryContextStore(
                root / "handoff",
                key=b"z" * 32,
                now=lambda: datetime.now(timezone.utc),
            )
            loaded_handoff = fresh_store.load_for_fresh_process(
                result.transaction.transaction_id,
                expected_repository=PUB_IDENTITY.repository,
                expected_pull_request=PUB_IDENTITY.pull_request,
                expected_base_sha=PUB_BASE,
                expected_head_sha=PUB_HEAD,
                expected_policy_commit="e" * 40,
                expected_result_sha256=result.content_digest(),
                current_run_id=2,
            )
            self.assertTrue(loaded_handoff.admitted)
            self.assertFalse(loaded_handoff.authority)

            state.fail_review = False
            code, output, error = pub_helper.invoke(root, state, reconstruct=True)
            self.assertEqual(code, 0, error)
            self.assertIn("published", output)
            recovered_pub = ledger.load(PUB_IDENTITY).record
            assert recovered_pub is not None and recovered_pub.transaction is not None
            self.assertEqual(recovered_pub.transaction.phase, "publication_succeeded")
            pub_env = evaluate_live_session_envelope(
                recovered_pub,
                repository_id=PUB_IDENTITY.repository_id,
                pull_request=PUB_IDENTITY.pull_request,
                review_summary_text=result.summary,
            )
            self.assertTrue(pub_env.combined_envelope_fits)

    def test_validation_and_invariant_error_paths(self) -> None:
        with self.assertRaises(ReviewInputError):
            CombinedComponentFootprint(original_api_limit=0)
        with self.assertRaises(ReviewInputError):
            evaluate_combined_capacity(
                CombinedComponentFootprint(accounting_version="caller-requests-v99")
            )

        rx = prescribe_recovery("supported_read_only_observation")
        with self.assertRaises(ReviewInputError):
            CombinedCapacityEvaluation(
                individual_components_fit=False,
                combined_envelope_fits=True,
                refusal_code=None,
                reason="bad",
                component_violations=("x",),
                projected_record_bytes=100,
                projected_comment_bytes=200,
                projected_review_body_bytes=100,
                total_ordinary_part_reads=1,
                total_projected_requests=10,
                remaining_api_headroom=502,
                recovery=rx,
            )
        with self.assertRaises(ReviewInputError):
            CombinedCapacityEvaluation(
                individual_components_fit=True,
                combined_envelope_fits=False,
                refusal_code="bad_code",
                reason="bad",
                component_violations=(),
                projected_record_bytes=100,
                projected_comment_bytes=200,
                projected_review_body_bytes=100,
                total_ordinary_part_reads=1,
                total_projected_requests=10,
                remaining_api_headroom=502,
                recovery=rx,
            )

        for kwargs in (
            {"payload_profile": "bad"},
            {"text_encoding": "utf16"},
            {"history_profile": "infinite"},
            {"interruption_stage": "unknown_stage"},
            {"interruption_mode": "unknown_mode"},
        ):
            base_args = {
                "scenario_id": "S1",
                "payload_profile": "compact",
                "text_encoding": "ascii",
                "history_profile": "minimal",
                "interruption_stage": "review_publication",
                "interruption_mode": "before_dispatch",
                **kwargs,
            }
            with self.assertRaises(ReviewInputError):
                InterruptionScenario(**base_args)

        valid_scen = InterruptionScenario(
            scenario_id="S1",
            payload_profile="compact",
            text_encoding="ascii",
            history_profile="minimal",
            interruption_stage="review_publication",
            interruption_mode="before_dispatch",
        )
        valid_eval = evaluate_combined_capacity(CombinedComponentFootprint())
        with self.assertRaises(ReviewInputError):
            InterruptionReplayResult(
                scenario=valid_scen,
                envelope_evaluation=valid_eval,
                interrupted_unknown_charges_retained=0,
                orphan_parts_trusted=True,
                owned_readback_reconciled=True,
                duplicate_side_effects=0,
                fresh_process_handoff_verified=False,
                fresh_inference_calls_on_replay=0,
                accounting_closed_after_replay=True,
                gate_succeeded_after_replay=True,
                converged_after_recovery=True,
                recovery=rx,
            )
        with self.assertRaises(ReviewInputError):
            InterruptionReplayResult(
                scenario=valid_scen,
                envelope_evaluation=valid_eval,
                interrupted_unknown_charges_retained=0,
                orphan_parts_trusted=False,
                owned_readback_reconciled=True,
                duplicate_side_effects=1,
                fresh_process_handoff_verified=False,
                fresh_inference_calls_on_replay=0,
                accounting_closed_after_replay=True,
                gate_succeeded_after_replay=True,
                converged_after_recovery=True,
                recovery=rx,
            )

        default_replay = simulate_interruption_and_fresh_replay(valid_scen)
        self.assertTrue(default_replay.fresh_process_handoff_verified)
        self.assertTrue(default_replay.converged_after_recovery)

        opt_out_replay = simulate_interruption_and_fresh_replay(
            valid_scen, verify_fresh_handoff=False
        )
        self.assertFalse(opt_out_replay.fresh_process_handoff_verified)
        self.assertTrue(opt_out_replay.converged_after_recovery)

        with self.assertRaisesRegex(ReviewInputError, "must all be provided together"):
            simulate_interruption_and_fresh_replay(valid_scen, handoff_key=b"k" * 32)

        # RS-FB3471: accounting_version validated before evidence_candidate early return:
        from types import SimpleNamespace

        obs_candidate = SimpleNamespace(anchor_document={}, ancestors=())
        with self.assertRaisesRegex(ReviewInputError, "unsupported accounting_version"):
            evaluate_combined_capacity(
                CombinedComponentFootprint(accounting_version="caller-requests-v99"),
                evidence_candidate=obs_candidate,
            )
        v3_before_obs = evaluate_combined_capacity(
            CombinedComponentFootprint(
                accounting_version=PARTITIONED_ACTION_ACCOUNTING_VERSION,
                allow_v3_writer=False,
            ),
            evidence_candidate=obs_candidate,
        )
        self.assertEqual(v3_before_obs.refusal_code, "unsupported_v3_writer")

        # RS-431704: evaluate_live_session_envelope rejects malformed api_run_accounting:
        from review_sensei.session import SessionIdentity

        bad_live_record = SessionRecord.create(
            SessionIdentity("malsabbagh/review-sensei", 448, 101)
        )
        object.__setattr__(bad_live_record, "api_run_accounting", {"version": None})
        with self.assertRaisesRegex(ReviewInputError, "missing a valid version"):
            evaluate_live_session_envelope(
                bad_live_record,
                repository_id=101,
                pull_request=448,
            )

        # RS-1378BC: exact ORDINARY_EVIDENCE_READ_LIMIT (60) vs +1 (61) boundary:
        exact_60 = evaluate_combined_capacity(
            CombinedComponentFootprint(
                retained_result_part_reads=30,
                disposition_snapshot_part_reads=30,
            )
        )
        self.assertTrue(exact_60.combined_envelope_fits)
        self.assertEqual(
            exact_60.total_ordinary_part_reads, ORDINARY_EVIDENCE_READ_LIMIT
        )
        over_61 = evaluate_combined_capacity(
            CombinedComponentFootprint(
                retained_result_part_reads=30,
                disposition_snapshot_part_reads=30,
                recovery_context_part_reads=1,
            )
        )
        self.assertFalse(over_61.combined_envelope_fits)
        self.assertEqual(over_61.refusal_code, "combined_evidence_read_budget_exceeded")


if __name__ == "__main__":
    unittest.main()
