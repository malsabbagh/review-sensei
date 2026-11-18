"""Pure proposed journal-v4 codec; intentionally unsupported by runtime writers.

The existing runtime journal validator still refuses v4. This module supplies no
issuer, lease, migration, dispatch or capacity grant. It delegates each old base
variant to the unchanged parser and shares the original one-MiB total limit.
"""

import json

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.finding_lifecycle import MAX_LIFECYCLE_BYTES

from .finding_lifecycle import _pack_journal, _unpack_journal, _validate
from .original_admission_projection import restore_original_admission

VERSION = "finding-lifecycle-journal-v4"
BASE_VERSIONS = frozenset(
    {
        "finding-lifecycle-journal-v1",
        "finding-lifecycle-journal-v2",
        "finding-lifecycle-journal-v3",
    }
)


def validate_original_admission_journal(document, source, *, identity, plan=None):
    if (
        isinstance(document, dict)
        and document.get("base_schema_version") == "grouped-inline-pair-v1"
    ):
        from .grouped_inline_journal import validate_grouped_inline_journal

        return validate_grouped_inline_journal(
            document, source, identity=identity, plan=plan
        )
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != VERSION
        or not isinstance(document.get("base_schema_version"), str)
        or document["base_schema_version"] not in BASE_VERSIONS
        or "original_admission" not in document
    ):
        raise ReviewInputError("original admission journal version is unsupported")
    if len(canonical_bytes(document)) > MAX_LIFECYCLE_BYTES:
        raise ReviewInputError(
            "original admission journal total byte capacity exceeded"
        )
    base = dict(document)
    base["schema_version"] = base.pop("base_schema_version")
    projection = base.pop("original_admission")
    _validate(base)  # Exact old shape/semantics, with no new defaults.
    restore_original_admission(projection, source, identity=identity)
    return json.loads(canonical_bytes(document))


def original_admission_journal(base, projection, source, *, identity):
    if (
        not isinstance(base, dict)
        or not isinstance(base.get("schema_version"), str)
        or base["schema_version"] not in BASE_VERSIONS
    ):
        raise ReviewInputError("original admission journal base is unsupported")
    _validate(base)
    document = {
        **base,
        "schema_version": VERSION,
        "base_schema_version": base["schema_version"],
        "original_admission": projection,
    }
    return validate_original_admission_journal(document, source, identity=identity)


def pack_original_admission_journal(document, source, *, identity, plan=None):
    return _pack_journal(
        validate_original_admission_journal(
            document, source, identity=identity, plan=plan
        )
    )


def unpack_original_admission_journal(packed, source, *, identity, plan=None):
    # The original decoder enforces both its one-MiB decompression limit and
    # its one-MiB reconstructed limit (including expanded scalar identities).
    return validate_original_admission_journal(
        _unpack_journal(packed), source, identity=identity, plan=plan
    )
