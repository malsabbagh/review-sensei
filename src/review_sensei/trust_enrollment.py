"""Immutable original trust observations; retained data never grants authority."""

from __future__ import annotations

import hashlib
import json

from .bounded_evidence import MAX_MANIFEST_BYTES, canonical_bytes
from .errors import ReviewInputError

VERSION = "trust-enrollment-reference-v1"
DOCUMENT_VERSION = "original-trust-enrollment-v1"


def normalize_reference(value):
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "document", "sha256"}
        or value["schema_version"] != VERSION
        or len(canonical_bytes(value)) > MAX_MANIFEST_BYTES
    ):
        raise ReviewInputError("trust enrollment reference is unsupported")
    document = value["document"]
    if value["sha256"] != hashlib.sha256(canonical_bytes(document)).hexdigest():
        raise ReviewInputError("trust enrollment document integrity failed")
    if (
        not isinstance(document, dict)
        or set(document) != {"schema_version", "app_id", "authority", "generation"}
        or document["schema_version"] != DOCUMENT_VERSION
        or type(document["app_id"]) is not int
        or not 0 < document["app_id"] <= (1 << 53) - 1
        or not isinstance(document["authority"], dict)
        or document["authority"].get("version") != 4
        or type(document["generation"]) is not int
        or not 0 <= document["generation"] <= 2147483647
    ):
        raise ReviewInputError("trust enrollment document is unsupported")
    authority = document["authority"]
    keys = {
        "version",
        "purpose",
        "repository",
        "repository_id",
        "pull_request",
        "head_sha",
        "run_id",
        "run_attempt",
        "job",
        "job_workflow_ref",
        "job_workflow_sha",
        "issued_at",
        "expires_at",
        "audience",
        "source",
        "actor",
        "current_observation",
    }
    if set(authority) != keys:
        raise ReviewInputError("trust enrollment authority fields are unsupported")
    observation = authority["current_observation"]
    if not isinstance(observation, dict) or set(observation) != {
        "schema_version",
        "repository",
        "installation",
        "capability",
        "actor",
    }:
        raise ReviewInputError("trust enrollment observation fields are unsupported")
    if observation["schema_version"] not in (
        "current-observation-v1",
        "current-observation-v2",
    ):
        raise ReviewInputError("trust enrollment observation version is unsupported")
    shapes = {
        "repository": {"id", "full_name", "owner_id", "owner_login", "fork"},
        "installation": {
            "id",
            "repository_selection",
            "repository_access",
            "permissions",
        },
        "capability": {"name", "permissions"},
        "actor": {"id", "login", "source_association"}
        | (
            {"repository_permission", "repository_role"}
            if observation["schema_version"] == "current-observation-v2"
            else set()
        ),
    }
    if any(
        not isinstance(observation[k], dict) or set(observation[k]) != fields
        for k, fields in shapes.items()
    ):
        raise ReviewInputError("trust enrollment observation fields are unsupported")
    if (
        not isinstance(authority["source"], dict)
        or set(authority["source"]) != {"kind", "id", "updated_at", "body_sha256"}
        or (
            authority["actor"] is not None
            and (
                not isinstance(authority["actor"], dict)
                or set(authority["actor"]) != {"login", "type", "association"}
            )
        )
    ):
        raise ReviewInputError("trust enrollment source fields are unsupported")

    return json.loads(canonical_bytes(value))


def require_successor(before, after):
    old, new = normalize_reference(before), normalize_reference(after)
    if old is not None and new != old:
        raise ReviewInputError(
            "original trust enrollment cannot be removed or replaced"
        )
