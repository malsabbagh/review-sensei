"""Closed encoded envelope for source-owned operation metadata comments.

This profile preserves legacy readers. Guarded producers require hashed grant
keys; unsupported existing metadata refuses before creation. A byte bound is
capacity only and never authenticates an operation, accepted result or grant.
"""

from __future__ import annotations

import json
import math
import re

from review_sensei.errors import ReviewInputError

from .operation_host import (
    _COMMENT_BOUND,
    _TRANSITION_FIELDS,
    MAX_TRANSITIONS_PER_EVENT,
    OPERATION_COMMENT_MARKER,
    _decode_event,
    _decode_index,
    _public_transition,
    comment_payload,
    render_marked_comment,
)

OPERATION_WIRE_BOUND = 65_536
_SHA = re.compile(r"[a-f0-9]{64}")


def operation_envelope_certificate() -> dict[str, int]:
    """Quote/escape bound for the exact existing canonical ASCII codec.

    Payload strings contain no quotes or backslashes: hashes, bounded ASCII
    plan/obligation labels and base64. The event-key tab is independently
    charged. An entry with four shortest obligations has at most58 quotes and
    at least800 bytes. Scope/grant/index entries have a lower escape density;
    their keys are64-byte digests. The finite event/wrapper overhead is256.
    Thus one extra encoded byte per12 text bytes is conservative. Literal
    Markdown newlines and the outer body object consume at most16 more bytes.
    """
    raw: dict[str, object] = {key: 0 for key in _TRANSITION_FIELDS}
    for key in (
        "scope_digest",
        "event_id",
        "attempt_id",
        "prior_root_sha",
        "target_root_sha",
        "request_digest",
        "dispatch_digest",
    ):
        raw[key] = "a" * 64
    raw.update(encoded="a", state="pending", obligation_ids=("a", "b", "c", "d"))
    transition = json.dumps(
        {"a" * 64: _public_transition(raw)}, sort_keys=True, separators=(",", ":")
    )
    # This is a source-version fence: a changed codec needs a new proof.
    if (
        MAX_TRANSITIONS_PER_EVENT != 64
        or len(transition) < 800
        or transition.count('"') > 58
        or _COMMENT_BOUND != 60_000
    ):
        raise ReviewInputError("operation metadata source envelope changed")
    encoded = _COMMENT_BOUND + math.ceil(_COMMENT_BOUND / 12) + 256 + 16
    if encoded > OPERATION_WIRE_BOUND:
        raise ReviewInputError("operation metadata does not fit its native envelope")
    return {
        "text_bytes": _COMMENT_BOUND,
        "encoded_upper_bound": encoded,
        "reserved_wire_bytes": OPERATION_WIRE_BOUND,
        "transition_minimum_bytes": 800,
        "transition_maximum_quotes": 58,
    }


def require_operation_envelope(body: str) -> None:
    """Check the producer profile and exact physical representation together."""
    operation_envelope_certificate()
    if not isinstance(body, str) or not body.startswith(
        OPERATION_COMMENT_MARKER + "\n"
    ):
        raise ReviewInputError("guarded operation metadata marker is invalid")
    try:
        document = json.loads(comment_payload(body))
        if (
            not isinstance(document, dict)
            or render_marked_comment(OPERATION_COMMENT_MARKER, document) != body
        ):
            raise ValueError("noncanonical operation metadata")
        if document.get("kind") == "event":
            _decode_event(document)
        elif document.get("kind") == "index":
            _decode_index(document)
        else:
            raise ValueError("unsupported operation metadata")
        grants = document.get("grants", {})
        if not isinstance(grants, dict) or any(
            not isinstance(key, str) or _SHA.fullmatch(key) is None for key in grants
        ):
            raise ValueError("unqualified legacy grant keys")
        if (
            len(body.encode()) > _COMMENT_BOUND
            or len(json.dumps({"body": body}, separators=(",", ":")).encode())
            > OPERATION_WIRE_BOUND
        ):
            raise ValueError("operation metadata exceeds encoded reserve")
    except (ValueError, TypeError, KeyError, UnicodeError) as error:
        raise ReviewInputError(
            "guarded operation metadata envelope is unsupported"
        ) from error
