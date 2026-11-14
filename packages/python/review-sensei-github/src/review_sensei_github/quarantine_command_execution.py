"""Source-owned receipt preparation for already admitted plain commands only.

This module never grants a business effect or a native capacity extension.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass
from typing import Any
from weakref import WeakKeyDictionary

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.source_registry import require_module_seal


def _source_frame(module, function):
    require_module_seal(module)
    frame = inspect.currentframe()
    while frame is not None:
        if frame.f_code is inspect.unwrap(
            function
        ).__code__ and frame.f_globals is vars(module):
            return frame
        frame = frame.f_back
    raise ReviewInputError("receipt issuer requires its original source frame")


@dataclass(frozen=True, eq=False)
class _Lease:
    pass


def _execution_owner(owner):
    return owner.run_attempt, owner.job, owner.generation


def _configuration(value):
    return value.commit, value.digest, value.limit


def _semantic_owner(row):
    user = row.get("user")
    if (
        not isinstance(user, dict)
        or type(user.get("id")) is not int
        or user["id"] <= 0
        or user.get("type") != "Bot"
        or not isinstance(user.get("login"), str)
        or not user["login"]
    ):
        raise ReviewInputError("receipt root owner is invalid")
    return user["id"], user["type"], user["login"]


def _blocks(caller):
    ordinary, terminal = caller._block, caller._terminal_block
    if (
        ordinary is None
        or terminal is None
        or ordinary == terminal
        or caller._terminal_reason is not None
    ):
        raise ReviewInputError(
            "receipt requires original ordinary and protected terminal blocks"
        )
    blocks = caller.accounting.blocks
    selected = [block for block in blocks if block.block_id == ordinary]
    if len(selected) != 1 or not any(block.block_id == terminal for block in blocks):
        raise ReviewInputError("receipt funding blocks are unavailable")
    fixed = tuple(
        (
            block.block_id,
            _execution_owner(block.issued_by),
            None
            if block.block_id == ordinary
            else (
                block.undispatched,
                tuple(
                    (pending.request_id, pending.deadline_unix_ms)
                    for pending in block.pending
                ),
            ),
        )
        for block in blocks
    )
    return (
        (
            ordinary,
            terminal,
            caller.accounting.remaining,
            caller.accounting.prior_nonstartup_liability,
            fixed,
        ),
        selected[0].undispatched,
        frozenset(
            (pending.request_id, pending.deadline_unix_ms)
            for pending in selected[0].pending
        ),
    )


def _make_issuer():
    starts: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()
    censuses: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()
    prepared: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()
    leases: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()

    def scope_binding(ledger, caller, plan):
        from .publication_recovery import _publication_transport

        guard = getattr(
            _publication_transport(ledger.http), "_quarantine_capacity_guard", None
        )
        capacity = (
            None
            if plan is None
            else (
                tuple(sorted(plan.known.items())),
                plan.unknown,
                tuple(sorted(plan.metadata_known.items())),
                plan.metadata_unknown,
                plan.worst_read_calls(),
            )
        )
        return (
            id(ledger),
            id(caller),
            id(plan),
            capacity,
            id(guard),
            _configuration(caller.accounting.configuration),
            _execution_owner(caller.accounting.owner),
            _blocks(caller)[0],
            caller._root,
            id(caller._effect_record),
            canonical_bytes(caller._effect_record.to_dict()),
            canonical_bytes(caller.accounting.action),
        )

    def begin_receipt_census(ledger, identity, plan=None):
        from . import packed_comment_census, quarantine_census

        producer = packed_comment_census if plan is not None else quarantine_census
        function = (
            producer.fetch_packed_comments
            if plan is not None
            else producer.fetch_comments
        )
        frame = _source_frame(producer, function)
        caller = getattr(ledger, "caller", None)
        from .publication_recovery import _publication_transport

        if (
            caller is None
            or caller._effect_record is None
            or caller.accounting.action is None
            or caller.accounting.action.get("phase") != "pending"
            or caller._terminal_block is None
            or caller._terminal_reason is not None
            or getattr(
                _publication_transport(ledger.http), "_quarantine_capacity_guard", None
            )
            is not None
        ):
            return None
        if (
            frame.f_locals.get("ledger") is not ledger
            or frame.f_locals.get("identity") is not identity
            or frame.f_locals.get("plan") is not plan
        ):
            raise ReviewInputError("receipt census entry source changed")
        token = _Lease()
        starts[token] = (
            ledger,
            identity,
            caller,
            plan,
            len(caller.observations),
            caller._root,
            caller._effect_record,
            caller.accounting.owner,
            scope_binding(ledger, caller, plan),
        )
        return token

    def observe_receipt_census(token, rows):
        from . import packed_comment_census, quarantine_census

        if token is None:
            return rows
        saved = starts.pop(token, None) if type(token) is _Lease else None
        if saved is None:
            raise ReviewInputError("receipt census entry is unissued or reused")
        (
            ledger,
            identity,
            caller,
            plan,
            observations_start,
            root,
            record,
            owner,
            scope,
        ) = saved
        producer = packed_comment_census if plan is not None else quarantine_census
        function = (
            producer.fetch_packed_comments
            if plan is not None
            else producer.fetch_comments
        )
        frame = _source_frame(producer, function)
        if (
            frame.f_locals.get("ledger") is not ledger
            or frame.f_locals.get("identity") is not identity
            or frame.f_locals.get("plan") is not plan
            or frame.f_locals.get("collected") is not rows
            or frame.f_locals.get("receipt_census") is not token
            or caller._root != root
            or caller._effect_record is not record
            or caller.accounting.owner != owner
            or scope_binding(ledger, caller, plan) != scope
        ):
            raise ReviewInputError("receipt census source custody changed")
        observations = caller.observations[observations_start:]
        if (
            not observations
            or len(observations) > 10
            or len(observations)
            != (
                frame.f_locals.get("calls")
                if plan is not None
                else frame.f_locals.get("_attempt", -1) + 1
            )
            or any(item.get("mutation") for item in observations)
            or caller._effect_record is None
        ):
            return rows
        censuses[ledger] = dict(
            scope=scope,
            plan=plan,
            caller=caller,
            identity=identity,
            rows=canonical_bytes(rows),
            root=caller._root,
            record=caller._effect_record,
            record_bytes=canonical_bytes(caller._effect_record.to_dict()),
            observations=tuple((item, canonical_bytes(item)) for item in observations),
            stop=len(caller.observations),
            owner=caller.accounting.owner,
        )
        return rows

    def prepare_receipt_phase(run, authority, target):
        from . import quarantine_action
        from .publication_recovery import _publication_transport
        from .quarantine_capacity import native_reserve
        from .quarantine_command_receipt import project_receipt_page
        from .quarantine_lifecycle_transfer import _target_binding, sizing_snapshot
        from .session_ledger import (
            parse_session_comment,
            plain_session_comment_json_bound,
        )

        frame = _source_frame(
            quarantine_action, quarantine_action.prepare_quarantine_target
        )
        if any(
            frame.f_locals.get(key) is not value
            for key, value in (
                ("run", run),
                ("authority", authority),
                ("target", target),
            )
        ):
            raise ReviewInputError("receipt preparation target source changed")
        if (
            native_reserve(target.authenticated.archive["target_isolation"]) is not None
            or getattr(
                _publication_transport(run.http), "_quarantine_capacity_guard", None
            )
            is not None
        ):
            return
        captured = censuses.pop(run.ledger, None)
        if captured is None:
            return
        record, _ = sizing_snapshot(target, run)
        if (
            captured["caller"] is not run.caller
            or scope_binding(run.ledger, run.caller, captured["plan"])
            != captured["scope"]
            or captured["identity"] != run.identity
            or captured["record"] is not record
            or captured["root"] != run.caller._root
            or captured["record_bytes"] != canonical_bytes(record.to_dict())
            or captured["owner"] != run.caller.accounting.owner
            or any(
                canonical_bytes(item) != frozen
                for item, frozen in captured["observations"]
            )
            or any(
                item.get("mutation")
                for item in run.caller.observations[captured["stop"] :]
            )
        ):
            raise ReviewInputError("receipt preparation complete census changed")
        rows = json.loads(captured["rows"])
        roots = [
            row
            for row in rows
            if (parsed := parse_session_comment(row["body"], identity=run.identity))
            is not None
            and parsed.record_sha256 == record.record_sha256
        ]
        if len(roots) != 1:
            raise ReviewInputError("receipt preparation root membership is ambiguous")
        root_id = roots[0]["id"]
        from .session_ledger import _CONSUMED_GRANT_LINE

        grants = tuple(sorted(_CONSUMED_GRANT_LINE.findall(roots[0]["body"])))
        if (
            set(grants) != run.ledger._observed_consumed_grants
            or run.ledger._accepted_consumed_grant is not None
        ):
            return

        # Full escaped JSON body ceilings, not raw UTF-8 lengths.
        try:
            projection = project_receipt_page(
                rows,
                mutable_bounds=(
                    (
                        root_id,
                        plain_session_comment_json_bound(
                            repository_id=run.identity.repository_id,
                            pull_request=run.identity.pull_request,
                            consumed_grants=tuple(
                                sorted(run.ledger._observed_consumed_grants)
                            ),
                        )
                        + 9,
                    ),
                ),
                future_bounds=(6 * 32768 + 11, 65536),
            )
        except ReviewInputError:
            # Optional physical reader only: preserve the existing admitted
            # plain route when its complete response cannot fit one page.
            return
        prepared[run.ledger] = dict(
            run=run,
            authority=authority,
            target=target,
            target_binding=_target_binding(target),
            captured=captured,
            rows=rows,
            root_id=root_id,
            projection=projection,
            authority_bytes=canonical_bytes(authority.document),
        )

    def require_receipt_phase(run: Any, authority, *, text, marker):
        from . import action_receipt, action_run

        writer = _source_frame(action_receipt, action_receipt.publish_command_receipt)
        if (
            any(
                writer.f_locals.get(key) is not value
                for key, value in (("run", run), ("authority", authority))
            )
            or writer.f_locals.get("text") != text
            or writer.f_locals.get("marker") != marker
        ):
            raise ReviewInputError("receipt publisher source inputs changed")
        entry = prepared.pop(run.ledger, None)
        if entry is None:
            return None
        execution = _source_frame(action_run, action_run.execute_command)
        result = writer.f_locals.get("result")
        if (
            execution.f_locals.get("run") is not run
            or execution.f_locals.get("authority") is not authority
            or execution.f_locals.get("result") is not result
            or result is None
            or result.evaluation_failed is not False
        ):
            raise ReviewInputError(
                "receipt requires its original successful command result"
            )
        if result.applied is not True:
            return None

        from review_sensei.bounded_evidence import partition_evidence

        from . import quarantine_action, quarantine_candidate_receipt
        from .quarantine_command_receipt import freeze_receipt_membership
        from .quarantine_creation import _body

        require_module_seal(quarantine_action)
        from .quarantine_lifecycle_transfer import _target_binding

        if (
            entry["run"] is not run
            or _target_binding(entry["target"]) != entry["target_binding"]
            or entry["authority"] is not authority
            or canonical_bytes(authority.document) != entry["authority_bytes"]
        ):
            raise ReviewInputError("receipt authority changed after preparation")
        app_id = run.ledger._evidence_author_id
        permit = getattr(run.ledger, "_quarantine_decision_permit", None)
        if (
            type(permit) is not quarantine_action._DecisionPermit
            or permit not in quarantine_action._ISSUED_DECISIONS
            or quarantine_action._DECISION_BINDINGS.get(permit)
            != quarantine_action._decision_permit_binding(permit)
            or not permit.consumed
            or permit.ledger is not run.ledger
            or permit.authority.run is not run
            or permit.before_state != entry["target"].authenticated.state_bytes
        ):
            return None
        final = censuses.pop(run.ledger, None)
        record = run.caller._effect_record
        if final is None or record is None:
            return None
        if (
            final["caller"] is not run.caller
            or final["identity"] != run.identity
            or any(
                canonical_bytes(item) != frozen
                for item, frozen in final["observations"]
            )
            or final["owner"] != entry["captured"]["owner"]
            or run.caller._root != record.record_sha256
            or run.caller._checkpoint is not None
        ):
            raise ReviewInputError("receipt final checkpoint custody changed")
        candidate_bytes = quarantine_action._DECISION_CANDIDATES.get(permit)
        if candidate_bytes is None:
            raise ReviewInputError("receipt own decision candidate is absent")
        candidate = json.loads(candidate_bytes)
        if (
            record.generation <= candidate["generation"]
            or record.quarantine_lineage != candidate["quarantine_lineage"]
            or record.to_dict()["transaction"] != candidate["transaction"]
        ):
            raise ReviewInputError(
                "receipt final journal is not the own decision successor"
            )
        # Only a genuine final candidate GET may replace the old census root.
        payload = json.loads(
            quarantine_candidate_receipt.consume(
                run.caller, run.ledger, run.identity, record
            )
        )
        rows = json.loads(final["rows"])
        before = {row["id"]: row for row in entry["rows"]}
        tail = json.loads(permit.tail)
        ids = [int(part["storage_id"]) for part in tail["parts"]]
        if len(ids) != 1 or ids[0] in before:
            raise ReviewInputError("receipt own event identity changed")
        parts = partition_evidence(
            json.loads(entry["target"].capacity_projection.event_bytes),
            binding=tail["binding"],
            item_count=1,
        )
        if len(parts) != 1:
            raise ReviewInputError("receipt own event allocation changed")
        event_body = _body(parts[0])
        if {row["id"] for row in rows} != set(before) | set(ids):
            raise ReviewInputError("receipt final membership changed")
        for index, row in enumerate(rows):
            if row["id"] == entry["root_id"]:
                if payload["id"] != row["id"] or _semantic_owner(
                    payload
                ) != _semantic_owner(row):
                    raise ReviewInputError("receipt final root owner changed")
                rows[index] = payload
            elif row["id"] == ids[0]:
                if (
                    row["body"] != event_body
                    or row["user"].get("id") != app_id
                    or row["user"].get("type") != "Bot"
                ):
                    raise ReviewInputError("receipt own event body changed")
            elif (
                row["body"] != before[row["id"]]["body"]
                or row["user"] != before[row["id"]]["user"]
            ):
                raise ReviewInputError("receipt retained member changed")
        prior = [
            row
            for row in rows
            if row["body"].rstrip().endswith(marker) and row["user"].get("id") == app_id
        ]
        if prior:
            return None  # Existing receipt updates retain conservative pricing.
        membership = freeze_receipt_membership(
            rows,
            projection=entry["projection"],
            receipt_text=text,
            update_id=None,
            app_id=app_id,
        )
        lease = _Lease()
        leases[lease] = (
            run,
            authority,
            record,
            canonical_bytes(record.to_dict()),
            membership,
            canonical_bytes(authority.document),
            _execution_owner(run.caller.accounting.owner),
            _configuration(run.caller.accounting.configuration),
            marker,
            _blocks(run.caller),
        )
        return lease

    def validate_receipt_phase(lease, run, authority):
        from . import action_receipt
        from .publication_recovery import _publication_transport

        writer = _source_frame(action_receipt, action_receipt.publish_command_receipt)
        entry = leases.get(lease) if type(lease) is _Lease else None
        if entry is None:
            raise ReviewInputError("receipt phase is unissued or consumed")
        (
            original_run,
            original_authority,
            record,
            frozen,
            membership,
            authority_bytes,
            owner,
            configuration,
            marker,
            block_state,
        ) = entry
        run.caller._require_ledger(run.ledger, run.identity)
        current_blocks, remaining, pending = _blocks(run.caller)
        if (
            current_blocks != block_state[0]
            or remaining > block_state[1]
            or not block_state[2].issubset(pending)
            or getattr(
                _publication_transport(run.http), "_quarantine_capacity_guard", None
            )
            is not None
            or writer.f_locals.get("run") is not run
            or writer.f_locals.get("authority") is not authority
            or writer.f_locals.get("text") != membership.receipt_text
            or writer.f_locals.get("marker") != marker
            or canonical_bytes(authority.document) != authority_bytes
            or _execution_owner(run.caller.accounting.owner) != owner
            or _configuration(run.caller.accounting.configuration) != configuration
            or run is not original_run
            or authority is not original_authority
            or run.caller._effect_record is not record
            or run.caller._root != record.record_sha256
            or canonical_bytes(record.to_dict()) != frozen
            or run.caller._checkpoint is not None
        ):
            raise ReviewInputError("receipt phase root or owner changed")
        return membership

    def close_receipt_phase(lease, run, authority):
        leases.pop(lease, None)

    return (
        begin_receipt_census,
        observe_receipt_census,
        prepare_receipt_phase,
        require_receipt_phase,
        validate_receipt_phase,
        close_receipt_phase,
    )


(
    begin_receipt_census,
    observe_receipt_census,
    prepare_receipt_phase,
    require_receipt_phase,
    validate_receipt_phase,
    close_receipt_phase,
) = _make_issuer()
del _make_issuer

from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
