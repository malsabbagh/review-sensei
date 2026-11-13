"""First-execution, complete creation censuses under unchanged allocation caps.

This is a capacity witness only. Both actual walks still authenticate every
member and body, and creation retains its independent old/new-root fences.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from weakref import WeakKeyDictionary

from review_sensei.bounded_evidence import (
    MAX_PART_BYTES,
    MAX_STORED_PART_BYTES,
    canonical_bytes,
)
from review_sensei.errors import ReviewInputError

_PLANS: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()
_ACTIVE: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()


def _seal(prepared):
    return canonical_bytes(
        {
            "previous": prepared.previous.to_dict(),
            "sources": prepared.sources.decode(),
            "isolation": prepared.isolation.receipt,
            "registration": prepared.slot.registration,
            "configuration": vars(prepared.configuration),
            "owner": vars(prepared.owner),
            "head_sha": prepared.scope.head_sha,
            "base_sha": prepared.scope.base_sha,
            "run": vars(prepared.ledger._startup_caller.run),
            "attempt": prepared.ledger._startup_caller.attempt,
            "job": prepared.ledger._startup_caller.job,
        }
    )


def _require_lifetime_capacity(plan):
    total = sum(plan.known.values()) + sum(plan.unknown)
    if total > MAX_STORED_PART_BYTES:
        raise ReviewInputError("quarantine creation lifetime storage capacity exceeded")
    return total


def prepare_census(prepared):
    from .native_operation_envelope import OPERATION_WIRE_BOUND
    from .operation_host import OPERATION_COMMENT_MARKER
    from .packed_comment_census import CommentAllocationPlan, wire
    from .publication_recovery import _publication_transport
    from .quarantine_capacity import full_review_comment_tail, native_reserve
    from .quarantine_creation import PreparedQuarantine, _plan_quarantine
    from .session_ledger import parse_session_comment, plain_session_comment_json_bound

    if type(prepared) is not PreparedQuarantine:
        raise ReviewInputError("quarantine creation census preparation is unissued")
    PreparedQuarantine.require(prepared)
    result = _plan_quarantine(prepared)
    parts, future_parts = result["parts"], result["future_parts"]
    ledger, identity = prepared.ledger, prepared.identity
    _PLANS[prepared] = (
        ledger,
        ledger.http,
        ledger._startup_caller,
        prepared.identity,
        prepared.slot,
        _seal(prepared),
        None,
        canonical_bytes(result),
        (
            prepared.scope,
            prepared.isolation,
            ledger._startup_caller.plan,
            ledger._startup_caller._slot,
            _publication_transport(ledger.http),
            ledger.token,
        ),
    )
    # Guarded writers retain their original mutable-object/receipt bounds.
    # Their separate extension must be authenticated, not replaced here.
    if (
        prepared.isolation.receipt.get("schema_version")
        != "quarantine-target-isolation-v1"
        or prepared.isolation.receipt.get("contract")
        != "literal-blocker-path-prepublication-v1"
        or native_reserve(prepared.isolation.receipt) is not None
        or getattr(
            _publication_transport(ledger.http), "_quarantine_capacity_guard", None
        )
        is not None
    ):
        return result
    if not ledger._inventory_complete or not (
        ledger._quarantine_required or ledger._quarantine_full_discovery
    ):
        return result
    known, roots = {}, {}
    for row in ledger._cached_comments():
        identifier, body = row.get("id"), row.get("body")
        if (
            type(identifier) is not int
            or identifier <= 0
            or identifier in known
            or not isinstance(body, str)
        ):
            raise ReviewInputError("quarantine creation census frontier conflicts")
        known[identifier] = wire({"body": body})
        record = parse_session_comment(body, identity=identity)
        if record is not None:
            ledger._part_owner(row, identity)
            roots[identifier] = record.record_sha256
            known[identifier] = max(
                known[identifier], 9 + plain_session_comment_json_bound()
            )
        if body.startswith(OPERATION_COMMENT_MARKER + "\n"):
            known[identifier] = max(known[identifier], OPERATION_WIRE_BOUND)
    if roots != {
        prepared.slot.registration[
            "predecessor_comment_id"
        ]: prepared.previous.record_sha256
    }:
        raise ReviewInputError("quarantine creation census predecessor changed")
    plan = CommentAllocationPlan(
        known,
        (
            9 + plain_session_comment_json_bound(),
            *full_review_comment_tail(),
            *((MAX_PART_BYTES + 512,) * (parts + future_parts)),
        ),
    )
    # Lifetime storage/allocation promises stay admitted in full. The phase
    # guard prohibits this successor's later decision/archive allocations until
    # activation finishes, including the successor native publication tail.
    # This exact legacy isolation source has no additional append allocation
    # reserve; mutable predecessor root maxima remain in both census plans.
    lifetime = plan
    result["lifetime_allocations"] = len(lifetime.known) + len(lifetime.unknown)
    result["lifetime_body_bytes"] = _require_lifetime_capacity(lifetime)
    plan = CommentAllocationPlan(
        known,
        (
            9 + plain_session_comment_json_bound(),
            *((MAX_PART_BYTES + 512,) * parts),
        ),
    )
    calls = plan.worst_read_calls()
    if calls > 10:
        raise ReviewInputError("quarantine creation census exceeds original read bound")
    result["requests"] -= 2 * (10 - calls)
    result["census_calls"] = calls
    _PLANS[prepared] = (
        ledger,
        ledger.http,
        ledger._startup_caller,
        prepared.identity,
        prepared.slot,
        _seal(prepared),
        plan,
        canonical_bytes(result),
        (
            prepared.scope,
            prepared.isolation,
            ledger._startup_caller.plan,
            ledger._startup_caller._slot,
            _publication_transport(ledger.http),
            ledger.token,
        ),
    )
    return result


def _require_scope(prepared, issued):
    from .publication_recovery import _publication_transport

    ledger = prepared.ledger
    caller = ledger._startup_caller
    objects = (
        prepared.scope,
        prepared.isolation,
        caller.plan,
        caller._slot,
        _publication_transport(ledger.http),
    )
    if (
        ledger is not issued[0]
        or ledger.http is not issued[1]
        or caller is not issued[2]
        or prepared.identity != issued[3]
        or prepared.slot is not issued[4]
        or _seal(prepared) != issued[5]
        or any(a is not b for a, b in zip(objects, issued[8][:5]))
        or ledger.token != issued[8][5]
        or issued[6] is not None
        and getattr(objects[4], "_quarantine_capacity_guard", None) is not None
    ):
        raise ReviewInputError("quarantine creation request phase changed")


@contextmanager
def creation_census(prepared, plan):
    from .quarantine_creation import PreparedQuarantine

    PreparedQuarantine.require(prepared)
    issued = _PLANS.pop(prepared, None)
    if issued is None:
        raise ReviewInputError(
            "quarantine creation census preparation is unissued or reused"
        )
    ledger = prepared.ledger
    _require_scope(prepared, issued)
    if (
        ledger in _ACTIVE
        or issued[:5]
        != (
            ledger,
            ledger.http,
            ledger._startup_caller,
            prepared.identity,
            prepared.slot,
        )
        or issued[5] != _seal(prepared)
        or issued[7] != canonical_bytes(plan)
    ):
        raise ReviewInputError("quarantine creation census source changed")
    _ACTIVE[ledger] = [prepared, issued, 2, False, None]
    _CALLERS[issued[2]] = ledger
    try:
        yield
    finally:
        _ACTIVE.pop(ledger, None)
        _CALLERS.pop(issued[2], None)


def selected_plan(ledger, identity):
    try:
        active = _ACTIVE.get(ledger)
    except TypeError:
        # Legacy adapters cannot hold this private weak-key capability.
        return None
    if active is None:
        return None
    prepared, issued, remaining, started = active[:4]
    _require_scope(prepared, issued)
    if (
        not started
        or remaining <= 0
        or identity != issued[3]
        or ledger is not issued[0]
        or ledger.http is not issued[1]
        or ledger._startup_caller is not issued[2]
        or prepared.slot is not issued[4]
        or issued[5] != _seal(prepared)
        or not prepared._consumed
    ):
        raise ReviewInputError("quarantine creation census source changed or reused")
    active[2] -= 1
    return issued[6]


def require_execution(prepared, plan):
    active = _ACTIVE.get(prepared.ledger)
    if (
        active is None
        or active[0] is not prepared
        or active[3]
        or active[1][5] != _seal(prepared)
        or active[1][7] != canonical_bytes(plan)
    ):
        raise ReviewInputError("quarantine creation execution is unissued or changed")
    _require_scope(prepared, active[1])
    active[3] = True


_EXECUTOR = None
_EXECUTOR_SOURCES = None
_CALLERS: WeakKeyDictionary = WeakKeyDictionary()


def creation_executor(method):
    """Keep request issuance inside the actual installed creation implementation."""
    from functools import wraps

    @wraps(method)
    def wrapped(prepared, *, actions_token, **kwargs):
        import sys

        module = sys.modules.get("review_sensei_github.quarantine_creation")
        sources = tuple(
            getattr(module, name, None)
            for name in ("create_quarantine", "_execute_creation", "_stage")
        )
        if kwargs or wrapped is not _EXECUTOR or sources != _EXECUTOR_SOURCES:
            raise ReviewInputError("quarantine creation executor source changed")
        live = True

        @contextmanager
        def dispatch(purpose, path, body):
            active = _ACTIVE.get(prepared.ledger)
            if active is None or active[0] is not prepared or not active[3] or not live:
                raise ReviewInputError(
                    "quarantine creation request scope is unavailable"
                )
            _require_scope(prepared, active[1])
            if sources != tuple(
                getattr(module, name, None)
                for name in ("create_quarantine", "_execute_creation", "_stage")
            ):
                raise ReviewInputError("quarantine creation executor source changed")
            if active[1][6] is None:
                yield
                return
            if purpose not in {"carrier", "root"} or active[4] is not None:
                raise ReviewInputError("quarantine creation request purpose conflicts")
            if path != prepared.ledger._comments_path(prepared.identity):
                raise ReviewInputError("quarantine creation request target conflicts")
            import json

            expected = (
                prepared.ledger.http.api_url.rstrip("/") + path,
                json.dumps(body, separators=(",", ":")).encode(),
                "Bearer " + prepared.ledger.token,
            )
            active[4] = expected
            try:
                yield
            finally:
                active[4] = None

        try:
            return method(
                prepared, actions_token=actions_token, _creation_dispatch=dispatch
            )
        finally:
            live = False

    return wrapped


def install_creation_executor(source, execute, stage):
    global _EXECUTOR, _EXECUTOR_SOURCES
    if _EXECUTOR is not None:
        raise ReviewInputError("quarantine creation executor cannot be replaced")
    _EXECUTOR = source
    _EXECUTOR_SOURCES = (source, execute, stage)


def before_creation_request(caller, request):
    ledger = _CALLERS.get(caller)
    if ledger is None:
        return
    active = _ACTIVE.get(ledger)
    if active is None or active[1][6] is None:
        return
    prepared, issued = active[:2]
    _require_scope(prepared, issued)
    if (
        not active[3]
        or caller._state is not None
        or not prepared._consumed
        or issued[2] is not caller
        or issued[5] != _seal(prepared)
    ):
        raise ReviewInputError("quarantine creation request phase changed")
    from .quarantine_census import is_readonly_census

    if request.get_method() in {"GET", "HEAD"} or is_readonly_census(
        request, ledger.http.api_url
    ):
        return
    expected = active[4]
    if (
        request.get_method() != "POST"
        or expected is None
        or (request.full_url, request.data, request.get_header("Authorization"))
        != expected
    ):
        raise ReviewInputError(
            "quarantine creation forbids premature business mutation"
        )
    # Consume before dispatch; an unknown result never permits another attempt.
    active[4] = None
