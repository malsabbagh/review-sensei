"""Pure historical-decision sizing; projected bytes are never write authority."""

from dataclasses import dataclass

from review_sensei.bounded_evidence import (
    MAX_MANIFEST_BYTES,
    MAX_PART_BYTES,
    canonical_bytes,
    evidence_manifest,
    inline_manifest,
    partition_evidence,
    read_inline_evidence,
)
from review_sensei.errors import ReviewInputError
from review_sensei.quarantine_assessments import require_assessment_successor
from review_sensei.quarantine_state import assessment_cursor, next_assessment_event

from .quarantine_creation import _body as _event_body


@dataclass(frozen=True)
class DecisionProjection:
    """Immutable observations only: no ledger, token, permit, or write method."""

    state_bytes: bytes
    event_bytes: bytes | None
    cursor_manifest_bytes: bytes
    event_body_bytes: int
    creates_event: bool


def successor_state(authenticated, decision):
    """Validate the same exact append used by the actual decision CAS."""
    state = authenticated.state
    following = {**state, "settlements": [*state["settlements"], decision]}
    require_assessment_successor(
        authenticated.archive["assessment_origins"],
        authenticated.inventories,
        state,
        following,
        scope=authenticated.lineage["scope_sha256"],
    )
    return following


def cursor_manifest(*, scope, count, tail, binding):
    manifest = inline_manifest(
        assessment_cursor(scope=scope, count=count, tail_manifest=tail),
        binding=binding,
        item_count=count,
    )
    if manifest is None or len(canonical_bytes(manifest)) > MAX_MANIFEST_BYTES:
        raise ReviewInputError("quarantine decision cursor exceeds its bound")
    return manifest


def project_decision(authenticated, record, decision, *, binding):
    """Price the immutable event and successor cursor, not the finalizer tail.

    A duplicate or conflicting request preserves the immutable prior decision.
    Both still require independent full finalization admission. A synthetic
    maximum-width storage ID is a sizing value, never an activated reference.
    """
    require_projection_source()
    current = record.quarantine_lineage["assessment_state_manifest"]
    scope = authenticated.lineage["scope_sha256"]
    existing = next(
        (
            item
            for item in authenticated.state["settlements"]
            if (item["origin_scope_sha256"], item["instance"])
            == (decision["origin_scope_sha256"], decision["instance"])
        ),
        None,
    )
    if existing is not None:
        return DecisionProjection(
            canonical_bytes(authenticated.state),
            None,
            canonical_bytes(current),
            0,
            False,
        )
    following = successor_state(authenticated, decision)
    cursor = read_inline_evidence(current, expected_binding=current["binding"])
    event = next_assessment_event(cursor, decision)
    parts = partition_evidence(event, binding=binding, item_count=1)
    if len(parts) != 1:
        raise ReviewInputError("quarantine individual event exceeds one bounded part")
    body_bytes = len(_event_body(parts[0]).encode())
    if body_bytes > MAX_PART_BYTES:
        raise ReviewInputError("quarantine individual event body exceeds bound")
    tail = evidence_manifest(
        event,
        binding=binding,
        item_count=1,
        parts=parts,
        storage_ids=("9" * 64,),
    )
    manifest = cursor_manifest(
        scope=scope,
        count=len(following["settlements"]),
        tail=tail,
        binding=binding,
    )
    return DecisionProjection(
        canonical_bytes(following),
        canonical_bytes(event),
        canonical_bytes(manifest),
        body_bytes,
        True,
    )


def require_projection_source():
    """Validate the pure sizing algorithm, not the supplied observation's truth."""
    import sys

    from review_sensei import (
        bounded_evidence,
        quarantine_assessments,
        quarantine_lineage,
        quarantine_state,
        schemas,
    )
    from review_sensei.source_registry import require_module_seal

    from . import quarantine_creation

    for module in (
        sys.modules[__name__],
        bounded_evidence,
        quarantine_assessments,
        quarantine_lineage,
        quarantine_state,
        schemas,
    ):
        require_module_seal(module)
    # The production event framer is a closed concatenation over the shared
    # canonical encoder. Bind both its definition and sole executable input.
    if (
        quarantine_creation._body is not _event_body
        or quarantine_creation.canonical_bytes is not canonical_bytes
    ):
        raise ReviewInputError("quarantine event sizing source changed")


from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
