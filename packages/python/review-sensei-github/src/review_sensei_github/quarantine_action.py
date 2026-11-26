"""Exact individual carried-assessment decisions from a current publication."""

from __future__ import annotations

import hashlib
import json
import re
import sys
import weakref
from collections import Counter
from dataclasses import dataclass
from typing import Any, Mapping, cast
from urllib.parse import quote

from review_sensei.bounded_evidence import (
    MAX_PART_BYTES,
    MAX_STORED_PART_BYTES,
    MAX_STORED_PARTS,
    canonical_bytes,
    evidence_manifest,
    partition_evidence,
    read_inline_evidence,
)
from review_sensei.disposition import (
    MaintainerCommandResult,
    OverrideReceipt,
    authorized_override,
)
from review_sensei.errors import ReviewInputError
from review_sensei.quarantine_state import assessment_cursor, next_assessment_event

from .approval import ReviewApprovalEligibility
from .default_human_reader import transaction_publication_digest
from .publication import (
    APPROVAL_ELIGIBILITY_MARKER_PREFIX,
    approval_eligibility_from_body,
    parse_finding_index,
)
from .run_authority import VerifiedRunAction


@dataclass(frozen=True)
class QuarantineCommand:
    action: str
    scope: str
    reference: str
    reason: str
    submitted_reason: str
    finding_reference: None = None
    finding_references: tuple = ()


def parse_quarantine_command(body):
    from review_sensei.disposition import parse_maintainer_command

    parsed = parse_maintainer_command(body, actor="untrusted")
    if parsed is None:
        if isinstance(body, str) and re.search(
            r"--reason[ \t]+[\"']?quarantine[:/@_-]", body, re.IGNORECASE | re.ASCII
        ):
            raise ReviewInputError(
                "quarantine command must use the complete supported finding syntax"
            )
        return None
    if (
        re.match(r"quarantine[:/@_-]", parsed.reason or "", re.IGNORECASE | re.ASCII)
        is None
    ):
        return None
    match = re.fullmatch(
        r"quarantine:([a-f0-9]{64}) (.+)", parsed.reason or "", re.ASCII
    )
    if (
        match is None
        or parsed.finding_fingerprint is None
        or re.fullmatch(r"[0-9a-fA-F]{64}", parsed.finding_fingerprint) is None
        or parsed.action not in {"dismiss", "accept-risk"}
    ):
        raise ReviewInputError(
            "quarantine assessment requires an exact origin scope and full instance fingerprint"
        )
    scope, reason = match.groups()
    if (
        not 16 <= len(reason.encode()) <= 4096
        or not reason.isprintable()
        or len((parsed.reason or "").encode()) > 4096
    ):
        raise ReviewInputError(
            "quarantine assessment requires a substantive bounded reason"
        )
    return QuarantineCommand(
        parsed.action,
        scope,
        "RS-" + parsed.finding_fingerprint.upper(),
        reason,
        parsed.reason or "",
    )


class QuarantineTargetRefusal(ReviewInputError):
    """Definite read-only target/permission refusal before a decision write."""


@dataclass(frozen=True)
class QuarantineTarget:
    reader: object
    authenticated: object
    instance: str
    permission: str
    lifecycle_transfer: object | None = None
    capacity_projection: object | None = None
    native_review_bytes: bytes | None = None
    command: QuarantineCommand | None = None
    native_frontier_bytes: bytes | None = None


def decision_authority(run, authority):
    """Fresh source/head/base/policy/maintainer checks before each decision effect."""
    origin_reader = getattr(run.ledger, "_quarantine_origin_reader", None)
    if origin_reader is None:
        raise ReviewInputError("quarantine decision provenance reader is absent")
    current_source = authority.fence(
        validate_pull=origin_reader.current_scope.validate_pull
    )
    source_user = current_source.get("user")
    if (
        not isinstance(source_user, dict)
        or type(source_user.get("id")) is not int
        or source_user["id"] < 1
    ):
        raise ReviewInputError(
            "quarantine decision source numeric actor is unavailable"
        )
    branch = getattr(run.ledger, "_quarantine_policy_branch", None)
    if not isinstance(branch, str) or not branch:
        raise ReviewInputError("quarantine current policy branch is absent")
    actor = authority.document["actor"]["login"]
    token = origin_reader.metadata_token

    def get(suffix):
        status, value = run.http.request(
            "GET",
            run.http.repository_path(run.identity.repository, suffix),
            token=token,
        )
        if status != 200 or not isinstance(value, dict):
            raise ReviewInputError("quarantine decision authority is unavailable")
        return value

    permission = get(f"/collaborators/{actor}/permission")
    role, user = permission.get("permission"), permission.get("user")
    if role == "write" and permission.get("role_name") == "maintain":
        role = "maintain"
    if (
        role not in {"maintain", "admin"}
        or not isinstance(user, dict)
        or user.get("login") != actor
        or user.get("type") != "User"
        or type(user.get("id")) is not int
        or user["id"] != source_user["id"]
    ):
        raise ReviewInputError("quarantine current maintainer authority was revoked")
    reference = get("/git/ref/heads/" + quote(branch, safe="/"))
    obj = reference.get("object")
    if (
        not isinstance(obj, dict)
        or obj.get("type") != "commit"
        or obj.get("sha") != run.caller.accounting.configuration.commit
    ):
        raise ReviewInputError("quarantine current policy changed")
    return role


# These guards expose a closed four-read cost, not a substitute for fresh
# maintainer/source/head/policy authentication on every existing invocation.
_DECISION_FENCES: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_VERIFIED_ACTION_FENCE = VerifiedRunAction.fence
_DECISION_AUTHORITY = decision_authority


@dataclass(frozen=True, eq=False)
class QuarantineDecisionFence:
    run: Any
    authority: VerifiedRunAction

    def __call__(self):
        if _decision_fence_scope(self) is None:
            raise ReviewInputError("quarantine decision fence source changed")
        role = decision_authority(self.run, self.authority)
        _seal_pending_decision_fence(self, role)
        return role


_DECISION_FENCE_CALL = QuarantineDecisionFence.__call__


def _action_fence_scope(ledger, authority):
    if type(authority) is not VerifiedRunAction:
        return None
    run = authority.run
    if ledger.caller._effect_record is None:
        return None
    if (
        run.ledger is not ledger
        or run.caller is not ledger.caller
        or run.identity != ledger.caller._effect_record.identity()
        or authority.owner != ledger.caller.accounting.owner
        or not isinstance(authority.document, dict)
        or run.head_sha != authority.document.get("head_sha")
        or getattr(authority.fence, "__func__", None) is not _VERIFIED_ACTION_FENCE
    ):
        return None
    return canonical_bytes(authority.document)


def _decision_fence_binding(run, authority, scope):
    reader = getattr(run.ledger, "_quarantine_origin_reader", None)
    current = None if reader is None else reader.current_scope
    if reader is None or current is None:
        return None
    return (
        id(run),
        id(authority),
        scope,
        id(run.http),
        id(run.ledger),
        id(run.caller),
        id(reader),
        id(current),
        reader.metadata_token,
        canonical_bytes(
            {
                "run": vars(run.caller.run),
                "identity": vars(run.identity),
                "head_sha": run.head_sha,
                "base_sha": run.base_sha,
                "scope_head": current.head_sha,
                "scope_base": current.base_sha,
                "policy_branch": getattr(run.ledger, "_quarantine_policy_branch", None),
            }
        ),
    )


def _decision_fence_scope(guard):
    if type(guard) is not QuarantineDecisionFence:
        return None
    run, authority = guard.run, guard.authority
    from .quarantine_evidence import CurrentReadScope

    reader = getattr(run.ledger, "_quarantine_origin_reader", None)
    scope = _action_fence_scope(run.ledger, authority)
    if (
        scope is None
        or authority.run is not run
        or reader is None
        or type(reader.current_scope) is not CurrentReadScope
        or getattr(reader.current_scope.validate_pull, "__func__", None)
        is not CurrentReadScope.validate_pull
        or getattr(guard.__call__, "__func__", None) is not _DECISION_FENCE_CALL
        or decision_authority is not _DECISION_AUTHORITY
        or _DECISION_FENCES.get(guard) != _decision_fence_binding(run, authority, scope)
    ):
        return None
    return scope


def issue_decision_fence(run, authority):
    scope = _action_fence_scope(run.ledger, authority)
    if scope is None or authority.run is not run:
        raise ReviewInputError("quarantine decision fence owner is unsupported")
    guard = QuarantineDecisionFence(run, authority)
    _DECISION_FENCES[guard] = _decision_fence_binding(run, authority, scope)
    if _decision_fence_scope(guard) is None:
        raise ReviewInputError("quarantine decision fence source is unsupported")
    return guard


def decision_fence_capacity(ledger):
    """Known source call counts only; arbitrary callbacks have no cheaper price."""
    from .review_run import ActionRunLedger

    guard = getattr(ledger, "business_guard", None)
    if type(ledger) is not ActionRunLedger:
        return (0, None) if guard is None else None
    if type(guard) is QuarantineDecisionFence:
        if guard.run.ledger is not ledger:
            return None
        scope = _decision_fence_scope(guard)
        return (4, (id(guard), scope.decode())) if scope is not None else None
    authority = getattr(guard, "__self__", None)
    if getattr(guard, "__func__", None) is not _VERIFIED_ACTION_FENCE:
        return None
    scope = _action_fence_scope(ledger, authority)
    return (2, (id(authority), scope.decode())) if scope is not None else None


def _current_collision_pairs(retained, state, head_sha):
    """Pure identity projection; caller must authenticate result and full state."""
    from review_sensei.active_finding_window import (
        ActiveWindowRoundState,
        obligation_from_comment,
    )
    from review_sensei.context import finding_lifecycle_for_comment
    from review_sensei.finding_drift import drift_obligation_from_comment
    from review_sensei.human_assessment import finding_instance_fingerprint

    window = isinstance(state, ActiveWindowRoundState)
    if isinstance(state, ActiveWindowRoundState):
        unresolved = {
            (item.obligation_id, item.resolution_criterion)
            for item in state.authoritative_decision_inventory.obligations
            if item.unresolved
        }
    else:
        unresolved = {
            (item.criterion.obligation_id, item.criterion.resolution_criterion)
            for item in state.inventory.obligations
            if item.unresolved
        }
    # Full immutable state includes hidden and omitted original obligations.
    # Latest provider output is not a complete inventory and cannot erase them.
    pairs: set[tuple[str, str | None]] = set(unresolved)
    for comment in retained.comments:
        criterion = (
            obligation_from_comment(comment)
            if window
            else drift_obligation_from_comment(comment, head_sha=head_sha).criterion
        )
        if (criterion.obligation_id, criterion.resolution_criterion) in unresolved:
            pairs.add(
                (
                    finding_instance_fingerprint(comment),
                    finding_lifecycle_for_comment(comment).concern,
                )
            )
    return pairs


def _state_before_post_publication_overrides(state, document, record, head_sha):
    """Project the round's initial publication state before command overrides."""
    from dataclasses import replace

    from review_sensei.active_finding_window import (
        ActiveWindowRoundState,
        AuthoritativeDecisionInventory,
        select_display_active_window,
    )
    from review_sensei.finding_drift import (
        BODY_PLACEMENT,
        INLINE_PLACEMENT,
        DriftActiveEntry,
        DriftInventory,
        _recompute_drift_verdict,
    )

    is_window = isinstance(state, ActiveWindowRoundState)
    obligations = (
        state.authoritative_decision_inventory.obligations
        if is_window
        else state.inventory.obligations
    )
    current_generation = max(0, state.round_number - 1)
    receipt_threads = {
        receipt["thread_id"]
        for receipt in document.get("native_receipts", ())
        if isinstance(receipt, dict)
        and receipt.get("head_sha") == head_sha
        and isinstance(receipt.get("thread_id"), str)
        and receipt["thread_id"]
    }
    from .finding_lifecycle import _KNOWN_OVERRIDE_ACTORS

    actors = {
        item["actor"].strip()
        for item in record.dispositions
        if isinstance(item, dict)
        and isinstance(item.get("actor"), str)
        and item["actor"].strip()
    } | set(_KNOWN_OVERRIDE_ACTORS)
    if isinstance(record.disposition_snapshot, dict):
        snap_manifest = record.disposition_snapshot.get("state_manifest")
        if (
            isinstance(snap_manifest, dict)
            and snap_manifest.get("encoding") == "inline-json-v1"
            and isinstance(snap_manifest.get("binding"), Mapping)
        ):
            snap_state = read_inline_evidence(
                snap_manifest,
                expected_binding=cast(Mapping[str, object], snap_manifest["binding"]),
            )
            if isinstance(record.disposition_snapshot.get("result_digest"), str):
                from review_sensei.disposition_snapshot import _unpack_page_index

                snap_state = _unpack_page_index(
                    snap_state,
                    expected_binding=cast(
                        Mapping[str, object], snap_manifest["binding"]
                    ),
                    result_digest=record.disposition_snapshot["result_digest"],
                )
            if isinstance(snap_state, dict):
                for rows_key in ("dispositions", "tail"):
                    rows = snap_state.get(rows_key)
                    if isinstance(rows, list):
                        for item in rows:
                            if (
                                isinstance(item, dict)
                                and isinstance(item.get("actor"), str)
                                and item["actor"].strip()
                            ):
                                actors.add(item["actor"].strip())
    overridden_ids: set[str] = set()
    for ob in obligations:
        if ob.lifecycle_state != "authorized-override":
            continue
        target_thread = (
            ob.native_target
            if is_window
            else (
                ob.thread_binding.thread_id if ob.thread_binding is not None else None
            )
        )
        matched = ob.generation == current_generation or (
            target_thread is not None and target_thread in receipt_threads
        )
        if not matched and actors and ob.resolution_proof_sha256 is not None:
            for actor in actors:
                for slot_released in (True, False):
                    if is_window:
                        proof = hashlib.sha256(
                            canonical_bytes(
                                {
                                    "kind": "command-override",
                                    "obligation_id": ob.obligation_id,
                                    "actor": actor,
                                    "head_sha": head_sha,
                                    "slot_released": slot_released,
                                }
                            )
                        ).hexdigest()
                    else:
                        proof = hashlib.sha256(
                            b"reviewsensei:drift-command-override:v1:"
                            + canonical_bytes(
                                {
                                    "obligation_id": ob.obligation_id,
                                    "actor": actor,
                                    "head_sha": head_sha,
                                    "slot_released": slot_released,
                                }
                            )
                        ).hexdigest()
                    if ob.resolution_proof_sha256 == proof:
                        matched = True
                        break
                if matched:
                    break
        if matched:
            overridden_ids.add(ob.obligation_id)
    if not overridden_ids:
        return state
    if is_window:
        restored_window_obs = tuple(
            replace(
                ob,
                lifecycle_state="active-unresolved",
                resolution_proof_sha256=None,
                target_state=("active" if ob.native_target is not None else None),
            )
            if ob.obligation_id in overridden_ids
            else ob
            for ob in obligations
        )
        restored_targets = {
            ob.native_target
            for ob in restored_window_obs
            if ob.obligation_id in overridden_ids and ob.native_target is not None
        }
        restored_window_slots = tuple(
            replace(slot, state="active") if slot.target in restored_targets else slot
            for slot in state.display_active_findings.slots
        )
        restored_window_inv = AuthoritativeDecisionInventory(
            obligations=restored_window_obs
        )
        restored_win = select_display_active_window(
            restored_window_inv,
            slots=restored_window_slots,
            max_active_findings=state.display_active_findings.max_active_findings,
        )
        return replace(
            state,
            authoritative_decision_inventory=restored_window_inv,
            display_active_findings=restored_win,
        )
    restored_drift_obs = tuple(
        replace(
            ob,
            lifecycle_state="active-unresolved",
            resolution_proof_sha256=None,
            thread_binding=(
                replace(
                    ob.thread_binding, slot_state="active", host_marked_resolved=False
                )
                if ob.thread_binding is not None
                else None
            ),
        )
        if ob.obligation_id in overridden_ids
        else ob
        for ob in obligations
    )
    restored_drift_inv = DriftInventory(obligations=restored_drift_obs)
    restored_drift_slots = tuple(
        ob.thread_binding.to_slot()
        for ob in restored_drift_obs
        if ob.thread_binding is not None
    )
    existing_by_id = {entry.obligation_id: entry for entry in state.active_entries}
    reserved_count = sum(slot.reserved for slot in restored_drift_slots)
    incumbents = [
        item
        for item in sorted(
            restored_drift_inv.unresolved_obligations, key=lambda o: o.priority_key
        )
        if item.thread_binding is not None
        and item.thread_binding.slot_state in {"active", "contingent"}
    ]
    challengers = [
        item
        for item in sorted(
            restored_drift_inv.unresolved_obligations, key=lambda o: o.priority_key
        )
        if item.thread_binding is None
        or item.thread_binding.slot_state not in {"active", "contingent", "uncertain"}
    ]
    selected: list[Any] = list(incumbents)
    for item in challengers:
        if reserved_count >= state.max_active_findings:
            break
        selected.append(item)
        reserved_count += 1
    entries: list[DriftActiveEntry] = []
    for item in selected:
        existing = existing_by_id.get(item.obligation_id)
        if existing is not None:
            entries.append(existing)
            continue
        if item.thread_binding is not None:
            placement = item.thread_binding.placement
            body_reason = item.thread_binding.body_reason
            thread_id: str | None = item.thread_binding.thread_id
            thread_action = "update"
        else:
            can_inline = (
                item.present_anchor.inline_anchorable
                and item.present_anchor.path is not None
                and item.present_anchor.line is not None
                and item.present_anchor.side in {"LEFT", "RIGHT"}
                and not document.get("native_receipts")
            )
            placement = INLINE_PLACEMENT if can_inline else BODY_PLACEMENT
            body_reason = None if can_inline else "unanchored-file"
            thread_id = None
            thread_action = "body-only"
        entries.append(
            DriftActiveEntry(
                obligation_id=item.obligation_id,
                original_fingerprint=item.criterion.original_fingerprint,
                origin_path=item.criterion.origin_path,
                present_path=item.present_anchor.path,
                present_line=item.present_anchor.line,
                present_side=item.present_anchor.side,
                drift_kind=item.present_anchor.drift_kind,
                lifecycle_state=item.lifecycle_state,
                placement=placement,
                body_reason=body_reason,
                thread_id=thread_id,
                thread_action=thread_action,
            )
        )
    restored_entries = tuple(entries)
    approved, reasons, steps = _recompute_drift_verdict(
        coverage_state=state.changed_file_coverage,
        review_complete=state.review_complete,
        inventory=restored_drift_inv,
        active_entries=restored_entries,
        slots=restored_drift_slots,
    )
    return replace(
        state,
        inventory=restored_drift_inv,
        active_entries=restored_entries,
        slots=restored_drift_slots,
        approved=approved,
        withheld_reasons=reasons,
        recovery_steps=steps,
    )


def _current_publication_digest(
    ledger, record, *, _collision_pairs=None, _capture=None, _transfer_capture=None
):
    """Bind a current display projection to its independently retained analysis.

    This is not a historical-origin selector. A command without a current
    publication transaction must establish that authority elsewhere, not borrow
    a predecessor's lifecycle as its current root.
    """
    transaction = record.transaction
    if transaction is None:
        raise ReviewInputError("current quarantine publication transaction is absent")
    if record.finding_lifecycle is None:
        return transaction_publication_digest(transaction)
    from dataclasses import replace

    from review_sensei.active_finding_window import (
        ActiveWindowRoundState,
        project_display_review_result,
    )
    from review_sensei.finding_drift import project_drift_review_result
    from review_sensei.finding_lifecycle import decode_lifecycle_state
    from review_sensei.finding_lifecycle_store import (
        _decode_selected_lifecycle,
        read_finding_lifecycle,
    )
    from review_sensei.models import ReviewResult
    from review_sensei.session import SessionLoadResult

    from .api_run_requests import ApiRunRequestTransport
    from .finding_lifecycle import _require_effect_scope, _unpack_journal, _validate
    from .finding_lifecycle_dictionary import resolve_stored
    from .publication import review_result_digest
    from .quarantine_origin import has_paged_v2_disposition_snapshot

    load = getattr(ledger, "load_retained_checkpoint_result", None)
    retained = (
        load(record.identity(), transaction=transaction, head_sha=transaction.head_sha)
        if callable(load)
        else None
    )
    if (
        not isinstance(retained, ReviewResult)
        or retained.transaction is None
        or not retained.transaction.logical_identity_matches(transaction)
        or retained.content_digest() != transaction.result_sha256
    ):
        raise ReviewInputError(
            "current quarantine retained publication source conflicts"
        )
    binding = record.finding_lifecycle["manifest"]["binding"]
    if any(
        binding[name] != getattr(transaction, name)
        for name in ("head_sha", "base_sha", "policy_digest", "configuration_digest")
    ):
        raise ReviewInputError("current quarantine lifecycle source scope conflicts")
    from .publication_recovery import _publication_transport

    caller = getattr(ledger, "caller", None)
    is_quarantine = bool(getattr(ledger, "_quarantine_required", False))
    is_partitioned = (
        isinstance(record.finding_lifecycle, dict)
        and isinstance(record.finding_lifecycle.get("manifest"), dict)
        and record.finding_lifecycle["manifest"].get("encoding") != "inline-json-v1"
    )
    cached_comments = (
        ledger._cached_comments()
        if is_quarantine and getattr(ledger, "_inventory_complete", False)
        else ()
    )
    cached_body_wire = (
        sum(
            len(canonical_bytes({"body": item["body"]}))
            for item in cached_comments
            if isinstance(item, dict) and isinstance(item.get("body"), str)
        )
        if cached_comments
        else 0
    )
    ledger_http = getattr(ledger, "http", None)
    is_unguarded = (
        getattr(_publication_transport(ledger_http), "_quarantine_capacity_guard", None)
        is None
        if is_quarantine and ledger_http is not None
        else True
    )
    authenticated = getattr(ledger, "_quarantine_authenticated", None)
    is_v3_guarded = (
        is_quarantine
        and not is_unguarded
        and authenticated is not None
        and isinstance(authenticated.archive.get("target_isolation"), dict)
        and authenticated.archive["target_isolation"].get("contract")
        == "failed-published-tail-isolation-v1"
    )
    is_paged_v2_guarded = (
        is_quarantine
        and isinstance(caller, ApiRunRequestTransport)
        and type(caller.accounting.configuration.limit) is int
        and caller.accounting.configuration.limit >= 1152
        and has_paged_v2_disposition_snapshot(record)
    )
    reuse_selected_load = (
        not is_quarantine
        or is_partitioned
        or (not is_unguarded and cached_body_wire > 512 * 1024)
        or is_v3_guarded
        or (is_unguarded and len(cached_comments) >= 80)
        or is_paged_v2_guarded
    )
    resolved_stored = None
    if (
        reuse_selected_load
        and isinstance(caller, ApiRunRequestTransport)
        and type(caller) is ApiRunRequestTransport
        and caller._effect_record is not None
        and (
            record is caller._effect_record
            or (
                record.record_sha256 == caller._effect_record.record_sha256
                and canonical_bytes(record.to_dict())
                == canonical_bytes(caller._effect_record.to_dict())
            )
        )
        and record.record_sha256 == caller._root
        and getattr(ledger, "_inventory_complete", False)
        and caller._checkpoint is None
    ):
        selected_load = SessionLoadResult(status="ok", record=record)
        if is_quarantine:
            prior_reuse = getattr(ledger, "_reuse_listed_parts", False)
            ledger._reuse_listed_parts = True
            try:
                if is_paged_v2_guarded and not is_partitioned:
                    stored = _decode_selected_lifecycle(ledger, record)
                else:
                    stored = read_finding_lifecycle(
                        ledger, record, _selected_load=selected_load
                    )
                if (
                    is_v3_guarded
                    or is_paged_v2_guarded
                    or (is_unguarded and len(cached_comments) >= 80)
                ):
                    resolved_stored = resolve_stored(ledger, record, stored)
            finally:
                ledger._reuse_listed_parts = prior_reuse
        else:
            stored = read_finding_lifecycle(
                ledger, record, _selected_load=selected_load
            )
    else:
        stored = read_finding_lifecycle(ledger, record)
    if resolved_stored is None:
        resolved_stored = resolve_stored(ledger, record, stored)
    document = _validate(_unpack_journal(resolved_stored))
    _require_effect_scope(document, record.identity(), transaction.head_sha)
    state = decode_lifecycle_state(document["lifecycle"]).state
    if state.head_sha != transaction.head_sha or state.base_sha != transaction.base_sha:
        raise ReviewInputError("current quarantine lifecycle state scope conflicts")
    if _collision_pairs is not None:
        _collision_pairs.extend(
            _current_collision_pairs(retained, state, transaction.head_sha)
        )
    if _transfer_capture is not None:
        _transfer_capture(document)
    publication_state = _state_before_post_publication_overrides(
        state, document, record, transaction.head_sha
    )
    original = replace(retained, transaction=transaction)
    projected = (
        project_display_review_result(original, publication_state)
        if isinstance(publication_state, ActiveWindowRoundState)
        else project_drift_review_result(original, publication_state)
    )
    # Decoding native placement state may reconstruct internal comment values
    # differently while preserving every canonical result field. The producer
    # retains its transaction when publication content is unchanged; do not
    # turn that same authenticated content into an unbound digest on cold load.
    if projected.content_digest() == original.content_digest():
        return review_result_digest(original)
    if (
        not isinstance(publication_state, ActiveWindowRoundState)
        and original.review_verdict is None
        and len(projected.comments) == len(original.comments)
        and len(projected.finding_lifecycles) == len(original.finding_lifecycles)
        and Counter(canonical_bytes(item.to_dict()) for item in projected.comments)
        == Counter(canonical_bytes(item.to_dict()) for item in original.comments)
        and Counter(
            canonical_bytes(item.to_dict()) for item in projected.finding_lifecycles
        )
        == Counter(
            canonical_bytes(item.to_dict()) for item in original.finding_lifecycles
        )
        and replace(
            projected,
            comments=original.comments,
            finding_lifecycles=original.finding_lifecycles,
        ).content_digest()
        == original.content_digest()
    ):
        return review_result_digest(original)
    return review_result_digest(projected)


def prepare_quarantine_target(run, authority, command, app, permission):
    if not authorized_override(
        association=authority.document["actor"]["association"], permission=permission
    ):
        raise QuarantineTargetRefusal(
            "quarantine assessment decision requires current maintain or admin permission"
        )
    reader = app.reviewer.finalizer.quarantine_reader(
        token=run.ledger.token,
        repository=run.identity.repository,
        pull_request=run.identity.pull_request,
        head_sha=run.head_sha,
    )
    if reader is None:
        raise ReviewInputError(
            "current review publishes no quarantine assessment target"
        )
    from review_sensei.source_registry import require_module_seal

    from . import body_census_hint

    require_module_seal(body_census_hint)
    with body_census_hint.window():
        authenticated = reader.read(head_sha=run.head_sha)
        read_observations_stop = len(run.caller.observations)
        archive = authenticated.archive
        require_module_seal(body_census_hint)
        body_census_hint.enable(archive)
        origins = archive["assessment_origins"]
        if command.scope not in {o["scope_sha256"] for o in origins["origins"]}:
            raise QuarantineTargetRefusal(
                "quarantine origin scope differs from the current publication"
            )
        instance = command.reference[3:].lower()
        members = [
            item
            for item in authenticated.inventories[command.scope]["assessments"]
            if item["instance"] == instance
        ]
        if len(members) != 1:
            raise QuarantineTargetRefusal(
                "quarantine assessment target is missing or ambiguous"
            )
        from review_sensei.source_registry import require_instance_source

        require_instance_source(run.http, methods=("paginate", "request"))
        rows = run.http.paginate(
            path=run.http.repository_path(
                run.identity.repository, f"/pulls/{run.identity.pull_request}/reviews"
            ),
            token=run.ledger.token,
        )
        from .quarantine_native_scope import filter_reviews

        rows = filter_reviews(
            run.http, rows, repository=run.identity.repository, head_sha=run.head_sha
        )
        candidates = [
            r
            for r in rows
            if isinstance(r, dict)
            and r.get("commit_id") == run.head_sha
            and isinstance(r.get("user"), dict)
            and r["user"].get("id") == run.ledger._evidence_author_id
            and r["user"].get("type") == "Bot"
            and type(r.get("id")) is int
            and isinstance(r.get("body"), str)
        ]
        if not candidates:
            raise ReviewInputError(
                "current quarantine assessment publication is unavailable"
            )
        marked = [
            r for r in candidates if APPROVAL_ELIGIBILITY_MARKER_PREFIX in r["body"]
        ]
        selected = max(marked or candidates, key=lambda r: r["id"])
        human_reader = app._human_inventory_reader(
            token=run.ledger.token,
            repository=run.identity.repository,
            pull_request=run.identity.pull_request,
            head_sha=run.head_sha,
            app_slug="reviewsensei[bot]",
            budget=run.caller.budget_view,
        )
        eligibility = approval_eligibility_from_body(
            selected["body"],
            human_inventory_reader=human_reader,
            native_union_reader=human_reader.native_union_reader(selected),
        )
        if (
            not isinstance(eligibility, ReviewApprovalEligibility)
            or eligibility.quarantine_assessments is None
        ):
            raise ReviewInputError(
                "current review has no explicit quarantine assessment marker"
            )
        from .publication_recovery import _publication_transport

        cached_comments = (
            run.ledger._cached_comments()
            if getattr(run.ledger, "_inventory_complete", False)
            else ()
        )
        cached_body_wire = sum(
            len(canonical_bytes({"body": item["body"]}))
            for item in cached_comments
            if isinstance(item, dict) and isinstance(item.get("body"), str)
        )
        unmutated_since_read = not any(
            item.get("mutation")
            for item in run.caller.observations[read_observations_stop:]
        )
        heavy_guarded_read_reuse = (
            bool(cached_comments)
            and cached_body_wire > 512 * 1024
            and isinstance(archive.get("target_isolation"), dict)
            and archive["target_isolation"].get("contract")
            in {"body-limit-prepublication-v1", "published-tail-reply-isolation-v1"}
        )
        v3_guarded_read_reuse = (
            bool(cached_comments)
            and isinstance(archive.get("target_isolation"), dict)
            and archive["target_isolation"].get("contract")
            == "failed-published-tail-isolation-v1"
        )
        plain_high_comment_read_reuse = (
            len(cached_comments) >= 80
            and getattr(
                _publication_transport(run.http), "_quarantine_capacity_guard", None
            )
            is None
            and isinstance(archive.get("target_isolation"), dict)
            and archive["target_isolation"].get("schema_version")
            == "quarantine-target-isolation-v1"
        )
        if unmutated_since_read and (
            heavy_guarded_read_reuse
            or v3_guarded_read_reuse
            or plain_high_comment_read_reuse
        ):
            from dataclasses import replace

            from review_sensei.quarantine_assessments import assessment_view

            record = reader.record_after_read()
            run.ledger._quarantine_origin_reader.current_scope.require()
            view = assessment_view(
                authenticated.archive["assessment_origins"],
                authenticated.inventories,
                authenticated.state,
                scope=authenticated.lineage["scope_sha256"],
                fresh_human_pending=eligibility.fresh_human_pending,
            )
            actual = replace(
                eligibility,
                quarantine_assessments=view,
                facts=replace(
                    eligibility.facts,
                    has_human_adjudication_findings=(
                        eligibility.fresh_human_pending
                        or bool(view["pending"])
                        or bool(
                            (eligibility.historical_assessments or {}).get("pending")
                        )
                    ),
                ),
            )
            if actual.to_dict() != eligibility.to_dict():
                raise ReviewInputError(
                    "quarantine assessment current publication is unconfirmed"
                )
        else:
            reader.require(eligibility, head_sha=run.head_sha)
            record = reader.record_after_read()
        collision_pairs: list[tuple[str, str | None]] = []
        lifecycle_capture: list[Any] = []
        if (
            record.transaction is None
            or eligibility.result_digest
            != _current_publication_digest(
                run.ledger,
                record,
                _collision_pairs=collision_pairs,
                _capture=lifecycle_capture,
            )
        ):
            raise ReviewInputError(
                "current quarantine assessment publication transaction conflicts"
            )
    index = parse_finding_index(
        selected["body"],
        repository_id=run.identity.repository_id,
        pull_request=run.identity.pull_request,
        head_sha=run.head_sha,
    )
    if index is None:
        raise ReviewInputError(
            "current quarantine assessment finding index is unavailable"
        )
    if authenticated.lineage["schema_version"] != "quarantine-published-tail-v1" and (
        any(
            item.instance == instance or item.lifecycle == members[0]["concern"]
            for item in index.entries
        )
        or any(
            original_instance == instance or original_concern == members[0]["concern"]
            for original_instance, original_concern in collision_pairs
        )
    ):
        raise QuarantineTargetRefusal(
            "quarantine assessment collides with a fresh finding; no decision applied"
        )
    # The published-tail variant has two exact publication origins. Its scoped
    # event changes only that origin's hold, never ordinary dispositions or a
    # fresh thread. A recurrence therefore retains its independent fresh gate.
    actual_permission = decision_authority(run, authority)
    from .quarantine_command_projection import project_decision

    # Pure sizing includes the maximum legal future generation and storage ID.
    # This detached value cannot enter the ledger or stand in for the own CAS.
    projected = project_decision(
        authenticated,
        record,
        _decision_value(run, authority, command, instance, actual_permission),
        binding={**_decision_binding(record, run), "generation": 2_147_483_647},
    )
    target = QuarantineTarget(
        reader,
        authenticated,
        instance,
        actual_permission,
        lifecycle_capture[0] if lifecycle_capture else None,
        projected,
        canonical_bytes(selected),
        command,
        canonical_bytes(rows),
    )
    from .quarantine_lifecycle_transfer import bind_target

    bind_target(target.lifecycle_transfer, target)
    from .quarantine_command_namespace import prepare as prepare_namespace

    prepare_namespace(run, authority, target)
    from .quarantine_command_suffix import prepare as prepare_suffix

    suffix = prepare_suffix(run, authority, target)
    if suffix is not None:
        from .quarantine_command_suffix import require_execution

        require_execution(suffix, run)
    _require_installed_command_tail(run, authenticated)
    from .quarantine_command_execution import prepare_receipt_phase

    prepare_receipt_phase(run, authority, target)
    return target


_GROUPED_HISTORICAL_LEASES: weakref.WeakKeyDictionary[Any, Any] = (
    weakref.WeakKeyDictionary()
)


def _is_funded_grouped_historical_command(run, guard) -> bool:
    """Return True when a grouped-native historical command is fully funded."""
    caller = run.caller
    state = caller.accounting
    terminal = next(
        (b for b in state.blocks if b.block_id == caller._terminal_block),
        None,
    )
    return (
        not guard.complete_tail
        and guard.native_profile is not None
        and type(state.configuration.limit) is int
        and state.configuration.limit >= 1152
        and caller.budget_view.ordinary_remaining() >= 220
        and terminal is not None
        and terminal.undispatched >= 50
        and not state.prior_nonstartup_liability
    )


def has_grouped_historical_lease(ledger, identity) -> bool:
    """Verify an active grouped-native historical lease for this ledger and caller."""
    from review_sensei.source_registry import require_module_seal

    from .quarantine_capacity import installed_capacity

    require_module_seal(sys.modules[__name__])
    caller = getattr(ledger, "caller", None)
    if caller is None:
        return False
    try:
        saved = _GROUPED_HISTORICAL_LEASES.get(caller)
    except TypeError:
        return False
    if saved is None:
        return False
    return (
        saved == (ledger, identity, installed_capacity(ledger, identity))
        and caller._terminal_reason is None
    )


def _require_installed_command_tail(run, authenticated):
    """Reject a known unsupported finalizer before entering a decision effect.

    A cold command's original native envelope is not a full-review promise.
    Authenticate the installed owner before inspecting its profile; do not
    manufacture that promise or settle the action as a successful no-op.
    """
    from .publication_recovery import _publication_transport
    from .quarantine_capacity import installed_capacity, native_reserve
    from .quarantine_origin import require_authenticated_read_owner

    require_authenticated_read_owner(
        authenticated, run.ledger, run.caller, run.identity
    )
    required = native_reserve(authenticated.archive["target_isolation"]) is not None
    transport = _publication_transport(run.ledger.http)
    if not required and getattr(transport, "_quarantine_capacity_guard", None) is None:
        return
    guard = installed_capacity(run.ledger, run.identity)
    from review_sensei.source_registry import require_module_seal

    from . import quarantine_historical_execution

    require_module_seal(quarantine_historical_execution)
    closed = quarantine_historical_execution.available(run.caller, run.ledger)
    if not guard.complete_tail and not closed:
        if _is_funded_grouped_historical_command(run, guard):
            _GROUPED_HISTORICAL_LEASES[run.caller] = (
                run.ledger,
                run.identity,
                guard,
            )
            return
        from .accepted_semantic_history import QUARANTINE_CAPACITY_REFUSAL_PREFIX

        raise ReviewInputError(
            f"{QUARANTINE_CAPACITY_REFUSAL_PREFIX} "
            "installed command tail has no complete lifecycle proof"
        )


def _stage_decision_event(run, authority, event, binding):
    """One separately metered immutable event before conditional root binding.

    A confirmed event alone discharges nothing. The native root mutation must
    still compare both complete states and append precisely this one reference.
    No nested allocation replenishes the root operation or its protected tail.
    """
    from .quarantine_creation import _body

    parts = partition_evidence(event, binding=binding, item_count=1)
    if len(parts) != 1:
        raise ReviewInputError("quarantine individual event exceeds one carrier")
    body = _body(parts[0])
    if len(body.encode()) > MAX_PART_BYTES:
        raise ReviewInputError("quarantine individual event exceeds its wire bound")
    stored = [
        item
        for item in run.ledger._cached_comments()
        if isinstance(item, dict)
        and isinstance(item.get("body"), str)
        and item["body"].endswith("<!-- reviewsensei:evidence-part:v1 -->")
    ]
    from .quarantine_published_tail import retained_storage_reserve

    reserve_parts, reserve_bytes = retained_storage_reserve(run.ledger)
    if (
        len(stored) + 1 + reserve_parts > MAX_STORED_PARTS
        or sum(len(item["body"].encode()) for item in stored)
        + len(body.encode())
        + reserve_bytes
        > MAX_STORED_PART_BYTES
    ):
        raise ReviewInputError(
            "quarantine individual event retention capacity exceeded"
        )
    if run.caller._action_effect_boundary is None or run.caller._checkpoint is not None:
        raise ReviewInputError(
            "quarantine decision effect is not entered or is uncertain"
        )
    decision_authority(run, authority)
    key = "quarantine-event:" + hashlib.sha256(body.encode()).hexdigest()
    with run.caller.mutation_operation(key) as operation:
        status, item = run.http.request(
            "POST",
            run.ledger._comments_path(run.identity),
            token=run.ledger.token,
            body={"body": body},
        )
        if (
            status != 201
            or not isinstance(item, dict)
            or type(item.get("id")) is not int
            or item.get("body") != body
        ):
            raise ReviewInputError(
                "quarantine individual event create is unconfirmed; no retry"
            )
        run.ledger._part_owner(item, run.identity)
        identifier = str(item["id"])
        status, readback = run.http.request(
            "GET",
            run.http.repository_path(
                run.identity.repository, "/issues/comments/" + identifier
            ),
            token=run.ledger.token,
        )
        if (
            status != 200
            or not isinstance(readback, dict)
            or readback.get("id") != item["id"]
            or readback.get("body") != body
        ):
            raise ReviewInputError("quarantine individual event readback conflicts")
        run.ledger._part_owner(readback, run.identity)
        run.ledger._merge_comments([readback])
        operation.confirmed = True
    return evidence_manifest(
        event, binding=binding, item_count=1, parts=parts, storage_ids=(identifier,)
    )


def _decision_value(run, authority, command, instance, permission):
    source, actor = authority.document["source"], authority.document["actor"]
    return {
        "origin_scope_sha256": command.scope,
        "instance": instance,
        "action": command.action,
        "actor": actor["login"],
        "role": permission,
        "head_sha": run.head_sha,
        "source_id": source["id"],
        "source_updated_at": source["updated_at"],
        "source_body_sha256": source["body_sha256"],
        "reason": command.submitted_reason,
        "policy_sha256": run.caller.accounting.configuration.digest,
    }


def _decision_binding(record, run):
    return {
        **record.quarantine_lineage["assessment_state_manifest"]["binding"],
        "generation": record.generation + 1,
        "head_sha": run.head_sha,
        "base_sha": run.base_sha,
        "configuration_digest": run.caller.accounting.configuration.digest,
    }


def historical_command_result(command, target, record, *, existing, actor):
    """Original historical result producer shared with detached size proofs."""
    if existing is None:
        summary = (
            (
                "Carried historical assessment dismissed"
                if command.action == "dismiss"
                else "Carried historical assessment risk accepted"
            )
            + "; the predecessor remains unresolved and no verified fix is claimed; fresh findings require independent decisions"
        )
        outcome, detail = "recorded", command.action
    else:
        same_decision = (existing["action"], existing["actor"]) == (
            command.action,
            actor["login"],
        )
        summary = (
            "Carried historical assessment decision already recorded"
            if same_decision
            else "Conflicting carried historical assessment decision not applied"
        ) + f"; immutable prior action: {existing['action']} by {existing['actor']}"
        outcome = "already-recorded" if same_decision else "not-applied"
        detail = f"prior action={existing['action']}; actor={existing['actor']}; requested action={command.action}; actor={actor['login']}"
    result = MaintainerCommandResult(
        action=command.action,
        applied=existing is None,
        operator_paused=record.operator_paused,
        summary=summary,
        receipts=(
            OverrideReceipt(
                "quarantine:" + command.scope + "/RS-" + target.instance.upper(),
                target.instance,
                outcome,
                detail,
            ),
        ),
    )
    return result


def apply_quarantine_target(run, authority, command, target, app):
    actor = authority.document["actor"]
    before = target.authenticated
    existing = next(
        (
            item
            for item in before.state["settlements"]
            if (item["origin_scope_sha256"], item["instance"])
            == (command.scope, target.instance)
        ),
        None,
    )
    lifecycle_transfer = None
    if existing is None:
        decision = _decision_value(
            run, authority, command, target.instance, target.permission
        )

        from review_sensei.session import SessionLoadResult

        from .publication_recovery import _publication_transport
        from .quarantine_origin import (
            has_paged_v2_disposition_snapshot,
            rebind_same_lineage_owner,
        )

        loaded = None
        current = None
        active_authenticated = getattr(run.ledger, "_quarantine_authenticated", None)
        if (
            (
                (
                    getattr(
                        _publication_transport(run.http),
                        "_quarantine_capacity_guard",
                        None,
                    )
                    is None
                    and len(run.ledger._cached_comments()) >= 80
                )
                or (
                    type(run.caller.accounting.configuration.limit) is int
                    and run.caller.accounting.configuration.limit >= 1152
                    and run.caller._effect_record is not None
                    and has_paged_v2_disposition_snapshot(run.caller._effect_record)
                )
            )
            and getattr(run.ledger, "_inventory_complete", False)
            and run.caller._checkpoint is None
            and run.caller._effect_record is not None
            and run.caller._effect_record.record_sha256 == run.caller._root
            and run.caller._effect_record.quarantine_lineage is not None
            and active_authenticated is not None
            and active_authenticated.lineage_bytes == before.lineage_bytes
            and active_authenticated.state_bytes == before.state_bytes
            and canonical_bytes(run.caller._effect_record.quarantine_lineage)
            == before.lineage_bytes
        ):
            try:
                current = rebind_same_lineage_owner(
                    active_authenticated,
                    run.ledger,
                    run.caller,
                    run.identity,
                    run.caller._effect_record,
                )
            except ReviewInputError:
                loaded = None
                current = None
            else:
                loaded = SessionLoadResult(
                    status="ok", record=run.caller._effect_record
                )
        if loaded is None:
            loaded = run.ledger.load(run.identity)
            current = getattr(run.ledger, "_quarantine_authenticated", None)
        if (
            loaded.record is None
            or current is None
            or current.record_sha256 != loaded.record.record_sha256
            or current.state_bytes != before.state_bytes
            or current.lineage["scope_sha256"] != before.lineage["scope_sha256"]
        ):
            raise ReviewInputError(
                "quarantine current complete state changed before event"
            )
        assert loaded.record.quarantine_lineage is not None
        current_manifest = cast(
            dict[str, Any],
            loaded.record.quarantine_lineage["assessment_state_manifest"],
        )
        cursor = read_inline_evidence(
            current_manifest,
            expected_binding=cast(Mapping[str, object], current_manifest["binding"]),
        )
        event = next_assessment_event(cursor, decision)
        binding = _decision_binding(loaded.record, run)
        tail = _stage_decision_event(run, authority, event, binding)

        def mutate(record):
            authenticated = getattr(run.ledger, "_quarantine_authenticated", None)
            if (
                authenticated is None
                or authenticated.record_sha256 != record.record_sha256
                or authenticated.lineage["scope_sha256"]
                != before.lineage["scope_sha256"]
                or authenticated.state_bytes != before.state_bytes
            ):
                raise ReviewInputError(
                    "quarantine complete assessment state changed before decision"
                )
            from .quarantine_command_projection import cursor_manifest, successor_state

            next_state = successor_state(authenticated, decision)
            manifest = cursor_manifest(
                scope=authenticated.lineage["scope_sha256"],
                count=len(next_state["settlements"]),
                tail=tail,
                binding={**binding, "generation": record.generation + 1},
            )
            return record.evolve(
                generation=record.generation + 1,
                quarantine_lineage={
                    **record.quarantine_lineage,
                    "assessment_state_manifest": manifest,
                },
            )

        _issue_decision_permit(run, authority, before, decision, tail)
        confirmed = run.ledger.replace(run.identity, mutate)
        lifecycle_transfer = _confirmed_own_decision(
            run, target, confirmed, decision=decision
        )
    else:
        confirmed = run.ledger.load(run.identity).record
    result = historical_command_result(
        command, target, confirmed, existing=existing, actor=actor
    )
    decision_authority(run, authority)
    return app._refinalize_command(
        result,
        oidc_token=run.oidc,
        repository=run.identity.repository,
        repository_id=run.identity.repository_id,
        pull_request=run.identity.pull_request,
        head_sha=run.head_sha,
        app_slug="reviewsensei[bot]",
        actor=actor["login"],
        record=confirmed,
        ledger=run.ledger,
        evidence_budget=run.caller.budget_view,
        policy_digest=run.caller.accounting.configuration.digest,
        _quarantine_lifecycle_transfer=lifecycle_transfer,
    )


def _confirmed_own_decision(run, target, confirmed, *, decision):
    """Reuse only the actual exact owned cursor GET, never a projection."""
    import inspect
    import sys

    from review_sensei.source_registry import require_module_seal

    from . import quarantine_candidate_receipt, quarantine_lifecycle_transfer
    from .publication_recovery import _publication_transport
    from .quarantine_command_projection import require_projection_source
    from .quarantine_origin import (
        advance_confirmed_decision_owner,
        has_paged_v2_disposition_snapshot,
        require_authenticated_read_owner,
    )

    current = inspect.currentframe()
    frame = current.f_back if current is not None else None
    if frame is None or frame.f_code is not apply_quarantine_target.__code__:
        raise ReviewInputError("quarantine own decision requires its original caller")
    require_module_seal(sys.modules[__name__])
    require_module_seal(quarantine_lifecycle_transfer)
    require_projection_source()
    require_authenticated_read_owner(
        target.authenticated, run.ledger, run.caller, run.identity
    )
    permit = getattr(run.ledger, "_quarantine_decision_permit", None)
    if (
        type(permit) is not _DecisionPermit
        or permit not in _ISSUED_DECISIONS
        or _DECISION_BINDINGS.get(permit) != _decision_permit_binding(permit)
        or permit.consumed is not True
        or permit.ledger is not run.ledger
        or permit.authority.run is not run
        or permit.run_id != run.caller.run.run_id
        or permit.head_sha != run.head_sha
        or permit.before_state != target.authenticated.state_bytes
        or permit.decision != canonical_bytes(decision)
        or permit.scope != target.authenticated.lineage["scope_sha256"]
        or _DECISION_CANDIDATES.get(permit) != canonical_bytes(confirmed.to_dict())
    ):
        raise ReviewInputError("quarantine own decision candidate custody changed")
    manifest = confirmed.quarantine_lineage["assessment_state_manifest"]
    if read_inline_evidence(
        manifest, expected_binding=manifest["binding"]
    ) != assessment_cursor(
        scope=permit.scope,
        count=len(target.authenticated.state["settlements"]) + 1,
        tail_manifest=json.loads(permit.tail),
    ):
        raise ReviewInputError("quarantine own decision committed cursor changed")
    quarantine_candidate_receipt.consume(
        run.caller, run.ledger, run.identity, confirmed
    )
    active_authenticated = getattr(run.ledger, "_quarantine_authenticated", None)
    if (
        getattr(run.ledger, "_inventory_complete", False)
        and active_authenticated is not None
        and (
            (
                getattr(
                    _publication_transport(run.http),
                    "_quarantine_capacity_guard",
                    None,
                )
                is None
                and len(cast(Any, run.ledger)._cached_comments()) >= 80
            )
            or (
                has_grouped_historical_lease(run.ledger, run.identity)
                and has_paged_v2_disposition_snapshot(confirmed)
            )
        )
    ):
        advance_confirmed_decision_owner(
            active_authenticated,
            run.ledger,
            run.caller,
            run.identity,
            confirmed,
            decision=decision,
        )
    return quarantine_lifecycle_transfer.confirmed_transfer(
        target, run, confirmed, decision_permit=permit
    )


# A document, hash, carrier or arbitrary mutator cannot authorize a state change.
# Only this process's exact authenticated action can issue one native transition.
_ISSUED_DECISIONS: weakref.WeakSet[_DecisionPermit] = weakref.WeakSet()
_DECISION_BINDINGS: weakref.WeakKeyDictionary[Any, tuple[Any, ...]] = (
    weakref.WeakKeyDictionary()
)
_DECISION_CANDIDATES: weakref.WeakKeyDictionary[Any, bytes] = (
    weakref.WeakKeyDictionary()
)
_FENCED_DECISIONS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _seal_pending_decision_fence(guard, role):
    """Only the actual own-CAS business guard completes a pending permit.

    Permit construction is pure. This is the original fresh four-read fence,
    not cached authority or an independently callable execution capability.
    """
    import inspect
    import sys

    from review_sensei.source_registry import (
        require_instance_source,
        require_module_seal,
    )

    from . import review_run
    from .review_run import ActionRunLedger

    run = guard.run
    caller = run.caller
    authority_document = canonical_bytes(guard.authority.document)
    permit = getattr(run.ledger, "_quarantine_decision_permit", None)
    if permit is None or getattr(permit, "consumed", False):
        return
    require_module_seal(sys.modules[__name__])
    require_instance_source(run.ledger, methods=("replace",))
    frame = inspect.currentframe()
    call = None if frame is None else frame.f_back
    replace_frame = None if call is None else call.f_back
    apply_frame = None if replace_frame is None else replace_frame.f_back
    if (
        type(permit) is not _DecisionPermit
        or permit not in _ISSUED_DECISIONS
        or _DECISION_BINDINGS.get(permit) != _decision_permit_binding(permit)
        or permit.ledger is not run.ledger
        or permit.authority is not guard.authority
        or getattr(run.ledger, "business_guard", None) is not guard
        or _decision_fence_scope(guard) is None
        or call is None
        or call.f_code is not _DECISION_FENCE_CALL.__code__
        or call.f_globals is not globals()
        or call.f_locals.get("self") is not guard
        or replace_frame is None
        or replace_frame.f_code is not ActionRunLedger.replace.__code__
        or replace_frame.f_globals is not vars(review_run)
        or replace_frame.f_locals.get("self") is not run.ledger
        or replace_frame.f_locals.get("identity") != run.identity
        or apply_frame is None
        or apply_frame.f_code is not apply_quarantine_target.__code__
        or apply_frame.f_globals is not globals()
        or apply_frame.f_locals.get("run") is not run
        or apply_frame.f_locals.get("mutate")
        is not replace_frame.f_locals.get("mutate")
        or json.loads(permit.decision)["role"] != role
    ):
        raise ReviewInputError(
            "quarantine decision requires its original fresh CAS fence"
        )
    _FENCED_DECISIONS[permit] = (
        guard,
        caller,
        caller._root,
        _decision_permit_binding(permit),
        authority_document,
    )


def _has_current_decision_fence(ledger, permit, before):
    guard = getattr(ledger, "business_guard", None)
    caller = getattr(ledger, "caller", None)
    if (
        caller is None
        or type(guard) is not QuarantineDecisionFence
        or _decision_fence_scope(guard) is None
    ):
        return False
    return (
        _FENCED_DECISIONS.get(permit)
        == (
            guard,
            caller,
            before.record_sha256,
            _decision_permit_binding(permit),
            canonical_bytes(guard.authority.document),
        )
        and caller._root == before.record_sha256
    )


def _decision_permit_binding(permit):
    return (
        permit.ledger,
        permit.authority,
        permit.run_id,
        permit.head_sha,
        permit.scope,
        permit.before_state,
        permit.decision,
        permit.tail,
    )


@dataclass(frozen=True, eq=False)
class _DecisionPermit:
    ledger: object
    authority: VerifiedRunAction
    run_id: int
    head_sha: str
    scope: str
    before_state: bytes
    decision: bytes
    tail: bytes
    consumed: bool = False


def _issue_decision_permit(run, authority, before, decision, tail):
    from .run_authority import VerifiedRunAction

    if (
        type(authority) is not VerifiedRunAction
        or authority.run is not run
        or run.caller._action_effect_boundary is None
    ):
        raise ReviewInputError(
            "quarantine individual decision requires current action authority"
        )
    # This pending permit cannot authorize a cursor write. The immediately
    # following original ledger.replace owns the fresh four-read authority
    # check and seals this exact permit before require_native_decision accepts it.
    source, actor = authority.document["source"], authority.document["actor"]
    if (
        decision["role"] not in {"maintain", "admin"}
        or actor["login"] != decision["actor"]
        or source["id"] != decision["source_id"]
        or source["updated_at"] != decision["source_updated_at"]
        or source["body_sha256"] != decision["source_body_sha256"]
    ):
        raise ReviewInputError(
            "quarantine individual decision source or authority changed"
        )
    permit = _DecisionPermit(
        run.ledger,
        authority,
        run.caller.run.run_id,
        run.head_sha,
        before.lineage["scope_sha256"],
        before.state_bytes,
        canonical_bytes(decision),
        canonical_bytes(tail),
    )
    _ISSUED_DECISIONS.add(permit)
    _DECISION_BINDINGS[permit] = _decision_permit_binding(permit)
    run.ledger._quarantine_decision_permit = permit


def require_native_decision(ledger, before, after):
    if before.quarantine_lineage is None:
        return
    previous = before.quarantine_lineage["assessment_state_manifest"]
    updated = (
        after.quarantine_lineage["assessment_state_manifest"]
        if after.quarantine_lineage is not None
        else None
    )
    if previous == updated:
        return
    permit = getattr(ledger, "_quarantine_decision_permit", None)
    caller = getattr(ledger, "caller", None)
    authenticated = getattr(ledger, "_quarantine_authenticated", None)
    if (
        type(permit) is not _DecisionPermit
        or permit not in _ISSUED_DECISIONS
        or _DECISION_BINDINGS.get(permit) != _decision_permit_binding(permit)
        or permit.consumed
        or permit.ledger is not ledger
        or not _has_current_decision_fence(ledger, permit, before)
        or caller is None
        or caller.run.run_id != permit.run_id
        or caller._action_effect_boundary is None
        or caller.accounting.owner != permit.authority.owner
        or authenticated is None
        or authenticated.record_sha256 != before.record_sha256
        or authenticated.lineage["scope_sha256"] != permit.scope
        or authenticated.state_bytes != permit.before_state
        or updated is None
    ):
        raise ReviewInputError(
            "quarantine assessment state requires an issued exact individual decision"
        )
    cursor = read_inline_evidence(updated, expected_binding=updated["binding"])
    if cursor != assessment_cursor(
        scope=permit.scope,
        count=len(authenticated.state["settlements"]) + 1,
        tail_manifest=json.loads(permit.tail),
    ):
        raise ReviewInputError("quarantine individual decision reference conflicts")
    # This is an exact proposed candidate, not a native confirmation receipt.
    # No caller may promote it to own-CAS authority before actual echo readback.
    _DECISION_CANDIDATES[permit] = canonical_bytes(after.to_dict())
    _FENCED_DECISIONS.pop(permit)
    object.__setattr__(permit, "consumed", True)


from .quarantine_lifecycle_transfer import install_source as _install_lifecycle_transfer  # noqa: E402

_current_publication_digest = _install_lifecycle_transfer(_current_publication_digest)
del _install_lifecycle_transfer


from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
