"""Closed lossless projections; callers independently authenticate their source.

These codecs grant no publication, source-selection or admission authority.
Their loaders must read the original selected owned result, never the latest
unrelated root. Old standalone result/inventory payloads remain separate codecs.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.context import finding_lifecycle_for_comment
from review_sensei.errors import ReviewInputError
from review_sensei.human_assessment import (
    PendingHumanReview,
    finding_instance_fingerprint,
)
from review_sensei.human_inventory import inventory_result_document
from review_sensei.models import ReviewResult, ReviewTransaction
from review_sensei.validation import DEFAULT_REVIEW_LIMITS

CHECKPOINT_VERSION = "operation-checkpoint-projection-v1"
INVENTORY_VERSION = "admitted-inventory-projection-v1"
MAX_CHECKPOINT_PROJECTION_BYTES = 8192
MAX_INVENTORY_PROJECTION_BYTES = 65_536
NATIVE_RENDER_FORMAT = "literal-malformed-v1"
GROUPED_NATIVE_RENDER_FORMAT = "bounded-inline-groups-v1"
NATIVE_RENDER_FORMATS = frozenset({NATIVE_RENDER_FORMAT, GROUPED_NATIVE_RENDER_FORMAT})


def _sha(payload):
    return hashlib.sha256(payload).hexdigest()


def _bounded(document, maximum):
    payload = canonical_bytes(document)
    if len(payload) > maximum:
        raise ReviewInputError("retention projection exceeds its closed byte bound")
    return payload


def _decode(payload, maximum):
    if not isinstance(payload, bytes) or not 0 < len(payload) <= maximum:
        raise ReviewInputError("retention projection byte bound is invalid")
    value = json.loads(payload)
    if not isinstance(value, dict) or canonical_bytes(value) != payload:
        raise ReviewInputError("retention projection is not canonical")
    return value


def _result(payload):
    if (
        not isinstance(payload, bytes)
        or not 0 < len(payload) <= DEFAULT_REVIEW_LIMITS.max_result_bytes
    ):
        raise ReviewInputError("retention projection source byte bound is invalid")
    value = json.loads(payload)
    result = ReviewResult.from_dict(value)
    if canonical_bytes(result.to_dict()) != payload:
        raise ReviewInputError("retention projection source is not canonical")
    return result


def checkpoint_projection(
    source_payload,
    result,
    source_reference,
    *,
    native_render_format=NATIVE_RENDER_FORMAT,
):
    """Encode only the source-proven transaction/overflow transformation."""
    if (
        not isinstance(native_render_format, str)
        or native_render_format not in NATIVE_RENDER_FORMATS
    ):
        raise ReviewInputError("checkpoint native producer format is unsupported")
    source = _result(source_payload)
    if not isinstance(result, ReviewResult) or result.transaction is None:
        raise ReviewInputError("checkpoint projection requires a transaction")
    transaction = result.transaction
    if (
        transaction.phase != "publication_pending"
        or transaction.result_sha256 != result.content_digest()
    ):
        raise ReviewInputError("checkpoint projection transaction conflicts")
    overflow = (
        source.review_status == "complete"
        and source.persistence_status is None
        and result.review_status == "partial"
        and result.persistence_status == "capacity-exceeded"
    )
    expected = replace(
        source,
        transaction=transaction,
        review_status="partial" if overflow else source.review_status,
        persistence_status="capacity-exceeded"
        if overflow
        else source.persistence_status,
    )
    if expected.to_dict() != result.to_dict():
        raise ReviewInputError(
            "checkpoint projection changes unsupported result fields"
        )
    return _bounded(
        {
            "schema_version": CHECKPOINT_VERSION,
            "native_render_format": native_render_format,
            "source": source_reference,
            "source_payload_sha256": _sha(source_payload),
            "transaction": transaction.to_dict(),
            "baseline_overflow": overflow,
            "result_payload_sha256": _sha(canonical_bytes(result.to_dict())),
        },
        MAX_CHECKPOINT_PROJECTION_BYTES,
    )


def restore_checkpoint_projection(payload, transaction, load_authenticated_source):
    """Reconstruct all fields, then check the entire canonical payload hash."""
    value = _decode(payload, MAX_CHECKPOINT_PROJECTION_BYTES)
    if (
        set(value)
        != {
            "schema_version",
            "native_render_format",
            "source",
            "source_payload_sha256",
            "transaction",
            "baseline_overflow",
            "result_payload_sha256",
        }
        or value["schema_version"] != CHECKPOINT_VERSION
        or not isinstance(value["native_render_format"], str)
        or value["native_render_format"] not in NATIVE_RENDER_FORMATS
    ):
        raise ReviewInputError("checkpoint projection fields are invalid")
    bound = ReviewTransaction.from_dict(value["transaction"])
    if (
        not isinstance(transaction, ReviewTransaction)
        or not bound.logical_identity_matches(transaction)
        or bound.phase != "publication_pending"
        or bound.result_sha256 != transaction.result_sha256
        or type(value["baseline_overflow"]) is not bool
    ):
        raise ReviewInputError("checkpoint projection selected transaction conflicts")
    source_payload = load_authenticated_source(value["source"])
    if _sha(source_payload) != value["source_payload_sha256"]:
        raise ReviewInputError("checkpoint projection source payload changed")
    source = _result(source_payload)
    if value["baseline_overflow"]:
        if source.review_status != "complete" or source.persistence_status is not None:
            raise ReviewInputError("checkpoint projection overflow source conflicts")
    result = replace(
        source,
        transaction=bound,
        review_status="partial" if value["baseline_overflow"] else source.review_status,
        persistence_status="capacity-exceeded"
        if value["baseline_overflow"]
        else source.persistence_status,
    )
    restored = canonical_bytes(result.to_dict())
    if (
        _sha(restored) != value["result_payload_sha256"]
        or result.content_digest() != bound.result_sha256
    ):
        raise ReviewInputError("checkpoint projection reconstructed payload conflicts")
    return restored


def _inventory_document(source, projection, binding):
    from .publication import parse_finding_index, review_result_digest

    transaction = source.transaction
    if (
        transaction is None
        or transaction.transaction_id != binding["transaction_id"]
        or source.content_digest() != binding["original_content_digest"]
        or review_result_digest(source) != binding["publication_digest"]
        or not transaction.identity_matches(
            repository=binding["repository"],
            pull_request=binding["pull_request"],
            base_sha=binding["base_sha"],
            head_sha=binding["head_sha"],
            policy_digest=binding["policy_digest"],
            configuration_digest=binding["configuration_digest"],
            evidence_digest=binding["evidence_digest"],
        )
    ):
        raise ReviewInputError("inventory projection original transaction conflicts")
    index = parse_finding_index(
        projection["finding_index"],
        repository_id=binding["repository_id"],
        pull_request=binding["pull_request"],
        head_sha=binding["head_sha"],
    )
    rows = projection["admission"]
    if (
        index is None
        or index.base_sha != binding["base_sha"]
        or index.result_digest != binding["publication_digest"]
        or not isinstance(rows, list)
        or len(rows) != len(index.entries)
        or len(rows) > 250
    ):
        raise ReviewInputError("inventory projection index is invalid")
    originals: dict[str, int] = {}
    for ordinal, comment in enumerate(source.comments):
        originals.setdefault(finding_instance_fingerprint(comment), ordinal)
    if len(originals) != len(rows):
        raise ReviewInputError("inventory projection omits an original instance")
    admitted, seen = [], set()
    entries = {entry.instance: entry for entry in index.entries}
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != {"ordinal", "instance", "blocking", "human"}
            or type(row["ordinal"]) is not int
            or not 0 <= row["ordinal"] < len(source.comments)
            or type(row["blocking"]) is not bool
            or type(row["human"]) is not bool
            or not isinstance(row["instance"], str)
            or row["instance"] in seen
        ):
            raise ReviewInputError("inventory projection admission is invalid")
        comment = source.comments[row["ordinal"]]
        instance = finding_instance_fingerprint(comment)
        entry = entries.get(instance)
        if (
            instance != row["instance"]
            or originals.get(instance) != row["ordinal"]
            or entry is None
            or entry.lifecycle != finding_lifecycle_for_comment(comment).fingerprint
            or (entry.blocking, entry.needs_human) != (row["blocking"], row["human"])
        ):
            raise ReviewInputError("inventory projection instance/admission conflicts")
        seen.add(instance)
        admitted.append(
            replace(
                comment, effective_blocking=row["blocking"], needs_human=row["human"]
            )
        )
    inventory = PendingHumanReview.from_result(
        replace(source, comments=tuple(admitted)), binding["base_sha"]
    )
    return {
        "human_inventory": inventory_result_document(
            inventory, result_digest=binding["publication_digest"]
        )
        if inventory is not None
        else None,
        "finding_index": projection["finding_index"],
        "visible_prose": [],
    }


def inventory_projection(source_payload, document, binding, source_reference):
    """Keep immutable admitted flags/index; reference every original explanation."""
    from .publication import parse_finding_index

    source = _result(source_payload)
    if (
        not isinstance(document, dict)
        or set(document) != {"human_inventory", "finding_index", "visible_prose"}
        or document["visible_prose"] != []
    ):
        raise ReviewInputError("inventory projection route is unsupported")
    index = parse_finding_index(
        document["finding_index"],
        repository_id=binding["repository_id"],
        pull_request=binding["pull_request"],
        head_sha=binding["head_sha"],
    )
    if index is None:
        raise ReviewInputError("inventory projection finding index is absent")
    ordinals: dict[str, int] = {}
    for ordinal, comment in enumerate(source.comments):
        ordinals.setdefault(finding_instance_fingerprint(comment), ordinal)
    projection = {
        "schema_version": INVENTORY_VERSION,
        "binding": binding,
        "source": source_reference,
        "source_payload_sha256": _sha(source_payload),
        "finding_index": document["finding_index"],
        "admission": [
            {
                "ordinal": ordinals.get(entry.instance),
                "instance": entry.instance,
                "blocking": entry.blocking,
                "human": entry.needs_human,
            }
            for entry in index.entries
        ],
        "document_sha256": _sha(canonical_bytes(document)),
    }
    if _inventory_document(source, projection, binding) != document:
        raise ReviewInputError("inventory projection cannot preserve the full document")
    return _bounded(projection, MAX_INVENTORY_PROJECTION_BYTES)


def restore_inventory_projection(payload, binding, load_authenticated_source):
    projection = _decode(payload, MAX_INVENTORY_PROJECTION_BYTES)
    if (
        set(projection)
        != {
            "schema_version",
            "binding",
            "source",
            "source_payload_sha256",
            "finding_index",
            "admission",
            "document_sha256",
        }
        or projection["schema_version"] != INVENTORY_VERSION
        or projection["binding"] != binding
    ):
        raise ReviewInputError("inventory projection stored binding conflicts")
    source_payload = load_authenticated_source(projection["source"])
    if _sha(source_payload) != projection["source_payload_sha256"]:
        raise ReviewInputError("inventory projection original result changed")
    document = _inventory_document(_result(source_payload), projection, binding)
    if _sha(canonical_bytes(document)) != projection["document_sha256"]:
        raise ReviewInputError("inventory projection reconstructed document conflicts")
    return document
