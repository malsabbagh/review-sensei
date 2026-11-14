"""Lossless pending carriers; an immutable base supplies bytes, never authority."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import zlib
from typing import NoReturn, cast

from review_sensei.bounded_evidence import (
    MAX_PARTITION_DECODED_BYTES,
    canonical_bytes,
    read_inline_evidence,
    validate_binding,
    validate_manifest,
)
from review_sensei.errors import ReviewInputError

VERSION = "finding-lifecycle-packed-v4"
DERIVATION = "canonical-unique-strings-tail-v1"
MAX_BYTES = 1_048_576
FIELDS = {
    "schema_version",
    "encoding",
    "decoded_bytes",
    "sha256",
    "data",
    "dictionary_manifest",
    "dictionary_derivation",
    "dictionary_bytes",
    "dictionary_sha256",
}


def _bad(message) -> NoReturn:
    raise ReviewInputError("finding lifecycle dictionary " + message)


def is_dictionary(value):
    return isinstance(value, dict) and value.get("schema_version") == VERSION


def _manifest(value, current_binding):
    if not isinstance(value, dict):
        _bad("manifest is invalid")
    binding = validate_binding(value.get("binding"))
    current = validate_binding(current_binding)
    if cast(int, binding["generation"]) > cast(int, current["generation"]) or any(
        binding[key] != item for key, item in current.items() if key != "generation"
    ):
        _bad("base scope conflicts with current authority")
    if value.get("encoding") == "inline-json-v1":
        read_inline_evidence(value, expected_binding=binding)
    else:
        validate_manifest(value)
    return binding


def _base(base, manifest, current_binding):
    from .finding_lifecycle import _unpack_journal, _validate

    _manifest(manifest, current_binding)
    if (
        not isinstance(base, dict)
        or set(base) != {"schema_version", "lifecycle"}
        or base["schema_version"] != "finding-lifecycle-evidence-v2"
        or not isinstance(base["lifecycle"], dict)
        or base["lifecycle"].get("schema_version") != "finding-lifecycle-packed-v3"
    ):
        _bad("base must be a nonrecursive full v3 snapshot")
    encoded = canonical_bytes(base)
    if (
        manifest["encoding"] != "inline-json-v1"
        and len(encoded) != manifest["decoded_bytes"]
    ) or hashlib.sha256(encoded).hexdigest() != manifest["sha256"]:
        _bad("base content differs from committed manifest")
    document = _validate(_unpack_journal(base["lifecycle"]))
    raw = canonical_bytes(document)
    strings = set()

    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                strings.add(key)
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, str):
            strings.add(value)

    visit(document)
    dictionary = canonical_bytes(sorted(strings))[-32768:]
    return dictionary, len(encoded) + len(raw)


def pack(document, *, base, manifest, current_binding):
    from .finding_lifecycle import _validate

    document = _validate(document)
    raw = canonical_bytes(document)
    dictionary, base_bytes = _base(base, manifest, current_binding)
    if len(raw) > MAX_BYTES or base_bytes + len(raw) > MAX_PARTITION_DECODED_BYTES:
        _bad("combined decoded capacity exceeded")
    compressor = zlib.compressobj(level=9, zdict=dictionary)
    data = compressor.compress(raw) + compressor.flush()
    value = {
        "schema_version": VERSION,
        "encoding": "zlib-json-dictionary-v1",
        "decoded_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "data": base64.b64encode(data).decode("ascii"),
        "dictionary_manifest": json.loads(canonical_bytes(manifest)),
        "dictionary_derivation": DERIVATION,
        "dictionary_bytes": len(dictionary),
        "dictionary_sha256": hashlib.sha256(dictionary).hexdigest(),
    }
    if len(canonical_bytes(value)) > MAX_BYTES:
        _bad("encoded capacity exceeded")
    return value


def _shape(value):
    if (
        not isinstance(value, dict)
        or set(value) != FIELDS
        or value.get("schema_version") != VERSION
        or value.get("encoding") != "zlib-json-dictionary-v1"
        or value.get("dictionary_derivation") != DERIVATION
        or type(value.get("decoded_bytes")) is not int
        or not 0 < value["decoded_bytes"] <= MAX_BYTES
        or type(value.get("dictionary_bytes")) is not int
        or not 0 < value["dictionary_bytes"] <= 32768
        or any(
            not isinstance(value.get(k), str)
            or re.fullmatch(r"[a-f0-9]{64}", value[k]) is None
            for k in ("sha256", "dictionary_sha256")
        )
        or not isinstance(value.get("data"), str)
        or len(value["data"]) > MAX_BYTES
        or len(canonical_bytes(value)) > MAX_BYTES
    ):
        _bad("carrier shape or bounds are invalid")


def unpack(value, *, base, current_binding):
    from .finding_lifecycle import _validate

    _shape(value)
    dictionary, base_bytes = _base(base, value["dictionary_manifest"], current_binding)
    if (
        len(dictionary) != value["dictionary_bytes"]
        or hashlib.sha256(dictionary).hexdigest() != value["dictionary_sha256"]
        or base_bytes + value["decoded_bytes"] > MAX_PARTITION_DECODED_BYTES
    ):
        _bad("dictionary commitment or combined capacity conflicts")
    try:
        data = base64.b64decode(value["data"], validate=True)
        decoder = zlib.decompressobj(zdict=dictionary)
        raw = decoder.decompress(data, value["decoded_bytes"] + 1)
        if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            _bad("stream is incomplete, trailing or oversized")
        raw += decoder.flush()
    except (ValueError, binascii.Error, zlib.error) as exc:
        raise ReviewInputError(
            "finding lifecycle dictionary stream is invalid"
        ) from exc
    if (
        len(raw) != value["decoded_bytes"]
        or hashlib.sha256(raw).hexdigest() != value["sha256"]
    ):
        _bad("target length or digest conflicts")
    try:
        document = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ReviewInputError(
            "finding lifecycle dictionary target JSON is invalid"
        ) from exc
    if canonical_bytes(document) != raw:
        _bad("target JSON is not canonical")
    return _validate(document)


def restore_cost(ledger, stored, *, record=None):
    """Original-domain dependent reads and closing fence; no new allowance."""
    if not is_dictionary(stored):
        return 0
    from review_sensei.session import LocalSessionLedger

    from .api_run_requests import ApiRunSessionLedger
    from .caller_operation import prewrite_fence_requests
    from .review_run import ActionRunLedger

    manifest = stored.get("dictionary_manifest")
    if not isinstance(manifest, dict):
        _bad("manifest is invalid")
    parts = (
        0
        if manifest.get("encoding") == "inline-json-v1"
        else len(manifest.get("parts", ()))
    )
    if type(ledger) is LocalSessionLedger:
        if record is None or any(
            getattr(record, field) is not None
            for field in (
                "convergence_history",
                "disposition_snapshot",
                "assessment_queue",
                "historical_assessments",
            )
        ):
            _bad("local closing read dependencies are unqualified")
        # A changed cached inode can cost observation + resolve + containment +
        # read. A missing closing root can also inspect its enrollment witness.
        return 4 * parts + 4
    if (
        type(ledger) in {ApiRunSessionLedger, ActionRunLedger}
        and getattr(ledger.load, "__func__", None) is ApiRunSessionLedger.load
    ):
        return parts + 10 + prewrite_fence_requests(ledger.caller)
    _bad("closing read source is unqualified")


def resolve_stored(ledger, record, stored):
    """Resolve bytes under current authority, then close every dependent read."""
    if not is_dictionary(stored):
        return stored
    from .finding_lifecycle import _pack_journal

    _shape(stored)
    reference = record.finding_lifecycle
    if not isinstance(reference, dict):
        _bad("current selected reference is missing")
    current_binding = reference["manifest"]["binding"]
    manifest = stored.get("dictionary_manifest")
    base_binding = _manifest(manifest, current_binding)
    budget = getattr(ledger, "evidence_budget", None)
    if budget is None:
        _bad("original evidence budget is unavailable")
    budget.preflight(restore_cost(ledger, stored, record=record))
    budget.check()
    base = (
        read_inline_evidence(manifest, expected_binding=base_binding)
        if manifest["encoding"] == "inline-json-v1"
        else ledger.read_evidence(
            record.identity(), manifest, expected_binding=base_binding
        )
    )
    budget.check()
    document = unpack(stored, base=base, current_binding=current_binding)
    budget.check()
    current = ledger.load(record.identity())
    if (
        ledger.evidence_budget is not budget
        or current.status != "ok"
        or current.record is None
        or current.record.identity() != record.identity()
        or current.record.record_sha256 != record.record_sha256
        or canonical_bytes(current.record.to_dict())
        != canonical_bytes(record.to_dict())
    ):
        _bad("current authority changed during dependent reads")
    budget.check()
    return _pack_journal(document)
