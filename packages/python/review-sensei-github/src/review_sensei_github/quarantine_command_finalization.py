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
