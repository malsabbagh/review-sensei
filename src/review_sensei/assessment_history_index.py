"""Proposed exact-source history index over authenticated immutable checkpoints.

The host must authenticate the latest queue root, its complete transitive part
inventory, each exact child producer/binding, and original control accounting.
This prototype supplies no transport, grant, infrastructure or ambient authority.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from . import bounded_evidence
from .assessment_queue import (
    MAX_OPERATION_RECEIPT_BYTES,
    MAX_RETAINED_ASSESSMENT_OPERATIONS,
    MAX_RETAINED_FACTORED_OPERATIONS,
    AssessmentJournal,
    AssessmentQueue,
    FactoredAssessmentJournal,
    QueueHostState,
)
from .bounded_evidence import canonical_bytes
from .errors import ReviewInputError

INDEX_INTERFACE_VERSION = "assessment-history-index-v1"
MAX_INDEXED_FRAMED_PART_BYTES = 32768
FRAMING_RESERVE = 512
_HASH = re.compile(r"[a-f0-9]{64}")


def history_envelope(journal: Mapping[str, object]) -> dict[str, object]:
    """Exact opt-in host carrier; no metadata supplies independent authority."""
    return {
        "schema_version": "1.0",
        "kind": "assessment-queue-state",
        "journal": dict(journal),
    }


def _unwrap(value: object) -> object:
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "kind", "journal"}
        or value["schema_version"] != "1.0"
        or value["kind"] != "assessment-queue-state"
    ):
        raise ReviewInputError("indexed history host envelope is invalid")
    return value["journal"]


def _hash(value: object) -> bool:
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


def _receipt(row: dict[str, object]) -> dict[str, object]:
    value = row.get("receipt")
    if not isinstance(value, dict):
        raise ReviewInputError("indexed history receipt is invalid")
    return value


@dataclass(frozen=True)
class HistoryPartReference:
    storage_id: str
    sha256: str
    bytes: int
    decoded_bytes: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.storage_id, str)
            or re.fullmatch(r"(?:[a-f0-9]{64}|[1-9][0-9]{0,18})", self.storage_id)
            is None
            or not _hash(self.sha256)
            or isinstance(self.bytes, bool)
            or not isinstance(self.bytes, int)
            or not 1 <= self.bytes <= MAX_INDEXED_FRAMED_PART_BYTES - FRAMING_RESERVE
            or isinstance(self.decoded_bytes, bool)
            or not isinstance(self.decoded_bytes, int)
            or not 1 <= self.decoded_bytes <= MAX_OPERATION_RECEIPT_BYTES
        ):
            raise ReviewInputError("indexed history part reference is invalid")

    def to_document(self) -> dict[str, object]:
        return {
            "storage_id": self.storage_id,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "decoded_bytes": self.decoded_bytes,
        }

    @classmethod
    def from_document(cls, value: object) -> HistoryPartReference:
        if not isinstance(value, dict) or set(value) != {
            "storage_id",
            "sha256",
            "bytes",
            "decoded_bytes",
        }:
            raise ReviewInputError("indexed history part reference is invalid")
        return cls(**value)


def resolve_history_part(
    reference: HistoryPartReference,
    *,
    reader: Callable[[str], Any],
    expected_binding: Mapping[str, object],
    budget: Any,
    item_count: int,
) -> object:
    """Read one exact owned part under the host's ORIGINAL shared allowance.

    reader charges its actual dispatch and returns A.AuthenticatedPart after
    authenticating producer/repo/PR. Transitive root/part association must be
    reviewed by A before this helper is composed into a public hosted route.
    A's decoder performs bounded inflation/inventory hash/ordering validation.
    """
    part_type = getattr(bounded_evidence, "AuthenticatedPart", None)
    decoder = getattr(bounded_evidence, "read_partitioned_evidence", None)
    encoding = getattr(bounded_evidence, "PARTITION_ENCODING", None)
    if (
        not isinstance(part_type, type)
        or not callable(decoder)
        or not isinstance(encoding, str)
    ):
        raise ReviewInputError("authenticated history part adapter is unavailable")
    if (
        isinstance(item_count, bool)
        or not isinstance(item_count, int)
        or not 1 <= item_count <= 250
    ):
        raise ReviewInputError("indexed history complete inventory count is invalid")
    budget.check()
    loaded = reader(reference.storage_id)
    budget.check()
    if not isinstance(loaded, part_type) or getattr(
        loaded, "producer", None
    ) != expected_binding.get("producer"):
        raise ReviewInputError("indexed history owner authentication failed")
    part = getattr(loaded, "document", None)
    if not isinstance(part, dict) or set(part) != {
        "encoding",
        "binding_sha256",
        "inventory_sha256",
        "index",
        "count",
        "data",
    }:
        raise ReviewInputError("indexed history part is malformed")
    if (
        not isinstance(part["data"], str)
        or len(part["data"]) > MAX_INDEXED_FRAMED_PART_BYTES
        or not _hash(part["binding_sha256"])
        or not _hash(part["inventory_sha256"])
    ):
        raise ReviewInputError("indexed history part exceeds bounds")
    wire = canonical_bytes(part)
    if (
        len(wire) != reference.bytes
        or hashlib.sha256(wire).hexdigest() != reference.sha256
        or part["index"] != 0
        or part["count"] != 1
        or isinstance(part["index"], bool)
        or isinstance(part["count"], bool)
    ):
        raise ReviewInputError("indexed history part integrity failed")
    manifest = {
        "encoding": encoding,
        "binding": dict(expected_binding),
        "sha256": part["inventory_sha256"],
        "decoded_bytes": reference.decoded_bytes,
        "encoded_bytes": reference.bytes + FRAMING_RESERVE,
        "item_count": item_count,
        "parts": [
            {
                key: value
                for key, value in reference.to_document().items()
                if key != "decoded_bytes"
            }
        ],
    }

    # The cached reader reuses this exact already-charged immutable fetch; it
    # cannot dispatch another request or resolve a different ID.
    def cached(storage_id: str) -> Any:
        if storage_id != reference.storage_id:
            raise ReviewInputError("indexed history resolver changed identity")
        return loaded

    return _unwrap(
        decoder(
            manifest, reader=cached, expected_binding=expected_binding, budget=budget
        )
    )


class IndexedAssessmentJournal:
    """Complete exact-source index plus one current inline original receipt.

    Prior accepted checkpoint parts become segments without additional writes.
    Every source remains indexed; no GC/tombstone expiry renews allowances.
    Resolving an old source performs one direct authenticated read, never follows
    that old packet's earlier-history links or infers omitted history.
    """

    def __init__(
        self,
        queue: AssessmentQueue,
        *,
        current: dict[str, object] | None = None,
        sources: tuple[dict[str, object], ...] = (),
        current_reference: HistoryPartReference | None = None,
        current_read_accounting: Mapping[str, int] | None = None,
        reader: Callable[[HistoryPartReference], object] | None = None,
    ) -> None:
        if (
            not isinstance(sources, tuple)
            or len(sources) + (current is not None) > MAX_RETAINED_FACTORED_OPERATIONS
        ):
            raise ReviewInputError("indexed history exceeds retained operation bounds")
        state = FactoredAssessmentJournal(
            queue, (current,) if current is not None else ()
        )
        seen = set()
        active = int(
            current is not None
            and not FactoredAssessmentJournal._settled(_receipt(current))
        )
        refs = set()
        for item in sources:
            if (
                not isinstance(item, dict)
                or set(item)
                != {
                    "source_digest",
                    "operation_id",
                    "reference",
                    "active",
                    "read_accounting",
                }
                or not _hash(item["source_digest"])
                or not _hash(item["operation_id"])
                or not isinstance(item["active"], bool)
                or item["source_digest"] in seen
                or (
                    current is not None
                    and item["source_digest"] == current["source_digest"]
                )
            ):
                raise ReviewInputError("indexed history source binding is invalid")
            self._accounting(item["read_accounting"])
            reference = HistoryPartReference.from_document(item["reference"])
            if reference.storage_id in refs:
                raise ReviewInputError("indexed history aliases source segments")
            refs.add(reference.storage_id)
            seen.add(item["source_digest"])
            active += item["active"]
        if active > MAX_RETAINED_ASSESSMENT_OPERATIONS:
            raise ReviewInputError("indexed history exceeds active operation bounds")
        document = {
            "schema_version": INDEX_INTERFACE_VERSION,
            "state": state.to_document(),
            "sources": list(sources),
        }
        self._bytes = canonical_bytes(document)
        if len(self._bytes) > MAX_OPERATION_RECEIPT_BYTES:
            raise ReviewInputError("indexed history exceeds decoded root bounds")
        if current_reference is not None and current_reference.decoded_bytes != len(
            canonical_bytes(history_envelope(document))
        ):
            raise ReviewInputError("indexed history current reference size differs")
        if current_read_accounting is not None:
            self._accounting(dict(current_read_accounting))
        self.queue = queue
        self.current_reference = current_reference
        self.current_read_accounting = (
            dict(current_read_accounting)
            if current_read_accounting is not None
            else None
        )
        self.reader = reader
        self._cache: dict[str, dict[str, object]] = {}

    @staticmethod
    def _accounting(value: object) -> None:
        if (
            not isinstance(value, dict)
            or set(value) != {"calls", "deadline_at_ms"}
            or any(
                isinstance(value[key], bool) or not isinstance(value[key], int)
                for key in value
            )
            or not 0 <= value["calls"] <= 64
            or not 1 <= value["deadline_at_ms"] <= 253402300799999
        ):
            raise ReviewInputError(
                "indexed history original control accounting is invalid"
            )

    def to_document(self) -> dict[str, object]:
        return json.loads(self._bytes)

    def _current(self) -> dict[str, object] | None:
        operations = FactoredAssessmentJournal.from_document(
            self.to_document()["state"]
        )._operations()
        return operations[0] if operations else None

    def _sources(self) -> tuple[dict[str, object], ...]:
        value = self.to_document()["sources"]
        assert isinstance(value, list)
        return tuple(value)

    def history_references(self) -> tuple[HistoryPartReference, ...]:
        """Complete direct source segment inventory for A graph association.

        This does not grant access or request recursive history traversal. A
        must separately authenticate ownership and retention before activation.
        The current root manifest supplies the current inline receipt reference.
        """
        return tuple(
            HistoryPartReference.from_document(item["reference"])
            for item in self._sources()
        )

    def source_read_accounting(
        self,
        *,
        source_digest: str,
        operation_id: str,
        authenticated_root: QueueHostState,
        expected_binding: Mapping[str, object],
    ) -> dict[str, int] | None:
        """Original sealed control summary from an authenticated latest root.

        authenticated_root is the trusted host reader's latest A-fenced root
        proof, not a document decoded from prose. Its envelope must bind this
        exact complete source index. The host remains responsible for actual
        producer/freshness/transitive association proof. No raw journal-only
        accessor supplies a control budget. Absence never permits refill.
        """
        if (
            not isinstance(authenticated_root, QueueHostState)
            or isinstance(authenticated_root.root_generation, bool)
            or not isinstance(authenticated_root.root_generation, int)
            or not 0 <= authenticated_root.root_generation < 2147483647
            or type(authenticated_root.item_count) is not int
            or authenticated_root.item_count != len(self.queue.inventory_ids)
            or canonical_bytes(authenticated_root.envelope)
            != canonical_bytes(history_envelope(self.to_document()))
            or canonical_bytes(dict(authenticated_root.binding))
            != canonical_bytes(dict(expected_binding))
            or any(
                expected_binding.get(key) != value
                for key, value in self.queue.snapshot.to_dict().items()
            )
            or expected_binding.get("purpose") != "queue"
        ):
            raise ReviewInputError(
                "indexed history accounting requires exact authenticated latest root"
            )
        if not _hash(source_digest) or not _hash(operation_id):
            raise ReviewInputError("indexed history lookup identity is invalid")
        current = self._current()
        if current is not None and current["source_digest"] == source_digest:
            if current["operation_id"] != operation_id:
                raise ReviewInputError("existing assessment source authority changed")
            if self.current_read_accounting is None:
                raise ReviewInputError(
                    "indexed current sealed accounting is unavailable"
                )
            return dict(self.current_read_accounting)
        for source in self._sources():
            if source["source_digest"] == source_digest:
                if source["operation_id"] != operation_id:
                    raise ReviewInputError(
                        "existing assessment source authority changed"
                    )
                value = source["read_accounting"]
                assert isinstance(value, dict)
                return dict(value)
        return None

    @classmethod
    def from_document(cls, value: object, **host: Any) -> IndexedAssessmentJournal:
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "state", "sources"}
            or value["schema_version"] != INDEX_INTERFACE_VERSION
        ):
            raise ReviewInputError("indexed history document is invalid")
        if len(canonical_bytes(value)) > MAX_OPERATION_RECEIPT_BYTES or not isinstance(
            value["sources"], list
        ):
            raise ReviewInputError("indexed history exceeds decoded root bounds")
        state = FactoredAssessmentJournal.from_document(value["state"])
        operations = state._operations()
        if len(operations) > 1:
            raise ReviewInputError("indexed history requires one current operation")
        result = cls(
            AssessmentQueue.from_document(state.to_document()["queue"]),
            current=operations[0] if operations else None,
            sources=tuple(value["sources"]),
            **host,
        )
        if result.to_document() != value:
            raise ReviewInputError("indexed history document is noncanonical")
        return result

    @classmethod
    def from_retained(
        cls,
        journal: AssessmentJournal,
        *,
        owned_segments: Mapping[str, HistoryPartReference],
        original_read_accounting: Mapping[str, Mapping[str, int]],
        reader: Callable[[HistoryPartReference], object],
    ) -> IndexedAssessmentJournal:
        """Explicit lossless v1/v2 migration after authenticated segmentation.

        The host must already have durably staged/read back every exact one-part
        inline receipt under its original authority/control envelopes. No child
        write, omission, inferred reference, or allowance is supplied here.
        Every child is revalidated once through the original-budget reader.
        Missing original sealed accounting or any unknown segment refuses.
        """
        if not isinstance(journal, AssessmentJournal):
            raise ReviewInputError("indexed history migration source is invalid")
        factored = (
            journal
            if isinstance(journal, FactoredAssessmentJournal)
            else FactoredAssessmentJournal.from_legacy(journal)
        )
        rows = factored._operations()
        identities = {row["source_digest"] for row in rows}
        if (
            set(owned_segments) != identities
            or set(original_read_accounting) != identities
        ):
            raise ReviewInputError(
                "indexed history migration must preserve every source"
            )
        queue = AssessmentQueue.from_document(factored.to_document()["queue"])
        sources = []
        for row in rows:
            identity = row["source_digest"]
            assert isinstance(identity, str)
            reference = owned_segments[identity]
            if not isinstance(reference, HistoryPartReference):
                raise ReviewInputError("indexed history migration reference is invalid")
            child = cls.from_document(reader(reference))
            if (
                child._current() != row
                or child.queue.snapshot != queue.snapshot
                or child.queue.inventory_digest != queue.inventory_digest
                or child.queue.inventory_ids != queue.inventory_ids
                or not set(child.queue.resolved_ids) <= set(queue.resolved_ids)
            ):
                raise ReviewInputError(
                    "indexed history migration child changed receipt"
                )
            sources.append(
                {
                    "source_digest": identity,
                    "operation_id": row["operation_id"],
                    "reference": reference.to_document(),
                    "active": not FactoredAssessmentJournal._settled(_receipt(row)),
                    "read_accounting": dict(original_read_accounting[identity]),
                }
            )
        return cls(queue, sources=tuple(sources), reader=reader)

    def receipt(self, *, source_digest: str, operation_id: str) -> object | None:
        if not _hash(source_digest) or not _hash(operation_id):
            raise ReviewInputError("indexed history lookup identity is invalid")
        current = self._current()
        if current is not None and current["source_digest"] == source_digest:
            if current["operation_id"] != operation_id:
                raise ReviewInputError("existing assessment source authority changed")
            return current["receipt"]
        for source in self._sources():
            if source["source_digest"] != source_digest:
                continue
            if source["operation_id"] != operation_id:
                raise ReviewInputError("existing assessment source authority changed")
            if source_digest in self._cache:
                return json.loads(canonical_bytes(self._cache[source_digest]))
            if self.reader is None:
                raise ReviewInputError(
                    "indexed history authenticated resolver is unavailable"
                )
            reference = HistoryPartReference.from_document(source["reference"])
            old = IndexedAssessmentJournal.from_document(self.reader(reference))
            candidate = old._current()
            if (
                candidate is None
                or candidate["source_digest"] != source_digest
                or candidate["operation_id"] != operation_id
                or old.queue.snapshot != self.queue.snapshot
                or old.queue.inventory_digest != self.queue.inventory_digest
                or old.queue.inventory_ids != self.queue.inventory_ids
                or not set(old.queue.resolved_ids) <= set(self.queue.resolved_ids)
                or source["active"]
                == FactoredAssessmentJournal._settled(_receipt(candidate))
            ):
                raise ReviewInputError("indexed history segment/source binding differs")
            self._cache[source_digest] = _receipt(candidate)
            return json.loads(canonical_bytes(candidate["receipt"]))
        return None

    def record(
        self, *, queue: AssessmentQueue, source_digest: str, receipt: dict[str, object]
    ) -> IndexedAssessmentJournal:
        operation_id = receipt.get("operation_id")
        if not _hash(operation_id):
            raise ReviewInputError("indexed history operation identity is invalid")
        assert isinstance(operation_id, str)
        previous = self.receipt(source_digest=source_digest, operation_id=operation_id)
        prior = (
            {
                "source_digest": source_digest,
                "operation_id": operation_id,
                "receipt": previous,
            }
            if previous is not None
            else None
        )
        updated = FactoredAssessmentJournal(
            self.queue, (prior,) if prior is not None else ()
        ).record(queue=queue, source_digest=source_digest, receipt=receipt)
        sources = [
            item for item in self._sources() if item["source_digest"] != source_digest
        ]
        current = self._current()
        if current is not None and current["source_digest"] != source_digest:
            if self.current_reference is None or self.current_read_accounting is None:
                raise ReviewInputError(
                    "indexed history archival requires authenticated owned readback"
                )
            sources.append(
                {
                    "source_digest": current["source_digest"],
                    "operation_id": current["operation_id"],
                    "reference": self.current_reference.to_document(),
                    "active": not FactoredAssessmentJournal._settled(_receipt(current)),
                    "read_accounting": self.current_read_accounting,
                }
            )
        return IndexedAssessmentJournal(
            AssessmentQueue.from_document(updated.to_document()["queue"]),
            current=updated._operations()[0],
            sources=tuple(sources),
            reader=self.reader,
        )
