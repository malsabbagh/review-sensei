"""Original confirmed-root custody for a historical command invocation.

A witness records confirmed successors only. It is not a writer capability,
funding grant, candidate-GET receipt, or permission to activate a guarded route.
"""

import inspect
import sys
from dataclasses import dataclass
from typing import Any
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
        if (
            frame.f_locals.get("run") is not run
            or frame.f_locals.get("target") is not target
            or frame.f_locals.get("projection") is None
            or run.ledger not in frame.f_locals["prepared"]
        ):
            raise ReviewInputError("historical successor requires original preparation")
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
            terminal=persisted.action is not None
            and persisted.action["phase"] == "terminal",
        )
        if entry["terminal"]:
            active.pop(caller, None)

    return start, require_current, confirm_checkpoint


start, require_current, confirm_checkpoint = _registry()
del _registry

from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
