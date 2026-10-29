"""Closed, bounded decision snapshot documents; references are not authority."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .bounded_evidence import (
    INLINE_ENCODING,
    MAX_MANIFEST_BYTES,
    canonical_bytes,
    evidence_manifest,
    inline_manifest,
    partition_evidence,
    read_inline_evidence,
    validate_manifest,
)
from .errors import ReviewInputError

MAX_DISPOSITION_SNAPSHOT_ROOT_BYTES = 8192
DISPOSITION_PAGE_ITEMS = 8
MAX_DISPOSITION_PAGES = 512


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
        or normalized["schema_version"] not in {"1.0", "2.0"}
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
        if normalized["schema_version"] == "1.0":
            snapshot_document(
                document,
                result_digest=normalized["result_digest"],
                item_count=manifest["item_count"],
            )
        else:
            validate_page_index(document, root=normalized)
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


def validate_page_index(
    value: object, *, root: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Validate a closed finite index; only the authenticated root selects pages."""
    count = root["state_manifest"]["item_count"]
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "result_digest", "pages", "tail"}
        or value["schema_version"] != "disposition-pages-v1"
        or value["result_digest"] != root["result_digest"]
        or not isinstance(value["pages"], list)
        or len(value["pages"]) != count // DISPOSITION_PAGE_ITEMS
        or len(value["pages"]) > MAX_DISPOSITION_PAGES
        or not isinstance(value["tail"], list)
        or len(value["tail"]) != count % DISPOSITION_PAGE_ITEMS
    ):
        raise ReviewInputError("disposition page index does not match root")
    expected = root["state_manifest"]["binding"]
    for index, page in enumerate(value["pages"]):
        if not isinstance(page, dict):
            raise ReviewInputError("disposition page reference is invalid")
        if page.get("encoding") == INLINE_ENCODING:
            read_inline_evidence(page, expected_binding=page.get("binding", {}))
        else:
            validate_manifest(page)
        binding = page["binding"]
        if (
            any(
                binding[key] != expected[key] for key in expected if key != "generation"
            )
            or binding["generation"] > expected["generation"]
            or page["item_count"]
            != min(DISPOSITION_PAGE_ITEMS, count - index * DISPOSITION_PAGE_ITEMS)
        ):
            raise ReviewInputError("disposition page scope/order/count conflicts")
    return value["pages"]


def read_paged_snapshot(
    document: object,
    *,
    root: Mapping[str, Any],
    reader: Callable[..., object],
    identity: object,
) -> tuple[dict[str, object], ...]:
    """Read all pages and validate the complete inventory, including duplicates."""
    from .session import _stored_dispositions

    values: list[dict[str, object]] = []
    for index, page in enumerate(validate_page_index(document, root=root)):
        decoded = reader(identity, page, expected_binding=page["binding"])
        if (
            not isinstance(decoded, dict)
            or set(decoded)
            != {"schema_version", "result_digest", "offset", "dispositions"}
            or decoded["schema_version"] != "disposition-page-v1"
            or decoded["result_digest"] != root["result_digest"]
            or type(decoded["offset"]) is not int
            or decoded["offset"] != index * DISPOSITION_PAGE_ITEMS
        ):
            raise ReviewInputError("disposition page contents conflict")
        validated = snapshot_document(
            {
                "schema_version": "1.0",
                "result_digest": decoded["result_digest"],
                "dispositions": decoded["dispositions"],
            },
            result_digest=root["result_digest"],
            item_count=page["item_count"],
        )
        values.extend(validated)
    assert isinstance(document, dict)
    values.extend(document["tail"])
    return _stored_dispositions(values, snapshot=True)


def stage_paged_snapshot(
    ledger: Any,
    record: Any,
    values: tuple[Mapping[str, object], ...],
    *,
    binding: dict[str, object],
    identity: object,
    max_manifest_bytes: int,
) -> dict[str, object]:
    """Reuse unchanged immutable pages; edits retain every old page and root.

    A previous authenticated index is only an optimization. Its full contents
    are read and compared before reusing any page; generation, producer, scope,
    result and position are independently checked. New snapshots never refer
    to earlier snapshots, so reads do not grow with the number of commands.
    """
    from .session import _stored_dispositions

    values = _stored_dispositions(list(values), snapshot=True)
    stage_batch = getattr(ledger, "stage_disposition_snapshot_batch", None)
    if not callable(stage_batch):
        raise ReviewInputError(
            "paged snapshot writer requires complete batch preflight"
        )
    if len(values) // DISPOSITION_PAGE_ITEMS > MAX_DISPOSITION_PAGES:
        raise ReviewInputError("disposition page index exceeds finite bounds")
    old_pages: list[dict[str, Any]] = []
    old_values: tuple[dict[str, object], ...] = ()
    prior = record.disposition_snapshot
    result_digest = record.transaction.result_sha256
    if (
        prior is not None
        and prior["schema_version"] == "2.0"
        and prior["result_digest"] == result_digest
    ):
        prior_binding = prior["state_manifest"]["binding"]
        if all(
            prior_binding[key] == binding[key] for key in binding if key != "generation"
        ):
            document = ledger.read_evidence(
                identity, prior["state_manifest"], expected_binding=prior_binding
            )
            old_values = read_paged_snapshot(
                document, root=prior, reader=ledger.read_evidence, identity=identity
            )
            old_pages = validate_page_index(document, root=prior)
    pages = []
    for index, offset in enumerate(
        range(
            0,
            len(values) - len(values) % DISPOSITION_PAGE_ITEMS,
            DISPOSITION_PAGE_ITEMS,
        )
    ):
        chunk = values[offset : offset + DISPOSITION_PAGE_ITEMS]
        if (
            index < len(old_pages)
            and chunk == old_values[offset : offset + DISPOSITION_PAGE_ITEMS]
        ):
            pages.append(old_pages[index])
            continue
        page = {
            "schema_version": "disposition-page-v1",
            "result_digest": result_digest,
            "offset": offset,
            "dispositions": list(chunk),
        }
        pages.append(page)
    return stage_batch(
        identity,
        record=record,
        binding=binding,
        pages=pages,
        values=values,
        result_digest=result_digest,
        max_manifest_bytes=min(MAX_MANIFEST_BYTES, max_manifest_bytes),
    )


@dataclass(frozen=True)
class PreparedLocalDispositionSnapshot:
    """Exact content-addressed write set and complete authenticated read set.

    Only local storage IDs can be derived from these bytes. GitHub's assigned
    comment identities require a different conservative adapter preflight; this
    preparation is not a network-storage certificate.
    """

    manifest: dict[str, object]
    parts: dict[str, dict[str, object]]
    read_manifests: tuple[dict[str, Any], ...]


def prepare_local_paged_snapshot(
    *,
    binding: dict[str, object],
    pages: Sequence[Mapping[str, Any]],
    values: tuple[Mapping[str, object], ...],
    result_digest: str,
    max_manifest_bytes: int,
) -> PreparedLocalDispositionSnapshot:
    """Prepare pages, their complete index and the future root before writes."""
    parts: dict[str, dict[str, object]] = {}

    def prepare(document: object, count: int, allowance: int) -> dict[str, Any]:
        inline = inline_manifest(document, binding=binding, item_count=count)
        if inline is not None and len(canonical_bytes(inline)) <= allowance:
            return inline
        chunks = partition_evidence(document, binding=binding, item_count=count)
        ids = tuple(
            hashlib.sha256(canonical_bytes(part)).hexdigest() for part in chunks
        )
        manifest = evidence_manifest(
            document, binding=binding, item_count=count, parts=chunks, storage_ids=ids
        )
        if len(canonical_bytes(manifest)) > allowance:
            raise ReviewInputError(
                "partition manifest exceeds actual lifecycle allocation"
            )
        parts.update(zip(ids, chunks))
        return manifest

    references: list[dict[str, Any]] = []
    for index, page in enumerate(pages):
        if page.get("schema_version") == "disposition-page-v1":
            offset = index * DISPOSITION_PAGE_ITEMS
            if (
                set(page)
                != {"schema_version", "result_digest", "offset", "dispositions"}
                or type(page["offset"]) is not int
                or page["offset"] != offset
                or page["result_digest"] != result_digest
                or page["dispositions"]
                != list(values[offset : offset + DISPOSITION_PAGE_ITEMS])
            ):
                raise ReviewInputError(
                    "prepared disposition page differs from inventory"
                )
            references.append(prepare(page, DISPOSITION_PAGE_ITEMS, 1024))
        else:
            references.append(dict(page))
    document = {
        "schema_version": "disposition-pages-v1",
        "result_digest": result_digest,
        "pages": references,
        "tail": list(values[len(pages) * DISPOSITION_PAGE_ITEMS :]),
    }
    manifest = prepare(document, len(values), max_manifest_bytes)
    root = {
        "schema_version": "2.0",
        "result_digest": result_digest,
        "state_manifest": manifest,
    }
    normalize_snapshot(root)
    validate_page_index(document, root=root)
    return PreparedLocalDispositionSnapshot(manifest, parts, (*references, manifest))
