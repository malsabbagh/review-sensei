"""One-use receipts for original exact candidate GETs, never write authority.

The candidate's bytes, source snapshot and physical read are captured together.
A separately qualified consumer must still prove its own business transition.
"""

import inspect
import sys
from dataclasses import dataclass
from typing import Any, cast
from weakref import WeakKeyDictionary

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.source_registry import require_instance_source, require_module_seal


@dataclass(eq=False, frozen=True)
class _Read:
    pass


def _make_registry():
    source: Any = None
    pending: WeakKeyDictionary = WeakKeyDictionary()
    confirmed: WeakKeyDictionary = WeakKeyDictionary()

    def install(cls: Any):
        nonlocal source
        current = inspect.currentframe()
        frame = current.f_back if current is not None else None
        module = sys.modules.get("review_sensei_github.quarantine_checkpoint")
        if (
            source is not None
            or module is None
            or frame is None
            or frame.f_globals is not vars(module)
            or frame.f_code.co_name != "<module>"
            or not getattr(getattr(module, "__spec__", None), "_initializing", False)
            or cls is not getattr(module, "_CheckpointSnapshot", None)
        ):
            raise ReviewInputError("candidate GET receipt requires defining source")
        source = cls, cast(Any, cls).get, cast(Any, cls).get.__code__

    def original_frame(snapshot: Any):
        current = inspect.currentframe()
        parent = current.f_back if current is not None else None
        frame = parent.f_back if parent is not None else None
        if (
            source is None
            or type(snapshot) is not source[0]
            or source[1].__code__ is not source[2]
            or frame is None
            or frame.f_code is not source[2]
            or frame.f_globals is not vars(sys.modules[source[0].__module__])
            or frame.f_locals.get("self") is not snapshot
        ):
            raise ReviewInputError("candidate GET receipt source changed")
        require_module_seal(sys.modules[__name__])
        require_instance_source(snapshot, methods=("require", "validate"))
        require_instance_source(
            cast(Any, snapshot).ledger,
            methods=("_request", "_part_owner", "evidence_producer"),
        )
        require_instance_source(
            cast(Any, snapshot).caller,
            methods=("open", "_require_ledger", "_accept_checkpoint"),
        )

    def binding(snapshot):
        return (
            snapshot.caller,
            snapshot.ledger,
            snapshot.identity,
            snapshot.operation,
            snapshot.block,
            snapshot.root,
            snapshot.comment_id,
            snapshot.owner_id,
            canonical_bytes(snapshot.record.to_dict()),
            snapshot.body,
            snapshot.lineage,
            snapshot.listed_ids,
            snapshot.staged,
            None
            if snapshot.candidate is None
            else canonical_bytes(snapshot.candidate.to_dict()),
            snapshot.candidate_body,
            canonical_bytes(vars(snapshot.caller.accounting.owner)),
            snapshot.operation.capacity,
            snapshot.operation.write_tail,
            snapshot.operation.key,
        )

    def begin(snapshot, *, candidate):
        original_frame(snapshot)
        if candidate is not True:
            return None
        snapshot.require()
        operation = snapshot.operation
        if (
            snapshot.candidate is None
            or snapshot.candidate_body is None
            or not operation.write_attempted
            or operation.confirmed
        ):
            raise ReviewInputError(
                "candidate GET receipt lacks attempted original write"
            )
        token = _Read()
        pending[token] = (
            snapshot,
            binding(snapshot),
            len(snapshot.caller.observations),
            operation.used,
            operation.confirmation_used,
        )
        return token

    def finish(token, snapshot, parsed, payload):
        original_frame(snapshot)
        saved = pending.pop(token, None) if type(token) is _Read else None
        if saved is None or saved[0] is not snapshot or saved[1] != binding(snapshot):
            raise ReviewInputError("candidate GET receipt custody changed")
        caller, operation = snapshot.caller, snapshot.operation
        observations = caller.observations[saved[2] :]
        if (
            len(observations) != 1
            or operation.used != saved[3] + 1
            or operation.confirmation_used != saved[4] + 1
        ):
            raise ReviewInputError("candidate GET receipt has no exact physical read")
        observation = observations[0]
        if (
            observation.get("mutation") is not False
            or observation.get("outcome") != "response"
            or observation.get("uncertain_effect")
            or type(observation.get("deadline_unix_ms")) is not int
            or int(caller._clock() * 1000) > observation["deadline_unix_ms"]
            or not any(
                item.request_id == observation.get("request_id")
                and item.deadline_unix_ms == observation["deadline_unix_ms"]
                for item in caller.accounting.completed
            )
            or caller._last_method != "GET"
            or canonical_bytes(parsed.to_dict()) != saved[1][13]
            or payload.get("id") != snapshot.comment_id
            or payload.get("body") != snapshot.candidate_body
            or payload.get("user", {}).get("type") != "Bot"
            or payload.get("user", {}).get("id") != snapshot.owner_id
        ):
            raise ReviewInputError(
                "candidate GET receipt is not confirmed exact evidence"
            )
        confirmed[caller] = (
            snapshot,
            saved[1],
            parsed,
            observation,
            canonical_bytes(observation),
            canonical_bytes(payload),
            operation.used,
            operation.confirmation_used,
        )
        return parsed

    def consume(caller, ledger, identity, record):
        require_module_seal(sys.modules[__name__])
        saved = confirmed.get(caller)
        if saved is None:
            raise ReviewInputError("candidate GET receipt is unavailable")
        (
            snapshot,
            sealed,
            parsed,
            observation,
            observation_bytes,
            payload_bytes,
            used,
            confirmation_used,
        ) = saved
        operation = snapshot.operation
        require_instance_source(
            caller, methods=("_require_ledger", "_accept_checkpoint")
        )
        require_instance_source(
            ledger, methods=("_request", "_part_owner", "evidence_producer")
        )
        if (
            binding(snapshot) != sealed
            or snapshot.caller is not caller
            or snapshot.ledger is not ledger
            or snapshot.identity != identity
            or caller._block != snapshot.block
            or ledger.caller is not caller
            or not operation.confirmed
            or not operation.write_attempted
            or operation.used != used
            or operation.confirmation_used != confirmation_used
            or not any(
                issued is operation and block == snapshot.block
                for issued, block in caller._issued_operations
            )
            or caller._checkpoint is not None
            or caller._checkpoint_operation is not None
            or caller._active_operation is not None
            or caller._effect_record is not record
            or record is not parsed
            or caller._root != record.record_sha256
            or canonical_bytes(record.to_dict()) != sealed[13]
            or not caller.observations
            or caller.observations[-1] is not observation
            or canonical_bytes(observation) != observation_bytes
            or int(caller._clock() * 1000) > observation["deadline_unix_ms"]
        ):
            raise ReviewInputError("candidate GET receipt is stale or changed")
        caller._require_ledger(ledger, identity)
        confirmed.pop(caller)
        return payload_bytes

    return install, begin, finish, consume


install_source, begin, finish, consume = _make_registry()
del _make_registry

from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
