"""Inert finite-namespace accounting data; never a writer or funding grant.

Only the negative namespace's original producer may attest these bytes. This
pure function alone conveys no source custody and cannot narrow generic core
JointPublicationProjection, whose future request width remains 128.
"""

import json

from review_sensei import api_work
from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.source_registry import require_module_seal

FUTURE_ID_BYTES = 31


def project_initial_accounting(initial: bytes, blocks: tuple[str, str]) -> bytes:
    require_module_seal(api_work)
    if (
        type(initial) is not bytes
        or type(blocks) is not tuple
        or len(blocks) != 2
        or any(type(key) is not str for key in blocks)
        or len(set(blocks)) != 2
    ):
        raise ReviewInputError("hot namespace projection inputs changed")
    state = api_work.ApiRunAccounting.from_document(json.loads(initial))
    if canonical_bytes(state.to_document()) != initial or state.action is not None:
        raise ReviewInputError("hot namespace initial accounting is not canonical")
    selected = tuple(
        next((b for b in state.blocks if b.block_id == key), None) for key in blocks
    )
    if any(block is None or block.issued_by != state.owner for block in selected):
        raise ReviewInputError("hot namespace projection owner changed")
    # This is the maximum original single invocation limit, not permission to
    # spend it. A projection with more synthetic slots is outside this slice.
    if state.configuration.limit != 512:
        raise ReviewInputError("hot namespace projection is outside its source domain")
    used = {request.request_id for request in state.completed}
    used.update(
        request.request_id for block in state.blocks for request in block.pending
    )
    projected = state
    serial = 0
    for block in selected:
        assert block is not None
        for _ in range(block.undispatched):
            while True:
                identifier = "p" + str(serial).zfill(FUTURE_ID_BYTES - 1)
                serial += 1
                if len(identifier) != FUTURE_ID_BYTES:
                    raise ReviewInputError("hot namespace synthetic range exceeded")
                if identifier not in used:
                    break
            used.add(identifier)
            projected = projected.dispatch(
                block.block_id,
                api_work.PendingApiRequest(identifier, api_work.MAX_API_REQUEST_LIMIT),
            )
    return canonical_bytes(projected.to_document())


from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
