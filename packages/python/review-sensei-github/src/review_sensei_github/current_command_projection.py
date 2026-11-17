"""Inert complete current-command roots from original lifecycle preparation.

These records include synthetic future carrier identifiers and must never be
read, staged, or consumed as writer authority. The current issuer authenticates
all inputs and separately owns execution/root-transition custody.
"""

import copy
import sys
from typing import Any

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


def terminal_pending_accounting(
    state,
    *,
    sequence,
    ordinary_id,
    terminal_id,
    ordinary,
    terminal,
    uncertain_ordinary=6,
    uncertain_terminal=None,
    deadline_ceiling=None,
):
    """Upper shape at terminal entry: ordinary action work is settled into completed."""
    from review_sensei import api_work
    from review_sensei.api_work import MAX_API_REQUEST_LIMIT, PendingApiRequest

    from .guarded_pending_publication import _original_accounting_graph

    require_module_seal(sys.modules[__name__])
    require_module_seal(api_work)
    _original_accounting_graph(state, models=True)
    if (
        any(
            type(value) is not int or value < 0
            for value in (sequence, ordinary, terminal, uncertain_ordinary)
        )
        or (
            uncertain_terminal is not None
            and (type(uncertain_terminal) is not int or uncertain_terminal < 0)
        )
        or (
            deadline_ceiling is not None
            and (
                type(deadline_ceiling) is not int
                or not 0 < deadline_ceiling <= MAX_API_REQUEST_LIMIT
            )
        )
        or ordinary_id == terminal_id
    ):
        raise ReviewInputError("current pending projection namespace is unsupported")
    effective_deadline = (
        MAX_API_REQUEST_LIMIT if deadline_ceiling is None else deadline_ceiling
    )
    original = canonical_bytes(state.to_document())
    used = {item.request_id for item in state.completed}
    used.update(item.request_id for block in state.blocks for item in block.pending)
    projected = state
    dispatched_ordinary = []
    dispatched_terminal = []
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
            request = PendingApiRequest(identifier, effective_deadline)
            projected = projected.dispatch(block_id, request)
            if block_id == ordinary_id:
                dispatched_ordinary.append(request)
            else:
                dispatched_terminal.append(request)
    confirm_ordinary = max(0, ordinary - uncertain_ordinary)
    for request in dispatched_ordinary[:confirm_ordinary]:
        projected = projected.confirm(request)
    if uncertain_terminal is not None:
        confirm_terminal = max(0, terminal - uncertain_terminal)
        for request in dispatched_terminal[:confirm_terminal]:
            projected = projected.confirm(request)
    if canonical_bytes(state.to_document()) != original:
        raise ReviewInputError("current projection changed original accounting")
    return projected.to_document()


def future_census_bound(plan, added_parts):
    """Read-only envelope for every known-ID refinement prefix.

    Exact echoed creates may become known while their original uncertainty
    slots remain. Price both occurrences, including the new known-ID metadata
    maximum, without modifying or installing any lifetime allocation plan.
    """
    from review_sensei.bounded_evidence import MAX_PART_BYTES, MAX_PARTS

    from . import packed_comment_census

    require_module_seal(sys.modules[__name__])
    require_module_seal(packed_comment_census)
    if (
        type(plan) is not packed_comment_census.CommentAllocationPlan
        or type(added_parts) is not int
        or not 0 <= added_parts <= MAX_PARTS
    ):
        raise ReviewInputError("current future census data is unsupported")
    before = (
        tuple(plan.known.items()),
        plan.unknown,
        tuple(plan.metadata_known.items()),
        plan.metadata_unknown,
    )
    envelope = packed_comment_census.CommentAllocationPlan(
        plan.known,
        (*plan.unknown, *((MAX_PART_BYTES + 512,) * added_parts)),
        metadata_known=plan.metadata_known,
        metadata_unknown=max(
            plan.metadata_unknown, packed_comment_census.NODE_METADATA_BYTES
        ),
    )
    after = (
        tuple(plan.known.items()),
        plan.unknown,
        tuple(plan.metadata_known.items()),
        plan.metadata_unknown,
    )
    if before != after:
        raise ReviewInputError("current future census changed allocation custody")
    return envelope.worst_read_calls()


def _maximum_candidate(
    journal,
    current,
    document,
    *,
    item_count,
    tail,
    exact,
    prior_settled_accounting=None,
):
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
        nonexact_packed_bound=(not exact),
    )
    selected = candidate.finding_lifecycle["manifest"]
    if selected["encoding"] == evidence.INLINE_ENCODING:
        if not exact:
            lifecycle = envelope.get("lifecycle")
            journal_payload = (
                lifecycle.get("journal") if isinstance(lifecycle, dict) else None
            )
            skeleton_bytes = (
                journal_payload.get("decoded_bytes")
                if isinstance(journal_payload, dict)
                else None
            )
            if (
                envelope.get("schema_version") != store.DOCUMENT_VERSION
                or not isinstance(lifecycle, dict)
                or lifecycle.get("schema_version") != "finding-lifecycle-packed-v3"
                or not isinstance(journal_payload, dict)
                or journal_payload.get("encoding") != "zlib-json-v1"
                or type(skeleton_bytes) is not int
                or skeleton_bytes <= 0
            ):
                raise ReviewInputError(
                    "current nonexact inline lifecycle root bound is unqualified"
                )
            packed_skeleton = (
                skeleton_bytes
                + (skeleton_bytes >> 12)
                + (skeleton_bytes >> 14)
                + (skeleton_bytes >> 25)
                + 13
            )
            maximum_envelope = {
                "schema_version": store.DOCUMENT_VERSION,
                "lifecycle": {
                    "schema_version": "finding-lifecycle-packed-v3",
                    "journal": {
                        "encoding": "zlib-json-v1",
                        "decoded_bytes": skeleton_bytes,
                        "sha256": "f" * 64,
                        "data": "A" * (4 * ((packed_skeleton + 2) // 3)),
                    },
                    "native_atoms": lifecycle["native_atoms"],
                },
            }
            maximum_inline = evidence.inline_manifest(
                maximum_envelope, binding=selected["binding"], item_count=item_count
            )
            if (
                maximum_inline is None
                or len(canonical_bytes(maximum_inline)) > allowance
            ):
                raise ReviewInputError(
                    "current nonexact inline lifecycle root bound is unqualified"
                )
            candidate = current.evolve(
                generation=current.generation + 1,
                transaction=candidate.transaction,
                finding_lifecycle={**reference, "manifest": maximum_inline},
            )
            store._require_retained_snapshot_capacity(candidate)
        return candidate, 0, 0, ()
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
    extra_candidates: tuple[Any, ...] = ()
    if not exact:
        try:
            reg_ref, reg_env, _, reg_allowance, reg_cand = store._prepare_lifecycle(
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
                nonexact_packed_bound=False,
            )
        except ReviewInputError:
            reg_cand = None
        if (
            reg_cand is not None
            and reg_cand.finding_lifecycle["manifest"]["encoding"]
            == evidence.INLINE_ENCODING
        ):
            lifecycle = reg_env.get("lifecycle")
            journal_payload = (
                lifecycle.get("journal") if isinstance(lifecycle, dict) else None
            )
            skeleton_bytes = (
                journal_payload.get("decoded_bytes")
                if isinstance(journal_payload, dict)
                else None
            )
            if (
                reg_env.get("schema_version") == store.DOCUMENT_VERSION
                and isinstance(lifecycle, dict)
                and lifecycle.get("schema_version") == "finding-lifecycle-packed-v3"
                and isinstance(journal_payload, dict)
                and journal_payload.get("encoding") == "zlib-json-v1"
                and type(skeleton_bytes) is int
                and skeleton_bytes > 0
            ):
                base_env: dict[str, Any] = {
                    "schema_version": store.DOCUMENT_VERSION,
                    "lifecycle": {
                        "schema_version": "finding-lifecycle-packed-v3",
                        "journal": {
                            "encoding": "zlib-json-v1",
                            "decoded_bytes": skeleton_bytes,
                            "sha256": "f" * 64,
                            "data": "",
                        },
                        "native_atoms": lifecycle["native_atoms"],
                    },
                }
                base_inline = evidence.inline_manifest(
                    base_env,
                    binding=selected["binding"],
                    item_count=item_count,
                )
                if base_inline is not None:
                    from review_sensei import session as core_session

                    lo = 0
                    hi = max(0, reg_allowance - len(canonical_bytes(base_inline)))
                    best_inline = None
                    witness = getattr(
                        journal.ledger.caller, "_accepted_semantic_capacity", None
                    )
                    while lo <= hi:
                        mid = (lo + hi) // 2
                        cand_env = {
                            **base_env,
                            "lifecycle": {
                                **base_env["lifecycle"],
                                "journal": {
                                    **base_env["lifecycle"]["journal"],
                                    "data": "A" * mid,
                                },
                            },
                        }
                        capped_inline = evidence.inline_manifest(
                            cand_env,
                            binding=selected["binding"],
                            item_count=item_count,
                        )
                        fits = (
                            capped_inline is not None
                            and len(canonical_bytes(capped_inline)) <= reg_allowance
                        )
                        if fits:
                            evolve_kwargs: dict[str, Any] = {
                                "generation": current.generation + 1,
                                "transaction": candidate.transaction,
                                "finding_lifecycle": {
                                    **reg_ref,
                                    "manifest": capped_inline,
                                },
                            }
                            if prior_settled_accounting is not None:
                                evolve_kwargs["api_run_accounting"] = (
                                    prior_settled_accounting
                                )
                            cand_rec = current.evolve(**evolve_kwargs)
                            try:
                                store._require_retained_snapshot_capacity(cand_rec)
                                if witness is not None:
                                    prosp_ref = store.normalize_reference(
                                        {
                                            **reg_ref,
                                            "schema_version": store.HISTORY_REFERENCE_VERSION,
                                            "accepted_semantic_history": witness[
                                                "history"
                                            ],
                                            "manifest": capped_inline,
                                        }
                                    )
                                    raw_doc = cand_rec.to_dict()
                                    raw_doc["finding_lifecycle"] = prosp_ref
                                    store._require_retained_snapshot_capacity(
                                        cand_rec, reference=prosp_ref
                                    )
                                    store.require_prospective_terminal_history_capacity(
                                        raw_doc
                                    )
                                    if (
                                        len(canonical_bytes(raw_doc))
                                        > core_session.MAX_SESSION_RECORD_BYTES
                                    ):
                                        fits = False
                            except ReviewInputError:
                                fits = False
                        if fits:
                            best_inline = capped_inline
                            lo = mid + 1
                        else:
                            hi = mid - 1
                    if best_inline is not None:
                        best_cand = current.evolve(
                            generation=current.generation + 1,
                            transaction=candidate.transaction,
                            finding_lifecycle={
                                **reg_ref,
                                "manifest": best_inline,
                            },
                        )
                        extra_candidates = (best_cand,)
    return candidate, count, retained, extra_candidates


def project(
    journal,
    *,
    preview_record,
    details,
    alternatives,
    item_count,
    quote,
    accounting_upper,
    terminal_accounting_upper=None,
    accounting_context=None,
    result_manifest,
):
    """Return complete rendered candidate sizes, never an executable proposal."""
    from review_sensei import bounded_evidence as evidence
    from review_sensei import finding_lifecycle_store as store
    from review_sensei import session
    from review_sensei.api_work import ACTION_NOOP_REASONS
    from review_sensei.terminal_representability import estimate_session_record_bytes

    from . import api_run_plan, quarantine_command_namespace

    for module in (
        sys.modules[__name__],
        evidence,
        store,
        session,
        api_run_plan,
        quarantine_command_namespace,
    ):
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
    if accounting_context is not None:
        (
            state,
            sequence,
            ordinary_id,
            terminal_id,
            ordinary,
            terminal_count,
            census,
            has_conditional_approval,
        ) = accounting_context
        deadline_ceiling = quarantine_command_namespace.MAX_COLD_DEADLINE_UNIX_MS
        ordinary_settled = terminal_pending_accounting(
            state,
            sequence=sequence,
            ordinary_id=ordinary_id,
            terminal_id=terminal_id,
            ordinary=ordinary,
            terminal=0,
            uncertain_ordinary=6,
            deadline_ceiling=deadline_ceiling,
        )
    for documents, exact_indices in alternatives:
        current = preview_record
        candidates = [current]
        parts = len(details["staged_parts"])
        retained = sum(
            len(raw) + evidence.PART_FRAMING_RESERVE for raw in details["staged_parts"]
        )
        final_candidates: set[int] = set()
        has_own_write = (
            preview_record.record_sha256 != journal._record.record_sha256
            or bool(details["staged_parts"])
        )
        for index, document in enumerate(documents):
            prior_settled_accounting = None
            if accounting_context is not None:
                min_settled = min(
                    ordinary,
                    (5 + 2 * len(details["staged_parts"]) if has_own_write else 0)
                    + 5 * index,
                )
                if min_settled > 0:
                    prior_settled_accounting = terminal_pending_accounting(
                        state,
                        sequence=sequence,
                        ordinary_id=ordinary_id,
                        terminal_id=terminal_id,
                        ordinary=min_settled,
                        terminal=0,
                        uncertain_ordinary=0,
                        deadline_ceiling=deadline_ceiling,
                    )
            current, count, framed, extra = _maximum_candidate(
                journal,
                current,
                document,
                item_count=item_count,
                tail=quote["reserved_tail_calls"],
                exact=index in exact_indices,
                prior_settled_accounting=prior_settled_accounting,
            )
            parts += count
            retained += framed
            candidates.extend(extra)
            candidates.append(current)
            if index == len(documents) - 1:
                final_candidates = {id(item) for item in (*extra, current)}
        branch_parts.append(parts)
        branch_bytes.append(retained)
        if accounting_context is not None:
            branch_journal_parts = max(0, parts - len(details["staged_parts"]))
            own_calls = (2 * census + 4) + 2 * len(details["staged_parts"])
            journal_calls = len(documents) * (2 * census + 2) + 2 * branch_journal_parts
            unreached_hold_calls = (
                0
                if has_conditional_approval
                else (api_run_plan.READ_OPERATION_REQUESTS + 1)
            )
            native_calls = max(
                0, quote["reserved_tail_calls"] - 6 - unreached_hold_calls
            )
            receipt_calls = 2 * census + 2
            branch_ordinary = min(
                ordinary, own_calls + journal_calls + native_calls + receipt_calls
            )
            branch_terminal_accounting = terminal_pending_accounting(
                state,
                sequence=sequence,
                ordinary_id=ordinary_id,
                terminal_id=terminal_id,
                ordinary=branch_ordinary,
                terminal=terminal_count,
                uncertain_ordinary=0,
                uncertain_terminal=1,
                deadline_ceiling=deadline_ceiling,
            )
        for candidate in candidates:
            ordinary_reference = store.normalize_reference(candidate.finding_lifecycle)
            terminal_reference = store.normalize_reference(
                {
                    **candidate.finding_lifecycle,
                    "schema_version": store.HISTORY_REFERENCE_VERSION,
                    "accepted_semantic_history": witness["history"],
                }
            )
            terminal_modes = (
                (False, True)
                if accounting_context is None or id(candidate) in final_candidates
                else (False,)
            )
            for terminal in terminal_modes:
                if accounting_context is not None:
                    source_accounting = (
                        branch_terminal_accounting if terminal else ordinary_settled
                    )
                    selected_reference = (
                        terminal_reference if terminal else ordinary_reference
                    )
                else:
                    source_accounting = (
                        accounting_upper
                        if not terminal or terminal_accounting_upper is None
                        else terminal_accounting_upper
                    )
                    selected_reference = terminal_reference
                accounting = copy.deepcopy(source_accounting)
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
                    finding_lifecycle=selected_reference,
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
