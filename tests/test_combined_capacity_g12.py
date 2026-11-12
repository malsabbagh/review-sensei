"""G12 qualification: combined capacity inventory, multibyte UTF-8, and cross-stage replay."""

from __future__ import annotations

import json
import os
import subprocess
import sys
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
    BoundaryInterruptionResult,
    CombinedCapacityError,
    CombinedCapacityEvaluation,
    CombinedComponentFootprint,
    InterruptionReplayResult,
    InterruptionScenario,
    RepresentationLimit,
    build_supported_scenario_matrix,
    decompose_session_and_result_footprint,
    estimate_lifecycle_future_growth_bytes,
    evaluate_combined_capacity,
    evaluate_live_session_envelope,
    lookup_representation_limit,
    measure_utf8_text,
    qualify_retained_interruption_replay,
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
                if (
                    os.name != "posix"
                    and scenario.payload_profile != "combined_overflow"
                ):
                    with self.assertRaisesRegex(
                        ReviewInputError, "unsupported on this platform"
                    ):
                        simulate_interruption_and_fresh_replay(
                            scenario,
                            handoff_directory=sub_dir,
                            handoff_key=handoff_key,
                            recovery_bundle=bundle,
                        )
                    self.assertEqual(list(sub_dir.iterdir()), [])
                    # Keep the portable simulation assertions, without claiming
                    # a private-store handoff or genuine process replay.
                    replay = simulate_interruption_and_fresh_replay(
                        scenario, verify_fresh_handoff=False
                    )
                else:
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
                    self.assertEqual(
                        replay.fresh_process_handoff_verified, os.name == "posix"
                    )
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

        if os.name == "posix":
            default_replay = simulate_interruption_and_fresh_replay(valid_scen)
            self.assertTrue(default_replay.fresh_process_handoff_verified)
            self.assertTrue(default_replay.converged_after_recovery)
        else:
            with self.assertRaisesRegex(
                ReviewInputError, "unsupported on this platform"
            ):
                simulate_interruption_and_fresh_replay(valid_scen)

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
        corrupt_v2_record = SessionRecord.create(
            SessionIdentity("malsabbagh/review-sensei", 448, 101)
        )
        object.__setattr__(
            corrupt_v2_record,
            "api_run_accounting",
            {"version": ACCOUNTING_VERSION, "confirmed": "corrupt"},
        )
        with self.assertRaises(ReviewInputError):
            evaluate_live_session_envelope(
                corrupt_v2_record,
                repository_id=101,
                pull_request=448,
            )
        with self.assertRaises(ReviewInputError):
            qualify_retained_interruption_replay(
                corrupt_v2_record,
                identity=SessionIdentity("malsabbagh/review-sensei", 448, 101),
                interruption_stage="review_publication",
                interruption_mode="in_flight_not_stored",
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

        # Protected 4-call terminal fence tail cannot be under-priced:
        under_tail = evaluate_combined_capacity(
            CombinedComponentFootprint(protected_tail_requests=3)
        )
        self.assertFalse(under_tail.individual_components_fit)
        self.assertEqual(under_tail.refusal_code, "individual_component_limit_exceeded")
        self.assertIn("protected_tail_requests=3", under_tail.reason)

    def test_complete_session_record_retained_result_future_growth_and_intermediate_peaks(
        self,
    ) -> None:
        """Decompose actual ReviewResult, complete SessionRecord (with snapshot & archive), future growth, and intermediate peaks."""
        from test_terminal_representability_g34 import _deterministic_valid_path

        from review_sensei.api_work import (
            ApiExecutionOwner,
            ApiRunIdentity,
            FrozenApiConfiguration,
            PendingApiRequest,
            StartupPlan,
            StartupSlot,
        )
        from review_sensei.bounded_evidence import (
            EvidenceReadBudget,
            evidence_manifest,
            partition_evidence,
        )
        from review_sensei.coverage import CoverageManifest, FileCoverage
        from review_sensei.disposition import (
            FindingIdentity,
            MaintainerCommand,
            apply_session_command,
        )
        from review_sensei.models import ReviewComment
        from review_sensei.session import ReviewTransaction
        from review_sensei.terminal_representability import (
            build_action_result_binding,
            preview_session_command_representability,
        )

        fixed_now = datetime(2026, 11, 11, 0, 0, tzinfo=timezone.utc)
        head_sha = "b" * 40
        tx_id = ReviewTransaction.derive_id(
            repository=IDENTITY.repository,
            pull_request=IDENTITY.pull_request,
            base_sha="a" * 40,
            head_sha=head_sha,
            policy_digest="c" * 64,
            configuration_digest="d" * 64,
            evidence_digest="e" * 64,
            reservation_id="2" * 64,
        )
        tx = ReviewTransaction(
            transaction_id=tx_id,
            repository=IDENTITY.repository,
            pull_request=IDENTITY.pull_request,
            base_sha="a" * 40,
            head_sha=head_sha,
            policy_digest="c" * 64,
            configuration_digest="d" * 64,
            evidence_digest="e" * 64,
            generation=1,
            reservation_id="2" * 64,
            phase="publication_succeeded",
            result_sha256="f" * 64,
        )
        files_12 = tuple(
            FileCoverage(
                path=_deterministic_valid_path(i, total_len=227),
                outcome="budget-exhausted",
            )
            for i in range(12)
        )
        retained = ReviewResult(
            summary="審査完了 🧪 Partial convergence summary with multibyte UTF-8",
            comments=(
                ReviewComment(
                    path="src/app.py",
                    line=12,
                    severity="high",
                    body="Guard missing on snapshot state",
                ),
            ),
            provider="openai:gpt-5",
            review_status="partial",
            coverage=CoverageManifest(
                files=files_12,
                enumerated_paths=tuple(f.path for f in files_12),
            ),
        )

        # Build authentic ApiRunAccounting with startup liability, archived predecessor, and pending unknown request:
        startup = StartupPlan(
            (
                StartupSlot("review", 1, ("pr_get", "comments_list", "terminal_patch")),
                StartupSlot("review", 2, ("pr_get", "comments_list", "terminal_patch")),
            )
        )
        prev_binding = {
            "repository": IDENTITY.repository,
            "repository_id": IDENTITY.repository_id,
            "pull_request": IDENTITY.pull_request,
            "base_sha": "a" * 40,
            "head_sha": head_sha,
            "policy_digest": "c" * 64,
            "configuration_digest": "d" * 64,
            "generation": 0,
            "producer": "local-ledger",
            "purpose": "evidence",
            "schema_version": "1.0",
        }
        prev_doc = {"archived_run_id": 100}
        prev_parts = partition_evidence(prev_doc, binding=prev_binding, item_count=1)
        prev_manifest = evidence_manifest(
            prev_doc,
            binding=prev_binding,
            item_count=1,
            parts=prev_parts,
            storage_ids=("9001",),
        )
        acc = ApiRunAccounting(
            version=ACCOUNTING_VERSION,
            identity=ApiRunIdentity(IDENTITY.repository_id or 123, 101),
            owner=ApiExecutionOwner(1, "review", 1),
            configuration=FrozenApiConfiguration("e" * 40, "d" * 64, 512),
            startup=startup,
            startup_used=(("review", 1),),
            previous_record={
                "run_id": 100,
                "root_sha256": "9" * 64,
                "manifest": prev_manifest,
            },
        )
        acc = acc.reserve("b0", 3)
        req1 = PendingApiRequest("r1", 1_700_000_000_001)
        req2 = PendingApiRequest("r2", 1_700_000_000_002)
        acc = acc.dispatch("b0", req1)
        acc = acc.confirm(req1)
        acc = acc.dispatch("b0", req2)  # left pending/uncertain!

        with tempfile.TemporaryDirectory() as tmp:
            ledger = LocalSessionLedger(
                Path(tmp),
                enable_disposition_snapshot_writes=True,
                evidence_budget=EvidenceReadBudget(),
            )
            rec = ledger.initialize(IDENTITY, now=fixed_now)
            rec = ledger.replace(
                IDENTITY,
                lambda r: r.evolve(
                    completed_initial_reviews=1,
                    last_committed_reservation_id="2" * 64,
                    transaction=tx,
                    api_run_accounting=acc.to_document(),
                ),
                now=fixed_now,
            )
            finding = FindingIdentity(instance="abcdef" + "0" * 58, needs_human=True)
            rec, _ = apply_session_command(
                ledger,
                IDENTITY,
                MaintainerCommand(
                    action="accept-risk",
                    actor="alice",
                    reason="ok",
                    head_sha=head_sha,
                    finding_references=("RS-ABCDEF",),
                ),
                finding_inventory=(finding,),
                actor_role="OWNER",
                policy_digest="c" * 64,
                provenance="comment:1",
                now=fixed_now,
            )
            # Stage large 2nd decision so disposition_snapshot is created on rec:
            files_37 = tuple(
                FileCoverage(
                    path=_deterministic_valid_path(i, total_len=227),
                    outcome="budget-exhausted",
                )
                for i in range(37)
            )
            res_37 = ReviewResult(
                summary="Partial",
                comments=(),
                provider="openai:gpt-5",
                review_status="partial",
                coverage=CoverageManifest(
                    files=files_37,
                    enumerated_paths=tuple(f.path for f in files_37),
                ),
            )
            from review_sensei.disposition import incomplete_coverage_from_result

            rec, _ = apply_session_command(
                ledger,
                IDENTITY,
                MaintainerCommand(
                    action="accept-coverage-uncertainty",
                    actor="alice",
                    reason="ok",
                    head_sha=head_sha,
                ),
                actor_role="OWNER",
                policy_digest="c" * 64,
                provenance="comment:2",
                incomplete_coverage=incomplete_coverage_from_result(res_37),
                now=fixed_now,
            )
            assert rec.disposition_snapshot is not None

            fp = decompose_session_and_result_footprint(
                rec,
                repository_id=123,
                pull_request=7,
                retained_result=retained,
                include_future_growth=True,
                include_intermediate_staging_peak=True,
                retry_reconcile_requests=2,
            )
            self.assertGreater(fp.disposition_snapshot_root_bytes, 400)
            self.assertEqual(fp.disposition_snapshot_part_reads, 1)
            self.assertGreaterEqual(fp.ancestry_part_reads, 1)
            self.assertEqual(fp.startup_requests, 6)
            self.assertEqual(fp.committed_requests, 1)
            self.assertEqual(fp.unknown_usage_requests, 1)
            self.assertEqual(fp.active_reserved_requests, 1)
            self.assertEqual(fp.coverage_entry_count, 13)
            self.assertEqual(fp.max_coverage_token_bytes, 249)
            self.assertGreater(fp.future_growth_bytes, 100)
            self.assertGreater(
                fp.max_intermediate_record_bytes, fp.projected_record_bytes
            )

            ev_live = evaluate_live_session_envelope(
                rec,
                repository_id=123,
                pull_request=7,
                retained_result=retained,
                include_future_growth=True,
                include_intermediate_staging_peak=True,
                retry_reconcile_requests=2,
            )
            self.assertTrue(ev_live.combined_envelope_fits)

            # Future growth alone pushes a near-ceiling record > 32,768B:
            growth_only = estimate_lifecycle_future_growth_bytes(rec)
            self.assertGreater(growth_only, 0)
            near_ceiling_reserve = (
                MAX_SESSION_RECORD_BYTES
                - len(canonical_bytes(rec.to_dict()))
                - growth_only
                + 16
            )
            ev_without_growth = evaluate_live_session_envelope(
                rec,
                repository_id=123,
                pull_request=7,
                future_root_reserve_bytes=near_ceiling_reserve,
                include_future_growth=False,
            )
            self.assertTrue(ev_without_growth.combined_envelope_fits)
            ev_with_growth = evaluate_live_session_envelope(
                rec,
                repository_id=123,
                pull_request=7,
                future_root_reserve_bytes=near_ceiling_reserve,
                include_future_growth=True,
            )
            self.assertFalse(ev_with_growth.combined_envelope_fits)
            self.assertEqual(
                ev_with_growth.refusal_code, "combined_session_record_bytes_exceeded"
            )

            # G34 2-decision snapshot replay counterexample caught inside G12 combined envelope:
            shared_budget = EvidenceReadBudget()
            _, _, g34_rep = preview_session_command_representability(
                identity=IDENTITY,
                current_record=rec,
                command=MaintainerCommand(
                    action="accept-risk",
                    actor="alice",
                    reason="ok",
                    head_sha=head_sha,
                    finding_references=("RS-ABCDEF",),
                ),
                intent_sha256="1" * 64,
                binding=build_action_result_binding(
                    identity=IDENTITY,
                    policy_commit="e" * 40,
                    head_sha=head_sha,
                    intent_sha256="1" * 64,
                    configuration_digest="d" * 64,
                    owner_generation=1,
                    producer="local-ledger",
                ),
                finding_inventory=(finding,),
                actor_role="OWNER",
                policy_digest="c" * 64,
                provenance="comment:3",
                enable_disposition_snapshot_writes=True,
                evidence_budget=shared_budget,
                source_ledger=ledger,
                now=fixed_now,
            )
            self.assertEqual(shared_budget.calls, 0)
            self.assertFalse(g34_rep.representable)
            g34_replay = qualify_retained_interruption_replay(
                rec,
                identity=IDENTITY,
                interruption_stage="terminal_accounting_close",
                interruption_mode="before_dispatch",
                retained_result=res_37,
                terminal_report=g34_rep,
                ledger_directory=Path(tmp),
            )
            self.assertFalse(g34_replay.envelope_evaluation.combined_envelope_fits)
            self.assertEqual(
                g34_replay.envelope_evaluation.refusal_code,
                "individual_component_limit_exceeded",
            )
            self.assertEqual(g34_rep.carrier_bytes, 9_294)
            self.assertIn(
                f"terminal_action_manifest_bytes={g34_rep.carrier_bytes} exceeds 8192",
                g34_replay.envelope_evaluation.reason,
            )
            self.assertFalse(g34_replay.fresh_ledger_reloaded)
            self.assertFalse(g34_replay.retained_snapshot_preserved)
            self.assertEqual(g34_replay.qualification_status, "capacity_refused")

            # Valid retained 2-decision snapshot state crossed with all 4 interruption modes + fresh-process replay:
            for mode in INTERRUPTION_MODES:
                ok_replay = qualify_retained_interruption_replay(
                    rec,
                    identity=IDENTITY,
                    interruption_stage="review_publication",
                    interruption_mode=mode,
                    retained_result=retained,
                    ledger_directory=Path(tmp),
                )
                self.assertTrue(ok_replay.envelope_evaluation.combined_envelope_fits)
                self.assertTrue(ok_replay.fresh_ledger_reloaded)
                self.assertFalse(ok_replay.fresh_handoff_verified)
                self.assertTrue(ok_replay.retained_snapshot_preserved)
                self.assertTrue(ok_replay.protected_fence_tail_preserved)
                self.assertIsNone(ok_replay.duplicate_side_effects)
                self.assertIsNone(ok_replay.fresh_inference_calls)
                self.assertFalse(ok_replay.to_dict()["authority"])
                self.assertEqual(
                    ok_replay.qualification_status, "snapshot_graph_verified"
                )
                self.assertEqual(
                    ok_replay.qualified_components,
                    ("session_root", "disposition_snapshot"),
                )
                self.assertGreater(ok_replay.physical_reads, 0)
                self.assertIn("archive", ok_replay.to_dict()["unqualified_components"])
                if mode in {"in_flight_not_stored", "in_flight_stored_response_lost"}:
                    self.assertGreaterEqual(ok_replay.retained_unknown_charges, 2)

            # A matching root is insufficient: its referenced bytes must exist
            # and authenticate in the child, independently of this warm ledger.
            assert rec.disposition_snapshot is not None
            manifest = rec.disposition_snapshot["state_manifest"]
            part_id = manifest["parts"][0]["storage_id"]
            part_path = next(Path(tmp).rglob(part_id))
            original = part_path.read_bytes()
            for mutation in ("missing", "corrupt"):
                with self.subTest(mutation=mutation):
                    if mutation == "missing":
                        part_path.unlink()
                    else:
                        part_path.write_bytes(b"{}")
                    try:
                        with self.assertRaisesRegex(
                            ReviewInputError, "subprocess authentication failed"
                        ):
                            qualify_retained_interruption_replay(
                                rec,
                                identity=IDENTITY,
                                interruption_stage="review_publication",
                                interruption_mode="before_dispatch",
                                ledger_directory=Path(tmp),
                            )
                    finally:
                        part_path.write_bytes(original)

    def test_paged_snapshot_subprocess_authenticates_each_retained_part(self) -> None:
        from test_paged_disposition_snapshots import (
            HEAD as SNAPSHOT_HEAD,
        )
        from test_paged_disposition_snapshots import (
            IDENTITY as SNAPSHOT_IDENTITY,
        )
        from test_paged_disposition_snapshots import (
            BatchCandidateLedger,
            LocalDispositionBatchTests,
            decision,
        )

        from review_sensei.bounded_evidence import EvidenceReadBudget
        from review_sensei.session import stage_session_dispositions

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            record = LocalDispositionBatchTests.record()
            seed = LocalSessionLedger(path)
            seed.initialize(SNAPSHOT_IDENTITY)
            seed.replace(
                SNAPSHOT_IDENTITY,
                lambda current: current.evolve(
                    generation=1,
                    transaction=record.transaction,
                    last_committed_reservation_id=record.last_committed_reservation_id,
                ),
            )
            writer = BatchCandidateLedger(
                path,
                evidence_budget=EvidenceReadBudget(),
                enable_disposition_snapshot_writes=True,
            )
            values = tuple(decision(i) for i in range(33))
            written = writer.replace(
                SNAPSHOT_IDENTITY,
                lambda current: stage_session_dispositions(
                    writer,
                    current,
                    values,
                    head_sha=SNAPSHOT_HEAD,
                    now=None,
                ),
            )
            args = dict(
                identity=SNAPSHOT_IDENTITY,
                interruption_stage="review_publication",
                interruption_mode="before_dispatch",
                ledger_directory=path,
            )
            receipt = qualify_retained_interruption_replay(written, **args)
            self.assertEqual(receipt.qualification_status, "snapshot_graph_verified")
            self.assertEqual(written.disposition_snapshot["schema_version"], "2.0")
            parts = LocalDispositionBatchTests.parts(path)
            self.assertGreater(len(parts), 1)
            for relative, original in parts.items():
                with self.subTest(part=relative):
                    target = path / relative
                    target.write_bytes(b"{}")
                    try:
                        with self.assertRaisesRegex(
                            ReviewInputError, "subprocess authentication failed"
                        ):
                            qualify_retained_interruption_replay(written, **args)
                    finally:
                        target.write_bytes(original)

    def test_snapshot_qualification_requires_actual_artifacts_and_reports_root_only(
        self,
    ) -> None:
        record = SessionRecord.create(IDENTITY)
        args = dict(
            identity=IDENTITY,
            interruption_stage="review_publication",
            interruption_mode="before_dispatch",
        )
        missing = qualify_retained_interruption_replay(record, **args)
        self.assertEqual(missing.qualification_status, "retained_artifacts_required")
        self.assertFalse(missing.fresh_ledger_reloaded)
        self.assertFalse(missing.fresh_handoff_verified)
        self.assertEqual(missing.qualified_components, ())
        self.assertIsNone(missing.physical_reads)
        self.assertIsNone(missing.duplicate_side_effects)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ReviewInputError, "directory is required"):
                qualify_retained_interruption_replay(
                    record, ledger_directory=root / "absent", **args
                )
            self.assertFalse((root / "absent").exists())
            ledger = LocalSessionLedger(root)
            stored = ledger.initialize(IDENTITY)
            receipt = qualify_retained_interruption_replay(
                stored, ledger_directory=root, **args
            )
            self.assertEqual(receipt.qualification_status, "root_verified")
            self.assertEqual(receipt.qualified_components, ("session_root",))
            self.assertFalse(receipt.retained_snapshot_preserved)
            self.assertFalse(receipt.fresh_handoff_verified)
            self.assertGreater(receipt.physical_reads, 0)
            with patch(
                "review_sensei.combined_capacity._run_snapshot_probe",
                side_effect=subprocess.TimeoutExpired("snapshot-probe", 1),
            ):
                with self.assertRaisesRegex(
                    ReviewInputError, "subprocess unavailable or timed out"
                ):
                    qualify_retained_interruption_replay(
                        stored, ledger_directory=root, **args
                    )
            for output in (
                b"not-json",
                b'{"components": ["session_root"], "physical_reads": true}',
                b"x" * 1025,
            ):
                with (
                    self.subTest(output_size=len(output)),
                    patch(
                        "review_sensei.combined_capacity._run_snapshot_probe",
                        return_value=subprocess.CompletedProcess(
                            [], 0, stdout=output, stderr=b""
                        ),
                    ),
                ):
                    with self.assertRaisesRegex(
                        ReviewInputError,
                        "subprocess (receipt invalid|authentication failed)",
                    ):
                        qualify_retained_interruption_replay(
                            stored, ledger_directory=root, **args
                        )
            with self.assertRaisesRegex(
                ReviewInputError, "subprocess authentication failed"
            ):
                qualify_retained_interruption_replay(
                    stored.evolve(completed_initial_reviews=1),
                    ledger_directory=root,
                    **args,
                )

    def test_retained_qualification_rejects_missing_repository_scope(self) -> None:
        from dataclasses import replace

        args = dict(
            interruption_stage="review_publication", interruption_mode="before_dispatch"
        )
        missing_id = replace(IDENTITY, repository_id=None)
        with self.assertRaisesRegex(ReviewInputError, "repository_id is required"):
            qualify_retained_interruption_replay(
                SessionRecord.create(missing_id), identity=missing_id, **args
            )

    def test_snapshot_probe_pins_reader_and_bounds_live_output(self) -> None:
        import review_sensei.combined_capacity as capacity

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = LocalSessionLedger(root / "ledger")
            stored = ledger.initialize(IDENTITY)
            args = dict(
                identity=IDENTITY,
                interruption_stage="review_publication",
                interruption_mode="before_dispatch",
                ledger_directory=root / "ledger",
            )
            # A cwd/PYTHONPATH shadow must never execute, even if it can forge
            # the otherwise plausible root-only receipt.
            shadow = root / "review_sensei"
            shadow.mkdir()
            marker = root / "shadow-executed"
            (shadow / "__init__.py").write_text(
                f"from pathlib import Path; Path({str(marker)!r}).touch(); raise RuntimeError()"
            )
            real_popen = subprocess.Popen

            def shadow_cwd(*a, **kw):
                return real_popen(*a, **kw, cwd=root)

            with (
                patch.dict(os.environ, {"PYTHONPATH": str(root)}),
                patch(
                    "review_sensei.combined_capacity.subprocess.Popen",
                    side_effect=shadow_cwd,
                ),
            ):
                result = qualify_retained_interruption_replay(stored, **args)
            self.assertEqual(result.qualification_status, "root_verified")
            self.assertFalse(marker.exists())

            identity = capacity._snapshot_reader_identity()
            mismatched = {path: "0" * 64 for path in identity}
            with patch.object(
                capacity, "_snapshot_reader_identity", return_value=mismatched
            ):
                with self.assertRaisesRegex(ReviewInputError, "authentication failed"):
                    qualify_retained_interruption_replay(stored, **args)

            # Exercise the real bounded pipe, not a mocked CompletedProcess.
            flood = "import os; " + "\nwhile True: os.write(1, b'x' * 4096)"
            with patch.object(
                capacity,
                "_snapshot_probe_command",
                return_value=[sys.executable, "-I", "-c", flood],
            ):
                with self.assertRaisesRegex(ReviewInputError, "authentication failed"):
                    qualify_retained_interruption_replay(stored, **args)

            receipt = json.dumps(
                {
                    "components": ["session_root"],
                    "physical_reads": 1,
                    "reader_identity": identity,
                }
            )
            stderr = f"import os; os.write(2, b'x' * 1048576); print({receipt!r})"
            with patch.object(
                capacity,
                "_snapshot_probe_command",
                return_value=[sys.executable, "-I", "-c", stderr],
            ):
                result = qualify_retained_interruption_replay(stored, **args)
                self.assertEqual(result.qualification_status, "root_verified")
            with (
                patch.object(capacity, "PART_READ_SECONDS", 0.1),
                patch.object(
                    capacity,
                    "_snapshot_probe_command",
                    return_value=[
                        sys.executable,
                        "-I",
                        "-c",
                        "import time; time.sleep(10)",
                    ],
                ),
            ):
                with self.assertRaisesRegex(
                    ReviewInputError, "unavailable or timed out"
                ):
                    qualify_retained_interruption_replay(stored, **args)

    def test_exact_boundary_sizes_crossed_with_interruption_and_fresh_replay(
        self,
    ) -> None:
        """Verify exact N-1 / N / N+1 boundaries across 8192, 10240, 32768, 65536, 60/64, and 512 with interruption & replay."""
        record = SessionRecord.create(IDENTITY)
        raw_rec_bytes = len(canonical_bytes(record.to_dict()))
        raw_com_bytes = len(
            render_session_comment(
                repository_id=123, pull_request=7, record=record
            ).encode("utf-8")
        )

        # 1. 8,191 / 8,192 / 8,193 terminal action manifest bytes:
        for carrier_b, expected_ok in ((8_191, True), (8_192, True), (8_193, False)):
            res = qualify_retained_interruption_replay(
                record,
                identity=IDENTITY,
                interruption_stage="terminal_accounting_close",
                interruption_mode="in_flight_stored_response_lost",
                terminal_carrier_bytes=carrier_b,
            )
            self.assertEqual(
                res.envelope_evaluation.combined_envelope_fits, expected_ok
            )
            self.assertFalse(res.fresh_ledger_reloaded)
            self.assertEqual(
                res.qualification_status,
                "retained_artifacts_required" if expected_ok else "capacity_refused",
            )

        # 2. 10,239 / 10,240 / 10,241 inline disposition bytes:
        for disp_b, expected_ok in ((10_239, True), (10_240, True), (10_241, False)):
            ev = evaluate_combined_capacity(
                CombinedComponentFootprint(dispositions_bytes=disp_b)
            )
            self.assertEqual(ev.combined_envelope_fits, expected_ok)

        # 3. 32,767 / 32,768 / 32,769 SessionRecord canonical bytes:
        for target_rec_b, expected_ok in (
            (32_767, True),
            (32_768, True),
            (32_769, False),
        ):
            res = qualify_retained_interruption_replay(
                record,
                identity=IDENTITY,
                interruption_stage="retained_result_staging",
                interruption_mode="before_dispatch",
                future_root_reserve_bytes=(target_rec_b - raw_rec_bytes),
            )
            self.assertEqual(
                res.envelope_evaluation.projected_record_bytes, target_rec_b
            )
            self.assertEqual(
                res.envelope_evaluation.combined_envelope_fits, expected_ok
            )

        # Intermediate peak > 32,768B refused even when terminal projected_record_bytes <= 32,768B:
        ev_inter_peak = evaluate_combined_capacity(
            CombinedComponentFootprint(
                base_shell_bytes=4_000,
                intermediate_peak_record_bytes=32_769,
            )
        )
        self.assertFalse(ev_inter_peak.combined_envelope_fits)
        self.assertEqual(
            ev_inter_peak.refusal_code, "combined_session_record_bytes_exceeded"
        )

        # 4. 65,535 / 65,536 / 65,537 session comment bytes (via grants + reserve) and review body bytes:
        grants_350 = tuple(f"{i:064x}" for i in range(350))
        com_350 = len(
            render_session_comment(
                repository_id=123,
                pull_request=7,
                record=record,
                consumed_grants=grants_350,
            ).encode("utf-8")
        )
        for target_com_b, expected_ok in (
            (65_535, True),
            (65_536, True),
            (65_537, False),
        ):
            reserve_needed = target_com_b - com_350
            self.assertLessEqual(
                raw_rec_bytes + reserve_needed, MAX_SESSION_RECORD_BYTES
            )
            ev_c = evaluate_live_session_envelope(
                record,
                repository_id=123,
                pull_request=7,
                consumed_grants=grants_350,
                future_root_reserve_bytes=reserve_needed,
            )
            self.assertEqual(ev_c.projected_comment_bytes, target_com_b)
            self.assertEqual(ev_c.combined_envelope_fits, expected_ok)

        for target_rev_b, expected_ok in (
            (65_535, True),
            (65_536, True),
            (65_537, False),
        ):
            ev_r = evaluate_combined_capacity(
                CombinedComponentFootprint(
                    published_review_summary_utf8_bytes=(target_rev_b - 1_024),
                    published_marker_overhead_bytes=1_024,
                )
            )
            self.assertEqual(ev_r.projected_review_body_bytes, target_rev_b)
            self.assertEqual(ev_r.combined_envelope_fits, expected_ok)

        # 5. 511 / 512 / 513 physical API requests crossed with in_flight interruption charge (+1 unknown):
        #    Note: in_flight_not_stored adds +1 unknown request, terminal_close=1, protected_tail=4 -> 6 fixed!
        for committed, expected_total, expected_ok in (
            (505, 511, True),
            (506, 512, True),
            (507, 513, False),
        ):
            res_api = qualify_retained_interruption_replay(
                record,
                identity=IDENTITY,
                interruption_stage="check_publication",
                interruption_mode="in_flight_not_stored",
                committed_requests=committed,
            )
            self.assertEqual(
                res_api.envelope_evaluation.total_projected_requests, expected_total
            )
            self.assertEqual(
                res_api.envelope_evaluation.combined_envelope_fits, expected_ok
            )

        # Definite rejection on check_publication vs other stages + BoundaryInterruptionResult invariants:
        def_rej_check = qualify_retained_interruption_replay(
            record,
            identity=IDENTITY,
            interruption_stage="check_publication",
            interruption_mode="definite_rejection",
        )
        self.assertFalse(def_rej_check.fresh_handoff_verified)
        def_rej_other = qualify_retained_interruption_replay(
            record,
            identity=IDENTITY,
            interruption_stage="native_approval",
            interruption_mode="definite_rejection",
        )
        self.assertEqual(
            def_rej_other.recovery.action, "rescan_under_pinned_provenance"
        )

        with self.assertRaises(ReviewInputError):
            qualify_retained_interruption_replay(
                record,
                identity=IDENTITY,
                interruption_stage="bad_stage",
                interruption_mode="before_dispatch",
            )
        with self.assertRaises(ReviewInputError):
            qualify_retained_interruption_replay(
                record,
                identity=IDENTITY,
                interruption_stage="review_publication",
                interruption_mode="bad_mode",
            )
        with self.assertRaises(ReviewInputError):
            BoundaryInterruptionResult(
                interruption_stage="review_publication",
                interruption_mode="before_dispatch",
                footprint=CombinedComponentFootprint(),
                envelope_evaluation=evaluate_combined_capacity(
                    CombinedComponentFootprint()
                ),
                fresh_ledger_reloaded=True,
                fresh_handoff_verified=True,
                retained_snapshot_preserved=False,
                retained_unknown_charges=0,
                protected_fence_tail_preserved=False,
                duplicate_side_effects=0,
                fresh_inference_calls=0,
                recovery=prescribe_recovery("supported_read_only_observation"),
            )
        self.assertGreater(raw_com_bytes, raw_rec_bytes)


if __name__ == "__main__":
    unittest.main()
