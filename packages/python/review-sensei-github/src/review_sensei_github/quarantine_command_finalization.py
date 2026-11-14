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
    from review_sensei.finding_lifecycle import encode_lifecycle_state

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
            checkpoints.append(
                {
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
        "max_checkpoints": max(len(branch) for branch in branches),
        "native_requests": requests,
        "prospective_root_bytes": len(canonical_bytes(prospective.to_dict())),
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
