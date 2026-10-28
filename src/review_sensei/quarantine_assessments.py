"""Complete independent quarantine origins and monotonic individual decisions.

These are integrity/value checks. The hosted reader must authenticate every
source and carrier before passing documents here. No dictionary grants authority
or establishes original admission from a raw result's human-review flags.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, cast

from .bounded_evidence import (
    INLINE_ENCODING,
    canonical_bytes,
    read_inline_evidence,
    validate_manifest,
)
from .errors import ReviewInputError
from .quarantine_lineage import validate_assessment_state
from .schemas import validate_public_document

MAX_ORIGINS = 2
MAX_ORIGIN_ASSESSMENTS = 100
MAX_ORIGIN_INVENTORY_BYTES = 24_000
_ORIGIN_FIELDS = frozenset(
    {"schema_version", "kind", "source_manifest", "inventory_manifest"}
)


def _digest(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def origin_scope(value):
    """Bind one immutable source and inventory, independent of other origins."""
    return hashlib.sha256(
        canonical_bytes(
            {
                "domain": "quarantine-assessment-origin-v1",
                "origin": {key: value[key] for key in _ORIGIN_FIELDS},
            }
        )
    ).hexdigest()


def inventory_manifest_bytes(manifest):
    if isinstance(manifest, dict) and manifest.get("encoding") == INLINE_ENCODING:
        value = read_inline_evidence(manifest, expected_binding=manifest["binding"])
        validate_inventory(value)
        return len(canonical_bytes(value))
    return validate_manifest(manifest)["decoded_bytes"]


def validate_origins(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "origins"}
        or value["schema_version"] != "quarantine-assessment-origins-v1"
        or not isinstance(value["origins"], list)
        or not 1 <= len(value["origins"]) <= MAX_ORIGINS
    ):
        raise ReviewInputError("quarantine assessment origins are unsupported")
    validate_public_document(value, "quarantine-assessment-origins")
    value = json.loads(canonical_bytes(value))
    scopes, kinds, common = [], [], None
    for origin in value["origins"]:
        if (
            not isinstance(origin, dict)
            or set(origin) != _ORIGIN_FIELDS | {"scope_sha256"}
            or origin["schema_version"] != "quarantine-assessment-origin-v1"
            or origin["kind"] not in {"retained-result", "published-assessment"}
            or not _digest(origin["scope_sha256"])
        ):
            raise ReviewInputError("quarantine assessment origin is unsupported")
        source = cast(dict[str, Any], validate_manifest(origin["source_manifest"]))
        inventory = origin["inventory_manifest"]
        inventory_bytes = inventory_manifest_bytes(inventory)
        if (
            source["item_count"] != 1
            or not 0 <= inventory["item_count"] <= MAX_ORIGIN_ASSESSMENTS
            or inventory_bytes > MAX_ORIGIN_INVENTORY_BYTES
            or source["binding"]["purpose"] != "evidence"
            or inventory["binding"] != source["binding"]
            or origin["scope_sha256"] != origin_scope(origin)
        ):
            raise ReviewInputError("quarantine assessment origin scope conflicts")
        identity = tuple(
            source["binding"][key]
            for key in ("repository", "repository_id", "pull_request", "producer")
        )
        if common is not None and identity != common:
            raise ReviewInputError("quarantine assessment origins have foreign owners")
        common = identity
        scopes.append(origin["scope_sha256"])
        kinds.append(origin["kind"])
    if (
        scopes != sorted(set(scopes))
        or len(kinds) != len(set(kinds))
        or "retained-result" not in kinds
    ):
        raise ReviewInputError(
            "quarantine assessment origins are incomplete or duplicated"
        )
    return value


def validate_inventory(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "assessments"}
        or value["schema_version"] != "quarantine-assessment-inventory-v1"
        or not isinstance(value["assessments"], list)
        or len(value["assessments"]) > MAX_ORIGIN_ASSESSMENTS
        or len(canonical_bytes(value)) > MAX_ORIGIN_INVENTORY_BYTES
    ):
        raise ReviewInputError("quarantine assessment inventory is unsupported")
    validate_public_document(value, "quarantine-assessment-inventory")
    value = json.loads(canonical_bytes(value))
    instances = []
    for assessment in value["assessments"]:
        if (
            not isinstance(assessment, dict)
            or set(assessment) != {"instance", "concern"}
            or not all(_digest(assessment[key]) for key in ("instance", "concern"))
        ):
            raise ReviewInputError("quarantine assessment inventory item is invalid")
        instances.append(assessment["instance"])
    if instances != sorted(set(instances)):
        raise ReviewInputError(
            "quarantine assessment inventory is duplicated or unordered"
        )
    return value


def pending_assessments(origins, inventories, state, *, scope):
    """Return every unresolved (origin scope, full instance) after complete IO.

    Callers supply one authenticated inventory per immutable origin. Fresh
    findings are deliberately absent from this operation: their omission cannot
    discharge historical assessments, and a fresh recurrence is independent.
    """
    origins = validate_origins(origins)
    if not _digest(scope) or not isinstance(inventories, dict):
        raise ReviewInputError("quarantine assessment aggregate scope is invalid")
    if set(inventories) != {o["scope_sha256"] for o in origins["origins"]}:
        raise ReviewInputError("quarantine assessment inventories are incomplete")
    members: list[tuple[str, str]] = []
    for origin in origins["origins"]:
        inventory = validate_inventory(inventories[origin["scope_sha256"]])
        raw = canonical_bytes(inventory)
        manifest = origin["inventory_manifest"]
        if (
            hashlib.sha256(raw).hexdigest() != manifest["sha256"]
            or len(raw) != inventory_manifest_bytes(manifest)
            or len(inventory["assessments"]) != manifest["item_count"]
        ):
            raise ReviewInputError(
                "quarantine assessment inventory differs from origin"
            )
        members.extend(
            (origin["scope_sha256"], item["instance"])
            for item in inventory["assessments"]
        )
    if not isinstance(state, dict) or not isinstance(state.get("settlements"), list):
        raise ReviewInputError("quarantine assessment state is invalid")
    validate_assessment_state(state, scope=scope, item_count=len(state["settlements"]))
    settled = {(d["origin_scope_sha256"], d["instance"]) for d in state["settlements"]}
    if not settled.issubset(members):
        raise ReviewInputError(
            "quarantine assessment decision targets a foreign origin or item"
        )
    return tuple(member for member in members if member not in settled)


def require_assessment_successor(origins, inventories, before, after, *, scope):
    """Require full authenticated documents and retain every earlier decision."""
    pending_assessments(origins, inventories, before, scope=scope)
    pending_assessments(origins, inventories, after, scope=scope)
    if after["settlements"][: len(before["settlements"])] != before["settlements"]:
        raise ReviewInputError(
            "quarantine assessment decisions cannot be removed or rewritten"
        )


def assessment_view(origins, inventories, state, *, scope, fresh_human_pending):
    """Publication facts from the complete authenticated inventories and state."""
    pending = pending_assessments(origins, inventories, state, scope=scope)
    return validate_view(
        {
            "scope_sha256": scope,
            "state_sha256": hashlib.sha256(canonical_bytes(state)).hexdigest(),
            "origins": [
                {
                    "scope_sha256": origin["scope_sha256"],
                    "kind": origin["kind"],
                    "inventory_sha256": origin["inventory_manifest"]["sha256"],
                    "item_count": origin["inventory_manifest"]["item_count"],
                }
                for origin in origins["origins"]
            ],
            "pending": [
                {"origin_scope_sha256": origin, "instance": instance}
                for origin, instance in pending
            ],
            "fresh_human_pending": fresh_human_pending,
        }
    )


def validate_view(value):
    """Validate a bounded publication view; the live reader verifies its source."""
    if (
        not isinstance(value, dict)
        or set(value)
        != {"scope_sha256", "state_sha256", "origins", "pending", "fresh_human_pending"}
        or not _digest(value["scope_sha256"])
        or not _digest(value["state_sha256"])
        or type(value["fresh_human_pending"]) is not bool
        or not isinstance(value["origins"], list)
        or not 1 <= len(value["origins"]) <= MAX_ORIGINS
        or not isinstance(value["pending"], list)
        or len(value["pending"]) > MAX_ORIGINS * MAX_ORIGIN_ASSESSMENTS
    ):
        raise ReviewInputError("quarantine assessment publication view is unsupported")
    validate_public_document(value, "quarantine-assessment-view")
    origins, kinds = {}, []
    for origin in value["origins"]:
        if (
            not isinstance(origin, dict)
            or set(origin) != {"scope_sha256", "kind", "inventory_sha256", "item_count"}
            or not _digest(origin["scope_sha256"])
            or not _digest(origin["inventory_sha256"])
            or origin["kind"] not in {"retained-result", "published-assessment"}
            or type(origin["item_count"]) is not int
            or not 0 <= origin["item_count"] <= MAX_ORIGIN_ASSESSMENTS
        ):
            raise ReviewInputError("quarantine publication origin is unsupported")
        origins[origin["scope_sha256"]] = origin["item_count"]
        kinds.append(origin["kind"])
    if (
        list(origins) != sorted(origins)
        or len(origins) != len(value["origins"])
        or len(kinds) != len(set(kinds))
        or "retained-result" not in kinds
    ):
        raise ReviewInputError("quarantine publication origins are incomplete")
    members, counts = [], dict.fromkeys(origins, 0)
    for item in value["pending"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"origin_scope_sha256", "instance"}
            or item["origin_scope_sha256"] not in origins
            or not _digest(item["instance"])
        ):
            raise ReviewInputError("quarantine publication pending target is invalid")
        members.append((item["origin_scope_sha256"], item["instance"]))
        counts[item["origin_scope_sha256"]] += 1
    if members != sorted(set(members)) or any(
        counts[scope] > origins[scope] for scope in origins
    ):
        raise ReviewInputError("quarantine publication pending inventory conflicts")
    return json.loads(canonical_bytes(value))
