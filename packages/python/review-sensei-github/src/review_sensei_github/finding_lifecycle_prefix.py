"""Self-contained packed-v5 DATA; no retained-prefix or dispatch authority.

The confirmed prefix is structurally separate from a scalar-independent v3
remainder. Runtime callers must independently prove prefix immutability before
using any capacity measurement. No existing serializer selects this format.
"""

import json
import sys

from review_sensei.bounded_evidence import (
    canonical_bytes,
    decode_evidence,
    encode_evidence,
)
from review_sensei.errors import ReviewInputError
from review_sensei.source_registry import require_module_seal

VERSION = "finding-lifecycle-packed-v5"
MAX_BYTES = 1_048_576


def _plain(value):
    nodes = 0

    def visit(item, depth):
        nonlocal nodes
        nodes += 1
        if depth > 64 or nodes > 200_000:
            raise ReviewInputError("prefix DATA graph exceeds bounds")
        kind = type(item)
        if item is None or kind is bool:
            return
        if kind is int:
            if not -(1 << 63) <= item < (1 << 63):
                raise ReviewInputError("prefix DATA integer exceeds bounds")
            return
        if kind is str:
            if len(item) > MAX_BYTES:
                raise ReviewInputError("prefix DATA string exceeds bounds")
            return
        if kind is list:
            for child in item:
                visit(child, depth + 1)
            return
        if kind is dict:
            for key, child in item.items():
                if type(key) is not str or len(key) > MAX_BYTES:
                    raise ReviewInputError("prefix DATA key is not plain")
                visit(child, depth + 1)
            return
        raise ReviewInputError("prefix DATA value is not plain")

    visit(value, 0)
    try:
        raw = canonical_bytes(value)
    except (ValueError, UnicodeError) as exc:
        raise ReviewInputError("prefix DATA encoding is invalid") from exc
    if len(raw) > MAX_BYTES:
        raise ReviewInputError("prefix DATA exceeds complete byte bound")
    return json.loads(raw)


def _sources():
    from review_sensei import bounded_evidence

    from . import finding_lifecycle, guarded_publication_batch

    for module in (
        sys.modules[__name__],
        bounded_evidence,
        finding_lifecycle,
        guarded_publication_batch,
    ):
        require_module_seal(module)
    return finding_lifecycle, guarded_publication_batch


def _request_hash(effect, journal):
    return journal._digest(
        {
            "method": effect["method"],
            "path": effect["path"],
            "request": journal._request_identity(effect["request"]),
        }
    )


def pack(document, *, prefix_count):
    """Encode validated DATA with a fixed nonempty suffix; never issue custody."""
    journal, pair = _sources()
    document = _plain(document)
    journal._validate(document)
    effects = document["effects"]
    if (
        document["schema_version"] != "finding-lifecycle-journal-v3"
        or type(prefix_count) is not int
        or not 3 <= prefix_count == len(effects) - 1 < 32
        or any(e["status"] != "confirmed" for e in effects[:prefix_count])
    ):
        raise ReviewInputError("prefix requires confirmed guarded history and a suffix")
    descriptor = pair.describe_pair(effects)
    if descriptor != document["guarded_publication_pair"]:
        raise ReviewInputError("prefix pair descriptor conflicts")
    prefix = effects[:prefix_count]
    for effect in prefix:
        if effect["request_sha256"] == _request_hash(effect, journal):
            del effect["request_sha256"]
    # All four values are validated exact dependencies, never guessed hashes.
    descriptor = {k: v for k, v in descriptor.items() if not k.endswith("_sha256")}
    remainder = {k: v for k, v in document.items() if k != "guarded_publication_pair"}
    remainder["effects"] = effects[prefix_count:]
    result = {
        "schema_version": VERSION,
        "prefix_count": prefix_count,
        "prefix": encode_evidence(
            {"effects": prefix, "guarded_publication_pair": descriptor},
            max_decoded_bytes=MAX_BYTES,
        ),
        "remainder": journal._pack_journal(remainder),
    }
    _plain(result)
    return result


def _decode(stored):
    """Reconstruct structurally bounded data, without accepting suffix semantics."""
    journal, pair = _sources()
    stored = _plain(stored)
    if (
        type(stored) is not dict
        or set(stored) != {"schema_version", "prefix_count", "prefix", "remainder"}
        or stored["schema_version"] != VERSION
        or type(stored["prefix_count"]) is not int
        or not 3 <= stored["prefix_count"] < 32
    ):
        raise ReviewInputError("prefix carrier shape is invalid")
    remainder = stored["remainder"]
    if (
        type(remainder) is not dict
        or set(remainder) != {"schema_version", "journal", "native_atoms"}
        or remainder["schema_version"] != "finding-lifecycle-packed-v3"
    ):
        raise ReviewInputError("prefix remainder must be nonrecursive original v3")
    prefix = decode_evidence(
        stored["prefix"], max_encoded_bytes=MAX_BYTES, max_decoded_bytes=MAX_BYTES
    )
    prefix = _plain(prefix)
    if (
        type(prefix) is not dict
        or set(prefix) != {"effects", "guarded_publication_pair"}
        or type(prefix["effects"]) is not list
        or len(prefix["effects"]) != stored["prefix_count"]
    ):
        raise ReviewInputError("prefix effect count or shape changed")
    for effect in prefix["effects"]:
        if type(effect) is not dict or not {
            "method",
            "path",
            "request",
            "status",
            "response",
        } <= set(effect):
            raise ReviewInputError("prefix effect is incomplete")
        allowed = {
            "method",
            "path",
            "request",
            "request_sha256",
            "status",
            "response",
            "check_update_precondition",
            "review_update_precondition",
        }
        if not set(effect) <= allowed:
            raise ReviewInputError("prefix effect fields changed")
        if "request_sha256" not in effect:
            effect["request_sha256"] = _request_hash(effect, journal)
    document = journal._unpack_journal(remainder)
    if (
        type(document) is not dict
        or set(document)
        != {
            "schema_version",
            "lifecycle",
            "effects",
            "unknown_trust_effect",
            "native_receipts",
            "accepted_semantic_history",
        }
        or document.get("schema_version") != "finding-lifecycle-journal-v3"
        or type(document.get("effects")) is not list
        or len(document["effects"]) != 1
    ):
        raise ReviewInputError("prefix remainder overlaps or has no suffix")
    document["effects"] = prefix["effects"] + document["effects"]
    if len(document["effects"]) > 32:
        raise ReviewInputError("prefix effect capacity exceeded")
    expected = pair.describe_pair(document["effects"])
    descriptor = prefix["guarded_publication_pair"]
    if descriptor != {k: v for k, v in expected.items() if not k.endswith("_sha256")}:
        raise ReviewInputError("prefix pair descriptor conflicts")
    document["guarded_publication_pair"] = expected
    _plain(document)
    if any(e["status"] != "confirmed" for e in prefix["effects"]):
        raise ReviewInputError("prefix contains an unconfirmed effect")
    for effect in prefix["effects"]:
        direct = journal._digest(
            {
                "method": effect["method"],
                "path": effect["path"],
                "request": effect["request"],
            }
        )
        if effect["request_sha256"] not in (direct, _request_hash(effect, journal)):
            raise ReviewInputError("prefix request digest conflicts")
    canonical_prefix = _plain(prefix)
    for effect in canonical_prefix["effects"]:
        if effect["request_sha256"] == _request_hash(effect, journal):
            del effect["request_sha256"]
    if (
        encode_evidence(canonical_prefix, max_decoded_bytes=MAX_BYTES)
        != stored["prefix"]
    ):
        raise ReviewInputError("prefix segment is noncanonical")
    return document


def unpack(stored):
    """Strict semantic decoding; a sizing assembly is not evidence."""
    journal, _pair = _sources()
    stored = _plain(stored)
    document = _decode(stored)
    journal._validate(document)
    if pack(document, prefix_count=stored["prefix_count"]) != stored:
        raise ReviewInputError("prefix carrier is noncanonical")
    return document


def prepare_prefix(document, *, prefix_count):
    """Return detached prefix DATA from a fully validated logical document."""
    value = pack(document, prefix_count=prefix_count)
    return {"prefix_count": value["prefix_count"], "prefix": value["prefix"]}


def remainder(document, *, prefix_count):
    """Return the independently legacy-packed suffix from validated DATA."""
    return pack(document, prefix_count=prefix_count)["remainder"]


def assemble_data(prefix, *, remainder):
    """Inert sizing assembly; never establishes prefix provenance or validity of
    hypothetical suffix semantics. Only unpack accepts a full logical journal.
    """
    _sources()
    prefix = _plain(prefix)
    if type(prefix) is not dict or set(prefix) != {"prefix_count", "prefix"}:
        raise ReviewInputError("prefix sizing segment shape changed")
    value = {"schema_version": VERSION, **prefix, "remainder": _plain(remainder)}
    _decode(value)
    return value


from review_sensei.source_registry import seal_module as _seal_source  # noqa: E402

_seal_source()
