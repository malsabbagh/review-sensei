"""Detached LAST sizing data. No reader, operation or execution authority."""

import sys
from dataclasses import dataclass

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.source_registry import require_module_seal, seal_module


@dataclass(frozen=True, eq=False)
class LastManifest:
    """Copyable diagnostic bytes, deliberately not a capability."""

    payload: bytes


def accounting_bytes(state):
    """Authenticate finite original model fields before original serialization."""
    require_module_seal(sys.modules[__name__])
    from review_sensei import api_work

    require_module_seal(api_work)
    original = vars(api_work)["ApiRunAccounting"]
    if type(state) is not original:
        raise ReviewInputError("LAST accounting model changed")
    count = 0

    def visit(value, ancestors, depth):
        nonlocal count
        count += 1
        if count > 20000 or depth > 32:
            raise ReviewInputError("LAST accounting graph exceeds bounds")
        kind = type(value)
        if value is None or kind is str or kind is int or kind is bool:
            return
        if id(value) in ancestors:
            raise ReviewInputError("LAST accounting graph is cyclic")
        ancestors = (*ancestors, id(value))
        if kind is tuple or kind is list:
            for child in value:
                visit(child, ancestors, depth + 1)
            return
        if kind is dict:
            for key, child in value.items():
                if type(key) is not str:
                    raise ReviewInputError("LAST accounting key changed")
                visit(child, ancestors, depth + 1)
            return
        # Identity against the sealed defining module precedes raw instance access.
        if not any(
            kind is item for item in vars(api_work).values() if type(item) is type
        ):
            raise ReviewInputError("LAST accounting nested model changed")
        fields = vars(kind).get("__dataclass_fields__")
        raw = object.__getattribute__(value, "__dict__")
        if type(fields) is not dict or type(raw) is not dict:
            raise ReviewInputError("LAST accounting fields unavailable")
        if any(type(key) is not str for key in raw) or set(raw) != set(fields):
            raise ReviewInputError("LAST accounting fields or method shadows changed")
        for child in raw.values():
            visit(child, ancestors, depth + 1)

    visit(state, (), 0)
    return canonical_bytes(vars(original)["to_document"](state))


def record_bytes(record):
    """Validate full original record graph before invoking its serializer."""
    require_module_seal(sys.modules[__name__])
    from review_sensei import session
    from review_sensei.source_registry import require_instance_source

    from . import historical_record_snapshot

    require_module_seal(historical_record_snapshot)
    # This rejects extra serializer shadows and nested impostors using raw
    # declared fields only; no record method executes while checking the graph.
    historical_record_snapshot._record_snapshot(record)
    require_instance_source(record, methods=("to_dict",))
    return canonical_bytes(session.SessionRecord.to_dict(record))


def preliminary_participants(observation):
    """Typed registry lookup for validation order only; not observation authority."""
    require_module_seal(sys.modules[__name__])
    from weakref import WeakKeyDictionary

    from review_sensei.source_registry import (
        require_instance_source,
        require_original_class_identity,
    )

    from . import quarantine_command_finalization as source
    from . import review_run

    require_module_seal(source)
    if type(observation) is not source.QuarantineFinalizerObservation:
        raise ReviewInputError("LAST observation identity is unavailable")
    if (
        type(source._OBSERVATIONS) is not WeakKeyDictionary
        or type(source._PREPARED) is not WeakKeyDictionary
    ):
        raise ReviewInputError("LAST preparation registry changed")
    raw = object.__getattribute__(observation, "__dict__")
    if type(raw) is not dict or any(type(key) is not str for key in raw):
        raise ReviewInputError("LAST observation fields changed")
    row = WeakKeyDictionary.get(source._OBSERVATIONS, observation)
    if type(row) is not tuple or len(row) != 3:
        raise ReviewInputError("LAST observation is unissued")
    token, payload, states = row
    if (
        type(token) is not source.QuarantineFinalizerPreparation
        or type(payload) is not bytes
        or type(states) is not bytes
        or type(raw.get("payload")) is not bytes
        or raw["payload"] != payload
        or type(raw.get("state_bytes")) is not bytes
        or raw["state_bytes"] != states
    ):
        raise ReviewInputError("LAST observation binding changed")
    saved = WeakKeyDictionary.get(source._PREPARED, token)
    if type(saved) is not dict or any(type(key) is not str for key in saved):
        raise ReviewInputError("LAST preparation fields changed")
    run = saved.get("run")
    require_original_class_identity(run, module=review_run, name="ReviewRun")
    require_instance_source(run, methods=())
    run_raw = object.__getattribute__(run, "__dict__")
    if type(run_raw) is not dict or any(type(key) is not str for key in run_raw):
        raise ReviewInputError("LAST run fields changed")
    from types import FunctionType

    for cls in type(run).__mro__:
        for name, member in vars(cls).items():
            if (
                type(member) is FunctionType
                or type(member) is property
                or type(member) is staticmethod
                or type(member) is classmethod
            ) and name in run_raw:
                raise ReviewInputError("LAST run executable instance shadow")
    caller = run_raw.get("caller")
    require_inline_participants(run_raw.get("ledger"), caller)
    caller_raw = object.__getattribute__(caller, "__dict__")
    record = saved.get("record")
    if record is not caller_raw.get("_effect_record"):
        raise ReviewInputError("LAST retained record owner changed")
    actual = record_bytes(record)
    if type(saved.get("root")) is not bytes or actual != saved["root"]:
        raise ReviewInputError("LAST retained record bytes changed")
    record_raw = object.__getattribute__(record, "__dict__")
    if (
        type(caller_raw.get("_root")) is not str
        or record_raw["record_sha256"] != caller_raw["_root"]
    ):
        raise ReviewInputError("LAST retained record root changed")
    return None


def require_inline_participants(ledger, caller, *, prospective=None):
    """Authenticate the exact no-I/O adapter decline BEFORE invoking builders.

    This is a negative source fence, not a price issuer. A detached record must
    fail the original quarantine pricer's identity test before reader traversal.
    """
    require_module_seal(sys.modules[__name__])
    from types import FunctionType

    from review_sensei import (
        api_work,
        bounded_evidence,
        finding_lifecycle_store,
        session,
    )
    from review_sensei.source_registry import (
        require_instance_source,
        require_original_class_export,
    )

    from . import (
        api_run_budget,
        api_run_requests,
        quarantine_census,
        quarantine_historical_execution,
        review_run,
        session_ledger,
    )

    for module in (
        api_work,
        bounded_evidence,
        finding_lifecycle_store,
        session,
        api_run_budget,
        api_run_requests,
        session_ledger,
        quarantine_census,
        quarantine_historical_execution,
    ):
        require_module_seal(module)
    # The original pricer constructs an allowed-class set. Authenticate even
    # unused exports before it can hash a hostile replacement metaclass.
    for name in ("ActionRunLedger", "ReviewRunLedger"):
        require_original_class_export(module=review_run, name=name)
    if (
        type(ledger) is not review_run.ActionRunLedger
        or type(caller) is not api_run_requests.ApiRunRequestTransport
    ):
        raise ReviewInputError("LAST inline adapter is not the original action owner")

    def raw_fields(value):
        raw = object.__getattribute__(value, "__dict__")
        if type(raw) is not dict or any(type(key) is not str for key in raw):
            raise ReviewInputError("LAST inline owner fields changed")
        for cls in type(value).__mro__:
            for name, member in vars(cls).items():
                if (
                    type(member) is FunctionType
                    or type(member) is property
                    or type(member) is staticmethod
                    or type(member) is classmethod
                ) and name in raw:
                    raise ReviewInputError("LAST inline executable instance shadow")
        return raw

    ledger_raw, caller_raw = raw_fields(ledger), raw_fields(caller)
    budget = caller_raw.get("_budget_view")
    if type(budget) is not api_run_budget.CallerRunBudget:
        raise ReviewInputError("LAST inline budget source changed")
    budget_raw = raw_fields(budget)
    if (
        ledger_raw.get("caller") is not caller
        or ledger_raw.get("evidence_budget") is not budget
        or budget_raw.get("caller") is not caller
        or ledger_raw.get("_quarantine_required") is not True
        or caller_raw.get("_state") is None
        or type(budget_raw.get("_ordinary_hold")) is not int
        or budget_raw["_ordinary_hold"] < 0
        or type(caller_raw.get("_block")) is not str
        or type(ledger_raw.get("app_slug")) is not str
        or type(ledger_raw.get("_evidence_author_id")) is not int
        or ledger_raw["_evidence_author_id"] <= 0
    ):
        raise ReviewInputError("LAST inline owner graph changed")
    accounting_bytes(caller_raw["_state"])
    if not any(
        block.block_id == caller_raw["_block"] for block in caller_raw["_state"].blocks
    ):
        raise ReviewInputError("LAST inline ordinary block is unavailable")
    require_instance_source(
        ledger,
        methods=(
            "evidence_producer",
            "preflight_finding_lifecycle_inline",
            "preflight_finding_lifecycle_checkpoint_pricing",
        ),
    )
    require_instance_source(
        budget, methods=("preflight", "check", "ordinary_remaining")
    )
    finding_lifecycle_store._require_checkpoint_graph(
        finding_lifecycle_store._CHECKPOINT_SOURCE
    )
    # trust_effects exports are installed dynamically. The sealed verifier
    # authenticates the original API-module capture and current export identity;
    # there is deliberately no late trust-module seal or missing-profile waiver.
    quarantine_historical_execution._require_default_profile(caller)
    if prospective is not None:
        if type(prospective) is not session.SessionRecord:
            raise ReviewInputError("LAST inline prospective model changed")
        raw_fields(prospective)
        require_instance_source(prospective, methods=("to_dict", "evolve", "identity"))
        if prospective is caller_raw.get("_effect_record"):
            raise ReviewInputError("LAST inline prospective record became current")
    return None


def measure(observation):
    """Retain original branch builders under the existing observation custody.

    Whole-sequence plans use the unchanged conservative native tail maximum7.
    They are not actual per-effect operation witnesses. Missing physical source
    pricing may refuse or retain generic pricing; it never earns a discount.
    """
    require_module_seal(sys.modules[__name__])
    from . import quarantine_command_finalization as source

    require_module_seal(source)
    preliminary_participants(observation)
    source.require_observation(observation)
    result = source.measure_inline_branches(observation, _retain_manifest=True)
    if not result["all_growth_roots_fit"]:
        raise ReviewInputError("LAST manifest growth roots do not fit")
    return LastManifest(canonical_bytes(result))


seal_module()
