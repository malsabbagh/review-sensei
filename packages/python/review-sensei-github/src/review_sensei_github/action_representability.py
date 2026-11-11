"""Hosted pre-effect terminal representability bridge for Actions commands and replies (G34)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict
from typing import Any

from review_sensei.disposition import (
    FindingIdentity,
    MaintainerCommand,
    MaintainerCommandResult,
)
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


def _command_pricing(
    caller: Any,
    *,
    action: str,
    writes_enabled: bool,
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
    elif action in {"accept-risk", "dismiss", "defer"}:
        business_reqs = 3
        refinalize_reqs = 3
        receipt_reqs = 2
    elif action in {"accept-coverage-uncertainty", "verify"}:
        business_reqs = 3
        refinalize_reqs = 3
        receipt_reqs = 0
    else:
        business_reqs = 3
        refinalize_reqs = 0
        receipt_reqs = 0

    return ActionSettlementPricing(
        cold_census_requests=0,
        business_mutation_requests=business_reqs,
        refinalization_requests=refinalize_reqs,
        receipt_publication_requests=receipt_reqs,
        part_write_and_readback_requests=0,
        terminal_close_requests=1,
        committed_requests=committed_effective,
        original_api_limit=max(1, limit),
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
        intent_digest = (
            str(boundary.intent_sha256)
            if boundary is not None and hasattr(boundary, "intent_sha256")
            else "0" * 64
        )
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
    consumed_grants = tuple(getattr(ledger, "_consumed_grants", ()) or ())
    pricing = _command_pricing(
        caller,
        action=command.action,
        writes_enabled=writes_enabled,
    )
    _preview_res, _preview_rec, report = preview_session_command_representability(
        identity=identity,
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
        evidence_budget=getattr(caller, "budget_view", None),
        accounting_version=str(caller.accounting.version),
        pricing=pricing,
        consumed_grants=consumed_grants,
        allow_v3_writer=False,
    )
    if report.representable:
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
        action_name=action_name,
    )
    return require_terminal_action_representable(report)


__all__ = [
    "guard_current_command_representability",
    "require_action_settlement_representable",
]
