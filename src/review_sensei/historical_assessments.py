"""Historical assessment obligations, distinct from current model findings.

Stored values preserve evidence; they authorize neither execution nor a human
decision. Hosted adapters must authenticate the current source and exact target.
"""

from __future__ import annotations

import hashlib
import json

from .bounded_evidence import canonical_bytes
from .errors import ReviewInputError

MAX_HOLD_BYTES = 24_000


def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def scope_digest(source, assessments):
    return digest(
        {
            "domain": "historical-assessment-v1",
            "source": source,
            "assessments": assessments,
        }
    )


def normalize_hold(value):
    if value is None:
        return None
    from .schemas import validate_public_document

    value = json.loads(json.dumps(value))
    validate_public_document(value, "historical-assessment-hold")
    source, assessments = value["source"], value["assessments"]
    ids = [item["instance"] for item in assessments]
    decisions = [item["instance"] for item in value["settlements"]]
    if (
        source.get("schema_version") != "caller-run-reference-v2"
        or source["completion"]["human_inventory_sha256"] != digest(assessments)
        or value["scope_sha256"] != scope_digest(source, assessments)
        or len(set(ids)) != len(ids)
        or len(set(decisions)) != len(decisions)
        or not set(decisions).issubset(ids)
        or len(canonical_bytes(value)) > MAX_HOLD_BYTES
        or any(
            not d["reason"].strip() or not d["reason"].isprintable()
            for d in value["settlements"]
        )
    ):
        raise ReviewInputError("historical assessment scope or state conflicts")
    return value


def initial_hold(source, assessments):
    if source["completion"]["human_inventory_sha256"] != digest(assessments):
        raise ReviewInputError("historical human inventory differs from completion")
    if not assessments:
        return None
    return normalize_hold(
        {
            "schema_version": "historical-assessment-hold-v1",
            "source": source,
            "assessments": assessments,
            "scope_sha256": scope_digest(source, assessments),
            "settlements": [],
        }
    )


def pending_assessments(value):
    value = normalize_hold(value)
    if value is None:
        return ()
    settled = {d["instance"] for d in value["settlements"]}
    return tuple(a for a in value["assessments"] if a["instance"] not in settled)


def require_hold_successor(before, after):
    before, after = normalize_hold(before), normalize_hold(after)
    if before is None:
        return
    if (
        after is None
        or any(
            before[k] != after[k]
            for k in ("schema_version", "source", "scope_sha256", "assessments")
        )
        or after["settlements"][: len(before["settlements"])] != before["settlements"]
    ):
        raise ReviewInputError("historical assessments cannot be removed or rewritten")


def hold_view(value):
    value = normalize_hold(value)
    if value is None:
        return None
    return {
        "scope_sha256": value["scope_sha256"],
        "state_sha256": digest(value),
        "pending": [a["instance"] for a in pending_assessments(value)],
        "review_id": value["source"]["completion"]["review_id"],
        "historical_head_sha": value["source"]["completion"]["historical_head_sha"],
    }


def validate_view(value):
    import re

    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "scope_sha256",
            "state_sha256",
            "pending",
            "fresh_human_pending",
            "review_id",
            "historical_head_sha",
        }
        or any(
            not isinstance(value[k], str)
            or re.fullmatch(r"[a-f0-9]{64}", value[k]) is None
            for k in ("scope_sha256", "state_sha256")
        )
        or type(value["review_id"]) is not int
        or not 0 < value["review_id"] <= 9007199254740991
        or not isinstance(value["historical_head_sha"], str)
        or re.fullmatch(r"[a-f0-9]{40}", value["historical_head_sha"]) is None
        or type(value["fresh_human_pending"]) is not bool
        or not isinstance(value["pending"], list)
        or len(value["pending"]) > 100
        or any(
            not isinstance(p, str) or re.fullmatch(r"[a-f0-9]{64}", p) is None
            for p in value["pending"]
        )
        or len(set(value["pending"])) != len(value["pending"])
    ):
        raise ReviewInputError("historical assessment eligibility view is invalid")
    return json.loads(json.dumps(value))
