"""Bounded append-only transport for complete origin-scoped assessment state.

Each decision adds one immutable event instead of restaging every earlier
reason. References are evidence, not decision authority. The hosted mutation
must authorize the exact new decision and compare both complete states.
"""

from __future__ import annotations

import re
from typing import Any, cast

from .bounded_evidence import (
    MAX_MANIFEST_BYTES,
    canonical_bytes,
    read_partitioned_evidence,
    validate_binding,
    validate_manifest,
)
from .errors import ReviewInputError
from .quarantine_lineage import validate_assessment_state
from .schemas import validate_public_document

MAX_DECISIONS = 200


def assessment_cursor(*, scope, count=0, tail_manifest=None):
    return validate_cursor(
        {
            "schema_version": "quarantine-assessment-cursor-v1",
            "scope_sha256": scope,
            "count": count,
            "tail_manifest": tail_manifest,
        }
    )


def validate_cursor(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "scope_sha256", "count", "tail_manifest"}
        or value["schema_version"] != "quarantine-assessment-cursor-v1"
        or not isinstance(value["scope_sha256"], str)
        or re.fullmatch(r"[a-f0-9]{64}", value["scope_sha256"]) is None
        or type(value["count"]) is not int
        or not 0 <= value["count"] <= MAX_DECISIONS
        or len(canonical_bytes(value)) > MAX_MANIFEST_BYTES + 256
    ):
        raise ReviewInputError("quarantine assessment cursor is unsupported")
    validate_public_document(value, "quarantine-assessment-cursor")
    if value["count"] == 0:
        if value["tail_manifest"] is not None:
            raise ReviewInputError("empty quarantine assessment cursor has a tail")
    else:
        manifest = cast(dict[str, Any], validate_manifest(value["tail_manifest"]))
        if (
            manifest["item_count"] != 1
            or manifest["binding"]["purpose"] != "evidence"
            or manifest["binding"]["policy_digest"] != value["scope_sha256"]
        ):
            raise ReviewInputError("quarantine assessment cursor tail scope conflicts")
    return {**value}


def next_assessment_event(cursor, decision):
    cursor = validate_cursor(cursor)
    if cursor["count"] == MAX_DECISIONS:
        raise ReviewInputError("quarantine assessment decision bound is exhausted")
    validate_assessment_state(
        {
            "schema_version": "quarantine-assessment-state-v1",
            "scope_sha256": cursor["scope_sha256"],
            "settlements": [decision],
        },
        scope=cursor["scope_sha256"],
        item_count=1,
    )
    return {
        "schema_version": "quarantine-assessment-event-v1",
        "scope_sha256": cursor["scope_sha256"],
        "sequence": cursor["count"] + 1,
        "previous_manifest": cursor["tail_manifest"],
        "decision": decision,
    }


def read_assessment_state(value, *, expected_binding, reader, budget):
    binding = cast(dict[str, Any], validate_binding(dict(expected_binding)))
    if binding["purpose"] != "evidence":
        raise ReviewInputError("quarantine assessment reader domain conflicts")
    scope = binding["policy_digest"]
    if (
        isinstance(value, dict)
        and value.get("schema_version") == "quarantine-assessment-state-v1"
    ):
        decisions = value.get("settlements")
        if not isinstance(decisions, list):
            raise ReviewInputError("quarantine assessment state is unsupported")
        return validate_assessment_state(value, scope=scope, item_count=len(decisions))
    cursor = validate_cursor(value)
    if cursor["scope_sha256"] != scope:
        raise ReviewInputError("quarantine assessment cursor aggregate scope conflicts")
    tail = cursor["tail_manifest"]
    decisions = []
    seen = set()
    generation = binding["generation"] + 1
    for sequence in range(cursor["count"], 0, -1):
        manifest = cast(dict[str, Any], validate_manifest(tail))
        source = manifest["binding"]
        if (
            manifest["item_count"] != 1
            or source["purpose"] != "evidence"
            or any(
                source[key] != binding[key]
                for key in (
                    "repository",
                    "repository_id",
                    "pull_request",
                    "producer",
                    "policy_digest",
                )
            )
            or source["generation"] >= generation
            or manifest["sha256"] in seen
        ):
            raise ReviewInputError(
                "quarantine assessment event source or sequence conflicts"
            )
        seen.add(manifest["sha256"])
        generation = source["generation"]
        event = read_partitioned_evidence(
            manifest, reader=reader, expected_binding=source, budget=budget
        )
        if (
            not isinstance(event, dict)
            or set(event)
            != {
                "schema_version",
                "scope_sha256",
                "sequence",
                "previous_manifest",
                "decision",
            }
            or event["schema_version"] != "quarantine-assessment-event-v1"
            or event["scope_sha256"] != scope
            or type(event["sequence"]) is not int
            or event["sequence"] != sequence
            or len(canonical_bytes(event)) > MAX_MANIFEST_BYTES + 8192
        ):
            raise ReviewInputError(
                "quarantine assessment event is unsupported or incomplete"
            )
        validate_public_document(event, "quarantine-assessment-event")
        validate_assessment_state(
            {
                "schema_version": "quarantine-assessment-state-v1",
                "scope_sha256": scope,
                "settlements": [event["decision"]],
            },
            scope=scope,
            item_count=1,
        )
        decisions.append(event["decision"])
        tail = event["previous_manifest"]
    if tail is not None:
        raise ReviewInputError(
            "quarantine assessment chain has omitted earlier decisions"
        )
    return validate_assessment_state(
        {
            "schema_version": "quarantine-assessment-state-v1",
            "scope_sha256": scope,
            "settlements": list(reversed(decisions)),
        },
        scope=scope,
        item_count=cursor["count"],
    )
