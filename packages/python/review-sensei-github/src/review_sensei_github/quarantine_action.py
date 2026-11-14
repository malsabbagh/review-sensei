"""Exact individual carried-assessment decisions from a current publication."""

from __future__ import annotations

import hashlib
import json
import re
import weakref
from dataclasses import dataclass
from typing import Any
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
from .publication import approval_eligibility_from_body, parse_finding_index
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
    from review_sensei.finding_lifecycle_store import read_finding_lifecycle
    from review_sensei.models import ReviewResult

    from .finding_lifecycle import _require_effect_scope, _unpack_journal, _validate
    from .finding_lifecycle_dictionary import resolve_stored
    from .publication import review_result_digest

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
    stored = read_finding_lifecycle(ledger, record)
    document = _validate(_unpack_journal(resolve_stored(ledger, record, stored)))
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
    original = replace(retained, transaction=transaction)
    projected = (
        project_display_review_result(original, state)
        if isinstance(state, ActiveWindowRoundState)
        else project_drift_review_result(original, state)
    )
    # Decoding native placement state may reconstruct internal comment values
    # differently while preserving every canonical result field. The producer
    # retains its transaction when publication content is unchanged; do not
    # turn that same authenticated content into an unbound digest on cold load.
    if projected.content_digest() == original.content_digest():
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
    authenticated = reader.read(head_sha=run.head_sha)
    origins = authenticated.archive["assessment_origins"]
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
    selected = max(candidates, key=lambda r: r["id"])
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
    _require_installed_command_tail(run, authenticated)
    from .quarantine_command_execution import prepare_receipt_phase

    prepare_receipt_phase(run, authority, target)
    return target


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
    if not guard.complete_tail:
        raise ReviewInputError(
            "quarantine command finalization capacity unavailable before business "
            "writes: installed command tail has no complete lifecycle proof"
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
        current_manifest = loaded.record.quarantine_lineage["assessment_state_manifest"]
        cursor = read_inline_evidence(
            current_manifest, expected_binding=current_manifest["binding"]
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
    from .quarantine_command_projection import require_projection_source
    from .quarantine_origin import require_authenticated_read_owner

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
