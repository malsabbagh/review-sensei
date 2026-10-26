"""Sealed prepaid liability, owned attempts, and no automatic restart allowance."""

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_review_transaction as fixture
from test_assessment_queue_storage import active_summary, queue_root
from test_authenticated_partitions import binding

from review_sensei.bounded_evidence import (
    ActivationTailPlan,
    EvidenceReadBudget,
    NonResumableActivationError,
    TailDispatch,
)
from review_sensei.errors import ReviewInputError
from review_sensei.session import (
    LocalSessionLedger,
    assessment_queue_manifest_capacity,
    read_session_assessment_queue,
)

RESERVATION = "9" * 64
OPERATION = {
    "operation_id": "1" * 64,
    "source_digest": "2" * 64,
    "authority_digest": "3" * 64,
    "execution_identity": "4" * 32,
    "inventory_digest": "5" * 64,
    "inventory_generation": 0,
}


def prepare_queue(ledger, *, phase=0, hook=None):
    def prepare(record):
        document = {
            "schema_version": "1.0",
            "kind": "synthetic-complete-journal",
            "phase": phase,
        }
        manifest = ledger.stage_evidence(
            fixture.IDENTITY,
            binding=binding(
                producer=ledger.evidence_producer(), generation=0, purpose="queue"
            ),
            document=document,
            item_count=1,
            max_manifest_bytes=assessment_queue_manifest_capacity(record),
        )
        active = active_summary()
        active["read_accounting"] = {
            "calls": ledger.evidence_budget.calls,
            "deadline_at_ms": ledger.evidence_budget.wall_deadline_ms,
        }
        draft = record.evolve(
            assessment_queue=queue_root(manifest, active=active),
            generation=record.generation + 1,
            now=fixture.NOW,
        )
        if hook:
            hook(record, draft)
        return draft

    return prepare


def seal_accounting(record, accounting):
    root = copy.deepcopy(record.assessment_queue)
    root["active_operation"]["read_accounting"] = dict(accounting)
    return record.evolve(assessment_queue=root, now=fixture.NOW)


def reserve_original(ledger):
    return ledger.reserve_for_tail(
        fixture.IDENTITY,
        operation_binding=OPERATION,
        slot="verification",
        reservation_id=RESERVATION,
        expected_generation=0,
        now=fixture.NOW,
    )


def activate(ledger, **kwargs):
    return ledger.replace_with_tail(
        fixture.IDENTITY,
        prepare_queue(ledger, **kwargs),
        operation_binding=OPERATION,
        attempt_reservation_id=RESERVATION,
        seal_accounting=seal_accounting,
        now=fixture.NOW,
    )


class TicketTests(unittest.TestCase):
    def plan(self, *steps):
        return ActivationTailPlan("local", "a" * 64, tuple(steps))

    def test_ordinary_hold_refuses_tail_reservation_until_released(self):
        plan = self.plan(TailDispatch("scan:1"))
        budget = EvidenceReadBudget(clock=lambda: 10, wall_clock=lambda: 1000)
        budget.hold_ordinary(18)
        with self.assertRaises(ReviewInputError):
            budget.reserve_tail(plan)
        budget.release_ordinary(18)
        ticket = budget.reserve_tail(plan)
        # Releasing the hold restores the ordinary tail path exactly: the
        # ticket charged by the reservation can complete its activation, so the
        # hold leaves no residue behind for the prepaid sequence.
        ticket.seal("b" * 64)
        ticket.start(scope_sha256=plan.scope_sha256, root_sha256="b" * 64)
        self.assertEqual(ticket.consume("scan:1"), 60)
        ticket.finish()
        self.assertEqual(ticket.dispatched, 1)

    def test_nonrefundable_precharge_optional_scan_and_one_use(self):
        budget = EvidenceReadBudget(clock=lambda: 10, wall_clock=lambda: 1000)
        budget.consume()
        deadline, wall = budget.deadline, budget.wall_deadline_ms
        plan = self.plan(
            TailDispatch("scan:1"),
            TailDispatch("scan:2", optional=True),
            TailDispatch("root:activate", fence=True),
            TailDispatch("root:readback", fence=True),
        )
        ticket = budget.reserve_tail(plan)
        self.assertEqual(budget.calls, 5)
        self.assertEqual(budget.snapshot()["calls"], 5)
        with self.assertRaises(ReviewInputError):
            budget.consume()
        ticket.seal("b" * 64)
        ticket.start(scope_sha256=plan.scope_sha256, root_sha256="b" * 64)
        for label, fence in (
            ("scan:1", False),
            ("root:activate", True),
            ("root:readback", True),
        ):
            self.assertEqual(ticket.consume(label, fence=fence), 60)
        ticket.finish()
        self.assertEqual(ticket.dispatched, 3)
        self.assertEqual(budget.calls, 5)  # unused optional work stays charged
        self.assertEqual((budget.deadline, budget.wall_deadline_ms), (deadline, wall))
        with self.assertRaises(ReviewInputError):
            ticket.consume("root:activate", fence=True)
        budget.consume()
        self.assertEqual(budget.calls, 6)

    def test_scope_root_fence_and_mandatory_readback_cannot_be_substituted(self):
        for failure in ("scope", "root", "label", "fence", "incomplete"):
            with self.subTest(failure=failure):
                budget = EvidenceReadBudget()
                plan = self.plan(
                    TailDispatch("GET root"), TailDispatch("PATCH root", fence=True)
                )
                ticket = budget.reserve_tail(plan)
                ticket.seal("b" * 64)
                with self.assertRaises(ReviewInputError):
                    if failure in ("scope", "root"):
                        ticket.start(
                            scope_sha256="c" * 64
                            if failure == "scope"
                            else plan.scope_sha256,
                            root_sha256="c" * 64 if failure == "root" else "b" * 64,
                        )
                    else:
                        ticket.start(
                            scope_sha256=plan.scope_sha256, root_sha256="b" * 64
                        )
                        if failure == "incomplete":
                            ticket.finish()
                        else:
                            ticket.consume(
                                "GET foreign" if failure == "label" else "GET root",
                                fence=failure == "fence",
                            )
                ticket.abort()
                self.assertEqual(budget.calls, 2)

    def test_unsealed_or_fabricated_ticket_cannot_admit_dispatch(self):
        from review_sensei.bounded_evidence import EvidenceTailTicket

        budget = EvidenceReadBudget()
        plan = self.plan(TailDispatch("read"))
        fabricated = EvidenceTailTicket(budget, plan)
        with self.assertRaises(ReviewInputError):
            fabricated.seal("b" * 64)
        self.assertEqual(budget.calls, 0)
        ticket = budget.reserve_tail(plan)
        with self.assertRaises(ReviewInputError):
            ticket.start(scope_sha256=plan.scope_sha256, root_sha256=None)
        self.assertEqual(budget.calls, 1)

    def test_original_caps_restoration_and_expiry_refuse_without_refund(self):
        budget = EvidenceReadBudget()
        budget.calls = 59
        with self.assertRaises(ReviewInputError):
            budget.reserve_tail(
                self.plan(TailDispatch("read:1"), TailDispatch("read:2"))
            )
        self.assertEqual(budget.calls, 59)
        ticket = budget.reserve_tail(
            self.plan(TailDispatch("read:1"), TailDispatch("fence:1", fence=True))
        )
        ticket.abort()
        self.assertEqual(budget.calls, 61)
        restored = EvidenceReadBudget(snapshot=budget.snapshot())
        with self.assertRaises(NonResumableActivationError):
            restored.reserve_tail(self.plan(TailDispatch("fence", fence=True)))
        now = [0.0]
        budget = EvidenceReadBudget(clock=lambda: now[0])
        ticket = budget.reserve_tail(self.plan(TailDispatch("read")))
        ticket.seal("b" * 64)
        ticket.start(scope_sha256=ticket.plan.scope_sha256, root_sha256="b" * 64)
        now[0] = budget.deadline
        with self.assertRaisesRegex(ReviewInputError, "deadline"):
            ticket.consume("read")
        ticket.abort()
        self.assertEqual(budget.calls, 1)


class LocalTailTests(unittest.TestCase):
    def ledger(self, directory, budget=None):
        return LocalSessionLedger(
            Path(directory),
            evidence_budget=budget or EvidenceReadBudget(),
            enable_partition_writes=True,
        )

    def test_mutation_requires_explicit_original_budget_and_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            for ledger in (
                LocalSessionLedger(Path(directory), enable_partition_writes=True),
                LocalSessionLedger(
                    Path(directory), evidence_budget=EvidenceReadBudget()
                ),
            ):
                with self.assertRaisesRegex(ReviewInputError, "original budget"):
                    reserve_original(ledger)
                with self.assertRaisesRegex(ReviewInputError, "original budget"):
                    activate(ledger)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_complete_tail_snapshot_counts_all_work_without_double_charge(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = self.ledger(directory)
            ledger.initialize(fixture.IDENTITY, now=fixture.NOW)
            reserved = reserve_original(ledger)
            captured = []
            issue = ledger.evidence_budget.reserve_tail

            def capture(plan):
                ticket = issue(plan)
                captured.append(ticket)
                return ticket

            with patch.object(
                ledger.evidence_budget, "reserve_tail", side_effect=capture
            ):
                saved = activate(ledger)
            ticket = captured[0]
            self.assertEqual(len(ticket.plan.steps), 7)
            self.assertEqual(ticket.dispatched, 7)
            self.assertEqual(
                saved.assessment_queue["active_operation"]["read_accounting"]["calls"],
                ledger.evidence_budget.calls,
            )
            self.assertEqual(saved.reservation_id, reserved.reservation_id)
            reader = self.ledger(directory)
            with self.assertRaises(NonResumableActivationError):
                activate(self.ledger(directory, ledger.evidence_budget), phase=1)
            self.assertEqual(
                reader.load(fixture.IDENTITY, now=fixture.NOW).record, saved
            )
            self.assertEqual(read_session_assessment_queue(reader, saved)["phase"], 0)

    def test_failed_prepare_and_fresh_or_restored_restart_are_nonresumable(self):
        for failure in ("prepare", "seal"):
            with (
                self.subTest(failure=failure),
                tempfile.TemporaryDirectory() as directory,
            ):
                ledger = self.ledger(directory)
                ledger.initialize(fixture.IDENTITY, now=fixture.NOW)
                before = reserve_original(ledger)
                prepare = prepare_queue(ledger)

                def fail(record):
                    prepare(record)
                    raise ReviewInputError("synthetic pre-root failure")

                def wrong_seal(record, accounting):
                    return seal_accounting(record, accounting).evolve(
                        operator_paused=True, now=fixture.NOW
                    )

                with self.assertRaises(ReviewInputError):
                    ledger.replace_with_tail(
                        fixture.IDENTITY,
                        fail if failure == "prepare" else prepare,
                        operation_binding=OPERATION,
                        attempt_reservation_id=RESERVATION,
                        seal_accounting=wrong_seal
                        if failure == "seal"
                        else seal_accounting,
                        now=fixture.NOW,
                    )
                charged = ledger.evidence_budget.calls
                for candidate in (
                    ledger,
                    self.ledger(directory),
                    self.ledger(
                        directory,
                        EvidenceReadBudget(snapshot=ledger.evidence_budget.snapshot()),
                    ),
                ):
                    with self.assertRaises(NonResumableActivationError):
                        activate(candidate)
                self.assertEqual(ledger.evidence_budget.calls, charged)
                self.assertEqual(
                    self.ledger(directory)
                    .load(fixture.IDENTITY, now=fixture.NOW)
                    .record,
                    before,
                )
                with self.assertRaises(NonResumableActivationError):
                    reserve_original(self.ledger(directory))

    def test_deadline_immediately_before_activation_keeps_old_root_and_burns_liability(
        self,
    ):
        now = [0.0]
        with tempfile.TemporaryDirectory() as directory:
            ledger = self.ledger(directory, EvidenceReadBudget(clock=lambda: now[0]))
            ledger.initialize(fixture.IDENTITY, now=fixture.NOW)
            before = reserve_original(ledger)
            dispatch = ledger._dispatch_evidence

            def expire(label, *, fence=False):
                if label == "root:activate":
                    now[0] = ledger.evidence_budget.deadline
                return dispatch(label, fence=fence)

            with patch.object(ledger, "_dispatch_evidence", side_effect=expire):
                with self.assertRaisesRegex(ReviewInputError, "deadline"):
                    activate(ledger)
            self.assertIsNone(ledger.evidence_budget._tail_ticket)
            self.assertEqual(
                self.ledger(directory).load(fixture.IDENTITY, now=fixture.NOW).record,
                before,
            )
            with self.assertRaises(NonResumableActivationError):
                activate(ledger)

    def test_committed_receipt_remains_readable_after_original_deadline(self):
        now = [0.0]
        with tempfile.TemporaryDirectory() as directory:
            ledger = self.ledger(directory, EvidenceReadBudget(clock=lambda: now[0]))
            ledger.initialize(fixture.IDENTITY, now=fixture.NOW)
            reserve_original(ledger)
            saved = activate(ledger)
            now[0] = ledger.evidence_budget.deadline
            reader = self.ledger(directory)
            loaded = reader.load(fixture.IDENTITY, now=fixture.NOW)
            self.assertEqual(loaded.record, saved)
            self.assertEqual(read_session_assessment_queue(reader, saved)["phase"], 0)
            with self.assertRaises(NonResumableActivationError):
                activate(reader, phase=1)

    def test_foreign_root_interleaving_refuses_before_authority_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = self.ledger(directory)
            ledger.initialize(fixture.IDENTITY, now=fixture.NOW)
            reserve_original(ledger)
            newer = []

            def interleave(record, draft):
                changed = record.evolve(
                    generation=record.generation + 1,
                    operator_paused=True,
                    now=fixture.NOW,
                )
                LocalSessionLedger(Path(directory))._write(fixture.IDENTITY, changed)
                newer.append(changed)

            with self.assertRaisesRegex(ReviewInputError, "conflict"):
                activate(ledger, hook=interleave)
            self.assertEqual(
                self.ledger(directory).load(fixture.IDENTITY, now=fixture.NOW).record,
                newer[0],
            )


class ConsumingBroker:
    """Synthetic current-v3 service, never an activation grant authority.

    Kept as a shared fixture import for parser/refusal tests. Callers needing
    hosted mutation must use CurrentHostedFixture's real run-owned ledger.
    """

    def __init__(self):
        import json

        from fake_github_http import json_response
        from review_sensei_github.broker_client import BrokerClient
        from test_run_authority import authority, granted

        self.calls = []
        self.issued = {}
        self.used = set()
        self.request = authority("reply")
        self.request.update(
            repository=fixture.IDENTITY.repository,
            repository_id=fixture.IDENTITY.repository_id,
            pull_request=fixture.IDENTITY.pull_request,
            head_sha=fixture.HEAD_SHA,
        )
        self.request["source"]["id"] = 123

        def opener(request, timeout):
            payload = json.loads(request.data)
            self.calls.append(request.full_url)
            if request.full_url.endswith("/session-grant"):
                grant = payload["session_grant"]
                if (
                    grant in self.used
                    or self.issued.get(grant) != payload["session_attestation"]
                ):
                    return json_response({"message": "spent or mismatched"}, 403)
                self.used.add(grant)
                return json_response({"session_attestation": self.issued[grant]})
            attestation = granted(payload["session_attestation"])
            grant = f"sg1.1780000000000.{len(self.issued) + 1:043d}"
            self.issued[grant] = attestation
            return json_response(
                {
                    "token": "synthetic-token",
                    "capability": "review_session",
                    "session_state": "known",
                    "app_id": 999,
                    "reservation_owner": None,
                    "session_grant": grant,
                    "session_attestation": attestation,
                }
            )

        self.client = BrokerClient(
            broker_url="https://broker.test/token",
            opener=opener,
            timeout=1,
            authority=self.request,
        )

    def issue(self, budget):
        if self.client.before_request is None:
            budget.consume()
        return self.client.authorize_session_mutation(
            "synthetic-oidc",
            repository_id=fixture.IDENTITY.repository_id,
            pull_request=fixture.IDENTITY.pull_request,
            head_sha=fixture.HEAD_SHA,
            session_attestation=self.request,
        )


def feedback_request(record, budget, *, reason="admission"):
    """Refuse retired feedback-phase construction; no incoming body is upgraded."""
    raise ReviewInputError(
        "feedback phase request fixture is retired; construct current source authority"
    )


class CurrentHostedFixture:
    """Real native ledger/caller accounting; synthetic HTTP and no provider."""

    def __init__(self, directory, *, limit=512):
        from review_sensei_github.api_run_requests import ApiRunSessionLedger
        from test_api_work_lifecycle import LifecycleFixture
        from test_api_work_persistence import IDENTITY

        self.identity = IDENTITY
        self.service = LifecycleFixture(directory, limit=limit)
        native, config, _, owner = self.service.prelude()
        self.caller = self.service.accounted
        self.caller.initialize_and_activate(native, IDENTITY, config, owner)
        self.native = native
        self.ledger = ApiRunSessionLedger(native, self.caller, IDENTITY)
        self.original = self.caller.accounting

    def checkpoint(self, phase=0):
        def prepare(record):
            return record.evolve(
                generation=record.generation + 1, operator_paused=bool(phase % 2)
            )

        return self.ledger.replace(self.identity, prepare)

    def assert_physical_accounting(self, case):
        state = self.caller.accounting
        ordinary = [o for o in self.caller.observations if o["phase"] == "ordinary"]
        case.assertEqual(state.confirmed + state.uncertain, len(ordinary))
        case.assertEqual(len(self.caller.observations), len(self.service.trace))
        case.assertEqual(state.configuration, self.original.configuration)
        case.assertEqual(state.owner, self.original.owner)
        case.assertEqual(state.startup, self.original.startup)
        case.assertEqual(state.remaining, self.original.remaining)
        case.assertEqual(
            state.configuration.limit,
            state.startup.liability
            + state.prior_nonstartup_liability
            + state.confirmed
            + state.uncertain
            + state.reserved_undispatched
            + state.remaining,
        )


class HostedTailTests(unittest.TestCase):
    def setup_host(self, **kwargs):
        """Current fixture; old feedback/callback selectors have no authority."""
        if kwargs:
            raise ReviewInputError(
                "phase-grant fixture selectors are retired; use CurrentHostedFixture"
            )
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        current = CurrentHostedFixture(directory.name)
        return current.service, current.caller, current.ledger

    def test_current_checkpoints_share_one_owner_and_count_each_physical_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            f = CurrentHostedFixture(directory)
            before = len(f.service.trace)
            first = f.checkpoint()
            middle = len(f.service.trace)
            second = f.checkpoint(1)
            self.assertEqual(middle - before, 4)
            self.assertEqual(len(f.service.trace) - middle, 4)
            self.assertNotEqual(first.record_sha256, second.record_sha256)
            self.assertTrue(second.operator_paused)
            self.assertEqual(f.caller.accounting.confirmed, 8)
            self.assertEqual(
                sum("broker" in p for _, _, p in f.service.trace[before:]), 0
            )
            f.assert_physical_accounting(self)

    def test_checkpoint_evolution_keeps_original_policy_and_terminal_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            f = CurrentHostedFixture(directory)
            terminal = next(
                b for b in f.original.blocks if b.block_id == f.caller._terminal_block
            )
            for phase in range(7):
                record = f.checkpoint(phase)
                self.assertEqual(record.operator_paused, bool(phase % 2))
                self.assertEqual(
                    next(
                        b
                        for b in f.caller.accounting.blocks
                        if b.block_id == terminal.block_id
                    ),
                    terminal,
                )
                self.assertEqual(f.caller.accounting.confirmed, (phase + 1) * 4)
            self.assertGreater(f.caller.usable, 0)
            f.assert_physical_accounting(self)

    def test_current_source_operation_and_scope_changes_cannot_rebind_action(self):
        from test_api_work_actions import SOURCE
        from test_api_work_persistence import HEAD

        with tempfile.TemporaryDirectory() as directory:
            f = CurrentHostedFixture(directory)
            args = dict(
                purpose="reply", source=SOURCE, head_sha=HEAD, operation_key="d" * 64
            )
            intent = f.caller.admit_action(f.ledger, f.identity, **args)
            for changed in (
                {"source": {**SOURCE, "id": 99}},
                {"source": {**SOURCE, "body_sha256": "e" * 64}},
                {"source": {**SOURCE, "updated_at": "2026-10-26T00:00:00Z"}},
                {"operation_key": "e" * 64},
                {"head_sha": "f" * 40},
                {"purpose": "command"},
            ):
                before = len(f.service.trace)
                with (
                    self.subTest(changed=changed),
                    self.assertRaisesRegex(
                        ReviewInputError, "source or operation conflicts"
                    ),
                ):
                    f.caller.admit_action(f.ledger, f.identity, **{**args, **changed})
                self.assertEqual(len(f.service.trace), before)
            self.assertEqual(f.caller.accounting.action["intent"], intent)
            f.assert_physical_accounting(self)

    def test_fractional_current_source_identity_refuses_before_authority_http(self):
        from review_sensei_github.broker_client import BrokerClient
        from review_sensei_github.errors import GitHubBrokerClientError
        from test_run_authority import authority

        calls = []
        request = authority("reply")
        request["source"]["id"] = 123.0
        with self.assertRaises(GitHubBrokerClientError):
            BrokerClient(authority=request, opener=lambda *a, **k: calls.append(a))
        self.assertFalse(calls)

    def test_foreign_native_budget_cannot_replace_original_caller(self):
        with tempfile.TemporaryDirectory() as directory:
            f = CurrentHostedFixture(directory)
            f.ledger.evidence_budget = EvidenceReadBudget()
            before = len(f.service.trace)
            with self.assertRaisesRegex(ReviewInputError, "owning native ledger"):
                f.checkpoint()
            self.assertEqual(len(f.service.trace), before)
            f.assert_physical_accounting(self)

    def test_configured_exhaustion_refuses_checkpoint_without_new_allocation(self):
        from urllib.request import Request

        with tempfile.TemporaryDirectory() as directory:
            f = CurrentHostedFixture(directory)
            saved = f.checkpoint()
            while f.caller.usable:
                with f.caller.open(
                    Request(
                        "https://api.github.test/repos/owner/repo/pulls/7", method="GET"
                    ),
                    timeout=30,
                ) as response:
                    response.read(524288)
            before = len(f.service.trace)
            blocks = f.caller.accounting.blocks
            with self.assertRaisesRegex(ReviewInputError, "unfunded"):
                f.checkpoint(1)
            self.assertEqual(len(f.service.trace), before)
            self.assertEqual(f.caller.accounting.blocks, blocks)
            from review_sensei_github.session_ledger import parse_session_comment

            self.assertEqual(
                parse_session_comment(f.service.comment["body"], identity=f.identity),
                saved,
            )
            f.assert_physical_accounting(self)

    def test_action_entry_is_one_use_instead_of_reusable_phase_grants(self):
        from test_api_work_actions import SOURCE
        from test_api_work_persistence import HEAD

        with tempfile.TemporaryDirectory() as directory:
            f = CurrentHostedFixture(directory)
            intent = f.caller.admit_action(
                f.ledger,
                f.identity,
                purpose="reply",
                source=SOURCE,
                head_sha=HEAD,
                operation_key="d" * 64,
            )
            f.caller.begin_action_effect(f.ledger, f.identity, intent)
            before = len(f.service.trace)
            with self.assertRaisesRegex(ReviewInputError, "may have started"):
                f.caller.begin_action_effect(f.ledger, f.identity, intent)
            self.assertEqual(len(f.service.trace), before)
            self.assertTrue(f.caller.accounting.action["execution_started"])
            f.assert_physical_accounting(self)

    def test_changed_root_or_numeric_owner_cannot_publish_checkpoint(self):
        from review_sensei_github.session_ledger import (
            parse_session_comment,
            render_session_comment,
        )

        for fault in ("root", "owner"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                f = CurrentHostedFixture(directory)
                item = f.service.comment
                if fault == "root":
                    record = parse_session_comment(item["body"], identity=f.identity)
                    item["body"] = render_session_comment(
                        repository_id=f.identity.repository_id,
                        pull_request=f.identity.pull_request,
                        record=record.evolve(generation=record.generation + 1),
                    )
                elif fault == "owner":
                    item["user"]["id"] = 12345
                else:
                    item["issue_url"] = (
                        "https://api.github.test/repos/other/repo/issues/7"
                    )
                before = len(f.service.trace)
                with self.assertRaises(ReviewInputError):
                    f.checkpoint()
                self.assertTrue(all(m == "GET" for _, m, _ in f.service.trace[before:]))
                f.assert_physical_accounting(self)

    def test_foreign_or_reconstructed_operation_cannot_supply_confirmation_tail(self):
        from review_sensei_github.caller_operation import CallerOperation

        with tempfile.TemporaryDirectory() as directory:
            f = CurrentHostedFixture(directory)
            for retained in (CallerOperation(10), copy.copy(CallerOperation(10))):
                before = len(f.service.trace)
                with self.assertRaisesRegex(
                    ReviewInputError, "foreign, settled or reconstructed"
                ):
                    with f.caller.read_operation("foreign", retained=retained):
                        self.fail("foreign operation accepted")
                self.assertEqual(len(f.service.trace), before)
            f.assert_physical_accounting(self)

    def test_unknown_patch_keeps_charge_and_original_readback_without_replay(self):
        from review_sensei_github.errors import GitHubPublicationTransientError

        for fault in ("unknown-stored", "unknown-absent"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                f = CurrentHostedFixture(directory)
                before = len(f.service.trace)
                f.service.fault = fault
                with self.assertRaises(GitHubPublicationTransientError):
                    f.checkpoint(1)
                if fault == "unknown-stored":
                    retained = f.caller.reconcile_checkpoint(f.ledger, f.identity)
                    self.assertTrue(retained.operator_paused)
                else:
                    with self.assertRaises(ReviewInputError):
                        f.caller.reconcile_checkpoint(f.ledger, f.identity)
                used = len(f.service.trace)
                with self.assertRaises(ReviewInputError):
                    f.caller.reconcile_checkpoint(f.ledger, f.identity)
                self.assertEqual(len(f.service.trace), used)
                self.assertEqual(
                    sum(m == "PATCH" for _, m, _ in f.service.trace[before:]), 1
                )
                self.assertEqual(f.caller.accounting.uncertain, 1)
                f.assert_physical_accounting(self)

    def test_committed_checkpoint_stays_readable_after_accounted_postprocessing_failure(
        self,
    ):
        from fake_github_http import json_response
        from review_sensei_github.errors import GitHubPublicationTransientError
        from review_sensei_github.session_ledger import parse_session_comment

        with tempfile.TemporaryDirectory() as directory:
            f = CurrentHostedFixture(directory)
            original = f.caller._opener

            def committed_then_unavailable(request, timeout):
                result = original(request, timeout)
                return json_response({}, 503) if request.method == "PATCH" else result

            f.caller._opener = committed_then_unavailable
            with self.assertRaises(GitHubPublicationTransientError):
                f.checkpoint(1)
            f.caller._opener = original
            retained = f.caller.reconcile_checkpoint(f.ledger, f.identity)
            self.assertTrue(retained.operator_paused)
            self.assertEqual(
                parse_session_comment(f.service.comment["body"], identity=f.identity),
                retained,
            )
            f.assert_physical_accounting(self)

    def test_finite_scan_stops_before_mutation_and_cannot_replenish(self):
        with tempfile.TemporaryDirectory() as directory:
            f = CurrentHostedFixture(directory)
            before = len(f.service.trace)
            f.service.extra_comments.update(
                {
                    2000 + i: {"id": 2000 + i, "body": "human comment"}
                    for i in range(600)
                }
            )
            with self.assertRaisesRegex(
                ReviewInputError, "preserve write confirmation"
            ):
                f.checkpoint()
            used = len(f.service.trace)
            self.assertEqual(used - before, 7)
            self.assertTrue(all(m == "GET" for _, m, _ in f.service.trace[before:]))
            with self.assertRaisesRegex(ReviewInputError, "cannot be replenished"):
                f.checkpoint()
            self.assertEqual(len(f.service.trace), used)
            f.assert_physical_accounting(self)

    def test_broker_bound_ledger_and_tail_rebind_remain_retired_before_http(self):
        from review_sensei_github.session_ledger import GitHubIssueCommentSessionLedger

        with tempfile.TemporaryDirectory() as directory:
            f = CurrentHostedFixture(directory)
            before = len(f.service.trace)
            for key, value in (
                ("broker", ConsumingBroker().client),
                ("session_grant", "synthetic"),
                ("session_attestation", ConsumingBroker().request),
                ("head_sha", fixture.HEAD_SHA),
            ):
                with (
                    self.subTest(key=key),
                    self.assertRaisesRegex(
                        ReviewInputError, "broker-bound phase ledgers are retired"
                    ),
                ):
                    GitHubIssueCommentSessionLedger(
                        f.service.http,
                        token="synthetic",
                        app_slug="reviewsensei[bot]",
                        **{key: value},
                    )
            with self.assertRaisesRegex(
                ReviewInputError, "phase activation grants are retired"
            ):
                f.ledger.bind_tail_grant(
                    f.identity,
                    None,
                    operation_binding=OPERATION,
                    attempt_reservation_id=RESERVATION,
                    max_scan_pages=1,
                )
            self.assertEqual(len(f.service.trace), before)

    def test_history_association_checks_native_root_repository_and_pr(self):
        with tempfile.TemporaryDirectory() as directory:
            f = CurrentHostedFixture(directory)
            f.service.comment["issue_url"] = (
                "https://api.github.test/repos/other/repo/issues/7"
            )
            before = len(f.service.trace)
            with self.assertRaisesRegex(ReviewInputError, "association does not match"):
                f.ledger._history_load(f.identity, max_scan_pages=1, now=None)
            self.assertTrue(all(m == "GET" for _, m, _ in f.service.trace[before:]))
            f.assert_physical_accounting(self)

    def test_obsolete_versions_and_phase_documents_refuse_before_broker_http(self):
        from review_sensei_github.broker_client import BrokerClient
        from review_sensei_github.errors import GitHubBrokerClientError
        from test_run_authority import authority

        calls = []
        budget = EvidenceReadBudget()
        current = authority("reply")
        client = BrokerClient(
            authority=current,
            before_request=budget.consume,
            opener=lambda *args, **kwargs: calls.append(args),
        )
        for document in (
            {**current, "version": 1},
            {**current, "version": 2},
            {**current, "mutation": {"root_digest": "a" * 64}},
            {**current, "feedback": {"interface": "feedback-v1"}},
        ):
            with (
                self.subTest(document=document),
                self.assertRaises(GitHubBrokerClientError),
            ):
                client.authorize_session_mutation(
                    "synthetic-oidc",
                    repository_id=current["repository_id"],
                    pull_request=current["pull_request"],
                    head_sha=current["head_sha"],
                    session_attestation=document,
                )
            self.assertFalse(calls)
            self.assertEqual(budget.calls, 0)
