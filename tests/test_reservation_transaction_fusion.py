"""Original reserve/attach uses one native CAS, including uncertain readback."""

import tempfile
import unittest
from collections import Counter
from unittest.mock import patch

from review_sensei_github.api_run_requests import ApiRunSessionLedger
from review_sensei_github.review_run import ReviewRunLedger
from review_sensei_github.session_ledger import (
    GitHubIssueCommentSessionLedger,
    parse_session_comment,
)
from test_api_work_lifecycle import LifecycleFixture
from test_api_work_persistence import BASE, HEAD, IDENTITY

from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewTransaction
from review_sensei.session import InMemorySessionLedger, prepare_review_transaction

POLICY = ReviewConvergencePolicy(mode="merge-focused")


def prepare(ledger, **overrides):
    arguments = dict(
        reservation_id="d" * 64,
        base_sha=BASE,
        head_sha=HEAD,
        configuration_digest="c" * 64,
        evidence_digest="e" * 64,
    )
    arguments.update(overrides)
    return prepare_review_transaction(ledger, IDENTITY, POLICY, **arguments)


class ReservationTransactionFusionTests(unittest.TestCase):
    def setup_native(self, directory, *, reconciles=False, with_owner=False):
        fixture = LifecycleFixture(directory, limit=1024)
        native, configuration, _evidence, owner = fixture.prelude()
        if with_owner:
            # The original prelude verified this exact broker-provided owner.
            app_id = native.authenticated_app_id
            native = GitHubIssueCommentSessionLedger(
                native.http,
                token=native.token,
                app_slug="reviewsensei[bot]",
                reservation_owner={"run_id": "456", "head_sha": HEAD},
            )
            native.authenticated_app_id = app_id
        fixture.accounted.initialize_and_activate(
            native, IDENTITY, configuration, owner
        )
        cls = ReviewRunLedger if reconciles else ApiRunSessionLedger
        return fixture, cls(native, fixture.accounted, IDENTITY)

    def test_original_native_reservation_attaches_in_one_confirmed_patch(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, ledger = self.setup_native(directory)
            before = fixture.accounted._effect_record
            offset = len(fixture.trace)
            result = prepare(ledger)
            trace = fixture.trace[offset:]
            self.assertEqual(sum(method == "PATCH" for _, method, _ in trace), 1)
            expected = ReviewTransaction.create(
                repository=IDENTITY.repository,
                pull_request=IDENTITY.pull_request,
                reservation_id=result.reservation_id,
                generation=before.generation + 1,
                base_sha=BASE,
                head_sha=HEAD,
                policy_digest=POLICY.digest(),
                configuration_digest="c" * 64,
                evidence_digest="e" * 64,
            )
            self.assertEqual(result.transaction, expected)
            self.assertEqual(result.record.transaction, expected)
            self.assertEqual(result.record.reservation_owner, ledger._reservation_owner)
            self.assertEqual(
                parse_session_comment(
                    fixture.comment["body"], identity=IDENTITY
                ).transaction,
                expected,
            )

    def test_actual_native_owner_metadata_survives_atomic_attachment(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, ledger = self.setup_native(directory, with_owner=True)
            result = prepare(ledger)
            self.assertEqual(
                result.record.reservation_owner, {"run_id": "456", "head_sha": HEAD}
            )
            durable = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            self.assertEqual(durable.reservation_owner, result.record.reservation_owner)
            self.assertEqual(durable.transaction, result.transaction)

    def test_original_reconciliation_returns_only_stored_complete_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, ledger = self.setup_native(directory, reconciles=True)
            fixture.fault = "unknown-stored"
            offset = len(fixture.trace)
            result = prepare(ledger)
            self.assertEqual(sum(m == "PATCH" for _, m, _ in fixture.trace[offset:]), 1)
            self.assertEqual(result.record.transaction, result.transaction)
            self.assertGreater(fixture.accounted.accounting.uncertain, 0)
            self.assertIsNone(fixture.accounted._checkpoint)

    def test_held_reservation_does_not_attach_or_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, ledger = self.setup_native(directory)
            first = prepare(ledger)
            offset = len(fixture.trace)
            second = prepare(ledger, reservation_id="f" * 64)
            self.assertIsNone(second.reservation_id)
            self.assertEqual(second.record.transaction, first.transaction)
            self.assertFalse(any(m == "PATCH" for _, m, _ in fixture.trace[offset:]))

    def test_absent_cas_loss_never_returns_prepared_transaction_or_replays(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, ledger = self.setup_native(directory, reconciles=True)
            before = fixture.accounted._effect_record
            fixture.fault = "unknown-absent"
            offset = len(fixture.trace)
            with self.assertRaisesRegex(
                ReviewInputError, "checkpoint publication is unconfirmed"
            ):
                prepare(ledger)
            self.assertEqual(sum(m == "PATCH" for _, m, _ in fixture.trace[offset:]), 1)
            durable = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            self.assertEqual(durable.reservation_id, before.reservation_id)
            self.assertEqual(durable.transaction, before.transaction)
            self.assertGreater(fixture.accounted.accounting.uncertain, 0)
            self.assertIsNotNone(fixture.accounted._checkpoint)

    def test_operator_pause_is_not_cleared_or_attached(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, ledger = self.setup_native(directory)
            ledger.replace(IDENTITY, lambda record: record.evolve(operator_paused=True))
            offset = len(fixture.trace)
            result = prepare(ledger)
            self.assertIsNone(result.reservation_id)
            self.assertIsNone(result.transaction)
            self.assertTrue(result.record.operator_paused)
            self.assertFalse(any(m == "PATCH" for _, m, _ in fixture.trace[offset:]))

    def test_unqualified_native_adapter_keeps_two_physical_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, ledger = self.setup_native(directory)
            original = ledger.reserve
            ledger.reserve = lambda *args, **kwargs: original(*args, **kwargs)
            offset = len(fixture.trace)
            result = prepare(ledger)
            self.assertEqual(sum(m == "PATCH" for _, m, _ in fixture.trace[offset:]), 2)
            self.assertEqual(result.transaction, result.record.transaction)

    def test_copied_bindings_cannot_enter_producer_or_register_source(self):
        from review_sensei import reservation_transaction_fusion as fusion

        with self.assertRaisesRegex(ReviewInputError, "original preparation"):
            fusion.reserve_transaction(None, IDENTITY, POLICY, binding={})
        with self.assertRaisesRegex(ReviewInputError, "defining source"):
            fusion.install({"__name__": "review_sensei.session"})

    def test_fusion_saves_actual_complete_second_checkpoint_calls(self):
        traces = []
        transactions = []
        for custom in (False, True):
            with (
                self.subTest(custom=custom),
                tempfile.TemporaryDirectory() as directory,
            ):
                fixture, ledger = self.setup_native(directory)
                if custom:
                    original = ledger.reserve
                    ledger.reserve = lambda *args, **kwargs: original(*args, **kwargs)
                offset = len(fixture.trace)
                original_blocks = tuple(
                    b.block_id for b in fixture.accounted.accounting.blocks
                )
                result = prepare(ledger)
                traces.append(fixture.trace[offset:])
                transactions.append(result.transaction)
                self.assertEqual(
                    tuple(b.block_id for b in fixture.accounted.accounting.blocks),
                    original_blocks,
                )
                self.assertEqual(fixture.accounted.accounting.configuration.limit, 1024)
        self.assertEqual(transactions[0], transactions[1])
        self.assertEqual(len(traces[1]) - len(traces[0]), 4)
        difference = Counter((m, p) for _, m, p in traces[1]) - Counter(
            (m, p) for _, m, p in traces[0]
        )
        self.assertEqual(
            dict(difference),
            {
                ("GET", "/repos/owner/repo/issues/7/comments"): 3,
                ("PATCH", "/repos/owner/repo/issues/comments/1000000"): 1,
            },
        )
        self.assertEqual(sum(m == "PATCH" for _, m, _ in traces[1]), 2)
        self.assertEqual(sum(m == "PATCH" for _, m, _ in traces[0]), 1)

    def test_no_progress_and_same_head_refusals_do_not_open_a_transaction(self):
        for flag in ("no_progress", "same_head_duplicate"):
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as directory:
                fixture, ledger = self.setup_native(directory)
                offset = len(fixture.trace)
                result = prepare(ledger, **{flag: True})
                self.assertIsNone(result.reservation_id)
                self.assertIsNone(result.transaction)
                self.assertFalse(
                    any(m == "PATCH" for _, m, _ in fixture.trace[offset:])
                )

    def test_replaced_fusion_entry_is_rejected_before_native_io(self):
        from review_sensei import reservation_transaction_fusion as fusion

        with tempfile.TemporaryDirectory() as directory:
            fixture, ledger = self.setup_native(directory)
            offset = len(fixture.trace)
            with patch.object(
                fusion,
                "reserve_transaction",
                side_effect=AssertionError("replacement must not execute"),
            ):
                with self.assertRaises(ReviewInputError):
                    prepare(ledger)
            self.assertEqual(fixture.trace[offset:], [])

    def test_dependency_replacement_after_entry_is_rejected_before_cas(self):
        from review_sensei import models, reservation_transaction_fusion, session

        targets = (
            (session, "mutate_reserved"),
            (reservation_transaction_fusion, "attach_reserved"),
            (models.ReviewTransaction, "create"),
        )
        for owner, name in targets:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                fixture, ledger = self.setup_native(directory)
                original = fixture.accounted._opener
                changed = []
                replacement = patch.object(
                    owner,
                    name,
                    side_effect=AssertionError("replacement must not execute"),
                )

                def external(request, timeout):
                    if not changed:
                        changed.append(True)
                        replacement.start()
                    return original(request, timeout=timeout)

                fixture.accounted._opener = external
                offset = len(fixture.trace)
                try:
                    with self.assertRaises(ReviewInputError):
                        prepare(ledger)
                finally:
                    replacement.stop()
                self.assertTrue(changed)
                self.assertFalse(
                    any(m == "PATCH" for _, m, _ in fixture.trace[offset:])
                )
                self.assertIsNone(
                    parse_session_comment(
                        fixture.comment["body"], identity=IDENTITY
                    ).transaction
                )

    def test_custom_ledger_keeps_separate_reservation_and_attachment(self):
        class Custom(InMemorySessionLedger):
            def __init__(self):
                super().__init__()
                self.writes = []

            def replace(self, identity, mutate, *, now=None):
                record = super().replace(identity, mutate, now=now)
                self.writes.append(record)
                return record

        ledger = Custom()
        result = prepare(ledger)
        self.assertEqual(len(ledger.writes), 2)
        self.assertIsNone(ledger.writes[0].transaction)
        self.assertEqual(ledger.writes[1].transaction, result.transaction)
