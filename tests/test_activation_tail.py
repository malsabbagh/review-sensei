"""Sealed prepaid liability, owned attempts, and no automatic restart allowance."""

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
from tests import test_review_transaction as fixture
from tests.test_assessment_queue_storage import active_summary, queue_root
from tests.test_authenticated_partitions import binding

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
    """Real client parsing, with an in-memory one-attempt HTTP service fixture."""

    def __init__(self):
        import json

        from review_sensei.hosting.github.broker_client import BrokerClient
        from tests.fake_github_http import json_response

        self.calls = []
        self.issued = {}
        self.used = set()
        self.request = {
            "version": 1,
            "repository": fixture.IDENTITY.repository,
            "repository_id": fixture.IDENTITY.repository_id,
            "pull_request": fixture.IDENTITY.pull_request,
            "head_sha": fixture.HEAD_SHA,
            "operation": "command",
            "source_comment_id": 123,
            "run_id": "10000000001",
            "issued_at": 1700000000,
            "concurrency_group": "reviewsensei-session-test",
            "job_workflow_ref": "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@refs/tags/v5",
            "job_workflow_sha": "a" * 40,
        }

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
            attestation = {
                **payload["session_attestation"],
                "actor": "maintainer",
                "actor_type": "User",
                "association": "OWNER",
            }
            if attestation["version"] == 2:
                attestation["actor_id"] = 42
            else:
                attestation.update(command_id=123, command_digest="b" * 64)
            grant = f"{len(self.issued) + 1:043d}"
            self.issued[grant] = attestation
            return json_response(
                {
                    "token": "synthetic-token",
                    "session_state": "known",
                    "session_grant": grant,
                    "session_attestation": attestation,
                }
            )

        self.client = BrokerClient(
            broker_url="https://broker.test/token", opener=opener, timeout=1
        )

    def issue(self, budget):
        if self.client.before_request is None:
            budget.consume()  # external composition work, not free authority
        return self.client.authorize_session_mutation(
            "synthetic-oidc",
            repository_id=fixture.IDENTITY.repository_id,
            pull_request=fixture.IDENTITY.pull_request,
            head_sha=fixture.HEAD_SHA,
            session_attestation=self.request,
        )


def feedback_request(record, budget, *, reason="admission"):
    from review_sensei.evidence import evidence_digest

    trigger = {"kind": "issue", "comment_id": 123, "updated_at": "2026-10-10T00:00:00Z"}
    selection = {
        "interface": "feedback-v1",
        "repository": fixture.IDENTITY.repository,
        "pull_request": fixture.IDENTITY.pull_request,
        "base_sha": fixture.BASE_SHA,
        "head_sha": fixture.HEAD_SHA,
        "trigger": trigger,
        "target_ids": ["d" * 64],
        "total_bytes": 10,
        "sources": [
            {
                **trigger,
                "author": "maintainer",
                "author_id": 42,
                "association": "OWNER",
                "root_comment_id": None,
                "body_bytes": 10,
                "body_sha256": "c" * 64,
            }
        ],
        "selection_digest": "e" * 64,
        "event_key": evidence_digest(
            {
                "domain": "reviewsensei:feedback-event:v1",
                "repository": fixture.IDENTITY.repository,
                "pull_request": fixture.IDENTITY.pull_request,
                "trigger": {"kind": "issue", "comment_id": 123},
            }
        ),
    }
    request = ConsumingBroker().request
    request.update(
        version=2,
        operation="feedback",
        feedback=selection,
        concurrency_group=f"reviewsensei-session-{fixture.IDENTITY.repository_id}-{fixture.IDENTITY.pull_request}",
        mutation={
            **OPERATION,
            "reservation_id": RESERVATION,
            "root_digest": record.record_sha256,
            "root_generation": record.generation,
            "reason": reason,
            "request_digest": None,
            "dispatch_digest": None,
            "read_accounting": budget.snapshot(),
        },
    )
    return request


class HostedTailTests(unittest.TestCase):
    def setup_host(self, *, clock=None, feedback=False, callback=False):
        from review_sensei.hosting.github import GitHubHttp
        from review_sensei.hosting.github.session_ledger import (
            GitHubIssueCommentSessionLedger,
        )
        from tests.fake_github_http import json_response
        from tests.test_authenticated_partitions import GitHubState

        state = GitHubState()
        original_open = state.open
        state.live_head = fixture.HEAD_SHA
        state.head_hook = None

        def opener(request, timeout):
            if request.full_url.endswith(f"/pulls/{fixture.IDENTITY.pull_request}"):
                state.calls.append((request.method, request.full_url))
                if state.head_hook:
                    state.head_hook()
                return json_response({"head": {"sha": state.live_head}})
            return original_open(request, timeout)

        state.http = GitHubHttp(api_url="https://api.github.test", opener=opener)
        initial = state.ledger().initialize(fixture.IDENTITY, now=fixture.NOW)
        state.calls.clear()  # enrollment preceded this new operation
        broker = ConsumingBroker()
        budget = EvidenceReadBudget(**({"clock": clock} if clock else {}))
        if callback:
            broker.client.before_request = budget.consume
        if feedback:
            broker.request = feedback_request(initial, budget)
        first = broker.issue(budget)
        ledger = GitHubIssueCommentSessionLedger(
            state.http,
            token=first.token,
            app_slug="sensei[bot]",
            broker=broker.client,
            session_grant=first.grant,
            session_attestation=first.attestation,
            head_sha=fixture.HEAD_SHA,
            evidence_budget=budget,
            enable_partition_writes=True,
        )
        return state, broker, ledger

    def reserve(self, ledger):
        return ledger.reserve_for_tail(
            fixture.IDENTITY,
            operation_binding=OPERATION,
            slot="verification",
            reservation_id=RESERVATION,
            expected_generation=0,
            max_scan_pages=1,
            head_sha=fixture.HEAD_SHA,
            now=fixture.NOW,
        )

    def fresh(self, broker, ledger, *, pages=1):
        grant = broker.issue(ledger.evidence_budget)
        ledger.bind_tail_grant(
            fixture.IDENTITY,
            grant,
            operation_binding=OPERATION,
            attempt_reservation_id=RESERVATION,
            max_scan_pages=pages,
            now=fixture.NOW,
        )
        return grant

    def activate(self, ledger, *, phase=0, pages=1):
        return ledger.replace_with_tail(
            fixture.IDENTITY,
            prepare_queue(ledger, phase=phase),
            operation_binding=OPERATION,
            attempt_reservation_id=RESERVATION,
            seal_accounting=seal_accounting,
            max_scan_pages=pages,
            now=fixture.NOW,
        )

    def test_real_consuming_grants_rebind_same_ledger_and_account_exact_tail(self):
        state, broker, ledger = self.setup_host()
        self.reserve(ledger)
        self.fresh(broker, ledger)
        tickets = []
        reserve = ledger.evidence_budget.reserve_tail

        def capture(plan):
            ticket = reserve(plan)
            tickets.append(ticket)
            return ticket

        with patch.object(ledger.evidence_budget, "reserve_tail", side_effect=capture):
            first = self.activate(ledger)
            self.fresh(broker, ledger)
            second = self.activate(ledger, phase=1)
        self.assertEqual(len(broker.used), 3)
        self.assertEqual([len(ticket.plan.steps) for ticket in tickets], [6, 7])
        self.assertEqual([ticket.dispatched for ticket in tickets], [6, 7])
        self.assertEqual(
            ledger.evidence_budget.calls, len(state.calls) + len(broker.calls)
        )
        self.assertEqual(
            second.assessment_queue["active_operation"]["read_accounting"]["calls"],
            ledger.evidence_budget.calls,
        )
        self.assertNotEqual(first.record_sha256, second.record_sha256)

    def test_v2_checkpoint_claims_evolve_on_original_live_ledger_without_double_charge(
        self,
    ):
        state, broker, ledger = self.setup_host(feedback=True, callback=True)
        current = self.reserve(ledger)
        for phase, reason in ((0, "admission"), (1, "accepted")):
            request = feedback_request(current, ledger.evidence_budget, reason=reason)
            request["issued_at"] += phase + 1
            request["mutation"]["request_digest"] = "f" * 64 if phase else None
            request["mutation"]["dispatch_digest"] = "d" * 64 if phase else None
            broker.request = request
            self.fresh(broker, ledger)
            current = self.activate(ledger, phase=phase)
        self.assertEqual(ledger.evidence_budget.calls, 44)
        self.assertEqual(
            ledger.evidence_budget.calls, len(state.calls) + len(broker.calls)
        )
        self.assertEqual(
            current.assessment_queue["active_operation"]["read_accounting"]["calls"], 44
        )
        self.assertEqual(len(broker.used), 3)

    def test_v2_wrong_root_binding_original_accounting_and_source_refuse_rebind(self):
        for fault in (
            "root_digest",
            "root_generation",
            "operation_id",
            "source_digest",
            "authority_digest",
            "execution_identity",
            "inventory_digest",
            "inventory_generation",
            "reservation_id",
            "deadline",
            "calls",
            "future_calls",
            "actor",
            "feedback",
        ):
            with self.subTest(fault=fault):
                state, broker, ledger = self.setup_host(feedback=True)
                current = self.reserve(ledger)
                broker.request = feedback_request(current, ledger.evidence_budget)
                mutation = broker.request["mutation"]
                if fault == "deadline":
                    mutation["read_accounting"]["deadline_unix_ms"] += 1
                elif fault == "calls":
                    # Previous attestation accounting alone can be behind all
                    # dispatches: retained completed-root liability is the floor.
                    self.fresh(broker, ledger)
                    current = self.activate(ledger)
                    broker.request = feedback_request(current, ledger.evidence_budget)
                    broker.request["mutation"]["read_accounting"]["calls"] = 0
                elif fault == "future_calls":
                    mutation["read_accounting"]["calls"] = 64
                elif fault in ("root_generation", "inventory_generation"):
                    mutation[fault] += 1
                elif fault == "feedback":
                    broker.request["feedback"]["selection_digest"] = "a" * 64
                elif fault == "actor":
                    broker.request["feedback"]["sources"][0]["author_id"] = 43
                else:
                    mutation[fault] = "7" * (
                        32 if fault == "execution_identity" else 64
                    )
                # Parser itself refuses a changed source actor inconsistent with
                # returned broker actor_id; other references parse then bind refuses.
                from review_sensei.hosting.github.errors import GitHubBrokerClientError

                with self.assertRaises(
                    ReviewInputError if fault != "actor" else GitHubBrokerClientError
                ):
                    self.fresh(broker, ledger)
                self.assertEqual(
                    state.ledger().load(fixture.IDENTITY, now=fixture.NOW).record,
                    current,
                )

    def test_v2_fractional_trigger_identity_refuses_before_authority_issue(self):
        from review_sensei.hosting.github.errors import GitHubBrokerClientError

        state, broker, ledger = self.setup_host(feedback=True, callback=True)
        broker.request["feedback"]["trigger"]["comment_id"] = 123.0
        calls = len(broker.calls)
        with self.assertRaises(GitHubBrokerClientError):
            broker.issue(ledger.evidence_budget)
        self.assertEqual(len(broker.calls), calls)
        self.assertEqual(
            state.ledger().load(fixture.IDENTITY, now=fixture.NOW).record.generation, 0
        )

    def test_broker_hook_requires_same_original_budget_not_a_new_allowance(self):
        state, broker, ledger = self.setup_host(callback=True)
        broker.client.before_request = EvidenceReadBudget().consume
        count = len(broker.calls)
        with self.assertRaisesRegex(ReviewInputError, "original budget"):
            self.reserve(ledger)
        self.assertEqual(len(broker.calls), count)
        self.assertEqual(
            state.ledger().load(fixture.IDENTITY, now=fixture.NOW).record.generation, 0
        )

    def test_third_minimal_checkpoint_refuses_original_cap_before_ack(self):
        state, broker, ledger = self.setup_host()
        self.reserve(ledger)
        self.assertEqual(ledger.evidence_budget.calls, 8)
        for phase, expected in ((0, 24), (1, 44)):
            self.fresh(broker, ledger)
            saved = self.activate(ledger, phase=phase)
            self.assertEqual(ledger.evidence_budget.calls, expected)
        self.fresh(broker, ledger)
        self.assertEqual(ledger.evidence_budget.calls, 48)
        with self.assertRaisesRegex(ReviewInputError, "liability"):
            self.activate(ledger, phase=2)
        # Third staging is charged and retained; no root activation or source ack.
        self.assertEqual(ledger.evidence_budget.calls, 57)
        self.assertEqual(len(state.calls), 49)
        self.assertEqual(len(broker.calls), 8)
        reader = state.ledger()
        self.assertEqual(reader.load(fixture.IDENTITY, now=fixture.NOW).record, saved)
        calls = ledger.evidence_budget.calls
        with self.assertRaises(NonResumableActivationError):
            self.activate(ledger, phase=2)
        self.assertEqual(ledger.evidence_budget.calls, calls)

    def test_reused_grant_cannot_supply_second_checkpoint(self):
        state, broker, ledger = self.setup_host()
        before = self.reserve(ledger)
        self.assertEqual(len(broker.used), 1)
        with self.assertRaisesRegex(ReviewInputError, "verification"):
            self.activate(ledger)
        self.assertEqual(
            state.ledger().load(fixture.IDENTITY, now=fixture.NOW).record, before
        )
        calls = ledger.evidence_budget.calls
        with self.assertRaises(NonResumableActivationError):
            self.activate(ledger)
        self.assertEqual(ledger.evidence_budget.calls, calls)

    def test_rebind_stale_head_scope_and_numeric_root_owner_refuse_before_install(self):
        from dataclasses import replace

        for fault in ("head", "scope", "owner", "association"):
            with self.subTest(fault=fault):
                state, broker, ledger = self.setup_host()
                self.reserve(ledger)
                grant = broker.issue(ledger.evidence_budget)
                old_grant = ledger._session_grant
                if fault == "head":
                    state.live_head = "c" * 40
                elif fault == "scope":
                    grant = replace(
                        grant, attestation={**grant.attestation, "run_id": "99"}
                    )
                elif fault == "owner":
                    next(iter(state.comments.values()))["user"]["id"] = 56
                else:
                    next(iter(state.comments.values()))["issue_url"] = (
                        "https://api.github.test/repos/other/repo/issues/1"
                    )
                with self.assertRaises(ReviewInputError):
                    ledger.bind_tail_grant(
                        fixture.IDENTITY,
                        grant,
                        operation_binding=OPERATION,
                        attempt_reservation_id=RESERVATION,
                        max_scan_pages=1,
                        now=fixture.NOW,
                    )
                self.assertEqual(ledger._session_grant, old_grant)
                with self.assertRaises(NonResumableActivationError):
                    self.activate(ledger)

    def test_new_ledger_same_budget_cannot_rebind_original_live_proof(self):
        from review_sensei.hosting.github.session_ledger import (
            GitHubIssueCommentSessionLedger,
        )

        state, broker, ledger = self.setup_host()
        self.reserve(ledger)
        grant = broker.issue(ledger.evidence_budget)
        other = GitHubIssueCommentSessionLedger(
            state.http,
            token=grant.token,
            app_slug="sensei[bot]",
            broker=broker.client,
            session_grant=grant.grant,
            session_attestation=grant.attestation,
            head_sha=fixture.HEAD_SHA,
            evidence_budget=ledger.evidence_budget,
            enable_partition_writes=True,
        )
        calls = ledger.evidence_budget.calls
        with self.assertRaises(NonResumableActivationError):
            other.bind_tail_grant(
                fixture.IDENTITY,
                grant,
                operation_binding=OPERATION,
                attempt_reservation_id=RESERVATION,
                max_scan_pages=1,
                now=fixture.NOW,
            )
        with self.assertRaises(NonResumableActivationError):
            self.activate(other)
        self.assertEqual(ledger.evidence_budget.calls, calls)

    def test_patch_ambiguity_and_expiry_burn_prepaid_tail_without_refund(self):
        for fault in ("patch", "expiry"):
            with self.subTest(fault=fault):
                now = [0.0]
                state, broker, ledger = self.setup_host(clock=lambda: now[0])
                before = self.reserve(ledger)
                self.fresh(broker, ledger)
                tickets = []
                reserve = ledger.evidence_budget.reserve_tail

                def capture(plan):
                    ticket = reserve(plan)
                    tickets.append(ticket)
                    return ticket

                if fault == "patch":
                    state.fail_root_patch = True
                else:
                    state.head_hook = lambda: (
                        now.__setitem__(0, ledger.evidence_budget.deadline)
                        if ledger._activation_ticket is not None
                        else None
                    )
                with patch.object(
                    ledger.evidence_budget, "reserve_tail", side_effect=capture
                ):
                    from review_sensei.hosting.github.errors import (
                        GitHubPublicationTransientError,
                    )

                    with self.assertRaises(
                        (ReviewInputError, GitHubPublicationTransientError)
                    ):
                        self.activate(ledger)
                self.assertEqual(
                    ledger.evidence_budget.calls,
                    len(state.calls)
                    + len(broker.calls)
                    + len(tickets[0].plan.steps)
                    - tickets[0].dispatched,
                )
                self.assertEqual(
                    state.ledger().load(fixture.IDENTITY, now=fixture.NOW).record,
                    before,
                )
                with self.assertRaises(NonResumableActivationError):
                    self.activate(ledger)

    def test_committed_but_lost_patch_response_keeps_readable_prepaid_root(self):
        from review_sensei.hosting.github.errors import GitHubPublicationTransientError
        from tests.fake_github_http import json_response

        state, broker, ledger = self.setup_host(feedback=True, callback=True)
        reserved = self.reserve(ledger)
        broker.request = feedback_request(reserved, ledger.evidence_budget)
        self.fresh(broker, ledger)
        original = state.http.opener

        def commit_then_ambiguous(request, timeout):
            response = original(request, timeout)
            if request.method == "PATCH":
                return json_response({}, 503)
            return response

        with patch.object(state.http, "opener", side_effect=commit_then_ambiguous):
            with self.assertRaises(GitHubPublicationTransientError):
                self.activate(ledger)
        reader = state.ledger()
        saved = reader.load(fixture.IDENTITY, now=fixture.NOW).record
        self.assertNotEqual(saved.record_sha256, reserved.record_sha256)
        self.assertEqual(
            saved.assessment_queue["active_operation"]["read_accounting"]["calls"],
            ledger.evidence_budget.calls,
        )
        self.assertEqual(read_session_assessment_queue(reader, saved)["phase"], 0)
        calls = ledger.evidence_budget.calls
        with self.assertRaises(NonResumableActivationError):
            self.activate(ledger, phase=1)
        self.assertEqual(ledger.evidence_budget.calls, calls)

    def test_scan_liability_is_prepaid_and_over_bound_refuses_before_post(self):
        state, broker, ledger = self.setup_host()
        self.reserve(ledger)
        self.fresh(broker, ledger, pages=2)
        captured = []
        reserve = ledger.evidence_budget.reserve_tail

        def capture(plan):
            ticket = reserve(plan)
            captured.append(ticket)
            return ticket

        with patch.object(ledger.evidence_budget, "reserve_tail", side_effect=capture):
            saved = self.activate(ledger, pages=2)
        ticket = captured[0]
        self.assertEqual(len(ticket.plan.steps), 8)
        self.assertEqual(ticket.dispatched, 6)
        self.assertEqual(
            ledger.evidence_budget.calls, len(state.calls) + len(broker.calls) + 2
        )
        self.assertEqual(
            saved.assessment_queue["active_operation"]["read_accounting"]["calls"],
            ledger.evidence_budget.calls,
        )
        self.fresh(broker, ledger, pages=2)
        # One root plus three retained parts reaches a full page after one new
        # part. Declared one-page acquisition cannot prove termination.
        for index in range(3):
            state.comments[2000 + index] = {"id": 2000 + index, "body": "human comment"}
        posts = len([call for call in state.calls if call[0] == "POST"])
        with self.assertRaises(ReviewInputError):
            self.activate(ledger, phase=1, pages=1)
        self.assertEqual(
            len([call for call in state.calls if call[0] == "POST"]), posts
        )


if __name__ == "__main__":
    unittest.main()
