"""One-use immutable lifecycle document custody across one own decision CAS.

Only decoding already authenticated unchanged finding data is reused. Current
quarantine assessment reads, source fences and native/checkpoint CAS remain live.
"""

import json
from dataclasses import dataclass
from functools import wraps
from weakref import WeakKeyDictionary

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError

_ISSUED: WeakKeyDictionary = WeakKeyDictionary()


@dataclass(frozen=True, eq=False)
class _Transfer:
    pass


def _make_installer():
    installed = False

    def install_source(producer):
        """Return the issuer to the module's actual native-read producer only."""
        nonlocal installed
        if installed:
            raise ReviewInputError("quarantine lifecycle source already installed")
        import inspect
        import sys

        current = inspect.currentframe()
        frame = current.f_back if current is not None else None
        module = sys.modules.get("review_sensei_github.quarantine_action")
        spec = getattr(module, "__spec__", None)
        loader = getattr(spec, "loader", None)
        get_code = getattr(loader, "get_code", None)
        if (
            module is None
            or frame is None
            or frame.f_globals is not vars(module)
            or frame.f_locals is not frame.f_globals
            or frame.f_code.co_name != "<module>"
            or not getattr(spec, "_initializing", False)
            or not callable(get_code)
            or get_code(module.__name__) != frame.f_code
            or getattr(module, "_current_publication_digest", None) is not producer
            or not callable(producer)
        ):
            raise ReviewInputError(
                "quarantine lifecycle issuer requires its defining module"
            )
        installed = True
        from review_sensei.source_registry import require_instance_source

        from .api_run_requests import ApiRunRequestTransport
        from .command_finalization_plan import _underlying_http
        from .review_run import ActionRunLedger

        def intact(ledger):
            if type(ledger) is not ActionRunLedger:
                return False
            try:
                require_instance_source(
                    ledger,
                    methods=("load", "read_evidence", "replace", "evidence_producer"),
                )
            except ReviewInputError:
                return False
            return True

        def issue(ledger, record, document):
            from . import quarantine_action

            caller = getattr(ledger, "caller", None)
            if (
                record is None
                or quarantine_action._current_publication_digest is not wrapped
                or not intact(ledger)
                or type(caller) is not ApiRunRequestTransport
                or caller._effect_record is None
                or record is not caller._effect_record
                or record.record_sha256 != caller._root
                or caller._checkpoint is not None
                or record.finding_lifecycle is None
            ):
                return None
            try:
                http = _underlying_http(ledger.http, budget=ledger.evidence_budget)
            except ReviewInputError:
                return None
            token = _Transfer()
            _ISSUED[token] = dict(
                ledger=ledger,
                caller=caller,
                record=record,
                original=canonical_bytes(record.to_dict()),
                reference=canonical_bytes(record.finding_lifecycle),
                document=canonical_bytes(document),
                http=http,
                budget=ledger.evidence_budget,
                token=ledger.token,
                run=caller.run,
                intact=intact,
                producer=wrapped,
                target=None,
                confirmed=None,
                before=None,
            )
            return token

        @wraps(producer)
        def wrapped(ledger, record, *, _collision_pairs=None, _capture=None, **kwargs):
            if kwargs:
                raise ReviewInputError("quarantine lifecycle capture is source-owned")
            retained = []

            def capture(document):
                retained.append(issue(ledger, record, document))

            result = producer(
                ledger,
                record,
                _collision_pairs=_collision_pairs,
                _transfer_capture=capture,
            )
            if _capture is not None:
                _capture.extend(retained)
            return result

        return wrapped

    return install_source


install_source = _make_installer()
del _make_installer


def _target_binding(target):
    from .quarantine_action import QuarantineCommand

    if type(target.command) is not QuarantineCommand:
        raise ReviewInputError("quarantine sizing target command changed")
    authenticated = target.authenticated
    return (
        target.instance,
        target.permission,
        authenticated.record_sha256,
        authenticated.lineage_bytes,
        authenticated.archive_bytes,
        authenticated.inventories_bytes,
        authenticated.state_bytes,
        authenticated.source_documents_bytes,
        target.native_review_bytes,
        canonical_bytes(vars(target.command)),
    )


def bind_target(token, target):
    if token is None:
        return
    saved = _ISSUED.get(token)
    if saved is None or saved["target"] is not None:
        raise ReviewInputError("quarantine lifecycle target custody changed")
    saved["target"] = target
    saved["target_bytes"] = _target_binding(target)


def sizing_snapshot(target, run):
    """Expose original authenticated bytes before effects, never confirm a CAS.

    The result is only input to pure finalizer sizing. Runtime restore still
    requires the separately issued, consumed own-decision permit below.
    """
    token = target.lifecycle_transfer
    saved = _ISSUED.get(token) if type(token) is _Transfer else None
    if (
        saved is None
        or saved["target"] is not target
        or saved["target_bytes"] != _target_binding(target)
        or saved["confirmed"] is not None
        or saved["ledger"] is not run.ledger
        or saved["caller"] is not run.caller
        or saved["record"] is not run.caller._effect_record
        or saved["record"].record_sha256 != run.caller._root
        or run.caller._checkpoint is not None
        or run.caller._action_effect_boundary is not None
        or run.caller.accounting.action is None
        or run.caller.accounting.action.get("execution_started") is not False
    ):
        raise ReviewInputError("quarantine sizing lifecycle custody changed")
    _require_unchanged(saved, saved["record"])
    return saved["record"], json.loads(saved["document"])


def confirmed_transfer(target, run, confirmed, *, decision_permit):
    token = target.lifecycle_transfer
    if token is None:
        return None
    saved = _ISSUED.get(token)
    if saved is None or saved["target"] is not target or saved["confirmed"] is not None:
        raise ReviewInputError("quarantine lifecycle target is copied or reused")
    from .quarantine_action import _ISSUED_DECISIONS, _DecisionPermit

    if (
        type(decision_permit) is not _DecisionPermit
        or decision_permit not in _ISSUED_DECISIONS
        or not decision_permit.consumed
        or decision_permit.ledger is not run.ledger
        or decision_permit.authority.run is not run
        or decision_permit.before_state != target.authenticated.state_bytes
        or decision_permit.scope != target.authenticated.lineage["scope_sha256"]
        or run.ledger is not saved["ledger"]
        or run.caller is not saved["caller"]
        or confirmed is not run.caller._effect_record
        or confirmed.record_sha256 != run.caller._root
        or run.caller._checkpoint is not None
        or run.caller._action_effect_boundary is None
    ):
        raise ReviewInputError("quarantine lifecycle own decision was not confirmed")
    _require_unchanged(saved, confirmed)
    saved["confirmed"] = confirmed
    saved["confirmed_bytes"] = canonical_bytes(confirmed.to_dict())
    return token


def _require_unchanged(saved, record):
    from . import quarantine_action
    from .command_finalization_plan import _underlying_http

    ledger, caller = saved["ledger"], saved["caller"]
    if (
        not saved["intact"](ledger)
        or quarantine_action._current_publication_digest is not saved["producer"]
        or ledger.caller is not caller
        or caller.run is not saved["run"]
        or ledger.evidence_budget is not saved["budget"]
        or ledger.token != saved["token"]
        or _underlying_http(ledger.http, budget=ledger.evidence_budget)
        is not saved["http"]
        or canonical_bytes(saved["record"].to_dict()) != saved["original"]
        or canonical_bytes(record.finding_lifecycle) != saved["reference"]
    ):
        raise ReviewInputError("quarantine retained lifecycle reference/source changed")
    original, actual = (
        json.loads(saved["original"]),
        json.loads(canonical_bytes(record.to_dict())),
    )
    # These are the only fields the admitted begin-action and exact decision CAS
    # may change. The native decision permit independently proves its cursor.
    for value in (original, actual):
        for key in ("generation", "record_sha256", "updated_at", "api_run_accounting"):
            value.pop(key, None)
        lineage = value.get("quarantine_lineage")
        if not isinstance(lineage, dict):
            raise ReviewInputError("quarantine lifecycle lineage unavailable")
        lineage.pop("assessment_state_manifest", None)
    if canonical_bytes(original) != canonical_bytes(actual):
        raise ReviewInputError("quarantine decision changed unrelated lifecycle fields")
    from review_sensei.api_work import ApiRunAccounting

    before = ApiRunAccounting.from_document(
        json.loads(saved["original"])["api_run_accounting"]
    )
    after = ApiRunAccounting.from_document(record.api_run_accounting)
    if (
        before.owner != after.owner
        or after.owner != caller.accounting.owner
        or before.configuration != after.configuration
        or after.configuration != caller.accounting.configuration
        or before.action is None
        or after.action is None
        or canonical_bytes(before.action["intent"])
        != canonical_bytes(after.action["intent"])
        or after.action["phase"] != "pending"
    ):
        raise ReviewInputError("quarantine lifecycle accepted operation changed")


def consume(token, ledger, identity):
    saved = _ISSUED.pop(token, None) if type(token) is _Transfer else None
    if saved is None or ledger is not saved["ledger"] or saved["confirmed"] is None:
        raise ReviewInputError("quarantine lifecycle transfer is unissued or reused")
    caller, record = saved["caller"], saved["confirmed"]
    if (
        record.identity() != identity
        or record is not caller._effect_record
        or record.record_sha256 != caller._root
        or caller._checkpoint is not None
        or canonical_bytes(record.to_dict()) != saved["confirmed_bytes"]
    ):
        raise ReviewInputError("quarantine lifecycle transfer current root changed")
    _require_unchanged(saved, record)
    return record, json.loads(saved["document"])


from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
