"""Inert whole-branch sizing from an executing original publication frame.

Nothing returned here is registered as capacity or an execution capability.
Unavailable source context is not an empty branch, and a maximum document is not
permission to skip an independently reachable checkpoint or native read.
"""

from __future__ import annotations

import copy
from dataclasses import fields, is_dataclass
from typing import Any

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.source_registry import require_instance_source, require_module_seal


def _data(value, *, _seen=None, _depth=0):
    if type(value) in (str, bytes, int, float, bool, type(None)):
        return
    if _depth > 64:
        raise ReviewInputError("materialized projection source graph is too deep")
    seen = set() if _seen is None else _seen
    if id(value) in seen:
        return
    if len(seen) >= 20_000:
        raise ReviewInputError("materialized projection source graph is too large")
    seen.add(id(value))
    if type(value) is dict:
        children = (*value.keys(), *value.values())
    elif type(value) in (tuple, list, set, frozenset):
        children = value
    elif not isinstance(value, type) and is_dataclass(value):
        try:
            require_instance_source(value, methods=())
        except ReviewInputError as error:
            raise ReviewInputError(
                f"materialized projection class is unavailable: {type(value).__module__}.{type(value).__qualname__}"
            ) from error
        if any(
            name in vars(value)
            for name in ("__copy__", "__deepcopy__", "__reduce__", "__reduce_ex__")
        ):
            raise ReviewInputError("materialized projection copy source changed")
        children = tuple(getattr(value, field.name) for field in fields(value))
    else:
        raise ReviewInputError("materialized projection source data is unsupported")
    for child in children:
        _data(child, _seen=seen, _depth=_depth + 1)


def project(journal, source):
    """Bound source request/stop documents; never admit or dispatch anything."""
    from review_sensei import context, models, presentation

    from . import approval, checks, finding_lifecycle, publication
    from . import ordinary_publication_plan as plan
    from .finding_lifecycle import _digest, _require_effect_scope
    from .guarded_pending_publication import _original_methods, _terminal_reservation

    for module in (
        plan,
        context,
        models,
        presentation,
        approval,
        checks,
        finding_lifecycle,
        publication,
    ):
        require_module_seal(module)
    publisher = journal.reviewer
    for owner in (
        journal,
        journal.ledger,
        publisher,
        publisher.checks,
        publisher.finalizer,
    ):
        _original_methods(owner)
    required = (
        "result",
        "preflight_review",
        "prospective_eligibility",
        "marker",
        "planned_check_outcome",
        "effective_policy",
        "complete_inventory",
        "human_inventory",
        "window_non_inline_blocking",
        "planned_window_state",
        "planned_drift_state",
        "convergence_policy",
        "auto_approval",
        "continuation_review",
        "retained_eligibility",
        "grouped_publication",
    )
    if any(name not in source for name in required):
        raise ReviewInputError("materialized projection source locals are incomplete")
    if (
        source["preflight_review"] is None
        or source["complete_inventory"] is not True
        or source["continuation_review"] is not None
        or source["retained_eligibility"] is not None
        or source["grouped_publication"]
    ):
        raise ReviewInputError("materialized projection source route is unsupported")
    _data(tuple(source[name] for name in required))
    if any(
        name in vars(journal)
        for name in ("__copy__", "__deepcopy__", "__reduce__", "__reduce_ex__")
    ):
        raise ReviewInputError("materialized projection journal copy source changed")
    _data((journal._record, journal.effects, journal._planned, journal._input_identity))
    for owner, names in (
        (
            journal,
            (
                "_finalizer_capacity_states",
                "_accepted_semantic_history",
                "_review_update_capacity_states",
                "_batch_body_updates",
            ),
        ),
        (
            publisher,
            (
                "_unknown_effect_holds",
                "_native_finding_context",
                "_native_finding_receipt_records",
                "_drift_states",
                "_drift_inputs",
                "_drift_concern_history",
                "_active_window_states",
                "_active_window_inputs",
            ),
        ),
    ):
        for name in names:
            _data(getattr(owner, name, None))
    terminal = _terminal_reservation(journal)
    caller = journal.ledger.caller
    before = (
        len(caller.observations),
        caller.usable,
        caller._root,
        canonical_bytes(journal._document()),
    )
    assembled = publisher._adjust_assembled_for_drift(
        publisher._adjust_assembled_for_window(
            source["preflight_review"],
            source["planned_window_state"],
            convergence_policy=source["convergence_policy"],
            auto_approval=source["auto_approval"],
        ),
        source["planned_drift_state"],
        convergence_policy=source["convergence_policy"],
        auto_approval=source["auto_approval"],
    )
    branches = plan.current_request_branches(
        publisher,
        journal,
        result=source["result"],
        assembled=assembled,
        eligibility=source["prospective_eligibility"],
        marker=source["marker"],
        outcome=source["planned_check_outcome"],
        policy=source["effective_policy"],
        complete_inventory=source["complete_inventory"],
        preserve_explanations=source["human_inventory"] is not None,
        window_non_inline_blocking=source["window_non_inline_blocking"],
    )
    summaries: list[dict[str, Any]] = []
    projected_prefixes = {}
    for branch in branches:
        shadow = copy.copy(journal)
        shadow.effects = []
        pending_sizes, stop_sizes = [], []
        keys = set()
        for method, path, body, precondition in branch:
            request = plan._capacity_request_identity(method, path, body)
            key = _digest({"method": method, "path": path, "request": request})
            if key in keys:
                raise ReviewInputError("materialized projection cannot assume replay")
            keys.add(key)
            effect = {
                "method": method,
                "path": path,
                "request": request,
                "request_sha256": key,
                "status": "pending",
                "response": None,
            }
            if precondition is not None:
                effect["check_update_precondition"] = precondition
            shadow.effects.append(effect)
            _require_effect_scope(
                shadow._document(), journal.identity, journal.head_sha
            )
            prefix_key = canonical_bytes(shadow.effects)
            cached = projected_prefixes.get(prefix_key)
            if cached is None:
                pending, stopped, confirmed = shadow._preflight_effect(
                    capacity_only=True
                )
                pending_bytes = len(canonical_bytes(pending))
                stop_bytes = max(pending_bytes, len(canonical_bytes(stopped)))
                cached = pending_bytes, stop_bytes, copy.deepcopy(confirmed)
                projected_prefixes[prefix_key] = cached
            pending_sizes.append(cached[0])
            stop_sizes.append(cached[1])
            shadow.effects = copy.deepcopy(cached[2])
        summaries.append(
            {
                "effects": len(branch),
                "pending_document_bytes": tuple(pending_sizes),
                "stop_document_bytes": tuple(stop_sizes),
                "maximum_finish_document_bytes": max(stop_sizes),
            }
        )
    if before != (
        len(caller.observations),
        caller.usable,
        caller._root,
        canonical_bytes(journal._document()),
    ):
        raise ReviewInputError("materialized projection changed physical source state")
    return {
        "authority": False,
        "source": "executing-original-publication",
        "branches": tuple(summaries),
        "ordinary_remaining": before[1],
        "protected_terminal_remaining": terminal.undispatched,
        "invocation_price_issued": False,
        "complete_funding_proof": False,
    }


from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
