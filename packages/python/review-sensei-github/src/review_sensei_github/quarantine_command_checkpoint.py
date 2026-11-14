"""Source-owned adjacent census/first-GET reuse for inline historical commands.

This removes one duplicate read, not a writer fence or admission obligation.
Guarded commands remain unsupported until a separately qualified route exists.
"""

import inspect
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, cast
from weakref import WeakKeyDictionary

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.source_registry import require_instance_source, require_module_seal


def _registry():
    sources: dict[str, Any] = {}
    active: ContextVar[Any] = ContextVar("quarantine_command_checkpoint", default=None)
    censuses: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()

    class Census:
        pass

    def install_source(kind, function):
        names = {
            "census": ("packed_comment_census", "fetch_packed_comments"),
            "get": ("quarantine_checkpoint", "_CheckpointSnapshot.get"),
            "open": ("api_run_requests", "ApiRunRequestTransport.open"),
        }
        suffix, qualified = names[kind]
        module = sys.modules.get("review_sensei_github." + suffix)
        spec = getattr(module, "__spec__", None)
        loader = getattr(spec, "loader", None)
        frame = cast(Any, inspect.currentframe()).f_back
        if (
            kind in sources
            or module is None
            or frame.f_globals is not vars(module)
            or frame.f_locals is not frame.f_globals
            or not getattr(spec, "_initializing", False)
            or not callable(getattr(loader, "get_code", None))
            or cast(Any, loader).get_code(module.__name__) != frame.f_code
            or function.__module__ != module.__name__
            or function.__qualname__ != qualified
            or function.__globals__ is not vars(module)
        ):
            raise ReviewInputError("cold checkpoint hooks require defining source")
        sources[kind] = (function, function.__code__, vars(module))

    def original(kind, frame):
        source = sources.get(kind)
        if (
            source is None
            or source[0].__code__ is not source[1]
            or frame is None
            or frame.f_code is not source[1]
            or frame.f_globals is not source[2]
        ):
            raise ReviewInputError("cold checkpoint hook source changed")
        require_module_seal(sys.modules[__name__])

    @contextmanager
    def phase(journal):
        from . import application
        from .publication_recovery import _publication_transport
        from .quarantine_capacity import native_reserve
        from .quarantine_origin import require_authenticated_read_owner

        ledger = journal.ledger
        reviewer = journal.reviewer
        record = journal._record
        identity = journal.identity
        caller = getattr(ledger, "caller", None)
        authenticated = getattr(ledger, "_quarantine_authenticated", None)
        from . import quarantine_historical_execution

        require_module_seal(quarantine_historical_execution)
        closed = quarantine_historical_execution.bind_journal(journal)
        if authenticated is None or (
            native_reserve(authenticated.archive["target_isolation"]) is not None
            and not closed
        ):
            yield
            return
        if (
            journal._record.finding_lifecycle["manifest"]["encoding"]
            != "inline-json-v1"
        ):
            yield
            return
        outer = inspect.currentframe()
        method = application.GitHubApplication._refinalize_command
        while outer is not None and outer.f_code is not method.__code__:
            outer = outer.f_back
        if (
            outer is None
            or outer.f_globals is not vars(application)
            or outer.f_locals.get("journal") is not journal
            or outer.f_locals["self"].session_ledger is not ledger
            or outer.f_locals["self"].reviewer is not reviewer
            or caller is None
            or record is not caller._effect_record
            or active.get() is not None
            or (
                getattr(
                    _publication_transport(ledger.http),
                    "_quarantine_capacity_guard",
                    None,
                )
                is not None
                and not closed
            )
        ):
            raise ReviewInputError(
                "cold inline checkpoint requires original command phase"
            )
        require_instance_source(
            outer.f_locals["self"], methods=("_refinalize_command",)
        )
        require_authenticated_read_owner(authenticated, ledger, caller, identity)
        entry = {
            "journal": journal,
            "ledger": ledger,
            "caller": caller,
            "receipt": None,
            "state": None,
            "coalesced": 0,
        }
        reset = active.set(entry)
        try:
            yield
        finally:
            active.reset(reset)
            entry["receipt"] = entry["state"] = None

    def begin_census(ledger, identity, plan):
        entry = active.get()
        scope = getattr(ledger, "_quarantine_lifecycle_capacity", None)
        if (
            entry is None
            or entry["ledger"] is not ledger
            or scope is None
            or scope.parts != 0
        ):
            return None
        original("census", cast(Any, inspect.currentframe()).f_back)
        from .quarantine_census import _LifecycleCapacity

        _LifecycleCapacity._require_scope(scope, ledger, identity)
        if plan is not scope.plan:
            raise ReviewInputError("cold census lifetime allocation plan changed")
        caller = entry["caller"]
        operation = caller._checkpoint_operation
        if operation is None or caller._active_operation is not operation:
            raise ReviewInputError(
                "cold census lacks its original checkpoint operation"
            )
        token = Census()
        censuses[token] = (entry, scope, operation, len(caller.observations))
        return token

    def completed_census(token, rows):
        if token is None:
            return rows
        frame = cast(Any, inspect.currentframe()).f_back
        original("census", frame)
        saved = censuses.pop(token, None)
        if saved is None or active.get() is not saved[0]:
            raise ReviewInputError("cold completed census is copied or expired")
        entry, scope, operation, before = saved
        caller, ledger = entry["caller"], entry["ledger"]
        from .quarantine_census import _LifecycleCapacity
        from .session_ledger import parse_session_comment

        _LifecycleCapacity._require_scope(scope, ledger, scope.identity)
        if (
            frame.f_locals.get("collected") is not rows
            or caller._active_operation is not operation
            or caller._checkpoint_operation is not operation
            or not 2 <= len(caller.observations) - before <= 10
            or frame.f_locals.get("calls") != len(caller.observations) - before
        ):
            raise ReviewInputError("cold complete census physical custody changed")
        selected = []
        for row in rows:
            record = parse_session_comment(row["body"], identity=scope.identity)
            if record is not None and record.record_sha256 == scope.root:
                ledger._part_owner(row, scope.identity)
                if canonical_bytes(record.to_dict()) != scope.record_bytes:
                    raise ReviewInputError("cold complete census root changed")
                selected.append(row)
        if len(selected) != 1:
            raise ReviewInputError("cold complete census root is not unique")
        row = selected[0]
        entry["receipt"] = (
            scope,
            operation,
            row["id"],
            row["body"],
            row["user"]["id"],
            len(caller.observations),
            caller.usable,
        )
        return rows

    def before_get(snapshot, *, candidate):
        entry = active.get()
        if entry is None or entry["ledger"] is not snapshot.ledger:
            return None, None
        original("get", cast(Any, inspect.currentframe()).f_back)
        state, receipt = entry["state"], entry["receipt"]
        if not candidate and snapshot.candidate is None and receipt is not None:
            from .quarantine_census import _LIFECYCLE_ACTIVE, _LifecycleCapacity
            from .quarantine_origin import require_authenticated_read_owner

            entry["receipt"] = None
            scope, operation, identifier, body, owner, observations, usable = receipt
            caller, ledger = entry["caller"], entry["ledger"]
            snapshot.require()
            authenticated = ledger._quarantine_authenticated
            require_authenticated_read_owner(
                authenticated, ledger, caller, snapshot.identity
            )
            issued = _LIFECYCLE_ACTIVE.get(scope)
            if (
                issued is None
                or issued[0] != _LifecycleCapacity._seal(scope)
                or issued[1] is not operation
                or ledger._quarantine_lifecycle_capacity is not scope
                or scope.caller is not caller
                or scope.parts != 0
                or scope.root != snapshot.root
                or scope.root != caller._root
                or snapshot.operation is not operation
                or caller._active_operation is not operation
                or snapshot.comment_id != identifier
                or snapshot.body != body
                or snapshot.owner_id != owner
                or snapshot.caller is not caller
                or snapshot.identity != scope.identity
                or entry["journal"]._record is not caller._effect_record
                or canonical_bytes(snapshot.record.to_dict()) != scope.record_bytes
                or authenticated.record_sha256 != scope.root
                or len(caller.observations) != observations
                or caller.usable != usable
            ):
                raise ReviewInputError(
                    "cold census first-root receipt is stale or foreign"
                )
            entry["state"] = [snapshot, "await-second-get"]
            entry["coalesced"] += 1
            return None, snapshot.record
        if (
            state is not None
            and state[0] is snapshot
            and state[1] == "await-second-get"
        ):
            if (
                candidate
                or snapshot.candidate is None
                or snapshot.candidate_body is None
            ):
                raise ReviewInputError("cold original second root GET is absent")
            return state, None
        return None, None

    def before_request(caller, request):
        entry = active.get()
        if entry is None or entry["caller"] is not caller:
            return
        state = entry["state"]
        if state is None or state[1] == "verified":
            return
        original("open", cast(Any, inspect.currentframe()).f_back)
        snapshot = state[0]
        frame = inspect.currentframe()
        found = False
        while frame is not None:
            if (
                frame.f_code is sources["get"][1]
                and frame.f_globals is sources["get"][2]
                and frame.f_locals.get("self") is snapshot
                and frame.f_locals.get("candidate") is False
            ):
                found = True
            frame = frame.f_back
        expected = snapshot.ledger.http.api_url + snapshot.ledger.http.repository_path(
            snapshot.identity.repository, f"/issues/comments/{snapshot.comment_id}"
        )
        if (
            state[1] != "await-second-get"
            or not found
            or snapshot.candidate is None
            or request.get_method() != "GET"
            or request.full_url != expected
        ):
            raise ReviewInputError("cold inline checkpoint attempted intervening I/O")
        snapshot.require()
        state[1] = "dispatched"

    def after_get(token, snapshot, parsed):
        if token is None:
            return
        original("get", cast(Any, inspect.currentframe()).f_back)
        entry = active.get()
        if (
            entry is None
            or entry["state"] is not token
            or token[0] is not snapshot
            or token[1] != "dispatched"
            or canonical_bytes(parsed.to_dict())
            != canonical_bytes(snapshot.record.to_dict())
        ):
            raise ReviewInputError("cold original second root GET was not confirmed")
        snapshot.require()
        token[1] = "verified"

    def observation(ledger):
        entry = active.get()
        return (
            None
            if entry is None or entry["ledger"] is not ledger
            else {"coalesced": entry["coalesced"]}
        )

    return (
        install_source,
        phase,
        begin_census,
        completed_census,
        before_get,
        before_request,
        after_get,
        observation,
    )


(
    install_source,
    phase,
    begin_census,
    completed_census,
    before_get,
    before_request,
    after_get,
    observation,
) = _registry()
del _registry

from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
