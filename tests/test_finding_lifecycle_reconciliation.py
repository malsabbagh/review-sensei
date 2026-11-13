"""Fresh native evidence is mandatory for public durable reconciliation."""

import tempfile
import unittest
from pathlib import Path

from review_sensei_github import ReviewPublisher
from review_sensei_github.finding_lifecycle import restore_finding_lifecycle
from review_sensei_github.finding_lifecycle_reconciliation import (
    reconcile_finding_lifecycle,
)
from review_sensei_github.publication import NativeFindingReceipt

from review_sensei.errors import ReviewInputError
from review_sensei.session import LocalSessionLedger
from tests import test_durable_finding_lifecycle_restart as restart


class FindingLifecycleReconciliationTests(unittest.TestCase):
    def fixture(self, root, mode, action="initial"):
        restart.DurableFindingRestartTests().run_phase(root, mode, action)
        service = restart.PersistentNativeService(root / "service.json", "1" * 40)
        ledger = LocalSessionLedger(
            root / "ledger", enable_finding_lifecycle_writes=True
        )
        reviewer = ReviewPublisher(http=service.http)
        restore_finding_lifecycle(reviewer, ledger, restart.IDENTITY)
        return service, ledger, reviewer

    def call(self, reviewer, ledger, mode, receipt, **changes):
        args = dict(
            mode=mode,
            target=receipt.target,
            resolved_state="active",
            proof_sha256=receipt.proof_sha256,
            token="fixture-token",
            app_slug="reviewsensei[bot]",
        )
        args.update(changes)
        return reconcile_finding_lifecycle(reviewer, ledger, restart.IDENTITY, **args)

    def test_cold_exact_native_receipt_replay_preserves_all_obligations(self):
        for mode in ("window", "drift"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                service, ledger, reviewer = self.fixture(Path(directory), mode)
                raw = reviewer.native_finding_receipts(repository_id=1, pull_request=1)[
                    0
                ]
                receipt = NativeFindingReceipt(
                    **{k: v for k, v in raw.items() if k != "schema_version"}
                )
                before = ledger.load(restart.IDENTITY).record
                state = self.call(reviewer, ledger, mode, receipt)
                self.assertIsNotNone(state)
                after = ledger.load(restart.IDENTITY).record
                self.assertEqual(
                    before.completed_initial_reviews, after.completed_initial_reviews
                )
                self.assertEqual(len(service.reviews), 1)
                fresh = ReviewPublisher(http=service.http)
                restore_finding_lifecycle(fresh, ledger, restart.IDENTITY)
                states = (
                    fresh._active_window_states
                    if mode == "window"
                    else fresh._drift_states
                )
                actual = states[(1, 1)]
                self.assertEqual(
                    len(
                        actual.authoritative_decision_inventory.obligations
                        if mode == "window"
                        else actual.inventory.obligations
                    ),
                    2,
                )

    def test_ordinary_response_loss_replay_promotes_exact_uncertain_native_slot(self):
        from review_sensei_github.finding_lifecycle import restore_finding_lifecycle

        for mode, loss in (
            (mode, loss) for mode in ("window", "drift") for loss in ("crash", "lost")
        ):
            with (
                self.subTest(mode=mode, loss=loss),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                runner = restart.DurableFindingRestartTests()
                runner.run_phase(
                    root, mode, loss, expected_exit=73 if loss == "crash" else 0
                )
                replay = runner.run_phase(root, mode, "lost_replay")
                self.assertIsNone(replay["error"])
                self.assertEqual(len(replay["reviews"]), 1)
                self.assertEqual(len(replay["state"]["obligations"]), 2)
                first = next(
                    item
                    for item in replay["state"]["obligations"]
                    if item["target"] is not None
                )
                hidden = next(
                    item
                    for item in replay["state"]["obligations"]
                    if item["target"] is None
                )
                self.assertEqual(first["slot_state"], "active")
                self.assertFalse(first["target"].startswith("intent:"))
                self.assertIsNone(hidden["target"])
                self.assertFalse(
                    any(
                        m == "POST" and p.endswith("/reviews")
                        for m, p, _ in replay["calls"]
                    )
                )
                service = restart.PersistentNativeService(
                    root / "service.json", "1" * 40
                )
                ledger = LocalSessionLedger(
                    root / "ledger", enable_finding_lifecycle_writes=True
                )
                cold = ReviewPublisher(http=service.http)
                restore_finding_lifecycle(cold, ledger, restart.IDENTITY)
                receipts = cold.native_finding_receipts(repository_id=1, pull_request=1)
                self.assertEqual(len(receipts), 1)
                self.assertEqual(receipts[0]["thread_id"], first["target"])
                repeated = runner.run_phase(root, mode, "lost_replay")
                self.assertEqual(
                    repeated["state"]["obligations"], replay["state"]["obligations"]
                )
                self.assertEqual(len(repeated["reviews"]), 1)

    def test_ordinary_crash_replay_does_not_adopt_foreign_or_edited_native_thread(self):
        for mode in ("window", "drift"):
            for mutation in ("body", "producer", "review", "head"):
                with (
                    self.subTest(mode=mode, mutation=mutation),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    root = Path(directory)
                    runner = restart.DurableFindingRestartTests()
                    runner.run_phase(root, mode, "crash", expected_exit=73)
                    service = restart.PersistentNativeService(
                        root / "service.json", "1" * 40
                    )
                    comment = service.threads[0]["comments"]["nodes"][0]
                    if mutation == "body":
                        comment["body"] += " edited"
                    elif mutation == "producer":
                        comment["author"]["databaseId"] = 999
                    elif mutation == "review":
                        comment["pullRequestReview"] = {"databaseId": 999}
                    else:
                        comment["commit"] = {"oid": "f" * 40}
                    service.save()
                    replay = runner.run_phase(root, mode, "lost_replay")
                    self.assertIsNotNone(replay["error"])
                    self.assertEqual(len(replay["reviews"]), 1)
                    self.assertFalse(
                        any(
                            item["slot_state"] == "active"
                            for item in replay["state"]["obligations"]
                        )
                    )
                    self.assertFalse(
                        any(
                            m == "POST" and p.endswith("/reviews")
                            for m, p, _ in replay["calls"]
                        )
                    )

    def test_real_drift_move_history_selects_current_native_receipt(self):
        from review_sensei_github import (
            ConversationPublisher,
            GitHubApplication,
            LearningPRPublisher,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = restart.DurableFindingRestartTests()
            for action in ("initial", "rename", "move"):
                outcome = runner.run_phase(root, "drift", action)
                self.assertIsNone(outcome["error"])
            service = restart.PersistentNativeService(root / "service.json", "3" * 40)
            ledger = LocalSessionLedger(
                root / "ledger", enable_finding_lifecycle_writes=True
            )
            reviewer = ReviewPublisher(http=service.http)
            restore_finding_lifecycle(reviewer, ledger, restart.IDENTITY)
            records = reviewer.native_finding_receipts(repository_id=1, pull_request=1)
            target = service.threads[0]["id"]
            aliases = [r for r in records if r["thread_id"] == target]
            self.assertGreater(len(aliases), 1)
            import hashlib

            body_hash = hashlib.sha256(
                service.threads[0]["comments"]["nodes"][0]["body"].encode()
            ).hexdigest()
            raw = next(r for r in aliases if r["body_sha256"] == body_hash)
            receipt = NativeFindingReceipt(
                **{k: v for k, v in raw.items() if k != "schema_version"}
            )
            cold = ReviewPublisher(http=service.http)
            app = GitHubApplication(
                http=service.http,
                broker=restart._WindowFakeBroker(),
                reviewer=cold,
                learner=LearningPRPublisher(http=service.http),
                replier=ConversationPublisher(http=service.http),
                session_ledger=ledger,
            )
            state = app.reconcile_drift_thread(
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                thread_id=target,
                resolved_state="active",
                proof_sha256=receipt.proof_sha256,
                token="fixture-token",
                app_slug="reviewsensei[bot]",
            )
            self.assertEqual(len(state.inventory.obligations), 2)
            fresh = ReviewPublisher(http=service.http)
            restore_finding_lifecycle(fresh, ledger, restart.IDENTITY)
            self.assertEqual(fresh._drift_states[(1, 1)], state)

    def test_pending_create_without_receipt_requires_original_publication_replay(self):
        for mode in ("window", "drift"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                restart.DurableFindingRestartTests().run_phase(
                    root, mode, "crash", expected_exit=73
                )
                service = restart.PersistentNativeService(
                    root / "service.json", "1" * 40
                )
                ledger = LocalSessionLedger(
                    root / "ledger", enable_finding_lifecycle_writes=True
                )
                reviewer = ReviewPublisher(http=service.http)
                before = ledger.load(restart.IDENTITY).record.record_sha256
                with self.assertRaisesRegex(
                    ReviewInputError, "original publication replay required"
                ):
                    reconcile_finding_lifecycle(
                        reviewer,
                        ledger,
                        restart.IDENTITY,
                        mode=mode,
                        target="unconfirmed-target",
                        resolved_state="active",
                        proof_sha256="f" * 64,
                        token="fixture-token",
                        app_slug="reviewsensei[bot]",
                    )
                self.assertEqual(
                    ledger.load(restart.IDENTITY).record.record_sha256, before
                )
                self.assertEqual(len(service.reviews), 1)

    def test_arbitrary_proof_edited_native_and_host_resolution_refuse(self):
        for mode in ("window", "drift"):
            for mutation in ("proof", "body", "producer", "review", "head", "released"):
                with (
                    self.subTest(mode=mode, mutation=mutation),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    service, ledger, reviewer = self.fixture(Path(directory), mode)
                    raw = reviewer.native_finding_receipts(
                        repository_id=1, pull_request=1
                    )[0]
                    receipt = NativeFindingReceipt(
                        **{k: v for k, v in raw.items() if k != "schema_version"}
                    )
                    before = ledger.load(restart.IDENTITY).record.record_sha256
                    root = service.threads[0]["comments"]["nodes"][0]
                    changes = {}
                    if mutation == "proof":
                        changes["proof_sha256"] = "f" * 64
                    if mutation == "body":
                        root["body"] += " edited"
                    if mutation == "producer":
                        root["author"]["databaseId"] = 999
                    if mutation == "review":
                        root["pullRequestReview"] = {"databaseId": 999}
                    if mutation == "head":
                        root["commit"] = {"oid": "f" * 40}
                    if mutation == "released":
                        service.threads[0]["isResolved"] = True
                        changes["resolved_state"] = "released"
                    with self.assertRaises(ReviewInputError):
                        self.call(reviewer, ledger, mode, receipt, **changes)
                    self.assertEqual(
                        ledger.load(restart.IDENTITY).record.record_sha256, before
                    )
                    self.assertEqual(len(service.reviews), 1)


class CurrentRunPartitionPreflightTests(unittest.TestCase):
    def test_current_run_without_issued_complete_frontier_cannot_mint_capacity(self):
        from review_sensei_github.api_run_requests import ApiRunSessionLedger
        from test_activation_tail import CurrentHostedFixture
        from test_api_work_persistence import IDENTITY

        with tempfile.TemporaryDirectory() as directory:
            fixture = CurrentHostedFixture(directory)
            ledger = ApiRunSessionLedger(fixture.native, fixture.caller, IDENTITY)
            before = fixture.caller.accounting.to_document()
            with self.assertRaisesRegex(
                ReviewInputError, "retained frontier is unsupported"
            ):
                ledger.preflight_finding_lifecycle_retention(
                    IDENTITY, added_parts=1, added_bytes=32768
                )
            self.assertEqual(fixture.caller.accounting.to_document(), before)
