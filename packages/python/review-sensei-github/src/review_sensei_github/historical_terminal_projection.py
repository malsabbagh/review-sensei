"""Conditional DATA for the existing negative sixteen-attempt terminal fence.

Never reduces funding or ordinary failure representation. Activation must
revalidate the same source binding and install the real negative fence.
"""

import hashlib
import sys

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.source_registry import require_module_seal


def _plain_accounting(state):
    """Identity-only model traversal before any source model comparison."""
    from dataclasses import fields

    from review_sensei import api_work
    from review_sensei.source_registry import require_instance_source

    require_module_seal(api_work)
    models = (
        api_work.ApiRunIdentity,
        api_work.ApiExecutionOwner,
        api_work.FrozenApiConfiguration,
        api_work.StartupSlot,
        api_work.StartupPlan,
        api_work.PendingApiRequest,
        api_work.CallerRequestBlock,
        api_work.ApiRunAccounting,
    )
    remaining = 20000

    def visit(value, depth):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 32:
            raise ReviewInputError("terminal DATA graph is unbounded")
        kind = type(value)
        if value is None or kind is str or kind is int or kind is bool:
            return
        if kind is tuple or kind is list:
            for child in value:
                visit(child, depth + 1)
            return
        if kind is dict:
            for key, child in value.items():
                if type(key) is not str:
                    raise ReviewInputError("terminal DATA key is not plain")
                visit(child, depth + 1)
            return
        if not any(kind is model for model in models):
            raise ReviewInputError("terminal DATA graph is not original")
        require_instance_source(value, methods=())
        for field in fields(kind):
            visit(object.__getattribute__(value, field.name), depth + 1)

    if type(state) is not api_work.ApiRunAccounting:
        raise ReviewInputError("terminal DATA accounting is not original")
    visit(state, 0)


def _admitted_terminal_shape(state, terminal_block) -> tuple[int, int, str] | None:
    if terminal_block is None:
        return None
    if state.configuration.limit == 512 and terminal_block.undispatched == 50:
        return (50, 34, "historical-original-512-50-terminal16-v1")
    if state.configuration.limit == 612 and terminal_block.undispatched == 68:
        from .api_run_host import review_startup_plan

        if state.startup == review_startup_plan("command", quarantine_v3=True):
            return (68, 52, "historical-v3-612-68-terminal16-v1")
    return None


def terminal_accounting_data(state, *, ordinary, terminal, sequence):
    """Pure original transitions; every old receipt remains, 34 slots unissued."""
    from review_sensei import api_work

    from . import guarded_pending_publication

    require_module_seal(api_work)
    require_module_seal(guarded_pending_publication)
    _plain_accounting(state)
    guarded_pending_publication._original_accounting_graph(state, models=True)
    if (
        type(ordinary) is not str
        or type(terminal) is not str
        or ordinary == terminal
        or type(sequence) is not int
        or not 0 <= sequence <= api_work.MAX_API_REQUEST_LIMIT
    ):
        raise ReviewInputError("terminal DATA namespace is invalid")
    selected = {b.block_id: b for b in state.blocks}
    shape = (
        None
        if terminal not in selected
        else _admitted_terminal_shape(state, selected[terminal])
    )
    if (
        ordinary not in selected
        or terminal not in selected
        or selected[ordinary].issued_by != state.owner
        or selected[terminal].issued_by != state.owner
        or shape is None
    ):
        raise ReviewInputError("terminal DATA requires original 512/50 tranches")
    projected = state
    for block, count in ((ordinary, selected[ordinary].undispatched), (terminal, 16)):
        for _ in range(count):
            sequence += 1
            if sequence > api_work.MAX_API_REQUEST_LIMIT:
                raise ReviewInputError("terminal DATA sequence exceeds namespace")
            projected = projected.dispatch(
                block,
                api_work.PendingApiRequest(
                    f"c-{state.owner.generation:x}-{sequence:x}",
                    api_work.MAX_API_REQUEST_LIMIT,
                ),
            )
    return projected.to_document()


def _digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _derive(run):
    from . import historical_terminal_fence as fence
    from . import quarantine_command_namespace as namespace

    require_module_seal(sys.modules[__name__])
    require_module_seal(fence)
    require_module_seal(namespace)
    origin = sys._getframe(1)
    if (
        origin.f_globals is not globals()
        or (
            origin.f_code is not prepare.__code__
            and origin.f_code is not require_activation.__code__
        )
        or origin.f_locals.get("run") is not run
    ):
        raise ReviewInputError("terminal DATA derivation lacks original consumer")
    fence.require_projection_shape(run)
    if run is None:
        raise ReviewInputError("terminal DATA run is absent")
    caller = run.caller
    # This authenticates the genuine paid prefix against complete private
    # namespace history. It neither refunds it nor learns from observations.
    full = namespace.project_prepared_prefix(caller)
    current = caller.accounting
    document = terminal_accounting_data(
        current,
        ordinary=caller._block,
        terminal=caller._terminal_block,
        sequence=caller._sequence,
    )
    shape = _admitted_terminal_shape(
        current,
        next((b for b in current.blocks if b.block_id == caller._terminal_block), None),
    )
    if shape is None:
        raise ReviewInputError("terminal DATA requires original 512/50 tranches")
    reserved, retained_undispatched, protocol = shape
    binding = {
        "protocol": protocol,
        "state": _digest(current.to_document()),
        "full_failure": _digest(full),
        "terminal": _digest(document),
        "root": caller._root,
        "sequence": caller._sequence,
        "ordinary": caller._block,
        "terminal_block": caller._terminal_block,
        "reserved": reserved,
        "maximum_attempts": 16,
        "retained_undispatched": retained_undispatched,
    }
    return document, binding, full


def prepare(observation, full_upper):
    from . import quarantine_command_finalization as finalization
    from . import quarantine_command_suffix as suffix

    for module in (sys.modules[__name__], finalization, suffix):
        require_module_seal(module)
    frame = sys._getframe(1)
    wrapper = frame.f_back
    outer = None if wrapper is None else wrapper.f_back
    if (
        frame.f_code is not finalization._measure_inline_branches.__code__
        or frame.f_globals is not vars(finalization)
    ):
        raise ReviewInputError("terminal DATA lacks original branch measurer")
    # Other original sizing callers keep the unchanged full terminal upper.
    if (
        wrapper is None
        or wrapper.f_code is not finalization.measure_inline_branches.__code__
        or wrapper.f_globals is not vars(finalization)
        or outer is None
        or outer.f_code is not finalization.intermediate_suffix_recipe.__code__
        or outer.f_globals is not vars(finalization)
    ):
        return None
    if (
        frame.f_locals.get("observation") is not observation
        or frame.f_locals.get("accounting_upper") is not full_upper
        or wrapper.f_locals.get("observation") is not observation
        or outer.f_locals.get("observation") is not observation
    ):
        raise ReviewInputError("terminal DATA observation binding changed")
    saved = finalization.require_observation(observation)
    run = saved["run"]
    if outer.f_locals.get("saved") is not saved:
        raise ReviewInputError("terminal DATA source observation changed")
    # Unsupported original routes retain the full fifty-attempt upper.
    _plain_accounting(run.caller.accounting)
    state = run.caller.accounting
    block = next(
        (b for b in state.blocks if b.block_id == run.caller._terminal_block), None
    )
    if _admitted_terminal_shape(state, block) is None:
        return None
    document, binding, full = _derive(run)
    if canonical_bytes(full_upper) != canonical_bytes(full):
        raise ReviewInputError("terminal DATA original failure upper changed")
    return document, binding


def require_activation(run, binding):
    from . import quarantine_historical_execution as historical

    require_module_seal(historical)
    frame = sys._getframe(1)
    if (
        frame.f_code is not historical.activate.__code__
        or frame.f_globals is not vars(historical)
        or frame.f_locals.get("run") is not run
    ):
        raise ReviewInputError("terminal DATA activation lacks original source")
    # JSON round-trip comparison is DATA only; reject arbitrary Python objects
    # before serialization or equality. No supplied callback is invoked.
    from . import packed_comment_census

    require_module_seal(packed_comment_census)
    packed_comment_census._single_plain(binding)
    _document, expected, _full = _derive(run)
    if canonical_bytes(binding) != canonical_bytes(expected):
        raise ReviewInputError("terminal DATA activation binding changed")


from review_sensei.source_registry import seal_module as _seal_source  # noqa: E402

_seal_source()
