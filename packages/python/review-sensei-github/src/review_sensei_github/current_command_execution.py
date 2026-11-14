"""One original current-command invocation, with negative per-prefix fences.

No sizing token enters this registry. Issuance, root confirmation and terminal
entry each require their own original source frame. Funding is never replaced.
"""

import inspect
import json
import sys
from dataclasses import dataclass
from types import FrameType
from typing import Any
from weakref import WeakKeyDictionary, WeakSet

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.source_registry import require_instance_source, require_module_seal

_ACTIVE: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()
_EXPIRED: WeakSet[Any] = WeakSet()
_CONTEXTS: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()


@dataclass(frozen=True, eq=False)
class _Context:
    pass


@dataclass(frozen=True, eq=False)
class _Invocation:
    pass


def _frame(function):
    original = inspect.unwrap(function)
    code = original.__code__
    frame: FrameType | None = sys._getframe(1)
    while frame is not None:
        if frame.f_code is code and frame.f_globals is original.__globals__:
            return frame
        frame = frame.f_back
    return None


def _current():
    from . import current_quarantine_finalization as current

    require_module_seal(sys.modules[__name__])
    require_module_seal(current)
    return current


def _record_fields(record, *, lifecycle=False):
    value = record.to_dict()
    for name in ("generation", "updated_at", "record_sha256", "api_run_accounting"):
        value.pop(name, None)
    if lifecycle:
        value.pop("finding_lifecycle", None)
    return canonical_bytes(value)


def _trust_profile(caller):
    """Use verifiers captured by the original transport installer, not late exports."""
    from . import api_run_requests, trust_effects

    require_module_seal(api_run_requests)
    if (
        trust_effects.verification_profile
        is not api_run_requests._original_trust_verification_profile
        or trust_effects.verification_calls
        is not api_run_requests._original_trust_verification_calls
    ):
        raise ReviewInputError("current execution trust verification source changed")
    return api_run_requests._original_trust_verification_profile(caller)


def _require(caller):
    current = _current()
    entry = _ACTIVE.get(caller)
    if entry is None:
        raise ReviewInputError("current execution lease is unissued or expired")
    from . import action_run, api_run_requests, quarantine_capacity
    from .guarded_pending_publication import _original_accounting_graph
    from .quarantine_command_namespace import observed_sequence

    for module in (action_run, api_run_requests, quarantine_capacity):
        require_module_seal(module)
    require_instance_source(
        caller,
        methods=("open", "close_action", "_accept_checkpoint", "_require_ledger"),
    )
    state = caller.accounting
    _original_accounting_graph(state, models=True)
    initial = entry["state"]
    if canonical_bytes(initial.to_document()) != entry["state_bytes"]:
        raise ReviewInputError("current initial funding and owner snapshot changed")
    initial.require_successor(state)
    outer = _frame(action_run.execute_command)
    if (
        getattr(caller, "_current_command_execution", None) is not entry["marker"]
        or outer is None
        or id(outer) != entry["execute_frame"]
        or outer.f_locals.get("caller") is not caller
        or outer.f_locals.get("run") is not entry["run"]
        or entry["plan"].payload != entry["plan_payload"]
        or current._PLANS.get(entry["plan"], (None, None))[1] != entry["plan_payload"]
        or state.owner != initial.owner
        or state.remaining != initial.remaining
        or state.prior_nonstartup_liability != initial.prior_nonstartup_liability
        or state.startup_used != initial.startup_used
        or tuple((b.block_id, b.issued_by) for b in state.blocks)
        != tuple((b.block_id, b.issued_by) for b in initial.blocks)
        or caller._terminal_block != entry["terminal_block"]
        or getattr(caller, "_controlled_restart_admission", None) is not None
        or _trust_profile(caller) is not entry["trust_profile"]
        or current._history(entry["ledger"], entry["identity"]) != entry["history"]
        or quarantine_capacity.installed_capacity(entry["ledger"], entry["identity"])
        is not entry["guard"]
        or entry["guard"].comment_plan.worst_read_calls() > entry["census"]
        or current._allocation_seal(entry["allocation"]) != entry["allocation_seal"]
        or caller._root != entry["record"].record_sha256
        or caller._effect_record is not entry["record"]
        or canonical_bytes(entry["record"].to_dict()) != entry["record_bytes"]
        or observed_sequence(caller) != caller._sequence
    ):
        raise ReviewInputError("current execution invocation, root or funding changed")
    caller._require_ledger(entry["ledger"], entry["identity"])
    for block in state.blocks:
        original = next(b for b in initial.blocks if b.block_id == block.block_id)
        if block.undispatched > original.undispatched:
            raise ReviewInputError("current execution funding was replenished")
    return entry


def _journal(entry):
    from . import command_finalization_plan, finding_lifecycle

    require_module_seal(command_finalization_plan)
    require_module_seal(finding_lifecycle)
    journal = entry.get("journal")
    if (
        journal is None
        or command_finalization_plan._EXECUTION_JOURNALS.get(journal)
        is not entry["plan"]
        or journal.ledger is not entry["ledger"]
        or journal.identity != entry["identity"]
        or journal._record is not entry["record"]
    ):
        raise ReviewInputError(
            "current execution journal is not the transferred source"
        )
    require_instance_source(
        journal, methods=("checkpoint", "_preflight_effect", "_dispatch_effect")
    )
    return journal


def attach(plan, journal):
    current = _current()
    frame = sys._getframe(1)
    if frame.f_code is not current.transfer.__code__:
        raise ReviewInputError("current journal transfer is not source-issued")
    entry = _require(journal.ledger.caller)
    if (
        entry["plan"] is not plan
        or entry.get("journal") is not None
        or not entry["own_confirmed"]
    ):
        raise ReviewInputError("current journal transfer is copied or premature")
    entry["journal"] = journal


def namespace(caller, deadline):
    if caller in _EXPIRED:
        raise ReviewInputError("completed current invocation cannot dispatch again")
    if caller not in _ACTIVE:
        if getattr(caller, "_current_command_execution", None) is not None:
            raise ReviewInputError("current execution caller was copied or expired")
        return False
    current = _current()
    if sys._getframe(1).f_code is not current.validate_namespace_request.__code__:
        raise ReviewInputError("current namespace callback source changed")
    from review_sensei.api_work import MAX_API_REQUEST_LIMIT

    from . import api_run_requests, quarantine_checkpoint
    from .quarantine_command_namespace import observed_sequence

    for module in (api_run_requests, quarantine_checkpoint):
        require_module_seal(module)
    entry = _require(caller)
    original = _frame(api_run_requests.ApiRunRequestTransport.open)
    if original is None or original.f_locals.get("self") is not caller:
        raise ReviewInputError("current namespace lacks original request entry")
    terminal = caller._terminal_reason is not None
    phase = "terminal" if terminal else "ordinary"
    if (
        type(deadline) is not int
        or not 0 <= deadline <= MAX_API_REQUEST_LIMIT
        or caller._block != entry[phase + "_block"]
        or observed_sequence(caller) != entry["start"] + sum(entry["used"].values())
        or entry["used"][phase] >= entry["limits"][phase]
        or terminal
        and not entry["terminal_ready"]
    ):
        raise ReviewInputError("current invocation request ceiling or phase changed")
    if entry["terminal_ready"] and not terminal:
        raise ReviewInputError("current invocation cannot return to ordinary work")
    if terminal:
        close = _frame(api_run_requests.ApiRunRequestTransport.close_action)
        if (
            close is None
            or close.f_locals.get("self") is not caller
            or close.f_locals.get("proof") is not entry["terminal_proof"]
            or caller._terminal_reason != "action-completion"
            or getattr(entry["ledger"], "_quarantine_lifecycle_capacity", None)
            is not None
        ):
            raise ReviewInputError("current terminal request is not its original close")
        operation = caller._active_operation
        if entry["terminal_operation"] is None:
            entry["terminal_operation"] = operation
        if (
            operation is None
            or operation is not entry["terminal_operation"]
            or operation is not caller._checkpoint_operation
        ):
            raise ReviewInputError("current terminal operation was restarted")
    request = original.f_locals["request"]
    snapshot = getattr(caller, "_quarantine_checkpoint_snapshot", None)
    if (
        request.get_method() == "PATCH"
        and "/issues/comments/" in request.full_url
        and snapshot is None
    ):
        raise ReviewInputError("current root write lacks original checkpoint custody")
    if snapshot is not None and request.get_method() == "PATCH":
        snapshot.require()
        candidate = snapshot.candidate
        if (
            snapshot.ledger is not entry["ledger"]
            or snapshot.identity != entry["identity"]
            or candidate is None
            or candidate is not caller._checkpoint
            or json.loads(request.data) != {"body": snapshot.candidate_body}
            or not request.full_url.endswith(f"/issues/comments/{snapshot.comment_id}")
        ):
            raise ReviewInputError(
                "current checkpoint candidate or destination changed"
            )
        if terminal:
            if entry["pending"] is not None:
                raise ReviewInputError("current terminal root PATCH was repeated")
        elif not entry["own_confirmed"]:
            from .application import GitHubApplication

            applying = _frame(GitHubApplication.apply_maintainer_command)
            if (
                applying is None
                or id(applying) != entry["apply_frame"]
                or _record_fields(candidate) != _record_fields(entry["preview"])
            ):
                raise ReviewInputError(
                    "current own decision differs from source preview"
                )
        else:
            journal = _journal(entry)
            from review_sensei import finding_lifecycle_store

            from .finding_lifecycle import FindingLifecycleJournal

            require_module_seal(finding_lifecycle_store)
            stage = _frame(finding_lifecycle_store.stage_finding_lifecycle)
            checkpoint = _frame(FindingLifecycleJournal.checkpoint)
            if (
                stage is None
                or checkpoint is None
                or checkpoint.f_locals.get("self") is not journal
                or stage.f_locals.get("ledger") is not entry["ledger"]
                or stage.f_locals.get("record") is not entry["record"]
                or stage.f_locals.get("document")
                is not checkpoint.f_locals.get("packed")
                or _record_fields(candidate, lifecycle=True)
                != _record_fields(entry["record"], lifecycle=True)
            ):
                raise ReviewInputError(
                    "current lifecycle candidate lacks original journal custody"
                )
        rows = entry["projection"]
        root_limit = max(
            row["record_bytes"] for row in rows if row["terminal"] == terminal
        )
        wire_limit = max(
            row["comment_bytes"] for row in rows if row["terminal"] == terminal
        )
        if (
            len(canonical_bytes(candidate.to_dict())) > root_limit
            or len(snapshot.candidate_body.encode()) > wire_limit
        ):
            raise ReviewInputError(
                "current candidate exceeds complete source projection"
            )
        if entry["pending"] is not None:
            raise ReviewInputError("current candidate write cannot be replayed")
        entry["pending"] = (
            candidate,
            canonical_bytes(candidate.to_dict()),
            snapshot.operation,
            terminal,
        )
    entry["used"][phase] += 1
    return True


def confirm(caller, ledger, record, persisted):
    if caller not in _ACTIVE:
        return
    current = _current()
    from . import api_run_requests

    require_module_seal(api_run_requests)
    frame = sys._getframe(2)
    if (
        sys._getframe(1).f_code is not current.confirm_checkpoint.__code__
        or frame.f_code
        is not api_run_requests.ApiRunRequestTransport._accept_checkpoint.__code__
        or any(
            frame.f_locals.get(k) is not v
            for k, v in (
                ("self", caller),
                ("ledger", ledger),
                ("record", record),
                ("persisted", persisted),
            )
        )
    ):
        raise ReviewInputError("current checkpoint confirmation source changed")
    entry = _require(caller)
    pending = entry["pending"]
    if (
        pending is None
        or ledger is not entry["ledger"]
        or caller._checkpoint is not pending[0]
        or canonical_bytes(record.to_dict()) != pending[1]
        or caller._checkpoint_operation is not pending[2]
        or caller._active_operation is not pending[2]
    ):
        raise ReviewInputError("current root confirmation lacks its original candidate")
    persisted.require_successor(caller.accounting)
    preparation = current._EXECUTED.get(entry["plan"])
    if preparation is not None:
        saved = current._PREPARED.get(preparation)
        if (
            saved is None
            or saved["journal"] is not entry["journal"]
            or saved["record"] is not entry["record"]
            or saved["root"] != entry["record_bytes"]
        ):
            raise ReviewInputError("current transferred preparation successor changed")
        # Only this original confirmed successor advances execution custody.
        # The initial sizing preparation and its detached preview stay frozen.
        current._PREPARED[preparation] = {
            **saved,
            "record": record,
            "root": canonical_bytes(record.to_dict()),
        }
    entry["record"], entry["record_bytes"] = record, canonical_bytes(record.to_dict())
    entry["pending"] = None
    if pending[3]:
        _ACTIVE.pop(caller)
        _EXPIRED.add(caller)
    else:
        entry["own_confirmed"] = True


def before_terminal(caller, run, proof):
    if caller not in _ACTIVE:
        return
    current = _current()
    from review_sensei import finding_lifecycle_store, session
    from review_sensei.api_work import ApiRunAccounting

    from . import action_run, api_run_requests
    from .current_command_projection import pending_accounting

    for module in (action_run, api_run_requests, finding_lifecycle_store, session):
        require_module_seal(module)
    frame = sys._getframe(2)
    entry = _require(caller)
    if (
        sys._getframe(1).f_code is not current.before_terminal.__code__
        or frame.f_code is not inspect.unwrap(action_run.execute_command).__code__
        or id(frame) != entry["execute_frame"]
        or any(
            frame.f_locals.get(k) is not v
            for k, v in (("caller", caller), ("run", run), ("proof", proof))
        )
        or run is not entry["run"]
        or type(proof) is not api_run_requests.ActionResultProof
        or proof is not caller._action_result_proof
        or proof.boundary is not caller._action_effect_boundary
        or proof.boundary is not frame.f_locals.get("boundary")
        or not entry["own_confirmed"]
        or entry["pending"] is not None
        or entry["terminal_ready"]
        or caller._terminal_reason is not None
        or caller._active_operation is not None
        or caller._checkpoint is not None
        or caller._checkpoint_operation is not None
        or getattr(run.ledger, "_quarantine_lifecycle_capacity", None) is not None
        or run.ledger._part_staging_identity is not None
        or caller.accounting.action["phase"] != "pending"
        or caller._action_unresolved()
    ):
        raise ReviewInputError(
            "current terminal entry is not its settled original source"
        )
    from review_sensei.action_result_codec import decode_action_result_carrier
    from review_sensei.bounded_evidence import read_inline_evidence

    from .api_run_history import action_result_binding

    binding = action_result_binding(
        entry["identity"], caller.accounting, run.ledger.evidence_producer()
    )
    envelope = decode_action_result_carrier(
        read_inline_evidence(proof.manifest, expected_binding=binding),
        history=caller._action_result_history,
    )
    if (
        envelope["intent_sha256"] != proof.boundary.intent_sha256
        or envelope["outcome"] != proof.outcome
        or len(canonical_bytes(proof.manifest)) > entry["result_bound"]
    ):
        raise ReviewInputError("current settled result exceeds its source envelope")
    # This second, defensive projection starts from the confirmed runtime
    # successor. Never demote already confirmed charges to the initial
    # all-pending shape merely to measure the remaining terminal suffix.
    document = pending_accounting(
        caller.accounting,
        sequence=caller._sequence,
        ordinary_id=entry["ordinary_block"],
        terminal_id=entry["terminal_block"],
        ordinary=0,
        terminal=entry["limits"]["terminal"],
    )
    document["action"].update(
        phase="terminal", result=proof.manifest, outcome=proof.outcome
    )
    terminal_accounting = ApiRunAccounting.from_document(document)
    reference = finding_lifecycle_store.reference_with_terminal_action_history(
        entry["record"],
        accounting=terminal_accounting,
        candidate_history=caller._action_result_history,
    )
    candidate = entry["record"].evolve(
        generation=session.MAX_GENERATION,
        api_run_accounting=document,
        finding_lifecycle=reference,
    )
    require_instance_source(run.ledger, methods=("_render_comment",))
    body = run.ledger._render_comment(
        repository_id=entry["identity"].repository_id,
        pull_request=entry["identity"].pull_request,
        record=candidate,
    )
    if len(canonical_bytes(candidate.to_dict())) > max(
        row["record_bytes"] for row in entry["projection"] if row["terminal"]
    ) or len(body.encode()) > max(
        row["comment_bytes"] for row in entry["projection"] if row["terminal"]
    ):
        raise ReviewInputError(
            "current actual terminal root exceeds complete source projection"
        )
    entry["terminal_ready"] = True
    entry["terminal_proof"] = proof


def issue(plan):
    """Issue only from completed original preflight, never from detached data."""
    current = _current()
    from . import action_run, application, quarantine_capacity
    from .current_command_projection import future_census_bound
    from .quarantine_command_namespace import observed_sequence

    for module in (action_run, application, quarantine_capacity):
        require_module_seal(module)
    caller_frame = sys._getframe(1)
    app_frame = caller_frame.f_back
    if (
        caller_frame.f_code is not current.require_execution_grant.__code__
        or app_frame is None
        or app_frame.f_code
        is not application.GitHubApplication._preflight_command_finalization.__code__
        or id(app_frame.f_locals.get("plan")) != id(plan)
    ):
        raise ReviewInputError(
            "current invocation issuance requires original preflight return"
        )
    saved = current.require_plan(plan, plan.journal)
    caller, ledger = saved["ledger"].caller, saved["ledger"]
    complete = current._COMPLETED_PREVIEWS.get(plan)
    projected = current._PROJECTIONS.get(plan)
    outer = _frame(action_run.execute_command)
    applying = app_frame.f_back
    if (
        complete is None
        or projected is None
        or outer is None
        or outer.f_locals.get("caller") is not caller
        or applying is None
        or applying.f_code
        is not application.GitHubApplication.apply_maintainer_command.__code__
        or caller in _ACTIVE
        or caller in _EXPIRED
        or getattr(caller, "_current_command_execution", None) is not None
        or saved["details"]["staged_parts"]
        or saved["preview_record"].disposition_snapshot is not None
        or caller._active_operation is not None
        or caller._checkpoint is not None
        or caller._terminal_reason is not None
        or getattr(caller, "_controlled_restart_admission", None) is not None
        or _trust_profile(caller) is not None
        or canonical_bytes(caller.accounting.to_document()) != projected[5]
        or observed_sequence(caller) != projected[4]
        or canonical_bytes(projected[0]) != projected[1]
        or canonical_bytes(complete[5]) != complete[7]
        or canonical_bytes([(docs, sorted(exact)) for docs, exact in complete[3]])
        != complete[6]
    ):
        raise ReviewInputError(
            "current complete invocation source domain is unsupported"
        )
    full = current._extended_plan(plan, plan.journal, complete[5]["added_parts"])
    if full is not projected[8] or current._allocation_seal(full) != projected[9]:
        raise ReviewInputError("current invocation full lifetime proposal changed")
    limits = {"ordinary": projected[2], "terminal": projected[3]}
    blocks = {b.block_id: b for b in caller.accounting.blocks}
    if (
        caller._block == caller._terminal_block
        or blocks[caller._block].undispatched < limits["ordinary"]
        or blocks[caller._terminal_block].undispatched < limits["terminal"]
        or getattr(ledger, "_quarantine_lifecycle_capacity", None) is not None
    ):
        raise ReviewInputError("current invocation cannot borrow protected funding")
    marker = _Invocation()
    caller._current_command_execution = marker
    _ACTIVE[caller] = {
        "marker": marker,
        "plan": plan,
        "plan_payload": plan.payload,
        "ledger": ledger,
        "identity": plan.journal.identity,
        "state": caller.accounting,
        "state_bytes": canonical_bytes(caller.accounting.to_document()),
        "record": saved["record"],
        "record_bytes": canonical_bytes(saved["record"].to_dict()),
        "preview": saved["preview_record"],
        "history": saved["history"],
        "run": outer.f_locals["run"],
        "execute_frame": id(outer),
        "apply_frame": id(applying),
        "trust_profile": None,
        "ordinary_block": caller._block,
        "terminal_block": caller._terminal_block,
        "limits": limits,
        "start": projected[4],
        "used": {"ordinary": 0, "terminal": 0},
        "projection": projected[0],
        "result_bound": current._PLANS[plan][3],
        "allocation": full,
        "allocation_seal": current._allocation_seal(full),
        "guard": projected[6],
        "initial_unknown": projected[7].unknown,
        "parts": complete[5]["added_parts"],
        "allocated_parts": 0,
        "census": future_census_bound(full, complete[5]["added_parts"]),
        "own_confirmed": False,
        "pending": None,
        "journal": None,
        "terminal_ready": False,
        "terminal_proof": None,
        "terminal_operation": None,
    }
    return _require(caller)


def require_plan_lease(plan):
    entry = _require(plan.journal.ledger.caller)
    if entry["plan"] is not plan:
        raise ReviewInputError("current invocation plan was copied or replaced")
    return entry


def lifecycle_context(ledger, identity, known, added_parts):
    current = _current()
    caller = getattr(ledger, "caller", None)
    if caller is None or caller not in _ACTIVE:
        return None
    from review_sensei import finding_lifecycle_store

    from . import finding_lifecycle, quarantine_capacity

    for module in (finding_lifecycle, quarantine_capacity, finding_lifecycle_store):
        require_module_seal(module)
    entry = _require(caller)
    journal = _journal(entry)
    helper = sys._getframe(2)
    extension = helper.f_back
    if (
        sys._getframe(1).f_code is not current.lifecycle_execution_context.__code__
        or helper.f_code
        is not quarantine_capacity._current_lifecycle_execution.__code__
        or extension is None
        or extension.f_code
        is not quarantine_capacity.lifecycle_capacity_extension.__code__
        or extension.f_locals.get("ledger") is not ledger
        or extension.f_locals.get("identity") is not identity
        or extension.f_locals.get("known") is not known
        or extension.f_locals.get("added_parts") != added_parts
        or ledger is not entry["ledger"]
        or identity != entry["identity"]
    ):
        raise ReviewInputError(
            "current lifecycle context requires original proposal source"
        )
    current._known_binding(known, added_parts)
    stage = _frame(finding_lifecycle_store.stage_finding_lifecycle)
    checkpoint = _frame(finding_lifecycle.FindingLifecycleJournal.checkpoint)
    preflight = _frame(finding_lifecycle.FindingLifecycleJournal._preflight_effect)
    if stage is not None:
        if (
            checkpoint is None
            or checkpoint.f_locals.get("self") is not journal
            or stage.f_locals.get("ledger") is not ledger
            or stage.f_locals.get("record") is not entry["record"]
            or stage.f_locals.get("document") is not checkpoint.f_locals.get("packed")
        ):
            raise ReviewInputError("current lifecycle stage source changed")
        stage_key = id(stage), canonical_bytes(stage.f_locals["document"])
    elif preflight is not None and preflight.f_locals.get("self") is journal:
        # This runtime preflight context cannot activate a writer. A fresh
        # original stage invocation must produce its own separate context.
        stage_key = None
    else:
        raise ReviewInputError("current lifecycle source is not a planned journal path")
    token = _Context()
    _CONTEXTS[token] = (
        entry,
        ledger,
        identity,
        caller._root,
        canonical_bytes(entry["record"].to_dict()),
        current._known_binding(known, added_parts),
        stage_key,
    )
    return token


def consume_context(token, proof, operation, known, added_parts):
    current = _current()
    saved = _CONTEXTS.get(token) if type(token) is _Context else None
    if saved is None or saved[6] is None:
        raise ReviewInputError(
            "current lifecycle context is unissued, consumed or sizing-only"
        )
    from review_sensei import finding_lifecycle_store
    from review_sensei.bounded_evidence import MAX_PART_BYTES

    from . import quarantine_capacity

    require_module_seal(quarantine_capacity)
    require_module_seal(finding_lifecycle_store)
    frame = sys._getframe(2)
    if (
        sys._getframe(1).f_code
        is not current.consume_lifecycle_execution_context.__code__
        or frame.f_code is not quarantine_capacity.activate_lifecycle_extension.__code__
        or id(frame.f_locals.get("proof")) != id(proof)
        or frame.f_locals.get("operation") is not operation
    ):
        raise ReviewInputError(
            "current lifecycle activation lacks original writer source"
        )
    entry, ledger, identity = saved[:3]
    caller = ledger.caller
    if _require(caller) is not entry:
        raise ReviewInputError("current lifecycle invocation changed")
    stage = _frame(finding_lifecycle_store.stage_finding_lifecycle)
    if (
        proof.current_execution is None
        or proof.current_execution[0] is not current
        or proof.current_execution[1] is not token
        or proof.sizing is not None
        or proof.ledger is not ledger
        or proof.identity != identity
        or caller._root != saved[3]
        or canonical_bytes(entry["record"].to_dict()) != saved[4]
        or current._known_binding(known, added_parts) != saved[5]
        or stage is None
        or id(stage) != saved[6][0]
        or canonical_bytes(stage.f_locals["document"]) != saved[6][1]
        or operation is not caller._active_operation
        or operation is not caller._checkpoint_operation
        or entry["allocated_parts"] + added_parts > entry["parts"]
        or proof.plan.worst_read_calls() > entry["census"]
    ):
        raise ReviewInputError("current lifecycle execution prefix changed")
    original = entry["allocation"]
    if (
        set(original.known) - set(proof.plan.known)
        or any(proof.plan.known[key] > value for key, value in original.known.items())
        or len(set(proof.plan.known) - set(original.known)) > entry["allocated_parts"]
        or any(
            value > MAX_PART_BYTES + 512
            for key, value in proof.plan.known.items()
            if key not in original.known
        )
        or tuple(proof.plan.unknown)
        != (
            *entry["initial_unknown"],
            *((MAX_PART_BYTES + 512,) * (entry["allocated_parts"] + added_parts)),
        )
        or tuple(proof.plan.metadata_known.items())
        != tuple(original.metadata_known.items())
        or proof.plan.metadata_unknown != original.metadata_unknown
    ):
        raise ReviewInputError("current lifecycle full lifetime allocation changed")
    _CONTEXTS.pop(token)
    entry["allocated_parts"] += added_parts


from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
