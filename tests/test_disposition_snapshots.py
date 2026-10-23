"""Bounded decision snapshots retain authority across fresh ledger readers."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from review_sensei.bounded_evidence import EvidenceReadBudget, inline_manifest
from review_sensei.errors import ReviewInputError
from review_sensei.session import LocalSessionLedger, SessionIdentity, SessionRecord

IDENTITY = SessionIdentity("owner/repo", 1, 1)
HEAD, BASE, POLICY, RESULT = "b" * 40, "a" * 40, "c" * 64, "d" * 64


def snapshot():
    binding = {
        "repository": IDENTITY.repository,
        "repository_id": IDENTITY.repository_id,
        "pull_request": IDENTITY.pull_request,
        "base_sha": BASE,
        "head_sha": HEAD,
        "policy_digest": POLICY,
        "configuration_digest": "e" * 64,
        "generation": 1,
        "producer": "local-ledger",
        "purpose": "dispositions",
        "schema_version": "1.0",
    }
    decision = {
        "fingerprint": "f" * 64,
        "action": "accept-risk",
        "reason": "Synthetic independently assessed risk.",
        "actor": "alice",
        "head_sha": HEAD,
        "expires_at": None,
    }
    document = {
        "schema_version": "1.0",
        "result_digest": RESULT,
        "dispositions": [decision],
    }
    manifest = inline_manifest(document, binding=binding, item_count=1)
    assert manifest is not None
    return {
        "schema_version": "1.0",
        "result_digest": RESULT,
        "state_manifest": manifest,
    }


class DispositionSnapshotReaderTests(unittest.TestCase):
    def test_audited_25_decisions_finish_bounded_threads_and_one_native_approval(self):
        from review_sensei.disposition import (
            FindingIdentity,
            apply_session_command,
            parse_maintainer_command,
            session_dispositions,
        )
        from review_sensei.models import ReviewTransaction
        from tests.test_command_capacity_recovery import CommandCapacityState

        state = CommandCapacityState(
            complete=True, human_count=25, inline_range=(0, 25), padded=False
        )
        state.resolved_threads.clear()
        inventory = tuple(
            FindingIdentity(
                entry.instance,
                entry.lifecycle,
                overridable=entry.overridable,
                needs_human=entry.needs_human,
            )
            for entry in state.entries
        )
        ids = [state.labels[e.instance] for e in state.entries if e.needs_human]
        transaction = ReviewTransaction.create(
            repository=IDENTITY.repository,
            pull_request=1,
            base_sha=BASE,
            head_sha=HEAD,
            policy_digest=POLICY,
            configuration_digest="e" * 64,
            evidence_digest="a" * 64,
            reservation_id="f" * 64,
            generation=1,
        ).with_result(state.result.content_digest())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            seed = LocalSessionLedger(path)
            seed.initialize(IDENTITY)
            seed.replace(
                IDENTITY,
                lambda record: record.evolve(
                    generation=1,
                    transaction=transaction,
                    last_committed_reservation_id=transaction.reservation_id,
                ),
            )
            for batch, start in enumerate(range(0, 25, 10)):
                ledger = LocalSessionLedger(
                    path,
                    evidence_budget=EvidenceReadBudget(),
                    enable_disposition_snapshot_writes=True,
                )
                _, outcome = apply_session_command(
                    ledger,
                    IDENTITY,
                    parse_maintainer_command(
                        "@reviewsensei override "
                        + " ".join(ids[start : start + 10])
                        + " acceptable risk",
                        actor="alice",
                        head_sha=HEAD,
                    ),
                    finding_inventory=inventory,
                    actor_role="admin",
                    policy_digest=POLICY,
                    provenance=f"comment:{batch + 1}",
                )
                self.assertTrue(outcome.applied)
                self.assertLessEqual(ledger.evidence_budget.calls, 60)
            fresh = LocalSessionLedger(path, evidence_budget=EvidenceReadBudget())
            record = fresh.load(IDENTITY).record
            self.assertIsNotNone(record.disposition_snapshot)
            decisions = session_dispositions(record, ledger=fresh)
            self.assertEqual(len(decisions), 25)
            self.assertTrue(all(d.actor_role == "admin" and d.provenance for d in decisions))
            progress = []
            for _ in range(8):
                outcome, budget, _ = state.verify(decisions)
                progress.append(len(state.resolved_threads))
                self.assertLessEqual(budget.calls, 60)
                if outcome.status == "approved":
                    break
            self.assertEqual(outcome.status, "approved", progress)
            self.assertGreater(len(progress), 1, progress)
            self.assertEqual(len(state.resolved_threads), 25)
            self.assertEqual(state.checks[-1]["conclusion"], "success")
            replay, _, _ = state.verify(decisions)
            self.assertEqual(replay.status, "already_approved")
            state.head = "c" * 40
            stale, _, _ = state.verify(decisions)
            self.assertEqual(stale.status, "skipped_stale_head")
        self.assertEqual(sum(r["state"] == "APPROVED" for r in state.reviews), 1)

    def test_large_snapshot_is_one_complete_inventory_and_refuses_corruption(self):
        from review_sensei.disposition import session_dispositions

        for count in (33, 257):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)
                ledger = LocalSessionLedger(
                    path,
                    evidence_budget=EvidenceReadBudget(),
                    enable_disposition_snapshot_writes=True,
                )
                ledger.initialize(IDENTITY)
                root = snapshot()
                binding = root["state_manifest"]["binding"]
                document = root["state_manifest"]["document"]
                seed = document["dispositions"][0]
                document["dispositions"] = [
                    {**seed, "fingerprint": hashlib.sha256(str(i).encode()).hexdigest()}
                    for i in range(count)
                ]

                def activate(current):
                    manifest = ledger.stage_evidence(
                        IDENTITY,
                        binding=binding,
                        document=document,
                        item_count=count,
                        max_manifest_bytes=2048,
                    )
                    return current.evolve(
                        generation=1,
                        disposition_snapshot={**root, "state_manifest": manifest},
                    )

                ledger.replace(IDENTITY, activate)
                fresh = LocalSessionLedger(path, evidence_budget=EvidenceReadBudget())
                record = fresh.load(IDENTITY).record
                self.assertEqual(len(session_dispositions(record, ledger=fresh)), count)
                part = record.disposition_snapshot["state_manifest"]["parts"][0]
                files = list(path.rglob(part["storage_id"]))
                self.assertEqual(len(files), 1)
                content = files[0].read_bytes()
                files[0].write_bytes(
                    content.replace(b"acceptable", b"unaccepted", 1)
                    if b"acceptable" in content
                    else content[:-1] + b"!"
                )
                refused = LocalSessionLedger(
                    path, evidence_budget=EvidenceReadBudget()
                ).load(IDENTITY)
                self.assertEqual(refused.status, "integrity-failed")
                self.assertIsNone(refused.record)

    def test_closed_snapshot_inventory_refuses_duplicate_and_foreign_authority(self):
        from review_sensei.disposition_snapshot import snapshot_document

        root = snapshot()
        document = root["state_manifest"]["document"]
        document["dispositions"] *= 2
        with self.assertRaisesRegex(ReviewInputError, "duplicated"):
            snapshot_document(document, result_digest=RESULT, item_count=2)
        with self.assertRaisesRegex(ReviewInputError, "does not match"):
            snapshot_document(document, result_digest="a" * 64, item_count=2)
        root = snapshot()
        root["state_manifest"]["binding"]["repository_id"] = 2
        with self.assertRaises(ReviewInputError):
            SessionRecord.create(IDENTITY, generation=1, disposition_snapshot=root)

    def test_audited_ten_ten_five_commands_activate_full_snapshot_and_replay_once(self):
        from review_sensei.disposition import (
            FindingIdentity,
            apply_session_command,
            parse_maintainer_command,
            session_dispositions,
        )
        from review_sensei.models import ReviewTransaction
        from review_sensei.presentation import finding_identifier

        inventory = tuple(
            FindingIdentity(
                hashlib.sha256(f"finding:{i}".encode()).hexdigest(), overridable=True
            )
            for i in range(25)
        )
        transaction = ReviewTransaction.create(
            repository=IDENTITY.repository,
            pull_request=1,
            base_sha=BASE,
            head_sha=HEAD,
            policy_digest=POLICY,
            configuration_digest="e" * 64,
            evidence_digest="a" * 64,
            reservation_id="f" * 64,
            generation=1,
        ).with_result(RESULT)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            seed = LocalSessionLedger(path)
            seed.initialize(IDENTITY)
            seed.replace(
                IDENTITY,
                lambda record: record.evolve(
                    generation=1,
                    transaction=transaction,
                    last_committed_reservation_id=transaction.reservation_id,
                ),
            )
            for batch, start in enumerate(range(0, 25, 10)):
                ledger = LocalSessionLedger(
                    path,
                    evidence_budget=EvidenceReadBudget(),
                    enable_disposition_snapshot_writes=True,
                )
                ids = [
                    finding_identifier(item.instance)
                    for item in inventory[start : start + 10]
                ]
                command = parse_maintainer_command(
                    "@reviewsensei override " + " ".join(ids) + " acceptable risk",
                    actor="alice",
                    head_sha=HEAD,
                )
                record, outcome = apply_session_command(
                    ledger,
                    IDENTITY,
                    command,
                    finding_inventory=inventory,
                    actor_role="admin",
                    policy_digest=POLICY,
                    provenance=f"comment:{batch + 1}",
                )
                self.assertTrue(outcome.applied)
                self.assertLessEqual(ledger.evidence_budget.calls, 60)
            fresh = LocalSessionLedger(path, evidence_budget=EvidenceReadBudget())
            loaded = fresh.load(IDENTITY).record
            self.assertIsNotNone(loaded.disposition_snapshot)
            self.assertEqual(loaded.dispositions, ())
            decisions = session_dispositions(loaded, ledger=fresh)
            self.assertEqual(len(decisions), 25)
            self.assertTrue(
                all(
                    item.actor_role == "admin" and item.provenance for item in decisions
                )
            )
            replay, outcome = apply_session_command(
                fresh,
                IDENTITY,
                command,
                finding_inventory=inventory,
                actor_role="admin",
                policy_digest=POLICY,
                provenance="comment:3",
            )
            self.assertEqual(replay.record_sha256, loaded.record_sha256)
            self.assertTrue(
                all(item.outcome == "already-recorded" for item in outcome.receipts)
            )

    def test_partitioned_twenty_five_decisions_survive_restart_and_missing_part_holds(
        self,
    ):
        from review_sensei.disposition import session_dispositions

        root = snapshot()
        binding = root["state_manifest"]["binding"]
        document = root["state_manifest"]["document"]
        seed = document["dispositions"][0]
        document["dispositions"] = [
            {**seed, "fingerprint": hashlib.sha256(str(index).encode()).hexdigest()}
            for index in range(25)
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            ledger = LocalSessionLedger(
                path, evidence_budget=EvidenceReadBudget(), enable_partition_writes=True
            )
            ledger.initialize(IDENTITY)

            def activate(current):
                manifest = ledger.stage_evidence(
                    IDENTITY,
                    binding=binding,
                    document=document,
                    item_count=25,
                    max_manifest_bytes=2048,
                )
                self.assertEqual(manifest["encoding"], "partitioned-json-v1")
                return current.evolve(
                    generation=1,
                    disposition_snapshot={**root, "state_manifest": manifest},
                )

            ledger.replace(IDENTITY, activate)
            fresh = LocalSessionLedger(path, evidence_budget=EvidenceReadBudget())
            loaded = fresh.load(IDENTITY)
            self.assertEqual(loaded.status, "ok")
            self.assertEqual(len(session_dispositions(loaded.record, ledger=fresh)), 25)
            files = list(
                path.rglob(
                    loaded.record.disposition_snapshot["state_manifest"]["parts"][0][
                        "storage_id"
                    ]
                )
            )
            self.assertEqual(len(files), 1)
            files[0].unlink()
            refused = LocalSessionLedger(path).load(IDENTITY)
            self.assertEqual(refused.status, "integrity-failed")
            self.assertIsNone(refused.record)

    def test_fresh_ledger_reads_snapshot_with_writer_off_and_refuses_downgrade(self):
        from review_sensei.disposition import session_dispositions

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = LocalSessionLedger(root)
            ledger.initialize(IDENTITY)
            ledger.replace(
                IDENTITY,
                lambda current: current.evolve(
                    generation=1, disposition_snapshot=snapshot()
                ),
            )
            fresh = LocalSessionLedger(root)
            record = fresh.load(IDENTITY).record
            self.assertIsNotNone(record)
            self.assertEqual(len(session_dispositions(record, ledger=fresh)), 1)
            with self.assertRaises(ReviewInputError):
                session_dispositions(record)
            with self.assertRaises(ReviewInputError):
                fresh.replace(
                    IDENTITY,
                    lambda current: current.evolve(
                        generation=2, disposition_snapshot=None
                    ),
                )
            self.assertEqual(fresh.load(IDENTITY).record, record)

    def test_root_roundtrip_and_counter_mutation_preserve_full_snapshot(self):
        from review_sensei.session import read_session_dispositions

        record = SessionRecord.create(
            IDENTITY, generation=1, disposition_snapshot=snapshot()
        )
        reloaded = SessionRecord.from_dict(record.to_dict())
        advanced = reloaded.evolve(generation=2, completed_verification_rounds=1)
        self.assertEqual(advanced.disposition_snapshot, record.disposition_snapshot)
        self.assertEqual(advanced.dispositions, ())
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(
                Path(directory), evidence_budget=EvidenceReadBudget()
            )
            decisions = read_session_dispositions(ledger, advanced)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(
            decisions[0]["reason"], "Synthetic independently assessed risk."
        )

    def test_dual_authority_and_future_generation_refuse(self):
        root = snapshot()
        with self.assertRaises(ReviewInputError):
            SessionRecord.create(IDENTITY, disposition_snapshot=root)
        with self.assertRaises(ReviewInputError):
            SessionRecord.create(
                IDENTITY,
                generation=1,
                dispositions=root["state_manifest"]["document"]["dispositions"],
                disposition_snapshot=root,
            )

    def test_snapshot_requires_authenticated_resolver_and_matching_producer(self):
        from review_sensei.session import read_session_dispositions

        record = SessionRecord.create(
            IDENTITY, generation=1, disposition_snapshot=snapshot()
        )
        with self.assertRaises(ReviewInputError):
            read_session_dispositions(object(), record)
