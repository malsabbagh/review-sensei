"""One owned reservation/admission mutation; not CLI or live broker qualification."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from review_sensei_github.operation_host import (
    AdmissionProof,
    InMemoryOperationStore,
    OperationHost,
    OperationRequest,
    RestoredOperation,
    TransitionPlan,
)

from review_sensei.errors import ReviewInputError
from review_sensei.session import read_session_assessment_queue
from tests import test_activation_tail as tail
from tests import test_review_transaction as fixture
from tests.test_activation_tail import OPERATION, prepare_queue, seal_accounting


def admitted_handle(budget, operation, identity=fixture.IDENTITY):
    origin = budget.wall_deadline_ms - 60_000
    host = OperationHost(InMemoryOperationStore(), now=lambda: origin)
    proof = AdmissionProof(
        hashlib.sha256(
            f"{identity.repository_id}:{identity.pull_request}".encode()
        ).hexdigest(),
        operation["authority_digest"],
        operation["execution_identity"],
        "b" * 64,
        "c" * 64,
        True,
    )
    request = OperationRequest(
        "d" * 64,
        operation["operation_id"],
        operation["inventory_digest"],
        operation["source_digest"],
        "e" * 64,
        0,
        "reassessment",
    )
    handle = host.begin_operation(proof, request, origin, 1)
    plan = TransitionPlan(
        "f" * 64, 0, 59, 4, "e" * 64, 0, "1" * 64, 1, "2" * 64, "3" * 64
    )
    host.consume_transition(handle, plan, consume_grant=lambda: True)
    restored = host.restore_operation(proof, request)
    assert isinstance(restored, RestoredOperation)
    return restored.handle


class InitialAssessmentTailTests(unittest.TestCase):
    def setup_host(self, operation=OPERATION):
        runner = tail.HostedTailTests()
        handles = []

        def before_issue(initial, budget, broker):
            restored_handle = admitted_handle(budget, operation)
            handles.append(restored_handle)
            broker.request = tail.feedback_request(
                initial, budget, reason="admission-dispatch"
            )
            broker.request["mutation"].update(
                **operation,
                reservation_id=restored_handle.binding,
                request_digest="f" * 64,
                dispatch_digest="d" * 64,
            )

        state, broker, ledger = runner.setup_host(
            callback=True, before_issue=before_issue
        )
        return runner, state, broker, ledger, handles[0]

    def test_actual_engine_dispatch_has_complete_owned_admission_before_provider(self):
        from review_sensei_github.session_ledger import GitHubIssueCommentSessionLedger

        from review_sensei.assessment_queue import (
            AssessmentCheckpoint,
            AssessmentQueue,
            FactoredAssessmentJournal,
            pack_queue_state,
            unpack_queue_state,
        )
        from review_sensei.bounded_evidence import inline_manifest
        from review_sensei.budgets import ReviewWorkBudgets
        from review_sensei.evidence import EvidenceBundle, EvidenceSnapshot
        from review_sensei.human_assessment import PendingHumanReview
        from review_sensei.outcomes import ResourceBudget, ResourceBudgetTracker
        from review_sensei.reassessment_work import reassess
        from review_sensei.session import assessment_queue_manifest_capacity
        from tests.test_assessment_queue import fixture as queue_fixture
        from tests.test_authenticated_partitions import binding
        from tests.test_review_work import HUMAN, AssessingProvider

        pending, bundle, _ = queue_fixture(4)
        snapshot = EvidenceSnapshot(
            fixture.IDENTITY.repository,
            fixture.IDENTITY.pull_request,
            fixture.BASE_SHA,
            fixture.HEAD_SHA,
        )
        pending = PendingHumanReview(snapshot.base_sha, pending.findings)
        bundle = EvidenceBundle(
            snapshot,
            tuple(replace(record, snapshot=snapshot) for record in bundle.records),
        )
        queue = AssessmentQueue.create(pending, snapshot)
        tracker = ResourceBudgetTracker(ResourceBudget.create())
        operation = {
            **OPERATION,
            "inventory_digest": queue.inventory_digest,
            "execution_identity": tracker.execution_identity,
        }
        _, state, broker, initial_ledger, handle = self.setup_host(operation)
        budget = initial_ledger.evidence_budget
        accepted = []
        documents = []

        def activate(document, mutation):
            self.assertEqual(mutation.reason, "admission-dispatch")
            current = initial_ledger.load(fixture.IDENTITY, now=fixture.NOW).record
            broker.request = tail.feedback_request(
                current, budget, reason=mutation.reason
            )
            broker.request["mutation"].update(
                **operation,
                reservation_id=handle.binding,
                request_digest=mutation.request_digest,
                dispatch_digest=mutation.dispatch_digest,
            )
            grant = broker.issue(budget)
            ledger = GitHubIssueCommentSessionLedger(
                state.http,
                token=grant.token,
                app_slug="sensei[bot]",
                broker=broker.client,
                session_grant=grant.grant,
                session_attestation=grant.attestation,
                head_sha=fixture.HEAD_SHA,
                evidence_budget=budget,
                enable_partition_writes=True,
            )

            def prepare(reserved):
                journal = FactoredAssessmentJournal(queue).record(
                    queue=queue,
                    source_digest=operation["source_digest"],
                    receipt=document,
                )
                envelope = {
                    "schema_version": "1.0",
                    "kind": "assessment-queue-state",
                    "journal": journal.to_document(),
                }
                manifest = inline_manifest(
                    pack_queue_state(envelope),
                    binding=binding(
                        producer=ledger.evidence_producer(),
                        generation=0,
                        purpose="queue",
                    ),
                    item_count=4,
                )
                self.assertIsNotNone(manifest)
                self.assertLessEqual(
                    len(json.dumps(manifest).encode()),
                    assessment_queue_manifest_capacity(reserved),
                )
                active = {
                    **{
                        key: value
                        for key, value in operation.items()
                        if not key.startswith("inventory_")
                    },
                    **document["counters"],
                    "deadline_at": document["deadline_at"].replace("+00:00", "Z"),
                    "expires_at": document["expires_at"].replace("+00:00", "Z"),
                    "response_bytes_reserved": document["response_bytes_reserved"],
                    "read_accounting": {
                        "calls": budget.calls,
                        "deadline_at_ms": budget.wall_deadline_ms,
                    },
                }
                return reserved.evolve(
                    assessment_queue={
                        "schema_version": "1.0",
                        "inventory_digest": queue.inventory_digest,
                        "inventory_generation": 0,
                        "state_manifest": manifest,
                        "active_operation": active,
                    },
                    now=fixture.NOW,
                )

            retained = ledger.admit_with_tail(
                fixture.IDENTITY,
                prepare,
                slot="verification",
                expected_generation=0,
                operation_binding=operation,
                attempt_reservation_id=handle.binding,
                seal_accounting=seal_accounting,
                max_scan_pages=1,
                now=fixture.NOW,
                witness=handle,
            )
            documents.append(copy.deepcopy(document))
            accepted.append(retained)

        owner = self

        class InterruptedProvider(AssessingProvider):
            def complete(self, request):
                self.calls.append(request)
                observer = GitHubIssueCommentSessionLedger(
                    state.http,
                    token="synthetic",
                    app_slug="sensei[bot]",
                    evidence_budget=budget,
                    enable_partition_writes=True,
                )
                current = observer.load(fixture.IDENTITY, now=fixture.NOW).record
                envelope = unpack_queue_state(
                    read_session_assessment_queue(observer, current)
                )
                receipt = FactoredAssessmentJournal.from_document(
                    envelope["journal"]
                ).receipt(
                    source_digest=operation["source_digest"],
                    operation_id=operation["operation_id"],
                )
                owner.assertEqual(receipt, documents[0])
                owner.assertEqual(receipt["counters"]["provider_calls"], 1)
                owner.assertEqual(len(receipt["admitted_queue"]["inventory_ids"]), 4)
                owner.assertEqual(len(receipt["pending"]), 4)
                owner.assertEqual(current.reservation_id, handle.binding)
                raise KeyboardInterrupt()

        checkpoint = AssessmentCheckpoint(
            operation_id=operation["operation_id"],
            read=lambda: None,
            write=lambda _: self.fail("guarded callback required"),
            on_mutation=activate,
            root_generation=lambda: 0,
            coalesce_admission_dispatch=True,
        )
        provider = InterruptedProvider()
        with self.assertRaises(KeyboardInterrupt):
            reassess(
                pending=pending,
                bundle=bundle,
                queue=queue,
                source_body=HUMAN,
                authority_digest=operation["authority_digest"],
                provider=provider,
                tracker=tracker,
                work_budgets=ReviewWorkBudgets(mode="unified"),
                checkpoint=checkpoint,
            )
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(broker.used), 1)

    def admit(self, ledger, handle, *, prepare_hook=None, **overrides):
        prepare = prepare_queue(ledger)

        def prepare_reserved(reserved):
            # Reservation is a candidate, not a separately published generation.
            draft = prepare(reserved).evolve(
                generation=reserved.generation, now=fixture.NOW
            )
            if prepare_hook is not None:
                prepare_hook(reserved, draft)
            return draft

        arguments = dict(
            slot="verification",
            expected_generation=0,
            operation_binding=OPERATION,
            attempt_reservation_id=handle.binding,
            seal_accounting=seal_accounting,
            max_scan_pages=1,
            now=fixture.NOW,
            witness=handle,
        )
        arguments.update(overrides)
        return ledger.admit_with_tail(fixture.IDENTITY, prepare_reserved, **arguments)

    def test_one_consumed_grant_persists_reservation_and_complete_candidate(self):
        _, state, broker, ledger, handle = self.setup_host()
        current = self.admit(ledger, handle)
        self.assertEqual(current.generation, 1)
        self.assertEqual(current.reservation_id, handle.binding)
        self.assertEqual(len(broker.used), 1)
        self.assertEqual(sum(method == "PATCH" for method, _ in state.calls), 1)
        self.assertEqual(
            ledger.evidence_budget.calls, len(state.calls) + len(broker.calls)
        )
        observer = state.ledger()
        retained = observer.load(fixture.IDENTITY, now=fixture.NOW).record
        self.assertEqual(retained.record_sha256, current.record_sha256)
        self.assertEqual(read_session_assessment_queue(observer, retained)["phase"], 0)

    def test_missing_changed_or_unfunded_original_witness_refuses_before_consumption(
        self,
    ):
        for fault in ("mapping", "foreign-scope", "operation", "deadline", "unfunded"):
            with self.subTest(fault=fault):
                _, state, broker, ledger, handle = self.setup_host()
                witness = {
                    "mapping": {"binding": handle.binding},
                    "foreign-scope": replace(handle, scope_digest="a" * 64),
                    "operation": replace(handle, operation_id="a" * 64),
                    "deadline": replace(
                        handle, control_deadline_ms=handle.control_deadline_ms + 1
                    ),
                    "unfunded": replace(handle, calls=0),
                }[fault]
                before = copy.deepcopy(state.comments)
                with self.assertRaises(ReviewInputError):
                    self.admit(ledger, handle, witness=witness)
                self.assertFalse(broker.used)
                self.assertEqual(state.comments, before)

    def test_interruption_burns_original_attempt_without_root_or_provider_work(self):
        _, state, broker, ledger, handle = self.setup_host()
        before = copy.deepcopy(state.comments)

        def interrupt(*_):
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            self.admit(ledger, handle, prepare_hook=interrupt)
        calls = ledger.evidence_budget.calls
        self.assertEqual(len(broker.used), 1)
        self.assertEqual(state.comments, before)
        with self.assertRaises(ReviewInputError):
            self.admit(ledger, handle)
        self.assertEqual(ledger.evidence_budget.calls, calls)

    def test_expired_or_partly_unfunded_tail_never_writes_reservation(self):
        for fault in ("expired", "tail-liability", "generation"):
            with self.subTest(fault=fault):
                _, state, broker, ledger, handle = self.setup_host()
                before = copy.deepcopy(state.comments)
                values = {}
                if fault == "tail-liability":
                    values["witness"] = replace(handle, calls=6)
                if fault == "generation":
                    values["expected_generation"] = 1

                def expire(*_):
                    if fault == "expired":
                        ledger.evidence_budget.clock = lambda: (
                            ledger.evidence_budget.deadline
                        )

                with self.assertRaises(ReviewInputError):
                    self.admit(ledger, handle, prepare_hook=expire, **values)
                self.assertEqual(state.comments, before)
                self.assertFalse(any(method == "PATCH" for method, _ in state.calls))
                self.assertEqual(len(broker.used), 1)

    def test_changed_root_or_head_and_refused_consume_never_publish_candidate(self):
        for fault in ("root", "head", "consume"):
            with self.subTest(fault=fault):
                _, state, broker, ledger, handle = self.setup_host()
                before = copy.deepcopy(state.comments)
                if fault == "consume":
                    ledger._broker.verify_session_grant(
                        ledger._session_grant, ledger._session_attestation
                    )
                if fault == "head":
                    state.head_hook = lambda: setattr(state, "live_head", "c" * 40)

                def change_root(_reserved, _draft):
                    if fault == "root":
                        from review_sensei_github.session_ledger import (
                            parse_session_comment,
                            render_session_comment,
                        )

                        item = next(iter(state.comments.values()))
                        current = parse_session_comment(
                            item["body"], identity=fixture.IDENTITY
                        )
                        current = current.evolve(
                            generation=current.generation + 1,
                            operator_paused=True,
                            now=fixture.NOW,
                        )
                        item["body"] = render_session_comment(
                            repository_id=fixture.IDENTITY.repository_id,
                            pull_request=fixture.IDENTITY.pull_request,
                            record=current,
                        )
                        state.touch(item)

                with self.assertRaises(ReviewInputError):
                    self.admit(ledger, handle, prepare_hook=change_root)
                self.assertFalse(any(method == "PATCH" for method, _ in state.calls))
                if fault != "root":
                    self.assertEqual(state.comments, before)

    def test_lost_or_partial_patch_response_reconciles_owned_readback_once(self):
        for fault in ("lost", "partial"):
            with self.subTest(fault=fault):
                _, state, broker, ledger, handle = self.setup_host()
                original = state.http.opener

                def open_request(request, timeout):
                    response = original(request, timeout)
                    if request.method == "PATCH":
                        if fault == "lost":
                            raise TimeoutError("synthetic lost response")
                        from tests.fake_github_http import json_response

                        return json_response({})
                    return response

                with patch.object(state.http, "opener", side_effect=open_request):
                    current = self.admit(ledger, handle)
                self.assertEqual(current.generation, 1)
                calls = ledger.evidence_budget.calls
                with self.assertRaises(ReviewInputError):
                    self.admit(ledger, handle)
                self.assertEqual(ledger.evidence_budget.calls, calls)
                self.assertEqual(len(broker.used), 1)

    def test_absent_commit_or_incomplete_owned_readback_refuses_without_retry(self):
        for fault in ("absent-commit", "incomplete-readback", "missing-consumption"):
            with self.subTest(fault=fault):
                _, state, broker, ledger, handle = self.setup_host()
                original = state.http.opener
                patched = []

                def open_request(request, timeout):
                    if request.method == "PATCH":
                        patched.append(True)
                        if fault == "absent-commit":
                            raise TimeoutError("synthetic no commit")
                    if (
                        patched
                        and request.method == "GET"
                        and "/comments?" in request.full_url
                        and fault != "missing-consumption"
                    ):
                        from tests.fake_github_http import json_response

                        return json_response([])
                    response = original(request, timeout)
                    if request.method == "PATCH" and fault == "missing-consumption":
                        item = next(iter(state.comments.values()))
                        item["body"] = "\n".join(
                            line
                            for line in item["body"].splitlines()
                            if "reviewsensei:consumed-grant:v1" not in line
                        )
                    return response

                with patch.object(state.http, "opener", side_effect=open_request):
                    with self.assertRaises(ReviewInputError):
                        self.admit(ledger, handle)
                calls = ledger.evidence_budget.calls
                with self.assertRaises(ReviewInputError):
                    self.admit(ledger, handle)
                self.assertEqual(ledger.evidence_budget.calls, calls)
                self.assertEqual(len(patched), 1)
                self.assertEqual(len(broker.used), 1)
