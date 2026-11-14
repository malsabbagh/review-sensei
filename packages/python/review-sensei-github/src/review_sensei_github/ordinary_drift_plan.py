"""Detached sizing experiments for a conservatively stopped drift route.

Nothing in this module admits a route, installs a read discount, suppresses an
existing preflight, or authorizes dispatch.  A future consumer must separately
prove a permanent stop before journal staging on every unsuccessful prefix.
The existing ordinary publisher does not consume these envelopes.
"""

from __future__ import annotations

import copy
from dataclasses import asdict, fields, is_dataclass
from typing import Any

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError


def _require_methods(value):
    from review_sensei.source_registry import require_instance_source

    try:
        require_instance_source(value, methods=())
    except ReviewInputError as error:
        raise ReviewInputError(
            f"ordinary drift source class unavailable: {type(value).__module__}.{type(value).__qualname__}"
        ) from error
    namespace = getattr(value, "__dict__", {})
    if any(
        name in namespace
        for name in (
            "__copy__",
            "__deepcopy__",
            "__reduce__",
            "__reduce_ex__",
            "__getstate__",
            "__setstate__",
        )
    ):
        raise ReviewInputError("ordinary drift source copy protocol changed")
    require_instance_source(
        value,
        methods=tuple(
            name
            for name in dir(type(value))
            if not name.startswith("__") and callable(getattr(type(value), name))
        ),
    )


def _require_data(value, seen=None):
    if type(value) in (str, bytes, int, float, bool, type(None)):
        return
    seen = set() if seen is None else seen
    if id(value) in seen:
        return
    seen.add(id(value))
    if type(value) is dict:
        for key, item in value.items():
            _require_data(key, seen)
            _require_data(item, seen)
    elif type(value) in (tuple, list, set, frozenset):
        for item in value:
            _require_data(item, seen)
    elif not isinstance(value, type) and is_dataclass(value):
        _require_methods(value)
        for field in fields(value):
            _require_data(getattr(value, field.name), seen)
    else:
        raise ReviewInputError("ordinary drift source data graph is unqualified")


def _require_preparation_source(publisher, journal, *data):
    from .ordinary_publication_reads import _source_guard

    _source_guard(publisher, journal)
    for owner in (
        publisher,
        publisher.finalizer,
        publisher.checks,
        journal,
        journal.ledger,
    ):
        _require_methods(owner)
    for value in (
        journal._record,
        journal.effects,
        getattr(journal, "_input_identity", None),
        getattr(journal, "_accepted_semantic_history", None),
        getattr(journal, "_review_update_capacity_states", None),
        getattr(journal, "_batch_body_updates", None),
        getattr(publisher, "_unknown_effect_holds", None),
        getattr(publisher, "_native_finding_context", None),
        *data,
    ):
        _require_data(value)


def stopped_prefix_documents(journal, requests, *, body_updates=()):
    """Project pending prefixes plus one maximum terminal/uncertain envelope.

    Input requests are sizing descriptors, never authenticated intents.  Unlike
    the hot-recovery planner, this projection cannot fund continuation after a
    failed native attempt.  It is deliberately not registered as a core sizing
    producer or bound to an execution plan.
    """
    from .finding_lifecycle import MAX_EFFECTS, _digest, _require_effect_scope
    from .ordinary_publication_plan import _capacity_request_identity

    _require_preparation_source(journal.reviewer, journal, requests, body_updates)
    if (
        type(requests) is not tuple
        or not requests
        or len(requests) > MAX_EFFECTS
        or journal.effects
    ):
        raise ReviewInputError("stopped drift sizing requires a fresh effect sequence")
    if type(body_updates) is not tuple:
        raise ReviewInputError("stopped drift body projection is invalid")
    shadow = copy.copy(journal)
    shadow.effects = []
    # Preserve the existing batch producer's complete old/new receipt variants.
    # No real receipt or mutable publisher state is changed by this projection.
    shadow._batch_body_updates = copy.deepcopy(body_updates)
    documents: list[dict[str, Any]] = []
    stops: list[dict[str, Any]] = []
    keys: set[str] = set()
    for method, path, body, precondition in requests:
        request = _capacity_request_identity(method, path, body)
        key = _digest({"method": method, "path": path, "request": request})
        if key in keys:
            raise ReviewInputError("stopped drift sizing cannot assume replay")
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
        _require_effect_scope(shadow._document(), journal.identity, journal.head_sha)
        pending, future, confirmed = shadow._preflight_effect(capacity_only=True)
        documents.append(pending)
        stops.extend((pending, future))
        shadow.effects = confirmed
    documents.append(max(stops, key=lambda document: len(canonical_bytes(document))))
    return tuple(documents), len(shadow.effects) + 1


def observe_current(
    publisher,
    journal,
    *,
    planned,
    result,
    assembled,
    eligibility,
    marker,
    outcome,
    policy,
    complete_inventory,
    convergence_policy,
    auto_approval,
):
    """Read and project the real pre-effect source, without admitting dispatch.

    This diagnostic preparation intentionally uses the existing bounded census
    and its original meter. Its return value is not a registered receipt or a
    capability, and cannot replace any later native or journal preflight.
    """
    from .ordinary_publication_plan import current_request_branches
    from .ordinary_publication_reads import _require_reader_profile

    if journal is None or planned is None or not complete_inventory:
        return None
    if journal.effects or not any(
        obligation.thread_binding is not None
        for obligation in planned.inventory.obligations
    ):
        return None
    if getattr(journal._record, "quarantine_lineage", None) is not None:
        return None
    _require_preparation_source(publisher, journal, planned, assembled, eligibility)
    _require_reader_profile(publisher)
    context = getattr(publisher, "_native_finding_context", None)
    if context is None or assembled is None:
        return None
    original_record = canonical_bytes(journal._record.to_dict())
    original_root = journal._record.record_sha256
    original_inventory = canonical_bytes(asdict(planned.inventory))
    caller = getattr(journal.ledger, "caller", None)
    if caller is None:
        return None
    before_calls = len(caller.observations)
    before_usable = caller.usable
    publisher._published_finding_suppression(
        token=context["token"],
        repository=context["repository"],
        pull_request=context["pull_request"],
        app_slug=context["app_slug"],
        repository_id=context["repository_id"],
        head_sha=context["head_sha"],
        base_sha=context["base_sha"],
    )
    _require_preparation_source(publisher, journal, planned, assembled, eligibility)
    if (
        journal._record.record_sha256 != original_root
        or canonical_bytes(journal._record.to_dict()) != original_record
        or canonical_bytes(asdict(planned.inventory)) != original_inventory
        or journal.effects
    ):
        raise ReviewInputError("ordinary drift preparation source changed")
    census_calls = len(caller.observations) - before_calls
    if not 0 < census_calls <= 10:
        raise ReviewInputError("ordinary drift preparation census is unqualified")
    assembled = publisher._adjust_assembled_for_drift(
        assembled,
        planned,
        convergence_policy=convergence_policy,
        auto_approval=auto_approval,
    )
    updates = publisher._prepare_existing_drift_updates(
        planned=planned, prepared=assembled.inline_comments
    )
    if not updates or any(
        resolved or carried for _, _, _, resolved, _, carried in updates
    ):
        return None
    edits = tuple(row for row in updates if row[1] != row[4])
    if not edits:
        return None
    branches = current_request_branches(
        publisher,
        journal,
        result=result,
        assembled=assembled,
        eligibility=eligibility,
        marker=marker,
        outcome=outcome,
        policy=policy,
        complete_inventory=complete_inventory,
    )
    from .finding_lifecycle import _request_identity

    body_updates = tuple(
        (
            previous,
            {
                **previous,
                "body_sha256": _request_identity({"body": desired})["body"][
                    "$body_sha256"
                ],
            },
        )
        for _, desired, previous, _, _, _ in edits
    )
    native_updates = tuple(
        (
            "PATCH",
            f"/repos/{journal.identity.repository}/pulls/comments/{previous['comment_id']}",
            {"body": desired},
            None,
        )
        for _, desired, previous, _, _, _ in edits
    )
    reused_bodies = {desired for _, desired, _, _, _, _ in updates}
    projections = []
    for branch in branches:
        # Only normal check/check/review sequences belong to the stopped
        # experiment. Existing warning/cleanup branches remain unqualified.
        if len(branch) != 3 or not branch[-1][1].endswith("/reviews"):
            continue
        method, path, body, condition = branch[-1]
        body = {
            **body,
            "comments": [
                item for item in body["comments"] if item["body"] not in reused_bodies
            ],
        }
        requests = (*branch[:2], *native_updates, (method, path, body, condition))
        documents, item_count = stopped_prefix_documents(
            journal, requests, body_updates=body_updates
        )
        projections.append((requests, documents, item_count))
    if not projections:
        return None
    # Measure already-packed wire alternatives. The core applies its original
    # outer compressBound; no exact-document proof or discounted dispatch is
    # supplied. Fourteen reads is an experimental envelope, not a qualified
    # reader profile, and must not be consumed as execution authority.
    from review_sensei.finding_lifecycle_store import preflight_finding_lifecycle

    documents = tuple(
        max(
            (values[index] for _, values, _ in projections),
            key=lambda value: len(canonical_bytes(value)),
        )
        for index in range(len(projections[0][1]))
    )
    quote = preflight_finding_lifecycle(
        journal.ledger,
        journal._record,
        document=documents[0],
        future_documents=documents[1:],
        operation_id=journal.operation_id,
        base_sha=journal.base_sha,
        head_sha=journal.head_sha,
        policy_digest=journal.policy_digest,
        configuration_digest=journal.configuration_digest,
        item_count=max(count for _, _, count in projections),
        reserved_tail_calls=14
        + max(len(requests) for requests, _, _ in projections)
        + 6,
    )
    return {
        "quote": quote,
        "root": original_root,
        "record": original_record,
        "inventory": original_inventory,
        "native_nodes": canonical_bytes(publisher._native_binding_nodes),
        "updates": canonical_bytes(updates),
        "census_calls": census_calls,
        "usable_before": before_usable,
        "usable_after": caller.usable,
        "projections": tuple(projections),
    }
