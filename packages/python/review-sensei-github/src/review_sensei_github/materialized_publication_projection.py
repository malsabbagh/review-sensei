"""Inert whole-branch sizing from an executing original publication frame.

Nothing returned here is registered as capacity or an execution capability.
Unavailable source context is not an empty branch, and a maximum document is not
permission to skip an independently reachable checkpoint or native read.
"""

from __future__ import annotations

import copy
from dataclasses import fields, is_dataclass
from typing import Any

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.source_registry import require_instance_source, require_module_seal


def _data(value, *, _seen=None, _depth=0):
    if type(value) in (str, bytes, int, float, bool, type(None)):
        return
    if _depth > 64:
        raise ReviewInputError("materialized projection source graph is too deep")
    seen = set() if _seen is None else _seen
    if id(value) in seen:
        return
    if len(seen) >= 20_000:
        raise ReviewInputError("materialized projection source graph is too large")
    seen.add(id(value))
    if type(value) is dict:
        children = (*value.keys(), *value.values())
    elif type(value) in (tuple, list, set, frozenset):
        children = value
    elif not isinstance(value, type) and is_dataclass(value):
        try:
            require_instance_source(value, methods=())
        except ReviewInputError as error:
            raise ReviewInputError(
                f"materialized projection class is unavailable: {type(value).__module__}.{type(value).__qualname__}"
            ) from error
        if any(
            name in vars(value)
            for name in ("__copy__", "__deepcopy__", "__reduce__", "__reduce_ex__")
        ):
            raise ReviewInputError("materialized projection copy source changed")
        children = tuple(getattr(value, field.name) for field in fields(value))
    else:
        raise ReviewInputError("materialized projection source data is unsupported")
    for child in children:
        _data(child, _seen=seen, _depth=_depth + 1)


def _future_accounting(state, block_ids):
    """Detached maximum-width pending receipts in the two existing tranches.

    V2 confirmed receipts are compact pairs; a pending receipt retains both
    field names. Thus retaining every future request as pending dominates every
    completed/pending mixture and every shorter prefix, including the at-most
    one comma boundary difference and decreasing undispatched digit widths.
    Existing unknown requests are never removed or reclassified here.
    """
    from review_sensei import api_work
    from review_sensei.api_work import (
        ACCOUNTING_VERSION,
        MAX_API_REQUEST_LIMIT,
        PendingApiRequest,
    )

    from .guarded_pending_publication import _original_accounting_graph

    require_module_seal(api_work)
    _original_accounting_graph(state, models=True)
    if (
        state.version != ACCOUNTING_VERSION
        or type(block_ids) is not tuple
        or len(block_ids) != 2
        or len(set(block_ids)) != 2
    ):
        raise ReviewInputError(
            "materialized accounting projection scope is unsupported"
        )
    selected = tuple(
        next((b for b in state.blocks if b.block_id == key), None) for key in block_ids
    )
    if any(block is None or block.issued_by != state.owner for block in selected):
        raise ReviewInputError("materialized accounting projection owner changed")
    counts = tuple(block.undispatched for block in selected if block is not None)
    if sum(counts) > state.configuration.limit:
        raise ReviewInputError(
            "materialized accounting projection exceeds original funding"
        )
    before = canonical_bytes(state.to_document())
    used = {item.request_id for item in state.completed}
    used.update(item.request_id for block in state.blocks for item in block.pending)
    projected = state
    serial = 0
    for key, count in zip(block_ids, counts, strict=True):
        for _ in range(count):
            # PendingApiRequest's original validated identifier bound is 128
            # ASCII bytes; its deadline bound is the closed 53-bit integer.
            # Sizing labels are never returned as requests or capabilities.
            while True:
                identifier = f"p{serial:0127d}"
                serial += 1
                if identifier not in used:
                    break
            used.add(identifier)
            projected = projected.dispatch(
                key, PendingApiRequest(identifier, MAX_API_REQUEST_LIMIT)
            )
    if canonical_bytes(state.to_document()) != before:
        raise ReviewInputError("materialized accounting projection changed source")
    return projected.to_document(), counts


def _measure_sequence(journal, documents, item_count, accounting_upper=None):
    """Pure original renderer/retention preparation, without ledger preflight."""
    from review_sensei import bounded_evidence, finding_lifecycle_store
    from review_sensei.bounded_evidence import (
        INLINE_ENCODING,
        PACKED_CHUNK_BYTES,
        PART_FRAMING_RESERVE,
    )

    require_module_seal(bounded_evidence)
    require_module_seal(finding_lifecycle_store)
    current = journal._record
    measurements = []
    for document in documents:
        reference, envelope, _, allowance, candidate = (
            finding_lifecycle_store._prepare_lifecycle(
                journal.ledger,
                current,
                document=document,
                operation_id=journal.operation_id,
                base_sha=journal.base_sha,
                head_sha=journal.head_sha,
                policy_digest=journal.policy_digest,
                configuration_digest=journal.configuration_digest,
                item_count=item_count,
            )
        )
        raw_bytes = len(canonical_bytes(envelope))
        selected = candidate.finding_lifecycle["manifest"]
        inline = selected["encoding"] == INLINE_ENCODING
        parts, retained = 0, 0
        if not inline:
            packed = (
                raw_bytes
                + (raw_bytes >> 12)
                + (raw_bytes >> 14)
                + (raw_bytes >> 25)
                + 13
            )
            parts = (packed + PACKED_CHUNK_BYTES - 1) // PACKED_CHUNK_BYTES
            if parts > bounded_evidence.MAX_PARTS:
                raise ReviewInputError("materialized projection part capacity exceeded")
            sizes = []
            for index in range(parts):
                maximum = {
                    "encoding": "evidence-part-v1",
                    "binding_sha256": "f" * 64,
                    "inventory_sha256": "f" * 64,
                    "index": index,
                    "count": parts,
                    "data": "A"
                    * (
                        4
                        * (
                            (
                                min(
                                    PACKED_CHUNK_BYTES,
                                    packed - index * PACKED_CHUNK_BYTES,
                                )
                                + 2
                            )
                            // 3
                        )
                    ),
                }
                sizes.append(len(canonical_bytes(maximum)))
            retained = sum(size + PART_FRAMING_RESERVE for size in sizes)
            manifest = {
                "encoding": "partitioned-json-v1",
                "binding": selected["binding"],
                "item_count": item_count,
                "decoded_bytes": raw_bytes,
                "sha256": "f" * 64,
                "encoded_bytes": retained,
                "parts": [
                    {"storage_id": f"{index:064d}", "bytes": size, "sha256": "f" * 64}
                    for index, size in enumerate(sizes)
                ],
            }
            if (
                len(canonical_bytes(manifest)) > allowance
                or retained > bounded_evidence.MAX_PARTITION_BYTES
            ):
                raise ReviewInputError(
                    "materialized projection complete manifest exceeds capacity"
                )
            candidate = current.evolve(
                generation=current.generation + 1,
                transaction=candidate.transaction,
                finding_lifecycle={**reference, "manifest": manifest},
            )
            finding_lifecycle_store._require_retained_snapshot_capacity(candidate)
            # The original preparer has authenticated this exact capacity
            # witness against the journal intent/current owned root. Retain its
            # prospective-history checks for our larger compressBound manifest,
            # not merely the selected concrete compression above.
            witness = getattr(
                journal.ledger.caller, "_accepted_semantic_capacity", None
            )
            if witness is not None:
                from review_sensei.session import MAX_SESSION_RECORD_BYTES

                prospective_reference = {
                    **reference,
                    "schema_version": finding_lifecycle_store.HISTORY_REFERENCE_VERSION,
                    "accepted_semantic_history": witness["history"],
                    "manifest": manifest,
                }
                finding_lifecycle_store.normalize_reference(prospective_reference)
                finding_lifecycle_store._require_retained_snapshot_capacity(
                    candidate, reference=prospective_reference
                )
                prospective_record = candidate.to_dict()
                prospective_record["finding_lifecycle"] = prospective_reference
                finding_lifecycle_store.require_prospective_terminal_history_capacity(
                    prospective_record
                )
                if len(canonical_bytes(prospective_record)) > MAX_SESSION_RECORD_BYTES:
                    raise ReviewInputError(
                        "materialized projection prospective history root capacity exceeded"
                    )
        accounting_root_bytes = None
        accounting_root_fits = None
        if accounting_upper is not None:
            from review_sensei.session import MAX_GENERATION, MAX_SESSION_RECORD_BYTES

            growth = candidate.to_dict()
            growth["api_run_accounting"] = accounting_upper
            growth["generation"] = MAX_GENERATION
            growth["completed_initial_reviews"] += 1
            growth["completed_verification_rounds"] += 1
            witness = getattr(
                journal.ledger.caller, "_accepted_semantic_capacity", None
            )
            if witness is not None:
                growth["finding_lifecycle"] = {
                    **growth["finding_lifecycle"],
                    "schema_version": finding_lifecycle_store.HISTORY_REFERENCE_VERSION,
                    "accepted_semantic_history": witness["history"],
                }
            accounting_root_bytes = max(
                len(canonical_bytes(growth)),
                finding_lifecycle_store.prospective_terminal_history_bytes(growth),
            )
            accounting_root_fits = accounting_root_bytes <= MAX_SESSION_RECORD_BYTES
        measurements.append(
            {
                "inline": inline,
                "envelope_bytes": raw_bytes,
                "root_bytes": len(canonical_bytes(candidate.to_dict())),
                "generation": candidate.generation,
                "reference_bytes": len(canonical_bytes(candidate.finding_lifecycle)),
                "future_parts": parts,
                "framed_part_bytes": retained,
                "accounting_root_bytes": accounting_root_bytes,
                "accounting_root_fits": accounting_root_fits,
            }
        )
        current = candidate
    return tuple(measurements)


def project(journal, source):
    """Bound source request/stop documents; never admit or dispatch anything."""
    from review_sensei import context, models, presentation

    from . import approval, checks, finding_lifecycle, publication
    from . import ordinary_publication_plan as plan
    from .finding_lifecycle import _digest, _require_effect_scope
    from .guarded_pending_publication import _original_methods, _terminal_reservation

    for module in (
        plan,
        context,
        models,
        presentation,
        approval,
        checks,
        finding_lifecycle,
        publication,
    ):
        require_module_seal(module)
    publisher = journal.reviewer
    for owner in (
        journal,
        journal.ledger,
        publisher,
        publisher.checks,
        publisher.finalizer,
    ):
        _original_methods(owner)
    required = (
        "result",
        "preflight_review",
        "prospective_eligibility",
        "marker",
        "planned_check_outcome",
        "effective_policy",
        "complete_inventory",
        "human_inventory",
        "window_non_inline_blocking",
        "planned_window_state",
        "planned_drift_state",
        "convergence_policy",
        "auto_approval",
        "continuation_review",
        "retained_eligibility",
        "grouped_publication",
    )
    if any(name not in source for name in required):
        raise ReviewInputError("materialized projection source locals are incomplete")
    if (
        source["preflight_review"] is None
        or source["complete_inventory"] is not True
        or source["continuation_review"] is not None
        or source["retained_eligibility"] is not None
        or source["grouped_publication"]
    ):
        raise ReviewInputError("materialized projection source route is unsupported")
    _data(tuple(source[name] for name in required))
    if any(
        name in vars(journal)
        for name in ("__copy__", "__deepcopy__", "__reduce__", "__reduce_ex__")
    ):
        raise ReviewInputError("materialized projection journal copy source changed")
    _data((journal._record, journal.effects, journal._planned, journal._input_identity))
    for owner, names in (
        (
            journal,
            (
                "_finalizer_capacity_states",
                "_accepted_semantic_history",
                "_review_update_capacity_states",
                "_batch_body_updates",
            ),
        ),
        (
            publisher,
            (
                "_unknown_effect_holds",
                "_native_finding_context",
                "_native_finding_receipt_records",
                "_drift_states",
                "_drift_inputs",
                "_drift_concern_history",
                "_active_window_states",
                "_active_window_inputs",
            ),
        ),
    ):
        for name in names:
            _data(getattr(owner, name, None))
    terminal = _terminal_reservation(journal)
    caller = journal.ledger.caller
    accounting_upper, accounting_counts = _future_accounting(
        caller.accounting, (caller._block, caller._terminal_block)
    )
    before = (
        len(caller.observations),
        caller.usable,
        caller._root,
        canonical_bytes(journal._document()),
    )
    assembled = publisher._adjust_assembled_for_drift(
        publisher._adjust_assembled_for_window(
            source["preflight_review"],
            source["planned_window_state"],
            convergence_policy=source["convergence_policy"],
            auto_approval=source["auto_approval"],
        ),
        source["planned_drift_state"],
        convergence_policy=source["convergence_policy"],
        auto_approval=source["auto_approval"],
    )
    branches: list[tuple[bool, tuple[Any, ...]]] = []
    for check_published in (False, True):
        projected = plan.current_request_branches(
            publisher,
            journal,
            result=source["result"],
            assembled=assembled,
            eligibility=source["prospective_eligibility"],
            marker=source["marker"],
            outcome=source["planned_check_outcome"],
            policy=source["effective_policy"],
            complete_inventory=source["complete_inventory"],
            preserve_explanations=source["human_inventory"] is not None,
            window_non_inline_blocking=source["window_non_inline_blocking"],
            check_published=check_published,
        )
        branches.extend((check_published, branch) for branch in projected)
    summaries: list[dict[str, Any]] = []
    projected_prefixes: dict[bytes, tuple[int, int, Any, Any, Any]] = {}
    measured_sequences: dict[Any, tuple[dict[str, Any], ...]] = {}
    # This cache is detached sizing only. Exact source root/binding/document
    # bytes are keys; it supplies neither current bodies nor any issued plan.
    source_binding = (
        canonical_bytes(journal._record.to_dict()),
        journal.operation_id,
        journal.base_sha,
        journal.head_sha,
        journal.policy_digest,
        journal.configuration_digest,
    )
    for check_published, branch in branches:
        shadow = copy.copy(journal)
        shadow.effects = []
        pending_sizes, stop_sizes = [], []
        pending_documents, prefix_measurements = [], []
        keys = set()
        for method, path, body, precondition in branch:
            request = plan._capacity_request_identity(method, path, body)
            key = _digest({"method": method, "path": path, "request": request})
            if key in keys:
                raise ReviewInputError("materialized projection cannot assume replay")
            keys.add(key)
            effect = {
                "method": method,
                "path": path,
                "request": request,
                "request_sha256": key,
                "status": "pending",
                "response": None,
            }
            if precondition is not None:
                effect["check_update_precondition"] = precondition
            shadow.effects.append(effect)
            _require_effect_scope(
                shadow._document(), journal.identity, journal.head_sha
            )
            prefix_key = canonical_bytes(shadow.effects)
            cached = projected_prefixes.get(prefix_key)
            if cached is None:
                pending, stopped, confirmed = shadow._preflight_effect(
                    capacity_only=True
                )
                pending_bytes = len(canonical_bytes(pending))
                stop_bytes = max(pending_bytes, len(canonical_bytes(stopped)))
                cached = (
                    pending_bytes,
                    stop_bytes,
                    copy.deepcopy(confirmed),
                    pending,
                    stopped,
                )
                projected_prefixes[prefix_key] = cached
            pending_sizes.append(cached[0])
            stop_sizes.append(cached[1])
            shadow.effects = copy.deepcopy(cached[2])
            pending_documents.append(cached[3])
            documents = (*pending_documents, cached[4])
            item_count = len(shadow.effects) + 1
            sequence_key = (
                source_binding,
                item_count,
                tuple(canonical_bytes(value) for value in documents),
            )
            measurement = measured_sequences.get(sequence_key)
            if measurement is None:
                measurement = _measure_sequence(
                    journal, documents, item_count, accounting_upper
                )
                measured_sequences[sequence_key] = measurement
            prefix_measurements.append(measurement)
        summaries.append(
            {
                "check_published_frame": check_published,
                "effects": len(branch),
                "pending_document_bytes": tuple(pending_sizes),
                "stop_document_bytes": tuple(stop_sizes),
                "maximum_finish_document_bytes": max(stop_sizes),
                "pending_and_stop_sequences": tuple(prefix_measurements),
            }
        )
    if before != (
        len(caller.observations),
        caller.usable,
        caller._root,
        canonical_bytes(journal._document()),
    ):
        raise ReviewInputError("materialized projection changed physical source state")
    return {
        "authority": False,
        "source": "executing-original-publication",
        "branches": tuple(summaries),
        "ordinary_remaining": before[1],
        "protected_terminal_remaining": terminal.undispatched,
        "accounting_future_requests": accounting_counts,
        "accounting_maximum_bytes": len(canonical_bytes(accounting_upper)),
        "invocation_price_issued": False,
        "complete_funding_proof": False,
    }


from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
