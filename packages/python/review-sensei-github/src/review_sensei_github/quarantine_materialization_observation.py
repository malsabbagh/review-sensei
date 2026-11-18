"""Diagnostic-only accepted-result sizing; never a census or write permission.

No runtime path consumes this report. Existing capture proves what the source
writer confirmed in this invocation, not current remote body freshness. It must
NOT be used to discard unknown allocations or replace the full review tail.
"""

import json
from dataclasses import dataclass
from hashlib import sha256

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewResult
from review_sensei.source_registry import require_instance_source

from .accepted_result import selected_accepted_result
from .operation_comments import result_part_bodies
from .publication_recovery import _publication_transport
from .quarantine_capacity import full_review_comment_tail


@dataclass(frozen=True)
class MaterializationObservation:
    caller_identity: int
    record_sha256: str
    transaction_id: str
    accepted_reference: bytes
    payload_sha256: str
    payload_bytes: int
    event_comment_id: int
    result_carrier_ids: tuple[int, ...]
    # Deterministic raw rendering, NOT evidence these parts were allocated.
    # An accepted result may instead be embedded in event_comment_id.
    raw_result_renderer_body_bytes: tuple[int, ...]
    actual_writer_variant: str
    generic_tail_slots: int
    generic_tail_body_bytes: int
    unresolved_requests: int
    # Always zero. The report cannot reduce either lifetime allocations or
    # unknown-write reservations, including when all observed requests settled.
    releasable_allocations: int = 0


def observe_materialization(ledger) -> MaterializationObservation:
    """Inspect original source capture and current owned root without any I/O.

    Exact accepted-result IDs may identify already materialized slots for later
    research. This function neither reauthenticates their remote bytes nor
    calculates a smaller remaining runtime plan. Raw and compact branches are
    reported separately; their renderer bounds cannot be interchanged.
    """
    require_instance_source(ledger, methods=("load", "read_evidence"))
    caller = ledger.caller
    require_instance_source(caller, methods=("open",))
    record = caller._effect_record
    if record is None or record.record_sha256 != caller._root:
        raise ReviewInputError("materialization observation has no current owned root")
    original = canonical_bytes(record.to_dict())
    caller._require_ledger(ledger, record.identity())
    transaction = record.transaction
    if transaction is None:
        raise ReviewInputError(
            "materialization observation has no selected transaction"
        )
    captured = selected_accepted_result(
        ledger.http, transaction, app_id=ledger.authenticated_app_id
    )
    reference = captured.reference
    if (
        reference["repository_id"] != record.repository_id
        or reference["run_id"] != str(caller.run.run_id)
        or reference["run_attempt"] != str(caller.attempt)
        or sha256(captured.payload).hexdigest() != reference["payload_sha256"]
    ):
        raise ReviewInputError("materialization observation selected source changed")
    accepted = ReviewResult.from_dict(json.loads(captured.payload))
    if accepted.content_digest() != transaction.result_sha256:
        raise ReviewInputError("materialization observation result digest changed")
    bodies = result_part_bodies(reference["event_key"], captured.payload)
    transport = _publication_transport(ledger.http)
    variant = (
        "raw-checkpoint-and-inventory"
        if getattr(transport, "_quarantine_capacity_guard", None) is None
        else "guard-present-requires-independent-compact-source-proof"
    )
    tail = full_review_comment_tail()
    if (
        caller._effect_record is not record
        or canonical_bytes(record.to_dict()) != original
    ):
        raise ReviewInputError(
            "materialization observation root changed during capture"
        )
    return MaterializationObservation(
        id(caller),
        record.record_sha256,
        transaction.transaction_id,
        captured.reference_bytes,
        reference["payload_sha256"],
        len(captured.payload),
        reference["event_comment_id"],
        tuple(reference["part_ids"]),
        tuple(len(canonical_bytes({"body": body})) for body in bodies),
        variant,
        len(tail),
        sum(tail),
        caller.accounting.uncertain,
    )
