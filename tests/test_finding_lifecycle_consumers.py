"""Cold consumer selection uses the ordinary producer's authenticated carrier."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from review_sensei_github import ReviewPublisher
from review_sensei_github.finding_lifecycle import _unpack_journal
from review_sensei_github.finding_lifecycle_consumers import finding_lifecycle_consumer

from review_sensei.errors import ReviewInputError
from review_sensei.finding_lifecycle_store import read_finding_lifecycle
from review_sensei.session import LocalSessionLedger
from tests import test_durable_finding_lifecycle_restart as restart


class ConsumerTests(unittest.TestCase):
    def test_cold_consumer_shares_state_preserves_funds_and_checkpoints_exception(self):
        for mode in ("window", "drift"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                outcome = restart.DurableFindingRestartTests.run_phase(
                    self, root, mode, "initial"
                )
                self.assertIsNone(outcome["error"])
                ledger = LocalSessionLedger(
                    root / "ledger", enable_finding_lifecycle_writes=True
                )
                before = ledger.load(restart.IDENTITY).record
                old_document = _unpack_journal(read_finding_lifecycle(ledger, before))
                head = before.finding_lifecycle["manifest"]["binding"]["head_sha"]
                publisher = ReviewPublisher(
                    http=restart.PersistentNativeService(
                        root / "service.json", head
                    ).http
                )
                separate = ReviewPublisher(http=publisher.http).finalizer
                original_http = separate.http
                native_bytes = (root / "service.json").read_bytes()
                context = finding_lifecycle_consumer(
                    publisher,
                    ledger,
                    restart.IDENTITY,
                    head_sha=head,
                    app_slug="reviewsensei[bot]",
                    finalizers=(separate, separate),
                )

                original_budget = ledger.evidence_budget
                separate.evidence_budget = original_budget
                separate.evidence_budget = object()
                with self.assertRaisesRegex(ReviewInputError, "budgets conflict"):
                    context.run(lambda: self.fail("must not replace caller allowance"))
                separate.evidence_budget = original_budget
                context.restore_only()
                context.bind(separate)
                self.assertIs(separate._finding_lifecycle_consumer, context)
                self.assertIs(separate.http, original_http)
                self.assertEqual(
                    before.record_sha256,
                    ledger.load(restart.IDENTITY).record.record_sha256,
                )

                def callback():
                    self.assertTrue(context.enrolled)
                    self.assertIs(context.journal._native_budget, original_budget)
                    self.assertIs(separate._drift_states, publisher._drift_states)
                    self.assertIs(
                        separate._active_window_states, publisher._active_window_states
                    )
                    self.assertIs(
                        separate._response_loss_holds, publisher._response_loss_holds
                    )
                    self.assertIs(
                        separate._artifact_decisions, publisher._artifact_decisions
                    )
                    self.assertIsNot(separate.http, original_http)
                    self.assertIs(publisher.http, original_http)
                    separate._unknown_effect_holds.add(
                        (restart.IDENTITY.repository_id, restart.IDENTITY.pull_request)
                    )
                    raise RuntimeError("consumer interrupted")

                with self.assertRaisesRegex(RuntimeError, "consumer interrupted"):
                    context.run(callback)
                self.assertIs(separate.http, original_http)
                after = ledger.load(restart.IDENTITY).record
                document = _unpack_journal(read_finding_lifecycle(ledger, after))
                self.assertTrue(document["unknown_trust_effect"])
                self.assertEqual(document["effects"], old_document["effects"])
                self.assertEqual(
                    document["native_receipts"], old_document["native_receipts"]
                )
                for field in (
                    "transaction",
                    "completed_initial_reviews",
                    "completed_verification_rounds",
                    "api_run_accounting",
                    "reservation_id",
                ):
                    self.assertEqual(getattr(before, field), getattr(after, field))
                self.assertEqual(native_bytes, (root / "service.json").read_bytes())
                discussion = finding_lifecycle_consumer(
                    publisher,
                    ledger,
                    restart.IDENTITY,
                    head_sha="f" * 40,
                    app_slug="reviewsensei[bot]",
                )
                discussion.bind(separate)
                self.assertIs(separate._finding_lifecycle_consumer, discussion)
                self.assertIs(separate.http, original_http)
                readonly = LocalSessionLedger(root / "ledger")
                read_context = finding_lifecycle_consumer(
                    publisher,
                    readonly,
                    restart.IDENTITY,
                    head_sha=head,
                    app_slug="reviewsensei[bot]",
                )
                read_context.bind(separate)
                with self.assertRaisesRegex(ReviewInputError, "checkpoint authority"):
                    read_context.run(
                        lambda: self.fail("read-only context cannot write")
                    )
                with self.assertRaisesRegex(ReviewInputError, "has not been reviewed"):
                    finding_lifecycle_consumer(
                        publisher,
                        ledger,
                        restart.IDENTITY,
                        head_sha="f" * 40,
                        app_slug="reviewsensei[bot]",
                        finalizers=(separate,),
                    ).run(lambda: self.fail("must refuse before callback"))

    def test_missing_selected_root_allows_discussion_not_finalization(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(
                Path(directory), enable_finding_lifecycle_writes=True
            )
            publisher = ReviewPublisher(http=object())
            context = finding_lifecycle_consumer(
                publisher,
                ledger,
                restart.IDENTITY,
                head_sha="a" * 40,
                app_slug="reviewsensei[bot]",
            )
            context.restore_only()
            context.bind(publisher.finalizer)
            with self.assertRaisesRegex(
                ReviewInputError, "inventory_recovery_required"
            ):
                context.run(
                    lambda: self.fail("missing root cannot authorize native approval")
                )
            self.assertFalse(context.enrolled)
            self.assertIsNone(ledger.load(restart.IDENTITY).record)

    def test_second_bind_reuses_readonly_snapshot_but_run_reauthenticates(self):
        publisher = ReviewPublisher(http=object())
        context = finding_lifecycle_consumer(
            publisher,
            object(),
            restart.IDENTITY,
            head_sha="a" * 40,
            app_slug="reviewsensei[bot]",
        )
        with patch(
            "review_sensei_github.finding_lifecycle_consumers.restore_finding_lifecycle",
            return_value=(None, None),
        ) as restore:
            context.bind(publisher.finalizer)
            context.bind(ReviewPublisher(http=object()).finalizer)
            self.assertEqual(restore.call_count, 1)
            context.restore_only()
            self.assertEqual(restore.call_count, 2)
            with self.assertRaisesRegex(
                ReviewInputError, "inventory_recovery_required"
            ):
                context.run(lambda: self.fail("missing authority"))
            self.assertEqual(restore.call_count, 3)

    def test_insufficient_legacy_and_missing_authority_refuse_before_callback(self):
        publisher = ReviewPublisher(http=object())
        context = finding_lifecycle_consumer(
            publisher,
            None,
            restart.IDENTITY,
            head_sha="a" * 40,
            app_slug="reviewsensei[bot]",
        )
        with self.assertRaisesRegex(ReviewInputError, "selected session authority"):
            context.run(lambda: self.fail("must not run"))
        record = SimpleNamespace(completed_initial_reviews=1)
        with patch(
            "review_sensei_github.finding_lifecycle_consumers.restore_finding_lifecycle",
            return_value=(record, None),
        ):
            with self.assertRaisesRegex(
                ReviewInputError, "inventory_recovery_required"
            ):
                finding_lifecycle_consumer(
                    publisher,
                    object(),
                    restart.IDENTITY,
                    head_sha="a" * 40,
                    app_slug="reviewsensei[bot]",
                ).run(lambda: self.fail("must not run"))

    def test_producer_reuse_requires_selected_root_and_unique_numeric_identity(self):
        publisher = ReviewPublisher(http=object())
        context = finding_lifecycle_consumer(
            publisher,
            None,
            restart.IDENTITY,
            head_sha="a" * 40,
            app_slug="reviewsensei[bot]",
        )
        key = (restart.IDENTITY.repository_id, restart.IDENTITY.pull_request)
        publisher._native_finding_receipt_records = {key: {}}
        context.record = SimpleNamespace(
            finding_lifecycle={"manifest": {"binding": {"producer": "github-bot:123"}}}
        )
        self.assertIsNone(context.authenticated_producer_id())
        context.enrolled = True
        self.assertEqual(context.authenticated_producer_id(), 123)
        for producer in ("github-bot:0", "github-bot:nope"):
            context.record.finding_lifecycle["manifest"]["binding"]["producer"] = (
                producer
            )
            self.assertIsNone(context.authenticated_producer_id())
        context.record.finding_lifecycle["manifest"]["binding"]["producer"] = (
            "local-ledger"
        )
        for receipts, expected in (
            ({}, None),
            ({"one": {"producer_id": 123}}, 123),
            ({"one": {"producer_id": 123}, "two": {"producer_id": 456}}, None),
            ({"one": {"producer_id": True}}, None),
        ):
            publisher._native_finding_receipt_records[key] = receipts
            self.assertEqual(context.authenticated_producer_id(), expected)

    def test_slot_release_requires_positive_criterion_and_exact_native_receipt(self):
        from review_sensei_github.conversation import (
            apply_authenticated_finding_resolution,
        )

        for mode in ("window", "drift"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                restart.DurableFindingRestartTests.run_phase(
                    self, root, mode, "initial"
                )
                ledger = LocalSessionLedger(
                    root / "ledger", enable_finding_lifecycle_writes=True
                )
                record = ledger.load(restart.IDENTITY).record
                head = record.finding_lifecycle["manifest"]["binding"]["head_sha"]
                publisher = ReviewPublisher(
                    http=restart.PersistentNativeService(
                        root / "service.json", head
                    ).http
                )
                context = finding_lifecycle_consumer(
                    publisher,
                    ledger,
                    restart.IDENTITY,
                    head_sha=head,
                    app_slug="reviewsensei[bot]",
                    finalizers=(publisher.finalizer,),
                )
                key = (restart.IDENTITY.repository_id, restart.IDENTITY.pull_request)
                with context:
                    states = (
                        publisher._active_window_states
                        if mode == "window"
                        else publisher._drift_states
                    )

                    def slots():
                        state = states[key]
                        return (
                            state.display_active_findings.slots
                            if mode == "window"
                            else state.slots
                        )

                    receipts = publisher._native_finding_receipt_records[key]
                    instance = next(iter(receipts))
                    target = receipts[instance]["thread_id"]
                    initial = slots()
                    context.release_confirmed_slots(
                        head_sha=head, instances=(instance,)
                    )
                    self.assertEqual(
                        slots(), initial
                    )  # host resolution cannot waive a criterion
                    apply_authenticated_finding_resolution(
                        publisher.finalizer,
                        repository_id=key[0],
                        pull_request=key[1],
                        head_sha=head,
                        instances=(instance,),
                        evidence_sha256="a" * 64,
                    )
                    self.assertEqual(
                        next(s for s in slots() if s.target == target).state,
                        "contingent",
                    )
                    contingent = slots()
                    context.release_confirmed_slots(
                        head_sha="f" * 40, instances=(instance,)
                    )
                    context.release_confirmed_slots(
                        head_sha=head, instances=("foreign",)
                    )
                    self.assertEqual(slots(), contingent)
                    receipt = receipts[instance]
                    receipts[instance] = {**receipt, "repository_id": key[0] + 1}
                    context.release_confirmed_slots(
                        head_sha=head, instances=(instance,)
                    )
                    self.assertEqual(slots(), contingent)
                    receipts[instance] = receipt
                    context.release_confirmed_slots(
                        head_sha=head, instances=(instance,)
                    )
                    self.assertEqual(
                        next(s for s in slots() if s.target == target).state, "released"
                    )
