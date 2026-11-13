"""Hosted pre-effect terminal representability bridge for Actions commands and replies (G34)."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import datetime
from typing import Any

from review_sensei.disposition import (
    FindingIdentity,
    MaintainerCommand,
    MaintainerCommandResult,
)
from review_sensei.errors import ReviewInputError
from review_sensei.session import SessionIdentity, SessionRecord
from review_sensei.terminal_representability import (
    ActionSettlementPricing,
    TerminalRepresentabilityReport,
    action_intent_sha256,
    build_action_result_binding,
    evaluate_terminal_action_representability,
    format_terminal_refusal_summary,
    preview_session_command_representability,
    require_terminal_action_representable,
)

from .accepted_semantic_history import (
    history_reference_reserve,
    preflight_history_capacity,
    prospective_history,
)


def _unlisted_snapshot_part_reads(
    ledger: Any,
    current_record: SessionRecord | None,
    *,
    action: str,
    writes_enabled: bool,
) -> int:
    if (
        not writes_enabled
        or current_record is None
        or current_record.disposition_snapshot is None
        or action
        not in {
            "accept-risk",
            "dismiss",
            "defer",
            "accept-coverage-uncertainty",
            "verify",
        }
    ):
        return 0
    capability = getattr(ledger, "snapshot_preview_read_requests", None)
    if not callable(capability):
        state = current_record.disposition_snapshot.get("state_manifest")
        if (
            isinstance(state, Mapping)
            and state.get("encoding") == "inline-json-v1"
            and current_record.disposition_snapshot.get("schema_version") == "1.0"
        ):
            return 0
        raise ReviewInputError("snapshot read pricing capability is unavailable")
    count = capability(current_record)
    if type(count) is not int or count < 0:
        raise ReviewInputError(
            "snapshot read pricing capability returned invalid count"
        )
    return count


def _command_pricing(
    caller: Any,
    *,
    action: str,
    writes_enabled: bool,
    ledger: Any = None,
    current_record: SessionRecord | None = None,
) -> ActionSettlementPricing:
    accounting = caller.accounting
    limit = int(accounting.configuration.limit)
    usable_work = int(getattr(caller, "usable", 0))
    terminal_undispatched = sum(
        int(block.undispatched)
        for block in accounting.blocks
        if block.block_id != getattr(caller, "_block", None)
    )
    available_total = usable_work + terminal_undispatched
    committed_effective = max(0, limit - available_total)

    if not writes_enabled or action == "status":
        business_reqs = 0
        refinalize_reqs = 0
        receipt_reqs = 0
    elif action in {
        "accept-risk",
        "dismiss",
        "defer",
        "accept-coverage-uncertainty",
    }:
        business_reqs = 3
        refinalize_reqs = 3
        receipt_reqs = 2
    elif action == "verify":
        business_reqs = 3
        refinalize_reqs = 3
        receipt_reqs = 0
    else:
        business_reqs = 3
        refinalize_reqs = 0
        receipt_reqs = 2

    cold_snapshot_reads = _unlisted_snapshot_part_reads(
        ledger,
        current_record,
        action=action,
        writes_enabled=writes_enabled,
    )

    return ActionSettlementPricing(
        cold_census_requests=0,
        business_mutation_requests=business_reqs,
        refinalization_requests=refinalize_reqs,
        receipt_publication_requests=receipt_reqs,
        part_write_and_readback_requests=0,
        terminal_close_requests=1,
        committed_requests=committed_effective,
        original_api_limit=max(1, limit),
        snapshot_read_requests=cold_snapshot_reads,
    )


def _binding_from_caller(
    *,
    identity: SessionIdentity,
    caller: Any,
    ledger: Any,
    head_sha: str,
) -> tuple[str, dict[str, object]]:
    accounting = caller.accounting
    action_state = accounting.action
    if isinstance(action_state, Mapping) and isinstance(
        action_state.get("intent"), Mapping
    ):
        intent_doc = dict(action_state["intent"])
        intent_digest = action_intent_sha256(intent_doc)
        owner = intent_doc.get("owner")
        generation = (
            int(owner["generation"])
            if isinstance(owner, Mapping) and isinstance(owner.get("generation"), int)
            else int(accounting.owner.generation)
        )
        effective_head = str(intent_doc.get("head_sha") or head_sha)
    else:
        boundary = getattr(caller, "_action_effect_boundary", None)
        raw_digest = (
            getattr(boundary, "intent_sha256", None) if boundary is not None else None
        )
        if (
            not isinstance(raw_digest, str)
            or re.fullmatch(r"^[a-f0-9]{64}$", raw_digest) is None
        ):
            raise ReviewInputError(
                "caller action intent digest is unavailable; cannot derive terminal binding"
            )
        intent_digest = raw_digest
        generation = int(accounting.owner.generation)
        effective_head = head_sha
    binding = build_action_result_binding(
        identity=identity,
        policy_commit=str(accounting.configuration.commit),
        head_sha=effective_head,
        intent_sha256=intent_digest,
        configuration_digest=str(accounting.configuration.digest),
        owner_generation=generation,
        producer=str(ledger.evidence_producer()),
    )
    return intent_digest, binding


def guard_current_command_representability(
    *,
    current_action: Any,
    ledger: Any,
    identity: SessionIdentity,
    command: MaintainerCommand,
    head_sha: str,
    writes_enabled: bool,
    finding_fingerprints: Sequence[str] | None = None,
    finding_inventory: Sequence[FindingIdentity] | None = None,
    prior_inventories: Sequence[tuple[str, Sequence[FindingIdentity]]] = (),
    actor_role: str | None = None,
    policy_digest: str | None = None,
    provenance: str | None = None,
    incomplete_coverage: Sequence[str] | None = None,
    capacity_preview: dict[str, object] | None = None,
    now: datetime | None = None,
) -> MaintainerCommandResult | None:
    """Validate pre-effect terminal representability before mutating the session ledger.

    Returns ``None`` when the planned command result and terminal root fit within
    all carrier, root, and physical request bounds. When the planned command
    result is unrepresentable (such as the G34 38-file witness exceeding the
    ``8,192``-byte inline ceiling), returns a compact ``applied=False`` refusal
    ``MaintainerCommandResult`` if that explicit no-op receipt itself is
    representable and funded, or raises ``TerminalRepresentabilityError`` before
    any business write if even the refusal receipt cannot settle.
    """

    if capacity_preview is not None:
        capacity_preview.clear()
    run = getattr(current_action, "run", None)
    if run is None:
        return None
    caller = run.caller
    current_record = getattr(caller, "_effect_record", None)
    if not isinstance(current_record, SessionRecord):
        return None

    intent_digest, binding = _binding_from_caller(
        identity=identity,
        caller=caller,
        ledger=ledger,
        head_sha=head_sha,
    )
    if (
        current_record.disposition_snapshot is not None
        and writes_enabled
        and command.action
        in {"accept-risk", "dismiss", "defer", "accept-coverage-uncertainty", "verify"}
    ):
        hydrate = getattr(ledger, "hydrate_snapshot_preview", None)
        if callable(hydrate):
            hydrate(identity, current_record)
        else:
            state = current_record.disposition_snapshot.get("state_manifest")
            if not (
                isinstance(state, Mapping)
                and state.get("encoding") == "inline-json-v1"
                and current_record.disposition_snapshot.get("schema_version") == "1.0"
            ):
                raise ReviewInputError(
                    "snapshot preview hydration capability is unavailable"
                )
    consumed_grants = tuple(getattr(ledger, "_consumed_grants", ()) or ())
    pricing = _command_pricing(
        caller,
        action=command.action,
        writes_enabled=writes_enabled,
        ledger=ledger,
        current_record=current_record,
    )
    admitted_action = caller.accounting.action
    if (
        admitted_action is None
        and getattr(caller, "_accepted_semantic_context", None) is not None
    ):
        raise ReviewInputError("semantic capacity preview requires an admitted action")
    history = (
        prospective_history(caller, identity, admitted_action["intent"])
        if admitted_action is not None
        else None
    )
    details: dict[str, object] = {}
    _preview_res, _preview_rec, report = preview_session_command_representability(
        identity=identity,
        capacity_details=details,
        now=now,
        current_record=current_record,
        command=command,
        intent_sha256=intent_digest,
        binding=binding,
        finding_fingerprints=finding_fingerprints,
        finding_inventory=finding_inventory,
        prior_inventories=prior_inventories,
        actor_role=actor_role,
        policy_digest=policy_digest,
        provenance=provenance,
        incomplete_coverage=incomplete_coverage,
        enable_disposition_snapshot_writes=bool(
            getattr(ledger, "enable_disposition_snapshot_writes", False)
        ),
        supports_paged_disposition_snapshot_writes=bool(
            getattr(ledger, "supports_paged_disposition_snapshot_writes", False)
        ),
        evidence_budget=(
            getattr(ledger, "evidence_budget", None)
            or getattr(caller, "budget_view", None)
        ),
        source_ledger=ledger,
        accounting_version=str(caller.accounting.version),
        pricing=pricing,
        consumed_grants=consumed_grants,
        allow_v3_writer=False,
        result_metadata={"accepted_semantic_history": history}
        if history is not None
        else None,
        future_reserve_bytes=(
            preflight_history_capacity(caller, identity, admitted_action["intent"])
            if admitted_action is not None
            else 0
        ),
    )
    if report.representable:
        if capacity_preview is not None:
            # Sizing data only. The preview record may contain synthetic part
            # identifiers and must never be loaded or selected as authority.
            capacity_preview.update(
                result=_preview_res, record=_preview_rec, details=details
            )
        return None

    refusal_result = MaintainerCommandResult(
        action=command.action,
        applied=False,
        operator_paused=bool(current_record.operator_paused),
        summary=format_terminal_refusal_summary(report),
    )
    noop_pricing = _command_pricing(caller, action="status", writes_enabled=False)
    noop_report = evaluate_terminal_action_representability(
        identity=identity,
        intent_sha256=intent_digest,
        outcome={"kind": "explicit_noop", "reason": "no_change"},
        result_document=asdict(refusal_result),
        binding=binding,
        current_record=current_record,
        accounting_version=str(caller.accounting.version),
        pricing=noop_pricing,
        consumed_grants=consumed_grants,
        allow_v3_writer=False,
        action_name=command.action,
    )
    if not noop_report.representable:
        require_terminal_action_representable(report)
    return refusal_result


def require_action_settlement_representable(
    run: Any,
    *,
    outcome: Mapping[str, object],
    result_document: Mapping[str, object],
    extra_work_requests: int = 0,
    action_name: str | None = None,
) -> TerminalRepresentabilityReport:
    """Require that an in-process action result can settle and close before receipt/close writes."""

    caller = run.caller
    ledger = run.ledger
    identity = run.identity
    from .accepted_semantic_history import enrich_settlement

    if getattr(caller, "_accepted_semantic_context", None) is not None:
        result_document = enrich_settlement(
            caller,
            ledger,
            identity,
            caller.accounting.action["intent"],
            dict(result_document),
        )
    current_record = getattr(caller, "_effect_record", None)
    intent_digest, binding = _binding_from_caller(
        identity=identity,
        caller=caller,
        ledger=ledger,
        head_sha=run.head_sha,
    )
    accounting = caller.accounting
    limit = int(accounting.configuration.limit)
    usable_work = int(getattr(caller, "usable", 0))
    terminal_undispatched = sum(
        int(block.undispatched)
        for block in accounting.blocks
        if block.block_id != getattr(caller, "_block", None)
    )
    committed_effective = max(0, limit - (usable_work + terminal_undispatched))
    pricing = ActionSettlementPricing(
        receipt_publication_requests=max(0, int(extra_work_requests)),
        terminal_close_requests=1,
        committed_requests=committed_effective,
        original_api_limit=max(1, limit),
    )
    consumed_grants = tuple(getattr(ledger, "_consumed_grants", ()) or ())
    report = evaluate_terminal_action_representability(
        identity=identity,
        intent_sha256=intent_digest,
        outcome=outcome,
        result_document=result_document,
        binding=binding,
        current_record=current_record
        if isinstance(current_record, SessionRecord)
        else None,
        accounting_version=str(accounting.version),
        pricing=pricing,
        consumed_grants=consumed_grants,
        allow_v3_writer=False,
        future_reserve_bytes=history_reference_reserve(
            current_record, result_document.get("accepted_semantic_history")
        ),
        action_name=action_name,
    )
    return require_terminal_action_representable(report)


__all__ = [
    "guard_current_command_representability",
    "require_action_settlement_representable",
]


from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
