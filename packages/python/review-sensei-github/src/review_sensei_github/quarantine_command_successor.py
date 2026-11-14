"""Original confirmed-root custody for a historical command invocation.

A witness records confirmed successors only. It is not a writer capability,
funding grant, candidate-GET receipt, or permission to activate a guarded route.
"""

import inspect
import sys
from dataclasses import dataclass
from typing import Any, cast
from weakref import WeakKeyDictionary

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.source_registry import require_module_seal


@dataclass(frozen=True, eq=False)
class SuccessorWitness:
    pass


def _registry():
    issued: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()
    active: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()

    def start(run, target):
        require_module_seal(sys.modules[__name__])
        from . import quarantine_command_execution
        from .quarantine_command_execution import _source_frame

        frame = _source_frame(
            quarantine_command_execution,
            quarantine_command_execution.prepare_receipt_phase,
        )
        values = cast(Any, frame.f_locals)
        if (
            sys._getframe(1) is not frame
            or values.get("run") is not run
            or values.get("target") is not target
            or values.get("projection") is None
            or run.ledger not in values["prepared"]
        ):
            raise ReviewInputError("historical successor requires original preparation")
        return issue(run, target)

    def start_guarded(run, target, lease):
        from . import quarantine_historical_execution

        require_module_seal(quarantine_historical_execution)
        frame = sys._getframe(1)
        if (
            frame.f_code is not quarantine_historical_execution.activate.__code__
            or frame.f_globals is not vars(quarantine_historical_execution)
            or frame.f_locals.get("run") is not run
            or frame.f_locals.get("lease") is not lease
            or frame.f_locals["entry"]["target"] is not target
        ):
            raise ReviewInputError("guarded successor lacks original closed lease")
        quarantine_historical_execution.require_closed(lease, run)
        return issue(run, target)

    def issue(run, target):
        caller = run.caller
        record = caller._effect_record
        if (
            caller in active
            or record is None
            or caller._checkpoint is not None
            or caller._root != record.record_sha256
            or record.identity() != run.identity
        ):
            raise ReviewInputError("historical successor preparation root changed")
        token = SuccessorWitness()
        issued[token] = dict(
            caller=caller,
            ledger=run.ledger,
            identity=run.identity,
            record=record,
            raw=canonical_bytes(record.to_dict()),
            root=caller._root,
            count=0,
            terminal=False,
            attempted=None,
            phase=None,
        )
        active[caller] = token
        return token

    def require_current(token, caller, ledger):
        require_module_seal(sys.modules[__name__])
        entry = issued.get(token) if type(token) is SuccessorWitness else None
        if (
            entry is None
            or entry["caller"] is not caller
            or entry["ledger"] is not ledger
            or active.get(caller) is not token
            or entry["terminal"]
            or caller._effect_record is not entry["record"]
            or caller._root != entry["root"]
            or canonical_bytes(entry["record"].to_dict()) != entry["raw"]
        ):
            raise ReviewInputError("historical successor witness is copied or stale")
        return entry["record"]

    def before_request(caller):
        token = active.get(caller)
        if token is None:
            return
        from . import quarantine_command_namespace
        from .quarantine_command_execution import _source_frame
        from .quarantine_command_phase import classify_original_mutation

        frame = _source_frame(
            quarantine_command_namespace, quarantine_command_namespace.before_sequence
        )
        values = cast(Any, frame.f_locals)
        if sys._getframe(1) is not frame or values.get("caller") is not caller:
            raise ReviewInputError(
                "historical root attempt requires original namespace source"
            )
        entry = issued[token]
        require_current(token, caller, entry["ledger"])
        outer = frame.f_back
        if outer is None:
            raise ReviewInputError("historical root attempt lacks original request")
        request = outer.f_locals.get("request")
        # Other mutation families retain their existing source admission. Only
        # an actual original checkpoint PATCH participates in this root fence.
        if (
            request is None
            or request.get_method() != "PATCH"
            or caller._checkpoint is None
        ):
            return
        description = classify_original_mutation(caller)
        if description is None or description.kind not in {"root", "terminal-root"}:
            raise ReviewInputError("historical checkpoint mutation family changed")
        if entry["attempted"] is not None:
            raise ReviewInputError("historical root cannot repeat an unconfirmed PATCH")
        phase = description.checkpoint_phase
        entry["attempted"] = (
            caller._checkpoint,
            caller._checkpoint.record_sha256,
            len(caller.observations),
            phase,
        )

    def confirm_checkpoint(caller, ledger, record, persisted):
        token = active.get(caller)
        if token is None:
            return
        from . import api_run_requests

        require_module_seal(api_run_requests)
        frame = inspect.currentframe()
        frame = frame.f_back if frame is not None else None
        method = inspect.unwrap(
            api_run_requests.ApiRunRequestTransport._accept_checkpoint
        )
        if (
            frame is None
            or frame.f_code is not method.__code__
            or frame.f_globals is not vars(api_run_requests)
            or any(
                frame.f_locals.get(name) is not value
                for name, value in (
                    ("self", caller),
                    ("ledger", ledger),
                    ("record", record),
                    ("persisted", persisted),
                )
            )
        ):
            raise ReviewInputError(
                "historical successor requires original confirmation"
            )
        previous = require_current(token, caller, ledger)
        entry = issued[token]
        attempt = entry["attempted"]
        if (
            attempt is None
            or caller._checkpoint is not attempt[0]
            or record.record_sha256 != attempt[1]
            or len(
                [
                    row
                    for row in caller.observations[attempt[2] :]
                    if row.get("mutation")
                ]
            )
            != 1
        ):
            raise ReviewInputError(
                "historical successor lacks its single physical PATCH"
            )
        if (
            caller._checkpoint is None
            or record.record_sha256 != caller._checkpoint.record_sha256
            or record.identity() != entry["identity"]
            or record.generation <= previous.generation
            or canonical_bytes(persisted.to_document())
            != canonical_bytes(record.api_run_accounting)
        ):
            raise ReviewInputError("historical confirmed successor changed")
        # Original _accept_checkpoint has already required accounting succession.
        # Do not consume the independent candidate receipt or adopt an unconfirmed
        # candidate. This hook runs before the caller's original root assignment.
        entry.update(
            record=record,
            raw=canonical_bytes(record.to_dict()),
            root=record.record_sha256,
            count=entry["count"] + 1,
            phase=attempt[3],
            attempted=None,
            terminal=persisted.action is not None
            and persisted.action["phase"] == "terminal",
        )
        if entry["terminal"]:
            active.pop(caller, None)

    return start, start_guarded, require_current, before_request, confirm_checkpoint


start, start_guarded, require_current, before_request, confirm_checkpoint = _registry()
del _registry

from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
