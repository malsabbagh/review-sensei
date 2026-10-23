"""Closed, bounded decision snapshot documents; references are not authority."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from .bounded_evidence import (
    INLINE_ENCODING,
    canonical_bytes,
    read_inline_evidence,
    validate_manifest,
)
from .errors import ReviewInputError

MAX_DISPOSITION_SNAPSHOT_ROOT_BYTES = 8192


def snapshot_document(
    value: object, *, result_digest: str, item_count: int
) -> tuple[dict[str, object], ...]:
    """Validate the entire decoded inventory without the embedded-array cap."""
    from .session import _stored_dispositions

    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "result_digest", "dispositions"}
        or value["schema_version"] != "1.0"
        or value["result_digest"] != result_digest
        or not isinstance(value["dispositions"], list)
        or len(value["dispositions"]) != item_count
    ):
        raise ReviewInputError("disposition snapshot inventory does not match root")
    return _stored_dispositions(value["dispositions"], snapshot=True)


def normalize_snapshot(value: object) -> dict[str, Any] | None:
    """Check a root's closed shape and codec; no external read or owner claim."""
    if value is None:
        return None
    normalized = json.loads(canonical_bytes(value))
    if (
        not isinstance(normalized, dict)
        or set(normalized) != {"schema_version", "result_digest", "state_manifest"}
        or normalized["schema_version"] != "1.0"
        or not isinstance(normalized["result_digest"], str)
        or re.fullmatch(r"[a-f0-9]{64}", normalized["result_digest"]) is None
        or len(canonical_bytes(normalized)) > MAX_DISPOSITION_SNAPSHOT_ROOT_BYTES
    ):
        raise ReviewInputError("disposition snapshot root is invalid")
    manifest = normalized["state_manifest"]
    if isinstance(manifest, dict) and manifest.get("encoding") == INLINE_ENCODING:
        if "binding" not in manifest:
            raise ReviewInputError("disposition snapshot binding is missing")
        document = read_inline_evidence(manifest, expected_binding=manifest["binding"])
        snapshot_document(
            document,
            result_digest=normalized["result_digest"],
            item_count=manifest["item_count"],
        )
    else:
        manifest = validate_manifest(manifest)
    if manifest["binding"]["purpose"] != "dispositions":
        raise ReviewInputError("disposition snapshot has a foreign domain")
    return normalized


def validate_snapshot_identity(
    root: Mapping[str, Any],
    *,
    repository: str,
    repository_id: int | None,
    pull_request: int,
    generation: int,
) -> None:
    binding = root["state_manifest"]["binding"]
    if (
        binding["repository"] != repository
        or binding["repository_id"] != repository_id
        or binding["pull_request"] != pull_request
        or binding["generation"] > generation
    ):
        raise ReviewInputError(
            "disposition snapshot does not match session identity/generation"
        )
