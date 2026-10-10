"""Opt-in current-root association; old packets never authorize further reads.

An association authenticates the complete declared inventory. Each selected
object is authenticated when fetched. It is neither a restart witness nor a
grant to resolve paths, arbitrary IDs, or an older packet's reference graph.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, cast

from .assessment_history_index import (
    HistoryPartReference,
    IndexedAssessmentJournal,
    resolve_history_part,
)
from .assessment_queue import FactoredAssessmentJournal, QueueHostState
from .bounded_evidence import (
    MAX_STORED_PART_BYTES,
    MAX_STORED_PARTS,
    PART_FRAMING_RESERVE,
    EvidenceReadBudget,
    canonical_bytes,
    validate_binding,
)
from .errors import ReviewInputError


def indexed_document(document: object) -> bool:
    return (
        isinstance(document, dict)
        and isinstance(document.get("journal"), dict)
        and (
            "sources" in document["journal"]
            or str(document["journal"].get("schema_version", "")).startswith(
                "assessment-history-index-"
            )
        )
    )


def parse_history(document: object, record: Any) -> IndexedAssessmentJournal:
    if (
        not isinstance(document, dict)
        or set(document) != {"schema_version", "kind", "journal"}
        or document["schema_version"] != "1.0"
        or document["kind"] != "assessment-queue-state"
        or record.assessment_queue is None
    ):
        raise ReviewInputError("authenticated history envelope is invalid")
    journal = IndexedAssessmentJournal.from_document(document["journal"])
    root = record.assessment_queue
    manifest = root["state_manifest"]
    binding = manifest["binding"]
    if (
        root["inventory_digest"] != journal.queue.inventory_digest
        or manifest["item_count"] != len(journal.queue.inventory_ids)
        or any(
            binding.get(key) != value
            for key, value in journal.queue.snapshot.to_dict().items()
        )
    ):
        raise ReviewInputError("history complete inventory or snapshot differs")
    refs = journal.history_references()
    producer = binding["producer"]
    pattern = r"[a-f0-9]{64}" if producer == "local-ledger" else r"[1-9][0-9]{0,18}"
    if any(
        re.fullmatch(pattern, ref.storage_id) is None
        or producer == "local-ledger"
        and ref.storage_id != ref.sha256
        for ref in refs
    ):
        raise ReviewInputError(
            "history complete inventory contains foreign adapter references"
        )
    root_ids = {item["storage_id"] for item in manifest["parts"]}
    history = record.convergence_history
    baseline = history.get("baseline") if isinstance(history, Mapping) else None
    baseline_parts = (
        baseline["parts"] if isinstance(baseline, dict) and "parts" in baseline else []
    )
    root_ids.update(item["storage_id"] for item in baseline_parts)
    if any(ref.storage_id in root_ids for ref in refs):
        raise ReviewInputError("history child aliases an authority root part")
    parts = list(manifest["parts"]) + list(baseline_parts)
    if (
        len(parts) + len(refs) > MAX_STORED_PARTS
        or sum(item["bytes"] + PART_FRAMING_RESERVE for item in parts)
        + sum(ref.bytes + PART_FRAMING_RESERVE for ref in refs)
        > MAX_STORED_PART_BYTES
    ):
        raise ReviewInputError("history complete direct retention exceeds bound")
    return journal


def _read_history_child(
    ledger: Any, identity: Any, document: dict[str, Any], row: dict[str, Any]
) -> object:
    binding = document["binding"]
    current = IndexedAssessmentJournal.from_document(document["envelope"]["journal"])
    reference = HistoryPartReference.from_document(row["reference"])
    packet = resolve_history_part(
        reference,
        reader=lambda storage_id: ledger._read_part(identity, storage_id),
        expected_binding=binding,
        budget=ledger.evidence_budget,
        item_count=len(current.queue.inventory_ids),
    )
    old = IndexedAssessmentJournal.from_document(packet)
    inline = old._current()
    if (
        inline is None
        or inline["source_digest"] != row["source_digest"]
        or inline["operation_id"] != row["operation_id"]
        or old.queue.snapshot != current.queue.snapshot
        or old.queue.inventory_digest != current.queue.inventory_digest
        or old.queue.inventory_ids != current.queue.inventory_ids
        or not set(old.queue.resolved_ids) <= set(current.queue.resolved_ids)
        or row["active"]
        == FactoredAssessmentJournal._settled(
            cast(dict[str, object], inline["receipt"])
        )
    ):
        raise ReviewInputError("owned history child source or inventory differs")
    return packet


def history_payload(record: Any, envelope: object) -> dict[str, Any]:
    journal = parse_history(envelope, record)
    return {
        "binding": record.assessment_queue["state_manifest"]["binding"],
        "envelope": envelope,
        "sources": journal.to_document()["sources"],
    }


@dataclass(frozen=True)
class HistoryAssociation:
    """Opaque same-ledger, same-budget proof of the complete latest inventory."""

    root_sha256: str
    root_generation: int
    inventory_sha256: str
    _payload: bytes = field(repr=False)
    _token: object = field(repr=False, compare=False)

    def host_state(self) -> QueueHostState:
        value = json.loads(self._payload)
        journal = IndexedAssessmentJournal.from_document(value["envelope"]["journal"])
        return QueueHostState(
            value["envelope"],
            self.root_generation,
            value["binding"],
            len(journal.queue.inventory_ids),
            None,
        )


def _owned_current(
    ledger: Any,
    identity: Any,
    *,
    expected_binding: Mapping[str, object],
    expected_root_sha256: str,
    expected_generation: int,
    max_scan_pages: int | None,
    now: Any,
) -> tuple[Any, dict[str, Any]]:
    binding = validate_binding(json.loads(canonical_bytes(dict(expected_binding))))
    if (
        not ledger.enable_history_graph
        or not isinstance(ledger.evidence_budget, EvidenceReadBudget)
        or ledger.evidence_budget._tail_ticket is not None
        or ledger._activation_ticket is not None
        or type(expected_generation) is not int
        or not 0 <= expected_generation <= 2147483647
        or type(expected_root_sha256) is not str
        or re.fullmatch(r"[a-f0-9]{64}", expected_root_sha256) is None
    ):
        raise ReviewInputError("history association requires enabled original reader")
    record, envelope = ledger._history_load(
        identity, max_scan_pages=max_scan_pages, now=now
    )
    if (
        record is None
        or record.record_sha256 != expected_root_sha256
        or record.generation != expected_generation
        or record.assessment_queue is None
        or record.assessment_queue["state_manifest"]["binding"] != binding
    ):
        raise ReviewInputError("history owned root is stale or context differs")
    payload = history_payload(record, envelope)
    ledger._history_head(identity, binding)
    return record, payload


def associate_history(
    ledger: Any,
    identity: Any,
    *,
    expected_binding: Mapping[str, object],
    expected_root_sha256: str,
    expected_generation: int,
    max_scan_pages: int | None = None,
    now: Any = None,
) -> HistoryAssociation:
    record, payload = _owned_current(
        ledger,
        identity,
        expected_binding=expected_binding,
        expected_root_sha256=expected_root_sha256,
        expected_generation=expected_generation,
        max_scan_pages=max_scan_pages,
        now=now,
    )
    encoded = canonical_bytes(payload)
    digest = hashlib.sha256(canonical_bytes(payload["sources"])).hexdigest()
    proof = HistoryAssociation(
        record.record_sha256, record.generation, digest, encoded, object()
    )
    if len(ledger._history_associations) >= 64:
        raise ReviewInputError("history association inventory is exhausted")
    if (
        sum(len(entry[0]._payload) for entry in ledger._history_associations.values())
        + len(encoded)
        > 2_097_152
    ):
        raise ReviewInputError("history association bytes exceed finite bound")
    ledger._history_associations[proof._token] = (
        proof,
        ledger.evidence_budget,
        identity,
        max_scan_pages,
        (
            proof.root_sha256,
            proof.root_generation,
            proof.inventory_sha256,
            proof._payload,
        ),
    )
    return proof


def read_associated_history(
    ledger: Any,
    proof: HistoryAssociation,
    *,
    source_digest: str,
    operation_id: str,
    now: Any = None,
) -> object:
    if any(
        type(value) is not str or re.fullmatch(r"[a-f0-9]{64}", value) is None
        for value in (source_digest, operation_id)
    ):
        raise ReviewInputError("history lookup identity is invalid")
    if not isinstance(proof, HistoryAssociation):
        raise ReviewInputError("history association proof is unavailable")
    registered = ledger._history_associations.get(proof._token)
    if (
        registered is None
        or registered[0] is not proof
        or registered[1] is not ledger.evidence_budget
    ):
        raise ReviewInputError("history association belongs to another reader")
    _, _, identity, scan_pages, sealed = registered
    if sealed != (
        proof.root_sha256,
        proof.root_generation,
        proof.inventory_sha256,
        proof._payload,
    ):
        raise ReviewInputError("history association sealed proof changed")
    payload = json.loads(proof._payload)
    if (
        hashlib.sha256(canonical_bytes(payload["sources"])).hexdigest()
        != proof.inventory_sha256
    ):
        raise ReviewInputError("history association inventory changed")
    selected = [
        row
        for row in payload["sources"]
        if row["source_digest"] == source_digest and row["operation_id"] == operation_id
    ]
    if len(selected) != 1:
        raise ReviewInputError("history lookup is not a current owned member")
    for phase in ("before", "after"):
        _, latest = _owned_current(
            ledger,
            identity,
            expected_binding=payload["binding"],
            expected_root_sha256=proof.root_sha256,
            expected_generation=proof.root_generation,
            max_scan_pages=scan_pages,
            now=now,
        )
        if canonical_bytes(latest) != proof._payload:
            raise ReviewInputError("history current reference inventory changed")
        if phase == "before":
            packet = _read_history_child(ledger, identity, payload, selected[0])
    return packet
