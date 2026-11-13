"""Closed action-result carriers. History commitments are data, never authority.

V2 keeps the full semantic history in the authenticated finding reference and
binds it outside compression, so unknown digest entropy cannot alter wire width.
The caller must supply that exact current/archive root's history when decoding.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .bounded_evidence import (
    MAX_MANIFEST_BYTES,
    MAX_PARTITION_DECODED_BYTES,
    canonical_bytes,
    decode_evidence,
    encode_evidence,
)
from .errors import ReviewInputError

HISTORY_KEY = "accepted_semantic_history"
V1 = "caller-action-result-v1"
V2 = "caller-action-result-v2"
COMMITMENT_KEY = "accepted_semantic_history_sha256"


def validate_action_result_envelope(value: object) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "intent_sha256", "outcome", "result"}
        or value["schema_version"] != V1
        or not isinstance(value["intent_sha256"], str)
        or re.fullmatch(r"[a-f0-9]{64}", value["intent_sha256"]) is None
        or not isinstance(value["outcome"], dict)
        or not isinstance(value["result"], dict)
    ):
        raise ReviewInputError("caller action result envelope is unsupported")
    return value


def encode_action_result_carrier(document: object, *, reference_history=False):
    full_bytes = canonical_bytes(validate_action_result_envelope(document))
    if len(full_bytes) > MAX_PARTITION_DECODED_BYTES:
        raise ReviewInputError("action result exceeds the decoded byte bound")
    envelope = json.loads(full_bytes)
    history = envelope["result"].get(HISTORY_KEY)
    committed = reference_history and history is not None
    if committed:
        if (
            not isinstance(history, dict)
            or len(canonical_bytes(history)) > MAX_MANIFEST_BYTES
        ):
            raise ReviewInputError("caller action history must be bounded data")
        del envelope["result"][HISTORY_KEY]
    carrier = {
        "schema_version": V2 if committed else V1,
        "payload": encode_evidence(
            envelope, max_decoded_bytes=MAX_PARTITION_DECODED_BYTES
        ),
    }
    if committed:
        carrier[COMMITMENT_KEY] = hashlib.sha256(canonical_bytes(history)).hexdigest()
    return carrier


def decode_action_result_carrier(value: object, *, history=None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReviewInputError("caller action result carrier is unsupported")
    version = value.get("schema_version")
    keys = {"schema_version", "payload"}
    if version == V2:
        keys.add(COMMITMENT_KEY)
    if version not in {V1, V2} or set(value) != keys:
        raise ReviewInputError("caller action result carrier is unsupported")
    document = validate_action_result_envelope(
        decode_evidence(
            value["payload"],
            max_encoded_bytes=MAX_MANIFEST_BYTES,
            max_decoded_bytes=MAX_PARTITION_DECODED_BYTES,
        )
    )
    if version == V2:
        digest = value[COMMITMENT_KEY]
        if (
            not isinstance(digest, str)
            or re.fullmatch(r"[a-f0-9]{64}", digest) is None
            or not isinstance(history, dict)
            or len(canonical_bytes(history)) > MAX_MANIFEST_BYTES
            or hashlib.sha256(canonical_bytes(history)).hexdigest() != digest
            or HISTORY_KEY in document["result"]
        ):
            raise ReviewInputError(
                "caller action retained history commitment conflicts or is missing"
            )
        document = {
            **document,
            "result": {
                **document["result"],
                HISTORY_KEY: json.loads(canonical_bytes(history)),
            },
        }
    if len(canonical_bytes(document)) > MAX_PARTITION_DECODED_BYTES:
        raise ReviewInputError("action result exceeds the decoded byte bound")
    return document


from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
