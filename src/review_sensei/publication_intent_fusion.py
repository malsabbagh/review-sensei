"""Closed source fusion of retained publication intent and its first journal CAS.

A deferral is not a write permit. Only the original retained-result validator
can create one, and the actual guarded publisher must fund its complete route
before the source journal can attempt the joint checkpoint.
"""

from __future__ import annotations

import inspect
import sys
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from functools import wraps
from typing import Any

from .bounded_evidence import canonical_bytes
from .errors import ReviewInputError
from .source_registry import require_instance_source, require_module_seal


@dataclass
class _Entry:
    app: Any
    retention: Any = None
    validated: Any = None
    receipt: Any = None
    original_receipt: Any = None
    approved: Any = None
    attempted: bool = False
    committed: Any = None
    application_code: Any = None
    grouped_progress: bool = False
    grouped_confirmed: bool = False


_ACTIVE: ContextVar[Any] = ContextVar("publication_intent_fusion", default=None)
_INSTALLED: set[str] = set()


def _definition(frame, name):
    module = sys.modules.get(name)
    spec = getattr(module, "__spec__", None)
    loader = getattr(spec, "loader", None)
    get_code = getattr(loader, "get_code", None)
    if (
        name in _INSTALLED
        or frame is None
        or module is None
        or frame.f_globals is not vars(module)
        or frame.f_locals is not frame.f_globals
        or not getattr(spec, "_initializing", False)
        or not callable(get_code)
        or frame.f_code != get_code(name)
    ):
        raise ReviewInputError("publication fusion requires defining source")
    _INSTALLED.add(name)


def _application_source_binding():
    source = None

    def capture(body):
        nonlocal source
        frame = inspect.currentframe()
        caller = frame.f_back if frame else None
        if (
            source is not None
            or caller is None
            or caller.f_code is not install_application.__code__
        ):
            raise ReviewInputError(
                "publication fusion application registration changed"
            )
        source = (
            body,
            body.__code__,
            body.__globals__,
            body.__defaults__,
            dict(body.__kwdefaults__ or {}),
        )

    def require(entry, frame):
        if (
            source is None
            or frame is None
            or frame.f_code is not source[1]
            or frame.f_globals is not source[2]
            or frame.f_locals.get("self") is not entry.app
            or source[0].__code__ is not source[1]
            or source[0].__globals__ is not source[2]
            or source[0].__defaults__ != source[3]
            or (source[0].__kwdefaults__ or {}) != source[4]
        ):
            raise ReviewInputError(
                "publication fusion requires original application frame"
            )
        require_module_seal(sys.modules["review_sensei_github.application"])
        require_instance_source(entry.app, methods=("publish_review",))

    return capture, require


_capture_application_source, _require_application_frame = _application_source_binding()
del _application_source_binding


def install_application(cls):
    frame = inspect.currentframe()
    _definition(frame.f_back if frame else None, cls.__module__)
    if (
        cls.__module__ != "review_sensei_github.application"
        or cls.__name__ != "GitHubApplication"
    ):
        raise ReviewInputError("publication fusion application is unsupported")
    original = cls.publish_review
    body = original
    while hasattr(body, "__wrapped__"):
        body = body.__wrapped__
    _capture_application_source(body)

    @wraps(original)
    def invoke(owner, **kwargs):
        entry = _Entry(owner, application_code=body.__code__)
        token = _ACTIVE.set(entry)
        try:
            return original(owner, **kwargs)
        finally:
            entry.receipt = entry.original_receipt = entry.approved = (
                entry.committed
            ) = None
            _ACTIVE.reset(token)

    cls.publish_review = invoke


def install_retention(namespace):
    frame = inspect.currentframe()
    name = "review_sensei_github.publication_recovery"
    _definition(frame.f_back if frame else None, name)
    if namespace is not vars(sys.modules[name]):
        raise ReviewInputError("publication fusion retention source changed")
    validate = namespace["_validate"]
    retain = namespace["retain_review_result"]

    @wraps(validate)
    def validated(*args, **kwargs):
        result = validate(*args, **kwargs)
        entry = _ACTIVE.get()
        caller = inspect.currentframe()
        caller = caller.f_back if caller else None
        if (
            entry is not None
            and entry.retention is not None
            and caller is not None
            and caller.f_code is retain.__code__
        ):
            entry.validated = (
                result,
                kwargs["ledger"],
                kwargs["identity"],
                retain.__code__,
            )
        return result

    @wraps(retain)
    def retaining(**kwargs):
        entry = _ACTIVE.get()
        if entry is None:
            return retain(**kwargs)
        if entry.retention is not None:
            raise ReviewInputError("publication fusion retention cannot nest")
        entry.retention = kwargs
        entry.validated = None
        try:
            return retain(**kwargs)
        finally:
            entry.retention = entry.validated = None

    namespace["_validate"] = validated
    namespace["retain_review_result"] = retaining


def defer_current():
    """Called lexically by original retain after its successful validation."""
    entry = _ACTIVE.get()
    frame = inspect.currentframe()
    frame = frame.f_back if frame else None
    if entry is None or entry.validated is None or entry.retention is None:
        return False
    result, ledger, identity, original = entry.validated
    if frame is None or frame.f_code is not original:
        raise ReviewInputError("publication fusion validation cannot be supplied")
    caller = getattr(ledger, "caller", None)
    record = getattr(caller, "_effect_record", None)
    if (
        caller is None
        or entry.receipt is not None
        or entry.app.session_ledger is not ledger
        or record is None
        or record.quarantine_lineage is None
        or record.transaction is None
        or record.transaction.phase != "publication_pending"
        or result.transaction != record.transaction
        or record.transaction.result_sha256 != result.content_digest()
        or record.record_sha256 != caller._root
        or caller._checkpoint is not None
        or record.identity() != identity
        or not getattr(ledger, "enable_finding_lifecycle_writes", False)
    ):
        return False
    require_module_seal(sys.modules["review_sensei_github.publication_recovery"])
    require_instance_source(
        ledger, methods=("load", "replace", "read_evidence", "evidence_producer")
    )
    caller._require_ledger(ledger, identity)
    # Plain target isolation has no installed native-capacity contract. Keep its
    # original mark transition; only an authenticated guarded producer may defer.
    origin = sys.modules["review_sensei_github.quarantine_origin"]
    require_module_seal(origin)
    authenticated = getattr(ledger, "_quarantine_authenticated", None)
    if authenticated is None:
        raise ReviewInputError("publication fusion origin custody is unavailable")
    origin.require_authenticated_read_owner(authenticated, ledger, caller, identity)
    if canonical_bytes(record.quarantine_lineage) != authenticated.lineage_bytes:
        raise ReviewInputError("publication fusion origin manifest changed")
    isolation = authenticated.archive["target_isolation"]
    isolation = isolation.get("execution_isolation", isolation)
    if isolation.get("schema_version") == "quarantine-target-isolation-v1":
        return False
    capacity = sys.modules["review_sensei_github.quarantine_capacity"]
    require_module_seal(capacity)
    guard = capacity.installed_capacity(ledger, identity)
    if result.comments and not guard.native_grouped:
        return False
    entry.grouped_progress = bool(result.comments)
    entry.receipt = (
        ledger,
        caller,
        record,
        canonical_bytes(record.to_dict()),
        ledger.http,
        ledger.evidence_budget,
        result.content_digest(),
        canonical_bytes(entry.retention["configuration_context"]),
        canonical_bytes(entry.retention["evidence_context"]),
    )
    entry.original_receipt = entry.receipt
    return True


def outstanding():
    entry = _ACTIVE.get()
    return entry is not None and entry.receipt is not None and entry.committed is None


def _receipt(ledger, record):
    entry = _ACTIVE.get()
    if entry is None or entry.receipt is None:
        return None
    owner, caller, original, raw, http, budget, digest, _, _ = entry.receipt
    retained = entry.original_receipt
    if (
        retained is None
        or retained[0] is not owner
        or retained[1] is not caller
        or canonical_bytes(retained[2].to_dict()) != retained[3]
        or retained[4:] != entry.receipt[4:]
    ):
        raise ReviewInputError("publication fusion retention receipt changed")
    if (
        owner is not ledger
        or ledger.caller is not caller
        or ledger.http is not http
        or ledger.evidence_budget is not budget
        or canonical_bytes(original.to_dict()) != raw
        or canonical_bytes(record.to_dict()) != raw
        or original.transaction.result_sha256 != digest
        or caller._checkpoint is not None
        or caller._root != original.record_sha256
    ):
        raise ReviewInputError("publication fusion original custody changed")
    require_instance_source(
        ledger, methods=("load", "replace", "read_evidence", "evidence_producer")
    )
    caller._require_ledger(ledger, record.identity())
    return entry


def prospective_transaction(ledger, record):
    entry = _ACTIVE.get()
    if entry is None or entry.receipt is None or entry.committed is not None:
        return None
    original = entry.receipt[2]
    entry = _receipt(ledger, original)
    from . import session

    require_module_seal(session)
    evolved = session._publication_state_mutation(
        original.transaction, published=False
    )(original)
    if canonical_bytes(record.to_dict()) != entry.receipt[3]:
        # The core's pure multi-checkpoint planner feeds each derived candidate
        # into the next projection. It already carries this transaction delta;
        # never advance the business transaction a second time. Actual stage
        # separately requires the exact original root and issued pending bytes.
        if (
            record.identity() != original.identity()
            or record.transaction != evolved.transaction
        ):
            raise ReviewInputError("publication fusion prospective transition changed")
        return None
    # Derive, never accept, the original exact transaction delta. The caller
    # combines it with its manifest at original generation+1, not a second CAS.
    return evolved.transaction


def _source_call(frame, module_name, function_name):
    module = sys.modules.get(module_name)
    if module is None:
        raise ReviewInputError("publication fusion producer is unavailable")
    require_module_seal(module)
    method = getattr(module, function_name, None)
    if frame is None or frame.f_code is not getattr(method, "__code__", None):
        raise ReviewInputError("publication fusion requires its original producer")


def approve_plan(journal):
    frame = inspect.currentframe()
    _source_call(
        frame.f_back if frame else None,
        "review_sensei_github.guarded_pending_publication",
        "admit",
    )
    entry = _receipt(journal.ledger, journal._record)
    if entry is not None:
        if (
            entry.approved is not None
            or entry.attempted
            or journal.effects
            or journal._record is not journal.ledger.caller._effect_record
        ):
            raise ReviewInputError("publication fusion plan was reused")
        entry.approved = journal


def _checkpoint_source_binding():
    selected: tuple[Any, ...] | None = None

    def install_journal(cls):
        nonlocal selected
        frame = inspect.currentframe()
        _definition(frame.f_back if frame else None, cls.__module__)
        method = cls.checkpoint
        if (
            selected is not None
            or cls.__module__ != "review_sensei_github.finding_lifecycle"
            or cls.__name__ != "FindingLifecycleJournal"
            or method.__module__ != cls.__module__
            or method.__qualname__ != "FindingLifecycleJournal.checkpoint"
            or hasattr(method, "__wrapped__")
        ):
            raise ReviewInputError("publication fusion journal source is unsupported")
        selected = (
            cls,
            method,
            method.__code__,
            method.__globals__,
            method.__defaults__,
            dict(method.__kwdefaults__ or {}),
        )

    def require(journal, frame):
        if (
            selected is None
            or type(journal) is not selected[0]
            or selected[1].__code__ is not selected[2]
            or frame is None
            or frame.f_code is not selected[2]
            or frame.f_globals is not selected[3]
            or selected[1].__defaults__ != selected[4]
            or (selected[1].__kwdefaults__ or {}) != selected[5]
            or frame.f_locals.get("self") is not journal
        ):
            raise ReviewInputError("publication fusion checkpoint source changed")

    return install_journal, require


install_journal, _require_checkpoint_source = _checkpoint_source_binding()
del _checkpoint_source_binding


def begin_checkpoint(journal, packed):
    entry = _ACTIVE.get()
    if entry is None or entry.receipt is None or entry.committed is not None:
        return False
    require_instance_source(journal, methods=("checkpoint",))
    frame = inspect.currentframe()
    frame = frame.f_back if frame else None
    _require_checkpoint_source(journal, frame)
    _receipt(journal.ledger, journal._record)
    if entry.approved is not journal or entry.attempted:
        raise ReviewInputError("publication fusion checkpoint is unapproved or reused")
    if len(journal.effects) != 1 or journal.effects[0]["status"] != "pending":
        raise ReviewInputError("publication fusion requires its first pending effect")
    effect = journal.effects[0]
    if (
        effect["method"] not in {"POST", "PATCH"}
        or "/check-runs" not in effect["path"]
        or effect["request"].get("status") != "in_progress"
    ):
        raise ReviewInputError("publication fusion first effect changed")
    entry.attempted = True
    entry.approved = (journal, canonical_bytes(packed))
    return True


def require_stage(ledger, record, document):
    entry = _ACTIVE.get()
    if entry is None or entry.receipt is None or entry.committed is not None:
        return
    _receipt(ledger, record)
    if (
        not entry.attempted
        or not isinstance(entry.approved, tuple)
        or entry.approved[0].ledger is not ledger
        or canonical_bytes(document) != entry.approved[1]
    ):
        raise ReviewInputError("publication fusion stage is not the admitted intent")


def confirm_checkpoint(journal, before):
    entry = _ACTIVE.get()
    if entry is None or entry.receipt is None or entry.committed is not None:
        return
    if not entry.attempted or entry.approved[0] is not journal:
        raise ReviewInputError("publication fusion checkpoint confirmation changed")
    from .session import _publication_state_mutation

    current = journal._record
    expected = _publication_state_mutation(before.transaction, published=False)(before)
    if (
        current.transaction != expected.transaction
        or current.generation != before.generation + 1
        or current is not journal.ledger.caller._effect_record
        or current.record_sha256 != journal.ledger.caller._root
    ):
        raise ReviewInputError("publication fusion own checkpoint remains unconfirmed")
    entry.committed = (before, current)


def committed_transition(journal):
    entry = _ACTIVE.get()
    if entry is None or entry.committed is None:
        return None
    if entry.approved[0] is not journal:
        raise ReviewInputError("publication fusion journal changed")
    return entry.committed


def record_progress(ledger, identity, transaction, **kwargs):
    """Run the original progress CAS and transfer only its exact owned delta."""
    from . import session

    require_module_seal(sys.modules[__name__])
    entry = _ACTIVE.get()
    if entry is None or entry.receipt is None:
        return session.record_admitted_blocker_progress(
            ledger, identity, transaction, **kwargs
        )
    frame = inspect.currentframe()
    frame = frame.f_back if frame else None
    _require_application_frame(entry, frame)
    require_module_seal(session)
    before = entry.receipt[2]
    _receipt(ledger, before)
    if entry.grouped_progress:
        return _record_grouped_progress(entry, ledger, identity, transaction, kwargs)
    if (
        transaction != before.transaction
        or kwargs.get("suppress_publication") is not False
        or kwargs.get("blocker_count") != 0
        or kwargs.get("blocker_set_sha256") != session.blocker_set_digest(())[0]
        or set(kwargs)
        - {"suppress_publication", "blocker_count", "blocker_set_sha256", "now"}
    ):
        raise ReviewInputError("publication fusion progress is not the empty original")
    after = session.record_admitted_blocker_progress(
        ledger, identity, transaction, **kwargs
    )
    caller = ledger.caller
    if (
        after is not caller._effect_record
        or after.record_sha256 != caller._root
        or caller._checkpoint is not None
    ):
        raise ReviewInputError("publication fusion progress confirmation changed")
    if canonical_bytes(after.to_dict()) == entry.receipt[3]:
        # The source may return its already-present exact marker without a CAS.
        entry.receipt = (
            *entry.receipt[:2],
            after,
            canonical_bytes(after.to_dict()),
            *entry.receipt[4:],
        )
        return after
    history = before.convergence_history
    if (
        not isinstance(history, dict)
        or not isinstance(history.get("progress"), list)
        or not history["progress"]
    ):
        raise ReviewInputError("publication fusion progress history is unavailable")
    item = dict(
        event="completed",
        generation=transaction.generation,
        blocker_set_sha256=kwargs["blocker_set_sha256"],
        blocker_count=0,
        transaction_id=transaction.transaction_id,
    )
    updated_history = {**history, "progress": [*history["progress"][:-1], item]}
    expected = before.evolve(
        now=datetime.fromisoformat(after.updated_at.replace("Z", "+00:00")),
        generation=before.generation + 1,
        convergence_history=updated_history,
        api_run_accounting=after.api_run_accounting,
    )
    if canonical_bytes(expected.to_dict()) != canonical_bytes(after.to_dict()):
        raise ReviewInputError(
            "publication fusion progress changed unrelated authority"
        )
    # This closed adapter receipt authenticates the source writer's final GET;
    # it is additional to, not a substitute for, the original method execution.
    source = sys.modules.get("review_sensei_github.quarantine_checkpoint")
    if source is None:
        raise ReviewInputError("publication fusion progress receipt is unavailable")
    require_module_seal(source)
    proof = source.confirmed_checkpoint_capacity(caller, ledger, identity)
    if proof.record is not after:
        raise ReviewInputError("publication fusion progress receipt changed")
    entry.receipt = (
        *entry.receipt[:2],
        after,
        canonical_bytes(after.to_dict()),
        *entry.receipt[4:],
    )
    return after


def _record_grouped_progress(entry, ledger, identity, transaction, kwargs):
    """Consume original retained source at original admitted progress only.

    No business effect is authorized here. The original failure-phase mutation
    and original progress mutation run against the same owned parent; only the
    two root writes coalesce. Unknown CAS never yields a usable candidate.
    """
    from . import session

    frame = inspect.currentframe()
    caller = frame.f_back if frame else None
    require_module_seal(sys.modules[__name__])
    require_module_seal(session)
    if (
        caller is None
        or caller.f_code is not record_progress.__code__
        or caller.f_globals is not globals()
        or caller.f_locals["entry"] is not entry
        or caller.f_locals["ledger"] is not ledger
        or caller.f_locals["transaction"] is not transaction
        or caller.f_locals["kwargs"] is not kwargs
        or _ACTIVE.get() is not entry
        or not entry.grouped_progress
        or transaction != entry.receipt[2].transaction
        or identity != entry.receipt[2].identity()
        or entry.attempted
        or set(kwargs)
        - {"suppress_publication", "blocker_count", "blocker_set_sha256", "now"}
    ):
        raise ReviewInputError("grouped progress fusion requires original admission")
    before = entry.receipt[2]
    _receipt(ledger, before)
    if kwargs.get("suppress_publication") is not False:
        # Suppression is not this two-transition publication path. Preserve
        # both original eager writers and their no-progress recovery rules.
        session.mark_review_publication_unconfirmed(ledger, identity, transaction)
        entry.receipt = entry.original_receipt = None
        entry.grouped_progress = False
        return session.record_admitted_blocker_progress(
            ledger, identity, transaction, **kwargs
        )
    now = kwargs.get("now")
    failure = session._publication_state_mutation(transaction, published=False, now=now)
    progress = session._admitted_blocker_progress_mutation(
        ledger, identity, transaction, **kwargs
    )
    entry.attempted = True

    def mutate(record):
        require_module_seal(sys.modules[__name__])
        require_module_seal(session)
        require_module_seal(sys.modules["review_sensei.models"])
        _receipt(ledger, record)
        failed = failure(record)
        after = progress(failed)
        if (
            failed.generation != record.generation + 1
            or after.generation != failed.generation + 1
            or after.transaction is None
            or after.transaction != failed.transaction
            or after.transaction.phase != "publication_failed"
        ):
            raise ReviewInputError("grouped progress transitions cannot coalesce")
        # Reconstruct from the exact owned parent. Never install a two-step
        # descendant as a one-step CAS or rewrite transaction generations.
        joint = record.evolve(
            now=now,
            generation=record.generation + 1,
            transaction=after.transaction,
            convergence_history=after.convergence_history,
        )
        expected = after.to_dict()
        actual = joint.to_dict()
        for key in ("generation", "record_sha256", "updated_at"):
            expected.pop(key, None)
            actual.pop(key, None)
        if canonical_bytes(expected) != canonical_bytes(actual):
            raise ReviewInputError("grouped progress changed unrelated state")
        return joint

    after = ledger.replace(identity, mutate, now=now)
    owner = ledger.caller
    if (
        after is not owner._effect_record
        or after.record_sha256 != owner._root
        or owner._checkpoint is not None
    ):
        raise ReviewInputError("grouped progress lacks confirmed own checkpoint")
    source = sys.modules.get("review_sensei_github.quarantine_checkpoint")
    if source is None:
        raise ReviewInputError("grouped progress confirmation source unavailable")
    require_module_seal(source)
    proof = source.confirmed_checkpoint_capacity(owner, ledger, identity)
    if proof.record is not after:
        raise ReviewInputError("grouped progress confirmation changed")
    entry.receipt = entry.original_receipt = None
    entry.grouped_progress = False
    entry.committed = after
    entry.grouped_confirmed = True
    return after


def confirmed_grouped_progress(record):
    """Original application may transfer only its positively confirmed metadata."""
    require_module_seal(sys.modules[__name__])
    entry = _ACTIVE.get()
    if entry is None or entry.committed is None or not entry.grouped_confirmed:
        return False
    frame = inspect.currentframe()
    caller = frame.f_back if frame else None
    _require_application_frame(entry, caller)
    if (
        caller is None
        or caller.f_locals["admitted_record"] is not record
        or record is not entry.committed
    ):
        raise ReviewInputError("grouped progress metadata requires original consumer")
    ledger = entry.app.session_ledger
    require_instance_source(entry.app, methods=("publish_review",))
    require_instance_source(
        ledger, methods=("load", "replace", "read_evidence", "evidence_producer")
    )
    if (
        record is not ledger.caller._effect_record
        or record.record_sha256 != ledger.caller._root
    ):
        raise ReviewInputError("grouped progress metadata lost current custody")
    return True


from .source_registry import seal_module as _seal_fusion_source  # noqa: E402

_seal_fusion_source()
