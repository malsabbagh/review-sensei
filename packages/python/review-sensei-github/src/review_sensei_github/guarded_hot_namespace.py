"""Negative-only hot request namespace custody, never funding or admission.

No accounting projection consumes this witness. The original generic 128-byte
bound remains unchanged. A later consumer needs separate complete-prefix proof.
"""

import inspect
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


_RECORD_FIELDS = frozenset(
    (
        "repository",
        "pull_request",
        "repository_id",
        "completed_initial_reviews",
        "completed_verification_rounds",
        "failed_attempts",
        "generation",
        "reservation_id",
        "reserved_slot",
        "last_committed_reservation_id",
        "created_at",
        "updated_at",
        "expires_at",
        "record_sha256",
        "operator_paused",
        "failed_attempts_head_sha",
        "dispositions",
        "continuation_grants",
        "transaction",
        "convergence_history",
        "reservation_owner",
        "rescan_state",
        "assessment_queue",
        "disposition_snapshot",
        "api_run_accounting",
        "historical_assessments",
        "quarantine_lineage",
        "publication_staging",
        "finding_lifecycle",
        "trust_enrollment",
        "_digest_shape",
    )
)
_TRANSACTION_FIELDS = frozenset(
    (
        "transaction_id",
        "repository",
        "pull_request",
        "base_sha",
        "head_sha",
        "policy_digest",
        "configuration_digest",
        "evidence_digest",
        "reservation_id",
        "generation",
        "phase",
        "result_sha256",
    )
)


def _record_snapshot(record):
    """Detached finite original data, without invoking instance serializers."""
    from review_sensei import models, session

    require_module_seal(session)
    require_module_seal(models)
    if type(record) is not session.SessionRecord:
        raise ReviewInputError("hot checkpoint record source changed")
    count = 0
    size = 0

    def visit(item, ancestors, depth, model_allowed=None):
        nonlocal count, size
        count += 1
        if count > 100_000 or depth > 32:
            raise ReviewInputError("hot checkpoint graph is unbounded")
        kind = type(item)
        if item is None or kind is bool:
            return item
        if kind is str:
            size += len(item.encode("utf-8"))
            if size > 8 * 1024 * 1024:
                raise ReviewInputError("hot checkpoint graph is unbounded")
            return item
        if kind is int:
            if not -MAX_API_REQUEST_LIMIT <= item <= MAX_API_REQUEST_LIMIT:
                raise ReviewInputError("hot checkpoint integer is unbounded")
            return item
        if id(item) in ancestors:
            raise ReviewInputError("hot checkpoint graph is cyclic")
        ancestors = ancestors | {id(item)}
        if kind in (tuple, list):
            return [visit(value, ancestors, depth + 1) for value in item]
        if kind in (session.SessionRecord, models.ReviewTransaction):
            if kind is not model_allowed:
                raise ReviewInputError("hot checkpoint model placement changed")
            fields = (
                _RECORD_FIELDS if kind is session.SessionRecord else _TRANSACTION_FIELDS
            )
            raw = object.__getattribute__(item, "__dict__")
            if (
                type(raw) is not dict
                or any(type(key) is not str for key in raw)
                or set(raw) != fields
            ):
                raise ReviewInputError("hot checkpoint declared fields changed")
            item = raw
        elif kind is not dict:
            raise ReviewInputError("hot checkpoint graph contains non-original data")
        if any(type(key) is not str for key in item):
            raise ReviewInputError("hot checkpoint graph keys changed")
        return {
            key: visit(
                value,
                ancestors,
                depth + 1,
                models.ReviewTransaction
                if kind is session.SessionRecord and key == "transaction"
                else None,
            )
            for key, value in item.items()
        }

    return visit(record, set(), 0, session.SessionRecord)


def _checkpoint_generation_matches(previous, candidate):
    """Negative lineage check, never an own-CAS or writer capability."""
    from review_sensei import session

    require_module_seal(session)
    if (
        type(previous) is not session.SessionRecord
        or type(candidate) is not session.SessionRecord
    ):
        return False
    # Read only original declared scalar storage before the ordinary +1 branch.
    left_raw = object.__getattribute__(previous, "__dict__")
    right_raw = object.__getattribute__(candidate, "__dict__")
    for raw in (left_raw, right_raw):
        if type(raw) is not dict or any(type(key) is not str for key in raw):
            raise ReviewInputError("hot checkpoint raw storage changed")
    old_generation = left_raw.get("generation")
    new_generation = right_raw.get("generation")
    if type(old_generation) is not int or type(new_generation) is not int:
        return False
    if new_generation == old_generation + 1:
        return True
    if new_generation != old_generation:
        return False
    left, right = _record_snapshot(previous), _record_snapshot(candidate)
    for field in ("api_run_accounting", "updated_at", "record_sha256"):
        left.pop(field, None)
        right.pop(field, None)
    return canonical_bytes(left) == canonical_bytes(right)


def _registry():
    from pathlib import Path

    defining_module = sys.modules[__name__]
    defining_spec = defining_module.__spec__
    if defining_spec is None:
        raise ReviewInputError("hot namespace defining loader is unavailable")
    defining_loader = defining_spec.loader
    definition_file = sys._getframe().f_code.co_filename
    defining_origin = defining_spec.origin
    defining_file = getattr(defining_module, "__file__", None)
    defining_state = tuple(vars(defining_loader).items())

    def loader_binding(loader):
        descriptor = inspect.getattr_static(type(loader), "get_code", None)
        if (
            descriptor is None
            or inspect.getattr_static(loader, "get_code", None) is not descriptor
        ):
            raise ReviewInputError("hot namespace loader implementation changed")
        implementation = getattr(descriptor, "__func__", descriptor)
        return (
            type(loader),
            descriptor,
            getattr(implementation, "__code__", None),
            getattr(implementation, "__globals__", None),
            getattr(implementation, "__defaults__", None),
            getattr(implementation, "__kwdefaults__", None),
        )

    loader_source = loader_binding(defining_loader)

    def _definition(name):
        require_module_seal(defining_module)
        frame = sys._getframe(2)
        module = sys.modules.get(name)
        if type(module) is not type(defining_module):
            raise ReviewInputError("hot namespace defining module changed")
        spec = getattr(module, "__spec__", None)
        if type(spec) is not type(defining_spec):
            raise ReviewInputError("hot namespace defining spec changed")
        loader = getattr(spec, "loader", None)
        stem = name.rsplit(".", 1)[-1]
        expected = (
            f"<frozen {name}>"
            if definition_file == f"<frozen {__name__}>"
            else str(Path(definition_file).with_name(stem + ".py"))
        )

        def sibling(value):
            if value in (None, "frozen"):
                return value
            return str(Path(value).with_name(stem + Path(value).suffix))

        replacements = {
            __name__: name,
            definition_file: expected,
            defining_origin: sibling(defining_origin),
            defining_file: sibling(defining_file),
        }
        expected_state = {
            key: replacements.get(value, value) if type(value) is str else value
            for key, value in defining_state
        }
        # Authenticate loader family/code BEFORE asking it for source code or
        # touching its supplied state; a forged loader must never run a callback.
        # Reject foreign families and substituted descriptors without reading
        # any descriptor metadata: even __getattr__ can execute supplied code.
        if (
            type(loader) is not loader_source[0]
            or type(defining_loader) is not loader_source[0]
            or inspect.getattr_static(type(loader), "get_code", None)
            is not loader_source[1]
            or inspect.getattr_static(type(defining_loader), "get_code", None)
            is not loader_source[1]
            or loader_binding(loader) != loader_source
            or loader_binding(defining_loader) != loader_source
        ):
            raise ReviewInputError("hot namespace defining loader changed")
        actual_state = vars(loader)
        original_state = vars(defining_loader)
        state_intact = (
            set(actual_state) == set(expected_state)
            and set(original_state) == {key for key, _ in defining_state}
            and all(original_state[key] is value for key, value in defining_state)
            and all(
                (type(actual_state[key]) is type(value) and actual_state[key] == value)
                if type(value) in (str, int, bool, type(None))
                else actual_state[key] is value
                for key, value in expected_state.items()
            )
        )
        if (
            not state_intact
            or module is None
            or frame.f_globals is not vars(module)
            or frame.f_locals is not frame.f_globals
            or not getattr(spec, "_initializing", False)
            or getattr(spec, "origin", None) != sibling(defining_origin)
            or getattr(module, "__file__", None) != sibling(defining_file)
            or frame.f_code.co_filename != expected
        ):
            raise ReviewInputError("hot namespace requires defining loader")
        if cast(Any, loader).get_code(name) != frame.f_code:
            raise ReviewInputError("hot namespace defining code changed")
        return module

    sources: dict[str, Any] = {}
    active: WeakKeyDictionary[Any, dict[str, Any]] = WeakKeyDictionary()
    context: ContextVar[Any] = ContextVar("original_hot_namespace", default=None)

    def install_caller(cls):
        if "caller" in sources:
            raise ReviewInputError("hot namespace caller already installed")
        module = _definition("review_sensei_github.api_run_requests")
        if cls is not module.ApiRunRequestTransport:
            raise ReviewInputError("hot namespace caller source changed")
        sources.update(
            caller=cls,
            open=_function(cls.open),
            accept=_function(cls._accept_checkpoint),
            finish=_function(cls.finish_review_run),
        )

    def install_application(cls):
        if "application" in sources:
            raise ReviewInputError("hot namespace application already installed")
        module = _definition("review_sensei_github.application")
        if cls is not module.GitHubApplication:
            raise ReviewInputError("hot namespace application source changed")
        original = cls.publish_review
        sources["application"] = _function(original)
        sources["application_type"] = cls

    def install_run(cls):
        if "run" in sources:
            raise ReviewInputError("hot namespace run already installed")
        module = _definition("review_sensei_github.review_run")
        if cls is not module.ReviewRun:
            raise ReviewInputError("hot namespace run source changed")
        original = cls.execute
        sources["run"] = _function(original)
        sources["run_type"] = cls
        sources["close_run"] = _function(module.close_review_run)

        @wraps(original)
        def executed(run, *args, **kwargs):
            scope = {"run": run, "thread": threading.get_ident(), "entry": None}
            token = context.set(scope)
            try:
                return original(run, *args, **kwargs)
            finally:
                entry = scope["entry"]
                if entry is not None:
                    active.pop(entry["caller"], None)
                    entry["expired"] = True
                    entry.pop("run_frame", None)
                context.reset(token)

        cls.execute = executed

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
        from . import review_run

        require_module_seal(review_run)
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
        while frame is not None and frame is not entry["run_frame"]:
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
        if (
            tuple(b.block_id for b in state.blocks) != tuple(entry["tranches"])
            or state.remaining != entry["remaining"]
            or state.prior_nonstartup_liability != entry["prior"]
        ):
            raise ReviewInputError("hot namespace frozen funding changed")
        for block in state.blocks:
            before = entry["tranches"][block.block_id]
            if (
                block.issued_by != before.issued_by
                or block.undispatched > before.undispatched
                or not set(before.pending).issubset(set(block.pending))
            ):
                raise ReviewInputError(
                    "hot namespace old unknown or allocation changed"
                )
            if block.block_id not in entry["blocks"] and block != before:
                raise ReviewInputError("hot namespace borrowed protected funding")
        terminal = next(b for b in state.blocks if b.block_id == entry["blocks"][1])
        if caller._terminal_reason is None:
            if (
                caller._block != entry["blocks"][0]
                or terminal != entry["tranches"][terminal.block_id]
            ):
                raise ReviewInputError(
                    "hot namespace terminal funds were used before closure"
                )
        elif caller._terminal_reason == "publication-completion":
            if caller._block != entry["blocks"][1]:
                raise ReviewInputError("hot namespace terminal block changed")
            from review_sensei import session

            require_module_seal(session)
            completion: Any = sys._getframe(1)
            while completion is not None and not (
                completion.f_code is session.complete_review_publication.__code__
                and completion.f_globals is vars(session)
                and completion.f_locals.get("ledger") is entry["ledger"]
                and completion.f_locals.get("identity") == entry["record"].identity()
            ):
                completion = completion.f_back
            if completion is not None:
                transaction = completion.f_locals.get("transaction")
                if (
                    not entry["ledger"]._terminal_claimed
                    or transaction is None
                    or transaction.transaction_id != entry["transaction"]
                ):
                    raise ReviewInputError("hot namespace terminal transaction changed")
                entry["terminal_entered"] = True
            else:
                finish: Any = sys._getframe(1)
                saved = sources["finish"]
                while finish is not None and not (
                    finish.f_code is saved[1]
                    and finish.f_globals is saved[2]
                    and finish.f_locals.get("self") is caller
                    and finish.f_locals.get("ledger") is entry["ledger"]
                ):
                    finish = finish.f_back
                close = None if finish is None else finish.f_back
                saved_close = sources["close_run"]
                if (
                    not entry["terminal_entered"]
                    or close is None
                    or close.f_code is not saved_close[1]
                    or close.f_globals is not saved_close[2]
                    or _function(saved[0]) != saved
                    or _function(saved_close[0]) != saved_close
                    or review_run.close_review_run is not saved_close[0]
                ):
                    raise ReviewInputError(
                        "hot namespace terminal close source changed"
                    )
        else:
            raise ReviewInputError("hot namespace terminal purpose changed")
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
        if (
            frame.f_code is not method.__code__
            or frame.f_globals is not vars(publication)
            or frame.f_locals.get("self") is not publisher
            or frame.f_locals.get("lifecycle_journal") is not journal
            or frame.f_locals.get("native_host") is not host
        ):
            raise ReviewInputError("hot namespace requires original grouped producer")
        # Classify the authentic source domain before requiring an application
        # or outer hosted extent. Generic grouped callers retain prior behavior.
        from . import finding_lifecycle

        require_module_seal(finding_lifecycle)
        if type(journal) is not finding_lifecycle.FindingLifecycleJournal:
            return
        require_instance_source(journal, methods=("request", "checkpoint", "finish"))
        if journal.effects:
            return
        ledger, record = journal.ledger, journal._record
        if (
            record is None
            or record.quarantine_lineage is None
            or record.api_run_accounting is None
            or not getattr(ledger, "_quarantine_required", False)
        ):
            return
        initial = ApiRunAccounting.from_document(record.api_run_accounting)
        if initial.configuration.limit != 512:
            return
        caller = ledger.caller
        require_instance_source(
            caller, methods=("open", "_require_ledger", "_accept_checkpoint")
        )
        caller._require_ledger(ledger, journal.identity)
        quarantine_capacity.installed_capacity(ledger, journal.identity)
        guarded_pending_publication._original_accounting_graph(
            caller.accounting, models=True
        )
        scope = context.get()
        from . import review_run

        require_module_seal(review_run)
        if (
            scope is None
            or scope["thread"] != threading.get_ident()
            or scope["entry"] is not None
            or type(scope["run"]) is not sources.get("run_type")
            or scope["run"].caller is not caller
            or scope["run"].ledger is not ledger
        ):
            raise ReviewInputError(
                "hot namespace eligible producer lacks original run extent"
            )
        require_instance_source(scope["run"], methods=("execute",))
        run_frame: Any = frame
        while run_frame is not None and not (
            run_frame.f_code is sources["run"][1]
            and run_frame.f_globals is sources["run"][2]
            and run_frame.f_locals.get("self") is scope["run"]
        ):
            run_frame = run_frame.f_back
        app_frame: Any = frame
        while app_frame is not None and not (
            app_frame.f_code is sources["application"][1]
            and app_frame.f_globals is sources["application"][2]
        ):
            app_frame = app_frame.f_back
        if run_frame is None or app_frame is None:
            raise ReviewInputError(
                "hot namespace original run/application frame is absent"
            )
        app = app_frame.f_locals.get("self")
        if (
            type(app) is not sources["application_type"]
            or app.session_ledger is not ledger
        ):
            raise ReviewInputError("hot namespace application owner changed")
        require_instance_source(app, methods=("publish_review",))
        if caller in active:
            raise ReviewInputError("hot namespace cannot nest")
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
        ):
            raise ReviewInputError("hot namespace initial producer custody changed")
        initial.require_successor(caller.accounting)
        sequence = quarantine_command_namespace.observed_sequence(caller)
        if not 0 <= sequence < MAX_API_REQUEST_LIMIT:
            raise ReviewInputError("hot namespace initial sequence is unavailable")
        entry = dict(
            scope=scope,
            run_frame=run_frame,
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
            terminal_entered=False,
            transaction=record.transaction.transaction_id,
            tranches={b.block_id: b for b in caller.accounting.blocks},
            remaining=caller.accounting.remaining,
            prior=caller.accounting.prior_nonstartup_liability,
        )
        scope["entry"] = entry
        active[caller] = entry
        require(entry, caller)

    def before_sequence(caller, deadline):
        entry = active.get(caller)
        scope = context.get()
        if entry is None:
            if scope is not None and scope["entry"] is not None:
                raise ReviewInputError(
                    "hot namespace foreign caller in original extent"
                )
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
                raise ReviewInputError(
                    "hot namespace foreign caller in original extent"
                )
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
            or not _checkpoint_generation_matches(entry["record"], record)
        ):
            raise ReviewInputError("hot namespace positive checkpoint changed")
        # The original caller has already checked persisted.require_successor.
        # This changes root custody only, never the initial complete future bound.
        entry["root"], entry["record"] = cast(Any, record).record_sha256, record

    return (
        install_caller,
        install_application,
        install_run,
        begin,
        before_sequence,
        confirm_checkpoint,
    )


(
    install_caller,
    install_application,
    install_run,
    begin,
    before_sequence,
    confirm_checkpoint,
) = _registry()
del _registry

from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
