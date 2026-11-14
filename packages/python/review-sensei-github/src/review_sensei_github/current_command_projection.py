"""Inert complete current-command roots from original lifecycle preparation.

These records include synthetic future carrier identifiers and must never be
read, staged, or consumed as writer authority. The current issuer authenticates
all inputs and separately owns execution/root-transition custody.
"""

import copy
import sys

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.source_registry import require_instance_source, require_module_seal


def pending_accounting(
    state, *, sequence, ordinary_id, terminal_id, ordinary, terminal
):
    """Preserve every old liability; spend only detached copies of existing slots."""
    from review_sensei import api_work
    from review_sensei.api_work import MAX_API_REQUEST_LIMIT, PendingApiRequest

    from .guarded_pending_publication import _original_accounting_graph

    require_module_seal(sys.modules[__name__])
    require_module_seal(api_work)
    _original_accounting_graph(state, models=True)
    if (
        any(
            type(value) is not int or value < 0
            for value in (sequence, ordinary, terminal)
        )
        or ordinary_id == terminal_id
    ):
        raise ReviewInputError("current pending projection namespace is unsupported")
    original = canonical_bytes(state.to_document())
    used = {item.request_id for item in state.completed}
    used.update(item.request_id for block in state.blocks for item in block.pending)
    projected = state
    for block_id, count in ((ordinary_id, ordinary), (terminal_id, terminal)):
        block = next((b for b in state.blocks if b.block_id == block_id), None)
        if (
            block is None
            or block.issued_by != state.owner
            or count > block.undispatched
        ):
            raise ReviewInputError("current projection cannot borrow or refill funding")
        for _ in range(count):
            sequence += 1
            identifier = f"c-{state.owner.generation:x}-{sequence:x}"
            if identifier in used:
                raise ReviewInputError(
                    "current future namespace collides with old liability"
                )
            used.add(identifier)
            projected = projected.dispatch(
                block_id, PendingApiRequest(identifier, MAX_API_REQUEST_LIMIT)
            )
    if canonical_bytes(state.to_document()) != original:
        raise ReviewInputError("current projection changed original accounting")
    return projected.to_document()


def _maximum_candidate(journal, current, document, *, item_count, tail, exact):
    from review_sensei import bounded_evidence as evidence
    from review_sensei import finding_lifecycle_store as store

    reference, envelope, actual_parts, allowance, candidate = store._prepare_lifecycle(
        journal.ledger,
        current,
        document=document,
        operation_id=journal.operation_id,
        base_sha=journal.base_sha,
        head_sha=journal.head_sha,
        policy_digest=journal.policy_digest,
        configuration_digest=journal.configuration_digest,
        item_count=item_count,
        reserved_tail_calls=tail,
    )
    selected = candidate.finding_lifecycle["manifest"]
    if selected["encoding"] == evidence.INLINE_ENCODING:
        if not exact:
            # A compressible synthetic future document is not a raw upper
            # bound for another inline document with the same logical widths.
            raise ReviewInputError(
                "current nonexact inline lifecycle root bound is unqualified"
            )
        return candidate, 0, 0
    raw = len(canonical_bytes(envelope))
    packed = raw + (raw >> 12) + (raw >> 14) + (raw >> 25) + 13
    count = (packed + evidence.PACKED_CHUNK_BYTES - 1) // evidence.PACKED_CHUNK_BYTES
    if count > evidence.MAX_PARTS:
        raise ReviewInputError("current future lifecycle part bound exceeded")
    if exact:
        sizes = [len(canonical_bytes(part)) for part in actual_parts]
        count = len(sizes)
    else:
        sizes = [
            len(
                canonical_bytes(
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
                                        evidence.PACKED_CHUNK_BYTES,
                                        packed - index * evidence.PACKED_CHUNK_BYTES,
                                    )
                                    + 2
                                )
                                // 3
                            )
                        ),
                    }
                )
            )
            for index in range(count)
        ]
    retained = sum(size + evidence.PART_FRAMING_RESERVE for size in sizes)
    maximum = {
        "encoding": evidence.PARTITION_ENCODING,
        "binding": selected["binding"],
        "item_count": item_count,
        "decoded_bytes": raw,
        "sha256": "f" * 64,
        "encoded_bytes": retained,
        "parts": [
            {"storage_id": f"{index:064d}", "bytes": size, "sha256": "f" * 64}
            for index, size in enumerate(sizes)
        ],
    }
    if (
        len(canonical_bytes(maximum)) > allowance
        or retained > evidence.MAX_PARTITION_BYTES
    ):
        raise ReviewInputError("current future lifecycle wire bound exceeded")
    candidate = current.evolve(
        generation=current.generation + 1,
        transaction=candidate.transaction,
        finding_lifecycle={**reference, "manifest": maximum},
    )
    store._require_retained_snapshot_capacity(candidate)
    return candidate, count, retained


def project(
    journal,
    *,
    preview_record,
    details,
    alternatives,
    item_count,
    quote,
    accounting_upper,
    result_manifest,
):
    """Return complete rendered candidate sizes, never an executable proposal."""
    from review_sensei import bounded_evidence as evidence
    from review_sensei import finding_lifecycle_store as store
    from review_sensei import session
    from review_sensei.api_work import ACTION_NOOP_REASONS
    from review_sensei.terminal_representability import estimate_session_record_bytes

    for module in (sys.modules[__name__], evidence, store, session):
        require_module_seal(module)
    ledger = journal.ledger
    require_instance_source(ledger, methods=("_render_comment",))
    if getattr(ledger, "_controlled_restart", None) is not None:
        raise ReviewInputError(
            "current root projection cannot include a restart renderer"
        )
    witness = getattr(ledger.caller, "_accepted_semantic_capacity", None)
    if witness is None or preview_record.identity() != journal.identity:
        raise ReviewInputError(
            "current root projection requires original prospective history"
        )
    before = canonical_bytes(journal._record.to_dict())
    rows = []
    branch_parts = []
    branch_bytes = []
    for documents, exact_indices in alternatives:
        current = preview_record
        candidates = [current]
        parts = len(details["staged_parts"])
        retained = sum(
            len(raw) + evidence.PART_FRAMING_RESERVE for raw in details["staged_parts"]
        )
        for index, document in enumerate(documents):
            current, count, framed = _maximum_candidate(
                journal,
                current,
                document,
                item_count=item_count,
                tail=quote["reserved_tail_calls"],
                exact=index in exact_indices,
            )
            parts += count
            retained += framed
            candidates.append(current)
        branch_parts.append(parts)
        branch_bytes.append(retained)
        for candidate in candidates:
            reference = store.normalize_reference(
                {
                    **candidate.finding_lifecycle,
                    "schema_version": store.HISTORY_REFERENCE_VERSION,
                    "accepted_semantic_history": witness["history"],
                }
            )
            for terminal in (False, True):
                accounting = copy.deepcopy(accounting_upper)
                if terminal:
                    accounting["action"].update(
                        phase="terminal",
                        execution_started=True,
                        result=result_manifest,
                        outcome={
                            "kind": "explicit_noop",
                            "reason": max(ACTION_NOOP_REASONS, key=len),
                        },
                    )
                projected = candidate.evolve(
                    generation=session.MAX_GENERATION,
                    api_run_accounting=accounting,
                    finding_lifecycle=reference,
                )
                body = ledger._render_comment(
                    repository_id=journal.identity.repository_id,
                    pull_request=journal.identity.pull_request,
                    record=projected,
                )
                root_bytes = estimate_session_record_bytes(projected)
                wire_bytes = len(body.encode())
                if (
                    root_bytes > session.MAX_SESSION_RECORD_BYTES
                    or wire_bytes > session.MAX_SESSION_COMMENT_BYTES
                ):
                    raise ReviewInputError(
                        "current complete future root or comment is unrepresentable"
                    )
                rows.append(
                    {
                        "terminal": terminal,
                        "record_bytes": root_bytes,
                        "comment_bytes": wire_bytes,
                        "parts": parts,
                    }
                )
    if (
        max(branch_parts) != quote["added_parts"]
        or max(branch_bytes) != quote["added_bytes"]
    ):
        raise ReviewInputError(
            "current candidate projection diverges from original whole-branch quote"
        )
    if canonical_bytes(journal._record.to_dict()) != before:
        raise ReviewInputError("current candidate projection changed original root")
    return tuple(rows)


from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
