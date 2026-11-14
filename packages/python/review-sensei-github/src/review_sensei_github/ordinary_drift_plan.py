"""Detached sizing experiments for a conservatively stopped drift route.

Nothing in this module admits a route, installs a read discount, suppresses an
existing preflight, or authorizes dispatch.  A future consumer must separately
prove a permanent stop before journal staging on every unsuccessful prefix.
The existing ordinary publisher does not consume these envelopes.
"""

from __future__ import annotations

import copy
from typing import Any

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError


def stopped_prefix_documents(journal, requests, *, body_updates=()):
    """Project pending prefixes plus one maximum terminal/uncertain envelope.

    Input requests are sizing descriptors, never authenticated intents.  Unlike
    the hot-recovery planner, this projection cannot fund continuation after a
    failed native attempt.  It is deliberately not registered as a core sizing
    producer or bound to an execution plan.
    """
    from .finding_lifecycle import MAX_EFFECTS, _digest, _require_effect_scope
    from .ordinary_publication_plan import _capacity_request_identity
    from .ordinary_publication_reads import _source_guard

    _source_guard(journal.reviewer, journal)
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
