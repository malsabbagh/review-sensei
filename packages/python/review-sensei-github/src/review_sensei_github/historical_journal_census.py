"""Checkpoint-only enumeration geometry; never a lifetime allocation waiver.

The existing historical admission and reservations are unchanged. This reader
can only narrow fresh enumeration inside the original two inline checkpoints.
"""

import sys
from weakref import WeakSet

from review_sensei.errors import ReviewInputError
from review_sensei.source_registry import require_module_seal

from .packed_comment_census import CommentAllocationPlan

_ATTEMPTED: WeakSet = WeakSet()


def project(lifetime, membership):
    """Pure geometry preserving every known positive-growth and metadata bound."""
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


def choose(ledger, identity, *, plan, added_parts):
    from . import quarantine_census
    from . import quarantine_historical_execution as historical

    require_module_seal(sys.modules[__name__])
    require_module_seal(quarantine_census)
    require_module_seal(historical)
    frame = sys._getframe(1)
    if (
        frame.f_code is not quarantine_census.lifecycle_capacity_plan.__code__
        or frame.f_globals is not vars(quarantine_census)
        or frame.f_locals.get("ledger") is not ledger
        or frame.f_locals.get("plan") is not plan
    ):
        raise ReviewInputError("historical journal plan lacks original capacity frame")
    membership = historical.journal_census_membership(
        ledger, identity, added_parts=added_parts
    )
    return None if membership is None else project(plan, membership)


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
    expected = project(scope.plan, membership)
    actual = scope.read_plan
    if (
        dict(actual.known) != dict(expected.known)
        or actual.unknown
        or dict(actual.metadata_known) != dict(expected.metadata_known)
        or actual.metadata_unknown != expected.metadata_unknown
    ):
        raise ReviewInputError("historical journal enumeration changed")


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


def begin(scope):
    _reader_frame(scope)
    require_scope(scope)
    if scope in _ATTEMPTED:
        raise ReviewInputError("historical journal complete read cannot be retried")
    _ATTEMPTED.add(scope)


def finish(scope, rows):
    _reader_frame(scope)
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
