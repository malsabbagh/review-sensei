"""Source-bound hypothetical finalizer observations, never effect authority.

These preparations exist only before the command's action boundary. Their
projected assessment view cannot be consumed by ordinary approval execution.
Physical admission must separately prove every native and lifecycle tail.
"""

import inspect
import json
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from types import FrameType
from typing import Any
from weakref import WeakKeyDictionary

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.quarantine_assessments import assessment_view
from review_sensei.source_registry import require_module_seal

from .quarantine_command_projection import project_decision, require_projection_source
from .quarantine_lifecycle_transfer import sizing_snapshot

_PREPARED: WeakKeyDictionary = WeakKeyDictionary()
_OBSERVATIONS: WeakKeyDictionary = WeakKeyDictionary()
_NATIVE_PLANS: WeakKeyDictionary = WeakKeyDictionary()


@dataclass(frozen=True, eq=False)
class QuarantineFinalizerPreparation:
    """Opaque process-local handle, with no read or write authorization."""


@dataclass(frozen=True, eq=False)
class QuarantineFinalizerObservation:
    """Data only; deliberately not a CommandFinalizationPlan."""

    payload: bytes
    state_bytes: bytes


def observe_plan(finalizer, *, payload, states, check_token, cleanup_instances=None):
    import inspect

    from review_sensei.finding_lifecycle import encode_lifecycle_state

    from . import publication

    require_module_seal(publication)
    frame = sys._getframe(1)
    source = publication.ReviewApprovalFinalizer._refinalize_with_dispositions_impl
    if (
        frame.f_code is not inspect.unwrap(source).__code__
        or frame.f_globals is not vars(publication)
        or frame.f_locals.get("self") is not finalizer
        or frame.f_locals.get("compacted") is not False
        or frame.f_locals.get("update_capacity_states") is not states
        or frame.f_locals.get("check_token") is not check_token
        or frame.f_locals.get("cleanup_instances") is not cleanup_instances
        or payload.get("review_before") is not frame.f_locals.get("chosen")
        or canonical_bytes(payload.get("frontier")) != frame.f_locals.get("plain")
    ):
        raise ReviewInputError(
            "quarantine sizing requires original uncompacted plan source"
        )
    if finalizer is None or states is None:
        raise ReviewInputError("quarantine sizing original inputs are unavailable")
    require(finalizer._quarantine_capacity_preparation, finalizer=finalizer)
    if (
        payload.get("read_contract") != "quarantine-sizing-observation-only-v1"
        or payload.get("execution_read_tail") is not None
        or payload.get("execution_capability_tail") is not None
    ):
        raise ReviewInputError(
            "quarantine observation cannot supply physical admission"
        )
    observed = QuarantineFinalizerObservation(
        canonical_bytes(payload),
        canonical_bytes([encode_lifecycle_state(state) for state in states]),
    )
    _OBSERVATIONS[observed] = (
        finalizer._quarantine_capacity_preparation,
        observed.payload,
        observed.state_bytes,
    )
    # This private source plan is only an input to the existing pure branch
    # generator. Its nonadmitting contract still cannot enter ordinary execution.
    from . import command_finalization_plan

    require_module_seal(command_finalization_plan)
    sizing_finalizer = _detached_sizing_finalizer(finalizer)
    _NATIVE_PLANS[observed] = command_finalization_plan.issue_plan(
        sizing_finalizer,
        payload=payload,
        states=states,
        check_token=check_token,
        cleanup_instances=cleanup_instances,
    )
    return observed


def require_observation(observation):
    """Authenticate the observation for further sizing, never for execution."""
    require_module_seal(sys.modules[__name__])
    saved = (
        _OBSERVATIONS.get(observation)
        if type(observation) is QuarantineFinalizerObservation
        else None
    )
    if (
        saved is None
        or observation.payload != saved[1]
        or observation.state_bytes != saved[2]
    ):
        raise ReviewInputError(
            "quarantine finalizer observation is unissued or changed"
        )
    return require(saved[0])


def require_finalizer_copy_source(finalizer):
    """Reject executable instance substitution before invoking copy protocols."""
    from review_sensei.source_registry import require_instance_source

    from .publication import ReviewApprovalFinalizer

    if type(finalizer) is not ReviewApprovalFinalizer:
        raise ReviewInputError("quarantine finalizer copy source is unsupported")
    require_instance_source(finalizer, methods=())
    if any(
        name in finalizer.__dict__
        for name in (
            "__copy__",
            "__deepcopy__",
            "__reduce__",
            "__reduce_ex__",
            "__getstate__",
            "__setstate__",
        )
    ):
        raise ReviewInputError("quarantine finalizer copy protocol changed")
    methods = tuple(
        name
        for name in dir(type(finalizer))
        if not name.startswith("__") and callable(getattr(type(finalizer), name))
    )
    require_instance_source(finalizer, methods=methods)


def hydrate_sizing_states(preparation, finalizer):
    """Install retained state only in the already detached observation finalizer."""
    from review_sensei.active_finding_window import ActiveWindowRoundState
    from review_sensei.finding_lifecycle import decode_lifecycle_state

    saved = require(preparation)
    require_finalizer_copy_source(finalizer)
    if finalizer is saved["app"].reviewer.finalizer:
        raise ReviewInputError("quarantine sizing cannot hydrate the live finalizer")
    document = json.loads(saved["document"])
    state = decode_lifecycle_state(document["lifecycle"]).state
    binding = saved["record"].finding_lifecycle["manifest"]["binding"]
    if state.head_sha != binding["head_sha"] or state.base_sha != binding["base_sha"]:
        raise ReviewInputError("quarantine sizing retained snapshot changed")
    key = (saved["run"].identity.repository_id, saved["run"].identity.pull_request)
    finalizer._active_window_states = dict(finalizer._active_window_states)
    finalizer._drift_states = dict(finalizer._drift_states)
    if isinstance(state, ActiveWindowRoundState):
        finalizer._active_window_states[key] = state
        finalizer._drift_states.pop(key, None)
    else:
        finalizer._drift_states[key] = state
        finalizer._active_window_states.pop(key, None)
    finalizer._unknown_effect_holds = set(finalizer._unknown_effect_holds)
    if document["unknown_trust_effect"]:
        finalizer._unknown_effect_holds.add(key)


def _detached_sizing_finalizer(finalizer):
    """Hydrate only detached sizing objects from the authenticated original bytes."""
    from copy import copy

    from review_sensei.finding_lifecycle import decode_lifecycle_state
    from review_sensei.source_registry import require_instance_source

    saved = require(finalizer._quarantine_capacity_preparation, finalizer=finalizer)
    require_finalizer_copy_source(finalizer)
    original = finalizer._finding_lifecycle_journal
    for owner in (original, original.reviewer):
        require_instance_source(owner, methods=())
        if any(
            name in owner.__dict__
            for name in (
                "__copy__",
                "__deepcopy__",
                "__reduce__",
                "__reduce_ex__",
                "__getstate__",
                "__setstate__",
            )
        ):
            raise ReviewInputError("quarantine detached sizing copy source changed")
        require_instance_source(
            owner,
            methods=tuple(
                name
                for name in dir(type(owner))
                if not name.startswith("__") and callable(getattr(type(owner), name))
            ),
        )
    document = json.loads(saved["document"])
    restored = decode_lifecycle_state(document["lifecycle"])
    if (
        restored.state.head_sha != original.head_sha
        or restored.state.base_sha != original.base_sha
    ):
        raise ReviewInputError("quarantine detached sizing snapshot changed")
    journal = copy(original)
    reviewer = copy(original.reviewer)
    journal.reviewer = reviewer
    journal._planned = restored.state
    journal._input_identity = restored.input_identity
    key = (journal.identity.repository_id, journal.identity.pull_request)
    reviewer._drift_concern_history = {
        (*key, obligation): concerns
        for obligation, concerns in restored.concern_history.items()
    }
    reviewer._native_finding_receipt_records = {
        key: {receipt["instance"]: receipt for receipt in document["native_receipts"]}
    }
    reviewer._unknown_effect_holds = (
        {key} if document["unknown_trust_effect"] else set()
    )
    if canonical_bytes(journal._document()) != saved["document"]:
        raise ReviewInputError("quarantine detached sizing lost retained evidence")
    detached = copy(finalizer)
    detached._finding_lifecycle_journal = journal
    return detached


def measure_inline_branches(observation, *, _retain_manifest=False):
    """Keep the original caller's width witness unchanged after pure sizing."""
    if type(_retain_manifest) is not bool:
        raise ReviewInputError("LAST manifest selector must be an exact bool")
    if _retain_manifest:
        from . import quarantine_last_manifest

        require_module_seal(quarantine_last_manifest)
        quarantine_last_manifest.preliminary_participants(observation)
    saved = require_observation(observation)
    caller = saved["run"].caller
    missing = object()
    previous = getattr(caller, "_accepted_semantic_capacity", missing)
    try:
        return _measure_inline_branches(observation, _retain_manifest=_retain_manifest)
    finally:
        if previous is missing:
            caller.__dict__.pop("_accepted_semantic_capacity", None)
        else:
            caller._accepted_semantic_capacity = previous


def _root_growth_measurement(
    record, accounting, witness, result_upper=None, grants=(), terminal_accounting=None
):
    """Validate and render inert full-width roots with original source machinery.

    The result carrier is deliberately not decodable evidence. These detached
    records never enter a ledger, a transfer registry or an execution grant.
    """
    from review_sensei import finding_lifecycle_store, session
    from review_sensei.api_work import ACTION_NOOP_REASONS

    from . import session_ledger

    require_module_seal(session)
    require_module_seal(session_ledger)
    if result_upper is None:
        raise ReviewInputError("cold terminal root lacks its complete result shape")
    raw = record.to_dict()
    raw["api_run_accounting"] = json.loads(canonical_bytes(accounting))
    raw.update(
        generation=session.MAX_GENERATION - 1,
        completed_initial_reviews=session.DIAGNOSTIC_ROUND_CEILING,
        completed_verification_rounds=session.DIAGNOSTIC_ROUND_CEILING,
        failed_attempts=session.MAX_FAILED_ATTEMPTS,
        updated_at="9999-12-31T23:59:59.999999Z",
    )
    # Created/expiry timestamps are immutable on this route, unlike updated_at.
    if witness is not None and raw.get("finding_lifecycle") is not None:
        raw["finding_lifecycle"] = {
            **raw["finding_lifecycle"],
            "schema_version": finding_lifecycle_store.HISTORY_REFERENCE_VERSION,
            "accepted_semantic_history": witness["history"],
        }

    def render(document):
        payload = {
            key: value for key, value in document.items() if key != "record_sha256"
        }
        document["record_sha256"] = session._digest_payload(payload)
        candidate = session.SessionRecord.from_dict(document)
        body = session_ledger.render_session_comment(
            repository_id=record.repository_id,
            pull_request=record.pull_request,
            record=candidate,
            consumed_grants=grants,
        )
        # Exercise the exact original readable comment contract as well as
        # construction: its marker/schema cannot be inferred from byte counts.
        restored = session_ledger.parse_session_comment(
            body, identity=record.identity()
        )
        if restored is None or canonical_bytes(restored.to_dict()) != canonical_bytes(
            candidate.to_dict()
        ):
            raise ReviewInputError("cold projected comment is not exactly readable")
        return len(canonical_bytes(candidate.to_dict())), len(body.encode())

    ordinary, ordinary_body = render(raw)
    terminal_raw = json.loads(canonical_bytes(raw))
    terminal_raw["generation"] = session.MAX_GENERATION
    if terminal_accounting is not None:
        terminal_raw["api_run_accounting"] = json.loads(
            canonical_bytes(terminal_accounting)
        )
    terminal_raw["api_run_accounting"]["action"].update(
        phase="terminal",
        execution_started=True,
        result=result_upper,
        outcome={"kind": "explicit_noop", "reason": max(ACTION_NOOP_REASONS, key=len)},
    )
    terminal, terminal_body = render(terminal_raw)
    return {
        "ordinary_root_bytes": ordinary,
        "comment_body_bytes": max(ordinary_body, terminal_body),
        "terminal_root_bytes": terminal,
        "root_fits": True,
        "original_validated_and_rendered": True,
    }


def _measure_inline_branches(observation, *, _retain_manifest=False):
    """Measure all reached prefixes under the original inline/root/history caps.

    No reader price, part allocation or permission is inferred from this value.
    A single non-inline branch refuses the proposed inline-only route.
    """
    from review_sensei import finding_lifecycle_store
    from review_sensei.source_registry import require_instance_source

    from . import accepted_semantic_history, finding_lifecycle, quarantine_action
    from .publication import ReviewPublisher

    if type(_retain_manifest) is not bool:
        raise ReviewInputError("LAST manifest selector must be an exact bool")
    if _retain_manifest:
        from . import quarantine_last_manifest

        require_module_seal(quarantine_last_manifest)
        quarantine_last_manifest.preliminary_participants(observation)
    saved = require_observation(observation)
    if _retain_manifest:
        from . import quarantine_last_manifest

        require_module_seal(quarantine_last_manifest)
        frame = sys._getframe(2)
        if (
            frame.f_code is not quarantine_last_manifest.measure.__code__
            or frame.f_globals is not vars(quarantine_last_manifest)
            or frame.f_locals.get("observation") is not observation
        ):
            raise ReviewInputError("LAST manifest requires original measurement entry")
        from . import quarantine_historical_execution

        require_module_seal(quarantine_historical_execution)
        quarantine_last_manifest.require_inline_participants(
            saved["run"].ledger, saved["run"].caller
        )
        quarantine_historical_execution._require_default_profile(saved["run"].caller)
    plan = _NATIVE_PLANS.get(observation)
    if plan is None:
        raise ReviewInputError("quarantine native branch source is unavailable")
    if (
        plan.payload != observation.payload
        or plan.state_bytes != observation.state_bytes
    ):
        raise ReviewInputError("quarantine native branch observation changed")
    require_module_seal(finding_lifecycle_store)
    require_module_seal(finding_lifecycle)
    require_module_seal(accepted_semantic_history)
    journal = plan.journal
    if type(journal.reviewer) is not ReviewPublisher:
        raise ReviewInputError("quarantine branch reviewer source is unsupported")
    for owner in (journal, journal.reviewer):
        require_instance_source(owner, methods=())
        if any(
            name in owner.__dict__
            for name in (
                "__copy__",
                "__deepcopy__",
                "__reduce__",
                "__reduce_ex__",
                "__getstate__",
                "__setstate__",
            )
        ):
            raise ReviewInputError("quarantine branch copy protocol changed")
        require_instance_source(
            owner,
            methods=tuple(
                name
                for name in dir(type(owner))
                if not name.startswith("__") and callable(getattr(type(owner), name))
            ),
        )
    run, target, record = saved["run"], saved["target"], saved["record"]
    action = run.caller.accounting.action
    if action is None or action.get("execution_started") is not False:
        raise ReviewInputError("quarantine branch measurement is not pre-effect")
    accepted_semantic_history.preflight_history_capacity(
        run.caller, run.identity, action["intent"]
    )
    alternatives, _pending, item_count, requests, _dictionary = (
        journal._command_branch_documents(plan, http=plan.http, budget=plan.budget)
    )
    if _retain_manifest:
        payload = json.loads(plan.payload)
        if (
            payload.get("cleanup_requests")
            or payload.get("conditional_approval") is None
        ):
            raise ReviewInputError(
                "LAST manifest requires an uncompacted approval branch"
            )
        initial_accounting = quarantine_last_manifest.accounting_bytes(
            run.caller.accounting
        )
        initial_root = run.caller._root
        initial_record = canonical_bytes(record.to_dict())
    generation = record.generation + (2 if saved["projection"].creates_event else 1)
    fields = {}
    if saved["projection"].creates_event:
        projected = project_decision(
            target.authenticated,
            record,
            quarantine_action._decision_value(
                run,
                saved["authority"],
                target.command,
                target.instance,
                target.permission,
            ),
            binding={
                **quarantine_action._decision_binding(record, run),
                "generation": generation,
            },
        )
        fields["quarantine_lineage"] = {
            **record.quarantine_lineage,
            "assessment_state_manifest": json.loads(projected.cursor_manifest_bytes),
        }
    prospective = record.evolve(generation=generation, **fields)
    from .quarantine_command_namespace import project_prepared_prefix

    accounting_upper = project_prepared_prefix(run.caller)
    from . import historical_terminal_projection

    require_module_seal(historical_terminal_projection)
    terminal_projection = historical_terminal_projection.prepare(
        observation, accounting_upper
    )
    terminal_upper = None if terminal_projection is None else terminal_projection[0]
    witness = getattr(run.caller, "_accepted_semantic_capacity", None)
    result_upper = None
    if saved["projection"].creates_event:
        import hashlib

        from .api_run_history import action_result_binding
        from .command_result_capacity import (
            command_result_capacity_upper,
            future_journal_approval_result_capacity_upper,
        )

        selected = json.loads(target.native_review_bytes)
        source_payload = json.loads(plan.payload)
        if (
            source_payload["review_before"]["id"] != selected["id"]
            or source_payload["head_sha"] != run.head_sha
            or type(selected.get("id")) is not int
            or not 0 < selected["id"] <= 2**63 - 1
            or len(run.head_sha) != 40
        ):
            raise ReviewInputError(
                "historical result native scalar source is unsupported"
            )
        base_result = quarantine_action.historical_command_result(
            target.command,
            target,
            record,
            existing=None,
            actor=saved["authority"].document["actor"],
        )
        approval = source_payload.get("conditional_approval")
        if approval is not None and (
            type(approval) is not list
            or len(approval) != 3
            or approval[:2]
            != [
                "POST",
                plan.http.repository_path(
                    run.identity.repository,
                    f"/pulls/{run.identity.pull_request}/reviews",
                ),
            ]
            or type(approval[2]) is not dict
            or approval[2].get("event") != "APPROVE"
            or approval[2].get("commit_id") != run.head_sha
        ):
            raise ReviewInputError("historical future approval source is unsupported")
        # Detached conditional sizing only. The intermediate execution issuer
        # still rejects this branch; an eventual lease must prove all approval
        # outputs pass the original journal's signed-63 receipt validator.
        upper_factory = (
            command_result_capacity_upper
            if approval is None
            else future_journal_approval_result_capacity_upper
        )
        approval_fields = {"approval_review_id": None} if approval is None else {}
        result_upper = upper_factory(
            base_result,
            binding=action_result_binding(
                run.identity, run.caller.accounting, run.ledger.evidence_producer()
            ),
            intent_sha256=hashlib.sha256(canonical_bytes(action["intent"])).hexdigest(),
            outcome={"kind": "applied", "reason": None},
            call_limit=run.caller.accounting.configuration.limit,
            reference_history=True,
            source_review_id=selected["id"],
            **approval_fields,
        )
    grants = tuple(sorted(run.ledger._observed_consumed_grants))
    growth_roots = [
        _root_growth_measurement(
            root, accounting_upper, witness, result_upper, grants, terminal_upper
        )
        for root in (record, prospective)
    ]
    branches = []
    retained = []
    for documents, _exact in alternatives:
        current = prospective
        checkpoints = []
        retained_checkpoints = []
        for document in documents:
            _reference, envelope, _parts, _allowance, candidate = (
                finding_lifecycle_store._prepare_lifecycle(
                    run.ledger,
                    current,
                    document=document,
                    operation_id=journal.operation_id,
                    base_sha=journal.base_sha,
                    head_sha=journal.head_sha,
                    policy_digest=journal.policy_digest,
                    configuration_digest=journal.configuration_digest,
                    item_count=item_count,
                )
            )
            if candidate.finding_lifecycle["manifest"]["encoding"] != "inline-json-v1":
                raise ReviewInputError(
                    "quarantine complete command requires a non-inline checkpoint"
                )
            growth = _root_growth_measurement(
                candidate,
                accounting_upper,
                witness,
                result_upper,
                grants,
                terminal_upper,
            )
            growth_roots.append(growth)
            checkpoints.append(
                {
                    "growth": growth,
                    "root_bytes": len(canonical_bytes(candidate.to_dict())),
                    "reference_bytes": len(
                        canonical_bytes(candidate.finding_lifecycle)
                    ),
                    "envelope_bytes": len(canonical_bytes(envelope)),
                }
            )
            if _retain_manifest:
                retained_checkpoints.append(
                    {
                        "parent": current.to_dict(),
                        "document": document,
                        "candidate": candidate.to_dict(),
                        "envelope": envelope,
                    }
                )
            current = candidate
        branches.append(tuple(checkpoints))
        if _retain_manifest:
            # Original conservative whole-sequence plan, NOT an issued local
            # pending/finish operation. No exact-document or reader grant.
            quarantine_last_manifest.require_inline_participants(
                run.ledger, run.caller, prospective=prospective
            )
            operation_plan = finding_lifecycle_store.preflight_finding_lifecycle(
                run.ledger,
                prospective,
                document=documents[0],
                future_documents=tuple(documents[1:]),
                operation_id=journal.operation_id,
                base_sha=journal.base_sha,
                head_sha=journal.head_sha,
                policy_digest=journal.policy_digest,
                configuration_digest=journal.configuration_digest,
                item_count=item_count,
                reserved_tail_calls=7,
            )
            retained.append(
                {
                    "checkpoints": retained_checkpoints,
                    "conservative_sequence_plan": operation_plan,
                }
            )
    require_observation(observation)
    result = {
        "terminal_projection": None
        if terminal_projection is None
        else terminal_projection[1],
        "branches": tuple(branches),
        "growth_roots": tuple(growth_roots),
        "result_manifest_bound": len(canonical_bytes(result_upper)),
        "future_approval_domain_requires_execution_proof": json.loads(plan.payload).get(
            "conditional_approval"
        )
        is not None,
        "all_growth_roots_fit": all(root["root_fits"] for root in growth_roots),
        "max_checkpoints": max(len(branch) for branch in branches),
        "native_requests": requests,
        "prospective_root_bytes": len(canonical_bytes(prospective.to_dict())),
    }
    if _retain_manifest:
        if (
            run.caller._root != initial_root
            or quarantine_last_manifest.accounting_bytes(run.caller.accounting)
            != initial_accounting
            or canonical_bytes(record.to_dict()) != initial_record
            or plan.payload != observation.payload
            or plan.state_bytes != observation.state_bytes
        ):
            raise ReviewInputError("LAST sizing changed original state")
        result["last_manifest"] = {
            "admission": False,
            "accounting": json.loads(initial_accounting),
            "record": json.loads(initial_record),
            "root": initial_root,
            "payload": json.loads(plan.payload),
            "states": json.loads(plan.state_bytes),
            "branches": retained,
            "operation_plan_kind": "conservative-whole-sequence-not-local-operation",
        }
    return result


def _require_no_body_readback(journal, review_id):
    """The inline-only recipe cannot omit an independently retained body GET."""
    from review_sensei.source_registry import require_instance_source

    require_instance_source(journal, methods=("_body_receipts",))
    require_instance_source(journal.reviewer, methods=("native_finding_receipts",))
    path = journal.ledger.http.repository_path(
        journal.identity.repository,
        f"/pulls/{journal.identity.pull_request}/reviews/{review_id}",
    )
    if journal._body_receipts(review_id) or any(
        effect["method"] == "PUT"
        and effect["path"] == path
        and "review_update_precondition" in effect
        for effect in journal.effects
    ):
        raise ReviewInputError(
            "guarded intermediate body receipt requires separately priced readback"
        )


def _intermediate_stages(C, R, receipt_reads, trust_calls):
    """Closed source call bounds, not a refund or an execution grant."""
    values = (C, R, receipt_reads, trust_calls)
    if any(type(value) is not int for value in values) or values not in {
        (4, 3, 1, 7),
        (5, 3, 1, 0),
        (5, 2, 2, 0),
    }:
        raise ReviewInputError("guarded intermediate source recipe is unsupported")
    return (
        ("entry-authority", 2),
        ("entry-checkpoint", C + 5),
        ("complete-inventory", C),
        ("event-authority-and-echo", 6),
        ("own-cas-authority-and-checkpoint", 4 + C + 5),
        ("post-cas-authority", 4),
        ("review-capability", 1),
        ("initial-reviews", R),
        ("carried-hold", C + 1),
        ("pre-update-reviews", R),
        ("pending-journal", 4 + C + 4),
        ("review-update", 1),
        ("post-update-reviews", R),
        ("check-capability-and-replay", 3),
        ("final-hold", C + 1),
        ("finish-journal", 4 + C + 4),
        ("receipt", 4 + receipt_reads),
        ("trust-verifications", trust_calls),
    )


def _default_c5_prefix_headroom(stages, *, terminal_comments, receipt_reads=1):
    """Local reservation liquidity under all four ordinary root read losses."""
    from . import api_run_plan

    require_module_seal(api_run_plan)
    overhead = (
        api_run_plan.QUARANTINE_CHECKPOINT_REQUESTS
        - api_run_plan.READ_OPERATION_REQUESTS
    )
    if overhead != 6 or api_run_plan.READ_OPERATION_REQUESTS != 10:
        raise ReviewInputError("guarded C5 checkpoint source constants changed")
    if type(receipt_reads) is not int or receipt_reads not in (1, 2):
        raise ReviewInputError("guarded C5 receipt reader is unsupported")
    expected = _intermediate_stages(5, 3 if receipt_reads == 1 else 2, receipt_reads, 0)
    if (
        type(stages) is not tuple
        or any(
            type(row) is not tuple
            or len(row) != 2
            or type(row[0]) is not str
            or type(row[1]) is not int
            for row in stages
        )
        or stages != expected
    ):
        raise ReviewInputError("guarded C5 source stages changed")
    C = 5
    checkpoint = 4 + C + overhead
    minima = {
        "entry-checkpoint": C + overhead,
        "own-cas-authority-and-checkpoint": checkpoint,
        "pending-journal": checkpoint + checkpoint + 6,
        "review-update": 1 + api_run_plan.NATIVE_COMMAND_UPDATE_CONFIRMATION_REQUESTS,
        "finish-journal": checkpoint,
        "receipt": 2 * receipt_reads + 6,
    }
    losses = {
        "entry-checkpoint",
        "own-cas-authority-and-checkpoint",
        "pending-journal",
        "finish-journal",
    }
    remaining = 105
    rows = []
    for name, calls in stages:
        if name in minima:
            required = minima[name]
            rows.append({"phase": name, "available": remaining, "minimum": required})
            if remaining < required:
                raise ReviewInputError("guarded C5 local reservation is unfunded")
        remaining -= (
            calls
            + int(name in losses)
            + (receipt_reads + 2 if name == "receipt" else 0)
        )
    if (
        remaining != receipt_reads - 1
        or type(terminal_comments) is not int
        or not 1 <= terminal_comments <= 10
        or terminal_comments + overhead > 50
    ):
        raise ReviewInputError("guarded C5 complete prefix accounting changed")
    return rows


def journal_c3_101_model():
    """Pure complete-prefix DATA; lifetime operation minima are unchanged."""
    from . import api_run_plan

    require_module_seal(api_run_plan)
    overhead = (
        api_run_plan.QUARANTINE_CHECKPOINT_REQUESTS
        - api_run_plan.READ_OPERATION_REQUESTS
    )
    checkpoint = 4 + 5 + overhead
    if overhead != 6 or api_run_plan.NATIVE_COMMAND_UPDATE_CONFIRMATION_REQUESTS != 34:
        raise ReviewInputError("historical C3 original reservation constants changed")
    stages = tuple(
        (name, calls - 2 if name in ("pending-journal", "finish-journal") else calls)
        for name, calls in _intermediate_stages(5, 2, 2, 0)
    )
    minima = {
        "entry-checkpoint": 5 + overhead,
        "own-cas-authority-and-checkpoint": checkpoint,
        "pending-journal": 2 * checkpoint + 6,
        "review-update": 1 + api_run_plan.NATIVE_COMMAND_UPDATE_CONFIRMATION_REQUESTS,
        "finish-journal": checkpoint,
        "receipt": 10,
    }
    losses = {
        "entry-checkpoint",
        "own-cas-authority-and-checkpoint",
        "pending-journal",
        "finish-journal",
    }
    remaining = 101
    headroom = []
    for name, calls in stages:
        if name in minima:
            headroom.append(
                {"phase": name, "available": remaining, "minimum": minima[name]}
            )
            if remaining < minima[name]:
                raise ReviewInputError("historical C3 local liquidity is insufficient")
        remaining -= calls + int(name in losses) + (4 if name == "receipt" else 0)
    # A transient PUT stops the noncompacted publisher before its R2/check/hold
    # continuation. Pay recovery's GET even though PUT recovery then declines,
    # AND finish's independent GET. Do not combine that stop with success reads.
    put_available = next(
        row["available"] for row in headroom if row["phase"] == "review-update"
    )
    stop_remaining = put_available - 1 - 1 - 1 - 12 - 10
    if remaining != 1 or stop_remaining < 0:
        raise ReviewInputError("historical C3 complete branch funding changed")
    return {
        "stages": stages,
        "normal_calls": sum(n for _, n in stages),
        "combined_loss_calls": 100,
        "ordinary_required": 101,
        "local_headroom": headroom,
        "put_stop": {
            "native": 1,
            "recovery_readback": 1,
            "independent_finish_readback": 1,
            "finish_with_loss": 12,
            "conservative_receipt_with_loss": 10,
            "remaining": stop_remaining,
        },
    }


def require_c3_101_observation(observation):
    """Revalidate original prepared journal, not an observed numeric quote."""
    from . import quarantine_historical_execution

    require_module_seal(sys.modules[__name__])
    require_module_seal(quarantine_historical_execution)
    require_observation(observation)
    plan = _NATIVE_PLANS.get(observation)
    if plan is None:
        raise ReviewInputError("historical C3 native preparation is absent")
    quarantine_historical_execution.require_no_ordinary_witness(plan.journal)
    payload = json.loads(plan.payload)
    _require_no_body_readback(plan.journal, payload["review_before"]["id"])
    if payload.get("conditional_approval") is not None or payload.get(
        "cleanup_requests"
    ):
        raise ReviewInputError("historical C3 native branch changed")


def exact_intermediate_receipt(
    *,
    action,
    scope,
    instance,
    operator_paused,
    actor,
    source_comment_id,
    command_digest,
):
    """Pure source-produced receipt DATA, never permission to publish it.

    Callers must bind these primitives to their original target/authority. The
    actual receipt window independently requires byte-identical dispatch text.
    """
    from types import SimpleNamespace

    from review_sensei import presentation

    from . import quarantine_action

    require_module_seal(sys.modules[__name__])
    require_module_seal(quarantine_action)
    require_module_seal(presentation)
    strings = (action, scope, instance, actor, command_digest)
    if (
        any(type(value) is not str for value in strings)
        or type(operator_paused) is not bool
        or type(source_comment_id) is not int
    ):
        raise ReviewInputError("intermediate receipt primitives are invalid")
    if (
        action not in ("dismiss", "accept-risk")
        or not 0 < source_comment_id <= 2**63 - 1
        or any(not value or len(value) > 65536 for value in strings)
    ):
        raise ReviewInputError("intermediate receipt primitive bounds changed")
    result = quarantine_action.historical_command_result(
        SimpleNamespace(action=action, scope=scope),
        SimpleNamespace(instance=instance),
        SimpleNamespace(operator_paused=operator_paused),
        existing=None,
        actor={"login": actor},
    )
    text = presentation.render_command_receipt(
        actor=actor,
        source_comment_id=source_comment_id,
        command_digest=command_digest,
        receipts=result.receipts,
        ledger_written=True,
    )
    wire = len(canonical_bytes({"body": text}))
    if wire > 65536:
        raise ReviewInputError(
            "guarded suffix receipt exceeds its complete wire projection"
        )
    return text, wire


def intermediate_suffix_recipe(observation, *, comment_rows, receipt_page):
    """Derive the closed intermediate recipe; data, not an execution lease."""
    from copy import copy, deepcopy

    from review_sensei.bounded_evidence import partition_evidence

    from . import (
        finding_lifecycle,
        quarantine_action,
        quarantine_capacity,
        quarantine_command_receipt,
        quarantine_command_suffix,
    )
    from .quarantine_creation import _body

    require_module_seal(quarantine_command_suffix)
    frame = sys._getframe(1)
    if (
        frame.f_code is not quarantine_command_suffix.prepare.__code__
        or frame.f_globals is not vars(quarantine_command_suffix)
        or frame.f_locals.get("observation") is not observation
        or frame.f_locals.get("page") is not receipt_page
        or frame.f_locals.get("rows") is not comment_rows
        or frame.f_locals.get("captured", {}).get("rows")
        != canonical_bytes(comment_rows)
    ):
        raise ReviewInputError("guarded suffix cache prefix lacks original paid census")
    require_module_seal(quarantine_command_receipt)
    if (
        type(receipt_page) is not quarantine_command_receipt.ReceiptPageProjection
        or quarantine_command_receipt._projection_binding(receipt_page)
        != frame.f_locals.get("page_binding")
    ):
        raise ReviewInputError("guarded suffix receipt projection changed")
    receipt_reads = 2 if receipt_page.pages else 1
    saved = require_observation(observation)
    plan = _NATIVE_PLANS.get(observation)
    if plan is None or saved["guard"] is None:
        raise ReviewInputError("guarded intermediate source plan is unavailable")
    for module in (finding_lifecycle, quarantine_action, quarantine_capacity):
        require_module_seal(module)
    payload = json.loads(plan.payload)
    review, check = payload.get("review_update"), payload.get("check_request")
    if (
        not saved["projection"].creates_event
        or review is None
        or review[0] != "PUT"
        or check is None
        or "/check-runs" not in check[1]
        or payload.get("cleanup_requests")
        or payload.get("conditional_approval") is not None
    ):
        raise ReviewInputError(
            "guarded suffix requires one update and check replay only"
        )
    _require_no_body_readback(plan.journal, payload["review_before"]["id"])
    if any(effect["status"] == "pending" for effect in plan.journal.effects):
        raise ReviewInputError(
            "guarded suffix retains a separately priced native pending effect"
        )
    method, path, raw = check
    keys = {
        finding_lifecycle._digest({"method": method, "path": path, "request": value})
        for value in (raw, finding_lifecycle._request_identity(raw))
    }
    matches = [
        effect for effect in plan.journal.effects if effect["request_sha256"] in keys
    ]
    if len(matches) != 1 or matches[0]["status"] != "confirmed":
        raise ReviewInputError("guarded suffix check is not an authenticated replay")
    measured = measure_inline_branches(observation)
    if (
        measured["max_checkpoints"] != 2
        or any(len(branch) != 2 for branch in measured["branches"])
        or len(measured["native_requests"]) != 2
    ):
        raise ReviewInputError(
            "guarded suffix has additional native/checkpoint branches"
        )
    guard = saved["guard"]
    original = quarantine_capacity._guard_state(guard)
    projected = copy(guard)
    for name in (
        "stats",
        "objects",
        "certificates",
        "comment_bounds",
        "review_bounds",
        "check_bounds",
        "inline_bounds",
        "reaction_bounds",
    ):
        setattr(projected, name, deepcopy(getattr(guard, name)))
    from .native_operation_envelope import OPERATION_WIRE_BOUND
    from .operation_host import OPERATION_COMMENT_MARKER
    from .packed_comment_census import CommentAllocationPlan
    from .session_ledger import parse_session_comment

    if not isinstance(comment_rows, list):
        raise ReviewInputError("guarded suffix complete census rows are unavailable")
    full_known = {}
    for row in comment_rows:
        identifier, body = row["id"], row["body"]
        if identifier in full_known:
            raise ReviewInputError("guarded suffix paid census has duplicate members")
        bound = max(
            guard.comment_plan.known.get(identifier, 0),
            quarantine_capacity._wire({"body": body}),
        )
        if body.startswith(OPERATION_COMMENT_MARKER + "\n"):
            bound = max(bound, OPERATION_WIRE_BOUND)
        if parse_session_comment(body, identity=saved["run"].identity) is not None:
            bound = max(bound, guard.closure_wire)
        full_known[identifier] = bound
    if set(guard.comment_plan.known) - set(full_known):
        raise ReviewInputError("guarded suffix retained known member disappeared")

    def full_cache_plan():
        # Exact-echo guard refinement is optional and can decline a C increase.
        # A later lifecycle scope still sees the actual cached immutable ID.
        # Price that appended known member independently, retaining every old
        # unknown allocation and both roots' original closure wire ceilings.
        return CommentAllocationPlan(
            full_known,
            guard.comment_plan.unknown,
            metadata_known=guard.comment_plan.metadata_known,
            metadata_unknown=guard.comment_plan.metadata_unknown,
        )

    cache_plan = full_cache_plan()
    prefixes = []

    def capture():
        certificate = projected.certificates["reviews"]
        if certificate.get("reader") != "bounded-cold-reviews":
            raise ReviewInputError("guarded suffix review reader is unsupported")
        prefixes.append(
            (
                max(
                    projected.comment_plan.worst_read_calls(),
                    cache_plan.worst_read_calls(),
                ),
                certificate["physical_reads_upper_bound"],
            )
        )

    capture()
    run, target = saved["run"], saved["target"]
    binding = {
        **quarantine_action._decision_binding(saved["record"], run),
        "generation": 2_147_483_647,
    }
    parts = partition_evidence(
        json.loads(target.capacity_projection.event_bytes),
        binding=binding,
        item_count=1,
    )
    if len(parts) != 1:
        raise ReviewInputError("guarded suffix event allocation changed")
    event_body = _body(parts[0])
    projected.before_request(
        "POST", run.ledger._comments_path(run.identity), {"body": event_body}
    )
    capture()
    future_id = int("9" * 64)
    if future_id in full_known:
        raise ReviewInputError("guarded suffix synthetic future ID collides")
    projected.after_response("POST", {"id": future_id, "body": event_body})
    full_known[future_id] = quarantine_capacity._wire({"body": event_body})
    cache_plan = full_cache_plan()
    capture()
    projected.before_request(*review)
    capture()
    # The receipt is a source-produced body, not a generic raw-size promise.
    # Existing same-marker receipts are a different update route and remain
    # pre-effect holds; unrelated retained receipts stay in the full census.
    authority = saved["authority"].document
    receipt, receipt_wire = exact_intermediate_receipt(
        action=target.command.action,
        scope=target.command.scope,
        instance=target.instance,
        operator_paused=saved["record"].operator_paused,
        actor=authority["actor"]["login"],
        source_comment_id=authority["source"]["id"],
        command_digest=authority["source"]["body_sha256"],
    )
    if (receipt, receipt_wire) != frame.f_locals.get("receipt_data"):
        raise ReviewInputError("guarded suffix exact receipt projection changed")
    marker = receipt.rstrip().rsplit("<!--", 1)[-1]
    if any(
        row["body"].rstrip().endswith("<!--" + marker)
        and row["user"].get("id") == run.ledger._evidence_author_id
        for row in comment_rows
    ):
        raise ReviewInputError(
            "guarded suffix existing receipt update is separately priced"
        )
    receipt_wire = quarantine_capacity._wire({"body": receipt})
    if receipt_wire > 65536:
        raise ReviewInputError(
            "guarded suffix receipt exceeds its complete wire projection"
        )
    projected.before_request(
        "POST", run.ledger._comments_path(run.identity), {"body": receipt}
    )
    receipt_id = future_id - 1
    if receipt_id in full_known:
        raise ReviewInputError("guarded suffix future receipt ID collides")
    full_known[receipt_id] = receipt_wire
    terminal_comments = full_cache_plan().worst_read_calls()
    if terminal_comments > 10 or terminal_comments + 6 > 50:
        raise ReviewInputError("guarded suffix complete terminal census is unfunded")
    if quarantine_capacity._guard_state(guard) != original:
        raise ReviewInputError("guarded suffix sizing changed installed capacity")
    C, R = max(value[0] for value in prefixes), max(value[1] for value in prefixes)
    if C not in (4, 5) or not 1 <= R <= 3:
        raise ReviewInputError("guarded suffix complete physical reader exceeds recipe")
    source_review_reads = R
    contract = "closed-inline-c4-reserved-verification-v1"
    trust_calls = 7
    if C == 5:
        from . import quarantine_historical_execution

        require_module_seal(quarantine_historical_execution)
        quarantine_historical_execution._require_default_profile(run.caller)
        contract = "closed-inline-c5-original-no-verification-v1"
        trust_calls = 0
        # The authentic body route currently needs two pages. Keep the larger
        # three-page source ceiling paid at every walk; this is not a refund.
        R = 3
    if receipt_reads == 2:
        if C != 5 or source_review_reads != 2:
            raise ReviewInputError(
                "two-page receipt needs the closed two-read review source"
            )
        contract = "closed-inline-c5-r2-p2-no-verification-v1"
        R = 2
        # Only this complete prospective two-read certificate funds the extra
        # receipt reads. Prior spending, lifetime promises and the ordinary105
        # requirement stay unchanged. The noncompacted native-loss branch stops
        # before receipt and independently reconciles through journal.finish().
    # Original source branches: coalescence saves only two pre-mutation GETs.
    # Four post-mutation lost GETs and receipt's one complete fresh reconciliation
    # remain paid. These quantities alone cannot authorize any dispatch.
    stages = _intermediate_stages(C, R, receipt_reads, trust_calls)
    normal = sum(count for _, count in stages)
    local_headroom = (
        _default_c5_prefix_headroom(
            stages, terminal_comments=terminal_comments, receipt_reads=receipt_reads
        )
        if C == 5
        else None
    )
    return {
        "contract": contract,
        "local_headroom": local_headroom,
        "stages": stages,
        "normal_calls": normal,
        "combined_loss_calls": normal + receipt_reads + 2 + 4,
        "ordinary_required": 105,
        "receipt_reads": receipt_reads,
        "comment_reads": C,
        "cache_prefix_known": len(cache_plan.known),
        "cache_prefix_unknown": len(cache_plan.unknown),
        "review_reads": R,
        "source_review_reads": source_review_reads,
        "root_measurement": measured,
        "review": json.loads(canonical_bytes(review)),
        "check": json.loads(canonical_bytes(check)),
        "event": event_body,
        "receipt": receipt,
        "receipt_wire": receipt_wire,
        "terminal_comment_reads": terminal_comments,
    }


_SIZING_SCOPE: ContextVar[Any] = ContextVar(
    "quarantine_prospective_sizing", default=None
)


def _hold_bytes(authenticated):
    """Freeze all retained hold components, without evaluating object properties."""
    from . import quarantine_origin

    require_module_seal(quarantine_origin)
    if type(authenticated) is not quarantine_origin.AuthenticatedQuarantine:
        raise ReviewInputError("prospective sizing hold type changed")
    raw = object.__getattribute__(authenticated, "__dict__")
    names = (
        "lineage_bytes",
        "archive_bytes",
        "inventories_bytes",
        "state_bytes",
        "source_documents_bytes",
    )
    if type(raw) is not dict or any(type(k) is not str for k in raw):
        raise ReviewInputError("prospective sizing hold fields changed")
    values = tuple(raw.get(name) for name in names)
    if any(type(value) is not bytes for value in values):
        raise ReviewInputError("prospective sizing hold bytes changed")
    return values


def _source_frame(module, owner, name):
    require_module_seal(module)
    function = vars(module)[owner] if owner is not None else None
    function = vars(function)[name] if function is not None else vars(module)[name]
    original = inspect.unwrap(function)
    frame: FrameType | None = sys._getframe(1)
    while frame is not None:
        if frame.f_code is original.__code__ and frame.f_globals is vars(module):
            return frame
        frame = frame.f_back
    raise ReviewInputError("prospective sizing original frame is absent")


@contextmanager
def prospective_sizing_scope(token, app, run, authority, target, finalizer):
    """Only the original observation call may enter; returned observations survive."""
    require_module_seal(sys.modules[__name__])
    from . import application, quarantine_historical_execution, quarantine_origin

    frame = _source_frame(
        application, "GitHubApplication", "_observe_quarantine_command_finalization"
    )
    if (
        any(
            frame.f_locals.get(name) is not value
            for name, value in (
                ("self", app),
                ("run", run),
                ("authority", authority),
                ("target", target),
                ("preparation", token),
                ("finalizer", finalizer),
            )
        )
        or _SIZING_SCOPE.get() is not None
    ):
        raise ReviewInputError("prospective sizing invocation is not original")
    saved = require(token)
    if (
        saved["app"] is not app
        or saved["run"] is not run
        or saved["target"] is not target
    ):
        raise ReviewInputError("prospective sizing preparation owner changed")
    require_module_seal(quarantine_historical_execution)
    quarantine_historical_execution._require_default_profile(run.caller)
    require_module_seal(quarantine_origin)
    quarantine_origin.require_authenticated_read_owner(
        target.authenticated, run.ledger, run.caller, run.identity
    )
    scope = (token, app, run, authority, target, finalizer, run.caller._root)
    reset = _SIZING_SCOPE.set(scope)
    try:
        yield
    finally:
        _SIZING_SCOPE.reset(reset)


def _require_prospective(token, finalizer):
    require_module_seal(sys.modules[__name__])
    from . import application, publication, quarantine_origin

    scope = _SIZING_SCOPE.get()
    if type(scope) is not tuple or len(scope) != 7 or scope[0] is not token:
        raise ReviewInputError("prospective sizing scope is absent or expired")
    saved = require(token, finalizer=finalizer)
    app, run, authority, target, initial, root = scope[1:]
    source = _source_frame(
        application, "GitHubApplication", "_observe_quarantine_command_finalization"
    )
    copied = _source_frame(
        publication, "ReviewApprovalFinalizer", "prepare_quarantine_command_capacity"
    )
    preparing = _source_frame(
        publication, "ReviewApprovalFinalizer", "prepare_command_capacity"
    )
    executing = _source_frame(
        publication, "ReviewApprovalFinalizer", "_refinalize_with_dispositions_impl"
    )
    if (
        source.f_locals.get("self") is not app
        or source.f_locals.get("run") is not run
        or source.f_locals.get("preparation") is not token
        or source.f_locals.get("finalizer") is not initial
        or copied.f_locals.get("self") is not initial
        or copied.f_locals.get("preparation") is not token
        or preparing.f_locals.get("self") is not copied.f_locals.get("operation")
        or preparing.f_locals.get("operation") is not finalizer
        or executing.f_locals.get("self") is not finalizer
        or run.caller._root != root
        or run.caller._checkpoint is not None
        or saved["run"] is not run
        or saved["authority"] is not authority
        or saved["target"] is not target
        or _hold_bytes(target.authenticated) != saved["hold_bytes"]
    ):
        raise ReviewInputError("prospective sizing copied source or root changed")
    require_module_seal(quarantine_origin)
    quarantine_origin.require_authenticated_read_owner(
        target.authenticated, run.ledger, run.caller, run.identity
    )
    return saved, executing


def attach_sizing_hold(token, finalizer, eligibility, *, head_sha):
    """Attach retained DATA then project a decision; never a fresh hold read."""
    require_module_seal(sys.modules[__name__])
    from . import publication

    saved, executing = _require_prospective(token, finalizer)
    frame = _source_frame(publication, "ReviewApprovalFinalizer", "attach_quarantine")
    carried = _source_frame(publication, "ReviewApprovalFinalizer", "attach_carried")
    if (
        frame.f_locals.get("self") is not finalizer
        or frame.f_locals.get("eligibility") is not eligibility
        or head_sha != saved["run"].head_sha
        # attach_historical intentionally replaces the outer adjusted value.
        # Bind its original consumer's post-historical local instead.
        or carried.f_locals.get("self") is not finalizer
        or carried.f_locals.get("eligibility") is not eligibility
        or frame.f_back is not carried
        or carried.f_back is not executing
    ):
        raise ReviewInputError("prospective sizing hold consumer changed")
    before = saved["hold_bytes"]
    lineage, archive, inventories, state = (json.loads(value) for value in before[:4])
    fresh = eligibility.fresh_human_pending
    current = assessment_view(
        archive["assessment_origins"],
        inventories,
        state,
        scope=lineage["scope_sha256"],
        fresh_human_pending=fresh,
    )
    actual = replace(
        eligibility,
        quarantine_assessments=current,
        facts=replace(
            eligibility.facts,
            has_human_adjudication_findings=(
                fresh
                or bool(current["pending"])
                or bool((eligibility.historical_assessments or {}).get("pending"))
            ),
        ),
    )
    return projected_eligibility(token, finalizer, actual)


@dataclass(frozen=True)
class ProspectiveSizingBranch:
    """Conditional continuation DATA, deliberately not _FinalizationPreflight."""

    repository_id: int
    base_sha: str
    result: None = None


def prospective_pr_branch(
    token, finalizer, *, repository, repository_id, pull_request, head_sha, base_sha
):
    require_module_seal(sys.modules[__name__])
    saved, frame = _require_prospective(token, finalizer)
    run = saved["run"]
    if (
        frame.f_locals.get("compacted") is not False
        or frame.f_locals.get("repository") != repository
        or frame.f_locals.get("repository_id") != repository_id
        or frame.f_locals.get("pull_request") != pull_request
        or frame.f_locals.get("head_sha") != head_sha
        or repository != run.identity.repository
        or repository_id != run.identity.repository_id
        or pull_request != run.identity.pull_request
        or head_sha != run.head_sha
        or base_sha != saved["record"].transaction.base_sha
    ):
        raise ReviewInputError("prospective sizing conditional scope changed")
    return ProspectiveSizingBranch(repository_id, base_sha)


def prepare(run, authority, target, app):
    from . import quarantine_action, quarantine_lifecycle_transfer
    from .application import GitHubApplication
    from .quarantine_capacity import installed_capacity, native_reserve

    require_module_seal(sys.modules[__name__])
    require_module_seal(quarantine_lifecycle_transfer)
    require_projection_source()
    require_module_seal(quarantine_action)
    record, document = sizing_snapshot(target, run)
    if (
        type(app) is not GitHubApplication
        or app.session_ledger is not run.ledger
        or authority.run is not run
        or not isinstance(target.native_review_bytes, bytes)
        or not isinstance(target.native_frontier_bytes, bytes)
        or getattr(run.caller, "_controlled_restart_admission", None) is not None
    ):
        raise ReviewInputError("quarantine finalizer preparation source changed")
    command = target.command
    if type(command) is not quarantine_action.QuarantineCommand:
        raise ReviewInputError("quarantine finalizer preparation command is absent")
    projected = project_decision(
        target.authenticated,
        record,
        quarantine_action._decision_value(
            run, authority, command, target.instance, target.permission
        ),
        binding={
            **quarantine_action._decision_binding(record, run),
            "generation": 2_147_483_647,
        },
    )
    if projected != target.capacity_projection:
        raise ReviewInputError("quarantine finalizer decision projection changed")
    guard = (
        installed_capacity(run.ledger, run.identity)
        if native_reserve(target.authenticated.archive["target_isolation"]) is not None
        else None
    )
    token = QuarantineFinalizerPreparation()
    _PREPARED[token] = {
        "run": run,
        "authority": authority,
        "target": target,
        "app": app,
        "record": record,
        "root": canonical_bytes(record.to_dict()),
        "document": canonical_bytes(document),
        "projection": projected,
        "native": target.native_review_bytes,
        "frontier": target.native_frontier_bytes,
        "guard": guard,
        "hold_bytes": _hold_bytes(target.authenticated),
    }
    return token


def require(token, *, finalizer=None):
    from . import quarantine_action, quarantine_lifecycle_transfer
    from .quarantine_capacity import installed_capacity

    require_module_seal(sys.modules[__name__])
    require_module_seal(quarantine_lifecycle_transfer)
    require_projection_source()
    require_module_seal(quarantine_action)
    saved = (
        _PREPARED.get(token) if type(token) is QuarantineFinalizerPreparation else None
    )
    if saved is None:
        raise ReviewInputError("quarantine finalizer preparation is unissued")
    run, target = saved["run"], saved["target"]
    record, document = sizing_snapshot(target, run)
    if (
        record is not saved["record"]
        or canonical_bytes(record.to_dict()) != saved["root"]
        or canonical_bytes(document) != saved["document"]
        or target.native_review_bytes != saved["native"]
        or target.native_frontier_bytes != saved["frontier"]
        or target.capacity_projection != saved["projection"]
        or _hold_bytes(target.authenticated) != saved["hold_bytes"]
        or saved["app"].session_ledger is not run.ledger
        or saved["authority"].run is not run
        or (
            saved["guard"] is not None
            and installed_capacity(run.ledger, run.identity) is not saved["guard"]
        )
    ):
        raise ReviewInputError("quarantine finalizer preparation custody changed")
    if finalizer is not None:
        from .finding_lifecycle import FindingLifecycleJournal
        from .publication import ReviewApprovalFinalizer

        journal = getattr(finalizer, "_finding_lifecycle_journal", None)
        if (
            type(finalizer) is not ReviewApprovalFinalizer
            or finalizer._command_capacity_requested is not True
            or type(journal) is not FindingLifecycleJournal
            or journal._record is not record
            or journal.ledger is not run.ledger
            or journal.reviewer is not saved["app"].reviewer
            or finalizer.evidence_budget is not run.caller.budget_view
        ):
            raise ReviewInputError("quarantine finalizer sizing consumer changed")
    return saved


def projected_eligibility(token, finalizer, eligibility):
    """Overlay only after the original current hold read has authenticated."""
    saved = require(token, finalizer=finalizer)
    before = saved["target"].authenticated
    fresh = eligibility.fresh_human_pending
    current = assessment_view(
        before.archive["assessment_origins"],
        before.inventories,
        before.state,
        scope=before.lineage["scope_sha256"],
        fresh_human_pending=fresh,
    )
    if current != eligibility.quarantine_assessments:
        raise ReviewInputError("quarantine finalizer current hold observation changed")
    projected = assessment_view(
        before.archive["assessment_origins"],
        before.inventories,
        json.loads(saved["projection"].state_bytes),
        scope=before.lineage["scope_sha256"],
        fresh_human_pending=fresh,
    )
    return replace(
        eligibility,
        quarantine_assessments=projected,
        facts=replace(
            eligibility.facts,
            has_human_adjudication_findings=(
                fresh
                or bool(projected["pending"])
                or bool((eligibility.historical_assessments or {}).get("pending"))
            ),
        ),
    )


def sizing_reviews(token, finalizer):
    """Decode the already paid complete frontier only for nonexecuting sizing."""
    from review_sensei.source_registry import require_instance_source

    saved = require(token, finalizer=finalizer)
    require_instance_source(saved["run"].http, methods=("paginate", "request"))
    rows = json.loads(saved["frontier"])
    if (
        not isinstance(rows, list)
        or not all(isinstance(row, dict) for row in rows)
        or sum(canonical_bytes(row) == saved["native"] for row in rows) != 1
    ):
        raise ReviewInputError("quarantine sizing native frontier changed")
    return rows


def original_frontier(token, finalizer, *, eligibility, index, reviews):
    """Exact original native frame only, without a one-page read-cost claim."""
    saved = require(token, finalizer=finalizer)
    from .publication import approval_eligibility_from_body, parse_finding_index

    native = json.loads(saved["native"])
    run = saved["run"]
    if (
        eligibility.native_union is not None
        or eligibility.human_inventory_reference is not None
        or eligibility.partitioned_root is not None
        or not isinstance(reviews, (list, tuple))
        or sum(canonical_bytes(item) == saved["native"] for item in reviews) != 1
        or approval_eligibility_from_body(native.get("body")) != eligibility
        or parse_finding_index(
            native.get("body"),
            repository_id=run.identity.repository_id,
            pull_request=run.identity.pull_request,
            head_sha=run.head_sha,
        )
        != index
    ):
        raise ReviewInputError("quarantine finalizer original native frontier changed")
    return canonical_bytes(list(reviews))


from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
