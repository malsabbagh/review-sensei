"""Opt-in shared host for admission, restore, acceptance, and reconciliation.

``run_review_trigger`` is the one entry for full review, reply, reassessment,
and verify. Callers that opt in must present a server-authenticated admission
proof. A fresh budget, a raw snapshot, or a consumed grant cannot restore an
operation or mint another allowance.

The public record is one App-owned GitHub issue comment of digests, counters,
and the accepted packet. It does not hold prompts, model text, or file bytes.
The worker authenticates the App so it can write that comment. It does not
store the review. ``InMemoryOperationStore`` is the transactional stand-in
inside one process. ``GitHubCommentOperationStore`` reloads that record from
the comment. Durable Object SQL is not this store. Debit and grant consumption
commit or roll back together inside this store when the grant callback returns
false. This host does not claim cross-store atomicity with a provider: a
rollback after an observed external effect must not reopen dispatch.

Ordinary dispatches stay 60, total dispatches stay 64, and the control
deadline stays 60 seconds. Nothing in this module refunds a committed charge.
The public 64/60 GitHub application profile is not measured here.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Protocol

ORDINARY_DISPATCH_LIMIT = 60
TOTAL_DISPATCH_LIMIT = 64
CONTROL_DEADLINE_MS = 60_000
MAX_FENCE_DISPATCHES = 4
MAX_OBLIGATIONS = 4
MAX_SCOPES = 1_024
MAX_EVENTS_PER_SCOPE = 256
MAX_TRANSITIONS_PER_EVENT = 64
MAX_RETAINED_TRANSITIONS = 16_384
_MAX_GENERATION = 2_147_483_647
_HELD = frozenset({"inflight", "unknown", "pending"})
_HEX64 = re.compile(r"^[a-f0-9]{64}$")
_HEX32 = re.compile(r"^[a-f0-9]{32}$")
_CONTRACT = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_OBLIGATION = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_GRANT = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _as_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("invalid stored integer")
    return value


def _as_ids(value: object) -> tuple[str, ...]:
    if isinstance(value, tuple) and all(isinstance(item, str) for item in value):
        return value
    raise ValueError("invalid stored obligation ids")


def _text(value: object, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"invalid {label}")
    return value


def _whole(value: object, maximum: int, label: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= maximum
    ):
        raise ValueError(f"invalid {label}")
    return value


@dataclass(frozen=True, slots=True)
class AdmissionProof:
    """Server-issued original admission witness. A caller nonce is not proof."""

    scope_digest: str
    authority_digest: str
    execution_identity: str
    owner_digest: str
    reservation_digest: str
    server_authenticated: bool

    def __post_init__(self) -> None:
        _text(self.scope_digest, _HEX64, "scope digest")
        _text(self.authority_digest, _HEX64, "authority digest")
        _text(self.execution_identity, _HEX32, "execution identity")
        _text(self.owner_digest, _HEX64, "owner digest")
        _text(self.reservation_digest, _HEX64, "reservation digest")
        if not isinstance(self.server_authenticated, bool):
            raise ValueError("invalid admission proof")


@dataclass(frozen=True, slots=True)
class OperationRequest:
    """Closed identity of one stable event. Extra fields are rejected."""

    event_id: str
    operation_id: str
    inventory_digest: str
    source_digest: str
    root_sha: str
    root_generation: int
    producer_contract: str

    def __post_init__(self) -> None:
        _text(self.event_id, _HEX64, "event id")
        _text(self.operation_id, _HEX64, "operation id")
        _text(self.inventory_digest, _HEX64, "inventory digest")
        _text(self.source_digest, _HEX64, "source digest")
        _text(self.root_sha, _HEX64, "root sha")
        _whole(self.root_generation, _MAX_GENERATION, "root generation")
        if (
            not isinstance(self.producer_contract, str)
            or _CONTRACT.fullmatch(self.producer_contract) is None
        ):
            raise ValueError("invalid producer contract")


@dataclass(frozen=True, slots=True)
class AcceptedPacket:
    """App-owned acceptance record. Acknowledgement is not this record."""

    decisions_digest: str
    known_output_bytes: int
    payload_digest: str
    request_digest: str
    obligation_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.decisions_digest, _HEX64, "decisions digest")
        _whole(self.known_output_bytes, 1_048_576, "known output bytes")
        _text(self.payload_digest, _HEX64, "payload digest")
        _text(self.request_digest, _HEX64, "request digest")
        _obligations(self.obligation_ids)


@dataclass(frozen=True, slots=True)
class TransitionPlan:
    """One prepaid transition. Obligation ids are accepted together."""

    attempt_id: str
    sequence: int
    ordinary_dispatches: int
    fence_dispatches: int
    prior_root_sha: str
    prior_root_generation: int
    target_root_sha: str
    target_root_generation: int
    request_digest: str
    dispatch_digest: str
    obligation_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.attempt_id, _HEX64, "attempt id")
        _whole(self.sequence, MAX_TRANSITIONS_PER_EVENT - 1, "sequence")
        _whole(self.ordinary_dispatches, ORDINARY_DISPATCH_LIMIT, "ordinary dispatches")
        _whole(self.fence_dispatches, MAX_FENCE_DISPATCHES, "fence dispatches")
        if self.ordinary_dispatches + self.fence_dispatches == 0:
            raise ValueError("invalid dispatch liability")
        _text(self.prior_root_sha, _HEX64, "prior root")
        _text(self.target_root_sha, _HEX64, "target root")
        prior = _whole(
            self.prior_root_generation, _MAX_GENERATION - 1, "prior root generation"
        )
        target = _whole(
            self.target_root_generation, _MAX_GENERATION, "target root generation"
        )
        if target != prior + 1:
            raise ValueError("invalid root generation")
        _text(self.request_digest, _HEX64, "request digest")
        _text(self.dispatch_digest, _HEX64, "dispatch digest")
        _obligations(self.obligation_ids)


@dataclass(frozen=True, slots=True)
class ProviderOutput:
    """Measured provider body. The host hashes these bytes; the model does not."""

    output: bytes
    decisions_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.output, bytes):
            raise ValueError("invalid provider output")
        _text(self.decisions_digest, _HEX64, "decisions digest")


@dataclass(frozen=True, slots=True)
class RootObservation:
    """One remote read, delayed write, absence, or unknown root effect."""

    kind: str
    observed_at_ms: int
    root_sha: str | None = None
    root_generation: int | None = None
    attempt_id: str | None = None
    write_started_at_ms: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"read", "write", "absent", "unknown"}:
            raise ValueError("invalid root observation")
        _whole(self.observed_at_ms, _MAX_GENERATION, "observed at")
        if self.root_sha is not None:
            _text(self.root_sha, _HEX64, "observed root")
        if self.root_generation is not None:
            _whole(self.root_generation, _MAX_GENERATION, "observed generation")
        if self.attempt_id is not None:
            _text(self.attempt_id, _HEX64, "observed attempt")
        if self.write_started_at_ms is not None:
            _whole(self.write_started_at_ms, _MAX_GENERATION, "write started at")
        if self.kind == "absent" and (
            self.root_sha is not None or self.root_generation is not None
        ):
            raise ValueError("invalid absence")
        if self.kind == "read" and (
            self.root_sha is None or self.root_generation is None
        ):
            raise ValueError("invalid root read")
        if self.kind == "write" and (
            self.root_sha is None
            or self.root_generation is None
            or self.write_started_at_ms is None
        ):
            raise ValueError("invalid root write")


@dataclass(frozen=True, slots=True)
class Acknowledgement:
    """Projection of a source acknowledgement. It is not the acceptance record."""

    marker: str
    committed: bool
    response_lost: bool = False
    unknown: bool = False

    def __post_init__(self) -> None:
        _text(self.marker, _HEX64, "acknowledgement marker")
        if not isinstance(self.committed, bool) or not isinstance(
            self.response_lost, bool
        ):
            raise ValueError("invalid acknowledgement")
        if not isinstance(self.unknown, bool):
            raise ValueError("invalid acknowledgement")


@dataclass(frozen=True, slots=True)
class OperationHandle:
    """Identity-bound handle. Counters on a stale handle are not authority."""

    scope_digest: str
    event_id: str
    operation_id: str
    binding: str
    created_at_ms: int
    control_deadline_ms: int
    calls: int
    sequence: int
    root_sha: str
    root_generation: int


@dataclass(frozen=True, slots=True)
class OperationRefusal:
    """Closed refusal. No partial permit is attached."""

    reason: str


@dataclass(frozen=True, slots=True)
class RetainedTransition:
    """A writer reservation that restore must not clear."""

    attempt_id: str
    state: str


@dataclass(frozen=True, slots=True)
class RestoredOperation:
    """Retained counters and deadline. This object is not a new budget."""

    handle: OperationHandle
    calls: int
    deadline_ms: int
    accepted_packet: AcceptedPacket | None
    unknown_transitions: tuple[RetainedTransition, ...]


@dataclass(frozen=True, slots=True)
class TransitionPermit:
    """First consumption, or an exact replay that does not consume again."""

    attempt_id: str
    first_permit: bool
    state: str
    calls: int
    sequence: int


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Provider execution after a prepaid charge. Unknown is not a retry."""

    status: str
    packet: AcceptedPacket | None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """Publication reconciliation. A lost ack is not success."""

    status: str
    writer_released: bool
    ack_marker: str | None = None
    reason: str | None = None
    accepted_packet: AcceptedPacket | None = None


class OperationRemote(Protocol):
    """Remote root and acknowledgement surface. Implementations are not atomic with SQL."""

    def read_root(self) -> tuple[RootObservation, ...]: ...

    def read_acknowledgement(
        self, packet: AcceptedPacket
    ) -> Acknowledgement | None: ...

    def acknowledge(self, packet: AcceptedPacket, marker: str) -> Acknowledgement: ...


class _Transaction:
    def __init__(self, store: InMemoryOperationStore) -> None:
        self._store = store
        self.finished = False

    def commit(self) -> None:
        if self.finished:
            raise RuntimeError("operation store transaction already finished")
        self._store._publish()
        self.finished = True

    def rollback(self) -> None:
        if self.finished:
            raise RuntimeError("operation store transaction already finished")
        self._store._discard()
        self.finished = True


class InMemoryOperationStore:
    """Copy-on-write store. Grant consumption must use the open transaction.

    A callback that returns false rolls the debit and the grant consumption
    back together. External writes made outside this store are not rolled back.
    """

    def __init__(self) -> None:
        self._committed: dict[str, dict[object, object]] = {
            "events": {},
            "transitions": {},
            "grants": {},
            "scopes": {},
        }
        self._txn: dict[str, dict[object, object]] | None = None
        self._open = False

    def _live(self) -> dict[str, dict[object, object]]:
        if self._open:
            assert self._txn is not None
            return self._txn
        return self._committed

    def _publish(self) -> None:
        assert self._txn is not None
        self._committed = self._txn
        self._txn = None
        self._open = False

    def _discard(self) -> None:
        self._txn = None
        self._open = False

    @contextmanager
    def transaction(self) -> Iterator[_Transaction]:
        if self._open:
            raise RuntimeError("operation store transactions do not nest")
        self._txn = copy.deepcopy(self._committed)
        self._open = True
        token = _Transaction(self)
        try:
            yield token
        except BaseException:
            if not token.finished:
                self._discard()
            raise
        finally:
            if not token.finished:
                self._discard()

    def insert_grant(self, grant_id: str) -> None:
        if not isinstance(grant_id, str) or _GRANT.fullmatch(grant_id) is None:
            raise ValueError("invalid grant")
        grants = self._live()["grants"]
        grants[grant_id] = True

    def consume_grant(self, grant_id: str) -> bool:
        if not isinstance(grant_id, str) or _GRANT.fullmatch(grant_id) is None:
            return False
        grants = self._live()["grants"]
        if grants.get(grant_id) is not True:
            return False
        del grants[grant_id]
        return True

    def has_grant(self, grant_id: str) -> bool:
        return self._committed["grants"].get(grant_id) is True


def _obligations(value: object) -> tuple[str, ...]:
    if not isinstance(value, tuple) or len(value) > MAX_OBLIGATIONS:
        raise ValueError("invalid obligation ids")
    seen: set[str] = set()
    for item in value:
        if (
            not isinstance(item, str)
            or _OBLIGATION.fullmatch(item) is None
            or item in seen
        ):
            raise ValueError("invalid obligation ids")
        seen.add(item)
    return value


def _binding(proof: AdmissionProof, request: OperationRequest) -> str:
    material = "\n".join(
        (
            proof.scope_digest,
            proof.authority_digest,
            proof.execution_identity,
            proof.owner_digest,
            proof.reservation_digest,
            request.event_id,
            request.operation_id,
            request.inventory_digest,
            request.source_digest,
            request.root_sha,
            str(request.root_generation),
            request.producer_contract,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _plan_key(plan: TransitionPlan) -> str:
    return "|".join(
        (
            plan.attempt_id,
            str(plan.sequence),
            str(plan.ordinary_dispatches),
            str(plan.fence_dispatches),
            plan.prior_root_sha,
            str(plan.prior_root_generation),
            plan.target_root_sha,
            str(plan.target_root_generation),
            plan.request_digest,
            plan.dispatch_digest,
            ",".join(plan.obligation_ids),
        )
    )


class OperationHost:
    """Admission, restore, provider acceptance, and publication reconciliation.

    The dispatch ceilings are the dormant journal ceilings. Reaching them
    refuses the next debit. Expiry refuses mutation and does not refund.
    """

    def __init__(
        self,
        store: InMemoryOperationStore | None = None,
        *,
        now: Callable[[], int] | None = None,
    ) -> None:
        self.store = store if store is not None else InMemoryOperationStore()
        self._now = now if now is not None else _wall_now

    def begin_operation(
        self,
        proof: AdmissionProof | None,
        request: OperationRequest,
        server_started_ms: int,
        bootstrap_dispatches: int,
    ) -> OperationHandle | OperationRefusal:
        """Admit a sticky event, or return the original allowance unchanged."""

        if proof is None or not isinstance(proof, AdmissionProof):
            return OperationRefusal("unauthenticated")
        if not proof.server_authenticated:
            return OperationRefusal("unauthenticated")
        if not isinstance(request, OperationRequest):
            return OperationRefusal("invalid")
        now = self._clock()
        if now is None:
            return OperationRefusal("invalid")
        if (
            isinstance(server_started_ms, bool)
            or not isinstance(server_started_ms, int)
            or not 0 <= server_started_ms <= now
        ):
            return OperationRefusal("clock_origin")
        if (
            isinstance(bootstrap_dispatches, bool)
            or not isinstance(bootstrap_dispatches, int)
            or not 1 <= bootstrap_dispatches <= ORDINARY_DISPATCH_LIMIT
        ):
            return OperationRefusal("invalid")
        binding = _binding(proof, request)
        key = (proof.scope_digest, request.event_id)
        with self.store.transaction() as txn:
            events = self.store._live()["events"]
            existing = events.get(key)
            if isinstance(existing, dict):
                if existing["binding"] != binding:
                    txn.rollback()
                    return OperationRefusal("binding_conflict")
                txn.rollback()
                return _handle(existing)
            scopes = self.store._live()["scopes"]
            scope = scopes.get(proof.scope_digest)
            if isinstance(scope, dict) and (
                scope["root_sha"] != request.root_sha
                or scope["root_generation"] != request.root_generation
            ):
                txn.rollback()
                return OperationRefusal("root_conflict")
            if scope is None and len(scopes) >= MAX_SCOPES:
                txn.rollback()
                return OperationRefusal("capacity")
            scope_events = [
                item
                for item in events.values()
                if isinstance(item, dict) and item["scope_digest"] == proof.scope_digest
            ]
            if len(scope_events) >= MAX_EVENTS_PER_SCOPE:
                txn.rollback()
                return OperationRefusal("capacity")
            if scope is None:
                scopes[proof.scope_digest] = {
                    "root_sha": request.root_sha,
                    "root_generation": request.root_generation,
                }
            record = {
                "scope_digest": proof.scope_digest,
                "event_id": request.event_id,
                "operation_id": request.operation_id,
                "binding": binding,
                "created_at_ms": server_started_ms,
                "control_deadline_ms": server_started_ms + CONTROL_DEADLINE_MS,
                "calls": bootstrap_dispatches,
                "sequence": 0,
                "root_sha": request.root_sha,
                "root_generation": request.root_generation,
                "observed_at_ms": now,
                "expired_observed": False,
                "accepted": None,
                "provider_charged": False,
                "provider_unknown": False,
                "inference_blocked": False,
                "quarantine": False,
                "ack_attempted": False,
            }
            events[key] = record
            txn.commit()
            return _handle(record)

    def restore_operation(
        self,
        proof: AdmissionProof | None,
        request: OperationRequest,
    ) -> RestoredOperation | OperationRefusal:
        """Return retained state. Never inserts an event or refreshes a budget."""

        if (
            proof is None
            or not isinstance(proof, AdmissionProof)
            or not proof.server_authenticated
        ):
            return OperationRefusal("unauthenticated")
        if not isinstance(request, OperationRequest):
            return OperationRefusal("invalid")
        event = self._event(proof.scope_digest, request.event_id)
        if event is None:
            return OperationRefusal("not_admitted")
        if event["binding"] != _binding(proof, request):
            return OperationRefusal("binding_conflict")
        return RestoredOperation(
            handle=_handle(event),
            calls=_as_int(event["calls"]),
            deadline_ms=_as_int(event["control_deadline_ms"]),
            accepted_packet=_packet(event),
            unknown_transitions=self._unknown(proof.scope_digest, request.event_id),
        )

    def consume_transition(
        self,
        handle: OperationHandle,
        plan: TransitionPlan,
        *,
        consume_grant: Callable[[], bool],
    ) -> TransitionPermit | OperationRefusal:
        """Debit the declared liability and consume the grant in one transaction."""

        if not isinstance(handle, OperationHandle) or not isinstance(
            plan, TransitionPlan
        ):
            return OperationRefusal("invalid")
        if not callable(consume_grant):
            return OperationRefusal("invalid")
        encoded = _plan_key(plan)
        replay = self._matching(handle, plan.attempt_id, encoded)
        if isinstance(replay, OperationRefusal):
            return replay
        if isinstance(replay, TransitionPermit):
            return replay
        now = self._clock()
        if now is None:
            return OperationRefusal("invalid")
        with self.store.transaction() as txn:
            event = self._event(handle.scope_digest, handle.event_id)
            if event is None or event["binding"] != handle.binding:
                txn.rollback()
                return OperationRefusal("invalid")
            if now < _as_int(event["created_at_ms"]) or now < _as_int(
                event["observed_at_ms"]
            ):
                txn.rollback()
                return OperationRefusal("clock_rollback")
            if bool(event["expired_observed"]) or now >= _as_int(
                event["control_deadline_ms"]
            ):
                event["observed_at_ms"] = max(_as_int(event["observed_at_ms"]), now)
                event["expired_observed"] = True
                txn.commit()
                return OperationRefusal("expired")
            if _as_int(event["sequence"]) != plan.sequence:
                txn.rollback()
                return OperationRefusal("sequence_conflict")
            if (
                event["root_sha"] != plan.prior_root_sha
                or _as_int(event["root_generation"]) != plan.prior_root_generation
            ):
                txn.rollback()
                return OperationRefusal("root_conflict")
            if self._scope_held(handle.scope_digest):
                txn.rollback()
                return OperationRefusal("unknown_reservation")
            transitions = self.store._live()["transitions"]
            event_count = sum(
                1
                for item in transitions.values()
                if isinstance(item, dict)
                and item["scope_digest"] == handle.scope_digest
                and item["event_id"] == handle.event_id
            )
            if (
                event_count >= MAX_TRANSITIONS_PER_EVENT
                or len(transitions) >= MAX_RETAINED_TRANSITIONS
            ):
                txn.rollback()
                return OperationRefusal("capacity")
            projected_ordinary = _as_int(event["calls"]) + plan.ordinary_dispatches
            projected_total = projected_ordinary + plan.fence_dispatches
            if (
                projected_ordinary > ORDINARY_DISPATCH_LIMIT
                or projected_total > TOTAL_DISPATCH_LIMIT
            ):
                txn.rollback()
                return OperationRefusal("budget")
            event["calls"] = projected_total
            event["sequence"] = _as_int(event["sequence"]) + 1
            event["observed_at_ms"] = now
            transitions[(handle.scope_digest, handle.event_id, plan.attempt_id)] = {
                "scope_digest": handle.scope_digest,
                "event_id": handle.event_id,
                "attempt_id": plan.attempt_id,
                "encoded": encoded,
                "state": "inflight",
                "sequence": plan.sequence,
                "ordinary": plan.ordinary_dispatches,
                "fence": plan.fence_dispatches,
                "prior_root_sha": plan.prior_root_sha,
                "prior_root_generation": plan.prior_root_generation,
                "target_root_sha": plan.target_root_sha,
                "target_root_generation": plan.target_root_generation,
                "request_digest": plan.request_digest,
                "dispatch_digest": plan.dispatch_digest,
                "obligation_ids": plan.obligation_ids,
            }
            if consume_grant() is not True:
                txn.rollback()
                return OperationRefusal("grant_invalid")
            txn.commit()
            return TransitionPermit(
                attempt_id=plan.attempt_id,
                first_permit=True,
                state="inflight",
                calls=projected_total,
                sequence=plan.sequence + 1,
            )

    def execute_and_accept(
        self,
        handle: OperationHandle,
        provider_call: Callable[[], ProviderOutput],
        validator: Callable[[ProviderOutput, int], bool],
    ) -> ExecutionResult:
        """Charge the provider attempt, then call the provider, then accept bytes.

        A second host bound to the same store sees a committed charge even when
        this process stops before the packet commit. That replay does not call
        the provider again.
        """

        if not isinstance(handle, OperationHandle) or not callable(provider_call):
            return ExecutionResult("refused", None, "invalid")
        if not callable(validator):
            return ExecutionResult("refused", None, "invalid")
        event = self._bound(handle)
        if isinstance(event, OperationRefusal):
            return ExecutionResult("refused", None, event.reason)
        packet = _packet(event)
        if packet is not None:
            readback = self._readback(handle)
            if readback != packet:
                return ExecutionResult("refused", None, "readback_failed")
            return ExecutionResult("accepted", readback, None)
        if bool(event["provider_charged"]) or bool(event["provider_unknown"]):
            return ExecutionResult("unknown", None, "provider_unknown")
        if bool(event["inference_blocked"]) or bool(event["quarantine"]):
            return ExecutionResult("refused", None, "disagreement")
        held = self._held(handle.scope_digest, handle.event_id)
        if held is None:
            return ExecutionResult("refused", None, "not_reserved")
        now = self._clock()
        if now is None:
            return ExecutionResult("refused", None, "invalid")
        if bool(event["expired_observed"]) or now >= _as_int(
            event["control_deadline_ms"]
        ):
            return ExecutionResult("refused", None, "expired")
        if not self._charge_provider(handle, str(held["attempt_id"])):
            return ExecutionResult("unknown", None, "provider_unknown")
        try:
            output = provider_call()
        except Exception:
            return ExecutionResult("unknown", None, "provider_unknown")
        if not isinstance(output, ProviderOutput):
            return ExecutionResult("unknown", None, "provider_unknown")
        measured = len(output.output)
        try:
            valid = validator(output, measured)
        except Exception:
            return ExecutionResult("unknown", None, "provider_unknown")
        if valid is not True:
            return ExecutionResult("unknown", None, "provider_unknown")
        try:
            accepted = AcceptedPacket(
                decisions_digest=output.decisions_digest,
                known_output_bytes=measured,
                payload_digest=hashlib.sha256(output.output).hexdigest(),
                request_digest=str(held["request_digest"]),
                obligation_ids=_as_ids(held["obligation_ids"]),
            )
        except ValueError:
            return ExecutionResult("unknown", None, "provider_unknown")
        stored = self._commit_packet(
            handle, str(held["attempt_id"]), accepted, measured
        )
        if stored is None:
            return ExecutionResult("unknown", None, "provider_unknown")
        return ExecutionResult("accepted", stored, None)

    def publish_or_reconcile(
        self,
        handle: OperationHandle,
        remote: OperationRemote,
    ) -> Reconciliation:
        """Read the packet back, reconcile the root, then acknowledge.

        Acknowledgement is not attempted until packet readback succeeds and the
        writer is released by an exact owned read. A lost ack response stays
        pending. Replay reuses a committed marker and does not commit it again.
        Absence and a delayed write do not release the writer.
        """

        if not isinstance(handle, OperationHandle):
            return _recon("refused", False, reason="invalid")
        event = self._bound(handle)
        if isinstance(event, OperationRefusal):
            return _recon("refused", False, reason=event.reason)
        try:
            observations = remote.read_root()
        except Exception:
            return _recon("pending", False, reason="remote_unknown")
        if not isinstance(observations, tuple) or any(
            not isinstance(item, RootObservation) for item in observations
        ):
            return _recon("refused", False, reason="invalid")
        packet = self._readback(handle)
        observed = self._observe(handle, observations, packet is not None)
        if isinstance(observed, OperationRefusal):
            return _recon("refused", False, reason=observed.reason)
        status, released = observed
        if packet is None or not released:
            return _recon(status, False, packet=packet)
        return self._acknowledge(handle, remote, packet, released)

    def _acknowledge(
        self,
        handle: OperationHandle,
        remote: OperationRemote,
        packet: AcceptedPacket,
        released: bool,
    ) -> Reconciliation:
        marker = _ack_marker(handle, packet)
        try:
            seen = remote.read_acknowledgement(packet)
        except Exception:
            return _recon("pending", released, reason="ack_unknown", packet=packet)
        if (
            isinstance(seen, Acknowledgement)
            and seen.committed
            and not seen.unknown
            and seen.marker == marker
        ):
            return _recon("reused", released, ack_marker=marker, packet=packet)
        event = self._bound(handle)
        if isinstance(event, OperationRefusal):
            return _recon("refused", released, reason=event.reason, packet=packet)
        if bool(event["ack_attempted"]):
            return _recon("pending", released, reason="ack_unknown", packet=packet)
        if not self._mark_ack_attempt(handle):
            return _recon("pending", released, reason="ack_unknown", packet=packet)
        try:
            outcome = remote.acknowledge(packet, marker)
        except Exception:
            return _recon("pending", released, reason="ack_unknown", packet=packet)
        if (
            not isinstance(outcome, Acknowledgement)
            or outcome.response_lost
            or outcome.unknown
            or not outcome.committed
            or outcome.marker != marker
        ):
            return _recon("pending", released, reason="ack_unknown", packet=packet)
        return _recon("acknowledged", released, ack_marker=marker, packet=packet)

    def _charge_provider(self, handle: OperationHandle, attempt_id: str) -> bool:
        with self.store.transaction() as txn:
            event = self._event(handle.scope_digest, handle.event_id)
            transition = self._transition(
                handle.scope_digest, handle.event_id, attempt_id
            )
            if (
                event is None
                or transition is None
                or event["binding"] != handle.binding
            ):
                txn.rollback()
                return False
            if event["accepted"] is not None or bool(event["provider_charged"]):
                txn.rollback()
                return False
            event["provider_charged"] = True
            event["provider_unknown"] = True
            transition["state"] = "unknown"
            txn.commit()
            return True

    def _commit_packet(
        self,
        handle: OperationHandle,
        attempt_id: str,
        packet: AcceptedPacket,
        measured: int,
    ) -> AcceptedPacket | None:
        with self.store.transaction() as txn:
            event = self._event(handle.scope_digest, handle.event_id)
            transition = self._transition(
                handle.scope_digest, handle.event_id, attempt_id
            )
            if event is None or transition is None:
                txn.rollback()
                return None
            if packet.known_output_bytes != measured:
                txn.rollback()
                return None
            current = _packet(event)
            if current is not None:
                txn.rollback()
                return current if current == packet else None
            event["accepted"] = packet
            event["provider_unknown"] = False
            if transition["state"] == "unknown":
                transition["state"] = "inflight"
            readback = _packet(event)
            if (
                readback != packet
                or readback is None
                or readback.known_output_bytes != measured
            ):
                txn.rollback()
                return None
            txn.commit()
        stored = self._event(handle.scope_digest, handle.event_id)
        if stored is None:
            return None
        return _packet(stored)

    def _observe(
        self,
        handle: OperationHandle,
        observations: tuple[RootObservation, ...],
        packet_ready: bool,
    ) -> tuple[str, bool] | OperationRefusal:
        with self.store.transaction() as txn:
            event = self._event(handle.scope_digest, handle.event_id)
            if event is None or event["binding"] != handle.binding:
                txn.rollback()
                return OperationRefusal("invalid")
            if bool(event["quarantine"]):
                txn.rollback()
                return ("quarantine", False)
            held = self._held(handle.scope_digest, handle.event_id)
            if held is None:
                confirmed = self._confirmed(handle)
                txn.rollback()
                if confirmed:
                    return ("confirmed", True)
                return ("pending", False)
            judgement = _judge(observations, held)
            if judgement == "quarantine":
                event["quarantine"] = True
                held["state"] = "pending"
                txn.commit()
                return ("quarantine", False)
            if judgement == "disagreement":
                event["inference_blocked"] = True
                held["state"] = "unknown"
                txn.commit()
                return ("disagreement", False)
            if judgement == "unknown":
                event["inference_blocked"] = True
                held["state"] = "unknown"
                txn.commit()
                return ("pending", False)
            if (
                judgement == "confirmed"
                and packet_ready
                and not bool(event["quarantine"])
            ):
                held["state"] = "confirmed"
                event["root_sha"] = held["target_root_sha"]
                event["root_generation"] = held["target_root_generation"]
                scope = self.store._live()["scopes"].get(handle.scope_digest)
                if isinstance(scope, dict):
                    scope["root_sha"] = held["target_root_sha"]
                    scope["root_generation"] = held["target_root_generation"]
                txn.commit()
                return ("confirmed", True)
            txn.rollback()
            return ("pending", False)

    def _mark_ack_attempt(self, handle: OperationHandle) -> bool:
        with self.store.transaction() as txn:
            event = self._event(handle.scope_digest, handle.event_id)
            if event is None or bool(event["ack_attempted"]):
                txn.rollback()
                return False
            event["ack_attempted"] = True
            txn.commit()
            return True

    def _matching(
        self,
        handle: OperationHandle,
        attempt_id: str,
        encoded: str,
    ) -> TransitionPermit | OperationRefusal | None:
        event = self._bound(handle)
        if isinstance(event, OperationRefusal):
            return event
        transition = self._transition(handle.scope_digest, handle.event_id, attempt_id)
        if transition is None:
            return None
        if transition["encoded"] != encoded:
            return OperationRefusal("transition_conflict")
        return TransitionPermit(
            attempt_id=attempt_id,
            first_permit=False,
            state=str(transition["state"]),
            calls=_as_int(event["calls"]),
            sequence=_as_int(event["sequence"]),
        )

    def _bound(self, handle: OperationHandle) -> dict[str, object] | OperationRefusal:
        event = self._event(handle.scope_digest, handle.event_id)
        if event is None or event["binding"] != handle.binding:
            return OperationRefusal("invalid")
        if event["operation_id"] != handle.operation_id:
            return OperationRefusal("invalid")
        return event

    def _event(self, scope: str, event_id: str) -> dict[str, object] | None:
        found = self.store._live()["events"].get((scope, event_id))
        if isinstance(found, dict):
            return found
        return None

    def _transition(
        self, scope: str, event_id: str, attempt_id: str
    ) -> dict[str, object] | None:
        found = self.store._live()["transitions"].get((scope, event_id, attempt_id))
        if isinstance(found, dict):
            return found
        return None

    def _held(self, scope: str, event_id: str) -> dict[str, object] | None:
        for item in self.store._live()["transitions"].values():
            if (
                isinstance(item, dict)
                and item["scope_digest"] == scope
                and item["event_id"] == event_id
                and item["state"] in _HELD
            ):
                return item
        return None

    def _scope_held(self, scope: str) -> bool:
        for item in self.store._live()["transitions"].values():
            if (
                isinstance(item, dict)
                and item["scope_digest"] == scope
                and item["state"] in _HELD
            ):
                return True
        return False

    def _confirmed(self, handle: OperationHandle) -> bool:
        for item in self.store._live()["transitions"].values():
            if (
                isinstance(item, dict)
                and item["scope_digest"] == handle.scope_digest
                and item["event_id"] == handle.event_id
                and item["state"] == "confirmed"
            ):
                return True
        return False

    def _unknown(self, scope: str, event_id: str) -> tuple[RetainedTransition, ...]:
        retained: list[RetainedTransition] = []
        for item in self.store._live()["transitions"].values():
            if (
                isinstance(item, dict)
                and item["scope_digest"] == scope
                and item["event_id"] == event_id
                and item["state"] in _HELD
            ):
                retained.append(
                    RetainedTransition(
                        attempt_id=str(item["attempt_id"]), state=str(item["state"])
                    )
                )
        retained.sort(key=lambda item: item.attempt_id)
        return tuple(retained)

    def _readback(self, handle: OperationHandle) -> AcceptedPacket | None:
        event = self._event(handle.scope_digest, handle.event_id)
        if event is None:
            return None
        first = _packet(event)
        second = _packet(event)
        if first is None or first != second:
            return None
        return first

    def _clock(self) -> int | None:
        try:
            now = self._now()
        except Exception:
            return None
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            return None
        if now > _MAX_GENERATION - CONTROL_DEADLINE_MS:
            return None
        return now


def _packet(event: dict[str, object]) -> AcceptedPacket | None:
    value = event.get("accepted")
    if isinstance(value, AcceptedPacket):
        return value
    return None


def _handle(event: dict[str, object]) -> OperationHandle:
    return OperationHandle(
        scope_digest=str(event["scope_digest"]),
        event_id=str(event["event_id"]),
        operation_id=str(event["operation_id"]),
        binding=str(event["binding"]),
        created_at_ms=_as_int(event["created_at_ms"]),
        control_deadline_ms=_as_int(event["control_deadline_ms"]),
        calls=_as_int(event["calls"]),
        sequence=_as_int(event["sequence"]),
        root_sha=str(event["root_sha"]),
        root_generation=_as_int(event["root_generation"]),
    )


def _ack_marker(handle: OperationHandle, packet: AcceptedPacket) -> str:
    material = "\n".join(
        (handle.event_id, packet.payload_digest, packet.request_digest)
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _exact(observation: RootObservation, transition: dict[str, object]) -> bool:
    return (
        observation.kind == "read"
        and observation.root_sha == transition["target_root_sha"]
        and observation.root_generation == transition["target_root_generation"]
    )


def _judge(
    observations: tuple[RootObservation, ...], transition: dict[str, object]
) -> str:
    reads = [item for item in observations if item.kind == "read"]
    writes = [item for item in observations if item.kind == "write"]
    for write in writes:
        started = write.write_started_at_ms
        if started is None:
            continue
        missed_by_newer_read = any(
            item.observed_at_ms > started
            and write.observed_at_ms > item.observed_at_ms
            and not _exact(item, transition)
            for item in reads
        )
        saw_target = any(
            item.observed_at_ms > started and _exact(item, transition) for item in reads
        )
        if missed_by_newer_read and not saw_target:
            return "quarantine"
    if any(_exact(item, transition) for item in reads):
        return "confirmed"
    if any(item.kind == "unknown" for item in observations):
        return "unknown"
    if any(item.kind == "absent" for item in observations):
        return "disagreement"
    return "pending"


def _recon(
    status: str,
    writer_released: bool,
    *,
    ack_marker: str | None = None,
    reason: str | None = None,
    packet: AcceptedPacket | None = None,
) -> Reconciliation:
    return Reconciliation(
        status=status,
        writer_released=writer_released,
        ack_marker=ack_marker,
        reason=reason,
        accepted_packet=packet,
    )


def _wall_now() -> int:
    return int(time.time() * 1000)


REVIEW_TRIGGERS = frozenset({"full-review", "reply", "reassessment", "verify"})
OPERATION_COMMENT_MARKER = "<!-- review-sensei-operation -->"
_COMMENT_BOUND = 60_000


class OperationCommentPort(Protocol):
    """App-owned pull-request comment. The worker is not this store."""

    def find(self, marker: str) -> tuple[int, str] | None: ...

    def create(self, body: str) -> tuple[int, str]: ...

    def update(self, comment_id: int, body: str) -> str: ...


class GitHubCommentOperationStore(InMemoryOperationStore):
    """Load and publish the operation record as one GitHub issue comment.

    A rolled-back transaction does not write. The comment is metadata only.
    """

    def __init__(self, port: OperationCommentPort) -> None:
        super().__init__()
        self._port = port
        self._comment_id: int | None = None
        self._load()

    def _load(self) -> None:
        found = self._port.find(OPERATION_COMMENT_MARKER)
        if found is None:
            return
        comment_id, body = found
        self._comment_id = comment_id
        self._committed = _decode_comment(body)

    def _publish(self) -> None:
        assert self._txn is not None
        body = _encode_comment(self._txn)
        if self._comment_id is None:
            self._comment_id, echoed = self._port.create(body)
        else:
            echoed = self._port.update(self._comment_id, body)
        if _comment_payload(echoed) != _comment_payload(body):
            raise ValueError("operation comment readback failed")
        super()._publish()


def run_review_trigger(
    host: OperationHost,
    *,
    trigger: str,
    proof: AdmissionProof,
    request: OperationRequest,
    plan: TransitionPlan,
    provider_call: Callable[[], ProviderOutput],
    validator: Callable[[ProviderOutput, int], bool],
    consume_grant: Callable[[], bool],
    server_started_ms: int,
    bootstrap_dispatches: int = 1,
) -> ExecutionResult | OperationRefusal:
    """Admit, restore, or finish one review trigger through the shared host.

    An accepted packet is read back and returned without another provider call.
    """

    if trigger not in REVIEW_TRIGGERS:
        return OperationRefusal("trigger")
    restored = host.restore_operation(proof, request)
    if isinstance(restored, RestoredOperation) and restored.accepted_packet is not None:
        return host.execute_and_accept(restored.handle, provider_call, validator)
    if isinstance(restored, RestoredOperation):
        handle = restored.handle
    else:
        if restored.reason != "not_admitted":
            return restored
        begun = host.begin_operation(
            proof, request, server_started_ms, bootstrap_dispatches
        )
        if not isinstance(begun, OperationHandle):
            return begun
        handle = begun
    if not _note_trigger(host, handle, trigger):
        return OperationRefusal("trigger")
    permit = host.consume_transition(handle, plan, consume_grant=consume_grant)
    if not isinstance(permit, TransitionPermit):
        return permit
    return host.execute_and_accept(handle, provider_call, validator)


def _note_trigger(host: OperationHost, handle: OperationHandle, trigger: str) -> bool:
    with host.store.transaction() as txn:
        event = host.store._live()["events"].get((handle.scope_digest, handle.event_id))
        if not isinstance(event, dict):
            txn.rollback()
            return False
        current = event.get("trigger")
        if current not in (None, trigger):
            txn.rollback()
            return False
        if current == trigger:
            txn.rollback()
            return True
        event["trigger"] = trigger
        txn.commit()
        return True


def _encode_comment(store: dict[str, dict[object, object]]) -> str:
    payload = json.dumps(_export_store(store), sort_keys=True, separators=(",", ":"))
    body = f"{OPERATION_COMMENT_MARKER}\n```json\n{payload}\n```\n"
    if len(body.encode("utf-8")) > _COMMENT_BOUND:
        raise ValueError("operation comment exceeds the GitHub bound")
    return body


def _decode_comment(body: str) -> dict[str, dict[object, object]]:
    return _import_store(json.loads(_comment_payload(body)))


def _comment_payload(body: str) -> str:
    start = body.find("```json\n")
    end = body.rfind("\n```")
    if start < 0 or end < 0 or end <= start:
        raise ValueError("operation comment is not a review record")
    return body[start + len("```json\n") : end]


def _export_store(
    store: dict[str, dict[object, object]],
) -> dict[str, dict[str, object]]:
    widths = {"events": 2, "transitions": 3, "grants": 1, "scopes": 1}
    exported: dict[str, dict[str, object]] = {}
    for name, width in widths.items():
        bucket: dict[str, object] = {}
        for key, value in store[name].items():
            if width == 1:
                encoded = str(key)
            else:
                if not isinstance(key, tuple) or len(key) != width:
                    raise ValueError("operation record key is invalid")
                encoded = "\t".join(str(part) for part in key)
            bucket[encoded] = _export_value(value)
        exported[name] = bucket
    return exported


def _export_value(value: object) -> object:
    if isinstance(value, AcceptedPacket):
        return {
            "kind": "accepted-packet",
            "decisions_digest": value.decisions_digest,
            "known_output_bytes": value.known_output_bytes,
            "payload_digest": value.payload_digest,
            "request_digest": value.request_digest,
            "obligation_ids": list(value.obligation_ids),
        }
    if isinstance(value, tuple):
        return {"kind": "tuple", "items": [_export_value(item) for item in value]}
    if isinstance(value, dict):
        return {str(key): _export_value(item) for key, item in value.items()}
    return value


def _import_store(payload: object) -> dict[str, dict[object, object]]:
    if not isinstance(payload, dict):
        raise ValueError("operation record is invalid")
    widths = {"events": 2, "transitions": 3, "grants": 1, "scopes": 1}
    imported: dict[str, dict[object, object]] = {}
    for name, width in widths.items():
        raw = payload.get(name)
        if not isinstance(raw, dict):
            raise ValueError("operation record is invalid")
        bucket: dict[object, object] = {}
        for key, value in raw.items():
            if not isinstance(key, str):
                raise ValueError("operation record key is invalid")
            if width == 1:
                decoded: object = key
            else:
                parts = key.split("\t")
                if len(parts) != width:
                    raise ValueError("operation record key is invalid")
                decoded = tuple(parts)
            bucket[decoded] = _import_value(value)
        imported[name] = bucket
    return imported


def _import_value(value: object) -> object:
    if isinstance(value, dict) and value.get("kind") == "accepted-packet":
        obligations = value.get("obligation_ids")
        return AcceptedPacket(
            decisions_digest=str(value.get("decisions_digest")),
            known_output_bytes=_as_int(value.get("known_output_bytes")),
            payload_digest=str(value.get("payload_digest")),
            request_digest=str(value.get("request_digest")),
            obligation_ids=tuple(obligations) if isinstance(obligations, list) else (),
        )
    if isinstance(value, dict) and value.get("kind") == "tuple":
        items = value.get("items")
        if not isinstance(items, list):
            raise ValueError("operation record is invalid")
        return tuple(_import_value(item) for item in items)
    if isinstance(value, dict):
        return {str(key): _import_value(item) for key, item in value.items()}
    return value
