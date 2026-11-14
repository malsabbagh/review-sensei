"""Negative-only original caller namespace fencing; never effect admission.

The fence protects a detached accounting size bound through the already issued
ordinary and terminal tranches. It neither issues funding nor admits a command.
"""

import hashlib
import inspect
import sys
from dataclasses import dataclass
from typing import Any, cast
from weakref import WeakKeyDictionary

from review_sensei.api_work import MAX_API_REQUEST_LIMIT, PendingApiRequest
from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.source_registry import require_instance_source, require_module_seal


def _owner(value):
    if (
        type(value.run_attempt) is not int
        or type(value.job) is not str
        or type(value.generation) is not int
    ):
        raise ReviewInputError("cold namespace owner scalar changed")
    return value.run_attempt, value.job, value.generation


def _configuration(value):
    if (
        type(value.commit) is not str
        or type(value.digest) is not str
        or type(value.limit) is not int
    ):
        raise ReviewInputError("cold namespace configuration scalar changed")
    return value.commit, value.digest, value.limit


def _function(value):
    return (
        value,
        value.__code__,
        value.__globals__,
        value.__defaults__,
        value.__kwdefaults__,
    )


def _next_sequence(sequence, ceiling, deadline):
    if (
        type(sequence) is not int
        or type(ceiling) is not int
        or not 0 <= sequence < ceiling <= MAX_API_REQUEST_LIMIT
        or type(deadline) is not int
        or not 0 <= deadline <= MAX_API_REQUEST_LIMIT
    ):
        raise ReviewInputError("cold namespace sequence or deadline ceiling exceeded")
    return sequence + 1


@dataclass(frozen=True, eq=False)
class _Namespace:
    pass


def _registry():
    source: dict[str, Any] = {}
    issued: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()
    active: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()
    observed: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()
    current: dict[str, Any] = {}
    confirmations: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()
    definition_file = cast(Any, inspect.currentframe()).f_code.co_filename
    defining_module = sys.modules[__name__]
    defining_spec = defining_module.__spec__
    if defining_spec is None:
        raise ReviewInputError("namespace defining loader is unavailable")
    defining_loader = defining_spec.loader

    def loader_binding(loader):
        descriptor = inspect.getattr_static(type(loader), "get_code", None)
        if (
            descriptor is None
            or inspect.getattr_static(loader, "get_code", None) is not descriptor
        ):
            raise ReviewInputError("current namespace loader implementation changed")
        implementation = getattr(descriptor, "__func__", descriptor)
        return (
            type(loader),
            descriptor,
            getattr(implementation, "__code__", None),
            getattr(implementation, "__globals__", None),
            getattr(implementation, "__defaults__", None),
            getattr(implementation, "__kwdefaults__", None),
        )

    defining_loader_binding = loader_binding(defining_loader)
    defining_origin = defining_spec.origin
    defining_file = getattr(defining_module, "__file__", None)
    defining_loader_state = tuple(vars(defining_loader).items())

    def install(caller_type):
        module = sys.modules.get("review_sensei_github.api_run_requests")
        frame = cast(Any, inspect.currentframe()).f_back
        spec = getattr(module, "__spec__", None)
        loader = getattr(spec, "loader", None)
        if (
            source
            or module is None
            or frame.f_globals is not vars(module)
            or frame.f_locals is not frame.f_globals
            or not getattr(spec, "_initializing", False)
            or not callable(getattr(loader, "get_code", None))
            or cast(Any, loader).get_code(module.__name__) != frame.f_code
            or caller_type is not module.ApiRunRequestTransport
        ):
            raise ReviewInputError("cold namespace requires original defining source")
        source.update(
            caller_type=caller_type,
            open=_function(caller_type.open),
            close=_function(caller_type.close_action),
            ledger=_function(caller_type._require_ledger),
            response_type=module._AccountedResponse,
            response_read=_function(module._AccountedResponse.read),
            confirmed_code=next(
                item
                for item in caller_type.open.__code__.co_consts
                if inspect.iscode(item) and item.co_name == "confirmed"
            ),
        )

    def install_current_validator(function):
        from pathlib import Path
        from types import FunctionType

        require_module_seal(sys.modules[__name__])
        name = "review_sensei_github.current_quarantine_finalization"
        module = sys.modules.get(name)
        frame = cast(Any, inspect.currentframe()).f_back
        spec = getattr(module, "__spec__", None)
        loader = getattr(spec, "loader", None)
        expected = (
            f"<frozen {name}>"
            if definition_file == f"<frozen {__name__}>"
            else str(
                Path(definition_file).with_name("current_quarantine_finalization.py")
            )
        )

        def sibling(value):
            if value in (None, "frozen"):
                return value
            return str(
                Path(value).with_name(
                    "current_quarantine_finalization" + Path(value).suffix
                )
            )

        expected_state = {}
        replacements = {
            __name__: name,
            definition_file: expected,
            defining_origin: sibling(defining_origin),
            defining_file: sibling(defining_file),
        }
        for key, value in defining_loader_state:
            expected_state[key] = (
                replacements.get(value, value) if type(value) is str else value
            )
        actual_state = vars(loader) if hasattr(loader, "__dict__") else {}
        original_state = vars(defining_loader)
        state_intact = (
            set(actual_state) == set(expected_state)
            and set(original_state) == {key for key, _ in defining_loader_state}
            and all(
                original_state[key] is value for key, value in defining_loader_state
            )
            and all(
                (
                    actual_state[key] == value
                    if type(value) in (str, int, bool, type(None))
                    else actual_state[key] is value
                )
                for key, value in expected_state.items()
            )
        )
        if (
            current
            or not state_intact
            or module is None
            or type(function) is not FunctionType
            or frame.f_globals is not vars(module)
            or frame.f_locals is not frame.f_globals
            or not getattr(spec, "_initializing", False)
            or loader_binding(loader) != defining_loader_binding
            or loader_binding(defining_loader) != defining_loader_binding
            or getattr(spec, "origin", None) != sibling(defining_origin)
            or getattr(module, "__file__", None) != sibling(defining_file)
            or frame.f_code.co_filename != expected
            or function.__module__ != name
            or function.__qualname__ != "validate_namespace_request"
            or id(getattr(module, "validate_namespace_request", None)) != id(function)
            or function.__globals__ is not vars(module)
            or function.__defaults__ is not None
            or function.__kwdefaults__ is not None
            or function.__closure__ is not None
            or hasattr(function, "__wrapped__")
        ):
            raise ReviewInputError(
                "current namespace requires original defining source"
            )
        try:
            original_code = cast(Any, loader).get_code(name)
        except (OSError, ImportError) as error:
            raise ReviewInputError(
                "current namespace defining source is unavailable"
            ) from error
        if original_code != frame.f_code:
            raise ReviewInputError(
                "current namespace requires original defining source"
            )
        current.update(module=module, function=_function(function))

    def observed_sequence(caller):
        require_module_seal(sys.modules[__name__])
        if type(caller) is not source.get("caller_type"):
            raise ReviewInputError("physical sequence source is unavailable")
        require_instance_source(
            caller, methods=("open", "close_action", "_require_ledger")
        )
        sequence = observed.get(caller)
        if (
            type(sequence) is not int
            or type(caller._sequence) is not int
            or sequence != caller._sequence
            or _function(source["open"][0]) != source["open"]
        ):
            raise ReviewInputError("physical sequence history is missing or changed")
        return sequence

    def validate_current(caller, deadline):
        if not current:
            return False
        module, saved = current["module"], current["function"]
        function = saved[0]
        require_module_seal(module)
        if (
            sys.modules.get(module.__name__) is not module
            or id(getattr(module, "validate_namespace_request", None)) != id(function)
            or _function(function) != saved
            or function.__closure__ is not None
            or hasattr(function, "__wrapped__")
        ):
            raise ReviewInputError("current namespace validator source changed")
        result = function(caller, deadline)
        if type(result) is not bool:
            raise ReviewInputError("current namespace validator result is invalid")
        return result

    def original(kind, frame):
        require_module_seal(sys.modules[__name__])
        saved = source[kind]
        if (
            _function(saved[0]) != saved
            or frame is None
            or frame.f_code is not saved[1]
            or frame.f_globals is not saved[2]
        ):
            raise ReviewInputError("cold namespace original source changed")

    def expire(token):
        entry = issued.pop(token, None)
        if entry is not None and active.get(entry["caller"]) is token:
            active.pop(entry["caller"], None)

    def _require(token, caller):
        require_module_seal(sys.modules[__name__])
        entry = issued.get(token) if type(token) is _Namespace else None
        if (
            entry is None
            or active.get(caller) is not token
            or entry["caller"] is not caller
        ):
            raise ReviewInputError("cold namespace is copied or expired")
        from review_sensei import api_work

        from .guarded_pending_publication import _original_accounting_graph

        require_module_seal(api_work)
        require_instance_source(
            caller, methods=("open", "close_action", "_require_ledger")
        )
        state = caller.accounting
        _original_accounting_graph(state, models=True)
        record = caller._effect_record
        persisted = None if record is None else record.api_run_accounting.get("action")
        if (
            persisted is not None
            and persisted.get("phase") == "terminal"
            and caller._checkpoint is None
        ):
            expire(token)
            raise ReviewInputError("cold namespace is copied or expired")
        if (
            type(caller) is not source["caller_type"]
            or _function(source["open"][0]) != source["open"]
            or _function(source["close"][0]) != source["close"]
            or getattr(caller._require_ledger, "__func__", None)
            is not source["ledger"][0]
            or _owner(state.owner) != entry["owner"]
            or _configuration(state.configuration) != entry["configuration"]
            or state.identity != entry["identity"]
            or state.remaining != entry["unallocated"]
            or state.prior_nonstartup_liability != entry["prior"]
            or state.action is None
            or canonical_bytes(state.action["intent"]) != entry["intent"]
            or caller._terminal_block != entry["terminal"]
            or tuple(b.block_id for b in state.blocks) != tuple(entry["blocks"])
        ):
            raise ReviewInputError("cold namespace owner or funding changed")
        caller._require_ledger(entry["ledger"], entry["session_identity"])
        for block in state.blocks:
            owner, remaining, pending = entry["blocks"][block.block_id]
            current_pending = {
                (p.request_id, p.deadline_unix_ms) for p in block.pending
            }
            if (
                _owner(block.issued_by) != owner
                or not pending.issubset(current_pending)
                or block.undispatched > remaining
                or block.undispatched > entry["remaining"][block.block_id]
            ):
                raise ReviewInputError("cold namespace block or unknown debit changed")
            if block.block_id not in (entry["ordinary"], entry["terminal"]) and (
                block.undispatched != remaining or current_pending != pending
            ):
                raise ReviewInputError("cold namespace borrowed another tranche")
        if caller._terminal_reason is None:
            if caller._block != entry["ordinary"]:
                raise ReviewInputError("cold namespace ordinary block changed")
            terminal = next(b for b in state.blocks if b.block_id == entry["terminal"])
            if terminal.undispatched != entry["blocks"][entry["terminal"]][1]:
                raise ReviewInputError("cold namespace terminal reservation changed")
        elif caller._terminal_reason == "action-completion":
            if caller._block != entry["terminal"]:
                raise ReviewInputError("cold namespace terminal block changed")
            frame = inspect.currentframe()
            while frame is not None and frame.f_code is not source["close"][1]:
                frame = frame.f_back
            original("close", frame)
            assert frame is not None
            proof = frame.f_locals.get("proof")
            if (
                frame.f_locals.get("self") is not caller
                or proof is not cast(Any, caller)._action_result_proof
                or proof.boundary is not cast(Any, caller)._action_effect_boundary
                or hashlib.sha256(
                    canonical_bytes(
                        {"manifest": proof.manifest, "outcome": proof.outcome}
                    )
                ).hexdigest()
                != cast(Any, caller)._action_result_fingerprint
            ):
                raise ReviewInputError(
                    "cold namespace terminal completion source changed"
                )
        else:
            raise ReviewInputError("cold namespace terminal purpose changed")
        if (
            type(caller._sequence) is not int
            or caller._sequence != entry["last"]
            or observed.get(caller) != caller._sequence
        ):
            raise ReviewInputError("cold namespace physical sequence changed")
        return entry

    def prepare(run, authority, target):
        from . import quarantine_action
        from .guarded_pending_publication import _original_accounting_graph
        from .quarantine_origin import require_authenticated_read_owner

        require_module_seal(quarantine_action)
        frame = cast(Any, inspect.currentframe()).f_back
        if (
            frame.f_code is not quarantine_action.prepare_quarantine_target.__code__
            or frame.f_globals is not vars(quarantine_action)
            or any(
                frame.f_locals.get(name) is not value
                for name, value in (
                    ("run", run),
                    ("authority", authority),
                    ("target", target),
                )
            )
        ):
            raise ReviewInputError(
                "cold namespace requires original target preparation"
            )
        caller = run.caller
        require_instance_source(
            caller, methods=("open", "close_action", "_require_ledger")
        )
        state = caller.accounting
        _original_accounting_graph(state, models=True)
        require_authenticated_read_owner(
            target.authenticated, run.ledger, caller, run.identity
        )
        ordinary, terminal = caller._block, caller._terminal_block
        blocks = {b.block_id: b for b in state.blocks}
        if (
            active.get(caller) is not None
            or type(caller) is not source["caller_type"]
            or state.action is None
            or state.action["phase"] != "pending"
            or state.action["execution_started"] is not False
            or authority.run is not run
            or _owner(authority.owner) != _owner(state.owner)
            or ordinary == terminal
            or ordinary not in blocks
            or terminal not in blocks
            or any(blocks[key].issued_by != state.owner for key in (ordinary, terminal))
            or caller._terminal_reason is not None
            or type(caller._sequence) is not int
            or not 0 <= caller._sequence <= MAX_API_REQUEST_LIMIT
            or observed.get(caller) != caller._sequence
        ):
            raise ReviewInputError("cold namespace preparation scope changed")
        counts = tuple(blocks[key].undispatched for key in (ordinary, terminal))
        ceiling = caller._sequence + sum(counts)
        if ceiling > MAX_API_REQUEST_LIMIT:
            raise ReviewInputError(
                "cold namespace sequence ceiling exceeds representation"
            )
        from .publication_recovery import _publication_transport

        guard = getattr(
            _publication_transport(run.http), "_quarantine_capacity_guard", None
        )
        token = _Namespace()
        issued[token] = dict(
            caller=caller,
            ledger=run.ledger,
            session_identity=run.identity,
            owner=_owner(state.owner),
            configuration=_configuration(state.configuration),
            identity=state.identity,
            intent=canonical_bytes(state.action["intent"]),
            ordinary=ordinary,
            terminal=terminal,
            unallocated=state.remaining,
            prior=state.prior_nonstartup_liability,
            blocks={
                b.block_id: (
                    _owner(b.issued_by),
                    b.undispatched,
                    frozenset((p.request_id, p.deadline_unix_ms) for p in b.pending),
                )
                for b in state.blocks
            },
            remaining={b.block_id: b.undispatched for b in state.blocks},
            start=caller._sequence,
            last=caller._sequence,
            ceiling=ceiling,
            counts=counts,
            state=state,
            state_bytes=canonical_bytes(state.to_document()),
            confirmed_receipts={},
            recording_confirmations=guard is not None and guard.complete_tail is False,
        )
        active[caller] = token
        _require(token, caller)
        return token

    def before_sequence(caller, deadline):
        token = active.get(caller)
        frame = cast(Any, inspect.currentframe()).f_back
        saved = source.get("open")
        genuine = (
            saved is not None
            and type(caller) is source["caller_type"]
            and frame.f_code is saved[1]
            and frame.f_globals is saved[2]
            and frame.f_locals.get("self") is caller
            and frame.f_locals.get("deadline") is deadline
            and _function(saved[0]) == saved
        )
        if genuine and validate_current(caller, deadline) and token is not None:
            raise ReviewInputError("current and historical namespaces cannot coexist")
        if token is None:
            # Observation is not authority. An incomplete or altered original
            # sequence history can never seed a later namespace projection.
            if not genuine:
                return
            previous = observed.get(caller, 0)
            sequence = caller._sequence
            if (
                type(previous) is not int
                or type(sequence) is not int
                or sequence != previous
                or not 0 <= sequence < MAX_API_REQUEST_LIMIT
            ):
                observed[caller] = None
            else:
                observed[caller] = sequence + 1
            return
        original("open", frame)
        entry = _require(token, caller)
        from . import quarantine_command_successor

        require_module_seal(quarantine_command_successor)
        quarantine_command_successor.before_request(caller)
        from . import quarantine_historical_execution

        require_module_seal(quarantine_historical_execution)
        quarantine_historical_execution.before_request(caller)
        entry["last"] = _next_sequence(caller._sequence, entry["ceiling"], deadline)
        observed[caller] = entry["last"]
        entry["remaining"] = {
            b.block_id: b.undispatched for b in caller.accounting.blocks
        }

    def project(token, caller):
        entry = _require(token, caller)
        state = entry["state"]
        if canonical_bytes(state.to_document()) != entry["state_bytes"]:
            raise ReviewInputError("cold namespace original accounting changed")
        projected = state
        sequence = entry["start"]
        for key, count in zip(
            (entry["ordinary"], entry["terminal"]), entry["counts"], strict=True
        ):
            for _ in range(count):
                sequence += 1
                projected = projected.dispatch(
                    key,
                    PendingApiRequest(
                        f"c-{state.owner.generation:x}-{sequence:x}",
                        MAX_API_REQUEST_LIMIT,
                    ),
                )
        return projected.to_document()

    def require(token, caller):
        _require(token, caller)

    def bind_response_confirmation(
        caller, function, pending, observation, response, context
    ):
        token = active.get(caller)
        if token is None or not issued.get(token, {}).get("recording_confirmations"):
            return
        frame = sys._getframe(1)
        original("open", frame)
        entry = _require(token, caller)
        values = cast(Any, frame.f_locals)
        wrapper = values.get("accounted_response")
        if (
            values.get("self") is not caller
            or values.get("namespace_confirmation_context") is not context
            or values.get("confirmed") is not function
            or values.get("pending") is not pending
            or values.get("observation") is not observation
            or values.get("response") is not response
            or type(wrapper) is not source["response_type"]
            or wrapper._confirmed is not function
            or wrapper._response is not response
            or function.__code__ is not source["confirmed_code"]
            or function.__globals__ is not source["open"][2]
            or function.__defaults__ is not None
            or function.__kwdefaults__ is not None
            or pending is None
            or caller.observations[-1] is not observation
            or observation.get("outcome") != "unknown"
            or not any(
                p is pending for b in caller.accounting.blocks for p in b.pending
            )
            or context
            != (
                caller._sequence,
                caller._block,
                pending.request_id,
                pending.deadline_unix_ms,
                observation.get("mutation"),
                context[5]
                if isinstance(context, tuple) and len(context) == 8
                else None,
                values["request"].get_method(),
                values["request"].full_url,
            )
            or type(context[5]) is not bool
            or function in confirmations
        ):
            raise ReviewInputError(
                "cold response binding lacks original object custody"
            )
        require_instance_source(wrapper, methods=("read",))
        closure = tuple(
            (cell, cell.cell_contents) for cell in function.__closure__ or ()
        )
        confirmations[function] = dict(
            entry=entry,
            caller=caller,
            function=_function(function),
            closure=closure,
            wrapper=wrapper,
            response=response,
            pending=pending,
            pending_bytes=canonical_bytes(
                {"id": pending.request_id, "deadline": pending.deadline_unix_ms}
            ),
            observation=observation,
            before=canonical_bytes(observation),
            status=values.get("observed_status"),
            context=context,
        )

    def confirm_response(caller, function, pending, observation):
        token = active.get(caller)
        if token is None or not issued.get(token, {}).get("recording_confirmations"):
            return
        frame = sys._getframe(1)
        entry = _require(token, caller)
        values = cast(Any, frame.f_locals)
        if (
            frame.f_code is not source["confirmed_code"]
            or frame.f_globals is not source["open"][2]
            or values.get("self") is not caller
            or values.get("confirmed") is not function
            or values.get("pending") is not pending
            or values.get("observation") is not observation
        ):
            raise ReviewInputError("cold confirmation lacks original nested source")
        saved = confirmations.get(function)
        previous = frame.f_back
        if saved is None:
            # Original HTTPError confirmation retains its ordinary accounting
            # semantics, but is never a successful preparation receipt.
            if (
                previous is not None
                and previous.f_code is source["open"][1]
                and previous.f_globals is source["open"][2]
                and previous.f_locals.get("confirmed") is function
            ):
                return
            raise ReviewInputError("cold confirmation callable is unissued or replayed")
        if (
            saved["entry"] is not entry
            or saved["caller"] is not caller
            or _function(function) != saved["function"]
            or any(cell.cell_contents is not value for cell, value in saved["closure"])
            or previous is None
            or previous.f_code is not source["response_read"][1]
            or previous.f_globals is not source["response_read"][2]
            or id(previous.f_locals.get("self")) != id(saved["wrapper"])
            or previous.f_locals.get("confirmed") is not function
            or saved["wrapper"]._confirmed is not function
            or saved["wrapper"]._response is not saved["response"]
            or pending is not saved["pending"]
            or observation is not saved["observation"]
            or canonical_bytes(
                {"id": pending.request_id, "deadline": pending.deadline_unix_ms}
            )
            != saved["pending_bytes"]
            or values.get("observed_status") != saved["status"]
        ):
            raise ReviewInputError("cold confirmation object or closure changed")
        require_instance_source(saved["wrapper"], methods=("read",))
        import json

        expected = json.loads(saved["before"])
        expected.update(
            outcome="response",
            uncertain_effect=bool(expected.get("mutation"))
            and (
                saved["status"] in {202, 409, 422, 429}
                or isinstance(saved["status"], int)
                and saved["status"] >= 500
            ),
        )
        if canonical_bytes(observation) != canonical_bytes(expected):
            raise ReviewInputError("cold confirmation observation changed")
        if pending not in caller.accounting.completed or any(
            p == pending for b in caller.accounting.blocks for p in b.pending
        ):
            raise ReviewInputError("cold confirmation accounting is unavailable")
        if pending.request_id in entry["confirmed_receipts"]:
            raise ReviewInputError("cold confirmation receipt was replayed")
        confirmations.pop(function)
        entry["confirmed_receipts"][pending.request_id] = (
            observation,
            canonical_bytes(observation),
            pending.deadline_unix_ms,
            saved["status"],
            saved["context"][5],
        )

    def capture_paid_prefix(caller):
        """Reconstruct a genuine completed preparation; no execution authority."""
        from . import quarantine_command_suffix

        require_module_seal(quarantine_command_suffix)
        frame = sys._getframe(1)
        if (
            caller is None
            or frame.f_code is not quarantine_command_suffix.prepare.__code__
            or frame.f_globals is not vars(quarantine_command_suffix)
            or id(getattr(frame.f_locals.get("run"), "caller", None)) != id(caller)
            or id(frame.f_locals.get("state")) != id(caller.accounting)
        ):
            raise ReviewInputError("cold paid prefix requires original preparation")
        from . import quarantine_command_finalization

        require_module_seal(quarantine_command_finalization)
        saved = quarantine_command_finalization.require_observation(
            frame.f_locals.get("observation")
        )
        if saved["run"] is not frame.f_locals["run"] or saved[
            "target"
        ] is not frame.f_locals.get("target"):
            raise ReviewInputError("cold paid prefix observation changed")
        entry = _require(active.get(caller), caller)
        if canonical_bytes(entry["state"].to_document()) != entry["state_bytes"]:
            raise ReviewInputError("cold namespace original accounting changed")
        if "paid_projection" in entry:
            raise ReviewInputError("cold paid prefix was already captured")
        start = frame.f_locals.get("before_preparation")
        count = frame.f_locals.get("preparation_calls")
        if (
            type(start) is not int
            or type(count) is not int
            or not 0 < count <= frame.f_locals.get("preparation_ceiling", 0)
            or len(caller.observations) != start + count
            or observed.get(caller) != entry["start"] + count
            or caller._sequence != entry["start"] + count
            or caller._terminal_reason is not None
        ):
            raise ReviewInputError("cold paid prefix sequence is unsupported")
        prefix = caller.observations[start:]
        reconstructed = entry["state"]
        for offset, item in enumerate(prefix, 1):
            identifier = (
                f"c-{reconstructed.owner.generation:x}-{entry['start'] + offset:x}"
            )
            receipt = entry["confirmed_receipts"].get(identifier)
            if (
                receipt is None
                or receipt[0] is not item
                or receipt[1] != canonical_bytes(item)
                or receipt[2] != item.get("deadline_unix_ms")
                or type(receipt[3]) is not int
                or not 200 <= receipt[3] < 300
                or item.get("request_id") != identifier
                or item.get("outcome") != "response"
                or (item.get("mutation") is not False and receipt[4] is not True)
                or item.get("uncertain_effect")
                or type(item.get("deadline_unix_ms")) is not int
            ):
                raise ReviewInputError("cold paid prefix response is unconfirmed")
            request = PendingApiRequest(identifier, item["deadline_unix_ms"])
            reconstructed = reconstructed.dispatch(entry["ordinary"], request).confirm(
                request
            )
        state_bytes = canonical_bytes(caller.accounting.to_document())
        if canonical_bytes(reconstructed.to_document()) != state_bytes:
            raise ReviewInputError("cold paid prefix accounting changed")
        projected = reconstructed
        sequence = caller._sequence
        for block in reconstructed.blocks:
            if block.block_id not in (entry["ordinary"], entry["terminal"]):
                continue
            for _ in range(block.undispatched):
                sequence += 1
                projected = projected.dispatch(
                    block.block_id,
                    PendingApiRequest(
                        f"c-{reconstructed.owner.generation:x}-{sequence:x}",
                        MAX_API_REQUEST_LIMIT,
                    ),
                )
        if sequence != entry["ceiling"]:
            raise ReviewInputError("cold paid prefix changed future namespace")
        entry["paid_state"] = state_bytes
        entry["paid_sequence"] = caller._sequence
        entry["paid_observations"] = tuple(
            (item, canonical_bytes(item)) for item in prefix
        )
        entry["paid_projection"] = canonical_bytes(projected.to_document())
        entry["recording_confirmations"] = False

    def project_prepared_prefix(caller):
        """Conditional detached data; original baseline projection is unchanged."""
        import json

        entry = _require(active.get(caller), caller)
        if "paid_projection" not in entry:
            return project(active.get(caller), caller)
        if (
            canonical_bytes(caller.accounting.to_document()) != entry["paid_state"]
            or caller._sequence != entry["paid_sequence"]
            or observed.get(caller) != entry["paid_sequence"]
            or any(
                canonical_bytes(item) != raw for item, raw in entry["paid_observations"]
            )
        ):
            raise ReviewInputError("cold paid prefix is stale or changed")
        return json.loads(entry["paid_projection"])

    def project_current(caller):
        return project(active.get(caller), caller)

    return (
        install,
        prepare,
        before_sequence,
        project,
        project_current,
        bind_response_confirmation,
        confirm_response,
        capture_paid_prefix,
        project_prepared_prefix,
        require,
        observed_sequence,
        install_current_validator,
    )


(
    install,
    prepare,
    before_sequence,
    project,
    project_current,
    bind_response_confirmation,
    confirm_response,
    capture_paid_prefix,
    project_prepared_prefix,
    require,
    observed_sequence,
    install_current_validator,
) = _registry()
del _registry

from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
