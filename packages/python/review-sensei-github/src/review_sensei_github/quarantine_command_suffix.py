"""Source-captured guarded suffix premises. Proposals never authorize effects."""

import json
import sys
from dataclasses import dataclass
from typing import Any
from weakref import WeakKeyDictionary

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.source_registry import require_module_seal


@dataclass(frozen=True, eq=False)
class _Census:
    pass


@dataclass(frozen=True, eq=False)
class SuffixProposal:
    pass


def _closed_event_body(run, target, record):
    """Exact next event bytes under the original entry-then-event sequence."""
    from review_sensei import bounded_evidence

    from . import quarantine_action, quarantine_command_projection

    require_module_seal(quarantine_action)
    quarantine_command_projection.require_projection_source()
    projection = target.capacity_projection
    if not projection.creates_event or not isinstance(projection.event_bytes, bytes):
        raise ReviewInputError("guarded suffix requires an exact new event")
    # The entry checkpoint is the sole predecessor of the event. Its loaded
    # generation is original+1, and _decision_binding advances it once more.
    binding = {
        **quarantine_action._decision_binding(record, run),
        "generation": record.generation + 2,
    }
    parts = bounded_evidence.partition_evidence(
        json.loads(projection.event_bytes), binding=binding, item_count=1
    )
    if len(parts) != 1:
        raise ReviewInputError("guarded suffix event exceeds one carrier")
    body = quarantine_command_projection._event_body(parts[0])
    if len(body.encode()) > bounded_evidence.MAX_PART_BYTES:
        raise ReviewInputError("guarded suffix event exceeds its original body bound")
    return body


def _registry():
    starts: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()
    completed: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()
    proposals: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()

    def source(ledger, identity, plan):
        from . import packed_comment_census, quarantine_census
        from .quarantine_capacity import installed_capacity
        from .quarantine_command_execution import _source_frame

        module = packed_comment_census if plan is not None else quarantine_census
        function = (
            module.fetch_packed_comments if plan is not None else module.fetch_comments
        )
        frame = _source_frame(module, function)
        if (
            frame.f_locals.get("ledger") is not ledger
            or frame.f_locals.get("identity") is not identity
            or frame.f_locals.get("plan") is not plan
        ):
            raise ReviewInputError("guarded suffix census source changed")
        guard = installed_capacity(ledger, identity)
        return frame, guard

    def begin_census(ledger, identity, plan):
        frame, guard = source(ledger, identity, plan)
        caller = ledger.caller
        token = _Census()
        starts[token] = (
            ledger,
            identity,
            plan,
            caller,
            guard,
            len(caller.observations),
            caller._root,
            caller._effect_record,
        )
        return token

    def owns_census(token):
        return type(token) is _Census

    def complete_census(token, rows):
        require_module_seal(sys.modules[__name__])
        saved = starts.pop(token, None) if type(token) is _Census else None
        if saved is None:
            raise ReviewInputError("guarded suffix census is copied or expired")
        ledger, identity, plan, caller, guard, before, root, record = saved
        frame, actual_guard = source(ledger, identity, plan)
        observations = caller.observations[before:]
        if (
            actual_guard is not guard
            or caller._root != root
            or caller._effect_record is not record
            or frame.f_locals.get("receipt_census") is not token
            or frame.f_locals.get("collected") is not rows
            or not 1 <= len(observations) <= 10
            or len(observations)
            != (
                frame.f_locals.get("calls")
                if plan is not None
                else frame.f_locals.get("_attempt", -1) + 1
            )
            or any(item.get("mutation") for item in observations)
        ):
            raise ReviewInputError("guarded suffix complete census custody changed")
        completed[ledger] = dict(
            identity=identity,
            caller=caller,
            guard=guard,
            root=root,
            record=record,
            rows=canonical_bytes(rows),
            stop=len(caller.observations),
            observations=tuple((item, canonical_bytes(item)) for item in observations),
        )
        from . import quarantine_historical_execution

        require_module_seal(quarantine_historical_execution)
        quarantine_historical_execution.observe_census(ledger, completed[ledger])
        return rows

    def prepare(run, authority, target):
        from . import quarantine_action
        from .quarantine_capacity import _guard_state, installed_capacity
        from .quarantine_command_execution import _source_frame
        from .quarantine_command_namespace import project_current
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
            frame.f_locals.get(name) is not value
            for name, value in (
                ("run", run),
                ("authority", authority),
                ("target", target),
            )
        ):
            raise ReviewInputError("guarded suffix target source changed")
        from .publication_recovery import _publication_transport

        if (
            getattr(
                _publication_transport(run.http), "_quarantine_capacity_guard", None
            )
            is None
        ):
            return None
        guard = installed_capacity(run.ledger, run.identity)
        if guard.complete_tail:
            # Existing complete-tail routes retain their original admission.
            # This experimental proposal concerns only the previously held
            # incomplete command envelope and cannot broaden that envelope.
            return None
        captured = completed.pop(run.ledger, None)
        state = run.caller.accounting
        if state.prior_nonstartup_liability or any(
            block.pending for block in state.blocks
        ):
            raise ReviewInputError(
                "guarded suffix retains unresolved prior physical liabilities"
            )
        record, _ = sizing_snapshot(target, run)
        if (
            captured is None
            or captured["guard"] is not guard
            or captured["caller"] is not run.caller
            or captured["identity"] != run.identity
            or captured["record"] is not record
            or captured["root"] != run.caller._root
            or any(
                canonical_bytes(item) != raw for item, raw in captured["observations"]
            )
            or any(
                item.get("mutation")
                for item in run.caller.observations[captured["stop"] :]
            )
        ):
            raise ReviewInputError("guarded suffix lacks a complete paid census")
        rows = json.loads(captured["rows"])
        roots = [
            row
            for row in rows
            if (parsed := parse_session_comment(row["body"], identity=run.identity))
            is not None
            and parsed.record_sha256 == record.record_sha256
        ]
        if len(roots) != 1 or run.ledger._accepted_consumed_grant is not None:
            raise ReviewInputError("guarded suffix root membership is unsupported")
        from .session_ledger import _CONSUMED_GRANT_LINE

        grants = tuple(sorted(_CONSUMED_GRANT_LINE.findall(roots[0]["body"])))
        if set(grants) != run.ledger._observed_consumed_grants:
            raise ReviewInputError("guarded suffix retained grant framing changed")
        # These are invocation growth promises, not replacement lifetime slots.
        # Admission must still prove and enforce the closed mutation families.
        event_body = _closed_event_body(run, target, record)
        event_wire = len(canonical_bytes({"body": event_body}))
        page = project_receipt_page(
            rows,
            mutable_bounds=(
                (
                    roots[0]["id"],
                    plain_session_comment_json_bound(
                        repository_id=run.identity.repository_id,
                        pull_request=run.identity.pull_request,
                        consumed_grants=grants,
                    )
                    + 9,
                ),
            ),
            future_bounds=(event_wire, 65536),
        )
        from .quarantine_command_finalization import intermediate_suffix_recipe

        app = frame.f_locals["app"]
        from review_sensei.source_registry import require_instance_source

        require_instance_source(
            app, methods=("_observe_quarantine_command_finalization",)
        )
        before_preparation = len(run.caller.observations)
        ordinary_before = run.caller.budget_view.ordinary_remaining()
        observation = app._observe_quarantine_command_finalization(
            run, authority, target
        )
        # Original sizing acquires a check capability and bounded read work.
        # This paid prefix is neither free observation nor a dispatch refund.
        preparation_calls = len(run.caller.observations) - before_preparation
        preparation_ceiling = guard.comment_plan.worst_read_calls() + 2
        if (
            not 1 <= preparation_calls <= preparation_ceiling
            or run.caller.budget_view.ordinary_remaining()
            != ordinary_before - preparation_calls
            or run.caller.accounting.prior_nonstartup_liability
            or any(block.pending for block in run.caller.accounting.blocks)
        ):
            raise ReviewInputError(
                "guarded suffix preparation is not a confirmed bounded paid prefix"
            )
        state = run.caller.accounting
        from .quarantine_command_namespace import capture_paid_prefix

        capture_paid_prefix(run.caller)
        recipe = intermediate_suffix_recipe(observation, comment_rows=rows)
        recipe["event_exact_body"] = event_body
        recipe["event_wire_bound"] = event_wire
        if run.caller.budget_view.ordinary_remaining() < recipe["combined_loss_calls"]:
            raise ReviewInputError("guarded suffix complete ordinary tail is unfunded")
        token = SuffixProposal()
        proposals[token] = dict(
            run=run,
            authority=authority,
            target=target,
            binding=_target_binding(target),
            state=canonical_bytes(state.to_document()),
            authority_bytes=canonical_bytes(authority.document),
            observation_count=len(run.caller.observations),
            root=run.caller._root,
            record=run.caller._effect_record,
            guard=guard,
            guard_state=_guard_state(guard),
            plan=guard.comment_plan,
            captured=captured,
            recipe=canonical_bytes(recipe),
            preparation_observations=tuple(
                (item, canonical_bytes(item))
                for item in run.caller.observations[before_preparation:]
            ),
            preparation_calls=preparation_calls,
            page=page,
            accounting=canonical_bytes(project_current(run.caller)),
        )
        return token

    def require_proposal(token, run):
        from .quarantine_capacity import _guard_state, installed_capacity
        from .quarantine_lifecycle_transfer import _target_binding

        require_module_seal(sys.modules[__name__])
        entry = proposals.get(token) if type(token) is SuffixProposal else None
        if entry is None or entry["run"] is not run:
            raise ReviewInputError("guarded suffix proposal is copied or expired")
        guard = installed_capacity(run.ledger, run.identity)
        if (
            guard is not entry["guard"]
            or _guard_state(guard) != entry["guard_state"]
            or guard.comment_plan is not entry["plan"]
            or _target_binding(entry["target"]) != entry["binding"]
            or canonical_bytes(run.caller.accounting.to_document()) != entry["state"]
            or canonical_bytes(entry["authority"].document) != entry["authority_bytes"]
            or len(run.caller.observations) != entry["observation_count"]
            or run.caller._root != entry["root"]
            or run.caller._effect_record is not entry["record"]
            or any(
                canonical_bytes(item) != raw
                for item, raw in entry["preparation_observations"]
            )
        ):
            raise ReviewInputError("guarded suffix proposal source changed")
        return entry["page"]

    def require_execution(token, run):
        require_proposal(token, run)
        entry = proposals[token]
        from . import quarantine_action, quarantine_historical_execution

        require_module_seal(quarantine_action)
        frame = sys._getframe(1)
        if (
            frame.f_code is not quarantine_action.prepare_quarantine_target.__code__
            or frame.f_globals is not vars(quarantine_action)
            or frame.f_locals.get("suffix") is not token
            or frame.f_locals.get("run") is not run
            or frame.f_locals.get("target") is not entry["target"]
        ):
            raise ReviewInputError(
                "guarded execution requires original target preparation"
            )

        require_module_seal(quarantine_historical_execution)
        return quarantine_historical_execution.activate(token, run, entry)

    return (
        begin_census,
        owns_census,
        complete_census,
        prepare,
        require_proposal,
        require_execution,
    )


(
    begin_census,
    owns_census,
    complete_census,
    prepare,
    require_proposal,
    require_execution,
) = _registry()
del _registry

from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
