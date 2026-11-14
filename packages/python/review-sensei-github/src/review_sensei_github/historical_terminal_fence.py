"""Negative terminal restriction for a genuine closed historical invocation.

No funding, sizing discount, reader or approval authority is issued here. The
full protected tranche stays reserved. A separately admitted historical command
may make one original close, in one existing sixteen-attempt operation.
"""

import hashlib
import inspect
import sys
from dataclasses import asdict
from types import FrameType
from typing import Any, cast
from weakref import ref

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.source_registry import require_instance_source, require_module_seal


def _frame(function, module, start) -> FrameType:
    require_module_seal(module)
    original = inspect.unwrap(function)
    while start is not None:
        if start.f_code is original.__code__ and start.f_globals is vars(module):
            return start
        start = start.f_back
    raise ReviewInputError("historical terminal requires original invocation source")


def _zero_fences(caller):
    from . import api_run_requests, trust_effects

    require_module_seal(api_run_requests)
    profile = api_run_requests._original_trust_verification_profile
    calls = api_run_requests._original_trust_verification_calls
    if (
        trust_effects.verification_profile is not profile
        or trust_effects.verification_calls is not calls
    ):
        raise ReviewInputError("historical terminal trust source changed")
    if (
        profile(caller) is not None
        or calls(caller) != 0
        or object.__getattribute__(caller, "__dict__").get(
            "_controlled_restart_admission"
        )
        is not None
    ):
        raise ReviewInputError("historical terminal refresh or restart is unsupported")


def _operation_shape(operation):
    from . import api_run_plan, caller_operation

    require_module_seal(api_run_plan)
    require_module_seal(caller_operation)
    if type(operation) is not caller_operation.CallerOperation:
        raise ReviewInputError("historical terminal operation type changed")
    require_instance_source(operation, methods=("consume",))
    if (
        api_run_plan.QUARANTINE_CHECKPOINT_REQUESTS != 16
        or type(operation.capacity) is not int
        or operation.capacity != 16
        or type(operation.write_tail) is not int
        or operation.write_tail != api_run_plan.WRITE_CONFIRMATION_TAIL
        or type(operation.used) is not int
        or not 0 <= operation.used <= 16
        or type(operation.write_attempted) is not bool
        or type(operation.confirmed) is not bool
    ):
        raise ReviewInputError("historical terminal original operation bound changed")


def _registry():
    # id lookup does not invoke an unissued object's hash or property callbacks.
    active: dict[int, Any] = {}
    closed: dict[int, Any] = {}

    def start(run, lease):
        from . import (
            action_run,
            api_run_requests,
        )
        from . import (
            quarantine_historical_execution as historical,
        )

        for module in (sys.modules[__name__], action_run, api_run_requests, historical):
            require_module_seal(module)
        frame = sys._getframe(1)
        if (
            frame.f_code is not historical.activate.__code__
            or frame.f_globals is not vars(historical)
            or cast(Any, frame.f_locals).get("run") is not run
            or cast(Any, frame.f_locals).get("lease") is not lease
        ):
            raise ReviewInputError(
                "historical terminal fence requires closed source activation"
            )
        if run is None:
            raise ReviewInputError("historical terminal run is absent")
        historical.require_closed(lease, run)
        caller = run.caller
        _zero_fences(caller)
        execution = _frame(action_run.execute_command, action_run, frame)
        if (
            cast(Any, execution.f_locals).get("run") is not run
            or cast(Any, execution.f_locals).get("caller") is not caller
        ):
            raise ReviewInputError("historical terminal original command owner changed")
        terminal = next(
            b for b in caller.accounting.blocks if b.block_id == caller._terminal_block
        )
        # This production restriction is only the original 512/50 shape. It
        # grants nothing to other historical budgets and does not alter them.
        if caller.accounting.configuration.limit != 512 or terminal.undispatched != 50:
            return
        if (
            id(caller) in active
            or id(caller) in closed
            or caller._active_operation is not None
            or caller._terminal_reason is not None
            or terminal.pending
            or caller._checkpoint is not None
            or caller.accounting.action is None
            or caller.accounting.action["phase"] != "pending"
            or caller.accounting.action["execution_started"]
        ):
            raise ReviewInputError("historical terminal initial scope changed")
        active[id(caller)] = dict(
            caller=caller,
            run=run,
            ledger=run.ledger,
            identity=run.identity,
            execution=execution,
            lease=lease,
            phase="ordinary",
            close=None,
            operation=None,
            proof=None,
            attempts=0,
            root=caller._root,
            record=caller._effect_record,
            record_bytes=canonical_bytes(caller._effect_record.to_dict()),
            owner=canonical_bytes(asdict(caller.accounting.owner)),
            configuration=canonical_bytes(asdict(caller.accounting.configuration)),
            intent=canonical_bytes(caller.accounting.action["intent"]),
            remaining=caller.accounting.remaining,
            prior=caller.accounting.prior_nonstartup_liability,
            terminal_block=caller._terminal_block,
            terminal_initial=terminal,
            ordinary_block=caller._block,
            unknowns=frozenset(),
        )

    def common(entry, caller):
        from . import action_run, api_run_requests

        require_module_seal(sys.modules[__name__])
        require_module_seal(api_run_requests)
        require_module_seal(action_run)
        require_instance_source(
            caller,
            methods=(
                "open",
                "close_action",
                "_action_checkpoint",
                "replace_checkpoint",
                "reconcile_checkpoint",
                "_action_unresolved",
            ),
        )
        _zero_fences(caller)
        execution = _frame(action_run.execute_command, action_run, sys._getframe(1))
        state = caller.accounting
        from . import guarded_pending_publication

        require_module_seal(guarded_pending_publication)
        guarded_pending_publication._original_accounting_graph(state, models=True)
        if (
            entry["caller"] is not caller
            or execution is not entry["execution"]
            or cast(Any, execution.f_locals).get("run") is not entry["run"]
            or cast(Any, execution.f_locals).get("caller") is not caller
            or caller._terminal_block != entry["terminal_block"]
            or canonical_bytes(asdict(state.owner)) != entry["owner"]
            or canonical_bytes(asdict(state.configuration)) != entry["configuration"]
            or state.remaining != entry["remaining"]
            or state.prior_nonstartup_liability != entry["prior"]
            or state.action is None
            or canonical_bytes(state.action["intent"]) != entry["intent"]
            or caller._root != entry["root"]
            or caller._effect_record is not entry["record"]
            or canonical_bytes(entry["record"].to_dict()) != entry["record_bytes"]
        ):
            raise ReviewInputError("historical terminal invocation custody changed")
        pending = frozenset(
            (b.block_id, p.request_id, p.deadline_unix_ms)
            for b in state.blocks
            for p in b.pending
        )
        if not entry["unknowns"].issubset(pending):
            raise ReviewInputError(
                "historical terminal prior unknown debit disappeared"
            )
        return state

    def before_terminal(caller, run, proof):
        entry = active.get(id(caller))
        if entry is None:
            ended = closed.get(id(caller))
            if ended is not None and ended() is caller:
                raise ReviewInputError("historical terminal invocation already closed")
            return
        assert caller is not None
        from . import action_run, api_run_requests

        frame = sys._getframe(1)
        state = common(entry, caller)
        if (
            frame is not entry["execution"]
            or frame.f_code is not action_run.execute_command.__code__
            or cast(Any, frame.f_locals).get("proof") is not proof
            or cast(Any, frame.f_locals).get("run") is not run
            or run is not entry["run"]
            or type(proof) is not api_run_requests.ActionResultProof
            or proof is not caller._action_result_proof
            or proof.boundary is not caller._action_effect_boundary
            or hashlib.sha256(
                canonical_bytes({"manifest": proof.manifest, "outcome": proof.outcome})
            ).hexdigest()
            != caller._action_result_fingerprint
            or entry["phase"] != "ordinary"
            or caller._terminal_reason is not None
            or caller._active_operation is not None
            or caller._checkpoint is not None
            or caller._action_unresolved()
            or state.action["phase"] != "pending"
            or state.action["execution_started"] is not True
            or next(b for b in state.blocks if b.block_id == entry["terminal_block"])
            != entry["terminal_initial"]
        ):
            raise ReviewInputError(
                "historical terminal entry lacks one settled source result"
            )
        entry.update(
            phase="closing",
            proof=proof,
            proof_bytes=canonical_bytes(
                {"manifest": proof.manifest, "outcome": proof.outcome}
            ),
            sequence=caller._sequence,
            observations=len(caller.observations),
            unknowns=frozenset(
                (b.block_id, p.request_id, p.deadline_unix_ms)
                for b in state.blocks
                for p in b.pending
            ),
        )

    def before_request(caller):
        entry = active.get(id(caller))
        if entry is None:
            ended = closed.get(id(caller))
            if ended is not None and ended() is caller:
                raise ReviewInputError("historical terminal invocation already closed")
            return
        assert caller is not None
        from . import api_run_requests
        from . import quarantine_command_namespace as namespace

        require_module_seal(namespace)
        frame = sys._getframe(1)
        if (
            frame.f_code is not namespace.before_sequence.__code__
            or frame.f_globals is not vars(namespace)
            or cast(Any, frame.f_locals).get("caller") is not caller
        ):
            raise ReviewInputError(
                "historical terminal request lacks original namespace"
            )
        state = common(entry, caller)
        if entry["phase"] == "ordinary":
            if caller._terminal_reason is not None:
                raise ReviewInputError("historical terminal skipped settled entry")
            return
        close = _frame(
            api_run_requests.ApiRunRequestTransport.close_action,
            api_run_requests,
            frame,
        )
        action_checkpoint = _frame(
            api_run_requests.ApiRunRequestTransport._action_checkpoint,
            api_run_requests,
            frame,
        )
        operation = caller._active_operation
        _operation_shape(operation)
        if (
            entry["phase"] != "closing"
            or caller._terminal_reason != "action-completion"
            or caller._block != entry["terminal_block"]
            or cast(Any, close.f_locals).get("self") is not caller
            or cast(Any, close.f_locals).get("ledger") is not entry["ledger"]
            or cast(Any, close.f_locals).get("proof") is not entry["proof"]
            or cast(Any, action_checkpoint.f_locals).get("self") is not caller
            or cast(Any, action_checkpoint.f_locals).get("ledger")
            is not entry["ledger"]
            or cast(Any, action_checkpoint.f_locals).get("mutate")
            is not cast(Any, close.f_locals).get("prepare")
            or caller._checkpoint_operation is not operation
            or not any(
                o is operation and b == entry["terminal_block"]
                for o, b in caller._issued_operations
            )
            or operation.used != entry["attempts"]
            or operation.confirmed
            or caller._sequence != entry["sequence"] + entry["attempts"]
            or len(caller.observations) != entry["observations"] + entry["attempts"]
            or entry["attempts"] >= 16
            or canonical_bytes(
                {"manifest": entry["proof"].manifest, "outcome": entry["proof"].outcome}
            )
            != entry["proof_bytes"]
        ):
            raise ReviewInputError(
                "historical terminal source operation or attempt bound changed"
            )
        if entry["operation"] is None:
            replace_frame = _frame(
                api_run_requests.ApiRunRequestTransport.replace_checkpoint,
                api_run_requests,
                frame,
            )
            if cast(Any, replace_frame.f_locals).get(
                "operation"
            ) is not operation or cast(Any, replace_frame.f_locals).get(
                "mutate"
            ) is not cast(Any, close.f_locals).get("prepare"):
                raise ReviewInputError(
                    "historical terminal first operation is not original close"
                )
            entry["operation"], entry["close"] = operation, close
        elif entry["operation"] is not operation or entry["close"] is not close:
            raise ReviewInputError(
                "historical terminal cannot begin a second close or operation"
            )
        terminal = next(
            b for b in state.blocks if b.block_id == entry["terminal_block"]
        )
        if terminal.undispatched != 50 - entry["attempts"]:
            raise ReviewInputError("historical terminal reservation changed")
        entry["attempts"] += 1

    def confirm_checkpoint(caller, ledger, record, persisted):
        entry = active.get(id(caller))
        if entry is None:
            ended = closed.get(id(caller))
            if ended is not None and ended() is caller:
                raise ReviewInputError("historical terminal invocation already closed")
            return
        assert caller is not None
        from . import api_run_requests
        from . import quarantine_command_successor as successor

        require_module_seal(successor)
        frame = sys._getframe(1)
        original = _frame(
            api_run_requests.ApiRunRequestTransport._accept_checkpoint,
            api_run_requests,
            frame,
        )
        if (
            frame.f_code is not successor.confirm_checkpoint.__code__
            or frame.f_globals is not vars(successor)
            or original is not frame.f_back
            or any(
                cast(Any, original.f_locals).get(k) is not v
                for k, v in (
                    ("self", caller),
                    ("ledger", ledger),
                    ("record", record),
                    ("persisted", persisted),
                )
            )
        ):
            raise ReviewInputError(
                "historical terminal successor lacks original confirmation"
            )
        common(entry, caller)
        if (
            ledger is not entry["ledger"]
            or caller._checkpoint is None
            or record.record_sha256 != caller._checkpoint.record_sha256
            or record.identity() != entry["identity"]
            or canonical_bytes(record.api_run_accounting)
            != canonical_bytes(persisted.to_document())
        ):
            raise ReviewInputError("historical terminal confirmed root changed")
        terminal = (
            persisted.action is not None and persisted.action["phase"] == "terminal"
        )
        if terminal:
            operation = entry["operation"]
            _operation_shape(operation)
            if (
                entry["phase"] != "closing"
                or operation is not caller._active_operation
                or operation is not caller._checkpoint_operation
                or not operation.write_attempted
                or operation.used != entry["attempts"]
                or not 1 <= entry["attempts"] <= 16
                or persisted.action["result"] != entry["proof"].manifest
                or persisted.action["outcome"] != entry["proof"].outcome
                or next(
                    b
                    for b in caller.accounting.blocks
                    if b.block_id == entry["terminal_block"]
                ).undispatched
                < 34
            ):
                raise ReviewInputError("historical terminal close successor changed")
            active.pop(id(caller))
            key = id(caller)

            def clear_closed(_value: Any, key: int = key) -> None:
                closed.pop(key, None)

            closed[key] = ref(caller, clear_closed)
        else:
            if entry["phase"] != "ordinary":
                raise ReviewInputError(
                    "historical terminal operation confirmed a nonterminal root"
                )
            entry.update(
                root=record.record_sha256,
                record=record,
                record_bytes=canonical_bytes(record.to_dict()),
            )

    return start, before_terminal, before_request, confirm_checkpoint


start, before_terminal, before_request, confirm_checkpoint = _registry()
del _registry

from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
