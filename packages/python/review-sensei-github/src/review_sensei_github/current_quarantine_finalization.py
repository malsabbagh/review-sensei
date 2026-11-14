"""Current inline decisions with unchanged carried holds, not historical recovery.

The preparation only proves a closed reader route. Original journal funding and
preview admission are still mandatory, and execution consumes the exact plan
once after ordinary non-business field validation.
"""

import inspect
import sys
from copy import copy
from dataclasses import asdict, dataclass
from types import FrameType
from typing import Any
from weakref import WeakKeyDictionary

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.source_registry import require_instance_source, require_module_seal

CONTRACT = "current-inline-with-unchanged-quarantine-holds-v1"
_PREPARED: WeakKeyDictionary = WeakKeyDictionary()
_PLANS: WeakKeyDictionary = WeakKeyDictionary()
_EXECUTED: WeakKeyDictionary = WeakKeyDictionary()
_SIZING: WeakKeyDictionary = WeakKeyDictionary()
_TERMINAL_REPORTS: WeakKeyDictionary = WeakKeyDictionary()
_COMPLETED_PREVIEWS: WeakKeyDictionary = WeakKeyDictionary()
_PROJECTIONS: WeakKeyDictionary = WeakKeyDictionary()
_EXTENSIONS: WeakKeyDictionary = WeakKeyDictionary()


@dataclass(frozen=True, eq=False)
class CurrentPreparation:
    pass


@dataclass(frozen=True, eq=False)
class TerminalPreview:
    """Original successful terminal sizing data, never an execution lease."""


def capture_terminal_preview(
    ledger: Any, record: Any, preview_record: Any, details: Any, report: Any
):
    from review_sensei import terminal_representability

    from . import action_representability

    require_module_seal(sys.modules[__name__])
    require_module_seal(action_representability)
    require_module_seal(terminal_representability)
    frame = sys._getframe(1)
    source: Any = frame.f_locals
    if ledger is None or record is None or preview_record is None or report is None:
        raise ReviewInputError("current terminal preview is not source-issued")
    if (
        frame.f_code
        is not action_representability.guard_current_command_representability.__code__
        or source.get("ledger") is not ledger
        or source.get("current_record") is not record
        or source.get("_preview_rec") is not preview_record
        or source.get("details") is not details
        or source.get("report") is not report
        or type(report) is not terminal_representability.TerminalRepresentabilityReport
        or not report.representable
        or report.authority
        or report.v3_writer_enabled
    ):
        raise ReviewInputError("current terminal preview is not source-issued")
    from review_sensei.disposition import MaintainerCommandResult

    result = source.get("_preview_res")
    if not isinstance(result, MaintainerCommandResult):
        raise ReviewInputError("current base result preview is unavailable")
    intent_sha256 = source.get("intent_digest")
    binding = source.get("binding")
    outcome = (
        {"kind": "applied", "reason": None}
        if result.applied
        else {
            "kind": "explicit_noop",
            "reason": "status" if result.action == "status" else "no_change",
        }
    )
    call_limit = ledger.caller.accounting.configuration.limit
    sizing = {
        "binding": binding,
        "intent_sha256": intent_sha256,
        "outcome": outcome,
        "call_limit": call_limit,
        "reference_history": preview_record.finding_lifecycle is not None,
    }
    token = TerminalPreview()
    _TERMINAL_REPORTS[token] = (
        ledger,
        record,
        canonical_bytes(record.to_dict()),
        _binding(ledger),
        preview_record,
        canonical_bytes(preview_record.to_dict()),
        details,
        _details_seal(details),
        report,
        canonical_bytes(asdict(report)),
        result,
        canonical_bytes(asdict(result)),
        sizing,
        canonical_bytes(sizing),
    )
    return token


def _details_seal(details):
    return canonical_bytes(
        {
            **details,
            "dispositions": details["dispositions"].decode(),
            "staged_parts": [part.decode() for part in details["staged_parts"]],
        }
    )


def require_terminal_preview(token, ledger, record, preview_record, details):
    require_module_seal(sys.modules[__name__])
    saved = _TERMINAL_REPORTS.get(token) if type(token) is TerminalPreview else None
    if saved is None or (
        ledger is not saved[0]
        or record is not saved[1]
        or canonical_bytes(record.to_dict()) != saved[2]
        or _binding(ledger) != saved[3]
        or preview_record is not saved[4]
        or canonical_bytes(preview_record.to_dict()) != saved[5]
        or details is not saved[6]
        or _details_seal(details) != saved[7]
        or canonical_bytes(asdict(saved[8])) != saved[9]
        or canonical_bytes(asdict(saved[10])) != saved[11]
        or canonical_bytes(saved[12]) != saved[13]
    ):
        raise ReviewInputError("current terminal preview is unissued or changed")
    return saved[8]


def _history(ledger, identity):
    from .quarantine_origin import require_authenticated_read_owner

    value = ledger._quarantine_authenticated
    require_authenticated_read_owner(value, ledger, ledger.caller, identity)
    if not value.pending:
        raise ReviewInputError(
            "current held route requires unresolved historical holds"
        )
    return tuple(
        getattr(value, name)
        for name in (
            "lineage_bytes",
            "archive_bytes",
            "inventories_bytes",
            "state_bytes",
        )
    )


def _binding(ledger):
    caller = ledger.caller
    action = caller.accounting.action
    if not isinstance(action, dict) or action.get("phase") != "pending":
        raise ReviewInputError(
            "current held route requires the original pending action"
        )
    return (
        caller,
        caller._block,
        caller.accounting.owner,
        canonical_bytes(action["intent"]),
    )


def require(preparation, finalizer=None):
    require_module_seal(sys.modules[__name__])
    saved = (
        _PREPARED.get(preparation) if type(preparation) is CurrentPreparation else None
    )
    if saved is None:
        raise ReviewInputError("current held preparation is unissued")
    from .application import GitHubApplication
    from .quarantine_capacity import installed_capacity
    from .quarantine_command_finalization import require_finalizer_copy_source

    app, journal, ledger, record = (
        saved[k] for k in ("app", "journal", "ledger", "record")
    )
    require_instance_source(
        app,
        methods=(
            "_preflight_command_finalization",
            "_quarantine_hold_reader",
            "_human_inventory_reader",
        ),
    )
    require_instance_source(journal, methods=("preflight_command_finalization",))
    require_instance_source(ledger, methods=("load",))
    if (
        type(app) is not GitHubApplication
        or app.session_ledger is not ledger
        or journal.ledger is not ledger
        or journal._record is not record
        or _binding(ledger) != saved["binding"]
        or canonical_bytes(record.to_dict()) != saved["root"]
        or _history(ledger, record.identity()) != saved["history"]
        or installed_capacity(ledger, record.identity()) is not saved["guard"]
    ):
        raise ReviewInputError("current held preparation owner or history changed")
    if finalizer is not None:
        require_finalizer_copy_source(finalizer)
        factory = finalizer.quarantine_reader_factory
        if (
            getattr(finalizer, "_finding_lifecycle_journal", None) is not journal
            or getattr(factory, "__self__", None) is not app
            or getattr(factory, "__func__", None)
            is not GitHubApplication._quarantine_hold_reader
            or finalizer._quarantine_capacity_preparation is not None
        ):
            raise ReviewInputError("current held finalizer source changed")
    return saved


def prepare(app, journal, *, preview_record, details):
    from .application import GitHubApplication
    from .finding_lifecycle import FindingLifecycleJournal
    from .quarantine_capacity import installed_capacity
    from .review_run import ActionRunLedger

    frame = sys._getframe(1)
    command_frame = frame.f_back
    ledger, record = journal.ledger, journal._record
    authenticated = ledger._quarantine_authenticated
    if (
        frame.f_code is not GitHubApplication._preflight_command_finalization.__code__
        or command_frame is None
        or command_frame.f_code
        is not GitHubApplication.apply_maintainer_command.__code__
        or command_frame.f_locals.get("self") is not app
        or command_frame.f_locals.get("capacity_preview")
        is not frame.f_locals.get("capacity_preview")
        or command_frame.f_locals.get("pre_effect_refusal") is not None
        or frame.f_locals.get("self") is not app
        or frame.f_locals.get("journal") is not journal
        or type(app) is not GitHubApplication
        or type(journal) is not FindingLifecycleJournal
        or type(ledger) is not ActionRunLedger
        or record is None
        or record.quarantine_lineage is None
        or ledger.caller._root != record.record_sha256
        or authenticated is None
        or authenticated.record_sha256 != record.record_sha256
        or ledger.caller._active_operation is not None
        or not isinstance(details, dict)
        or details.get("original_root") != record.record_sha256
        or details.get("preview_root") != preview_record.record_sha256
        or not isinstance(details.get("dispositions"), bytes)
        or canonical_bytes(record.quarantine_lineage)
        != canonical_bytes(preview_record.quarantine_lineage)
    ):
        raise ReviewInputError(
            "current held preparation requires original pre-effect preview"
        )
    terminal_preview = frame.f_locals["capacity_preview"].get(
        "current_terminal_preview"
    )
    require_terminal_preview(terminal_preview, ledger, record, preview_record, details)
    proof = CurrentPreparation()
    _PREPARED[proof] = {
        "terminal_preview": terminal_preview,
        "preview_record": preview_record,
        "app": app,
        "journal": journal,
        "ledger": ledger,
        "record": record,
        "binding": _binding(ledger),
        "root": canonical_bytes(record.to_dict()),
        "history": _history(ledger, record.identity()),
        "guard": installed_capacity(ledger, record.identity()),
        "preview": canonical_bytes(preview_record.to_dict()),
        "details": details,
        "details_seal": canonical_bytes(
            {
                **details,
                "dispositions": details["dispositions"].decode(),
                "staged_parts": [part.decode() for part in details["staged_parts"]],
            }
        ),
    }
    require(proof)
    return proof


def _selected_review_id(row):
    """Refuse a matching malformed source, never skip it for a second match."""
    value = row.get("id") if isinstance(row, dict) else None
    if type(value) is not int or not 1 <= value <= 2**63 - 1:
        raise ReviewInputError("current selected source review identity is unsupported")
    return value


def frontier(finalizer, *, eligibility, index, reviews):
    saved = require(finalizer._current_quarantine_preparation, finalizer)
    from review_sensei.quarantine_assessments import assessment_view

    from .publication import approval_eligibility_from_body, parse_finding_index

    ledger, record = saved["ledger"], saved["record"]
    auth = ledger._quarantine_authenticated
    view = assessment_view(
        auth.archive["assessment_origins"],
        auth.inventories,
        auth.state,
        scope=auth.lineage["scope_sha256"],
        fresh_human_pending=eligibility.fresh_human_pending,
    )
    if (
        eligibility.native_union is not None
        or eligibility.human_inventory_reference is not None
        or eligibility.partitioned_root is not None
        or eligibility.quarantine_assessments != view
        or eligibility.historical_assessments is not None
        or not isinstance(reviews, (list, tuple))
        or not 0 < len(reviews) < 100
    ):
        raise ReviewInputError("current held inline frontier is unsupported")
    producer = ledger.evidence_producer()
    matches = []
    for row in reviews:
        user = row.get("user") if isinstance(row, dict) else None
        if (
            isinstance(user, dict)
            and type(user.get("id")) is int
            and producer == f"github-bot:{user['id']}"
            and user.get("type") == "Bot"
            and isinstance(user.get("login"), str)
            and user["login"].casefold() == saved["journal"].app_slug.casefold()
            and row.get("commit_id") == saved["journal"].head_sha
            and row.get("edited_by_human") is not True
            and row.get("state") in {"COMMENTED", "APPROVED", "CHANGES_REQUESTED"}
            and approval_eligibility_from_body(row.get("body")) == eligibility
            and parse_finding_index(
                row.get("body"),
                repository_id=record.repository_id,
                pull_request=record.pull_request,
                head_sha=saved["journal"].head_sha,
            )
            == index
        ):
            _selected_review_id(row)
            matches.append(row)
    if len(matches) != 1:
        raise ReviewInputError("current held original inline frame is ambiguous")
    return canonical_bytes(list(reviews))


def issue_plan(finalizer, *, payload, **arguments):
    from .api_run_plan import READ_OPERATION_REQUESTS
    from .command_finalization_plan import issue_plan as original_issue
    from .publication import ReviewApprovalFinalizer

    frame = sys._getframe(1)
    proof = finalizer._current_quarantine_preparation
    saved = require(proof, finalizer)
    if (
        frame.f_code
        is not ReviewApprovalFinalizer._refinalize_with_dispositions_impl.__code__
        or frame.f_locals.get("self") is not finalizer
        or payload["conditional_approval"] is not None
        or frame.f_locals.get("possible_approval") is None
        or frame.f_locals["possible_approval"].approved
        or payload["read_contract"] != "closed-plain-single-complete-page-v1"
    ):
        raise ReviewInputError("current held plan is not the original withheld route")
    payload = {
        **payload,
        "read_contract": CONTRACT,
        "execution_read_tail": {
            **payload["execution_read_tail"],
            # attach_carried and finalize.require each call the genuine native
            # load (physically capped at ten), then scope.require's fresh PR.
            # No adjacent-read reuse is assumed and no reservation is enlarged.
            "unchanged_historical_holds": 2 * (READ_OPERATION_REQUESTS + 1),
        },
    }
    plan = original_issue(finalizer, payload=payload, **arguments)
    from .command_result_capacity import command_result_capacity_bound

    selected_id = _selected_review_id(payload["review_before"])
    terminal = _TERMINAL_REPORTS[saved["terminal_preview"]]
    domain = {"source_review_id": selected_id, "approval_review_id": None}
    bound = command_result_capacity_bound(terminal[10], **terminal[12], **domain)
    _PLANS[plan] = (proof, plan.payload, domain, bound)
    return plan


def require_plan(plan, journal, preview_record=None, details=None):
    item = _PLANS.get(plan)
    if item is None or plan.payload != item[1]:
        raise ReviewInputError("current held finalization plan is unissued or changed")
    saved = require(item[0])
    if journal is not saved["journal"] or plan.journal is not journal:
        raise ReviewInputError("current held finalization journal changed")
    require_terminal_preview(
        saved["terminal_preview"],
        saved["ledger"],
        saved["record"],
        saved["preview_record"],
        saved["details"],
    )
    from .command_result_capacity import command_result_capacity_bound

    terminal = _TERMINAL_REPORTS[saved["terminal_preview"]]
    if (
        command_result_capacity_bound(terminal[10], **terminal[12], **item[2])
        != item[3]
    ):
        raise ReviewInputError("current result source domain changed")
    if preview_record is not None:
        if (
            canonical_bytes(preview_record.to_dict()) != saved["preview"]
            or details is not saved["details"]
            or canonical_bytes(
                {
                    **details,
                    "dispositions": details["dispositions"].decode(),
                    "staged_parts": [part.decode() for part in details["staged_parts"]],
                }
            )
            != saved["details_seal"]
        ):
            raise ReviewInputError("current held finalization preview changed")
    return saved


def is_current_plan(plan):
    require_module_seal(sys.modules[__name__])
    return plan in _PLANS


@dataclass(frozen=True, eq=False)
class LifecycleSizingContext:
    """Opaque detached proposal, deliberately never an execution grant."""


def _sizing_frame():
    from . import finding_lifecycle

    require_module_seal(finding_lifecycle)
    code = inspect.unwrap(
        finding_lifecycle.FindingLifecycleJournal.preflight_command_finalization
    ).__code__
    frame: FrameType | None = sys._getframe(1)
    while frame is not None:
        if frame.f_code is code:
            return frame
        frame = frame.f_back
    return None


def _known_binding(known, added_parts):
    if (
        type(known) is not dict
        or any(
            type(key) is not int or key <= 0 or type(value) is not int or value < 0
            for key, value in known.items()
        )
        or type(added_parts) is not int
        or not 0 <= added_parts <= 256
    ):
        raise ReviewInputError("current lifecycle sizing members are unsupported")
    return tuple(known.items()), added_parts


def lifecycle_sizing_context(ledger, identity, known, added_parts):
    """Issue only inside the exact original whole-current-command preflight."""
    require_module_seal(sys.modules[__name__])
    frame = _sizing_frame()
    if frame is None or not is_current_plan(frame.f_locals.get("plan")):
        return None
    from . import quarantine_capacity

    require_module_seal(quarantine_capacity)
    caller_frame = sys._getframe(1)
    extension_frame = caller_frame.f_back
    if (
        caller_frame.f_code
        is not quarantine_capacity._current_lifecycle_sizing.__code__
        or extension_frame is None
        or extension_frame.f_code
        is not quarantine_capacity.lifecycle_capacity_extension.__code__
        or any(
            extension_frame.f_locals.get(name) is not value
            for name, value in (
                ("ledger", ledger),
                ("identity", identity),
                ("known", known),
            )
        )
        or extension_frame.f_locals.get("added_parts") != added_parts
    ):
        raise ReviewInputError("current lifecycle proposal caller is unsupported")
    context = frame.f_locals
    plan, journal = context["plan"], context["self"]
    saved = require_plan(
        plan, journal, context["preview_record"], context["capacity_details"]
    )
    if (
        ledger is not saved["ledger"]
        or identity != journal.identity
        or ledger.caller._root != saved["record"].record_sha256
        or ledger.caller._effect_record is not saved["record"]
    ):
        raise ReviewInputError("current lifecycle proposal root or owner changed")
    guard = quarantine_capacity.installed_capacity(ledger, identity)
    token = LifecycleSizingContext()
    _SIZING[token] = (
        plan,
        journal,
        context["preview_record"],
        context["capacity_details"],
        ledger,
        identity,
        _known_binding(known, added_parts),
        guard,
        guard.comment_plan,
        quarantine_capacity._guard_state(guard),
    )
    return token


def validate_lifecycle_sizing_context(token, ledger, identity, known, added_parts):
    """Revalidate proposal custody without granting physical activation."""
    require_module_seal(sys.modules[__name__])
    saved = _SIZING.get(token) if type(token) is LifecycleSizingContext else None
    if saved is None:
        raise ReviewInputError("current lifecycle sizing context is unissued")
    frame = _sizing_frame()
    plan, journal, preview, details = saved[:4]
    if (
        frame is None
        or frame.f_locals.get("plan") is not plan
        or frame.f_locals.get("self") is not journal
        or ledger is not saved[4]
        or identity != saved[5]
        or _known_binding(known, added_parts) != saved[6]
    ):
        raise ReviewInputError("current lifecycle sizing context changed or expired")
    proof = require_plan(plan, journal, preview, details)
    from .quarantine_capacity import _guard_state, installed_capacity

    guard = installed_capacity(ledger, identity)
    if (
        guard is not saved[7]
        or guard.comment_plan is not saved[8]
        or _guard_state(guard) != saved[9]
        or ledger.caller._root != proof["record"].record_sha256
        or ledger.caller._effect_record is not proof["record"]
    ):
        raise ReviewInputError("current lifecycle sizing lifetime plan changed")
    return token


def _allocation_seal(plan):
    require_instance_source(plan, methods=("worst_read_calls",))
    return canonical_bytes(
        {
            "known": tuple(plan.known.items()),
            "unknown": tuple(plan.unknown),
            "metadata_known": tuple(plan.metadata_known.items()),
            "metadata_unknown": plan.metadata_unknown,
        }
    )


def capture_lifecycle_extension(proof, ledger, identity):
    """Capture only original sizing return custody, never a live writer lease."""
    from . import quarantine_capacity

    require_module_seal(sys.modules[__name__])
    require_module_seal(quarantine_capacity)
    frame = sys._getframe(1)
    if (
        proof is None
        or frame.f_code is not quarantine_capacity.lifecycle_capacity_extension.__code__
        or frame.f_globals is not vars(quarantine_capacity)
        or id(frame.f_locals.get("proof")) != id(proof)
        or frame.f_locals.get("ledger") is not ledger
        or frame.f_locals.get("identity") is not identity
        or proof.sizing is None
        or proof.sizing[0] is not sys.modules[__name__]
    ):
        raise ReviewInputError("current lifetime proposal is not source-issued")
    token = proof.sizing[1]
    validate_lifecycle_sizing_context(
        token, ledger, identity, dict(proof.sizing[2]), proof.sizing[3]
    )
    saved = _SIZING[token]
    full = quarantine_capacity.lifecycle_extension_plan(
        proof, ledger=ledger, identity=identity
    )
    entries = _EXTENSIONS.setdefault(saved[0], [])
    entries.append((proof, full, _allocation_seal(full), proof.sizing[3]))


def _extended_plan(plan, journal, added_parts):
    from .quarantine_capacity import lifecycle_extension_plan

    entries = _EXTENSIONS.get(plan)
    if not entries:
        raise ReviewInputError("current appended lifetime proposal is absent")
    selected = []
    for proof, full, seal, count in entries:
        if (
            lifecycle_extension_plan(
                proof, ledger=journal.ledger, identity=journal.identity
            )
            is not full
            or _allocation_seal(full) != seal
        ):
            raise ReviewInputError("current appended lifetime proposal changed")
        if count == added_parts:
            selected.append((full, seal))
    if not selected or any(seal != selected[0][1] for _, seal in selected):
        raise ReviewInputError("current complete lifetime proposal is ambiguous")
    return selected[0][0]


def capture_completed_preview(
    plan, journal, *, preview_record, details, alternatives, item_count, quote
):
    """Retain original whole-branch sizing custody, never an execution grant."""
    from . import finding_lifecycle

    require_module_seal(sys.modules[__name__])
    require_module_seal(finding_lifecycle)
    frame = sys._getframe(1)
    original = inspect.unwrap(
        finding_lifecycle.FindingLifecycleJournal.preflight_command_finalization
    )
    expected = {
        "self": journal,
        "plan": plan,
        "preview_record": preview_record,
        "capacity_details": details,
        "alternatives": alternatives,
        "item_count": item_count,
        "result": quote,
    }
    if (
        frame.f_code is not original.__code__
        or frame.f_globals is not original.__globals__
        or any(frame.f_locals.get(key) is not value for key, value in expected.items())
        or plan in _COMPLETED_PREVIEWS
    ):
        raise ReviewInputError("current completed preview is not original or is reused")
    require_plan(plan, journal, preview_record, details)
    # Exact branch objects and their immutable byte seals are retained together.
    # Neither this capsule nor the sizing extension may authorize a dispatch.
    branch_seal = canonical_bytes(
        [(documents, sorted(exact)) for documents, exact in alternatives]
    )
    _COMPLETED_PREVIEWS[plan] = (
        journal,
        preview_record,
        details,
        alternatives,
        item_count,
        quote,
        branch_seal,
        canonical_bytes(quote),
        _details_seal(details),
        canonical_bytes(preview_record.to_dict()),
    )

    from . import api_run_plan, caller_operation, quarantine_capacity
    from .command_result_capacity import command_result_capacity_upper
    from .current_command_projection import (
        future_census_bound,
        pending_accounting,
        project,
    )
    from .quarantine_command_namespace import observed_sequence

    caller = journal.ledger.caller
    saved = require_plan(plan, journal, preview_record, details)
    for module in (api_run_plan, caller_operation, quarantine_capacity):
        require_module_seal(module)
    if (
        caller._terminal_reason is not None
        or getattr(caller, "_controlled_restart_admission", None) is not None
        or caller._active_operation is not None
    ):
        raise ReviewInputError("current projection requires untouched ordinary entry")
    fences = caller_operation.prewrite_fence_requests(caller)
    if type(fences) is not int or fences not in (0, 1):
        raise ReviewInputError("current terminal trust fence is not bounded")
    guard = quarantine_capacity.installed_capacity(journal.ledger, journal.identity)
    full_plan = _extended_plan(plan, journal, quote["added_parts"])
    from .http import MAX_PAGINATION_PAGES

    census = future_census_bound(full_plan, quote["added_parts"])
    if census > MAX_PAGINATION_PAGES:
        raise ReviewInputError("current future census exceeds original read ceiling")
    # These remain proposed reachable request counts, not funding changes or
    # permission to consume them. Full execution-path/source fencing is separate.
    ordinary = quote["conditional_calls"] + quote["reserved_tail_calls"]
    # Retain the possible original trust verification even when currently zero.
    ordinary += 2 * census + 6 + 1
    terminal = api_run_plan.QUARANTINE_CHECKPOINT_REQUESTS + 1
    sequence = observed_sequence(caller)
    accounting_upper = pending_accounting(
        caller.accounting,
        sequence=sequence,
        ordinary_id=caller._block,
        terminal_id=caller._terminal_block,
        ordinary=ordinary,
        terminal=terminal,
    )
    base = _TERMINAL_REPORTS[saved["terminal_preview"]]
    manifest = command_result_capacity_upper(base[10], **base[12], **_PLANS[plan][2])
    projection = project(
        journal,
        preview_record=preview_record,
        details=details,
        alternatives=alternatives,
        item_count=item_count,
        quote=quote,
        accounting_upper=accounting_upper,
        result_manifest=manifest,
    )
    require_plan(plan, journal, preview_record, details)
    if observed_sequence(caller) != sequence:
        raise ReviewInputError("current projection performed an unexpected request")
    _PROJECTIONS[plan] = (
        projection,
        canonical_bytes(projection),
        ordinary,
        terminal,
        sequence,
        canonical_bytes(caller.accounting.to_document()),
        guard,
        guard.comment_plan,
        full_plan,
        _allocation_seal(full_plan),
    )


def _execution_module():
    from . import current_command_execution

    require_module_seal(sys.modules[__name__])
    require_module_seal(current_command_execution)
    return current_command_execution


def lifecycle_execution_context(ledger, identity, known, added_parts):
    return _execution_module().lifecycle_context(ledger, identity, known, added_parts)


def consume_lifecycle_execution_context(token, proof, operation, known, added_parts):
    return _execution_module().consume_context(
        token, proof, operation, known, added_parts
    )


def validate_namespace_request(caller, deadline):
    return _execution_module().namespace(caller, deadline)


def before_terminal(caller, run, proof):
    return _execution_module().before_terminal(caller, run, proof)


def confirm_checkpoint(caller, ledger, record, persisted):
    return _execution_module().confirm(caller, ledger, record, persisted)


def require_execution_grant(plan):
    """Issue a distinct source invocation lease, never promote a sizing token."""
    require_module_seal(sys.modules[__name__])
    from .command_finalization_plan import CommandFinalizationPlan

    if type(plan) is not CommandFinalizationPlan:
        raise ReviewInputError("current execution grant requires a source-issued plan")
    if is_current_plan(plan):
        require_plan(plan, plan.journal)
        return _execution_module().issue(plan)


def transfer(plan, finalizer, journal):
    from .command_finalization_plan import attach_execution_reads
    from .quarantine_command_finalization import require_finalizer_copy_source

    frame = sys._getframe(1)
    if frame.f_code is not attach_execution_reads.__code__ or plan in _EXECUTED:
        raise ReviewInputError(
            "current held execution transfer is unissued or consumed"
        )
    saved = require_plan(plan, plan.journal)
    _execution_module().require_plan_lease(plan)
    require_finalizer_copy_source(finalizer)
    if (
        journal.ledger is not saved["ledger"]
        or _history(journal.ledger, journal.identity) != saved["history"]
    ):
        raise ReviewInputError("current held execution history changed")
    proof = CurrentPreparation()
    _PREPARED[proof] = {
        **saved,
        "journal": journal,
        "record": journal._record,
        "root": canonical_bytes(journal._record.to_dict()),
    }
    operation = copy(finalizer)
    operation._current_quarantine_preparation = proof
    require(proof, operation)
    _EXECUTED[plan] = proof
    _execution_module().attach(plan, journal)
    return operation


from .quarantine_command_namespace import (  # noqa: E402
    install_current_validator as _install_current_validator,
)

_install_current_validator(validate_namespace_request)
del _install_current_validator


from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
