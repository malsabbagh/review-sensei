"""Quarantine lineage values: preservation, never predecessor completion.

Integrity-checked references alone grant no creation, inference or decision
authority. The hosted adapter must independently authenticate every carrier,
the execution slot, current authority and the isolated predecessor targets.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, cast

from .bounded_evidence import (
    INLINE_ENCODING,
    MAX_MANIFEST_BYTES,
    canonical_bytes,
    read_inline_evidence,
    validate_manifest,
)
from .errors import ReviewInputError

_IMMUTABLE = frozenset(
    {
        "schema_version",
        "predecessor_comment_id",
        "predecessor_record_sha256",
        "creation_run_id",
        "registration_sha256",
        "origin_manifest",
        "origin_count",
    }
)


def lineage_scope(value):
    return hashlib.sha256(
        canonical_bytes(
            {
                "domain": "quarantine-lineage-v1",
                "origin": {key: value[key] for key in _IMMUTABLE},
            }
        )
    ).hexdigest()


def _digest(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


def normalize_lineage(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != _IMMUTABLE | {
        "scope_sha256",
        "assessment_state_manifest",
    }:
        raise ReviewInputError("quarantine lineage is unsupported")
    value = json.loads(canonical_bytes(value))
    if (
        value["schema_version"]
        not in {"quarantine-lineage-v1", "quarantine-published-tail-v1"}
        or any(
            type(value[k]) is not int or not 1 <= value[k] <= 9_007_199_254_740_991
            for k in ("predecessor_comment_id", "creation_run_id")
        )
        or type(value["origin_count"]) is not int
        or not 1 <= value["origin_count"] <= 2
        or (
            value["schema_version"] == "quarantine-published-tail-v1"
            and value["origin_count"] != 2
        )
        or any(
            not _digest(value[k])
            for k in (
                "predecessor_record_sha256",
                "registration_sha256",
                "scope_sha256",
            )
        )
        or len(canonical_bytes(value)) > 2 * MAX_MANIFEST_BYTES + 1024
    ):
        raise ReviewInputError("quarantine lineage values are invalid")
    origin = validate_manifest(value["origin_manifest"])
    origin_binding = cast(dict[str, Any], origin["binding"])
    if origin_binding["purpose"] != "evidence" or origin["item_count"] != 1:
        raise ReviewInputError("quarantine origin carrier domain conflicts")
    if value["scope_sha256"] != lineage_scope(value):
        raise ReviewInputError("quarantine lineage scope conflicts")
    state = value["assessment_state_manifest"]
    if isinstance(state, dict) and state.get("encoding") == INLINE_ENCODING:
        if not isinstance(state.get("binding"), dict):
            raise ReviewInputError("quarantine assessment binding is absent")
        document = read_inline_evidence(state, expected_binding=state["binding"])
        if (
            isinstance(document, dict)
            and document.get("schema_version") == "quarantine-assessment-cursor-v1"
        ):
            from .quarantine_state import validate_cursor

            cursor = validate_cursor(document)
            if (
                cursor["scope_sha256"] != value["scope_sha256"]
                or cursor["count"] != state["item_count"]
            ):
                raise ReviewInputError(
                    "quarantine assessment cursor count or scope conflicts"
                )
        else:
            validate_assessment_state(
                document, scope=value["scope_sha256"], item_count=state["item_count"]
            )
    else:
        state = validate_manifest(state)
    if (
        type(state["item_count"]) is not int
        or not 0 <= state["item_count"] <= 200
        or state["binding"]["purpose"] != "evidence"
        or state["binding"]["policy_digest"] != value["scope_sha256"]
        or any(
            state["binding"][key] != origin_binding[key]
            for key in ("repository", "repository_id", "pull_request", "producer")
        )
    ):
        raise ReviewInputError("quarantine assessment carrier scope conflicts")
    return value


def validate_assessment_state(value, *, scope, item_count):
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "scope_sha256", "settlements"}
        or value["schema_version"] != "quarantine-assessment-state-v1"
        or value["scope_sha256"] != scope
        or not isinstance(value["settlements"], list)
        or len(value["settlements"]) != item_count
        or type(item_count) is not int
        or not 0 <= item_count <= 200
    ):
        raise ReviewInputError("quarantine assessment state conflicts")
    # Individual decision authorization and complete-origin membership are
    # independently enforced by the authenticated reader, not a carrier hash.
    required = {
        "origin_scope_sha256",
        "instance",
        "action",
        "actor",
        "role",
        "head_sha",
        "source_id",
        "source_updated_at",
        "source_body_sha256",
        "reason",
        "policy_sha256",
    }
    keys = []
    for decision in value["settlements"]:
        if not isinstance(decision, dict) or set(decision) != required:
            raise ReviewInputError("quarantine assessment decision is unsupported")
        if (
            any(
                not _digest(decision[k])
                for k in (
                    "origin_scope_sha256",
                    "instance",
                    "source_body_sha256",
                    "policy_sha256",
                )
            )
            or decision["action"] not in {"dismiss", "accept-risk"}
            or decision["role"] not in {"admin", "maintain"}
            or type(decision["source_id"]) is not int
            or not 1 <= decision["source_id"] <= 9_007_199_254_740_991
            or not isinstance(decision["reason"], str)
            or not 16 <= len(decision["reason"].encode()) <= 4096
            or not decision["reason"].isprintable()
        ):
            raise ReviewInputError("quarantine assessment decision values are invalid")
        if (
            not isinstance(decision["head_sha"], str)
            or re.fullmatch(r"[a-f0-9]{40}", decision["head_sha"]) is None
            or not isinstance(decision["source_updated_at"], str)
            or re.fullmatch(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", decision["source_updated_at"]
            )
            is None
        ):
            raise ReviewInputError("quarantine decision head or source is invalid")
        try:
            datetime.fromisoformat(decision["source_updated_at"].replace("Z", "+00:00"))
        except ValueError as exc:
            raise ReviewInputError(
                "quarantine decision source time is invalid"
            ) from exc
        if (
            not isinstance(decision["actor"], str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", decision["actor"]) is None
        ):
            raise ReviewInputError("quarantine decision actor is invalid")
        keys.append((decision["origin_scope_sha256"], decision["instance"]))
    if len(keys) != len(set(keys)):
        raise ReviewInputError("quarantine assessment decision is duplicated")
    return value


def require_lineage_successor(before, after):
    before, after = normalize_lineage(before), normalize_lineage(after)
    if before is None:
        if after is not None:
            raise ReviewInputError("quarantine requires a separately created ledger")
        return
    if after is None or any(
        before[k] != after[k] for k in _IMMUTABLE | {"scope_sha256"}
    ):
        raise ReviewInputError("quarantine origin cannot be removed or rewritten")
    before_state, after_state = (
        before["assessment_state_manifest"],
        after["assessment_state_manifest"],
    )
    if (
        before_state != after_state
        and after_state["item_count"] != before_state["item_count"] + 1
    ):
        raise ReviewInputError(
            "quarantine assessments require one append-only individual decision"
        )
    if before_state.get("encoding") == after_state.get("encoding") == INLINE_ENCODING:
        a, b = before_state["document"], after_state["document"]
        if (
            "settlements" in a
            and "settlements" in b
            and b["settlements"][: len(a["settlements"])] != a["settlements"]
        ):
            raise ReviewInputError(
                "quarantine assessment decisions cannot be rewritten"
            )
    # The hosted mutation boundary additionally reads both complete assessment
    # documents and requires an authorized append-only state successor.


def full_analysis_reservation(scope, ordinary_reservation):
    """Durable semantic binding for a full pass; this hash grants no authority."""
    if not _digest(scope) or not _digest(ordinary_reservation):
        raise ReviewInputError("quarantine full-analysis reservation scope is invalid")
    return hashlib.sha256(
        canonical_bytes(
            {
                "domain": "quarantine-full-analysis-v1",
                "scope_sha256": scope,
                "ordinary_reservation": ordinary_reservation,
            }
        )
    ).hexdigest()


def is_full_analysis_transaction(record, current_key):
    from .session import session_reservation_id

    lineage, transaction = record.quarantine_lineage, record.transaction
    if lineage is None or transaction is None or current_key is None:
        return False
    ordinary = session_reservation_id(
        repository=record.repository,
        pull_request=record.pull_request,
        head_sha=transaction.head_sha,
        kind="publish:"
        + current_key.digest()
        + ":"
        + transaction.policy_digest
        + ":"
        + transaction.configuration_digest
        + ":"
        + transaction.evidence_digest,
    )
    return transaction.reservation_id == full_analysis_reservation(
        lineage["scope_sha256"], ordinary
    )
