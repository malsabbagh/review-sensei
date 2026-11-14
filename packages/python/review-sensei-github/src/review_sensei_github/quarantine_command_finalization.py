"""Source-bound hypothetical finalizer observations, never effect authority.

These preparations exist only before the command's action boundary. Their
projected assessment view cannot be consumed by ordinary approval execution.
Physical admission must separately prove every native and lifecycle tail.
"""

import json
import sys
from dataclasses import dataclass, replace
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


def measure_inline_branches(observation):
    """Keep the original caller's width witness unchanged after pure sizing."""
    saved = require_observation(observation)
    caller = saved["run"].caller
    missing = object()
    previous = getattr(caller, "_accepted_semantic_capacity", missing)
    try:
        return _measure_inline_branches(observation)
    finally:
        if previous is missing:
            caller.__dict__.pop("_accepted_semantic_capacity", None)
        else:
            caller._accepted_semantic_capacity = previous


def _root_growth_measurement(record, accounting, witness, result_upper=None, grants=()):
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


def _measure_inline_branches(observation):
    """Measure all reached prefixes under the original inline/root/history caps.

    No reader price, part allocation or permission is inferred from this value.
    A single non-inline branch refuses the proposed inline-only route.
    """
    from review_sensei import finding_lifecycle_store
    from review_sensei.source_registry import require_instance_source

    from . import accepted_semantic_history, finding_lifecycle, quarantine_action
    from .publication import ReviewPublisher

    saved = require_observation(observation)
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
    from .quarantine_command_namespace import project_current

    accounting_upper = project_current(run.caller)
    witness = getattr(run.caller, "_accepted_semantic_capacity", None)
    result_upper = None
    if saved["projection"].creates_event:
        import hashlib

        from .api_run_history import action_result_binding
        from .command_result_capacity import command_result_capacity_upper

        selected = json.loads(target.native_review_bytes)
        source_payload = json.loads(plan.payload)
        if (
            source_payload.get("conditional_approval") is not None
            or source_payload["review_before"]["id"] != selected["id"]
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
        result_upper = command_result_capacity_upper(
            base_result,
            binding=action_result_binding(
                run.identity, run.caller.accounting, run.ledger.evidence_producer()
            ),
            intent_sha256=hashlib.sha256(canonical_bytes(action["intent"])).hexdigest(),
            outcome={"kind": "applied", "reason": None},
            call_limit=run.caller.accounting.configuration.limit,
            reference_history=True,
            source_review_id=selected["id"],
            approval_review_id=None,
        )
    grants = tuple(sorted(run.ledger._observed_consumed_grants))
    growth_roots = [
        _root_growth_measurement(root, accounting_upper, witness, result_upper, grants)
        for root in (record, prospective)
    ]
    branches = []
    for documents, _exact in alternatives:
        current = prospective
        checkpoints = []
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
                candidate, accounting_upper, witness, result_upper, grants
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
            current = candidate
        branches.append(tuple(checkpoints))
    require_observation(observation)
    return {
        "branches": tuple(branches),
        "growth_roots": tuple(growth_roots),
        "result_manifest_bound": len(canonical_bytes(result_upper)),
        "all_growth_roots_fit": all(root["root_fits"] for root in growth_roots),
        "max_checkpoints": max(len(branch) for branch in branches),
        "native_requests": requests,
        "prospective_root_bytes": len(canonical_bytes(prospective.to_dict())),
    }


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


def _default_c5_prefix_headroom(stages, *, terminal_comments):
    """Local reservation liquidity under all four ordinary root read losses."""
    from . import api_run_plan

    require_module_seal(api_run_plan)
    overhead = (
        api_run_plan.QUARANTINE_CHECKPOINT_REQUESTS
        - api_run_plan.READ_OPERATION_REQUESTS
    )
    if overhead != 6 or api_run_plan.READ_OPERATION_REQUESTS != 10:
        raise ReviewInputError("guarded C5 checkpoint source constants changed")
    C = 5
    checkpoint = 4 + C + overhead
    minima = {
        "entry-checkpoint": C + overhead,
        "own-cas-authority-and-checkpoint": checkpoint,
        "pending-journal": checkpoint + checkpoint + 6,
        "review-update": 1 + api_run_plan.NATIVE_COMMAND_UPDATE_CONFIRMATION_REQUESTS,
        "finish-journal": checkpoint,
        "receipt": 8,
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
        remaining -= calls + int(name in losses) + (3 if name == "receipt" else 0)
    if remaining != 0 or terminal_comments + overhead > 50:
        raise ReviewInputError("guarded C5 complete prefix accounting changed")
    return rows


def intermediate_suffix_recipe(observation, *, comment_rows):
    """Derive the closed intermediate recipe; data, not an execution lease."""
    from copy import copy, deepcopy

    from review_sensei.bounded_evidence import partition_evidence

    from . import (
        finding_lifecycle,
        quarantine_action,
        quarantine_capacity,
        quarantine_command_suffix,
    )
    from .quarantine_creation import _body

    require_module_seal(quarantine_command_suffix)
    frame = sys._getframe(1)
    if (
        frame.f_code is not quarantine_command_suffix.prepare.__code__
        or frame.f_globals is not vars(quarantine_command_suffix)
        or frame.f_locals.get("observation") is not observation
        or frame.f_locals.get("rows") is not comment_rows
        or frame.f_locals.get("captured", {}).get("rows")
        != canonical_bytes(comment_rows)
    ):
        raise ReviewInputError("guarded suffix cache prefix lacks original paid census")
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
    from review_sensei import presentation

    require_module_seal(presentation)
    result = quarantine_action.historical_command_result(
        target.command,
        target,
        saved["record"],
        existing=None,
        actor=saved["authority"].document["actor"],
    )
    authority = saved["authority"].document
    receipt = presentation.render_command_receipt(
        actor=authority["actor"]["login"],
        source_comment_id=authority["source"]["id"],
        command_digest=authority["source"]["body_sha256"],
        receipts=result.receipts,
        ledger_written=True,
    )
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
    # Original source branches: coalescence saves only two pre-mutation GETs.
    # Four post-mutation lost GETs and receipt's one complete fresh reconciliation
    # remain paid. These quantities alone cannot authorize any dispatch.
    stages = (
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
        ("receipt", 5),
        ("trust-verifications", trust_calls),
    )
    normal = sum(count for _, count in stages)
    local_headroom = (
        _default_c5_prefix_headroom(stages, terminal_comments=terminal_comments)
        if C == 5
        else None
    )
    return {
        "contract": contract,
        "local_headroom": local_headroom,
        "stages": stages,
        "normal_calls": normal,
        "combined_loss_calls": normal + 3 + 4,
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
