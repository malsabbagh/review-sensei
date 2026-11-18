"""Pure, source-profile-bound verification costs, never dispatch admission.

These line items deliberately do not assert that a route is complete. Its exact
source producer must include initialization, entry, business, checkpoint, report,
receipt and terminal writes before granting any separate execution permit.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from weakref import WeakKeyDictionary

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError


@dataclass(frozen=True, eq=False)
class VerificationCost:
    """Immutable arithmetic receipt; possessing it grants no native capability."""

    native: int
    checkpoints: int
    reports: int
    entry: int
    receipt: int
    terminal: int

    @property
    def total(self) -> int:
        return sum(
            (
                self.native,
                self.checkpoints,
                self.reports,
                self.entry,
                self.receipt,
                self.terminal,
            )
        )


_ISSUED: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()
_PLANS: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()


def _signature(cost):
    return (
        cost.native,
        cost.checkpoints,
        cost.reports,
        cost.entry,
        cost.receipt,
        cost.terminal,
    )


def price_verification_components(
    caller,
    *,
    native_requests=(),
    checkpoint_parts=(),
    report_pages=(),
    entry_checkpoint=False,
    command_receipt=False,
    terminal_checkpoint=False,
):
    """Count exact supplied mutations under the initial plain/inline profile.

    The caller's source plan, not these optional component arguments, proves
    completeness. Unknown receipt existence should be conservatively counted.
    A partitioned prospective checkpoint is explicitly unsupported in opted mode.
    No HTTP, accounting debit, permission, or operation allocation is performed.
    """
    from .trust_effects import verification_calls, verification_profile

    profile = verification_profile(caller)
    unit = verification_calls(caller)
    if type(unit) is not int or unit != int(profile is not None):
        raise ReviewInputError("trust verification pricing profile changed")
    if (
        not isinstance(native_requests, tuple)
        or not isinstance(checkpoint_parts, tuple)
        or not isinstance(report_pages, tuple)
        or any(
            type(flag) is not bool
            for flag in (entry_checkpoint, command_receipt, terminal_checkpoint)
        )
        or any(type(n) is not int or n < 0 for n in checkpoint_parts)
        or any(not isinstance(body, str) for body in report_pages)
    ):
        raise ReviewInputError("trust verification pricing components are invalid")
    for request in native_requests:
        if (
            not isinstance(request, (tuple, list))
            or len(request) != 3
            or request[0] not in {"POST", "PUT", "PATCH", "DELETE"}
            or not isinstance(request[1], str)
            or not request[1].startswith("/")
            or request[1].startswith("//")
            or not isinstance(request[2], Mapping)
        ):
            raise ReviewInputError("trust verification mutation identity is invalid")
    if profile is not None and (
        any(checkpoint_parts)
        or getattr(caller, "_controlled_restart_admission", None) is not None
    ):
        raise ReviewInputError("trust verification inline pricing is unsupported")
    cost = VerificationCost(
        unit * len(native_requests),
        unit * len(checkpoint_parts),
        unit * len(report_pages),
        unit * int(entry_checkpoint),
        unit * int(command_receipt),
        unit * int(terminal_checkpoint),
    )
    record = getattr(caller, "_effect_record", None)
    _ISSUED[cost] = (
        caller,
        profile,
        getattr(caller, "_root", None),
        record,
        canonical_bytes(record.to_dict()) if record is not None else None,
        canonical_bytes([native_requests, checkpoint_parts, report_pages]),
        _signature(cost),
    )
    return cost


def require_verification_cost(cost, caller):
    """Recheck sizing custody at admission, without authorizing any effect."""
    from .trust_effects import verification_profile

    issued = _ISSUED.get(cost) if type(cost) is VerificationCost else None
    record = getattr(caller, "_effect_record", None)
    if (
        issued is None
        or issued[0] is not caller
        or issued[1] is not verification_profile(caller)
        or issued[2] != getattr(caller, "_root", None)
        or issued[3] is not record
        or issued[4]
        != (canonical_bytes(record.to_dict()) if record is not None else None)
        or issued[6] != _signature(cost)
    ):
        raise ReviewInputError("trust verification pricing custody changed")
    plan_source = _PLANS.get(cost)
    if plan_source is not None:
        plan, journal, http, budget, payload = plan_source
        type(plan).require(plan, journal, http=http, budget=budget)
        if plan.payload != payload:
            raise ReviewInputError("trust verification source plan changed")
    return cost.total


def price_finalization_verification(plan, journal, *, http, budget, checkpoint_parts):
    """Derive native/report counts from an actually issued finalization plan.

    Entry and disposition writes precede this scope and must be quoted separately.
    Command receipt publication follows it and is also a separate component.
    The one inline terminal close is included; this remains a cost, not a permit.
    """
    from .command_finalization_plan import CommandFinalizationPlan
    from .reply_finalization_plan import ReplyFinalizationPlan

    if type(plan) not in {CommandFinalizationPlan, ReplyFinalizationPlan}:
        raise ReviewInputError("trust finalization pricing plan is unissued")
    type(plan).require(plan, journal, http=http, budget=budget)
    payload = json.loads(plan.payload)
    requests = (
        payload.get("ordered_requests")
        if type(plan) is ReplyFinalizationPlan
        else [
            request
            for request in (
                payload.get("review_update"),
                *payload.get("cleanup_requests", ()),
                payload.get("check_request"),
                payload.get("conditional_approval"),
            )
            if request is not None
        ]
    )
    if not isinstance(requests, list):
        raise ReviewInputError("trust finalization mutation plan is missing")
    ledger = journal.ledger
    from .trust_effects import verification_profile

    if verification_profile(ledger.caller) is not None and getattr(
        ledger, "_quarantine_required", False
    ):
        raise ReviewInputError("trust finalization quarantine pricing is unsupported")
    reports = (
        payload.get("report_pages", []) if type(plan) is ReplyFinalizationPlan else []
    )
    if not isinstance(reports, list):
        raise ReviewInputError("trust finalization report plan is invalid")
    cost = price_verification_components(
        ledger.caller,
        native_requests=tuple(requests),
        checkpoint_parts=checkpoint_parts,
        report_pages=tuple(reports),
        terminal_checkpoint=True,
    )
    _PLANS[cost] = (plan, journal, http, budget, plan.payload)
    return cost
