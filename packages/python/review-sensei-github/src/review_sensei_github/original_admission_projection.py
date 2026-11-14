"""Lossless original admission data for a proposed journal-v4 envelope.

Not registered with any reader/writer and never an execution capability. A
future issuer must authenticate the original producer; a future cold consumer
must independently select the accepted source and latest owned root. Retain
original data, not a digest of a later lifecycle that may legitimately change.
"""

from __future__ import annotations

import json

from review_sensei.active_finding_window import (
    ActiveWindowEntry,
    ActiveWindowRoundState,
)
from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.context import finding_lifecycle_for_comment
from review_sensei.errors import ReviewInputError
from review_sensei.finding_drift import DriftActiveEntry
from review_sensei.finding_lifecycle import decode_lifecycle_state
from review_sensei.human_assessment import finding_instance_fingerprint
from review_sensei.models import ReviewResult

from .lifecycle_admission import restore_original_lifecycle_admission
from .publication import IndexedFinding, finding_index_marker, review_result_digest

VERSION = "original-admission-projection-v1"
FIELDS = frozenset(
    {
        "schema_version",
        "repository_id",
        "pull_request",
        "transaction_id",
        "result_sha256",
        "original_lifecycle",
        "finding_index",
    }
)


def _reconstruct(source, lifecycle, *, identity):
    if type(source) is not ReviewResult or source.transaction is None:
        raise ReviewInputError(
            "original admission requires the complete accepted source"
        )
    tx = source.transaction
    if (
        tx.repository != identity.repository
        or tx.pull_request != identity.pull_request
        or tx.result_sha256 != source.content_digest()
    ):
        raise ReviewInputError("original admission accepted source scope conflicts")
    decoded = decode_lifecycle_state(lifecycle)
    if decoded.input_identity is None or decoded.input_identity[:2] != (
        tx.head_sha,
        tx.base_sha,
    ):
        raise ReviewInputError("original admission input identity is unavailable")
    restored = restore_original_lifecycle_admission(
        source, decoded.state, head_sha=tx.head_sha, base_sha=tx.base_sha
    )
    # This proposal covers exact original all-inline admission only. It does
    # not reinterpret body-placed, carried, hidden or later edited obligations.
    state = decoded.state
    entries: tuple[ActiveWindowEntry, ...] | tuple[DriftActiveEntry, ...]
    if isinstance(state, ActiveWindowRoundState):
        entries = state.display_active_findings.entries
        obligation_ids = {
            item.obligation_id
            for item in state.authoritative_decision_inventory.obligations
        }
        # Active-window entries encode anchors, not the final placement label.
        # The independently supplied original producer index below must still
        # prove every item body_placed=False; anchors alone do not prove it.
        complete_inline = len(entries) == len(restored.comments) and all(
            item.path is not None
            and item.line is not None
            and item.side in {"LEFT", "RIGHT"}
            for item in entries
        )
    else:
        entries = state.active_entries
        obligation_ids = {item.obligation_id for item in state.inventory.obligations}
        complete_inline = len(state.active_entries) == len(restored.comments) and all(
            item.placement == "inline" for item in state.active_entries
        )
    if (
        not complete_inline
        or len({item.obligation_id for item in entries}) != len(entries)
        or {item.obligation_id for item in entries} != obligation_ids
    ):
        raise ReviewInputError("original admission placement is not complete inline")
    index_entries = tuple(
        IndexedFinding(
            finding_instance_fingerprint(comment),
            finding_lifecycle_for_comment(comment).fingerprint,
            comment.effective_blocking is True,
            comment.needs_human,
            body_placed=False,
            overridable=comment.effective_blocking is True or comment.needs_human,
        )
        for comment in restored.comments
    )
    index = finding_index_marker(
        repository_id=identity.repository_id,
        pull_request=identity.pull_request,
        head_sha=tx.head_sha,
        base_sha=tx.base_sha,
        result_digest=review_result_digest(restored),
        entries=index_entries,
        complete_inventory=True,
    )
    return restored, index


def project_original_admission(source, lifecycle, index, *, identity):
    """Validate original producer values; copying this data confers no authority."""
    restored, expected = _reconstruct(source, lifecycle, identity=identity)
    if index != expected or any(
        (a.effective_blocking, a.needs_human) != (b.effective_blocking, b.needs_human)
        for a, b in zip(source.comments, restored.comments, strict=True)
    ):
        raise ReviewInputError(
            "original admission differs from complete source producer"
        )
    return json.loads(
        canonical_bytes(
            {
                "schema_version": VERSION,
                "repository_id": identity.repository_id,
                "pull_request": identity.pull_request,
                "transaction_id": source.transaction.transaction_id,
                "result_sha256": source.content_digest(),
                "original_lifecycle": lifecycle,
                "finding_index": index,
            }
        )
    )


def restore_original_admission(value, source, *, identity):
    """Restore from retained ORIGINAL bytes, never from a later current state."""
    if (
        not isinstance(value, dict)
        or set(value) != FIELDS
        or value.get("schema_version") != VERSION
        or type(source) is not ReviewResult
        or source.transaction is None
        or type(value.get("repository_id")) is not int
        or type(value.get("pull_request")) is not int
        or value["repository_id"] != identity.repository_id
        or value["pull_request"] != identity.pull_request
        or value["transaction_id"] != source.transaction.transaction_id
        or value["result_sha256"] != source.content_digest()
    ):
        raise ReviewInputError("original admission retained projection conflicts")
    restored, index = _reconstruct(
        source, value["original_lifecycle"], identity=identity
    )
    if index != value["finding_index"]:
        raise ReviewInputError("original admission retained index conflicts")
    return restored, index


def require_preserved_original_admission(before, after):
    """Pure same-lineage requirement; no caller can issue a replacement here."""
    if (
        before is None
        or after is None
        or canonical_bytes(before) != canonical_bytes(after)
    ):
        raise ReviewInputError("original admission cannot be dropped or replaced")
