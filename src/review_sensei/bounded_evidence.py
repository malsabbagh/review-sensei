"""Lossless inline evidence; the owning ledger authenticates this envelope.

Decode before parsing with a hard expansion budget. No external objects, URLs,
file access or partial inventories are involved.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import zlib

from .errors import ReviewInputError

ENCODING = "zlib-json-v1"


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def encode_evidence(value: object, *, max_decoded_bytes: int) -> dict[str, object]:
    raw = canonical_bytes(value)
    if len(raw) > max_decoded_bytes:
        raise ReviewInputError("evidence exceeds the decoded byte bound")
    return {
        "encoding": ENCODING,
        "decoded_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "data": base64.b64encode(zlib.compress(raw, level=9)).decode("ascii"),
    }


def decode_evidence(
    value: object, *, max_encoded_bytes: int, max_decoded_bytes: int
) -> object:
    if not isinstance(value, dict) or set(value) != {
        "encoding",
        "decoded_bytes",
        "sha256",
        "data",
    }:
        raise ReviewInputError("encoded evidence has an invalid shape")
    size = value["decoded_bytes"]
    digest = value["sha256"]
    data = value["data"]
    if (
        value["encoding"] != ENCODING
        or isinstance(size, bool)
        or not isinstance(size, int)
        or not 0 < size <= max_decoded_bytes
        or not isinstance(digest, str)
        or len(digest) != 64
        or not isinstance(data, str)
        or len(data) > max_encoded_bytes
        or len(canonical_bytes(value)) > max_encoded_bytes
    ):
        raise ReviewInputError("encoded evidence exceeds its contract")
    try:
        packed = base64.b64decode(data, validate=True)
        decoder = zlib.decompressobj()
        raw = decoder.decompress(packed, size + 1)
        if (
            len(raw) != size
            or not decoder.eof
            or decoder.unused_data
            or decoder.unconsumed_tail
        ):
            raise ValueError("invalid stream")
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("invalid digest")
        document = json.loads(
            raw,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("nonfinite number")
            ),
        )
        # Reject duplicate keys, noncanonical forms and nonfinite JSON numbers.
        if canonical_bytes(document) != raw:
            raise ValueError("noncanonical document")
        return document
    except (
        ValueError,
        UnicodeError,
        binascii.Error,
        zlib.error,
        RecursionError,
    ) as exc:
        raise ReviewInputError("encoded evidence is invalid") from exc
