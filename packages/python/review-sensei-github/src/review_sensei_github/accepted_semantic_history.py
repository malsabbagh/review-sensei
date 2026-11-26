"""Bounded semantic observations carried by authenticated action results.

These records never grant dispatch, cessation, cancellation or approval authority.
Only the existing owned action settlement publishes them. There is no history
walk: cold continuation opens at most the immediately retained predecessor.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, NoReturn

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError

KEY = "accepted_semantic_history"
VERSION = "accepted-semantic-history-v1"
LIMIT = 2


class DuplicateTerminalDelivery(ReviewInputError):
    """Cross-run duplicate delivery of an already terminally completed source."""


def action_operation_key(
    *,
    purpose: str,
    repository_id: int,
    pull_request: int,
    head_sha: str,
    source: Mapping[str, Any],
    configuration_digest: str,
) -> str:
    """Compute the canonical operation key shared by action admission and guards."""
    return hashlib.sha256(
        canonical_bytes(
            {
                "purpose": purpose,
                "repository_id": repository_id,
                "pull_request": pull_request,
                "head_sha": head_sha,
                "source": dict(source),
                "configuration": configuration_digest,
            }
        )
    ).hexdigest()


_STATE_ADVANCING_COMMAND_ACTIONS = frozenset(
    {
        "dismiss",
        "defer",
        "accept-risk",
        "accept-coverage-uncertainty",
        "approve-media",
        "continue",
        "reenroll",
    }
)
_NON_ADVANCING_COMMAND_ACTIONS = frozenset({"status", "pause", "verify"})


def _target_allows_disposition_entry(caller: Any) -> bool:
    target = getattr(caller, "_accepted_semantic_target", None)
    return (
        type(target) is tuple
        and len(target) >= 4
        and type(target[0]) is str
        and target[0] == "command"
        and type(target[3]) is str
        and target[3] in _STATE_ADVANCING_COMMAND_ACTIONS
    )


def _disposition_entry_allowed(caller: Any, history: dict[str, Any]) -> bool:
    if not _target_allows_disposition_entry(caller):
        return False
    trailing_disposition_no_progress = 0
    for item in reversed(history["records"]):
        if item["made_progress"]:
            break
        if item.get("disposition_entry") is True:
            trailing_disposition_no_progress += 1
    return trailing_disposition_no_progress < LIMIT


def _digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _hold(reason) -> NoReturn:
    raise ReviewInputError("accepted_semantic_history_" + reason)


def _target_identity(
    caller: Any,
) -> tuple[str | None, str | None, Any, str | None, bool]:
    target = getattr(caller, "_accepted_semantic_target", None)
    if type(target) is not tuple:
        return None, None, None, None, False
    purpose = target[0] if len(target) >= 1 and type(target[0]) is str else None
    action = target[3] if len(target) >= 4 and type(target[3]) is str else None
    source = target[4] if len(target) >= 5 and isinstance(target[4], dict) else None
    op_key = target[5] if len(target) >= 6 and type(target[5]) is str else None
    allow_dup = getattr(caller, "_allow_duplicate_terminal_delivery", None)
    allowed = (
        allow_dup
        if type(allow_dup) is bool
        else (purpose == "command" and action != "approve-media")
    )
    return purpose, action, source, op_key, allowed


def _matches_target(
    purpose: str | None,
    source: dict[str, Any] | None,
    op_key: str | None,
    *,
    item_purpose: Any,
    item_source: Any,
    item_op_key: Any,
) -> bool:
    return (
        purpose in {"command", "reply"}
        and item_purpose == purpose
        and isinstance(op_key, str)
        and isinstance(item_op_key, str)
        and item_op_key == op_key
        and isinstance(source, dict)
        and item_source == source
    )


def _check_duplicate_terminal_delivery(
    caller: Any,
    history: Any,
    *,
    old_action: Mapping[str, Any] | None = None,
    record: Any = None,
    old_record: Any = None,
) -> None:
    if (
        getattr(record, "quarantine_lineage", None) is not None
        or getattr(old_record, "quarantine_lineage", None) is not None
    ):
        return
    purpose, action, source, op_key, allowed = _target_identity(caller)
    if not allowed or action == "approve-media" or (source is None and op_key is None):
        return
    if (
        isinstance(old_action, Mapping)
        and old_action.get("phase") == "terminal"
        and not (
            isinstance(old_action.get("outcome"), Mapping)
            and old_action["outcome"].get("kind") == "canceled_unstarted"
        )
    ):
        old_intent = old_action.get("intent")
        if isinstance(old_intent, Mapping) and _matches_target(
            purpose,
            source,
            op_key,
            item_purpose=old_intent.get("purpose"),
            item_source=old_intent.get("source"),
            item_op_key=old_intent.get("operation_key"),
        ):
            raise DuplicateTerminalDelivery(
                "accepted_semantic_history_duplicate_terminal_delivery"
            )
    rows: Any = None
    if isinstance(history, dict):
        if history.get("qualification") == "unavailable":
            prior = history.get("prior")
            if isinstance(prior, dict):
                rows = prior.get("records")
        else:
            rows = history.get("records")
    if isinstance(rows, list):
        for item in rows:
            if isinstance(item, dict) and _matches_target(
                purpose,
                source,
                op_key,
                item_purpose=item.get("purpose"),
                item_source=item.get("source"),
                item_op_key=item.get("operation_key"),
            ):
                raise DuplicateTerminalDelivery(
                    "accepted_semantic_history_duplicate_terminal_delivery"
                )


COMMAND_CAPACITY_REFUSAL_PREFIX = (
    "command finalization capacity unavailable before business writes:"
)
QUARANTINE_CAPACITY_REFUSAL_PREFIX = (
    "quarantine command finalization capacity unavailable before business writes:"
)
PREWRITE_CAPACITY_REFUSAL_PREFIXES: tuple[str, ...] = (
    COMMAND_CAPACITY_REFUSAL_PREFIX,
    QUARANTINE_CAPACITY_REFUSAL_PREFIX,
)


def _is_prewrite_capacity_refusal(result: Mapping[str, Any] | None) -> bool:
    """True for unapplied pre-write command finalization capacity refusals."""
    if not isinstance(result, Mapping) or result.get("applied") is not False:
        return False
    summary = result.get("summary")
    return isinstance(summary, str) and summary.startswith(
        PREWRITE_CAPACITY_REFUSAL_PREFIXES
    )


def _is_prewrite_non_dispositional_noop(
    result: Mapping[str, Any],
    *,
    unchanged_lifecycle: bool,
    raw_before: Mapping[str, Any] | None,
    raw_after: Mapping[str, Any],
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> bool:
    """True when a command aborted before business writes without mutating lifecycle state."""
    if not isinstance(result, Mapping) or result.get("applied") is not False:
        return False
    if not unchanged_lifecycle or after != before:
        return False
    if raw_before is not None and raw_after != raw_before:
        return False
    return _is_prewrite_capacity_refusal(result) or (
        result.get("evaluation_failed") is True and not result.get("receipts")
    )


def action_allows_carried_history_fallback(
    action: Mapping[str, Any] | None,
    retained_action: Mapping[str, Any] | None = None,
) -> bool:
    """True when a settled no-op/unstarted predecessor legitimately omitted history."""
    if not isinstance(action, Mapping):
        return False
    outcome = action.get("outcome")
    if not isinstance(outcome, Mapping):
        return False
    kind = outcome.get("kind")
    if kind == "canceled_unstarted":
        return True
    if outcome in (
        {"kind": "explicit_noop", "reason": "capacity_unavailable"},
        {"kind": "explicit_noop", "reason": "evaluation_failed"},
    ):
        return True
    if kind == "explicit_noop" and isinstance(retained_action, Mapping):
        return _is_prewrite_capacity_refusal(retained_action.get("result"))
    return False


def _record_has_pending_receipt(record: Any) -> bool:
    if record is None:
        return False
    if getattr(record, "publication_staging", None) is not None:
        return True
    queue = getattr(record, "assessment_queue", None)
    return isinstance(queue, Mapping) and queue.get("active_operation") is not None


def semantic_snapshot(state, journal):
    """Project complete decoded inventory, excluding root/request/round churn."""
    from review_sensei.active_finding_window import (
        ActiveWindowRoundState,
        AuthoritativeObligation,
    )
    from review_sensei.finding_drift import DriftEvaluationResult

    if not isinstance(state, (ActiveWindowRoundState, DriftEvaluationResult)):
        _hold("inventory_required")
    inventory = (
        state.authoritative_decision_inventory
        if isinstance(state, ActiveWindowRoundState)
        else state.inventory
    )
    obligations = inventory.obligations
    unresolved = sorted(o.obligation_id for o in obligations if o.unresolved)
    uncertain = []
    criteria = []
    for ob in obligations:
        if isinstance(ob, AuthoritativeObligation):
            criteria.append((ob.obligation_id, ob.resolution_criterion))
            if ob.target_state == "uncertain":
                uncertain.append(ob.obligation_id)
        else:
            criteria.append((ob.obligation_id, ob.criterion.resolution_criterion))
            if (
                ob.thread_binding is not None
                and ob.thread_binding.slot_state == "uncertain"
            ):
                uncertain.append(ob.obligation_id)
    coverage = state.changed_file_coverage
    if coverage.approval_state == "partial" and not coverage.fully_reviewed:
        criteria.append(("coverage-uncertainty", "partial"))
        if not coverage.coverage_uncertainty_accepted:
            unresolved.append("coverage-uncertainty")
            unresolved.sort()
    pending = sorted(
        e["request_sha256"] for e in journal["effects"] if e["status"] == "pending"
    )
    return {
        "base_sha": state.base_sha,
        "head_sha": state.head_sha,
        "review_complete": state.review_complete,
        "inventory_sha256": _digest(sorted(criteria)),
        "unresolved_sha256": _digest(unresolved),
        "unresolved_count": len(unresolved),
        "enumerated_sha256": _digest(sorted(coverage.enumerated_paths)),
        "reviewed_sha256": _digest(sorted(coverage.reviewed_paths)),
        "reviewed_count": len(coverage.reviewed_paths),
        "enumeration_complete": coverage.enumeration_complete,
        "fully_reviewed": coverage.fully_reviewed,
        "uncertain_sha256": _digest(sorted(uncertain)),
        "uncertain_count": len(uncertain),
        "pending_sha256": _digest(pending),
        "pending_count": len(pending),
        "unknown_trust_effect": journal["unknown_trust_effect"],
        "receipts_sha256": _digest(
            sorted(journal["native_receipts"], key=lambda x: x["instance"])
        ),
    }


def historical_snapshot(snapshot, ledger, record):
    """Observe complete authenticated origin obligations, never cursor churn."""
    if record.quarantine_lineage is not None:
        from .quarantine_origin import _AUTHENTICATED, AuthenticatedQuarantine

        authenticated = getattr(ledger, "_quarantine_authenticated", None)
        if (
            type(authenticated) is not AuthenticatedQuarantine
            or authenticated not in _AUTHENTICATED
            or authenticated.lineage != record.quarantine_lineage
            or authenticated.original_identity != record.identity()
        ):
            _hold("authenticated_historical_inventory_required")
        if authenticated.record_sha256 != record.record_sha256:
            from .quarantine_checkpoint import confirmed_checkpoint_capacity

            caller = getattr(ledger, "caller", None)
            if caller is None or authenticated.original_caller is not caller:
                _hold("authenticated_historical_inventory_required")
            receipt = confirmed_checkpoint_capacity(caller, ledger, record.identity())
            if receipt.record.to_dict() != record.to_dict():
                _hold("authenticated_historical_inventory_required")
            # This exact owned GET proves the current root retained the identical
            # complete lineage (including the decision-state manifest). The earlier
            # authenticated observation stays attached to its original source root;
            # neither its registration nor any serialized authority is rewritten.
        # pending validates complete inventory digests, all origin memberships and
        # each retained individual decision. No transport references count as work.
        pending = authenticated.pending
        snapshot = {
            **snapshot,
            "inventory_sha256": _digest(
                {
                    "domain": "current-and-authenticated-historical-obligations-v1",
                    "current": snapshot["inventory_sha256"],
                    "historical": authenticated.inventories,
                }
            ),
            "unresolved_sha256": _digest(
                {
                    "domain": "current-and-authenticated-historical-unresolved-v1",
                    "current": snapshot["unresolved_sha256"],
                    "historical": sorted(pending),
                }
            ),
            "unresolved_count": snapshot["unresolved_count"] + len(pending),
        }
    historical_assessments = getattr(record, "historical_assessments", None)
    if historical_assessments is not None:
        from review_sensei.historical_assessments import (
            normalize_hold,
            pending_assessments,
        )

        hold = normalize_hold(historical_assessments)
        if hold is None:
            _hold("historical_assessments_invalid")
        hold_pending = sorted(a["instance"] for a in pending_assessments(hold))
        snapshot = {
            **snapshot,
            "inventory_sha256": _digest(
                {
                    "domain": "current-and-historical-assessments-v1",
                    "current": snapshot["inventory_sha256"],
                    "scope_sha256": hold["scope_sha256"],
                }
            ),
            "unresolved_sha256": _digest(
                {
                    "domain": "current-and-historical-assessments-unresolved-v1",
                    "current": snapshot["unresolved_sha256"],
                    "pending": hold_pending,
                }
            ),
            "unresolved_count": snapshot["unresolved_count"] + len(hold_pending),
        }
    return snapshot


def semantic_advancement(before, after):
    """Positive semantic progress, never arbitrary changed bytes or bookkeeping."""
    same_snapshot = (before["base_sha"], before["head_sha"]) == (
        after["base_sha"],
        after["head_sha"],
    )
    if not same_snapshot:
        return (
            after["review_complete"]
            and after["fully_reviewed"]
            and not after["pending_count"]
            and not after["unknown_trust_effect"]
        )
    return bool(
        (
            before["inventory_sha256"] == after["inventory_sha256"]
            and after["unresolved_count"] < before["unresolved_count"]
        )
        or (
            before["enumerated_sha256"] == after["enumerated_sha256"]
            and after["reviewed_count"] > before["reviewed_count"]
        )
        or (not before["review_complete"] and after["review_complete"])
        or (
            before["uncertain_count"] > after["uncertain_count"]
            and before["inventory_sha256"] == after["inventory_sha256"]
        )
        or (
            before["pending_count"] > after["pending_count"]
            and before["inventory_sha256"] == after["inventory_sha256"]
        )
        or (
            before["unknown_trust_effect"]
            and not after["unknown_trust_effect"]
            and before["inventory_sha256"] == after["inventory_sha256"]
        )
    )


def _validate_snapshot(value):
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
    if type(value) is not dict or set(value) != hashes | counts | flags | {
        "base_sha",
        "head_sha",
    }:
        _hold("invalid_snapshot")
    for name in hashes | {"base_sha", "head_sha"}:
        n = 40 if name in {"base_sha", "head_sha"} else 64
        if (
            type(value[name]) is not str
            or re.fullmatch("[a-f0-9]{%d}" % n, value[name]) is None
        ):
            _hold("invalid_snapshot")
    if any(
        type(value[k]) is not int or not 0 <= value[k] <= 100000 for k in counts
    ) or any(type(value[k]) is not bool for k in flags):
        _hold("invalid_snapshot")
    return value


def validate_history(value, identity):
    if (
        type(value) is not dict
        or set(value)
        != {"schema_version", "repository_id", "pull_request", "streak", "records"}
        or value["schema_version"] != VERSION
    ):
        _hold("unsupported")
    if (
        type(value["repository_id"]) is not int
        or type(value["pull_request"]) is not int
        or not 0 < value["repository_id"] <= (1 << 53) - 1
        or not 0 < value["pull_request"] <= (1 << 53) - 1
        or value["repository_id"] != identity.repository_id
        or value["pull_request"] != identity.pull_request
    ):
        _hold("foreign_scope")
    if (
        type(value["streak"]) is not int
        or not 0 <= value["streak"] <= LIMIT
        or type(value["records"]) is not list
        or not 1 <= len(value["records"]) <= LIMIT
    ):
        _hold("incomplete")
    base_keys = {
        "operation_key",
        "purpose",
        "source",
        "snapshot",
        "made_progress",
        "target_base_sha",
        "target_head_sha",
    }
    seen = set()
    for item in value["records"]:
        if type(item) is not dict or (
            set(item) != base_keys and set(item) != (base_keys | {"disposition_entry"})
        ):
            _hold("invalid_record")
        if "disposition_entry" in item and (
            item["purpose"] != "command"
            or item["disposition_entry"] is not True
            or item["made_progress"] is not False
        ):
            _hold("invalid_record")
        source = item["source"]
        if (
            type(source) is not dict
            or set(source) != {"kind", "id", "updated_at", "body_sha256"}
            or source["kind"] not in {"issue_comment", "review_comment"}
            or type(source["id"]) is not int
            or not 0 < source["id"] <= (1 << 53) - 1
            or type(source["updated_at"]) is not str
            or not 1 <= len(source["updated_at"]) <= 128
            or type(source["body_sha256"]) is not str
            or re.fullmatch(r"[a-f0-9]{64}", source["body_sha256"]) is None
        ):
            _hold("source_binding_required")
        if (
            item["purpose"] not in {"command", "reply"}
            or type(item["operation_key"]) is not str
            or not 1 <= len(item["operation_key"]) <= 256
            or item["operation_key"] in seen
            or type(item["made_progress"]) is not bool
        ):
            _hold("invalid_record")
        for name in ("target_base_sha", "target_head_sha"):
            target = item[name]
            if target is not None and (
                type(target) is not str or re.fullmatch(r"[a-f0-9]{40}", target) is None
            ):
                _hold("invalid_operation_target")
        seen.add(item["operation_key"])
        _validate_snapshot(item["snapshot"])
    trailing = 0
    for item in reversed(value["records"]):
        if item["made_progress"]:
            break
        trailing += 1
    if value["streak"] != trailing:
        _hold("inconsistent_streak")
    if len(canonical_bytes(value)) > 8192:
        _hold("capacity_exceeded")
    return value


def append_history(
    previous,
    identity,
    intent,
    before,
    after,
    *,
    target_base_sha=None,
    disposition_entry=False,
):
    if type(disposition_entry) is not bool:
        _hold("invalid_record")
    _validate_snapshot(before)
    _validate_snapshot(after)
    records = (
        []
        if previous is None
        else list(validate_history(previous, identity)["records"])
    )
    if any(r["operation_key"] == intent["operation_key"] for r in records):
        _hold("duplicate_settlement")
    progress = semantic_advancement(before, after)
    # Authenticated improvement since the last operation also breaks a streak.
    if records:
        progress = progress or semantic_advancement(records[-1]["snapshot"], after)
    streak = 0 if progress else min(LIMIT, (previous["streak"] if previous else 0) + 1)
    entry = {
        "operation_key": intent["operation_key"],
        "purpose": intent["purpose"],
        "source": dict(intent["source"]),
        "snapshot": after,
        "made_progress": progress,
        "target_base_sha": target_base_sha,
        "target_head_sha": intent.get("head_sha"),
    }
    if disposition_entry and intent["purpose"] == "command" and not progress:
        entry["disposition_entry"] = True
    records.append(entry)
    value = {
        "schema_version": VERSION,
        "repository_id": identity.repository_id,
        "pull_request": identity.pull_request,
        "streak": streak,
        "records": records[-LIMIT:],
    }
    return validate_history(value, identity)


def capture_selected(ledger, identity, *, include_historical=True):
    from review_sensei.finding_lifecycle import decode_lifecycle_state
    from review_sensei.finding_lifecycle_store import (
        read_finding_lifecycle,
        select_finding_lifecycle,
    )
    from review_sensei.session import SessionLoadResult

    from . import current_command_execution
    from .api_run_requests import ApiRunRequestTransport, ApiRunSessionLedger
    from .application import GitHubApplication
    from .finding_lifecycle import _require_effect_scope, _unpack_journal, _validate
    from .finding_lifecycle_dictionary import resolve_stored
    from .publication_recovery import _publication_transport
    from .quarantine_origin import (
        has_paged_v2_disposition_snapshot,
        rebind_same_lineage_owner,
    )

    selected: Any = None
    packed: Any = None
    if getattr(ledger, "_quarantine_required", False) is True and isinstance(
        ledger, ApiRunSessionLedger
    ):
        caller = getattr(ledger, "caller", None)
        authenticated = getattr(ledger, "_quarantine_authenticated", None)
        effect_record = (
            getattr(caller, "_effect_record", None)
            if type(caller) is ApiRunRequestTransport
            else None
        )
        guard = getattr(
            _publication_transport(ledger.http), "_quarantine_capacity_guard", None
        )
        if (
            caller is not None
            and getattr(ledger, "_inventory_complete", False) is True
            and (
                (guard is None and len(ledger._cached_comments()) >= 80)
                or (
                    type(caller.accounting.configuration.limit) is int
                    and caller.accounting.configuration.limit >= 1152
                    and has_paged_v2_disposition_snapshot(effect_record)
                    and caller.accounting.action is not None
                    and caller.accounting.action.get("phase") == "pending"
                    and caller.accounting.action.get("execution_started") is True
                    and (
                        guard is None
                        or current_command_execution._frame(
                            GitHubApplication.apply_maintainer_command
                        )
                        is not None
                    )
                )
            )
            and caller._checkpoint is None
            and effect_record is not None
            and effect_record.record_sha256 == caller._root
            and authenticated is not None
            and effect_record.quarantine_lineage is not None
            and canonical_bytes(effect_record.quarantine_lineage)
            == authenticated.lineage_bytes
        ):
            try:
                rebind_same_lineage_owner(
                    authenticated, ledger, caller, identity, effect_record
                )
            except ReviewInputError:
                pass
            else:
                selected = SessionLoadResult(status="ok", record=effect_record)
                if effect_record.finding_lifecycle is None:
                    packed = None
                else:
                    prior_reuse = getattr(ledger, "_reuse_listed_parts", False)
                    ledger._reuse_listed_parts = True
                    try:
                        packed = read_finding_lifecycle(
                            ledger, effect_record, _selected_load=selected
                        )
                        packed = resolve_stored(ledger, effect_record, packed)
                    finally:
                        ledger._reuse_listed_parts = prior_reuse
    resolved = selected is not None
    if not resolved:
        selected, packed = select_finding_lifecycle(ledger, identity)
    if selected.record is None or packed is None:
        _hold("complete_inventory_required")
    record = selected.record
    if not resolved:
        packed = resolve_stored(ledger, record, packed)
    document = _validate(_unpack_journal(packed))
    state = decode_lifecycle_state(document["lifecycle"]).state
    binding = record.finding_lifecycle["manifest"]["binding"]
    if state.base_sha != binding["base_sha"] or state.head_sha != binding["head_sha"]:
        _hold("snapshot_binding_conflict")
    _require_effect_scope(document, identity, state.head_sha)
    producer = binding["producer"]
    for receipt in document["native_receipts"]:
        if (receipt["repository_id"], receipt["pull_request"]) != (
            identity.repository_id,
            identity.pull_request,
        ) or (
            producer.startswith("github-bot:")
            and str(receipt["producer_id"]) != producer.split(":", 1)[1]
        ):
            _hold("receipt_scope_conflict")
    snapshot = semantic_snapshot(state, document)
    return record, (
        historical_snapshot(snapshot, ledger, record)
        if include_historical
        else snapshot
    )


@dataclass(frozen=True)
class SemanticContext:
    before: dict[str, Any]
    previous: dict[str, Any] | None
    identity: Any
    reference_sha256: str
    target_base_sha: str
    finding_before: dict[str, Any] | None = None


def _prepare_action(run):
    """Read actual retained data before admitting new work, never supplied history."""
    from review_sensei.api_work import ApiRunAccounting

    from .api_run_history import read_action_result, read_previous_run

    record, finding_before = capture_selected(
        run.ledger, run.identity, include_historical=False
    )
    snapshot = historical_snapshot(finding_before, run.ledger, record)
    if record.record_sha256 != run.caller._root:
        _hold("selected_root_changed")
    state = ApiRunAccounting.from_document(record.api_run_accounting)
    previous = None
    old_action_for_dup: Mapping[str, Any] | None = None
    old_record_for_dup: Any = None
    if hasattr(run.caller, "_accepted_semantic_predecessor"):
        previous = run.caller._accepted_semantic_predecessor
    elif state.previous_record is not None:
        # One exact immutable predecessor, not an ancestor census or search.
        old = read_previous_run(run.ledger, run.identity, state.previous_record)
        old_record_for_dup = old
        old_state = ApiRunAccounting.from_document(old.api_run_accounting)
        if old_state.action is None:
            previous = history_from_record(run.ledger, run.identity, record)
        else:
            old_action_for_dup = old_state.action if state.action is None else None
            retained_old = read_action_result(run.ledger, run.identity, old)
            previous = retained_old["result"].get(KEY)
            if previous is None:
                if action_allows_carried_history_fallback(
                    old_state.action, retained_old
                ):
                    previous = history_from_record(run.ledger, run.identity, old)
                else:
                    _hold("missing_predecessor_summary")
    if previous is not None:
        if isinstance(previous, dict) and (
            previous == {"missing": True}
            or previous.get("qualification") == "unavailable"
        ):
            _check_duplicate_terminal_delivery(
                run.caller,
                previous,
                old_action=old_action_for_dup,
                record=record,
                old_record=old_record_for_dup,
            )
            _hold("missing_predecessor_summary")
        validate_history(previous, run.identity)
        _check_duplicate_terminal_delivery(
            run.caller,
            previous,
            old_action=old_action_for_dup,
            record=record,
            old_record=old_record_for_dup,
        )
        # Pending native publication/result recovery precedes refusal. Never
        # replace its original liability with new work or report convergence.
        pending = (
            snapshot["pending_count"]
            or snapshot["unknown_trust_effect"]
            or _record_has_pending_receipt(record)
            or (state.action is not None and state.action["phase"] == "pending")
        )
        if (
            previous["streak"] >= LIMIT
            and not pending
            and not _disposition_entry_allowed(run.caller, previous)
            and not semantic_advancement(previous["records"][-1]["snapshot"], snapshot)
        ):
            _hold("finite_no_progress_stop")
    elif old_action_for_dup is not None:
        _check_duplicate_terminal_delivery(
            run.caller,
            None,
            old_action=old_action_for_dup,
            record=record,
            old_record=old_record_for_dup,
        )
    run.caller._accepted_semantic_context = SemanticContext(
        snapshot,
        previous,
        run.identity,
        _digest(record.finding_lifecycle),
        run.base_sha,
        finding_before,
    )


@dataclass(frozen=True)
class UnqualifiedContext:
    reason: str
    identity: Any


def prepare_action(run, *, allow_unqualified=False, allow_missing_predecessor=False):
    try:
        _prepare_action(run)
    except ReviewInputError as error:
        if isinstance(error, DuplicateTerminalDelivery):
            raise
        # Existing executors still own reviewed-snapshot and finalization gates.
        # Preserve their ordinary outcomes only when no owned finding_lifecycle
        # exists yet (`complete_inventory_required`). When an owned
        # finding_lifecycle exists, missing_predecessor_summary refuses fail-closed.
        permitted = {"accepted_semantic_history_complete_inventory_required"}
        if allow_missing_predecessor:
            permitted.add("accepted_semantic_history_missing_predecessor_summary")
        if not allow_unqualified or str(error) not in permitted:
            raise
        run.caller._accepted_semantic_context = UnqualifiedContext(
            str(error), run.identity
        )


def enrich_settlement(caller, ledger, identity, intent, result):
    context = getattr(caller, "_accepted_semantic_context", None)
    if context is None:
        return result  # Older direct executors are not semantic-qualified routes.
    if context.identity != identity or KEY in result:
        _hold("settlement_scope_conflict")
    if isinstance(context, UnqualifiedContext):
        return {
            **result,
            KEY: {
                "schema_version": VERSION,
                "qualification": "unavailable",
                "reason": context.reason,
            },
        }
    cache = getattr(caller, "_accepted_semantic_result_cache", None)
    if cache is not None and cache[:2] == (caller._root, _digest(result)):
        return cache[2]
    from review_sensei.bounded_evidence import read_inline_evidence
    from review_sensei.finding_lifecycle import decode_lifecycle_state
    from review_sensei.session import SessionRecord

    from .finding_lifecycle import _require_effect_scope, _unpack_journal, _validate

    owned = getattr(caller, "_effect_record", None)
    if not isinstance(owned, SessionRecord) or owned.record_sha256 != caller._root:
        _hold("owned_checkpoint_required")
    record = SessionRecord.from_dict(owned.to_dict())
    if record.identity() != identity or record.record_sha256 != caller._root:
        _hold("selected_root_changed")
    observation = getattr(caller, "_accepted_semantic_checkpoint", None)
    unchanged_lifecycle = _digest(record.finding_lifecycle) == context.reference_sha256
    if unchanged_lifecycle:
        after = (
            context.finding_before
            if context.finding_before is not None
            else context.before
        )
    elif (
        isinstance(observation, CheckpointSemantics)
        and observation.identity == identity
        and observation.reference_sha256 == _digest(record.finding_lifecycle)
    ):
        after = _validate_snapshot(json.loads(observation.snapshot))
    else:
        reference = record.finding_lifecycle
        manifest = reference.get("manifest") if isinstance(reference, dict) else None
        if (
            not isinstance(manifest, dict)
            or manifest.get("encoding") != "inline-json-v1"
        ):
            # Preserve the confirmed action result and earlier qualified history.
            # A new partitioned carrier needs its actual checkpoint producer to
            # issue a semantic snapshot; no unpriced post-effect census is added.
            return {
                **result,
                KEY: {
                    "schema_version": VERSION,
                    "qualification": "unavailable",
                    "reason": "changed_partitioned_checkpoint",
                    "prior": context.previous,
                },
            }
        document = _validate(
            _unpack_journal(
                read_inline_evidence(manifest, expected_binding=manifest["binding"])
            )
        )
        state = decode_lifecycle_state(document["lifecycle"]).state
        binding = manifest["binding"]
        if (
            state.base_sha != binding["base_sha"]
            or state.head_sha != binding["head_sha"]
        ):
            _hold("snapshot_binding_conflict")
        _require_effect_scope(document, identity, state.head_sha)
        after = semantic_snapshot(state, document)
    raw_after = after
    after = historical_snapshot(after, ledger, record)
    if context.previous is not None and _is_prewrite_non_dispositional_noop(
        result,
        unchanged_lifecycle=unchanged_lifecycle,
        raw_before=context.finding_before,
        raw_after=raw_after,
        before=context.before,
        after=after,
    ):
        enriched = {**result, KEY: context.previous}
        caller._accepted_semantic_result_cache = (
            caller._root,
            _digest(result),
            enriched,
        )
        return enriched
    # The existing close_action fresh expected-root CAS and readback authenticates
    # this exact internally issued checkpoint. This is reviewed carrier state,
    # not a claim that mutable native effects have been independently reread.
    history = append_history(
        context.previous,
        identity,
        intent,
        context.before,
        after,
        target_base_sha=context.target_base_sha,
        disposition_entry=_target_allows_disposition_entry(caller),
    )
    enriched = {**result, KEY: history}
    caller._accepted_semantic_result_cache = (caller._root, _digest(result), enriched)
    return enriched


def guard_successor_admission(caller, ledger, identity, record, retained):
    """Stop before successor archive/allocation using the already opened result."""
    return guard_carried_successor(
        caller, ledger, identity, record, retained["result"].get(KEY)
    )


def guard_carried_successor(caller, ledger, identity, record, history):
    if getattr(caller, "_accepted_semantic_target", None) is None:
        return
    from review_sensei.api_work import ApiRunAccounting

    old_action = (
        ApiRunAccounting.from_document(record.api_run_accounting).action
        if getattr(record, "api_run_accounting", None) is not None
        else None
    )
    unavailable = (
        isinstance(history, dict) and history.get("qualification") == "unavailable"
    )
    if history is not None and not unavailable:
        validate_history(history, identity)
    _check_duplicate_terminal_delivery(
        caller, history, old_action=old_action, record=record
    )
    if history is None or unavailable:
        return  # No semantic claim; the ordinary route handles missing history.
    if history["streak"] < LIMIT:
        return
    selected, snapshot = capture_selected(ledger, identity)
    if selected.record_sha256 != record.record_sha256:
        _hold("selected_root_changed")
    if (
        snapshot["pending_count"]
        or snapshot["unknown_trust_effect"]
        or _record_has_pending_receipt(record)
        or _record_has_pending_receipt(selected)
    ):
        return  # Original pending result reconciliation retains priority.
    if not _disposition_entry_allowed(caller, history) and not semantic_advancement(
        history["records"][-1]["snapshot"], snapshot
    ):
        _hold("finite_no_progress_stop")


@dataclass(frozen=True)
class CheckpointSemantics:
    identity: Any
    reference_sha256: str
    snapshot: bytes


def record_checkpoint_semantics(ledger, identity, record, document):
    """Observe exactly the committed producer bytes; failure preserves its result.

    This is bounded data, not a serialized execution or cessation capability.
    The committed evidence digest must equal the actual canonical v2 envelope of
    the supplied packed journal, for inline and partitioned carriers alike.
    """
    from review_sensei.finding_lifecycle import decode_lifecycle_state
    from review_sensei.session import SessionRecord

    from .finding_lifecycle import (
        _envelope_for_manifest,
        _require_effect_scope,
        _validate,
    )

    caller = getattr(ledger, "caller", None)
    if caller is None:
        return False
    try:
        if not isinstance(record, SessionRecord) or record.identity() != identity:
            return False
        owned = getattr(caller, "_effect_record", None)
        if (
            not isinstance(owned, SessionRecord)
            or owned.record_sha256 != record.record_sha256
            or record.record_sha256 != caller._root
        ):
            return False
        detached = SessionRecord.from_dict(record.to_dict())
        if detached.to_dict() != SessionRecord.from_dict(owned.to_dict()).to_dict():
            return False
        reference = detached.finding_lifecycle
        if reference is None:
            return False
        validated = _validate(document)
        manifest = reference["manifest"]
        if not isinstance(manifest, dict) or not isinstance(
            manifest.get("binding"), dict
        ):
            return False
        if _envelope_for_manifest(validated, manifest) is None:
            return False
        binding = manifest["binding"]
        if (
            binding["repository_id"] != identity.repository_id
            or binding["pull_request"] != identity.pull_request
            or binding["repository"] != identity.repository
        ):
            return False
        state = decode_lifecycle_state(validated["lifecycle"]).state
        if (
            state.base_sha != binding["base_sha"]
            or state.head_sha != binding["head_sha"]
        ):
            return False
        _require_effect_scope(validated, identity, state.head_sha)
        for receipt in validated["native_receipts"]:
            if (receipt["repository_id"], receipt["pull_request"]) != (
                identity.repository_id,
                identity.pull_request,
            ) or (
                binding["producer"].startswith("github-bot:")
                and str(receipt["producer_id"]) != binding["producer"].split(":", 1)[1]
            ):
                return False
        snapshot = canonical_bytes(semantic_snapshot(state, validated))
        _validate_snapshot(json.loads(snapshot))
        caller._accepted_semantic_checkpoint = CheckpointSemantics(
            identity, _digest(reference), snapshot
        )
        return True
    except (ReviewInputError, KeyError, TypeError, ValueError):
        return False


def history_from_record(ledger, identity, record):
    """Read carried history from the already-selected root, no ancestor census."""
    from review_sensei.bounded_evidence import read_inline_evidence
    from review_sensei.finding_lifecycle_store import read_finding_lifecycle
    from review_sensei.session import SessionRecord

    from .finding_lifecycle import _unpack_journal, _validate

    record = SessionRecord.from_dict(record.to_dict())
    if record.identity() != identity:
        _hold("foreign_scope")
    reference = record.finding_lifecycle
    if reference is None:
        return None
    if KEY in reference:
        history = reference[KEY]
        return (
            history
            if history.get("qualification") == "unavailable"
            else validate_history(history, identity)
        )
    manifest = reference["manifest"]
    if manifest["encoding"] == "inline-json-v1":
        envelope = read_inline_evidence(manifest, expected_binding=manifest["binding"])
        if (
            not isinstance(envelope, dict)
            or envelope.get("schema_version") != "finding-lifecycle-evidence-v2"
        ):
            _hold("unsupported")
        packed = envelope["lifecycle"]
    else:
        packed = read_finding_lifecycle(ledger, record)
    from .finding_lifecycle_dictionary import resolve_stored

    packed = resolve_stored(ledger, record, packed)
    history = _validate(_unpack_journal(packed)).get(KEY)
    return None if history is None else validate_history(history, identity)


def carry_history_in_rollover(ledger, identity, previous, candidate, history):
    """Amend only bounded reference metadata from the exact terminal result.

    Finding evidence bytes, binding and lineage remain unchanged, even when the
    finding carrier is partitioned. The live core verifier independently echoes
    the committed INLINE action result; caller-supplied history is not proof.
    """
    from datetime import datetime

    from review_sensei.api_work import ApiRunAccounting
    from review_sensei.finding_lifecycle_store import (
        reference_with_terminal_action_history,
    )

    if (
        previous.finding_lifecycle is None
        or ApiRunAccounting.from_document(previous.api_run_accounting).action is None
    ):
        return candidate
    reference = reference_with_terminal_action_history(previous)
    if reference == previous.finding_lifecycle:
        return candidate
    return candidate.evolve(
        now=datetime.fromisoformat(candidate.updated_at.replace("Z", "+00:00")),
        generation=candidate.generation,
        finding_lifecycle=reference,
    )


def prospective_history(caller, identity, intent):
    """Pure width witness only. Never retained or used as semantic authority."""
    context = getattr(caller, "_accepted_semantic_context", None)
    if context is None:
        return None
    if isinstance(context, UnqualifiedContext):
        return {
            "schema_version": VERSION,
            "qualification": "unavailable",
            "reason": context.reason,
        }
    snapshot = dict(context.before)
    for key, value in snapshot.items():
        if type(value) is bool:
            snapshot[key] = False
        elif type(value) is int:
            snapshot[key] = 100000
    row = {
        "operation_key": intent["operation_key"],
        "purpose": intent["purpose"],
        "source": dict(intent["source"]),
        "snapshot": snapshot,
        "made_progress": False,
        "target_base_sha": context.target_base_sha,
        "target_head_sha": intent.get("head_sha"),
    }
    if _target_allows_disposition_entry(caller) and intent["purpose"] == "command":
        row["disposition_entry"] = True
    previous = context.previous
    rows = ([] if previous is None else list(previous["records"][-1:])) + [row]
    streak = 0
    for item in reversed(rows):
        if item["made_progress"]:
            break
        streak += 1
    qualified = validate_history(
        {
            "schema_version": VERSION,
            "repository_id": identity.repository_id,
            "pull_request": identity.pull_request,
            "streak": streak,
            "records": rows,
        },
        identity,
    )
    unavailable = {
        "schema_version": VERSION,
        "qualification": "unavailable",
        "reason": "changed_partitioned_checkpoint",
        "prior": context.previous,
    }
    return max((qualified, unavailable), key=lambda value: len(canonical_bytes(value)))


def history_reference_reserve(record, history):
    """Exact duplicated reference metadata width, with the original 8 KiB cap."""
    from review_sensei.finding_lifecycle_store import (
        HISTORY_REFERENCE_VERSION,
        normalize_reference,
    )

    if record is None or record.finding_lifecycle is None or history is None:
        return 0
    old = record.finding_lifecycle
    new = normalize_reference(
        {**old, "schema_version": HISTORY_REFERENCE_VERSION, KEY: history}
    )
    return max(0, len(canonical_bytes(new)) - len(canonical_bytes(old)))


def preflight_history_capacity(caller, identity, intent):
    """Reject unrepresentable metadata before marking any native effect started."""
    from review_sensei.finding_lifecycle_store import (
        HISTORY_REFERENCE_VERSION,
        require_prospective_terminal_history_capacity,
    )

    record = getattr(caller, "_effect_record", None)
    history = prospective_history(caller, identity, intent)
    reserve = history_reference_reserve(record, history)
    # Pure, invocation-local sizing data. Core rechecks current owned root and
    # admitted intent before using it; it never supplies semantic authority.
    caller._accepted_semantic_capacity = None
    if record is not None and history is not None:
        caller._accepted_semantic_capacity = {
            "intent_sha256": _digest(intent),
            "initial_root": record.record_sha256,
            "operation_id": record.finding_lifecycle["operation_id"]
            if record.finding_lifecycle
            else None,
            "history": json.loads(canonical_bytes(history)),
        }
    if record is not None and history is not None:
        # Width only: no fake manifest is decoded, persisted, or granted authority.
        # The existing terminal planner still prices actual business-root changes.
        raw = json.loads(canonical_bytes(record.to_dict()))
        if record.finding_lifecycle is not None:
            raw["finding_lifecycle"] = {
                **record.finding_lifecycle,
                "schema_version": HISTORY_REFERENCE_VERSION,
                KEY: history,
            }
        require_prospective_terminal_history_capacity(raw)
    return reserve


from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
