"""Purpose-bound finding evidence selected by the independently authenticated root.

The evidence transport supplies integrity and producer authentication; only a
current session ledger selects authority. This is not a new storage backend or
an inter-process fencing mechanism. Callers must preflight their native effect
and confirmation tail before activating a pending intent here.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping
from weakref import WeakKeyDictionary

from .bounded_evidence import (
    INLINE_ENCODING,
    canonical_bytes,
    evidence_manifest,
    inline_manifest,
    partition_evidence,
    read_inline_evidence,
    validate_binding,
    validate_manifest,
)
from .errors import ReviewInputError

REFERENCE_VERSION = "finding-lifecycle-reference-v1"
HISTORY_REFERENCE_VERSION = "finding-lifecycle-reference-v2"
DOCUMENT_VERSION = "finding-lifecycle-evidence-v2"
LEGACY_DOCUMENT_VERSION = "finding-lifecycle-evidence-v1"
MAX_REFERENCE_BYTES = 8192
_SHA = re.compile(r"[a-f0-9]{64}")


def _sha(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise ReviewInputError(f"finding lifecycle {label} is invalid")
    return value


def normalize_reference(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ReviewInputError("finding lifecycle reference is unsupported")
    fields = {
        "schema_version",
        "operation_id",
        "previous_root_sha256",
        "previous_snapshot_sha256",
        "manifest",
    }
    if value.get("schema_version") == HISTORY_REFERENCE_VERSION:
        fields.add("accepted_semantic_history")
    elif value.get("schema_version") != REFERENCE_VERSION:
        raise ReviewInputError("finding lifecycle reference is unsupported")
    if set(value) != fields:
        raise ReviewInputError("finding lifecycle reference is unsupported")
    for name in ("operation_id", "previous_root_sha256"):
        _sha(value[name], name)
    if value["previous_snapshot_sha256"] is not None:
        _sha(value["previous_snapshot_sha256"], "predecessor")
    manifest = value["manifest"]
    if not isinstance(manifest, dict):
        raise ReviewInputError("finding lifecycle manifest is invalid")
    binding = validate_binding(manifest.get("binding"))
    if binding["purpose"] != "finding-lifecycle":
        raise ReviewInputError("finding lifecycle purpose conflicts")
    if "accepted_semantic_history" in value:
        _validate_accepted_history_metadata(value["accepted_semantic_history"], binding)
    if manifest.get("encoding") == INLINE_ENCODING:
        read_inline_evidence(manifest, expected_binding=binding)
    else:
        validate_manifest(manifest)
    if len(canonical_bytes(value)) > MAX_REFERENCE_BYTES:
        raise ReviewInputError("finding lifecycle reference capacity exceeded")
    return json.loads(canonical_bytes(value))


def require_reference_successor(before: object, after: object) -> None:
    old, new = normalize_reference(before), normalize_reference(after)
    if old is None:
        if new is not None and "accepted_semantic_history" in new:
            raise ReviewInputError(
                "finding lifecycle initial history lacks terminal proof"
            )
        if new is not None and new["previous_snapshot_sha256"] is not None:
            raise ReviewInputError("finding lifecycle initial predecessor conflicts")
        return
    if new is None:
        raise ReviewInputError("finding lifecycle authority cannot be removed")
    if old == new or is_history_metadata_amendment(old, new):
        return
    if old.get("accepted_semantic_history") != new.get("accepted_semantic_history"):
        raise ReviewInputError("finding lifecycle history requires terminal amendment")
    old_binding, new_binding = old["manifest"]["binding"], new["manifest"]["binding"]
    if (
        new["previous_snapshot_sha256"] != old["manifest"]["sha256"]
        or new_binding["generation"] <= old_binding["generation"]
        or any(
            new_binding[k] != old_binding[k]
            for k in ("repository", "repository_id", "pull_request", "producer")
        )
    ):
        raise ReviewInputError("finding lifecycle lineage conflicts")


def _selected(ledger: Any, record: Any) -> None:
    loaded = ledger.load(record.identity())
    if (
        loaded.status != "ok"
        or loaded.record is None
        or loaded.record.record_sha256 != record.record_sha256
    ):
        raise ReviewInputError("finding lifecycle current session authority conflicts")


def read_finding_lifecycle(
    ledger: Any, record: Any, *, _selected_load: Any = None
) -> dict[str, Any] | None:
    """Load only the reference selected by a freshly authenticated current root."""
    if _selected_load is None:
        _selected(ledger, record)
    elif (
        _selected_load.status not in {"ok", "migrated"}
        or _selected_load.record is None
        or _selected_load.record.identity() != record.identity()
        or _selected_load.record.record_sha256 != record.record_sha256
    ):
        raise ReviewInputError("finding lifecycle selected load conflicts")
    # A supplied load is only a claim until the independent final fresh fence.
    value = _decode_selected_lifecycle(ledger, record)
    _selected(ledger, record)
    return value


def select_finding_lifecycle(ledger: Any, identity: Any) -> tuple[Any, Any]:
    """Select a current root ourselves, then decode inline bytes without more I/O.

    Unlike the compatibility read API, callers cannot supply the selected root.
    Inline evidence is entirely inside that authenticated root; decoding it is
    pure. External partition reads retain a final fresh root fence.
    """
    selected = ledger.load(identity)
    if selected.status not in {"ok", "migrated", "missing"}:
        raise ReviewInputError(
            "finding lifecycle selected session is unavailable: " + selected.status
        )
    record = selected.record
    if record is None or record.finding_lifecycle is None:
        return selected, None
    value = _decode_selected_lifecycle(ledger, record)
    if record.finding_lifecycle["manifest"]["encoding"] != INLINE_ENCODING:
        _selected(ledger, record)
    return selected, value


def _decode_selected_lifecycle(ledger: Any, record: Any) -> dict[str, Any] | None:
    reference = normalize_reference(record.finding_lifecycle)
    if reference is None:
        return None  # Not proof of empty/new enrollment; admission decides that.
    manifest = reference["manifest"]
    binding = manifest["binding"]
    if (
        binding["producer"] != ledger.evidence_producer()
        or binding["repository"] != record.repository
        or binding["repository_id"] != record.repository_id
        or binding["pull_request"] != record.pull_request
        or binding["generation"] > record.generation
    ):
        raise ReviewInputError(
            "finding lifecycle independently selected scope conflicts"
        )
    value = (
        read_inline_evidence(manifest, expected_binding=binding)
        if manifest["encoding"] == INLINE_ENCODING
        else ledger.read_evidence(record.identity(), manifest, expected_binding=binding)
    )
    if not isinstance(value, dict):
        raise ReviewInputError("finding lifecycle evidence is unsupported")
    version = value.get("schema_version")
    lineage = {"operation_id", "previous_root_sha256", "previous_snapshot_sha256"}
    expected = {"schema_version", "lifecycle"}
    if version == LEGACY_DOCUMENT_VERSION:
        expected |= lineage
    elif version != DOCUMENT_VERSION:
        raise ReviewInputError("finding lifecycle evidence is unsupported")
    if set(value) != expected:
        raise ReviewInputError("finding lifecycle evidence is unsupported")
    if version == LEGACY_DOCUMENT_VERSION and any(
        value[k] != reference[k] for k in lineage
    ):
        raise ReviewInputError("finding lifecycle operation/lineage conflicts")
    # V2 omits duplicate metadata, not authority: the selected authenticated root
    # commits operation/lineage alongside the manifest digest. Root transitions
    # independently check admission and predecessors; evidence never selects them.
    if not isinstance(value["lifecycle"], dict):
        raise ReviewInputError("finding lifecycle document is invalid")
    return value["lifecycle"]


def _inline_local_commit_calls(ledger: Any, record: Any) -> int | None:
    """Exact post-mutator liability for the plain local, self-contained root.

    The dedicated inline path validates the selected snapshot without another
    initial load. With no external baseline, disposition or queue reads,
    its physical dispatches are: prewrite root fence (1), durable temp
    write including sync/cleanup (1), atomic activation/directory-sync fence (1),
    and persisted-root readback fence (1). Inline staging itself performs no I/O.

    Do not apply this count to hosted/wrapped/subclass adapters or roots requiring
    evidence reads. Their existing conservative preflight remains unchanged.
    All four are conservatively reserved as ordinary work, leaving the fixed four
    reconciliation calls untouched. This is not a native-effect tail allowance.
    """
    from .session import LocalSessionLedger

    if type(ledger) is not LocalSessionLedger:
        return None
    if record.disposition_snapshot is not None or record.assessment_queue is not None:
        return None
    history = record.convergence_history
    baseline = history.get("baseline") if isinstance(history, Mapping) else None
    if (
        isinstance(baseline, Mapping)
        and baseline.get("encoding") == "partitioned-json-v1"
    ):
        return None
    return 4


# These one-use objects are in-process sizing witnesses, never stored evidence
# or permission to dispatch. Only an adapter's verified plain writer issues one.
class _CheckpointPricing:
    pass


_CHECKPOINT_PRICING: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()
_CHECKPOINT_PRICERS: set[Any] = set()
_CHECKPOINT_PRODUCERS: set[Any] = set()
_CHECKPOINT_CANDIDATES: dict[Any, Any] = {}
_CHECKPOINT_SOURCE: Any = None
_CHECKPOINT_SUPPORTS: Any = None


def _checkpoint_pricer(method):
    """Inject issuance only while executing the exact installed host producer."""
    import sys
    from functools import wraps

    token = object()

    def supported(owner):
        module = sys.modules.get("review_sensei_github.api_run_requests")
        run_module = sys.modules.get("review_sensei_github.review_run")
        cls = getattr(module, "ApiRunSessionLedger", None)
        allowed = {
            cls,
            getattr(run_module, "ActionRunLedger", None),
            getattr(run_module, "ReviewRunLedger", None),
        }
        return (
            wrapped is _CHECKPOINT_SOURCE
            and cls is not None
            and type(owner) in allowed
            and method.__name__ == "preflight_finding_lifecycle_checkpoint_pricing"
            and getattr(cls, method.__name__, None) is wrapped
            and getattr(getattr(owner, method.__name__, None), "__func__", None)
            is wrapped
        )

    @wraps(method)
    def wrapped(owner, *args, **kwargs):
        if "_checkpoint_factory" in kwargs:
            raise ReviewInputError(
                "finding lifecycle checkpoint producer is unsupported"
            )
        if not supported(owner):
            module = sys.modules.get("review_sensei_github.api_run_requests")
            cls = getattr(module, "ApiRunSessionLedger", None)
            if (
                wrapped is _CHECKPOINT_SOURCE
                and cls is not None
                and getattr(cls, method.__name__, None) is wrapped
                and isinstance(owner, cls)
                and getattr(getattr(owner, method.__name__, None), "__func__", None)
                is wrapped
            ):
                # A custom inherited adapter gets the existing generic guard,
                # never the installed source body or its issuing factory.
                return None
            raise ReviewInputError(
                "finding lifecycle checkpoint producer is unsupported"
            )

        active = True

        def issue(ledger, record, **values):
            if not active or ledger is not owner or not supported(owner):
                raise ReviewInputError("finding lifecycle checkpoint producer changed")
            return _issue_checkpoint_pricing(ledger, record, _source=token, **values)

        try:
            return method(owner, *args, _checkpoint_factory=issue, **kwargs)
        finally:
            active = False

    def install():
        if _CHECKPOINT_SOURCE is not wrapped:
            raise ReviewInputError(
                "finding lifecycle checkpoint source is not installed"
            )
        _CHECKPOINT_PRICERS.add(token)

    _CHECKPOINT_CANDIDATES[wrapped] = (install, supported)
    return wrapped


def _install_checkpoint_pricer(source):
    """Pin the actual adapter producer once, during its trusted module install."""
    import sys

    global _CHECKPOINT_SOURCE, _CHECKPOINT_SUPPORTS
    module = sys.modules.get("review_sensei_github.api_run_requests")
    cls = getattr(module, "ApiRunSessionLedger", None)
    if (
        _CHECKPOINT_SOURCE is not None
        or cls is None
        or getattr(cls, "preflight_finding_lifecycle_checkpoint_pricing", None)
        is not source
        or source not in _CHECKPOINT_CANDIDATES
    ):
        raise ReviewInputError("finding lifecycle checkpoint source cannot be replaced")
    install, supported = _CHECKPOINT_CANDIDATES.pop(source)
    _CHECKPOINT_SOURCE, _CHECKPOINT_SUPPORTS = source, supported
    _CHECKPOINT_PRODUCERS.add(source)
    install()


def _qualified_checkpoint_pricer(ledger):
    method = getattr(ledger, "preflight_finding_lifecycle_checkpoint_pricing", None)
    if (
        _CHECKPOINT_SOURCE is None
        or getattr(method, "__func__", None) is not _CHECKPOINT_SOURCE
        or not _CHECKPOINT_SUPPORTS(ledger)
    ):
        return None
    return method


def _issue_checkpoint_pricing(
    ledger,
    record,
    *,
    checkpoint_parts,
    capacity,
    calls,
    validate,
    page_counts=None,
    profile="plain-v1",
    _source=None,
    staging_target=None,
):
    source = getattr(
        getattr(ledger, "preflight_finding_lifecycle_checkpoint_pricing", None),
        "__func__",
        None,
    )
    if _source not in _CHECKPOINT_PRICERS:
        raise ReviewInputError(
            "finding lifecycle checkpoint pricing source is unsupported"
        )
    proof = _CheckpointPricing()
    _CHECKPOINT_PRICING[proof] = (
        ledger,
        record,
        canonical_bytes(record.to_dict()),
        checkpoint_parts,
        canonical_bytes(capacity),
        calls,
        validate,
        source,
        tuple(1 for _ in checkpoint_parts) if page_counts is None else page_counts,
        profile,
        _source,
        canonical_bytes(staging_target) if staging_target is not None else None,
    )
    return proof


def _consume_checkpoint_pricing(
    proof, ledger, record, checkpoint_parts, *, staging_target=None
):
    if type(proof) is not _CheckpointPricing:
        raise ReviewInputError("finding lifecycle checkpoint pricing is unissued")
    issued = _CHECKPOINT_PRICING.pop(proof, None)
    if (
        issued is None
        or issued[0] is not ledger
        or issued[1] is not record
        or issued[2] != canonical_bytes(record.to_dict())
        or issued[3] != checkpoint_parts
        or getattr(
            getattr(ledger, "preflight_finding_lifecycle_checkpoint_pricing", None),
            "__func__",
            None,
        )
        is not issued[7]
        or issued[10] not in _CHECKPOINT_PRICERS
        or not issued[6]()
    ):
        raise ReviewInputError("finding lifecycle checkpoint pricing changed or reused")
    prices = issued[5]
    pages = issued[8]
    if (
        not isinstance(pages, tuple)
        or len(pages) != len(checkpoint_parts)
        or any(type(page) is not int or not 1 <= page <= 10 for page in pages)
    ):
        raise ReviewInputError("finding lifecycle checkpoint page pricing is invalid")
    profile = issued[9]
    if (
        profile in {"quarantine-staging-v1", "quarantine-inline-staging-v1"}
        and (staging_target is None or issued[11] != canonical_bytes(staging_target))
    ) or (
        profile not in {"quarantine-staging-v1", "quarantine-inline-staging-v1"}
        and staging_target is not None
    ):
        raise ReviewInputError("finding lifecycle staging target changed or absent")
    if profile not in {
        "plain-v1",
        "plain-trust-inline-v1",
        "quarantine-complete-census-v1",
        "quarantine-staging-v1",
        "quarantine-inline-v1",
        "quarantine-inline-staging-v1",
    }:
        raise ReviewInputError(
            "finding lifecycle checkpoint pricing profile is invalid"
        )
    if (
        not isinstance(prices, tuple)
        or len(prices) != len(checkpoint_parts)
        or any(type(n) is not int or n < 0 for n in checkpoint_parts)
        or any(
            type(price) is not int
            or (
                profile == "plain-v1"
                and price - 2 * (page - 1)
                not in ({5, 7} if n == 0 else {4 + 3 * n, 6 + 3 * n})
            )
            or (
                profile == "plain-trust-inline-v1"
                and (n != 0 or price - 2 * (page - 1) not in {6, 8})
            )
            or (
                profile == "quarantine-complete-census-v1"
                and (n < 0 or page < 3 or price != (17 if n == 0 else page + 6 + 2 * n))
            )
            or (
                profile == "quarantine-staging-v1"
                and (n < 1 or page != 1 or price != 2 * n + 5)
            )
            or (
                profile == "quarantine-inline-v1"
                and (n != 0 or page < 3 or price not in {page + 6, page + 8, page + 10})
            )
            or (
                profile == "quarantine-inline-staging-v1"
                and (n != 0 or page != 1 or price != 5)
            )
            for n, price, page in zip(checkpoint_parts, prices, pages)
        )
    ):
        raise ReviewInputError("finding lifecycle checkpoint pricing is invalid")
    return json.loads(issued[4]), prices


class _ExactCheckpointDocuments:
    """Opaque sizing commitment; never a dispatch or durable authority token."""


_EXACT_DOCUMENTS: WeakKeyDictionary = WeakKeyDictionary()
_EXACT_DOCUMENT_ISSUERS: set[Any] = set()


def _sizing_producer(method):
    """Keep issuers in the actual host producer closure, not public markers."""
    import sys
    from functools import wraps

    source = object()
    _EXACT_DOCUMENT_ISSUERS.add(source)

    def supported(owner):
        module = sys.modules.get("review_sensei_github.finding_lifecycle")
        cls = getattr(module, "FindingLifecycleJournal", None)
        return (
            cls is not None
            and type(owner) is cls
            and method.__name__
            in {
                "preflight_native_batch",
                "_exact_pending_proof",
                "preflight_command_finalization",
                "_preflight_preentry_command_branches",
                "preflight_reply_finalization",
            }
            and getattr(cls, method.__name__, None) is wrapped
        )

    @wraps(method)
    def wrapped(owner, *args, **kwargs):
        if "_sizing_factories" in kwargs or not supported(owner):
            raise ReviewInputError("finding lifecycle sizing producer is unsupported")

        def exact(ledger, record, documents, indices, validate):
            if (
                not supported(owner)
                or ledger is not owner.ledger
                or record is not owner._record
            ):
                raise ReviewInputError("finding lifecycle sizing producer changed")
            return _issue_exact_documents(
                source, ledger, record, documents, indices, validate
            )

        def projected(ledger, record, preview, parts, intent, plan, validate):
            if (
                not supported(owner)
                or ledger is not owner.ledger
                or record is not owner._record
            ):
                raise ReviewInputError("finding lifecycle sizing producer changed")
            return _issue_projected_root(
                source, ledger, record, preview, parts, intent, plan, validate
            )

        def branches(ledger, record, alternatives, validate):
            if (
                not supported(owner)
                or ledger is not owner.ledger
                or record is not owner._record
            ):
                raise ReviewInputError("finding lifecycle branch producer changed")
            return _issue_checkpoint_branches(
                source, ledger, record, alternatives, validate
            )

        return method(
            owner, *args, _sizing_factories=(exact, projected, branches), **kwargs
        )

    return wrapped


def _issue_exact_documents(source, ledger, record, documents, indices, validate):
    if (
        source not in _EXACT_DOCUMENT_ISSUERS
        or not isinstance(documents, tuple)
        or not isinstance(indices, frozenset)
        or any(type(i) is not int or not 0 <= i < len(documents) for i in indices)
        or not callable(validate)
        or not validate()
    ):
        raise ReviewInputError("finding lifecycle exact checkpoints are unqualified")
    proof = _ExactCheckpointDocuments()
    _EXACT_DOCUMENTS[proof] = (
        source,
        ledger,
        record,
        canonical_bytes(record.to_dict()),
        tuple(canonical_bytes(value) for value in documents),
        indices,
        validate,
    )
    return proof


def _consume_exact_documents(proof, ledger, record, documents):
    if type(proof) is not _ExactCheckpointDocuments:
        raise ReviewInputError("finding lifecycle exact checkpoints are unissued")
    issued = _EXACT_DOCUMENTS.pop(proof, None)
    if (
        issued is None
        or issued[0] not in _EXACT_DOCUMENT_ISSUERS
        or issued[1] is not ledger
        or issued[2] is not record
        or issued[3] != canonical_bytes(record.to_dict())
        or issued[4] != tuple(canonical_bytes(value) for value in documents)
        or not issued[6]()
    ):
        raise ReviewInputError("finding lifecycle exact checkpoints changed or reused")
    return issued[5]


class _CheckpointBranches:
    """Mutually exclusive source-owned checkpoint sequences, sizing only."""


_CHECKPOINT_BRANCHES: WeakKeyDictionary = WeakKeyDictionary()


def _issue_checkpoint_branches(source, ledger, record, alternatives, validate):
    if (
        source not in _EXACT_DOCUMENT_ISSUERS
        or not isinstance(alternatives, tuple)
        or not 1 <= len(alternatives) <= 64
        or not callable(validate)
        or not validate()
    ):
        raise ReviewInputError("finding lifecycle branches are unqualified")
    encoded = []
    for documents, indices in alternatives:
        if (
            not isinstance(documents, tuple)
            or not 1 <= len(documents) <= 64
            or any(not isinstance(value, Mapping) for value in documents)
            or not isinstance(indices, frozenset)
            or any(type(i) is not int or not 0 <= i < len(documents) for i in indices)
        ):
            raise ReviewInputError("finding lifecycle branch shape is invalid")
        encoded.append((tuple(canonical_bytes(value) for value in documents), indices))
    proof = _CheckpointBranches()
    _CHECKPOINT_BRANCHES[proof] = (
        source,
        ledger,
        record,
        canonical_bytes(record.to_dict()),
        tuple(encoded),
        validate,
    )
    return proof


def _consume_checkpoint_branches(proof, ledger, record, documents):
    if type(proof) is not _CheckpointBranches:
        raise ReviewInputError("finding lifecycle branches are unissued")
    issued = _CHECKPOINT_BRANCHES.pop(proof, None)
    if (
        issued is None
        or issued[0] not in _EXACT_DOCUMENT_ISSUERS
        or issued[1] is not ledger
        or issued[2] is not record
        or issued[3] != canonical_bytes(record.to_dict())
        or issued[4][0][0] != tuple(canonical_bytes(value) for value in documents)
        or not issued[5]()
    ):
        raise ReviewInputError("finding lifecycle branches changed or reused")
    return tuple(
        (tuple(json.loads(value) for value in values), indices)
        for values, indices in issued[4]
    )


class _ProjectedRootSizing:
    """A detached prospective envelope; never read or selected as authority."""


_PROJECTED_ROOTS: WeakKeyDictionary = WeakKeyDictionary()


def _issue_projected_root(
    source, ledger, record, preview, staged_parts, intent, plan, validate
):
    if (
        source not in _EXACT_DOCUMENT_ISSUERS
        or preview.identity() != record.identity()
        or not isinstance(staged_parts, tuple)
        or any(not isinstance(value, bytes) for value in staged_parts)
        or not isinstance(intent, str)
        or _SHA.fullmatch(intent) is None
        or not callable(validate)
        or not validate()
    ):
        raise ReviewInputError("finding lifecycle projected root is unqualified")
    # Closed canonical payloads from the core dry-run are sizing only.
    for raw in staged_parts:
        if canonical_bytes(json.loads(raw)) != raw:
            raise ReviewInputError("finding lifecycle projected part is not canonical")
    proof = _ProjectedRootSizing()
    _PROJECTED_ROOTS[proof] = (
        source,
        ledger,
        record,
        canonical_bytes(record.to_dict()),
        preview,
        canonical_bytes(preview.to_dict()),
        staged_parts,
        intent,
        plan,
        validate,
    )
    return proof


def _consume_projected_root(proof, ledger, record):
    if type(proof) is not _ProjectedRootSizing:
        raise ReviewInputError("finding lifecycle projected root is unissued")
    issued = _PROJECTED_ROOTS.pop(proof, None)
    if (
        issued is None
        or issued[0] not in _EXACT_DOCUMENT_ISSUERS
        or issued[1] is not ledger
        or issued[2] is not record
        or issued[3] != canonical_bytes(record.to_dict())
        or issued[5] != canonical_bytes(issued[4].to_dict())
        or not issued[9]()
    ):
        raise ReviewInputError("finding lifecycle projected root changed or reused")
    return issued[4], issued[6]


def _refine_checkpoint_pricing(
    ledger, record, checkpoints, *, added_parts, added_bytes
):
    pricer = _qualified_checkpoint_pricer(ledger)
    if pricer is None:
        return None
    counts = tuple(checkpoint["parts"] for checkpoint in checkpoints)
    proof = pricer(
        record,
        added_parts=added_parts,
        added_bytes=added_bytes,
        checkpoint_parts=counts,
    )
    if proof is None:
        return None
    issued = _CHECKPOINT_PRICING.get(proof)
    if issued is not None and issued[9] in {
        "quarantine-staging-v1",
        "quarantine-inline-staging-v1",
    }:
        raise ReviewInputError(
            "finding lifecycle staging proof cannot price future checkpoints"
        )
    capacity, prices = _consume_checkpoint_pricing(proof, ledger, record, counts)
    for checkpoint, price in zip(checkpoints, prices):
        # ApiRun's inline path does not use the selected-parts capability.
        # Its existing conservative price is deliberately unchanged.
        if checkpoint.get("source_pricing", True) and (
            checkpoint["parts"] or checkpoint.get("inline_source_plain", False)
        ):
            checkpoint["conditional_calls"] = price
    return capacity


def _remaining_staging_price(
    ledger, record, parts, *, envelope, binding, item_count, allowance
):
    pricer = _qualified_checkpoint_pricer(ledger)
    if pricer is None:
        return None
    target = {
        "binding": binding,
        "document_sha256": hashlib.sha256(canonical_bytes(envelope)).hexdigest(),
        "parts": tuple(
            hashlib.sha256(canonical_bytes(part)).hexdigest() for part in parts
        ),
        "item_count": item_count,
        "manifest_allowance": allowance,
    }
    if not parts:
        manifest = inline_manifest(envelope, binding=binding, item_count=item_count)
        if manifest is None or len(canonical_bytes(manifest)) > allowance:
            return None
        target["inline_manifest"] = manifest
    proof = pricer(
        record,
        added_parts=len(parts),
        added_bytes=0,
        checkpoint_parts=(len(parts),),
        checkpoint_stage=True,
        checkpoint_target=target,
    )
    if proof is None:
        return None
    issued = _CHECKPOINT_PRICING.get(proof)
    if issued is None or issued[9] != (
        "quarantine-staging-v1" if parts else "quarantine-inline-staging-v1"
    ):
        raise ReviewInputError(
            "finding lifecycle remaining stage pricing is unsupported"
        )
    _, prices = _consume_checkpoint_pricing(
        proof, ledger, record, (len(parts),), staging_target=target
    )
    return prices[0]


def _inline_checkpoint_read_free(record):
    """Closed prospective root shape whose inline writer has no evidence reads."""
    reference = record.finding_lifecycle
    if isinstance(reference, Mapping):
        manifest = reference.get("manifest")
        if (
            isinstance(manifest, Mapping)
            and manifest.get("encoding") == INLINE_ENCODING
        ):
            envelope = manifest.get("document")
            packed = (
                envelope.get("lifecycle") if isinstance(envelope, Mapping) else None
            )
            if (
                isinstance(packed, Mapping)
                and packed.get("schema_version") == "finding-lifecycle-packed-v4"
            ):
                # An inline outer carrier does not make its external dictionary
                # read-free. Only a separately qualified writer may price its
                # actual no-restore CAS path; generic restoration must pay reads.
                return False
    if (
        record.assessment_queue is not None
        or record.historical_assessments is not None
        or record.quarantine_lineage is not None
    ):
        return False
    history = record.convergence_history
    baseline = history.get("baseline") if isinstance(history, Mapping) else None
    if (
        isinstance(baseline, Mapping)
        and baseline.get("encoding") == "partitioned-json-v1"
    ):
        return False
    if record.disposition_snapshot is not None:
        from .disposition_snapshot import normalize_snapshot

        snapshot = normalize_snapshot(record.disposition_snapshot)
        if (
            snapshot is None
            or snapshot["schema_version"] != "1.0"
            or snapshot["state_manifest"]["encoding"] != INLINE_ENCODING
        ):
            return False
    return True


def _remote_inline_plan(ledger: Any, record: Any) -> Mapping[str, object] | None:
    preflight = getattr(ledger, "preflight_finding_lifecycle_inline", None)
    if not callable(preflight):
        return None
    plan = preflight(record)
    if plan is None:
        return None
    if isinstance(plan, Mapping) and "checkpoint_pricing" in plan:
        proof = plan["checkpoint_pricing"]
        issued = (
            _CHECKPOINT_PRICING.get(proof)
            if type(proof) is _CheckpointPricing
            else None
        )
        _, prices = _consume_checkpoint_pricing(proof, ledger, record, (0,))
        assert issued is not None
        pages = issued[8][0]
        plan = {
            key: value for key, value in plan.items() if key != "checkpoint_pricing"
        }
        # Plain pricing includes its separately supplied initial observation.
        # Quarantine preparation is pure: its complete census is still unpaid.
        plan["conditional_calls"] = prices[0] - (
            0 if issued[9] == "quarantine-inline-v1" else 1
        )
        if issued[9] not in {
            "plain-v1",
            "plain-trust-inline-v1",
            "quarantine-inline-v1",
        }:
            raise ReviewInputError(
                "finding lifecycle inline checkpoint profile is unsupported"
            )
        plan["post_mutator_calls"] = (
            5
            if issued[9] == "quarantine-inline-v1"
            else 3 + 2 * (pages - 1) + int(issued[9] == "plain-trust-inline-v1")
        )
        if (
            set(plan)
            != {"conditional_calls", "post_mutator_calls", "page_size", "sealed"}
            or type(plan["sealed"]) is not bool
            or type(plan["page_size"]) is not int
            or not 1 <= plan["page_size"] <= 100
        ):
            raise ReviewInputError("finding lifecycle adapter inline proof is invalid")
        return plan
    if (
        not isinstance(plan, Mapping)
        or set(plan)
        != {"conditional_calls", "post_mutator_calls", "page_size", "sealed"}
        or plan["conditional_calls"] != 4
        or plan["post_mutator_calls"] != 3
        or type(plan["page_size"]) is not int
        or not 1 <= plan["page_size"] <= 100
        or type(plan["sealed"]) is not bool
    ):
        raise ReviewInputError("finding lifecycle adapter inline proof is invalid")
    return plan


def require_accepted_operation(
    record: Any,
    operation_id: str,
    *,
    base_sha: str,
    head_sha: str,
    policy_digest: str,
    configuration_digest: str,
) -> None:
    """Bind updates to existing transaction or head-bound reserved round authority."""
    previous = normalize_reference(record.finding_lifecycle)
    transaction = record.transaction
    if transaction is not None:
        if operation_id != transaction.transaction_id or (
            base_sha,
            head_sha,
            policy_digest,
            configuration_digest,
        ) != (
            transaction.base_sha,
            transaction.head_sha,
            transaction.policy_digest,
            transaction.configuration_digest,
        ):
            raise ReviewInputError("finding lifecycle accepted operation conflicts")
    elif record.reservation_id is not None:
        from .session import session_reservation_id

        owner = record.reservation_owner
        owner_head = owner.get("head_sha") if isinstance(owner, Mapping) else None
        standard = session_reservation_id(
            repository=record.repository,
            pull_request=record.pull_request,
            head_sha=head_sha,
            kind="publish",
        )
        if (
            operation_id != record.reservation_id
            or record.reserved_slot not in {"initial", "verification"}
            or record.failed_attempts_head_sha != head_sha
            or (owner_head != head_sha and record.reservation_id != standard)
        ):
            raise ReviewInputError(
                "finding lifecycle accepted reserved operation conflicts"
            )
    elif (
        previous is None
        or operation_id != previous["operation_id"]
        or any(
            previous["manifest"]["binding"][key] != value
            for key, value in (
                ("base_sha", base_sha),
                ("head_sha", head_sha),
                ("policy_digest", policy_digest),
                ("configuration_digest", configuration_digest),
            )
        )
    ):
        raise ReviewInputError("finding lifecycle accepted operation is missing")


def _require_retained_snapshot_capacity(record, *, reference=None):
    """Reprice the retained decision authority against this exact future root."""
    from .session import disposition_snapshot_capacity

    if record.disposition_snapshot is None:
        return
    projected = (
        record if reference is None else record.evolve(finding_lifecycle=reference)
    )
    if len(
        canonical_bytes(projected.disposition_snapshot)
    ) > disposition_snapshot_capacity(projected):
        raise ReviewInputError(
            "finding lifecycle retained snapshot joint capacity exceeded"
        )


def _source_semantic_history_width(binding):
    """Sizing only for the closed ApiRunAccounting action producer, not all imports.

    That producer emits SHA-256 operation keys, an ISO UTC timestamp with at
    most six fractional digits, and bounded semantic snapshot counters. Imported
    histories have a wider validator and keep their own exact capacity checks.
    No value returned here may be published or treated as an observation.
    """
    snapshot: dict[str, Any] = {
        key: "f" * 64
        for key in (
            "inventory_sha256",
            "unresolved_sha256",
            "enumerated_sha256",
            "reviewed_sha256",
            "uncertain_sha256",
            "pending_sha256",
            "receipts_sha256",
        )
    }
    snapshot.update(
        {
            key: 100000
            for key in (
                "unresolved_count",
                "reviewed_count",
                "uncertain_count",
                "pending_count",
            )
        }
    )
    snapshot.update(
        {
            key: False
            for key in (
                "review_complete",
                "enumeration_complete",
                "fully_reviewed",
                "unknown_trust_effect",
            )
        }
    )
    snapshot.update(base_sha="f" * 40, head_sha="f" * 40)
    rows = [
        {
            "operation_key": str(index) * 64,
            "purpose": "command",
            "source": {
                "kind": "review_comment",
                "id": (1 << 53) - 1,
                "updated_at": "9999-12-31T23:59:59.999999Z",
                "body_sha256": "f" * 64,
            },
            "snapshot": snapshot,
            "made_progress": False,
            "target_base_sha": "f" * 40,
            "target_head_sha": "f" * 40,
        }
        for index in (1, 2)
    ]
    prior = {
        "schema_version": "accepted-semantic-history-v1",
        "repository_id": binding["repository_id"],
        "pull_request": binding["pull_request"],
        "streak": 2,
        "records": rows,
    }
    # Source unavailability reasons are ASCII diagnostics. Retaining a full
    # prior pair is wider than either a qualified pair or absent-history hold.
    return {
        "schema_version": "accepted-semantic-history-v1",
        "qualification": "unavailable",
        "reason": "x" * 128,
        "prior": prior,
    }


def _prepare_lifecycle(
    ledger: Any,
    record: Any,
    *,
    document: Mapping[str, object],
    operation_id: str,
    base_sha: str,
    head_sha: str,
    policy_digest: str,
    configuration_digest: str,
    item_count: int,
    reserved_tail_calls: int = 0,
) -> tuple[dict[str, Any], dict[str, Any], tuple[dict[str, object], ...], int, Any]:
    _sha(operation_id, "operation")
    if not isinstance(document, Mapping):
        raise ReviewInputError("finding lifecycle document is invalid")
    if type(reserved_tail_calls) is not int or reserved_tail_calls < 0:
        raise ReviewInputError("finding lifecycle tail is invalid")
    from .publication_intent_fusion import prospective_transaction

    fused_transaction = prospective_transaction(ledger, record)
    fusion_fields = (
        {} if fused_transaction is None else {"transaction": fused_transaction}
    )
    previous = normalize_reference(record.finding_lifecycle)
    require_accepted_operation(
        record,
        operation_id,
        base_sha=base_sha,
        head_sha=head_sha,
        policy_digest=policy_digest,
        configuration_digest=configuration_digest,
    )
    binding = validate_binding(
        {
            "repository": record.repository,
            "repository_id": record.repository_id,
            "pull_request": record.pull_request,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "policy_digest": policy_digest,
            "configuration_digest": configuration_digest,
            "generation": record.generation + 1,
            "producer": ledger.evidence_producer(),
            "purpose": "finding-lifecycle",
            "schema_version": "1.0",
        }
    )
    reference: dict[str, Any] = {
        "schema_version": REFERENCE_VERSION,
        "operation_id": operation_id,
        "previous_root_sha256": record.record_sha256,
        "previous_snapshot_sha256": previous["manifest"]["sha256"]
        if previous
        else None,
    }
    if previous is not None and "accepted_semantic_history" in previous:
        reference["schema_version"] = HISTORY_REFERENCE_VERSION
        reference["accepted_semantic_history"] = previous["accepted_semantic_history"]
    envelope = {
        "schema_version": DOCUMENT_VERSION,
        "lifecycle": json.loads(canonical_bytes(document)),
    }
    parts = partition_evidence(envelope, binding=binding, item_count=item_count)
    prospective = evidence_manifest(
        envelope,
        binding=binding,
        item_count=item_count,
        parts=parts,
        storage_ids=tuple(f"{i:064d}" for i in range(len(parts))),
    )
    # The complete future root is measured before the first part write.
    record.evolve(
        generation=record.generation + 1,
        **fusion_fields,
        finding_lifecycle={**reference, "manifest": prospective},
    )
    capacity_reference = reference
    caller = getattr(ledger, "caller", None)
    witness = getattr(caller, "_accepted_semantic_capacity", None)
    if witness is not None:
        from .api_work import ApiRunAccounting

        action = ApiRunAccounting.from_document(record.api_run_accounting).action
        owned = getattr(caller, "_effect_record", None)
        owned_action = (
            ApiRunAccounting.from_document(owned.api_run_accounting).action
            if owned is not None
            else None
        )
        if (
            not isinstance(witness, dict)
            or action is None
            or witness.get("intent_sha256")
            != hashlib.sha256(canonical_bytes(action["intent"])).hexdigest()
            or witness.get("operation_id") != operation_id
            or not isinstance(witness.get("initial_root"), str)
            or _SHA.fullmatch(witness["initial_root"]) is None
            or owned is None
            or owned_action is None
            or owned_action["intent"] != action["intent"]
            or owned.identity() != record.identity()
            or getattr(caller, "_root", None) != owned.record_sha256
        ):
            raise ReviewInputError(
                "finding lifecycle prospective history context changed"
            )
        capacity_reference = {
            **reference,
            "schema_version": HISTORY_REFERENCE_VERSION,
            "accepted_semantic_history": witness["history"],
        }
        normalize_reference({**capacity_reference, "manifest": prospective})
    metadata_widths = [
        len(canonical_bytes({**reference, "manifest": {}})),
        len(canonical_bytes({**capacity_reference, "manifest": {}})),
    ]
    if record.api_run_accounting is not None:
        from .api_work import ApiRunAccounting

        # Qualify the closed producer's root. This only narrows an inline
        # optimization; wider imported histories still retain exact checks.
        ApiRunAccounting.from_document(record.api_run_accounting)
        metadata_widths.append(
            len(
                canonical_bytes(
                    {
                        **reference,
                        "schema_version": HISTORY_REFERENCE_VERSION,
                        "accepted_semantic_history": _source_semantic_history_width(
                            binding
                        ),
                        "manifest": {},
                    }
                )
            )
        )
    allowance = MAX_REFERENCE_BYTES - max(metadata_widths) + 2

    inlined = inline_manifest(envelope, binding=binding, item_count=item_count)
    selected = (
        inlined
        if inlined is not None and len(canonical_bytes(inlined)) <= allowance
        else prospective
    )

    def prepare_candidate(manifest):
        candidate = record.evolve(
            generation=record.generation + 1,
            **fusion_fields,
            finding_lifecycle={**reference, "manifest": manifest},
        )
        _require_retained_snapshot_capacity(candidate)
        if capacity_reference is not reference:
            # Sizing data is never published as a semantic observation.
            from .session import MAX_SESSION_RECORD_BYTES

            normalize_reference({**capacity_reference, "manifest": manifest})
            raw = candidate.to_dict()
            raw["finding_lifecycle"] = {**capacity_reference, "manifest": manifest}
            _require_retained_snapshot_capacity(
                candidate, reference=raw["finding_lifecycle"]
            )
            require_prospective_terminal_history_capacity(raw)
            if len(canonical_bytes(raw)) > MAX_SESSION_RECORD_BYTES:
                raise ReviewInputError(
                    "finding lifecycle prospective history root capacity exceeded"
                )
        return candidate

    try:
        candidate = prepare_candidate(selected)
    except ReviewInputError as error:
        # Inline is an optimization, not permission to consume the terminal
        # root's reserved space. The supported partition representation may
        # have a smaller root even when inline fits the reference's 8 KiB cap.
        capacity_errors = {
            "session record exceeds the configured size limit",
            "finding lifecycle prospective terminal history root capacity exceeded",
            "finding lifecycle prospective history root capacity exceeded",
            "finding lifecycle retained snapshot joint capacity exceeded",
        }
        if (
            inlined is None
            or selected is not inlined
            or str(error) not in capacity_errors
        ):
            raise
        partition_allowance = min(allowance, len(canonical_bytes(inlined)) - 1)
        if len(canonical_bytes(prospective)) > partition_allowance:
            raise
        candidate = prepare_candidate(prospective)
        # The ordinary stage must make the same choice, not opportunistically
        # inline again after the pure preflight priced real parts and readbacks.
        allowance = partition_allowance
    return reference, envelope, parts, allowance, candidate


def stage_finding_lifecycle(
    ledger: Any,
    record: Any,
    *,
    document: Mapping[str, object],
    operation_id: str,
    base_sha: str,
    head_sha: str,
    policy_digest: str,
    configuration_digest: str,
    item_count: int,
    reserved_tail_calls: int = 0,
) -> Any:
    """Stage/read back all bytes within the existing conditional owner boundary.

    Returns the new root. Never dispatches a native review, changes a reservation
    or resets accounting. Partial staging is nonauthoritative retained evidence.
    The caller retains ownership of full effect/tail funding and its admission.
    """
    from .publication_intent_fusion import prospective_transaction, require_stage

    require_stage(ledger, record, document)
    fused_transaction = prospective_transaction(ledger, record)
    fusion_fields = (
        {} if fused_transaction is None else {"transaction": fused_transaction}
    )
    reference, envelope, parts, allowance, prospective_record = _prepare_lifecycle(
        ledger,
        record,
        document=document,
        operation_id=operation_id,
        base_sha=base_sha,
        head_sha=head_sha,
        policy_digest=policy_digest,
        configuration_digest=configuration_digest,
        item_count=item_count,
        reserved_tail_calls=reserved_tail_calls,
    )
    # The prepared envelope is bound independently to this next root generation.
    binding = validate_binding(
        {
            "repository": record.repository,
            "repository_id": record.repository_id,
            "pull_request": record.pull_request,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "policy_digest": policy_digest,
            "configuration_digest": configuration_digest,
            "generation": record.generation + 1,
            "producer": ledger.evidence_producer(),
            "purpose": "finding-lifecycle",
            "schema_version": "1.0",
        }
    )

    def mutate(current: Any) -> Any:
        if current.record_sha256 != record.record_sha256:
            raise ReviewInputError("finding lifecycle session changed before staging")
        budget = getattr(ledger, "evidence_budget", None)
        if budget is not None:
            inlined = inline_manifest(envelope, binding=binding, item_count=item_count)
            part_calls = (
                0
                if inlined is not None and len(canonical_bytes(inlined)) <= allowance
                else 8 * len(parts)
            )
            local_calls = (
                _inline_local_commit_calls(ledger, current) if part_calls == 0 else None
            )
            commit_calls = 16 if local_calls is None else local_calls
            remote_plan = (
                _remote_inline_plan(ledger, current) if part_calls == 0 else None
            )
            if remote_plan is not None and remote_plan["sealed"]:
                post_calls = remote_plan["post_mutator_calls"]
                assert type(post_calls) is int
                commit_calls = post_calls
            remaining_price = _remaining_staging_price(
                ledger,
                current,
                parts if part_calls else (),
                envelope=envelope,
                binding=binding,
                item_count=item_count,
                allowance=allowance,
            )
            budget.preflight(
                (
                    part_calls + commit_calls
                    if remaining_price is None
                    else remaining_price
                )
                + reserved_tail_calls
            )
        manifest = ledger.stage_evidence(
            current.identity(),
            binding=binding,
            document=envelope,
            item_count=item_count,
            max_manifest_bytes=allowance,
        )
        actual_transaction = prospective_transaction(ledger, current)
        if actual_transaction != fused_transaction:
            raise ReviewInputError("finding lifecycle fused transition changed")
        candidate = current.evolve(
            generation=current.generation + 1,
            **fusion_fields,
            finding_lifecycle={**reference, "manifest": manifest},
        )
        return candidate

    # stage_evidence already read back every part, and replace independently
    # fences and reads back the current root. Repeating both complete reads here
    # consumes another operation tail without adding an independent authority.
    if prospective_record.finding_lifecycle["manifest"][
        "encoding"
    ] == INLINE_ENCODING and (
        _inline_local_commit_calls(ledger, record) is not None
        or _remote_inline_plan(ledger, record) is not None
    ):
        kwargs = (
            {"reserved_tail_calls": reserved_tail_calls}
            if _qualified_checkpoint_pricer(ledger) is not None
            else {}
        )
        return ledger.replace_finding_lifecycle_inline(
            record.identity(), mutate, record=record, **kwargs
        )
    scoped_parts = getattr(ledger, "replace_finding_lifecycle_parts", None)
    if prospective_record.finding_lifecycle["manifest"][
        "encoding"
    ] != INLINE_ENCODING and callable(scoped_parts):
        kwargs = (
            {"reserved_tail_calls": reserved_tail_calls}
            if _qualified_checkpoint_pricer(ledger) is not None
            else {}
        )
        return scoped_parts(
            record.identity(),
            mutate,
            record=record,
            parts=len(parts),
            retained_bytes=sum(len(canonical_bytes(part)) + 128 for part in parts),
            **kwargs,
        )
    return ledger.replace(record.identity(), mutate)


def preflight_finding_lifecycle(
    ledger: Any,
    record: Any,
    *,
    document: Mapping[str, object],
    future_documents: tuple[Mapping[str, object], ...] = (),
    operation_id: str,
    base_sha: str,
    head_sha: str,
    policy_digest: str,
    configuration_digest: str,
    item_count: int,
    reserved_tail_calls: int = 0,
    exact_documents: object | None = None,
    projected_root: object | None = None,
    branch_documents: object | None = None,
) -> dict[str, Any]:
    """Measure pending and worst-width future checkpoints before native dispatch.

    This read-only capacity plan is not authority, a dispatch ticket, or a new
    budget. The caller supplies normalized complete future-state upper bounds,
    including its typed native receipts, and must reject an actual state outside
    those bounds. The ordinary stage still rechecks current root and all bounds.
    Inline plans need no storage census. Partition plans charge an existing
    bounded local read-only census, or require the hosted adapter's explicit
    preflight seam; they never silently expand reader or storage limits.
    """
    from .bounded_evidence import (
        MAX_PARTITION_BYTES,
        MAX_PARTS,
        MAX_STORED_PART_BYTES,
        MAX_STORED_PARTS,
        PACKED_CHUNK_BYTES,
        PART_FRAMING_RESERVE,
    )
    from .session import LocalSessionLedger

    if (
        not isinstance(future_documents, tuple)
        or len(future_documents) > 63
        or any(not isinstance(value, Mapping) for value in future_documents)
    ):
        raise ReviewInputError("finding lifecycle future checkpoints are invalid")
    if type(reserved_tail_calls) is not int or reserved_tail_calls < 0:
        raise ReviewInputError("finding lifecycle tail is invalid")
    budget = getattr(ledger, "evidence_budget", None)
    if budget is None:
        raise ReviewInputError("finding lifecycle original evidence budget is required")
    documents = (document, *future_documents)
    exact_indices = (
        _consume_exact_documents(exact_documents, ledger, record, documents)
        if exact_documents is not None
        else frozenset()
    )
    alternatives = (
        _consume_checkpoint_branches(branch_documents, ledger, record, documents)
        if branch_documents is not None
        else ((documents, exact_indices),)
    )
    if branch_documents is not None and exact_documents is not None:
        raise ReviewInputError("finding lifecycle branch proofs cannot be mixed")
    if branch_documents is not None and type(ledger) is LocalSessionLedger:
        raise ReviewInputError("finding lifecycle branch producer must be hosted")
    projected_record, staged_parts = (
        _consume_projected_root(projected_root, ledger, record)
        if projected_root is not None
        else (record, ())
    )
    has_projected_root = projected_root is not None

    def measure(documents, exact_indices):
        checkpoints: list[dict[str, Any]] = []
        current = projected_record
        added_parts = added_bytes = calls = 0
        if has_projected_root:
            added_parts = len(staged_parts)
            added_bytes = sum(len(raw) + PART_FRAMING_RESERVE for raw in staged_parts)
            # This is the separate disposition/root stage, not the optimized
            # finding-carrier writer. Keep its conservative physical cost.
            calls = 17 + 8 * added_parts
            checkpoints.append(
                {
                    "lifecycle_bytes": 0,
                    "envelope_bytes": 0,
                    "reference_bytes": 0,
                    "root_bytes": len(canonical_bytes(current.to_dict())),
                    "inline": not staged_parts,
                    "parts": added_parts,
                    "retained_bytes": added_bytes,
                    "conditional_calls": calls,
                    "source_pricing": False,
                }
            )
        for checkpoint_index, value in enumerate(documents):
            reference, envelope, actual_parts, allowance, candidate = (
                _prepare_lifecycle(
                    ledger,
                    current,
                    document=value,
                    operation_id=operation_id,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    policy_digest=policy_digest,
                    configuration_digest=configuration_digest,
                    item_count=item_count,
                    reserved_tail_calls=reserved_tail_calls,
                )
            )
            selected = candidate.finding_lifecycle["manifest"]
            is_inline = selected["encoding"] == INLINE_ENCODING
            envelope_bytes = len(canonical_bytes(envelope))
            count = retained_bytes = 0
            if not is_inline:
                # zlib's documented conservative compressBound, not optimistic
                # compression of repeated placeholder IDs. Every future part is
                # reserved as new; no deduplication or removable-history assumption.
                packed_bound = (
                    envelope_bytes
                    + (envelope_bytes >> 12)
                    + (envelope_bytes >> 14)
                    + (envelope_bytes >> 25)
                    + 13
                )
                count = (packed_bound + PACKED_CHUNK_BYTES - 1) // PACKED_CHUNK_BYTES
                if count > MAX_PARTS:
                    raise ReviewInputError(
                        "finding lifecycle future part capacity exceeded"
                    )
                binding = selected["binding"]
                maximum_parts = tuple(
                    {
                        "encoding": "evidence-part-v1",
                        "binding_sha256": "f" * 64,
                        "inventory_sha256": "f" * 64,
                        "index": index,
                        "count": count,
                        "data": "A"
                        * (
                            4
                            * (
                                (
                                    min(
                                        PACKED_CHUNK_BYTES,
                                        packed_bound - index * PACKED_CHUNK_BYTES,
                                    )
                                    + 2
                                )
                                // 3
                            )
                        ),
                    }
                    for index in range(count)
                )
                if checkpoint_index in exact_indices:
                    # Only the source-issued, sequence-enforced pending document is
                    # exact. The compressed envelope contains no binding, root,
                    # generation, clock or item_count. Part binding/inventory hashes
                    # remain fixed-width, so fresh authority does not change lengths.
                    # Unknown receipt and stop/final variants keep compressBound.
                    count = len(actual_parts)
                    sizes = [len(canonical_bytes(part)) for part in actual_parts]
                else:
                    sizes = [len(canonical_bytes(part)) for part in maximum_parts]
                retained_bytes = sum(size + PART_FRAMING_RESERVE for size in sizes)
                if retained_bytes > MAX_PARTITION_BYTES:
                    raise ReviewInputError(
                        "finding lifecycle future framed byte capacity exceeded"
                    )
                # Manifest sizing includes maximal storage IDs and real maximum
                # number of entries; checks complete prospective root allocation.
                maximum_manifest = {
                    "encoding": "partitioned-json-v1",
                    "binding": binding,
                    "item_count": item_count,
                    "decoded_bytes": envelope_bytes,
                    "sha256": "f" * 64,
                    "encoded_bytes": retained_bytes,
                    "parts": [
                        {
                            "storage_id": f"{index:064d}",
                            "bytes": size,
                            "sha256": "f" * 64,
                        }
                        for index, size in enumerate(sizes)
                    ],
                }
                if len(canonical_bytes(maximum_manifest)) > allowance:
                    raise ReviewInputError(
                        "finding lifecycle future reference capacity exceeded"
                    )
                candidate = current.evolve(
                    generation=current.generation + 1,
                    transaction=candidate.transaction,
                    finding_lifecycle={**reference, "manifest": maximum_manifest},
                )
                _require_retained_snapshot_capacity(candidate)
                witness = getattr(
                    getattr(ledger, "caller", None), "_accepted_semantic_capacity", None
                )
                if witness is not None:
                    from .session import MAX_SESSION_RECORD_BYTES

                    prospective_reference = normalize_reference(
                        {
                            **reference,
                            "manifest": maximum_manifest,
                            "schema_version": HISTORY_REFERENCE_VERSION,
                            "accepted_semantic_history": witness["history"],
                        }
                    )
                    raw = candidate.to_dict()
                    raw["finding_lifecycle"] = prospective_reference
                    _require_retained_snapshot_capacity(
                        candidate, reference=prospective_reference
                    )
                    require_prospective_terminal_history_capacity(raw)
                    if len(canonical_bytes(raw)) > MAX_SESSION_RECORD_BYTES:
                        raise ReviewInputError(
                            "finding lifecycle future history root capacity exceeded"
                        )
            local_calls = (
                _inline_local_commit_calls(ledger, current) if is_inline else None
            )
            checkpoint_calls = 4 if local_calls is not None else 17 + 8 * count
            # Later roots are prospective, not yet in the frontier. The initial
            # adapter proof seals the same non-expanding full-page envelope for each
            # actual stage; every stage revalidates its own current-root proof.
            remote_plan = (
                _remote_inline_plan(ledger, record)
                if is_inline and branch_documents is None
                else None
            )
            if remote_plan is not None:
                source_calls = remote_plan["conditional_calls"]
                assert type(source_calls) is int
                checkpoint_calls = source_calls
            calls += checkpoint_calls
            added_parts += count
            added_bytes += retained_bytes
            checkpoints.append(
                {
                    "lifecycle_bytes": len(canonical_bytes(value)),
                    "envelope_bytes": envelope_bytes,
                    "reference_bytes": len(
                        canonical_bytes(candidate.finding_lifecycle)
                    ),
                    "root_bytes": len(canonical_bytes(candidate.to_dict())),
                    "inline": is_inline,
                    "parts": count,
                    "retained_bytes": retained_bytes,
                    "conditional_calls": checkpoint_calls,
                    "inline_source_plain": is_inline
                    and _inline_checkpoint_read_free(current),
                }
            )
            current = candidate
        return checkpoints, added_parts, added_bytes, calls

    measured = [measure(values, indices) for values, indices in alternatives]
    # One observed frontier covers the independent maxima of all reachable
    # branches. The quoted vector must have exactly the maximum part count.
    checkpoints, added_parts, _, calls = max(
        measured, key=lambda measured: (measured[1], measured[3])
    )
    added_bytes = max(measured[2] for measured in measured)
    if added_parts > MAX_STORED_PARTS or added_bytes > MAX_STORED_PART_BYTES:
        raise ReviewInputError(
            "finding lifecycle whole future retention capacity exceeded"
        )
    remote_observed = None
    if type(ledger) is not LocalSessionLedger:
        remote_observed = _refine_checkpoint_pricing(
            ledger,
            record,
            checkpoints,
            added_parts=added_parts,
            added_bytes=added_bytes,
        )
        if remote_observed is not None:
            if branch_documents is not None:
                prices: dict[tuple[Any, Any, Any], int] = {}
                for checkpoint in checkpoints:
                    shape = (
                        checkpoint["parts"],
                        checkpoint.get("source_pricing", True),
                        checkpoint.get("inline_source_plain", False),
                    )
                    prices[shape] = max(
                        prices.get(shape, 0), checkpoint["conditional_calls"]
                    )
                for sequence, _, _, _ in measured:
                    for checkpoint in sequence:
                        shape = (
                            checkpoint["parts"],
                            checkpoint.get("source_pricing", True),
                            checkpoint.get("inline_source_plain", False),
                        )
                        if shape in prices:
                            checkpoint["conditional_calls"] = prices[shape]
            calls = sum(checkpoint["conditional_calls"] for checkpoint in checkpoints)
    if branch_documents is not None:
        calls = max(
            sum(checkpoint["conditional_calls"] for checkpoint in sequence)
            for sequence, _, _, _ in measured
        )
    budget.preflight(calls + reserved_tail_calls)
    if added_parts:
        if type(ledger) is LocalSessionLedger:
            import os

            # Do not call _retained_evidence_parts: its creation of the part
            # directory is a write. This census performs no filesystem mutation.
            directory = ledger._part_directory(record.identity())
            budget.consume()  # bounded enumeration, including absent directory
            stored_count = stored_bytes = 0
            try:
                entries = os.scandir(directory)
            except FileNotFoundError:
                entries = None
            except OSError as exc:
                raise ReviewInputError(
                    "finding lifecycle retention census failed"
                ) from exc
            if entries is not None:
                try:
                    with entries:
                        for entry in entries:
                            budget.consume()  # actual metadata/stat work
                            if (
                                stored_count >= MAX_STORED_PARTS
                                or entry.is_symlink()
                                or not entry.is_file()
                                or not re.fullmatch(
                                    r"(?:[a-f0-9]{64}|\.staging-[A-Za-z0-9_-]{1,64})",
                                    entry.name,
                                )
                            ):
                                raise ReviewInputError(
                                    "finding lifecycle retention census is unsupported"
                                )
                            stored_count += 1
                            stored_bytes += entry.stat().st_size
                except OSError as exc:
                    raise ReviewInputError(
                        "finding lifecycle retention changed during census"
                    ) from exc
            if (
                stored_count + added_parts > MAX_STORED_PARTS
                or stored_bytes + added_bytes > MAX_STORED_PART_BYTES
            ):
                raise ReviewInputError(
                    "finding lifecycle whole future retention capacity exceeded"
                )
            # Each later partition stage re-enumerates retained metadata. Count
            # all existing and all conservatively newly retained earlier parts,
            # rather than hiding that work in the fixed per-checkpoint tail.
            preceding_parts = 0
            for checkpoint in checkpoints:
                if not checkpoint["inline"]:
                    extra_calls = stored_count + preceding_parts
                    checkpoint["conditional_calls"] += extra_calls
                    calls += extra_calls
                    preceding_parts += checkpoint["parts"]
        else:
            preflight = getattr(ledger, "preflight_finding_lifecycle_retention", None)
            if not callable(preflight):
                raise ReviewInputError(
                    "finding lifecycle hosted partition preflight is unsupported"
                )
            observed = remote_observed
            if observed is None:
                observed = preflight(
                    record.identity(),
                    added_parts=added_parts,
                    added_bytes=added_bytes,
                    checkpoint_parts=tuple(cp["parts"] for cp in checkpoints),
                )
            if (
                not isinstance(observed, Mapping)
                or set(observed)
                != {
                    "additional_calls",
                    "comment_allocations",
                    "retained_parts",
                    "retained_bytes",
                }
                or any(
                    type(value) is not int or value < 0 for value in observed.values()
                )
            ):
                raise ReviewInputError(
                    "finding lifecycle hosted capacity proof is invalid"
                )
            if (
                observed["comment_allocations"] > 200
                or observed["retained_parts"] > MAX_STORED_PARTS
                or observed["retained_bytes"] > MAX_STORED_PART_BYTES
            ):
                raise ReviewInputError("finding lifecycle hosted capacity exceeded")
            calls += observed["additional_calls"]
    budget.preflight(calls + reserved_tail_calls)
    return {
        "schema_version": "finding-lifecycle-preflight-v1",
        "root_sha256": record.record_sha256,
        "checkpoints": checkpoints,
        "added_parts": added_parts,
        "added_bytes": added_bytes,
        "conditional_calls": calls,
        "reserved_tail_calls": reserved_tail_calls,
    }


def is_history_metadata_amendment(before, after) -> bool:
    if (
        not isinstance(before, Mapping)
        or not isinstance(after, Mapping)
        or after.get("schema_version") != HISTORY_REFERENCE_VERSION
    ):
        return False
    left, right = dict(before), dict(after)
    for value in (left, right):
        value.pop("schema_version", None)
        value.pop("accepted_semantic_history", None)
    return left == right


def inline_terminal_action_history(record, *, accounting=None, candidate_history=None):
    """Decode exact committed inline result data, never a supplied summary proof."""
    from .api_work import ApiRunAccounting

    state = (
        ApiRunAccounting.from_document(record.api_run_accounting)
        if accounting is None
        else accounting
    )
    action = state.action
    if action is None or action["phase"] != "terminal":
        raise ReviewInputError(
            "accepted history amendment requires terminal action result"
        )
    manifest = action["result"]
    if manifest.get("encoding") != INLINE_ENCODING:
        raise ReviewInputError(
            "accepted history partitioned action result proof unavailable"
        )
    reference = normalize_reference(record.finding_lifecycle)
    if reference is None:
        raise ReviewInputError("accepted history amendment requires finding reference")
    intent = action["intent"]
    intent_hash = hashlib.sha256(canonical_bytes(intent)).hexdigest()
    binding = {
        "repository": record.repository,
        "repository_id": record.repository_id,
        "pull_request": record.pull_request,
        "base_sha": state.configuration.commit,
        "head_sha": intent["head_sha"],
        "policy_digest": intent_hash,
        "configuration_digest": state.configuration.digest,
        "generation": intent["owner"]["generation"],
        "producer": reference["manifest"]["binding"]["producer"],
        "purpose": "evidence",
        "schema_version": "1.0",
    }
    carrier = read_inline_evidence(manifest, expected_binding=binding)
    from .action_result_codec import decode_action_result_carrier

    history = (
        candidate_history
        if candidate_history is not None
        else reference.get("accepted_semantic_history")
    )
    document = decode_action_result_carrier(carrier, history=history)
    if (
        document["intent_sha256"] != intent_hash
        or document["outcome"] != action["outcome"]
    ):
        raise ReviewInputError("accepted history action result binding conflicts")
    return document["result"].get("accepted_semantic_history")


def reference_with_terminal_action_history(
    record, *, accounting=None, candidate_history=None
):
    if record.finding_lifecycle is None:
        return None
    history = inline_terminal_action_history(
        record, accounting=accounting, candidate_history=candidate_history
    )
    if history is None:
        return record.finding_lifecycle
    return normalize_reference(
        {
            **record.finding_lifecycle,
            "schema_version": HISTORY_REFERENCE_VERSION,
            "accepted_semantic_history": history,
        }
    )


def require_terminal_history_amendment(before, after) -> None:
    old, new = (
        normalize_reference(before.finding_lifecycle),
        normalize_reference(after.finding_lifecycle),
    )
    if not is_history_metadata_amendment(old, new):
        raise ReviewInputError("accepted history amendment changed finding evidence")
    from .api_work import ApiRunAccounting

    previous = ApiRunAccounting.from_document(before.api_run_accounting)
    following = ApiRunAccounting.from_document(after.api_run_accounting)
    if previous.identity != following.identity:
        source = before
    else:
        previous.require_successor(following)
        if (
            previous.action is None
            or following.action is None
            or previous.action["intent"] != following.action["intent"]
        ):
            raise ReviewInputError("accepted history amendment changed action intent")
        # A terminal predecessor must already carry its own complete evidence.
        # Only an actual pending→terminal transition may introduce the new
        # reference data alongside the newly retained result commitment.
        source = before if previous.action["phase"] == "terminal" else after
    history = inline_terminal_action_history(source)
    if (
        history is None
        or new is None
        or canonical_bytes(history) != canonical_bytes(new["accepted_semantic_history"])
    ):
        raise ReviewInputError(
            "accepted history amendment differs from terminal result"
        )


def _validate_accepted_history_metadata(value, binding, *, depth=0):
    def invalid():
        raise ReviewInputError("finding lifecycle accepted history is invalid")

    if (
        depth > 1
        or not isinstance(value, dict)
        or value.get("schema_version") != "accepted-semantic-history-v1"
    ):
        invalid()
    if value.get("qualification") == "unavailable":
        if (
            set(value)
            not in (
                {"schema_version", "qualification", "reason"},
                {"schema_version", "qualification", "reason", "prior"},
            )
            or type(value["reason"]) is not str
            or not 1 <= len(value["reason"]) <= 128
        ):
            invalid()
        if value.get("prior") is not None:
            _validate_accepted_history_metadata(
                value["prior"], binding, depth=depth + 1
            )
        return
    if (
        set(value)
        != {"schema_version", "repository_id", "pull_request", "streak", "records"}
        or type(value["repository_id"]) is not int
        or type(value["pull_request"]) is not int
        or value["repository_id"] != binding["repository_id"]
        or value["pull_request"] != binding["pull_request"]
        or type(value["streak"]) is not int
        or not 0 <= value["streak"] <= 2
        or not isinstance(value["records"], list)
        or not 1 <= len(value["records"]) <= 2
    ):
        invalid()
    seen = set()
    hashes = {
        "inventory_sha256",
        "unresolved_sha256",
        "enumerated_sha256",
        "reviewed_sha256",
        "uncertain_sha256",
        "pending_sha256",
        "receipts_sha256",
    }
    counts = {"unresolved_count", "reviewed_count", "uncertain_count", "pending_count"}
    flags = {
        "review_complete",
        "enumeration_complete",
        "fully_reviewed",
        "unknown_trust_effect",
    }
    for item in value["records"]:
        if (
            not isinstance(item, dict)
            or set(item)
            != {
                "operation_key",
                "purpose",
                "source",
                "snapshot",
                "made_progress",
                "target_base_sha",
                "target_head_sha",
            }
            or type(item["operation_key"]) is not str
            or not 1 <= len(item["operation_key"]) <= 256
            or item["operation_key"] in seen
            or item["purpose"] not in {"command", "reply"}
            or type(item["made_progress"]) is not bool
        ):
            invalid()
        seen.add(item["operation_key"])
        for key in ("target_base_sha", "target_head_sha"):
            if item[key] is not None and (
                type(item[key]) is not str
                or re.fullmatch(r"[a-f0-9]{40}", item[key]) is None
            ):
                invalid()
        source, snapshot = item["source"], item["snapshot"]
        if (
            not isinstance(source, dict)
            or set(source) != {"kind", "id", "updated_at", "body_sha256"}
            or source["kind"] not in {"issue_comment", "review_comment"}
            or type(source["id"]) is not int
            or not 0 < source["id"] <= (1 << 53) - 1
            or type(source["updated_at"]) is not str
            or not 1 <= len(source["updated_at"]) <= 128
            or type(source["body_sha256"]) is not str
            or _SHA.fullmatch(source["body_sha256"]) is None
        ):
            invalid()
        if not isinstance(snapshot, dict) or set(
            snapshot
        ) != hashes | counts | flags | {"base_sha", "head_sha"}:
            invalid()
        for key in hashes | {"base_sha", "head_sha"}:
            length = 40 if key in {"base_sha", "head_sha"} else 64
            if (
                type(snapshot[key]) is not str
                or re.fullmatch("[a-f0-9]{%d}" % length, snapshot[key]) is None
            ):
                invalid()
        if any(
            type(snapshot[key]) is not int or not 0 <= snapshot[key] <= 100000
            for key in counts
        ) or any(type(snapshot[key]) is not bool for key in flags):
            invalid()
    trailing = 0
    for item in reversed(value["records"]):
        if item["made_progress"]:
            break
        trailing += 1
    if value["streak"] != trailing:
        invalid()


def prospective_terminal_history_bytes(document):
    """Pure upper-width terminal projection; no projected data is an authority."""
    from .api_work import ACTION_NOOP_REASONS
    from .bounded_evidence import MAX_MANIFEST_BYTES

    raw = json.loads(canonical_bytes(document))
    action = raw.get("api_run_accounting", {}).get("action")
    if isinstance(action, dict):
        action.update(
            phase="terminal",
            execution_started=True,
            result="x" * (MAX_MANIFEST_BYTES - 2),
            outcome={
                "kind": "explicit_noop",
                "reason": max(ACTION_NOOP_REASONS, key=len),
            },
        )
    raw["generation"] += 1
    return len(canonical_bytes(raw))


def require_prospective_terminal_history_capacity(document):
    from .session import MAX_SESSION_RECORD_BYTES

    if prospective_terminal_history_bytes(document) > MAX_SESSION_RECORD_BYTES:
        raise ReviewInputError(
            "finding lifecycle prospective terminal history root capacity exceeded"
        )


from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
