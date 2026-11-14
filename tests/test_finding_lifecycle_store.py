"""Authenticated lifecycle reference, original root retention and bounded transport."""

import tempfile
import unittest
from pathlib import Path

from review_sensei.errors import ReviewInputError
from review_sensei.finding_lifecycle_store import (
    normalize_reference,
    read_finding_lifecycle,
    stage_finding_lifecycle,
)
from review_sensei.models import ReviewTransaction
from review_sensei.session import LocalSessionLedger, SessionIdentity, SessionRecord


class FindingLifecycleStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.identity = SessionIdentity("owner/repo", 7, 11)
        self.ledger = LocalSessionLedger(
            self.root, enable_finding_lifecycle_writes=True
        )
        self.record = self.ledger.initialize(self.identity)
        self.transaction = ReviewTransaction.create(
            repository="owner/repo",
            pull_request=7,
            base_sha="a" * 40,
            head_sha="b" * 40,
            policy_digest="c" * 64,
            configuration_digest="d" * 64,
            evidence_digest="e" * 64,
            reservation_id="1" * 64,
            generation=1,
        )
        self.record = self.ledger.replace(
            self.identity,
            lambda record: record.evolve(
                generation=1,
                reservation_id="1" * 64,
                reserved_slot="initial",
                transaction=self.transaction,
            ),
        )

    def stage(self, ledger=None, record=None, document=None, **kw):
        return stage_finding_lifecycle(
            ledger or self.ledger,
            record or self.record,
            document=document or {"required": ["one", "two"], "uncertain": True},
            operation_id=self.transaction.transaction_id,
            base_sha="a" * 40,
            head_sha="b" * 40,
            policy_digest="c" * 64,
            configuration_digest="d" * 64,
            item_count=2,
            **kw,
        )

    def test_root_capacity_partition_choice_is_shared_by_preflight_and_stage(self):
        import hashlib
        from unittest.mock import patch

        from review_sensei.bounded_evidence import canonical_bytes
        from review_sensei.finding_lifecycle_store import (
            _prepare_lifecycle,
            preflight_finding_lifecycle,
        )

        document = {
            "growth": "".join(
                hashlib.sha256(str(index).encode()).hexdigest() for index in range(80)
            )
        }
        args = dict(
            document=document,
            operation_id=self.transaction.transaction_id,
            base_sha="a" * 40,
            head_sha="b" * 40,
            policy_digest="c" * 64,
            configuration_digest="d" * 64,
            item_count=2,
        )
        # Exercise a root boundary independently of the 8 KiB reference bound.
        # Lowering this test-only limit never increases the production capacity.
        root_limit = len(canonical_bytes(self.record.to_dict())) + 2000
        with patch("review_sensei.session.MAX_SESSION_RECORD_BYTES", root_limit):
            _, _, _, allowance, candidate = _prepare_lifecycle(
                self.ledger, self.record, **args
            )
            self.assertEqual(
                candidate.finding_lifecycle["manifest"]["encoding"],
                "partitioned-json-v1",
            )
            plan = preflight_finding_lifecycle(self.ledger, self.record, **args)
            self.assertGreater(plan["checkpoints"][0]["parts"], 0)
            self.assertLess(allowance, 8192)
            paths_before = set(self.root.rglob("*"))
            with patch.object(
                self.ledger.evidence_budget,
                "preflight",
                side_effect=ReviewInputError("original allowance one short"),
            ):
                with self.assertRaisesRegex(ReviewInputError, "one short"):
                    self.stage(document=document)
            self.assertEqual(set(self.root.rglob("*")), paths_before)
            self.assertEqual(
                self.ledger.load(self.identity).record.record_sha256,
                self.record.record_sha256,
            )
            result = self.stage(document=document)
            self.assertEqual(
                result.finding_lifecycle["manifest"]["encoding"],
                "partitioned-json-v1",
            )
            self.assertEqual(read_finding_lifecycle(self.ledger, result), document)

    def test_cold_read_preserves_complete_bytes_and_current_root_binding(self):
        result = self.stage()
        cold = LocalSessionLedger(self.root, enable_finding_lifecycle_writes=True)
        loaded = cold.load(self.identity)
        self.assertEqual(loaded.status, "ok")
        self.assertEqual(
            read_finding_lifecycle(cold, loaded.record),
            {"required": ["one", "two"], "uncertain": True},
        )
        self.assertEqual(result.finding_lifecycle, loaded.record.finding_lifecycle)
        self.assertEqual(
            result.finding_lifecycle["previous_root_sha256"], self.record.record_sha256
        )
        self.assertEqual(
            result.completed_initial_reviews, self.record.completed_initial_reviews
        )
        self.assertEqual(result.reservation_id, self.record.reservation_id)

    def legacy_root(self, **overrides):
        from review_sensei.bounded_evidence import inline_manifest
        from review_sensei.finding_lifecycle_store import _prepare_lifecycle

        reference, envelope, _, _, candidate = _prepare_lifecycle(
            self.ledger,
            self.record,
            document={"required": ["one", "two"]},
            operation_id=self.transaction.transaction_id,
            base_sha="a" * 40,
            head_sha="b" * 40,
            policy_digest="c" * 64,
            configuration_digest="d" * 64,
            item_count=2,
        )
        envelope.update(reference)
        envelope.update(schema_version="finding-lifecycle-evidence-v1", **overrides)
        manifest = inline_manifest(
            envelope,
            binding=candidate.finding_lifecycle["manifest"]["binding"],
            item_count=2,
        )
        return self.ledger.replace(
            self.identity,
            lambda current: current.evolve(
                generation=current.generation + 1,
                finding_lifecycle={**reference, "manifest": manifest},
            ),
        )

    def test_authenticated_v1_cold_read_and_v2_successor_are_lossless(self):
        legacy = self.legacy_root()
        cold = LocalSessionLedger(self.root, enable_finding_lifecycle_writes=True)
        document = read_finding_lifecycle(cold, legacy)
        self.assertEqual(document, {"required": ["one", "two"]})
        updated = self.stage(cold, legacy, document)
        manifest = updated.finding_lifecycle["manifest"]
        envelope = cold.read_evidence(
            self.identity, manifest, expected_binding=manifest["binding"]
        )
        self.assertEqual(
            envelope,
            {
                "schema_version": "finding-lifecycle-evidence-v2",
                "lifecycle": document,
            },
        )
        self.assertEqual(
            updated.finding_lifecycle["previous_snapshot_sha256"],
            legacy.finding_lifecycle["manifest"]["sha256"],
        )
        self.assertEqual(read_finding_lifecycle(cold, updated), document)

    def test_legacy_duplicate_operation_must_match_authenticated_reference(self):
        legacy = self.legacy_root(operation_id="f" * 64)
        with self.assertRaisesRegex(ReviewInputError, "operation/lineage"):
            read_finding_lifecycle(self.ledger, legacy)

    def test_v2_reference_operation_and_lineage_mutants_refuse_activation(self):
        from review_sensei.finding_lifecycle_store import _prepare_lifecycle

        result = self.stage()
        _, _, _, _, candidate = _prepare_lifecycle(
            self.ledger,
            result,
            document={"required": ["one", "two"]},
            operation_id=self.transaction.transaction_id,
            base_sha="a" * 40,
            head_sha="b" * 40,
            policy_digest="c" * 64,
            configuration_digest="d" * 64,
            item_count=2,
        )
        for key, error in (
            ("operation_id", "accepted operation"),
            ("previous_root_sha256", "root predecessor"),
            ("previous_snapshot_sha256", "lineage"),
        ):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ReviewInputError, error):
                    self.ledger.replace(
                        self.identity,
                        lambda current: current.evolve(
                            generation=current.generation + 1,
                            finding_lifecycle={
                                **candidate.finding_lifecycle,
                                key: "f" * 64,
                            },
                        ),
                    )
                self.assertEqual(
                    self.ledger.load(self.identity).record.record_sha256,
                    result.record_sha256,
                )

    def test_internal_selected_load_reuses_read_but_keeps_final_fresh_fence(self):
        from unittest.mock import patch

        result = self.stage()
        loaded = self.ledger.load(self.identity)
        with patch.object(self.ledger, "load", wraps=self.ledger.load) as load:
            self.assertEqual(
                read_finding_lifecycle(self.ledger, result, _selected_load=loaded),
                {"required": ["one", "two"], "uncertain": True},
            )
            self.assertEqual(load.call_count, 1)
        self.stage(record=result, document={"required": ["two"]})
        with self.assertRaisesRegex(ReviewInputError, "current session"):
            read_finding_lifecycle(self.ledger, result, _selected_load=loaded)

    def test_retained_across_terminal_and_unrelated_transitions(self):
        result = self.stage()
        changed = result.evolve(
            transaction=None,
            reservation_id=None,
            reserved_slot=None,
            completed_initial_reviews=1,
            generation=result.generation + 1,
        )
        self.assertEqual(changed.finding_lifecycle, result.finding_lifecycle)
        with self.assertRaises(ReviewInputError):
            result.evolve(finding_lifecycle=None)

    def test_old_reader_cannot_drop_unknown_reference_and_reuse_digest(self):
        result = self.stage()
        old = result.to_dict()
        old.pop("finding_lifecycle")
        with self.assertRaisesRegex(ReviewInputError, "integrity"):
            SessionRecord.from_dict(old)

    def test_unknown_reference_version_unknown_field_and_null_rejected(self):
        result = self.stage()
        for key, value in (
            ("schema_version", "finding-lifecycle-reference-v2"),
            ("extra", "hidden"),
        ):
            changed = {**result.finding_lifecycle, key: value}
            with self.assertRaises(ReviewInputError):
                normalize_reference(changed)
        raw = result.to_dict()
        raw["finding_lifecycle"] = None
        with self.assertRaises(ReviewInputError):
            SessionRecord.from_dict(raw)
        raw = result.to_dict()
        raw["schema_version"] = "unsupported"
        with self.assertRaises(ReviewInputError):
            SessionRecord.from_dict(raw)

    def test_stale_root_cannot_read_or_write(self):
        result = self.stage()
        cold = LocalSessionLedger(self.root, enable_finding_lifecycle_writes=True)
        with self.assertRaisesRegex(ReviewInputError, "current session"):
            read_finding_lifecycle(cold, self.record)
        with self.assertRaisesRegex(
            ReviewInputError, "generation conflict before activation"
        ):
            self.stage(ledger=cold, record=self.record)
        self.assertEqual(
            cold.load(self.identity).record.record_sha256, result.record_sha256
        )

    def test_mutated_missing_and_foreign_parts_fail_closed(self):
        import random

        randomizer = random.Random(9)
        value = "".join(randomizer.choices("0123456789abcdef", k=20000))
        result = self.stage(document={"required": value})
        reference = result.finding_lifecycle["manifest"]["parts"][0]
        path = next(self.root.rglob(reference["storage_id"]))
        path.write_text("{}")
        cold = LocalSessionLedger(self.root, enable_finding_lifecycle_writes=True)
        with self.assertRaises(ReviewInputError):
            read_finding_lifecycle(cold, result)
        path.unlink()
        with self.assertRaises(ReviewInputError):
            read_finding_lifecycle(cold, result)

    def test_tail_capacity_refuses_before_part_write(self):
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))
        with self.assertRaises(ReviewInputError):
            self.stage(reserved_tail_calls=64)
        after = sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))
        self.assertEqual(before, after)
        self.assertEqual(
            self.ledger.load(self.identity).record.record_sha256,
            self.record.record_sha256,
        )

    def test_new_snapshot_chains_without_recursive_prior_document(self):
        first = self.stage()
        cold = LocalSessionLedger(self.root, enable_finding_lifecycle_writes=True)
        second = self.stage(cold, first, {"required": ["two"], "uncertain": False})
        self.assertEqual(
            second.finding_lifecycle["previous_snapshot_sha256"],
            first.finding_lifecycle["manifest"]["sha256"],
        )
        fresh = LocalSessionLedger(self.root, enable_finding_lifecycle_writes=True)
        self.assertEqual(
            read_finding_lifecycle(fresh, second),
            {"required": ["two"], "uncertain": False},
        )

    def test_producer_is_observed_independently_not_trusted_from_payload(self):
        from unittest.mock import patch

        result = self.stage()
        cold = LocalSessionLedger(self.root, enable_finding_lifecycle_writes=True)
        with patch.object(cold, "evidence_producer", return_value="github-bot:99"):
            with self.assertRaisesRegex(ReviewInputError, "selected scope"):
                read_finding_lifecycle(cold, result)

    def test_operation_cannot_choose_an_unadmitted_transaction(self):
        from dataclasses import replace

        wrong = replace(
            self.transaction,
            head_sha="e" * 40,
            transaction_id=ReviewTransaction.derive_id(
                repository="owner/repo",
                pull_request=7,
                base_sha="a" * 40,
                head_sha="e" * 40,
                policy_digest="c" * 64,
                configuration_digest="d" * 64,
                evidence_digest="e" * 64,
                reservation_id="1" * 64,
            ),
        )
        self.transaction = wrong
        with self.assertRaisesRegex(ReviewInputError, "accepted operation"):
            self.stage()
        self.assertIsNone(self.ledger.load(self.identity).record.finding_lifecycle)

    def test_cannot_start_lifecycle_without_accepted_operation(self):
        record = self.record.evolve(transaction=None)
        with self.assertRaisesRegex(ReviewInputError, "accepted .*operation"):
            self.stage(record=record)

    def test_inline_local_exact_four_dispatches_keep_four_protected_calls(self):
        budget = self.ledger.evidence_budget
        while budget.calls < 56:
            budget.consume()
        result = self.stage()
        # Fresh prewrite read, durable write, activation, and final readback.
        self.assertEqual(budget.calls, 60)
        self.assertEqual(budget.ordinary_remaining(), 0)
        self.assertIsNotNone(result.finding_lifecycle)
        for _ in range(4):
            budget.consume(fence=True)
        self.assertEqual(budget.calls, 64)
        with self.assertRaises(ReviewInputError):
            budget.consume(fence=True)

    def test_inline_local_one_call_short_refuses_before_root_write(self):
        budget = self.ledger.evidence_budget
        path = self.ledger._path(self.identity)
        before = path.read_bytes()
        while budget.calls < 57:
            budget.consume()
        with self.assertRaises(ReviewInputError):
            self.stage()
        self.assertEqual(budget.calls, 57)  # pure preflight refuses before any I/O
        self.assertEqual(path.read_bytes(), before)

    def test_inline_local_preserves_explicit_five_call_terminal_tail(self):
        budget = self.ledger.evidence_budget
        while budget.calls < 51:
            budget.consume()
        self.stage(reserved_tail_calls=5)
        self.assertEqual(budget.calls, 55)
        for _ in range(5):
            budget.consume()
        self.assertEqual(budget.calls, 60)

    def test_selected_inline_read_performs_exactly_one_fresh_root_io(self):
        from review_sensei.finding_lifecycle_store import select_finding_lifecycle

        self.stage()
        before = self.ledger.evidence_budget.calls
        selected, value = select_finding_lifecycle(self.ledger, self.identity)
        self.assertEqual(self.ledger.evidence_budget.calls - before, 1)
        self.assertEqual(selected.status, "ok")
        self.assertEqual(value["required"], ["one", "two"])

    def test_selected_inline_commit_cannot_change_unrelated_authority(self):
        from review_sensei.finding_lifecycle_store import _prepare_lifecycle

        *_, candidate = _prepare_lifecycle(
            self.ledger,
            self.record,
            document={"required": ["one"]},
            operation_id=self.transaction.transaction_id,
            base_sha="a" * 40,
            head_sha="b" * 40,
            policy_digest="c" * 64,
            configuration_digest="d" * 64,
            item_count=1,
        )
        before = self.ledger._path(self.identity).read_bytes()
        with self.assertRaisesRegex(ReviewInputError, "unrelated authority"):
            self.ledger.replace_finding_lifecycle_inline(
                self.identity,
                lambda _: candidate.evolve(operator_paused=True),
                record=self.record,
            )
        self.assertEqual(self.ledger._path(self.identity).read_bytes(), before)

    def test_local_subclass_does_not_inherit_exact_inline_tail_qualification(self):
        class DifferentLocalLedger(LocalSessionLedger):
            pass

        ledger = DifferentLocalLedger(self.root, enable_finding_lifecycle_writes=True)
        while ledger.evidence_budget.calls < 55:
            ledger.evidence_budget.consume()
        before = self.ledger._path(self.identity).read_bytes()
        with self.assertRaises(ReviewInputError):
            self.stage(ledger=ledger)
        self.assertEqual(self.ledger._path(self.identity).read_bytes(), before)

    def preflight(self, **kw):
        from review_sensei.finding_lifecycle_store import preflight_finding_lifecycle

        return preflight_finding_lifecycle(
            self.ledger,
            self.record,
            document={"required": ["one", "two"], "uncertain": True},
            operation_id=self.transaction.transaction_id,
            base_sha="a" * 40,
            head_sha="b" * 40,
            policy_digest="c" * 64,
            configuration_digest="d" * 64,
            item_count=2,
            **kw,
        )

    def test_inline_whole_pending_future_preflight_has_no_io(self):
        before = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        calls = self.ledger.evidence_budget.calls
        future = {
            "required": ["one", "two"],
            "uncertain": False,
            "native_comment_id": 9999999999999999999,
            "native_thread_id": "X" * 256,
        }
        plan = self.preflight(future_documents=(future,), reserved_tail_calls=5)
        self.assertEqual(plan["conditional_calls"], 8)
        self.assertEqual(plan["added_parts"], 0)
        self.assertEqual(plan["added_bytes"], 0)
        self.assertTrue(all(row["inline"] for row in plan["checkpoints"]))
        self.assertEqual(self.ledger.evidence_budget.calls, calls)
        self.assertEqual(
            before,
            {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()},
        )

    def test_whole_pending_future_tail_boundary(self):
        budget = self.ledger.evidence_budget
        while budget.calls < 47:
            budget.consume()
        self.preflight(future_documents=({"receipt": "x"},), reserved_tail_calls=5)
        self.assertEqual(budget.calls, 47)
        budget.consume()
        with self.assertRaises(ReviewInputError):
            self.preflight(future_documents=({"receipt": "x"},), reserved_tail_calls=5)
        self.assertEqual(budget.calls, 48)

    def test_partition_preflight_is_read_only_and_does_not_create_directory(self):
        future = {"required": "a" * 10000}
        before = sorted(str(p) for p in self.root.rglob("*"))
        calls = self.ledger.evidence_budget.calls
        plan = self.preflight(future_documents=(future,))
        self.assertGreater(plan["added_parts"], 0)
        self.assertGreater(plan["added_bytes"], 10000)
        self.assertEqual(self.ledger.evidence_budget.calls, calls + 2)
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob("*")))

    def test_future_growth_refuses_before_any_storage_io(self):
        calls = self.ledger.evidence_budget.calls
        before = sorted(str(p) for p in self.root.rglob("*"))
        with self.assertRaises(ReviewInputError):
            self.preflight(future_documents=({"required": "x" * 2100000},))
        self.assertEqual(self.ledger.evidence_budget.calls, calls)
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob("*")))

    def test_partition_future_bytes_include_existing_retention(self):
        from review_sensei.bounded_evidence import MAX_STORED_PART_BYTES

        directory = self.ledger._part_directory(self.identity)
        directory.mkdir(parents=True)
        sentinel = directory / ("f" * 64)
        with sentinel.open("wb") as stream:
            stream.truncate(MAX_STORED_PART_BYTES)
        calls = self.ledger.evidence_budget.calls
        with self.assertRaisesRegex(ReviewInputError, "whole future retention"):
            self.preflight(future_documents=({"required": "a" * 10000},))
        self.assertEqual(self.ledger.evidence_budget.calls, calls + 3)
        self.assertEqual(sentinel.stat().st_size, MAX_STORED_PART_BYTES)
        self.assertEqual(list(directory.iterdir()), [sentinel])

    def test_ordinary_head_bound_reserved_round_persists_without_transaction(self):
        from review_sensei.convergence import ReviewConvergencePolicy
        from review_sensei.session import prepare_session_round, session_reservation_id

        ledger = LocalSessionLedger(
            self.root / "legacy", enable_finding_lifecycle_writes=True
        )
        reservation = session_reservation_id(
            repository="owner/repo", pull_request=7, head_sha="b" * 40, kind="publish"
        )
        prepared = prepare_session_round(
            ledger,
            self.identity,
            ReviewConvergencePolicy(),
            reservation_id=reservation,
            head_sha="b" * 40,
        )
        original = prepared.record
        self.assertIsNone(original.transaction)
        updated = stage_finding_lifecycle(
            ledger,
            original,
            document={"required": ["one"]},
            operation_id=reservation,
            base_sha="a" * 40,
            head_sha="b" * 40,
            policy_digest="c" * 64,
            configuration_digest="d" * 64,
            item_count=1,
        )
        self.assertEqual(updated.finding_lifecycle["operation_id"], reservation)
        self.assertEqual(updated.reservation_id, reservation)
        self.assertEqual(updated.failed_attempts_head_sha, "b" * 40)
        self.assertEqual(
            updated.completed_initial_reviews, original.completed_initial_reviews
        )
        self.assertIsNone(updated.transaction)

    def test_stale_failed_attempt_head_is_not_reserved_operation_authority(self):
        from review_sensei.convergence import ReviewConvergencePolicy
        from review_sensei.session import prepare_session_round

        ledger = LocalSessionLedger(
            self.root / "legacy-opaque", enable_finding_lifecycle_writes=True
        )
        ledger.initialize(self.identity)
        ledger.replace(
            self.identity,
            lambda record: record.evolve(failed_attempts_head_sha="b" * 40),
        )
        prepared = prepare_session_round(
            ledger,
            self.identity,
            ReviewConvergencePolicy(),
            reservation_id="f" * 64,
            head_sha=None,
        )
        self.assertEqual(prepared.record.failed_attempts_head_sha, "b" * 40)
        with self.assertRaisesRegex(
            ReviewInputError, "accepted reserved operation conflicts"
        ):
            stage_finding_lifecycle(
                ledger,
                prepared.record,
                document={"required": ["one"]},
                operation_id="f" * 64,
                base_sha="a" * 40,
                head_sha="b" * 40,
                policy_digest="c" * 64,
                configuration_digest="d" * 64,
                item_count=1,
            )
        self.assertIsNone(ledger.load(self.identity).record.finding_lifecycle)
