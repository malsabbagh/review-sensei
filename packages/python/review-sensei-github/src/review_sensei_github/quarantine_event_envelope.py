"""Pure capacity experiment for the existing one-part assessment-event writer.

This calculation is NOT an admission receipt. No runtime writer or census uses
it. In particular, the generic cursor accepts multi-part manifests; a future
consumer must independently prove an actual source-owned one-part chain before
using this narrower envelope. Existing archives retain their full bounds.
"""

from dataclasses import dataclass

from review_sensei.bounded_evidence import (
    MAX_PART_BYTES,
    MAX_PARTITION_DECODED_BYTES,
    PACKED_CHUNK_BYTES,
    PART_ENCODING,
    PART_FRAMING_RESERVE,
    PARTITION_ENCODING,
    canonical_bytes,
    validate_binding,
    validate_manifest,
)
from review_sensei.quarantine_state import assessment_cursor, next_assessment_event

from .quarantine_creation import _body


@dataclass(frozen=True)
class EventEnvelope:
    decoded_bytes: int
    compressed_bytes: int
    part_bytes: int
    body_bytes: int
    body_json_bytes: int


def one_part_event_envelope() -> EventEnvelope:
    """Bound every closed event scalar, without assuming compressibility.

    Canonical JSON uses ensure_ascii=True. A printable reason of <=4096 UTF-8
    bytes expands at most threefold: two-byte scalars need six ASCII bytes;
    ASCII quote/backslash need two, BMP three-byte scalars six, and non-BMP
    four-byte scalars twelve. U+00A1 attains the maximum. Every other variable
    field is ASCII, bounded by its existing validator. The previous manifest
    has ONE part, but retains generic 64-character storage IDs and all maximum
    numeric widths, not optimistic actual IDs. No source or authority follows
    from the synthetic values used to measure the envelope.
    """
    digest = "f" * 64
    binding = validate_binding(
        {
            "repository": "a" * 127 + "/" + "b" * 128,
            "repository_id": 2_147_483_647,
            "pull_request": 2_147_483_647,
            "base_sha": "f" * 40,
            "head_sha": "f" * 40,
            "policy_digest": digest,
            "configuration_digest": digest,
            "generation": 2_147_483_647,
            "producer": "github-bot:" + "9" * 19,
            "purpose": "evidence",
            "schema_version": "1.0",
        }
    )
    previous = validate_manifest(
        {
            "encoding": PARTITION_ENCODING,
            "binding": binding,
            "item_count": 1,
            "decoded_bytes": MAX_PARTITION_DECODED_BYTES,
            "sha256": digest,
            "encoded_bytes": MAX_PART_BYTES,
            "parts": [
                {
                    "storage_id": "z" * 64,
                    "bytes": MAX_PART_BYTES - PART_FRAMING_RESERVE,
                    "sha256": digest,
                }
            ],
        }
    )
    event = next_assessment_event(
        assessment_cursor(scope=digest, count=199, tail_manifest=previous),
        {
            "origin_scope_sha256": digest,
            "instance": digest,
            "action": "accept-risk",
            "actor": "a" * 39,
            "role": "maintain",
            "head_sha": "f" * 40,
            "source_id": 9_007_199_254_740_991,
            "source_updated_at": "9999-12-31T23:59:59Z",
            "source_body_sha256": digest,
            "reason": "\u00a1" * 2048,
            "policy_sha256": digest,
        },
    )
    raw = len(canonical_bytes(event))
    # zlib compressBound for the actual zlib-wrapped codec, not raw DEFLATE.
    compressed = raw + (raw >> 12) + (raw >> 14) + (raw >> 25) + 13
    if compressed > PACKED_CHUNK_BYTES:
        raise ValueError("assessment event envelope is not a single codec chunk")
    part = {
        "encoding": PART_ENCODING,
        "binding_sha256": digest,
        "inventory_sha256": digest,
        "index": 0,
        "count": 1,
        "data": "A" * (4 * ((compressed + 2) // 3)),
    }
    body = _body(part)
    return EventEnvelope(
        raw,
        compressed,
        len(canonical_bytes(part)),
        len(body.encode()),
        len(canonical_bytes({"body": body})),
    )
