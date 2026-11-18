"""Closed, bounded decision snapshot documents; references are not authority."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import struct
import zlib
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .bounded_evidence import (
    INLINE_ENCODING,
    MAX_MANIFEST_BYTES,
    MAX_PARTITION_DECODED_BYTES,
    MAX_PARTS,
    PART_FRAMING_RESERVE,
    PARTITION_ENCODING,
    canonical_bytes,
    evidence_manifest,
    inline_manifest,
    partition_evidence,
    read_inline_evidence,
    validate_binding,
    validate_manifest,
)
from .errors import ReviewInputError

MAX_DISPOSITION_SNAPSHOT_ROOT_BYTES = 8192
DISPOSITION_PAGE_ITEMS = 8
MAX_DISPOSITION_PAGES = 512
PACKED_PAGE_INDEX_VERSION = "disposition-pages-packed-v1"


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


_PACKED_TAIL_REQUIRED = frozenset(
    {"fingerprint", "action", "reason", "actor", "head_sha", "expires_at"}
)
_PACKED_TAIL_OPTIONAL = frozenset(
    {
        "actor_role",
        "finding_reference",
        "recorded_at",
        "policy_digest",
        "provenance",
        "resolution_kind",
        "target_kind",
        "incomplete_coverage",
    }
)
_PACKED_TAIL_ALLOWED = _PACKED_TAIL_REQUIRED | _PACKED_TAIL_OPTIONAL
_PACKED_PAGE_ZDICT = (
    b'[["dismiss","accept-risk","defer","accept-coverage-uncertainty",'
    b'"admin","maintain","write","RS-",null,0,'
    b'"github-issue-comment:","github-review-comment:","T00:00:00Z","2026-"]]'
)


def pack_page_index(
    document: Mapping[str, Any], *, binding: Mapping[str, object]
) -> dict[str, Any] | None:
    """Compact one validated page index while leaving part IDs uncompressed."""
    if (
        not isinstance(document, Mapping)
        or document.get("schema_version") != "disposition-pages-v1"
        or not isinstance(document.get("result_digest"), str)
        or re.fullmatch(r"[a-f0-9]{64}", document["result_digest"]) is None
        or not isinstance(document.get("pages"), list)
        or len(document["pages"]) > MAX_DISPOSITION_PAGES
        or not isinstance(document.get("tail"), list)
        or len(document["tail"]) > 4096
    ):
        return None
    ids: list[list[str]] = []
    # Keep high-entropy SHA-256 digests uncompressed in binary and deflate only
    # structured metadata so small indexes stay compact under Base85 framing.
    hashes = bytearray()
    meta_buf = bytearray()
    meta_buf += struct.pack(">HH", len(document["pages"]), len(document["tail"]))
    for page in document["pages"]:
        if (
            not isinstance(page, Mapping)
            or page.get("encoding") != PARTITION_ENCODING
            or not isinstance(page.get("binding"), Mapping)
            or any(
                page["binding"].get(key) != binding.get(key)
                for key in binding
                if key != "generation"
            )
            or page.get("item_count") != DISPOSITION_PAGE_ITEMS
            or type(page["binding"].get("generation")) is not int
            or not 0 <= page["binding"]["generation"] <= 0xFFFFFFFF
            or type(page.get("decoded_bytes")) is not int
            or not 0 <= page["decoded_bytes"] <= 0xFFFFFFFF
            or type(page.get("encoded_bytes")) is not int
            or not 0 <= page["encoded_bytes"] <= 0xFFFFFFFF
            or not isinstance(page.get("sha256"), str)
            or re.fullmatch(r"[a-f0-9]{64}", page["sha256"]) is None
            or not isinstance(page.get("parts"), list)
            or not 1 <= len(page["parts"]) <= MAX_PARTS
        ):
            return None
        page_ids: list[str] = []
        meta_buf += struct.pack(
            ">IIH",
            page["binding"]["generation"],
            page["decoded_bytes"],
            len(page["parts"]),
        )
        hashes += bytes.fromhex(page["sha256"])
        expected_encoded = 0
        for part in page["parts"]:
            if (
                not isinstance(part, Mapping)
                or not isinstance(part.get("storage_id"), str)
                or type(part.get("bytes")) is not int
                or not 0 <= part["bytes"] <= 0xFFFFFFFF
                or not isinstance(part.get("sha256"), str)
                or re.fullmatch(r"[a-f0-9]{64}", part["sha256"]) is None
            ):
                return None
            page_ids.append(part["storage_id"])
            expected_encoded += part["bytes"] + PART_FRAMING_RESERVE
            meta_buf += struct.pack(">I", part["bytes"])
            hashes += bytes.fromhex(part["sha256"])
        if page["encoded_bytes"] != expected_encoded:
            return None
        ids.append(page_ids)
    tail_meta: list[list[Any]] = []
    for item in document["tail"]:
        if (
            not isinstance(item, Mapping)
            or not _PACKED_TAIL_REQUIRED <= set(item) <= _PACKED_TAIL_ALLOWED
            or not isinstance(item["fingerprint"], str)
            or re.fullmatch(r"[a-f0-9]{64}", item["fingerprint"]) is None
            or any(item[key] is None for key in set(item) - _PACKED_TAIL_REQUIRED)
        ):
            return None
        fp = item["fingerprint"]
        hashes += bytes.fromhex(fp)
        fref = item.get("finding_reference")
        if (
            isinstance(fref, str)
            and fref.startswith("RS-")
            and 1 <= len(fref) - 3 <= len(fp)
            and fref == "RS-" + fp[: len(fref) - 3].upper()
        ):
            fref_meta: object = len(fref) - 3
        elif (
            isinstance(fref, str)
            and fref.startswith("RS-")
            and 1 <= len(fref) - 3 <= len(fp)
            and fref == "RS-" + fp[: len(fref) - 3].lower()
        ):
            fref_meta = -(len(fref) - 3)
        else:
            if type(fref) is int:
                return None
            fref_meta = fref
        hsha = item["head_sha"]
        if isinstance(hsha, str) and hsha == binding.get("head_sha"):
            hsha_meta: object = 0
        else:
            if type(hsha) is int:
                return None
            hsha_meta = hsha
        pdig = item.get("policy_digest")
        if isinstance(pdig, str) and pdig == binding.get("policy_digest"):
            pdig_meta: object = 0
        else:
            if type(pdig) is int:
                return None
            pdig_meta = pdig
        tail_meta.append(
            [
                item["action"],
                item["reason"],
                item["actor"],
                hsha_meta,
                item["expires_at"],
                item.get("actor_role"),
                fref_meta,
                item.get("recorded_at"),
                pdig_meta,
                item.get("provenance"),
                item.get("resolution_kind"),
                item.get("target_kind"),
                item.get("incomplete_coverage"),
            ]
        )
    meta_buf += canonical_bytes(tail_meta)
    if len(hashes) > 0xFFFF or len(meta_buf) > MAX_PARTITION_DECODED_BYTES:
        return None
    compressor = zlib.compressobj(level=9, wbits=-15, zdict=_PACKED_PAGE_ZDICT)
    comp_meta = compressor.compress(bytes(meta_buf)) + compressor.flush()
    wire = struct.pack(">HI", len(hashes), len(meta_buf)) + bytes(hashes) + comp_meta
    packed = {
        "schema_version": PACKED_PAGE_INDEX_VERSION,
        "ids": ids,
        "d": base64.b85encode(wire).decode("ascii"),
    }
    try:
        unpacked = _unpack_page_index(
            packed,
            expected_binding=binding,
            result_digest=document["result_digest"],
        )
    except ReviewInputError:
        return None
    if canonical_bytes(unpacked) != canonical_bytes(document):
        return None
    return packed


def _unpack_page_index(
    value: object,
    *,
    expected_binding: Mapping[str, object],
    result_digest: str,
) -> object:
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != PACKED_PAGE_INDEX_VERSION
    ):
        return value
    if (
        set(value) != {"schema_version", "ids", "d"}
        or not isinstance(value["ids"], list)
        or len(value["ids"]) > MAX_DISPOSITION_PAGES
        or type(value["d"]) is not str
        or len(value["d"]) > 2 * MAX_PARTITION_DECODED_BYTES
    ):
        raise ReviewInputError("disposition page index does not match root")
    try:
        wire = base64.b85decode(value["d"].encode("ascii"))
        if len(wire) < 6:
            raise ReviewInputError("disposition page index does not match root")
        hashes_len, meta_len = struct.unpack_from(">HI", wire, 0)
        if (
            not 0 <= hashes_len <= len(wire) - 6
            or not 4 <= meta_len <= MAX_PARTITION_DECODED_BYTES
        ):
            raise ReviewInputError("disposition page index does not match root")
        hashes = wire[6 : 6 + hashes_len]
        decomp = zlib.decompressobj(wbits=-15, zdict=_PACKED_PAGE_ZDICT)
        meta_buf = decomp.decompress(wire[6 + hashes_len :], meta_len + 1)
        meta_buf += decomp.flush(1)
        if not decomp.eof or decomp.unconsumed_tail or len(meta_buf) != meta_len:
            raise ReviewInputError("disposition page index does not match root")
        hpos = 0
        page_count, tail_count = struct.unpack_from(">HH", meta_buf, 0)
        mpos = 4
        if page_count != len(value["ids"]) or tail_count > 4096:
            raise ReviewInputError("disposition page index does not match root")
        pages: list[dict[str, Any]] = []
        for id_list in value["ids"]:
            if (
                not isinstance(id_list, list)
                or mpos + 10 > meta_len
                or hpos + 32 > hashes_len
            ):
                raise ReviewInputError("disposition page index does not match root")
            gen, dec_bytes, part_count = struct.unpack_from(">IIH", meta_buf, mpos)
            mpos += 10
            sha = hashes[hpos : hpos + 32].hex()
            hpos += 32
            if (
                not 1 <= part_count == len(id_list) <= MAX_PARTS
                or mpos + 4 * part_count > meta_len
                or hpos + 32 * part_count > hashes_len
            ):
                raise ReviewInputError("disposition page index does not match root")
            parts: list[dict[str, object]] = []
            encoded_bytes = 0
            for sid in id_list:
                if type(sid) is not str:
                    raise ReviewInputError("disposition page index does not match root")
                (size,) = struct.unpack_from(">I", meta_buf, mpos)
                mpos += 4
                psha = hashes[hpos : hpos + 32].hex()
                hpos += 32
                encoded_bytes += size + PART_FRAMING_RESERVE
                parts.append({"storage_id": sid, "bytes": size, "sha256": psha})
            pages.append(
                {
                    "encoding": PARTITION_ENCODING,
                    "binding": {**expected_binding, "generation": gen},
                    "item_count": DISPOSITION_PAGE_ITEMS,
                    "decoded_bytes": dec_bytes,
                    "sha256": sha,
                    "encoded_bytes": encoded_bytes,
                    "parts": parts,
                }
            )
        if hpos + 32 * tail_count != hashes_len:
            raise ReviewInputError("disposition page index does not match root")
        tail_fps = [
            hashes[hpos + 32 * index : hpos + 32 * (index + 1)].hex()
            for index in range(tail_count)
        ]
        tail_meta = json.loads(meta_buf[mpos:].decode("utf-8"))
        if not isinstance(tail_meta, list) or len(tail_meta) != tail_count:
            raise ReviewInputError("disposition page index does not match root")
        tail: list[dict[str, Any]] = []
        for fp, row in zip(tail_fps, tail_meta):
            if not isinstance(row, list) or len(row) != 13:
                raise ReviewInputError("disposition page index does not match root")
            (
                act,
                rsn,
                actor,
                hsha_meta,
                exp,
                arole,
                fref_meta,
                rec_at,
                pdig_meta,
                prov,
                rkind,
                tkind,
                inc_cov,
            ) = row
            item_dict: dict[str, Any] = {
                "fingerprint": fp,
                "action": act,
                "reason": rsn,
                "actor": actor,
                "head_sha": expected_binding.get("head_sha")
                if type(hsha_meta) is int and hsha_meta == 0
                else hsha_meta,
                "expires_at": exp,
            }
            if arole is not None:
                item_dict["actor_role"] = arole
            if type(fref_meta) is int and 1 <= fref_meta <= len(fp):
                item_dict["finding_reference"] = "RS-" + fp[:fref_meta].upper()
            elif type(fref_meta) is int and -len(fp) <= fref_meta <= -1:
                item_dict["finding_reference"] = "RS-" + fp[:-fref_meta].lower()
            elif type(fref_meta) is int:
                raise ReviewInputError("disposition page index does not match root")
            elif fref_meta is not None:
                item_dict["finding_reference"] = fref_meta
            if rec_at is not None:
                item_dict["recorded_at"] = rec_at
            if type(pdig_meta) is int and pdig_meta == 0:
                item_dict["policy_digest"] = expected_binding.get("policy_digest")
            elif pdig_meta is not None:
                item_dict["policy_digest"] = pdig_meta
            if prov is not None:
                item_dict["provenance"] = prov
            if rkind is not None:
                item_dict["resolution_kind"] = rkind
            if tkind is not None:
                item_dict["target_kind"] = tkind
            if inc_cov is not None:
                item_dict["incomplete_coverage"] = inc_cov
            tail.append(item_dict)
    except (ValueError, zlib.error, struct.error, UnicodeError) as error:
        raise ReviewInputError("disposition page index does not match root") from error
    return {
        "schema_version": "disposition-pages-v1",
        "result_digest": result_digest,
        "pages": pages,
        "tail": tail,
    }


def validate_page_index(
    value: object, *, root: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Validate a closed finite index; only the authenticated root selects pages."""
    from .session import MAX_DURABLE_BLOCKER_COUNT, _stored_dispositions

    manifest = root.get("state_manifest") if isinstance(root, Mapping) else None
    if (
        not isinstance(manifest, Mapping)
        or type(manifest.get("item_count")) is not int
        or not 0 <= manifest["item_count"] <= MAX_DURABLE_BLOCKER_COUNT
        or not isinstance(root.get("result_digest"), str)
        or re.fullmatch(r"[a-f0-9]{64}", root["result_digest"]) is None
    ):
        raise ReviewInputError("disposition page root is invalid")
    count = manifest["item_count"]
    expected = validate_binding(manifest.get("binding"))
    value = _unpack_page_index(
        value, expected_binding=expected, result_digest=root["result_digest"]
    )
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
    _stored_dispositions(value["tail"], snapshot=True)
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


def _prepared_page_reference(
    page: Mapping[str, Any],
    *,
    binding: dict[str, object],
    values: tuple[Mapping[str, object], ...],
    result_digest: str,
    offset: int,
) -> dict[str, Any]:
    """Bind a reused reference to the exact requested page before any writes.

    The owning adapter still authenticates its physical bytes on readback.
    A matching root/result alone does not prove the requested inventory matches.
    """
    reference = json.loads(canonical_bytes(page))
    if reference.get("encoding") == INLINE_ENCODING:
        read_inline_evidence(reference, expected_binding=reference.get("binding", {}))
    else:
        validate_manifest(reference)
    expected = {
        "schema_version": "disposition-page-v1",
        "result_digest": result_digest,
        "offset": offset,
        "dispositions": list(values[offset : offset + DISPOSITION_PAGE_ITEMS]),
    }
    scope = reference["binding"]
    if (
        reference["item_count"] != DISPOSITION_PAGE_ITEMS
        or reference["sha256"] != hashlib.sha256(canonical_bytes(expected)).hexdigest()
        or any(scope[key] != binding[key] for key in binding if key != "generation")
        or scope["generation"] > binding["generation"]
    ):
        raise ReviewInputError(
            "prepared disposition page reference differs from inventory"
        )
    return reference


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
    unpacked = _unpack_page_index(
        document,
        expected_binding=validate_binding(root["state_manifest"]["binding"]),
        result_digest=root["result_digest"],
    )
    assert isinstance(unpacked, dict)
    values.extend(unpacked["tail"])
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
    allow_packed_index: bool = False,
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
            references.append(
                _prepared_page_reference(
                    page,
                    binding=binding,
                    values=values,
                    result_digest=result_digest,
                    offset=index * DISPOSITION_PAGE_ITEMS,
                )
            )
    document = {
        "schema_version": "disposition-pages-v1",
        "result_digest": result_digest,
        "pages": references,
        "tail": list(values[len(pages) * DISPOSITION_PAGE_ITEMS :]),
    }
    if allow_packed_index:
        packed_doc = pack_page_index(document, binding=binding)
        packed_inline = (
            inline_manifest(packed_doc, binding=binding, item_count=len(values))
            if packed_doc is not None
            else None
        )
        if (
            packed_doc is not None
            and packed_inline is not None
            and len(canonical_bytes(packed_inline)) <= max_manifest_bytes
        ):
            document = packed_doc
            manifest = packed_inline
        else:
            manifest = prepare(document, len(values), max_manifest_bytes)
    else:
        manifest = prepare(document, len(values), max_manifest_bytes)
    root = {
        "schema_version": "2.0",
        "result_digest": result_digest,
        "state_manifest": manifest,
    }
    normalize_snapshot(root)
    validate_page_index(document, root=root)
    return PreparedLocalDispositionSnapshot(manifest, parts, (*references, manifest))
