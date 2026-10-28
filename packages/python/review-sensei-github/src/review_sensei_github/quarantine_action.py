"""Exact individual carried-assessment decisions from a current publication."""

from __future__ import annotations

import hashlib
import json
import re
import weakref
from dataclasses import dataclass
from urllib.parse import quote

from review_sensei.bounded_evidence import (
    MAX_MANIFEST_BYTES,
    MAX_PART_BYTES,
    MAX_STORED_PART_BYTES,
    MAX_STORED_PARTS,
    canonical_bytes,
    evidence_manifest,
    inline_manifest,
    partition_evidence,
    read_inline_evidence,
)
from review_sensei.disposition import (
    MaintainerCommandResult,
    OverrideReceipt,
    authorized_override,
)
from review_sensei.errors import ReviewInputError
from review_sensei.quarantine_assessments import require_assessment_successor
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


def decision_authority(run, authority):
    """Fresh source/head/base/policy/maintainer checks before each decision effect."""
    current_source = authority.fence()
    source_user = current_source.get("user")
    if (
        not isinstance(source_user, dict)
        or type(source_user.get("id")) is not int
        or source_user["id"] < 1
    ):
        raise ReviewInputError(
            "quarantine decision source numeric actor is unavailable"
        )
    origin_reader = getattr(run.ledger, "_quarantine_origin_reader", None)
    if origin_reader is None:
        raise ReviewInputError("quarantine decision provenance reader is absent")
    origin_reader.current_scope.require()
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
    rows = run.http.paginate(
        path=run.http.repository_path(
            run.identity.repository, f"/pulls/{run.identity.pull_request}/reviews"
        ),
        token=run.ledger.token,
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
    eligibility = approval_eligibility_from_body(
        selected["body"],
        human_inventory_reader=app._human_inventory_reader(
            token=run.ledger.token,
            repository=run.identity.repository,
            pull_request=run.identity.pull_request,
            head_sha=run.head_sha,
            app_slug="reviewsensei[bot]",
            budget=run.caller.budget_view,
        ),
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
    if (
        record.transaction is None
        or eligibility.result_digest
        != transaction_publication_digest(record.transaction)
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
    if any(
        item.instance == instance or item.lifecycle == members[0]["concern"]
        for item in index.entries
    ):
        raise QuarantineTargetRefusal(
            "quarantine assessment collides with a fresh finding; no decision applied"
        )
    actual_permission = decision_authority(run, authority)
    return QuarantineTarget(reader, authenticated, instance, actual_permission)


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
    if (
        len(stored) + 1 > MAX_STORED_PARTS
        or sum(len(item["body"].encode()) for item in stored) + len(body.encode())
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


def apply_quarantine_target(run, authority, command, target, app):
    source, actor = authority.document["source"], authority.document["actor"]
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
    if existing is None:
        decision = {
            "origin_scope_sha256": command.scope,
            "instance": target.instance,
            "action": command.action,
            "actor": actor["login"],
            "role": target.permission,
            "head_sha": run.head_sha,
            "source_id": source["id"],
            "source_updated_at": source["updated_at"],
            "source_body_sha256": source["body_sha256"],
            "reason": command.submitted_reason,
            "policy_sha256": run.caller.accounting.configuration.digest,
        }

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
        binding = {
            **current_manifest["binding"],
            "generation": loaded.record.generation + 1,
            "head_sha": run.head_sha,
            "base_sha": run.base_sha,
            "configuration_digest": run.caller.accounting.configuration.digest,
        }
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
            next_state = {
                **authenticated.state,
                "settlements": [*authenticated.state["settlements"], decision],
            }
            require_assessment_successor(
                authenticated.archive["assessment_origins"],
                authenticated.inventories,
                authenticated.state,
                next_state,
                scope=authenticated.lineage["scope_sha256"],
            )
            value = assessment_cursor(
                scope=authenticated.lineage["scope_sha256"],
                count=len(next_state["settlements"]),
                tail_manifest=tail,
            )
            manifest = inline_manifest(
                value,
                binding={**binding, "generation": record.generation + 1},
                item_count=len(next_state["settlements"]),
            )
            if manifest is None or len(canonical_bytes(manifest)) > MAX_MANIFEST_BYTES:
                raise ReviewInputError("quarantine decision cursor exceeds its bound")
            return record.evolve(
                generation=record.generation + 1,
                quarantine_lineage={
                    **record.quarantine_lineage,
                    "assessment_state_manifest": manifest,
                },
            )

        _issue_decision_permit(run, authority, before, decision, tail)
        confirmed = run.ledger.replace(run.identity, mutate)
        actual = target.reader.read(head_sha=run.head_sha)
        if (
            not actual.state["settlements"]
            or actual.state["settlements"][-1] != decision
        ):
            raise ReviewInputError("quarantine individual decision was not confirmed")
    else:
        confirmed = run.ledger.load(run.identity).record
    result = MaintainerCommandResult(
        action=command.action,
        applied=existing is None,
        operator_paused=confirmed.operator_paused,
        summary=(
            "Carried historical assessment dismissed"
            if command.action == "dismiss"
            else "Carried historical assessment risk accepted"
        )
        + "; the predecessor remains unresolved and no verified fix is claimed"
        if existing is None
        else "Carried historical assessment already has an individual durable decision",
        receipts=(
            OverrideReceipt(
                "quarantine:" + command.scope + "/RS-" + target.instance.upper(),
                target.instance,
                "recorded" if existing is None else "already-recorded",
                command.action,
            ),
        ),
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
    )


# A document, hash, carrier or arbitrary mutator cannot authorize a state change.
# Only this process's exact authenticated action can issue one native transition.
_ISSUED_DECISIONS: weakref.WeakSet[_DecisionPermit] = weakref.WeakSet()


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
    role = decision_authority(run, authority)
    source, actor = authority.document["source"], authority.document["actor"]
    if (
        role != decision["role"]
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
        or permit.consumed
        or permit.ledger is not ledger
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
    object.__setattr__(permit, "consumed", True)
