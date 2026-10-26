"""OperationHandle is the original-attempt witness. Snapshots stay refused."""

import tempfile
import unittest
from pathlib import Path

import test_review_transaction as fixture
from review_sensei_github.operation_host import OperationHandle
from test_activation_tail import (
    OPERATION,
    RESERVATION,
    prepare_queue,
    seal_accounting,
)

from review_sensei.bounded_evidence import (
    ActivationTailPlan,
    EvidenceReadBudget,
    NonResumableActivationError,
    TailDispatch,
)
from review_sensei.session import LocalSessionLedger


def operation_handle(binding: str) -> OperationHandle:
    return OperationHandle(
        scope_digest="a" * 64,
        event_id="b" * 64,
        operation_id="c" * 64,
        binding=binding,
        created_at_ms=1,
        control_deadline_ms=2,
        calls=1,
        sequence=0,
        root_sha="e" * 64,
        root_generation=0,
    )


def restored_budget() -> EvidenceReadBudget:
    return EvidenceReadBudget(snapshot=EvidenceReadBudget().snapshot())


class BudgetWitnessTests(unittest.TestCase):
    def plan(self) -> ActivationTailPlan:
        return ActivationTailPlan("local", "a" * 64, (TailDispatch("read"),))

    def test_restored_tail_accepts_handle_and_refuses_snapshots(self):
        handle = operation_handle(RESERVATION)
        admitted = restored_budget()
        ticket = admitted.reserve_tail(
            self.plan(), witness=handle, reservation_id=RESERVATION
        )
        self.assertEqual(admitted.calls, 1)
        self.assertIs(admitted._tail_ticket, ticket)
        self.assertEqual(ticket.plan.adapter, "local")
        for witness in ({"binding": RESERVATION}, RESERVATION, {"calls": 1}):
            with self.subTest(witness=type(witness).__name__):
                budget = restored_budget()
                with self.assertRaises(NonResumableActivationError) as caught:
                    budget.reserve_tail(
                        self.plan(), witness=witness, reservation_id=RESERVATION
                    )
                self.assertEqual(
                    caught.exception.reason, "original-attempt-witness-required"
                )
                self.assertEqual(budget.calls, admitted.calls - 1)

    def test_live_attempt_gates_accept_handle_and_refuse_snapshots(self):
        handle = operation_handle(RESERVATION)
        owner = object()
        root = "b" * 64
        remembered = restored_budget()
        remembered._remember_live_attempt(
            "scope",
            root,
            owner=owner,
            witness=handle,
            reservation_id=RESERVATION,
        )
        self.assertEqual(remembered._live_attempts["scope"], root)
        self.assertIs(remembered._live_attempt_owners["scope"], owner)
        missing = EvidenceReadBudget()
        missing._require_live_attempt(
            "scope",
            root,
            owner=owner,
            witness=handle,
            reservation_id=RESERVATION,
        )
        for witness in ({"binding": RESERVATION}, RESERVATION):
            with self.subTest(witness=type(witness).__name__):
                budget = restored_budget()
                with self.assertRaises(NonResumableActivationError) as caught:
                    budget._remember_live_attempt(
                        "scope",
                        root,
                        owner=owner,
                        witness=witness,
                        reservation_id=RESERVATION,
                    )
                self.assertEqual(
                    caught.exception.reason, "original-attempt-witness-required"
                )
                self.assertNotIn("scope", budget._live_attempts)
                fresh = EvidenceReadBudget()
                with self.assertRaises(NonResumableActivationError):
                    fresh._require_live_attempt(
                        "scope",
                        root,
                        owner=owner,
                        witness=witness,
                        reservation_id=RESERVATION,
                    )

    def test_burned_attempt_stays_refused_with_operation_handle(self):
        budget = EvidenceReadBudget()
        owner = object()
        budget._remember_live_attempt("scope", "b" * 64, owner=owner)
        budget._burn_live_attempt("scope")
        with self.assertRaises(NonResumableActivationError):
            budget._require_live_attempt(
                "scope",
                "b" * 64,
                owner=owner,
                witness=operation_handle(RESERVATION),
                reservation_id=RESERVATION,
            )


class LocalLedgerWitnessTests(unittest.TestCase):
    def ledger(self, directory, budget=None):
        return LocalSessionLedger(
            Path(directory),
            evidence_budget=budget or EvidenceReadBudget(),
            enable_partition_writes=True,
        )

    def reserve(self, ledger, witness=None):
        return ledger.reserve_for_tail(
            fixture.IDENTITY,
            operation_binding=OPERATION,
            slot="verification",
            reservation_id=RESERVATION,
            expected_generation=0,
            now=fixture.NOW,
            witness=witness,
        )

    def activate(self, ledger, witness=None):
        return ledger.replace_with_tail(
            fixture.IDENTITY,
            prepare_queue(ledger),
            operation_binding=OPERATION,
            attempt_reservation_id=RESERVATION,
            seal_accounting=seal_accounting,
            now=fixture.NOW,
            witness=witness,
        )

    def test_restored_reserve_and_activation_accept_handle(self):
        handle = operation_handle(RESERVATION)
        with tempfile.TemporaryDirectory() as directory:
            original = self.ledger(directory)
            original.initialize(fixture.IDENTITY, now=fixture.NOW)
            self.reserve(original)
            restored = self.ledger(
                directory,
                EvidenceReadBudget(snapshot=original.evidence_budget.snapshot()),
            )
            for witness in ({"binding": RESERVATION}, RESERVATION):
                with self.subTest(witness=type(witness).__name__):
                    with self.assertRaises(NonResumableActivationError) as caught:
                        self.activate(restored, witness)
                    self.assertEqual(
                        caught.exception.reason, "original-attempt-witness-required"
                    )
            saved = self.activate(restored, handle)
            self.assertEqual(saved.reservation_id, RESERVATION)
            self.assertEqual(saved.generation, 2)

    def test_restored_reserve_accepts_handle_and_refuses_snapshots(self):
        handle = operation_handle(RESERVATION)
        with tempfile.TemporaryDirectory() as directory:
            ledger = self.ledger(directory, restored_budget())
            ledger.initialize(fixture.IDENTITY, now=fixture.NOW)
            for witness in ({"binding": RESERVATION}, RESERVATION):
                with self.assertRaises(NonResumableActivationError):
                    self.reserve(ledger, witness)
            reserved = self.reserve(ledger, handle)
            self.assertEqual(reserved.reservation_id, RESERVATION)


class GitHubLedgerWitnessTests(unittest.TestCase):
    def test_current_caller_rejects_restored_snapshots_and_fabricated_operation_tails(
        self,
    ):
        from review_sensei_github.api_run_requests import ApiRunSessionLedger
        from review_sensei_github.caller_operation import CallerOperation
        from test_activation_tail import CurrentHostedFixture

        from review_sensei.errors import ReviewInputError

        with tempfile.TemporaryDirectory() as directory:
            f = CurrentHostedFixture(directory)
            # Current hosted authority is the live native caller, never an old
            # serialized metadata counter or reconstructed OperationHandle.
            with self.assertRaisesRegex(ReviewInputError, "authenticated run record"):
                f.caller.budget_view.snapshot()
            for witness in (
                {"binding": RESERVATION},
                RESERVATION,
                operation_handle(RESERVATION),
                CallerOperation(10),
            ):
                before = len(f.service.trace)
                with (
                    self.subTest(witness=type(witness).__name__),
                    self.assertRaisesRegex(
                        ReviewInputError, "phase activation grants are retired"
                    ),
                ):
                    f.ledger.bind_tail_grant(
                        f.identity,
                        None,
                        operation_binding=OPERATION,
                        attempt_reservation_id=RESERVATION,
                        max_scan_pages=1,
                        witness=witness,
                    )
                self.assertEqual(len(f.service.trace), before)
            for retained in (CallerOperation(10), CallerOperation(10, write_tail=3)):
                before = len(f.service.trace)
                with self.assertRaisesRegex(
                    ReviewInputError, "foreign, settled or reconstructed"
                ):
                    with f.caller.read_operation("forged", retained=retained):
                        self.fail("reconstructed operation accepted")
                self.assertEqual(len(f.service.trace), before)
            other = ApiRunSessionLedger(f.native, f.caller, f.identity)
            first = f.checkpoint()
            retained = other.replace(
                f.identity,
                lambda record: record.evolve(generation=record.generation + 1),
            )
            self.assertGreater(retained.generation, first.generation)
            self.assertIs(other.evidence_budget, f.caller.budget_view)
            self.assertEqual(len(f.caller.accounting.blocks), len(f.original.blocks))
            f.assert_physical_accounting(self)
