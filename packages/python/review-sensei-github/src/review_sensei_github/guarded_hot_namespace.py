"""Negative-only hot request namespace custody, never funding or admission.

No accounting projection consumes this witness. The original generic 128-byte
bound remains unchanged. A later consumer needs separate complete-prefix proof.
"""

import sys
import threading
from contextvars import ContextVar
from functools import wraps
from typing import Any, cast
from weakref import WeakKeyDictionary

from review_sensei.api_work import MAX_API_REQUEST_LIMIT, ApiRunAccounting
from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.source_registry import require_instance_source, require_module_seal


def _function(function):
    return (
        function,
        function.__code__,
        function.__globals__,
        function.__defaults__,
        function.__kwdefaults__,
    )


def _definition(name):
    frame = sys._getframe(2)
    module = sys.modules.get(name)
    spec = getattr(module, "__spec__", None)
    loader = getattr(spec, "loader", None)
    if (
        module is None
        or frame.f_globals is not vars(module)
        or frame.f_locals is not frame.f_globals
        or not getattr(spec, "_initializing", False)
        or not callable(getattr(loader, "get_code", None))
        or cast(Any, loader).get_code(name) != frame.f_code
    ):
        raise ReviewInputError("hot namespace requires defining loader")
    return module


def _registry():
    sources: dict[str, Any] = {}
    active: WeakKeyDictionary[Any, dict[str, Any]] = WeakKeyDictionary()
    context: ContextVar[Any] = ContextVar("original_hot_namespace", default=None)

    def install_caller(cls):
        module = _definition("review_sensei_github.api_run_requests")
        if "caller" in sources or cls is not module.ApiRunRequestTransport:
            raise ReviewInputError("hot namespace caller source changed")
        sources.update(
            caller=cls,
            open=_function(cls.open),
            accept=_function(cls._accept_checkpoint),
        )

    def install_application(cls):
        module = _definition("review_sensei_github.application")
        if "application" in sources or cls is not module.GitHubApplication:
            raise ReviewInputError("hot namespace application source changed")
        original = cls.publish_review
        sources["application"] = _function(original)
        sources["application_type"] = cls

        @wraps(original)
        def published(app, **kwargs):
            scope = {"app": app, "thread": threading.get_ident(), "entry": None}
            token = context.set(scope)
            try:
                return original(app, **kwargs)
            finally:
                entry = scope["entry"]
                if entry is not None:
                    active.pop(entry["caller"], None)
                    entry["expired"] = True
                    entry.pop("application_frame", None)
                context.reset(token)

        cls.publish_review = published

    def source(kind, frame, caller):
        from . import api_run_requests

        require_module_seal(sys.modules[__name__])
        require_module_seal(api_run_requests)
        saved = sources[kind]
        if (
            type(caller) is not sources["caller"]
            or frame.f_code is not saved[1]
            or frame.f_globals is not saved[2]
            or frame.f_locals.get("self") is not caller
            or _function(saved[0]) != saved
        ):
            raise ReviewInputError("hot namespace original caller frame changed")
        require_instance_source(
            caller, methods=("open", "_accept_checkpoint", "_require_ledger")
        )

    def require(entry: dict[str, Any], caller: Any):
        from . import (
            application,
            guarded_pending_publication,
            quarantine_command_namespace,
        )

        require_module_seal(application)
        require_module_seal(guarded_pending_publication)
        require_module_seal(quarantine_command_namespace)
        scope = context.get()
        if (
            entry["expired"]
            or scope is not entry["scope"]
            or scope["thread"] != threading.get_ident()
            or active.get(caller) is not entry
        ):
            raise ReviewInputError("hot namespace copied or expired scope")
        frame: Any = sys._getframe(1)
        while frame is not None and frame is not entry["application_frame"]:
            frame = frame.f_back
        if frame is None:
            raise ReviewInputError("hot namespace original application extent changed")
        state = caller.accounting
        guarded_pending_publication._original_accounting_graph(state, models=True)
        if (
            canonical_bytes(vars(state.owner)) != entry["owner"]
            or canonical_bytes(vars(state.configuration)) != entry["configuration"]
            or caller._block not in entry["blocks"]
            or caller._terminal_block != entry["blocks"][1]
            or caller._root != entry["root"]
            or caller._effect_record is not entry["record"]
            or type(caller._sequence) is not int
            or not entry["start"] <= caller._sequence <= MAX_API_REQUEST_LIMIT
            or quarantine_command_namespace.observed_sequence(caller)
            != caller._sequence
        ):
            raise ReviewInputError(
                "hot namespace owner root tranche or sequence changed"
            )
        if canonical_bytes(entry["initial"].to_document()) != entry["initial_bytes"]:
            raise ReviewInputError("hot namespace initial accounting changed")
        entry["initial"].require_successor(state)
        return entry

    def begin(publisher, journal, host):
        from review_sensei import api_work

        from . import (
            application,
            guarded_pending_publication,
            publication,
            quarantine_capacity,
            quarantine_command_namespace,
        )

        for module in (api_work, quarantine_capacity, quarantine_command_namespace):
            require_module_seal(module)

        for module in (application, publication, guarded_pending_publication):
            require_module_seal(module)
        frame = sys._getframe(1)
        method = publication.ReviewPublisher._publish_impl
        require_instance_source(publisher, methods=("_publish_impl",))
        while hasattr(method, "__wrapped__"):
            method = method.__wrapped__
        scope = context.get()
        if (
            frame.f_code is not method.__code__
            or frame.f_globals is not vars(publication)
            or frame.f_locals.get("self") is not publisher
            or frame.f_locals.get("lifecycle_journal") is not journal
            or frame.f_locals.get("native_host") is not host
            or scope is None
            or scope["thread"] != threading.get_ident()
            or scope["entry"] is not None
        ):
            raise ReviewInputError("hot namespace requires original grouped producer")
        if (
            journal is None
            or journal.effects
            or journal._record.quarantine_lineage is None
        ):
            return
        if type(scope["app"]) is not sources["application_type"]:
            raise ReviewInputError("hot namespace application owner changed")
        require_instance_source(scope["app"], methods=("publish_review",))
        application_frame: Any = frame
        while application_frame is not None and not (
            application_frame.f_code is sources["application"][1]
            and application_frame.f_globals is sources["application"][2]
            and application_frame.f_locals.get("self") is scope["app"]
        ):
            application_frame = application_frame.f_back
        if application_frame is None:
            raise ReviewInputError("hot namespace original application frame is absent")
        ledger, record = journal.ledger, journal._record
        caller = ledger.caller
        if caller in active:
            raise ReviewInputError("hot namespace cannot nest")
        require_instance_source(
            caller, methods=("open", "_require_ledger", "_accept_checkpoint")
        )
        caller._require_ledger(ledger, journal.identity)
        quarantine_capacity.installed_capacity(ledger, journal.identity)
        guarded_pending_publication._original_accounting_graph(
            caller.accounting, models=True
        )
        initial = ApiRunAccounting.from_document(record.api_run_accounting)
        if initial.configuration.limit != 512:
            return
        blocks = (caller._block, caller._terminal_block)
        selected = tuple(
            next((b for b in initial.blocks if b.block_id == key), None)
            for key in blocks
        )
        if (
            initial.configuration.limit != 512
            or len(set(blocks)) != 2
            or any(b is None or b.issued_by != initial.owner for b in selected)
            or initial.owner != caller.accounting.owner
            or record.record_sha256 != caller._root
            or caller._effect_record is not record
            or cast(Any, scope["app"]).session_ledger is not ledger
        ):
            raise ReviewInputError("hot namespace initial producer custody changed")
        initial.require_successor(caller.accounting)
        sequence = quarantine_command_namespace.observed_sequence(caller)
        if not 0 <= sequence < MAX_API_REQUEST_LIMIT:
            raise ReviewInputError("hot namespace initial sequence is unavailable")
        entry = dict(
            scope=scope,
            application_frame=application_frame,
            caller=caller,
            ledger=ledger,
            root=record.record_sha256,
            record=record,
            initial=initial,
            initial_bytes=canonical_bytes(initial.to_document()),
            owner=canonical_bytes(vars(initial.owner)),
            configuration=canonical_bytes(vars(initial.configuration)),
            blocks=blocks,
            start=sequence,
            expired=False,
        )
        scope["entry"] = entry
        active[caller] = entry
        require(entry, caller)

    def before_sequence(caller, deadline):
        entry = active.get(caller)
        scope = context.get()
        if entry is None:
            if scope is not None and scope["entry"] is not None:
                raise ReviewInputError("hot namespace foreign caller in original extent")
            return
        frame = sys._getframe(1)
        source("open", frame, caller)
        if frame.f_locals.get("deadline") is not deadline:
            raise ReviewInputError("hot namespace original deadline changed")
        require(entry, caller)
        if (
            type(deadline) is not int
            or not 0 < deadline <= MAX_API_REQUEST_LIMIT
            or caller._sequence >= MAX_API_REQUEST_LIMIT
        ):
            raise ReviewInputError("hot namespace finite request range exceeded")

    def confirm_checkpoint(caller: Any, ledger: Any, record: Any, persisted: Any):
        entry = active.get(caller)
        scope = context.get()
        if entry is None:
            if scope is not None and scope["entry"] is not None:
                raise ReviewInputError("hot namespace foreign caller in original extent")
            return
        frame = sys._getframe(1)
        source("accept", frame, caller)
        require(entry, caller)
        if record is None:
            raise ReviewInputError("hot namespace checkpoint record is absent")
        if (
            frame.f_locals.get("ledger") is not ledger
            or ledger is not entry["ledger"]
            or frame.f_locals.get("record") is not record
            or frame.f_locals.get("persisted") is not persisted
            or caller._checkpoint is None
            or caller._checkpoint.record_sha256 != cast(Any, record).record_sha256
            or cast(Any, record).generation != entry["record"].generation + 1
        ):
            raise ReviewInputError("hot namespace positive checkpoint changed")
        # The original caller has already checked persisted.require_successor.
        # This changes root custody only, never the initial complete future bound.
        entry["root"], entry["record"] = cast(Any, record).record_sha256, record

    return (
        install_caller,
        install_application,
        begin,
        before_sequence,
        confirm_checkpoint,
    )


install_caller, install_application, begin, before_sequence, confirm_checkpoint = (
    _registry()
)
del _registry

from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
