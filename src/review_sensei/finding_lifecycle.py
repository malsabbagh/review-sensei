"""Closed, lossless private finding-lifecycle payload codec.

This codec supplies representation, not authority. Hosts must authenticate the
current session root, producer, repository, operation and lineage independently
before accepting these bytes. Do not expose this document through public status
serializers: it includes non-displayed obligations and admitted comment facts.
Transport/accounting limits also apply; fitting this payload is not a preflight
for its enclosing manifest, receipt or protected confirmation tail.
"""

from __future__ import annotations

import json
import re
import types
from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any, cast, get_args, get_origin, get_type_hints

from .active_finding_window import (
    ActiveWindowApprovalEligibility,
    ActiveWindowEntry,
    ActiveWindowRecoveryStep,
    ActiveWindowRoundState,
    AuthoritativeDecisionInventory,
    AuthoritativeObligation,
    ChangedFileCoverageState,
    DisplayActiveWindow,
    evaluate_window_approval_eligibility,
)
from .bounded_evidence import canonical_bytes
from .errors import ReviewInputError
from .finding_drift import (
    DriftActiveEntry,
    DriftEvaluationResult,
    DriftInventory,
    DriftTrackedObligation,
    NativeThreadBinding,
    OriginalFindingCriterion,
    PresentLocationAnchor,
    _recompute_drift_verdict,
)
from .models import ReviewComment
from .publication_selection import PublishedSlot
from .validation import DEFAULT_REVIEW_LIMITS

LIFECYCLE_SCHEMA_VERSION = "finding-lifecycle-payload-v1"
LIFECYCLE_PURPOSE = "complete-finding-lifecycle"
MAX_LIFECYCLE_BYTES = 1_048_576
_MAX_ITEMS = DEFAULT_REVIEW_LIMITS.max_comments * 4
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_COMMIT = re.compile(r"[a-f0-9]{40,64}\Z")

# Explicit versioned field sets. Adding a Python dataclass field must not silently
# enlarge this durable contract. In particular, never use ReviewComment.to_dict.
_FIELDS: dict[type, tuple[str, ...]] = {
    ReviewComment: (
        "path",
        "line",
        "body",
        "blocking",
        "severity",
        "category",
        "fix_effort",
        "symbol",
        "defect_kind",
        "evidence_id",
        "side",
        "effective_blocking",
        "needs_human",
    ),
    ChangedFileCoverageState: (
        "coverage_mode",
        "approval_state",
        "enumeration_complete",
        "fully_reviewed",
        "enumerated_paths",
        "reviewed_paths",
        "coverage_uncertainty_accepted",
        "acceptance_proof_sha256",
    ),
    AuthoritativeObligation: (
        "obligation_id",
        "fingerprint",
        "resolution_criterion",
        "path",
        "line",
        "side",
        "symbol",
        "defect_kind",
        "concern",
        "blocking",
        "needs_human",
        "impact",
        "evidence",
        "lifecycle_state",
        "generation",
        "native_target",
        "target_state",
        "target_proof_sha256",
        "resolution_proof_sha256",
        "proposal_rank",
        "comment",
    ),
    AuthoritativeDecisionInventory: ("obligations",),
    ActiveWindowEntry: (
        "obligation_id",
        "fingerprint",
        "path",
        "line",
        "side",
        "required",
        "impact",
        "lifecycle_state",
        "native_target",
        "target_action",
        "consumes_new_slot",
    ),
    DisplayActiveWindow: ("max_active_findings", "entries", "slots"),
    PublishedSlot: ("target", "state", "proof_sha256"),
    ActiveWindowRecoveryStep: ("route", "reason_code", "target_scope"),
    ActiveWindowApprovalEligibility: (
        "approved",
        "outcome",
        "withheld_reasons",
        "recovery_steps",
    ),
    ActiveWindowRoundState: (
        "head_sha",
        "base_sha",
        "round_number",
        "review_complete",
        "changed_file_coverage",
        "authoritative_decision_inventory",
        "display_active_findings",
        "approval_eligibility",
    ),
    OriginalFindingCriterion: (
        "obligation_id",
        "original_fingerprint",
        "resolution_criterion",
        "origin_head_sha",
        "origin_path",
        "origin_line",
        "origin_side",
        "origin_symbol",
        "defect_kind",
        "concern",
        "blocking",
        "needs_human",
        "impact",
        "evidence",
    ),
    PresentLocationAnchor: (
        "head_sha",
        "path",
        "line",
        "side",
        "symbol",
        "anchor_location",
        "drift_kind",
        "candidate_paths",
    ),
    NativeThreadBinding: (
        "thread_id",
        "comment_id",
        "placement",
        "body_reason",
        "slot_state",
        "proof_sha256",
        "host_marked_outdated",
        "host_marked_resolved",
    ),
    DriftTrackedObligation: (
        "criterion",
        "present_anchor",
        "lifecycle_state",
        "generation",
        "thread_binding",
        "resolution_proof_sha256",
        "proposal_rank",
        "latest_comment",
    ),
    DriftInventory: ("obligations",),
    DriftActiveEntry: (
        "obligation_id",
        "original_fingerprint",
        "origin_path",
        "present_path",
        "present_line",
        "present_side",
        "drift_kind",
        "lifecycle_state",
        "placement",
        "body_reason",
        "thread_id",
        "thread_action",
    ),
    DriftEvaluationResult: (
        "head_sha",
        "base_sha",
        "round_number",
        "review_complete",
        "max_active_findings",
        "changed_file_coverage",
        "inventory",
        "active_entries",
        "slots",
        "approved",
        "withheld_reasons",
        "recovery_steps",
        "body_placement_note",
    ),
}


@dataclass(frozen=True)
class RestoredFindingLifecycle:
    state: ActiveWindowRoundState | DriftEvaluationResult
    input_identity: tuple[str, str, str] | None
    concern_history: Mapping[str, frozenset[str]]


def _invalid() -> ReviewInputError:
    # Never put hidden inventory, counts, bodies or provider text in diagnostics.
    return ReviewInputError("finding lifecycle payload is invalid or unsupported")


def _closed(value: object, names: tuple[str, ...]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != set(names):
        raise _invalid()
    return value


def _encode(value: object) -> object:
    cls = type(value)
    if cls in _FIELDS:
        names = _FIELDS[cls]
        if tuple(f.name for f in fields(cast(Any, cls))) != names:
            raise _invalid()
        return {name: _encode(getattr(value, name)) for name in names}
    if cls is tuple:
        return [_encode(item) for item in cast(tuple[object, ...], value)]
    if value is None or cls in {str, bool, int}:
        return value
    raise _invalid()


def _decode(value: object, expected: Any) -> Any:
    origin = get_origin(expected)
    if origin is types.UnionType:
        for option in get_args(expected):
            try:
                return _decode(value, option)
            except ReviewInputError:
                pass
        raise _invalid()
    if origin is tuple:
        if type(value) is not list or len(value) > _MAX_ITEMS:
            raise _invalid()
        args = get_args(expected)
        if len(args) != 2 or args[1] is not Ellipsis:
            raise _invalid()
        return tuple(_decode(item, args[0]) for item in value)
    if expected in _FIELDS:
        names = _FIELDS[expected]
        if tuple(f.name for f in fields(expected)) != names:
            raise _invalid()
        obj = _closed(value, names)
        hints = get_type_hints(expected)
        return expected(**{name: _decode(obj[name], hints[name]) for name in names})
    if expected is type(None):
        if value is not None:
            raise _invalid()
        return None
    if expected in {str, bool, int} and type(value) is expected:
        if (
            expected is str
            and len(cast(str, value).encode("utf-8"))
            > DEFAULT_REVIEW_LIMITS.max_summary_bytes
        ):
            raise _invalid()
        if expected is int and not -(2**63) <= cast(int, value) < 2**63:
            raise _invalid()
        return value
    raise _invalid()


def _validate_state(state: ActiveWindowRoundState | DriftEvaluationResult) -> None:
    if (
        _COMMIT.fullmatch(state.head_sha) is None
        or _COMMIT.fullmatch(state.base_sha) is None
    ):
        raise _invalid()
    if state.round_number < 1:
        raise _invalid()
    coverage = state.changed_file_coverage
    if (
        len(set(coverage.enumerated_paths)) != len(coverage.enumerated_paths)
        or len(set(coverage.reviewed_paths)) != len(coverage.reviewed_paths)
        or not set(coverage.reviewed_paths) <= set(coverage.enumerated_paths)
    ):
        raise _invalid()
    if isinstance(state, ActiveWindowRoundState):
        inventory = state.authoritative_decision_inventory
        window = state.display_active_findings
        items = {item.obligation_id: item for item in inventory.obligations}
        slots = {slot.target: slot for slot in window.slots}
        bound_targets = [
            item.native_target
            for item in inventory.obligations
            if item.native_target is not None
        ]
        if len(set(bound_targets)) != len(bound_targets):
            raise _invalid()
        for item in inventory.obligations:
            if item.native_target is not None:
                slot = slots.get(item.native_target)
                if slot is None or (slot.state, slot.proof_sha256) != (
                    item.target_state,
                    item.target_proof_sha256,
                ):
                    raise _invalid()
        for entry in window.entries:
            selected = items.get(entry.obligation_id)
            if selected is None or not selected.unresolved:
                raise _invalid()
            if (
                entry.fingerprint,
                entry.path,
                entry.line,
                entry.side,
                entry.required,
                entry.impact,
                entry.lifecycle_state,
                entry.native_target,
            ) != (
                selected.fingerprint,
                selected.path,
                selected.line,
                selected.side,
                selected.required,
                selected.impact,
                selected.lifecycle_state,
                selected.native_target,
            ):
                raise _invalid()
        expected = evaluate_window_approval_eligibility(
            changed_file_coverage=coverage,
            authoritative_decision_inventory=inventory,
            display_active_findings=window,
            review_complete=state.review_complete,
        )
        if state.approval_eligibility != expected:
            raise _invalid()
    else:
        if not 1 <= state.max_active_findings <= DEFAULT_REVIEW_LIMITS.max_comments:
            raise _invalid()
        if len(state.active_entries) > state.max_active_findings:
            raise _invalid()
        drift_items = {item.obligation_id: item for item in state.inventory.obligations}
        if len({entry.obligation_id for entry in state.active_entries}) != len(
            state.active_entries
        ):
            raise _invalid()
        if len({slot.target for slot in state.slots}) != len(state.slots):
            raise _invalid()
        expected_slots = {
            item.thread_binding.thread_id: item.thread_binding.to_slot()
            for item in state.inventory.obligations
            if item.thread_binding is not None
        }
        if len(expected_slots) != sum(
            item.thread_binding is not None for item in state.inventory.obligations
        ):
            raise _invalid()
        if {slot.target: slot for slot in state.slots} != expected_slots:
            raise _invalid()
        occupied = sum(slot.reserved for slot in state.slots)
        for drift_entry in state.active_entries:
            ob = drift_items.get(drift_entry.obligation_id)
            if ob is None or not ob.unresolved:
                raise _invalid()
            binding = ob.thread_binding
            if (
                drift_entry.original_fingerprint,
                drift_entry.origin_path,
                drift_entry.present_path,
                drift_entry.present_line,
                drift_entry.present_side,
                drift_entry.drift_kind,
                drift_entry.lifecycle_state,
                drift_entry.thread_id,
            ) != (
                ob.criterion.original_fingerprint,
                ob.criterion.origin_path,
                ob.present_anchor.path,
                ob.present_anchor.line,
                ob.present_anchor.side,
                ob.present_anchor.drift_kind,
                ob.lifecycle_state,
                None if binding is None else binding.thread_id,
            ):
                raise _invalid()
            if drift_entry.thread_action not in {
                "create",
                "body-only",
                "reopen",
                "update",
            }:
                raise _invalid()
            if drift_entry.placement not in {"inline", "body"}:
                raise _invalid()
            if drift_entry.thread_action in {"update", "reopen"} and binding is None:
                raise _invalid()
            if (
                drift_entry.thread_action in {"create", "body-only"}
                and binding is not None
                and binding.slot_state != "uncertain"
            ):
                raise _invalid()
            if binding is None or not binding.to_slot().reserved:
                occupied += 1
        if occupied > state.max_active_findings:
            raise _invalid()
        approved, reasons, steps = _recompute_drift_verdict(
            coverage_state=coverage,
            review_complete=state.review_complete,
            inventory=state.inventory,
            active_entries=state.active_entries,
            slots=state.slots,
        )
        if (state.approved, state.withheld_reasons, state.recovery_steps) != (
            approved,
            reasons,
            steps,
        ):
            raise _invalid()


def encode_lifecycle_state(
    state: ActiveWindowRoundState | DriftEvaluationResult,
    *,
    input_identity: tuple[str, str, str] | None = None,
    concern_history: Mapping[str, frozenset[str]] | None = None,
) -> dict[str, object]:
    """Encode the full private payload; validate it through the cold-reader seam."""
    if type(state) not in {ActiveWindowRoundState, DriftEvaluationResult}:
        raise _invalid()
    document: dict[str, object] = {
        "schema_version": LIFECYCLE_SCHEMA_VERSION,
        "purpose": LIFECYCLE_PURPOSE,
        "mode": "active-window"
        if isinstance(state, ActiveWindowRoundState)
        else "drift",
        "state": _encode(state),
        "input_identity": None if input_identity is None else list(input_identity),
        "concern_history": {
            key: sorted(values) for key, values in (concern_history or {}).items()
        },
    }
    decode_lifecycle_state(document)
    return document


def decode_lifecycle_state(document: object) -> RestoredFindingLifecycle:
    """Reject incomplete/unknown payloads instead of defaulting any authority."""
    try:
        # Bound pre-parsed callers too. Untrusted byte readers must use the
        # bounded JSON entry point below before materializing arbitrary JSON.
        if len(canonical_bytes(document)) > MAX_LIFECYCLE_BYTES:
            raise _invalid()
        obj = _closed(
            document,
            (
                "schema_version",
                "purpose",
                "mode",
                "state",
                "input_identity",
                "concern_history",
            ),
        )
        if (
            obj["schema_version"] != LIFECYCLE_SCHEMA_VERSION
            or obj["purpose"] != LIFECYCLE_PURPOSE
        ):
            raise _invalid()
        mode = obj["mode"]
        if mode not in {"active-window", "drift"}:
            raise _invalid()
        state = _decode(
            obj["state"],
            ActiveWindowRoundState
            if mode == "active-window"
            else DriftEvaluationResult,
        )
        _validate_state(state)
        identity = obj["input_identity"]
        if identity is not None:
            if (
                type(identity) is not list
                or len(identity) != 3
                or any(type(part) is not str for part in identity)
                or identity[:2] != [state.head_sha, state.base_sha]
                or _SHA.fullmatch(identity[2]) is None
            ):
                raise _invalid()
            identity = tuple(identity)
        history = obj["concern_history"]
        if type(history) is not dict or len(history) > _MAX_ITEMS:
            raise _invalid()
        ids = (
            {o.obligation_id for o in state.inventory.obligations}
            if mode == "drift"
            else {
                o.obligation_id
                for o in state.authoritative_decision_inventory.obligations
            }
        )
        restored: dict[str, frozenset[str]] = {}
        for key, values in history.items():
            if (
                key not in ids
                or type(values) is not list
                or len(values) > _MAX_ITEMS
                or any(type(v) is not str or _SHA.fullmatch(v) is None for v in values)
                or len(set(values)) != len(values)
            ):
                raise _invalid()
            restored[key] = frozenset(values)
        return RestoredFindingLifecycle(state, identity, restored)
    except (
        TypeError,
        ValueError,
        AttributeError,
        KeyError,
        RecursionError,
        OverflowError,
    ) as exc:
        raise _invalid() from exc


def lifecycle_state_bytes(
    state: ActiveWindowRoundState | DriftEvaluationResult,
    *,
    input_identity: tuple[str, str, str] | None = None,
    concern_history: Mapping[str, frozenset[str]] | None = None,
) -> bytes:
    return canonical_bytes(
        encode_lifecycle_state(
            state, input_identity=input_identity, concern_history=concern_history
        )
    )


def decode_lifecycle_bytes(payload: bytes) -> RestoredFindingLifecycle:
    """Read bounded JSON without accepting duplicate keys or non-finite numbers."""
    if type(payload) is not bytes or len(payload) > MAX_LIFECYCLE_BYTES:
        raise _invalid()

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise _invalid()
            result[key] = value
        return result

    def invalid_constant(value: str) -> None:
        raise _invalid()

    try:
        document = json.loads(
            payload, object_pairs_hook=unique, parse_constant=invalid_constant
        )
        return decode_lifecycle_state(document)
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise _invalid() from exc


from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
