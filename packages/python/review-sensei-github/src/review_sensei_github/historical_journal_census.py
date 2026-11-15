"""Checkpoint-only enumeration geometry; never a lifetime allocation waiver.

The existing historical admission and reservations are unchanged. This reader
can only narrow fresh enumeration inside the original two inline checkpoints.
"""

import hashlib
import json
import sys
from typing import Any, cast
from weakref import WeakSet

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.source_registry import require_module_seal

from .packed_comment_census import CommentAllocationPlan

_ATTEMPTED: WeakSet = WeakSet()


def _plain_data(value):
    """Validate finite exact DATA before hashing, lookup or comparison."""
    active = set()
    nodes = 0

    def visit(item, depth):
        nonlocal nodes
        nodes += 1
        if nodes > 10000 or depth > 20:
            raise ReviewInputError("historical journal DATA exceeds bounds")
        kind = type(item)
        if item is None or kind is bool or kind is int:
            return
        if kind is str:
            if len(item) > 65536:
                raise ReviewInputError("historical journal DATA string exceeds bounds")
            return
        if kind is not dict and kind is not list and kind is not tuple:
            raise ReviewInputError("historical journal DATA is not plain")
        identity = id(item)
        if identity in active:
            raise ReviewInputError("historical journal DATA is cyclic")
        active.add(identity)
        try:
            if kind is dict:
                if any(type(key) is not str for key in item):
                    raise ReviewInputError("historical journal DATA keys changed")
                for child in item.values():
                    visit(child, depth + 1)
            else:
                for child in item:
                    visit(child, depth + 1)
        finally:
            active.remove(identity)

    visit(value, 0)


def prospective_geometry(known, lifetime, event_wire):
    """DATA only: one unknown event, all interleavings, default metadata.

    Only a numerical nonfit returns None. Invalid data/source never falls back.
    """
    from . import packed_comment_census

    require_module_seal(sys.modules[__name__])
    require_module_seal(packed_comment_census)
    lifetime_known, lifetime_metadata, lifetime_unknown, metadata_unknown = (
        packed_comment_census._single_plan_snapshot(lifetime)
    )
    if (
        type(known) is not dict
        or type(lifetime) is not CommentAllocationPlan
        or type(event_wire) is not int
        or event_wire <= 0
        or any(
            type(key) is not int or type(value) is not int
            for key, value in known.items()
        )
        or set(dict(lifetime_known)) - set(known)
        or any(known[key] < value for key, value in lifetime_known)
    ):
        raise ReviewInputError("historical prospective journal inputs changed")
    future = CommentAllocationPlan(known, (event_wire,))
    calls = future.worst_read_calls()
    if calls > 3:
        return None
    return {
        "protocol": "historical-inline-journal-c3-v1",
        "known": [[key, value] for key, value in known.items()],
        "event_unknown": [event_wire],
        "metadata_unknown": future.metadata_unknown,
        "complete_calls": calls,
        "lifetime_known": [[key, value] for key, value in lifetime_known],
        "lifetime_unknown": list(lifetime_unknown),
        "lifetime_metadata": [[key, value] for key, value in lifetime_metadata],
        "lifetime_metadata_unknown": metadata_unknown,
    }


def proposal_descriptor(run, guard, captured, recipe):
    """Recompute DATA only inside original preparation or activation."""
    from . import (
        operation_host,
        packed_comment_census,
        quarantine_capacity,
        quarantine_command_suffix,
        quarantine_historical_execution,
        session_ledger,
    )

    for module in (
        sys.modules[__name__],
        quarantine_command_suffix,
        quarantine_historical_execution,
        session_ledger,
        operation_host,
        quarantine_capacity,
        packed_comment_census,
    ):
        require_module_seal(module)
    frame = sys._getframe(1)
    values = cast(Any, frame.f_locals)
    preparing = (
        frame.f_code is quarantine_command_suffix.prepare.__code__
        and frame.f_globals is vars(quarantine_command_suffix)
    )
    activating = (
        frame.f_code is quarantine_historical_execution.activate.__code__
        and frame.f_globals is vars(quarantine_historical_execution)
    )
    if (
        not (preparing or activating)
        or values.get("run") is not run
        or values.get("recipe") is not recipe
    ):
        raise ReviewInputError("historical prospective journal source changed")
    if preparing:
        if values.get("guard") is not guard or values.get("captured") is not captured:
            raise ReviewInputError("historical prospective journal preparation changed")
    else:
        entry = values.get("entry")
        if (
            type(entry) is not dict
            or entry.get("guard") is not guard
            or entry.get("captured") is not captured
        ):
            raise ReviewInputError("historical prospective journal activation changed")
    if recipe["contract"] not in (
        "closed-inline-c5-r2-p2-no-verification-v1",
        "closed-inline-c5-r2-p2-journal-c3-reserved105-v1",
    ):
        return None
    measured = recipe["root_measurement"]
    if (
        measured.get("max_checkpoints") != 2
        or measured.get("all_growth_roots_fit") is not True
        or not measured.get("growth_roots")
        or any(
            row.get("original_validated_and_rendered") is not True
            or row.get("root_fits") is not True
            for row in measured["growth_roots"]
        )
    ):
        raise ReviewInputError("historical prospective journal root proof changed")
    if run.caller.accounting.prior_nonstartup_liability or any(
        block.pending for block in run.caller.accounting.blocks
    ):
        raise ReviewInputError(
            "historical prospective journal retains physical uncertainty"
        )
    if quarantine_capacity.installed_capacity(run.ledger, run.identity) is not guard:
        raise ReviewInputError("historical prospective journal guard changed")
    rows = json.loads(captured["rows"])
    known = {}
    for row in rows:
        identifier, body = row["id"], row["body"]
        if type(identifier) is not int or type(body) is not str or identifier in known:
            raise ReviewInputError("historical prospective journal membership changed")
        bound = max(
            guard.comment_plan.known.get(identifier, 0),
            packed_comment_census.wire({"body": body}),
        )
        if body.startswith(operation_host.OPERATION_COMMENT_MARKER + "\n"):
            # The supported original operation envelope is 65536 bytes.
            # This is a conservative capacity ceiling, never an identity.
            bound = max(bound, 65536)
        if (
            session_ledger.parse_session_comment(body, identity=run.identity)
            is not None
        ):
            # Same installed-guard bound used by lifecycle_capacity_plan. The
            # original guard enforces it on every root PATCH; do not shrink it.
            bound = max(bound, guard.closure_wire)
        known[identifier] = bound
    event_wire = packed_comment_census.wire({"body": recipe["event_exact_body"]})
    if (
        len(canonical_bytes({"body": recipe["event_exact_body"]}))
        != recipe["event_wire_bound"]
    ):
        raise ReviewInputError("historical prospective event wire changed")
    descriptor = prospective_geometry(known, guard.comment_plan, event_wire)
    if descriptor is None:
        return None
    descriptor["binding"] = hashlib.sha256(
        canonical_bytes(
            {
                "rows": captured["rows"].decode(),
                "root": run.caller._root,
                "accounting": run.caller.accounting.to_document(),
                "guard": hashlib.sha256(
                    quarantine_capacity._guard_state(guard)
                ).hexdigest(),
                "event": recipe["event_exact_body"],
                "root_measurement": measured,
                "review": recipe["review"],
                "check": recipe["check"],
            }
        )
    ).hexdigest()
    return descriptor


def validate_actual_plan(descriptor, plan, membership):
    """DATA comparison; original live scope/confirmed-event checks are separate."""
    from . import packed_comment_census

    require_module_seal(sys.modules[__name__])
    require_module_seal(packed_comment_census)
    known, metadata, unknown, metadata_unknown = (
        packed_comment_census._single_plan_snapshot(plan)
    )
    _plain_data(descriptor)
    _plain_data(membership)
    if type(descriptor) is not dict or type(membership) is not tuple:
        raise ReviewInputError("historical journal prospective binding is absent")
    pairs = descriptor.get("known")
    event_slots = descriptor.get("event_unknown")
    old_unknown = descriptor.get("lifetime_unknown")
    maximum_metadata = descriptor.get("metadata_unknown")
    if (
        type(pairs) is not list
        or any(
            type(pair) is not list
            or len(pair) != 2
            or type(pair[0]) is not int
            or type(pair[1]) is not int
            for pair in pairs
        )
        or type(event_slots) is not list
        or len(event_slots) != 1
        or type(event_slots[0]) is not int
        or type(old_unknown) is not list
        or any(type(value) is not int for value in old_unknown)
        or type(maximum_metadata) is not int
        or any(type(key) is not int for key in membership)
    ):
        raise ReviewInputError("historical journal prospective DATA changed")
    before = dict(pairs)
    if (
        len(before) != len(pairs)
        or len(membership) != len(before) + 1
        or membership[:-1] != tuple(before)
        or len(set(membership)) != len(membership)
    ):
        raise ReviewInputError("historical journal prospective membership changed")
    event = membership[-1]
    allowed = {**before, event: event_slots[0]}
    # A changed API order holds; this is not guaranteed completion under reorder.
    if (
        tuple(key for key, _ in known if key != event) != tuple(before)
        or set(dict(known)) != set(allowed)
        or any(value > allowed[key] for key, value in known)
        or unknown != tuple(old_unknown)
        or any(value > maximum_metadata for _, value in metadata)
        or metadata_unknown > maximum_metadata
    ):
        raise ReviewInputError(
            "historical journal actual growth exceeds prospective bound"
        )


def project(lifetime, membership):
    """Pure geometry preserving every known positive-growth and metadata bound."""
    from . import packed_comment_census

    require_module_seal(sys.modules[__name__])
    require_module_seal(packed_comment_census)
    if CommentAllocationPlan is not vars(packed_comment_census).get(
        "CommentAllocationPlan"
    ):
        raise ReviewInputError("historical journal plan defining source changed")
    if type(lifetime) is not CommentAllocationPlan or type(membership) is not tuple:
        raise ReviewInputError("historical journal geometry is unsupported")
    if any(type(key) is not int or key <= 0 for key in membership):
        raise ReviewInputError("historical journal membership is invalid")
    if len(set(membership)) != len(membership) or set(membership) != set(
        lifetime.known
    ):
        raise ReviewInputError("historical journal membership is incomplete")
    selected = CommentAllocationPlan(
        lifetime.known,
        (),
        metadata_known=lifetime.metadata_known,
        metadata_unknown=lifetime.metadata_unknown,
    )
    if selected.worst_read_calls() > 3:
        raise ReviewInputError("historical journal complete geometry exceeds C3")
    return selected


def complete_read_cap(required, supplied):
    """Never increase a caller's physical ceiling to complete a census."""
    if type(required) is not int or not 2 <= required <= 10:
        raise ReviewInputError("historical journal complete read bound is invalid")
    if supplied is None:
        return required
    if type(supplied) is not int or not 2 <= supplied <= 10:
        raise ReviewInputError("packed comment physical request bound is invalid")
    if supplied < required:
        raise ReviewInputError(
            "historical journal caller cap cannot fund complete census"
        )
    return required


def capacity_definition():
    """Resolve the sealed installed wrapper's exact original defining body."""
    import inspect
    from types import FunctionType

    from . import quarantine_census

    require_module_seal(sys.modules[__name__])
    require_module_seal(quarantine_census)
    exported = vars(quarantine_census).get("lifecycle_capacity_plan")
    if type(exported) is not FunctionType:
        raise ReviewInputError("historical journal capacity export changed")
    # The module seal covers the wrapper chain identity, code, globals,
    # defaults and __wrapped__ metadata before inspect follows that chain.
    original = inspect.unwrap(exported)
    if (
        type(original) is not FunctionType
        or original.__globals__ is not vars(quarantine_census)
        or original.__closure__ is not None
    ):
        raise ReviewInputError("historical journal capacity definition changed")
    return original


def choose(ledger, identity, *, plan, added_parts):
    from . import quarantine_census
    from . import quarantine_historical_execution as historical

    require_module_seal(sys.modules[__name__])
    require_module_seal(quarantine_census)
    require_module_seal(historical)
    original = capacity_definition()
    frame = sys._getframe(1)
    if (
        frame.f_code is not original.__code__
        or frame.f_globals is not vars(quarantine_census)
        or frame.f_locals.get("ledger") is not ledger
        or frame.f_locals.get("plan") is not plan
        or frame.f_locals.get("identity") is not identity
        or frame.f_locals.get("added_parts") != added_parts
    ):
        raise ReviewInputError("historical journal plan lacks original capacity frame")
    membership = historical.journal_census_membership(
        ledger, identity, added_parts=added_parts
    )
    if membership is None:
        return None
    descriptor = historical.journal_census_descriptor(ledger, identity)
    validate_actual_plan(descriptor, plan, membership)
    return project(plan, membership)


def require_scope(scope):
    from . import quarantine_census
    from . import quarantine_historical_execution as historical

    require_module_seal(quarantine_census)
    if type(scope) is not quarantine_census._LifecycleCapacity:
        raise ReviewInputError("historical journal scope is unissued")
    issued = quarantine_census._LIFECYCLE_ACTIVE.get(scope)
    if issued is None or issued[0] != quarantine_census._LifecycleCapacity._seal(scope):
        raise ReviewInputError("historical journal scope is copied or changed")
    require_module_seal(sys.modules[__name__])
    require_module_seal(historical)
    membership = historical.journal_census_membership(
        scope.ledger,
        scope.identity,
        added_parts=scope.parts,
    )
    if membership is None or type(scope.read_plan) is not CommentAllocationPlan:
        raise ReviewInputError("historical journal reader expired")
    descriptor = historical.journal_census_descriptor(scope.ledger, scope.identity)
    validate_actual_plan(descriptor, scope.plan, membership)
    expected = project(scope.plan, membership)
    actual = scope.read_plan
    if (
        dict(actual.known) != dict(expected.known)
        or actual.unknown
        or dict(actual.metadata_known) != dict(expected.metadata_known)
        or actual.metadata_unknown != expected.metadata_unknown
    ):
        raise ReviewInputError("historical journal enumeration changed")


def packed_reader(expected=None):
    """Authenticate defining source before resolving an external-call target."""
    from types import FunctionType

    from . import packed_comment_census

    require_module_seal(sys.modules[__name__])
    require_module_seal(packed_comment_census)
    reader = vars(packed_comment_census).get("fetch_packed_comments")
    if type(reader) is not FunctionType or (
        expected is not None and reader is not expected
    ):
        raise ReviewInputError("historical packed reader source changed")
    return reader


def _reader_frame(scope):
    from . import quarantine_census

    require_module_seal(quarantine_census)
    frame = sys._getframe(2)
    if (
        frame.f_code is not quarantine_census.fetch_comments.__code__
        or frame.f_globals is not vars(quarantine_census)
        or frame.f_locals.get("lifecycle") is not scope
    ):
        raise ReviewInputError("historical journal read lacks original source")
    return frame


def begin(scope):
    _reader_frame(scope)
    require_scope(scope)
    if scope in _ATTEMPTED:
        raise ReviewInputError("historical journal complete read cannot be retried")
    _ATTEMPTED.add(scope)


def finish(scope, rows):
    frame = _reader_frame(scope)
    if frame.f_locals.get("rows") is not rows:
        raise ReviewInputError("historical journal returned rows were substituted")
    require_scope(scope)
    if scope not in _ATTEMPTED or type(rows) is not list:
        raise ReviewInputError("historical journal read completion changed")
    return validate_membership(scope.read_plan, rows)


def validate_membership(plan, rows):
    if type(plan) is not CommentAllocationPlan or type(rows) is not list:
        raise ReviewInputError("historical journal membership source changed")
    identifiers = []
    for row in rows:
        if type(row) is not dict or any(type(key) is not str for key in row):
            raise ReviewInputError("historical journal row is unsupported")
        identifier = row.get("id")
        if type(identifier) is not int or identifier <= 0:
            raise ReviewInputError("historical journal row identity changed")
        identifiers.append(identifier)
    if len(set(identifiers)) != len(identifiers) or set(identifiers) != set(plan.known):
        raise ReviewInputError("historical journal fresh membership changed")
    return rows


from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
