"""Unit proof for the opt-in operation host. This is not a public 64/60 trace."""

from __future__ import annotations

import hashlib
import unittest
from collections.abc import Callable
from dataclasses import dataclass, replace

from review_sensei.hosting.github.operation_host import (
    CONTROL_DEADLINE_MS,
    ORDINARY_DISPATCH_LIMIT,
    TOTAL_DISPATCH_LIMIT,
    AcceptedPacket,
    Acknowledgement,
    AdmissionProof,
    ExecutionResult,
    InMemoryOperationStore,
    OperationHandle,
    OperationHost,
    OperationRefusal,
    OperationRequest,
    ProviderOutput,
    RestoredOperation,
    RootObservation,
    TransitionPermit,
    TransitionPlan,
)


def _hex(text: str, width: int = 64) -> str:
    if len(text) != 1:
        raise AssertionError("digest seed must be one character")
    return text * width


ROOT = _hex("3")
TARGET = _hex("5")
NEXT_ROOT = _hex("b")


def _proof() -> AdmissionProof:
    return AdmissionProof(
        scope_digest=_hex("a"),
        authority_digest=_hex("e"),
        execution_identity=_hex("f", 32),
        owner_digest=_hex("1"),
        reservation_digest=_hex("2"),
        server_authenticated=True,
    )


def _request() -> OperationRequest:
    return OperationRequest(
        event_id=_hex("b"),
        operation_id=_hex("c"),
        inventory_digest=_hex("0"),
        source_digest=_hex("d"),
        root_sha=ROOT,
        root_generation=0,
        producer_contract="existing-inventory-reassessment",
    )


def _plan(
    attempt: str = "4",
    *,
    sequence: int = 0,
    ordinary: int = 1,
    fence: int = 0,
    prior_sha: str = ROOT,
    prior_generation: int = 0,
    target_sha: str = TARGET,
    target_generation: int = 1,
    obligations: tuple[str, ...] = (),
) -> TransitionPlan:
    return TransitionPlan(
        attempt_id=_hex(attempt),
        sequence=sequence,
        ordinary_dispatches=ordinary,
        fence_dispatches=fence,
        prior_root_sha=prior_sha,
        prior_root_generation=prior_generation,
        target_root_sha=target_sha,
        target_root_generation=target_generation,
        request_digest=_hex("6"),
        dispatch_digest=_hex("7"),
        obligation_ids=obligations,
    )


def _output(body: bytes = b"known-output") -> ProviderOutput:
    return ProviderOutput(output=body, decisions_digest=_hex("d"))


def _accept(output: ProviderOutput, measured: int) -> bool:
    return measured == len(output.output)


@dataclass
class ScriptedRemote:
    observations: tuple[RootObservation, ...]
    lose_response: bool = False

    def __post_init__(self) -> None:
        self.ack_calls = 0
        self.commits = 0
        self.marker: str | None = None

    def read_root(self) -> tuple[RootObservation, ...]:
        return self.observations

    def read_acknowledgement(self, packet: AcceptedPacket) -> Acknowledgement | None:
        del packet
        if self.marker is None:
            return None
        return Acknowledgement(marker=self.marker, committed=True)

    def acknowledge(self, packet: AcceptedPacket, marker: str) -> Acknowledgement:
        del packet
        self.ack_calls += 1
        self.commits += 1
        self.marker = marker
        return Acknowledgement(
            marker=marker,
            committed=True,
            response_lost=self.lose_response,
        )


class OperationHostTests(unittest.TestCase):
    def _host(
        self,
        *,
        bootstrap: int = 1,
        started: int = 1_000,
        store: InMemoryOperationStore | None = None,
        request: OperationRequest | None = None,
    ) -> tuple[OperationHost, InMemoryOperationStore, AdmissionProof, OperationRequest, OperationHandle, dict[str, int]]:
        clock = {"now": started}
        backing = store if store is not None else InMemoryOperationStore()
        host = OperationHost(backing, now=lambda: clock["now"])
        proof = _proof()
        identity = request if request is not None else _request()
        admitted = host.begin_operation(proof, identity, started, bootstrap)
        self.assertIsInstance(admitted, OperationHandle)
        assert isinstance(admitted, OperationHandle)
        return host, backing, proof, identity, admitted, clock

    def _consume(
        self,
        host: OperationHost,
        handle: OperationHandle,
        plan: TransitionPlan,
        grant: Callable[[], bool] | None = None,
    ) -> TransitionPermit:
        permit = host.consume_transition(
            handle,
            plan,
            consume_grant=(lambda: True) if grant is None else grant,
        )
        self.assertIsInstance(permit, TransitionPermit)
        assert isinstance(permit, TransitionPermit)
        return permit

    def test_crash_after_accept_does_not_call_provider_again(self) -> None:
        host, store, proof, request, handle, clock = self._host()
        plan = _plan()
        calls = {"n": 0}

        def provider() -> ProviderOutput:
            calls["n"] += 1
            return _output()

        self._consume(host, handle, plan)
        first = host.execute_and_accept(handle, provider, _accept)
        self.assertEqual(first.status, "accepted")
        self.assertIsNotNone(first.packet)
        assert first.packet is not None
        self.assertEqual(calls["n"], 1)
        self.assertEqual(first.packet.known_output_bytes, len(b"known-output"))
        self.assertEqual(
            first.packet.payload_digest,
            hashlib.sha256(b"known-output").hexdigest(),
        )

        restored_host = OperationHost(store, now=lambda: clock["now"])
        restored = restored_host.restore_operation(proof, request)
        self.assertIsInstance(restored, RestoredOperation)
        assert isinstance(restored, RestoredOperation)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(restored.accepted_packet, first.packet)
        self.assertEqual(restored.calls, handle.calls + plan.ordinary_dispatches)
        replay = restored_host.execute_and_accept(restored.handle, provider, _accept)
        self.assertEqual(replay.status, "accepted")
        self.assertEqual(replay.packet, first.packet)
        self.assertEqual(calls["n"], 1)

    def test_lost_ack_reuses_marker_without_a_second_commit(self) -> None:
        host, store, proof, request, handle, clock = self._host()
        plan = _plan()
        calls = {"n": 0}

        def provider() -> ProviderOutput:
            calls["n"] += 1
            return _output()

        self._consume(host, handle, plan)
        accepted = host.execute_and_accept(handle, provider, _accept)
        self.assertEqual(accepted.status, "accepted")
        remote = ScriptedRemote(
            (
                RootObservation(
                    kind="read",
                    observed_at_ms=2_000,
                    root_sha=TARGET,
                    root_generation=1,
                ),
            ),
            lose_response=True,
        )
        first = host.publish_or_reconcile(handle, remote)
        self.assertEqual(first.status, "pending")
        self.assertNotEqual(first.status, "acknowledged")
        self.assertIsNone(first.ack_marker)
        self.assertEqual(remote.commits, 1)
        self.assertEqual(calls["n"], 1)

        restarted = OperationHost(store, now=lambda: clock["now"])
        restored = restarted.restore_operation(proof, request)
        assert isinstance(restored, RestoredOperation)
        replay = restarted.publish_or_reconcile(restored.handle, remote)
        self.assertEqual(replay.status, "reused")
        self.assertEqual(replay.ack_marker, remote.marker)
        self.assertEqual(remote.commits, 1)
        self.assertEqual(remote.ack_calls, 1)
        self.assertEqual(calls["n"], 1)
        again = restarted.execute_and_accept(restored.handle, provider, _accept)
        self.assertEqual(again.status, "accepted")
        self.assertEqual(calls["n"], 1)

    def test_kill_after_charge_before_accept_stays_unknown(self) -> None:
        host, store, _proof, request, handle, clock = self._host()
        proof = _proof
        plan = _plan()
        calls = {"n": 0}

        def provider() -> ProviderOutput:
            calls["n"] += 1
            raise RuntimeError("killed before accept")

        self._consume(host, handle, plan)
        first = host.execute_and_accept(handle, provider, _accept)
        self.assertEqual(first.status, "unknown")
        self.assertIsNone(first.packet)
        self.assertEqual(calls["n"], 1)

        restarted = OperationHost(store, now=lambda: clock["now"])
        restored = restarted.restore_operation(proof, request)
        assert isinstance(restored, RestoredOperation)
        self.assertIsNone(restored.accepted_packet)
        self.assertEqual(
            [(item.attempt_id, item.state) for item in restored.unknown_transitions],
            [(plan.attempt_id, "unknown")],
        )
        second = restarted.execute_and_accept(restored.handle, provider, _accept)
        self.assertEqual(second.status, "unknown")
        self.assertIsNone(second.packet)
        self.assertEqual(calls["n"], 1)
        refused = restarted.consume_transition(
            restored.handle,
            _plan(
                "9",
                sequence=1,
                prior_sha=ROOT,
                prior_generation=0,
                target_sha=NEXT_ROOT,
                target_generation=1,
            ),
            consume_grant=lambda: True,
        )
        self.assertIsInstance(refused, OperationRefusal)
        assert isinstance(refused, OperationRefusal)
        self.assertEqual(refused.reason, "unknown_reservation")
        retained = restarted.restore_operation(proof, request)
        assert isinstance(retained, RestoredOperation)
        self.assertEqual(retained.calls, restored.calls)
        self.assertEqual(retained.unknown_transitions, restored.unknown_transitions)

    def test_github_absence_while_inflight_does_not_redispatch(self) -> None:
        host, _store, proof, request, handle, _clock = self._host()
        plan = _plan()
        calls = {"n": 0}

        def provider() -> ProviderOutput:
            calls["n"] += 1
            return _output()

        permit = self._consume(host, handle, plan)
        remote = ScriptedRemote((RootObservation(kind="absent", observed_at_ms=2_000),))
        observed = host.publish_or_reconcile(handle, remote)
        self.assertEqual(observed.status, "disagreement")
        self.assertFalse(observed.writer_released)
        self.assertEqual(remote.ack_calls, 0)
        execution = host.execute_and_accept(handle, provider, _accept)
        self.assertEqual(execution.status, "refused")
        self.assertEqual(execution.reason, "disagreement")
        self.assertEqual(calls["n"], 0)
        refused = host.consume_transition(
            handle,
            _plan(
                "9",
                sequence=permit.sequence,
                prior_sha=ROOT,
                prior_generation=0,
                target_sha=NEXT_ROOT,
                target_generation=1,
            ),
            consume_grant=lambda: True,
        )
        self.assertIsInstance(refused, OperationRefusal)
        assert isinstance(refused, OperationRefusal)
        self.assertEqual(refused.reason, "unknown_reservation")
        restored = host.restore_operation(proof, request)
        assert isinstance(restored, RestoredOperation)
        self.assertEqual(
            [(item.attempt_id, item.state) for item in restored.unknown_transitions],
            [(plan.attempt_id, "unknown")],
        )
        self.assertEqual(restored.calls, permit.calls)
        self.assertEqual(remote.ack_calls, 0)

    def test_delayed_write_after_newer_read_stays_quarantined(self) -> None:
        host, _store, proof, request, handle, _clock = self._host()
        plan = _plan()
        self._consume(host, handle, plan)
        remote = ScriptedRemote(
            (
                RootObservation(
                    kind="read",
                    observed_at_ms=5_000,
                    root_sha=ROOT,
                    root_generation=0,
                ),
                RootObservation(
                    kind="write",
                    observed_at_ms=6_000,
                    root_sha=TARGET,
                    root_generation=1,
                    attempt_id=plan.attempt_id,
                    write_started_at_ms=1_000,
                ),
            )
        )
        observed = host.publish_or_reconcile(handle, remote)
        self.assertEqual(observed.status, "quarantine")
        self.assertFalse(observed.writer_released)
        self.assertEqual(remote.ack_calls, 0)
        later = host.publish_or_reconcile(
            handle,
            ScriptedRemote(
                (
                    RootObservation(
                        kind="read",
                        observed_at_ms=7_000,
                        root_sha=TARGET,
                        root_generation=1,
                    ),
                )
            ),
        )
        self.assertEqual(later.status, "quarantine")
        self.assertFalse(later.writer_released)
        refused = host.consume_transition(
            handle,
            _plan(
                "9",
                sequence=1,
                prior_sha=ROOT,
                prior_generation=0,
                target_sha=NEXT_ROOT,
                target_generation=1,
            ),
            consume_grant=lambda: True,
        )
        self.assertIsInstance(refused, OperationRefusal)
        assert isinstance(refused, OperationRefusal)
        self.assertEqual(refused.reason, "unknown_reservation")
        restored = host.restore_operation(proof, request)
        assert isinstance(restored, RestoredOperation)
        self.assertEqual(restored.unknown_transitions[0].state, "pending")
        self.assertEqual(restored.handle.root_sha, ROOT)
        self.assertEqual(restored.handle.root_generation, 0)

    def test_budget_and_deadline_refuse_without_refund(self) -> None:
        self.assertEqual(ORDINARY_DISPATCH_LIMIT, 60)
        self.assertEqual(TOTAL_DISPATCH_LIMIT, 64)
        self.assertEqual(CONTROL_DEADLINE_MS, 60_000)
        host, _store, proof, request, handle, _clock = self._host(bootstrap=60)
        grants = {"n": 0}

        def grant() -> bool:
            grants["n"] += 1
            return True

        ordinary = host.consume_transition(handle, _plan(ordinary=1, fence=0), consume_grant=grant)
        self.assertIsInstance(ordinary, OperationRefusal)
        assert isinstance(ordinary, OperationRefusal)
        self.assertEqual(ordinary.reason, "budget")
        self.assertEqual(grants["n"], 0)
        after_ordinary = host.restore_operation(proof, request)
        assert isinstance(after_ordinary, RestoredOperation)
        self.assertEqual(after_ordinary.calls, 60)

        filled = self._consume(host, handle, _plan(ordinary=0, fence=4), grant)
        self.assertTrue(filled.first_permit)
        self.assertEqual(filled.calls, 64)
        self.assertEqual(grants["n"], 1)
        calls = {"n": 0}

        def provider() -> ProviderOutput:
            calls["n"] += 1
            return _output(b"x")

        accepted = host.execute_and_accept(handle, provider, _accept)
        self.assertEqual(accepted.status, "accepted")
        self.assertEqual(calls["n"], 1)
        released = host.publish_or_reconcile(
            handle,
            ScriptedRemote(
                (
                    RootObservation(
                        kind="read",
                        observed_at_ms=2_000,
                        root_sha=TARGET,
                        root_generation=1,
                    ),
                )
            ),
        )
        self.assertTrue(released.writer_released)
        after_release = host.restore_operation(proof, request)
        assert isinstance(after_release, RestoredOperation)
        self.assertEqual(after_release.calls, 64)
        overflow = host.consume_transition(
            handle,
            _plan(
                "9",
                sequence=1,
                ordinary=0,
                fence=1,
                prior_sha=TARGET,
                prior_generation=1,
                target_sha=NEXT_ROOT,
                target_generation=2,
            ),
            consume_grant=grant,
        )
        self.assertIsInstance(overflow, OperationRefusal)
        assert isinstance(overflow, OperationRefusal)
        self.assertEqual(overflow.reason, "budget")
        self.assertEqual(grants["n"], 1)
        retained = host.restore_operation(proof, request)
        assert isinstance(retained, RestoredOperation)
        self.assertEqual(retained.calls, 64)
        self.assertEqual(retained.deadline_ms, 1_000 + CONTROL_DEADLINE_MS)

        deadline_host, _deadline_store, deadline_proof, deadline_request, deadline_handle, deadline_clock = (
            self._host(bootstrap=3, started=5_000)
        )
        deadline_clock["now"] = 5_000 + CONTROL_DEADLINE_MS
        expired = deadline_host.consume_transition(
            deadline_handle,
            _plan(),
            consume_grant=grant,
        )
        self.assertIsInstance(expired, OperationRefusal)
        assert isinstance(expired, OperationRefusal)
        self.assertEqual(expired.reason, "expired")
        self.assertEqual(grants["n"], 1)
        replay = deadline_host.begin_operation(
            deadline_proof,
            deadline_request,
            deadline_clock["now"],
            1,
        )
        self.assertIsInstance(replay, OperationHandle)
        assert isinstance(replay, OperationHandle)
        self.assertEqual(replay.calls, 3)
        self.assertEqual(replay.created_at_ms, 5_000)
        self.assertEqual(replay.control_deadline_ms, 5_000 + CONTROL_DEADLINE_MS)
        still = deadline_host.restore_operation(deadline_proof, deadline_request)
        assert isinstance(still, RestoredOperation)
        self.assertEqual(still.calls, 3)
        self.assertEqual(still.deadline_ms, replay.control_deadline_ms)

    def test_four_obligations_share_one_provider_call(self) -> None:
        host, _store, _proof, _request, handle, _clock = self._host()
        obligations = ("ob1", "ob2", "ob3", "ob4")
        plan = _plan(obligations=obligations)
        self._consume(host, handle, plan)
        calls = {"n": 0}

        def provider() -> ProviderOutput:
            calls["n"] += 1
            return _output(b"four")

        result = host.execute_and_accept(handle, provider, _accept)
        self.assertEqual(result.status, "accepted")
        self.assertEqual(calls["n"], 1)
        self.assertIsNotNone(result.packet)
        assert result.packet is not None
        self.assertEqual(result.packet.obligation_ids, obligations)
        self.assertEqual(len(set(result.packet.obligation_ids)), 4)
        self.assertEqual(result.packet.known_output_bytes, len(b"four"))
        self.assertEqual(result.packet.request_digest, plan.request_digest)

    def test_grant_callback_false_rolls_back_debit_and_consumption(self) -> None:
        host, store, proof, request, handle, _clock = self._host(bootstrap=3)
        store.insert_grant("grant-1")

        def reject() -> bool:
            self.assertTrue(store.consume_grant("grant-1"))
            return False

        refused = host.consume_transition(handle, _plan(), consume_grant=reject)
        self.assertIsInstance(refused, OperationRefusal)
        assert isinstance(refused, OperationRefusal)
        self.assertEqual(refused.reason, "grant_invalid")
        self.assertTrue(store.has_grant("grant-1"))
        restored = host.restore_operation(proof, request)
        assert isinstance(restored, RestoredOperation)
        self.assertEqual(restored.calls, 3)
        self.assertEqual(restored.unknown_transitions, ())

        def allow() -> bool:
            return store.consume_grant("grant-1")

        permit = self._consume(host, handle, _plan(), allow)
        self.assertTrue(permit.first_permit)
        self.assertFalse(store.has_grant("grant-1"))
        charged = host.restore_operation(proof, request)
        assert isinstance(charged, RestoredOperation)
        self.assertEqual(charged.calls, 4)

        def replay_grant() -> bool:
            raise AssertionError("replay must not consume a second permit")

        replay = host.consume_transition(handle, _plan(), consume_grant=replay_grant)
        self.assertIsInstance(replay, TransitionPermit)
        assert isinstance(replay, TransitionPermit)
        self.assertFalse(replay.first_permit)
        self.assertEqual(replay.state, "inflight")
        self.assertEqual(replay.calls, 4)

    def test_restore_does_not_mint_a_budget(self) -> None:
        store = InMemoryOperationStore()
        host = OperationHost(store, now=lambda: 1_000)
        proof, request = _proof(), _request()
        missing = host.restore_operation(proof, request)
        self.assertIsInstance(missing, OperationRefusal)
        assert isinstance(missing, OperationRefusal)
        self.assertEqual(missing.reason, "not_admitted")
        unauthenticated = host.begin_operation(None, request, 1_000, 1)
        self.assertIsInstance(unauthenticated, OperationRefusal)
        assert isinstance(unauthenticated, OperationRefusal)
        self.assertEqual(unauthenticated.reason, "unauthenticated")
        still_missing = host.restore_operation(proof, request)
        assert isinstance(still_missing, OperationRefusal)
        self.assertEqual(still_missing.reason, "not_admitted")

        handle = host.begin_operation(proof, request, 1_000, 4)
        assert isinstance(handle, OperationHandle)
        conflict = host.begin_operation(
            proof,
            replace(request, inventory_digest=_hex("9")),
            1_000,
            1,
        )
        self.assertIsInstance(conflict, OperationRefusal)
        assert isinstance(conflict, OperationRefusal)
        self.assertEqual(conflict.reason, "binding_conflict")
        restored = host.restore_operation(proof, request)
        assert isinstance(restored, RestoredOperation)
        self.assertEqual(restored.calls, 4)
        self.assertEqual(restored.deadline_ms, handle.control_deadline_ms)
        self.assertIsNone(restored.accepted_packet)

    def test_unknown_root_patch_refuses_new_inference(self) -> None:
        host, _store, proof, request, handle, _clock = self._host()
        plan = _plan()
        self._consume(host, handle, plan)
        observed = host.publish_or_reconcile(
            handle,
            ScriptedRemote((RootObservation(kind="unknown", observed_at_ms=2_000),)),
        )
        self.assertEqual(observed.status, "pending")
        self.assertFalse(observed.writer_released)
        refused = host.consume_transition(
            handle,
            _plan(
                "9",
                sequence=1,
                prior_sha=ROOT,
                prior_generation=0,
                target_sha=NEXT_ROOT,
                target_generation=1,
            ),
            consume_grant=lambda: True,
        )
        self.assertIsInstance(refused, OperationRefusal)
        assert isinstance(refused, OperationRefusal)
        self.assertEqual(refused.reason, "unknown_reservation")
        restored = host.restore_operation(proof, request)
        assert isinstance(restored, RestoredOperation)
        self.assertEqual(restored.unknown_transitions[0].state, "unknown")

    def test_closed_records_reject_extra_fields(self) -> None:
        with self.assertRaises(TypeError):
            OperationRequest(
                event_id=_hex("b"),
                operation_id=_hex("c"),
                inventory_digest=_hex("0"),
                source_digest=_hex("d"),
                root_sha=ROOT,
                root_generation=0,
                producer_contract="existing-inventory-reassessment",
                surplus="no",  # type: ignore[call-arg]
            )
        request = _request()
        with self.assertRaises(AttributeError):
            request.event_id = _hex("a")  # type: ignore[misc]
        with self.assertRaises(TypeError):
            AcceptedPacket(
                decisions_digest=_hex("d"),
                known_output_bytes=1,
                payload_digest=_hex("e"),
                request_digest=_hex("6"),
                extra=True,  # type: ignore[call-arg]
            )
        with self.assertRaises(ValueError):
            _plan(obligations=("ob1", "ob1", "ob3", "ob4"))
        result = ExecutionResult("accepted", None)
        self.assertEqual(result.status, "accepted")
