"""Exact reviewed retained-record data snapshot, never record authority.

Copied without bound changes from guarded_hot_namespace at 5a38f56f. Native
signed-63 values outside the original signed-53 graph domain remain unsupported.
"""

from review_sensei.api_work import MAX_API_REQUEST_LIMIT
from review_sensei.errors import ReviewInputError
from review_sensei.source_registry import require_module_seal

_RECORD_FIELDS = frozenset(
    (
        "repository",
        "pull_request",
        "repository_id",
        "completed_initial_reviews",
        "completed_verification_rounds",
        "failed_attempts",
        "generation",
        "reservation_id",
        "reserved_slot",
        "last_committed_reservation_id",
        "created_at",
        "updated_at",
        "expires_at",
        "record_sha256",
        "operator_paused",
        "failed_attempts_head_sha",
        "dispositions",
        "continuation_grants",
        "transaction",
        "convergence_history",
        "reservation_owner",
        "rescan_state",
        "assessment_queue",
        "disposition_snapshot",
        "api_run_accounting",
        "historical_assessments",
        "quarantine_lineage",
        "publication_staging",
        "finding_lifecycle",
        "trust_enrollment",
        "_digest_shape",
    )
)
_TRANSACTION_FIELDS = frozenset(
    (
        "transaction_id",
        "repository",
        "pull_request",
        "base_sha",
        "head_sha",
        "policy_digest",
        "configuration_digest",
        "evidence_digest",
        "reservation_id",
        "generation",
        "phase",
        "result_sha256",
    )
)


def _record_snapshot(record):
    """Detached finite original data, without invoking instance serializers."""
    from review_sensei import models, session

    require_module_seal(session)
    require_module_seal(models)
    if type(record) is not session.SessionRecord:
        raise ReviewInputError("hot checkpoint record source changed")
    count = 0
    size = 0

    def visit(item, ancestors, depth, model_allowed=None):
        nonlocal count, size
        count += 1
        if count > 100_000 or depth > 32:
            raise ReviewInputError("hot checkpoint graph is unbounded")
        kind = type(item)
        if item is None or kind is bool:
            return item
        if kind is str:
            size += len(item.encode("utf-8"))
            if size > 8 * 1024 * 1024:
                raise ReviewInputError("hot checkpoint graph is unbounded")
            return item
        if kind is int:
            if not -MAX_API_REQUEST_LIMIT <= item <= MAX_API_REQUEST_LIMIT:
                raise ReviewInputError("hot checkpoint integer is unbounded")
            return item
        if id(item) in ancestors:
            raise ReviewInputError("hot checkpoint graph is cyclic")
        ancestors = ancestors | {id(item)}
        if kind in (tuple, list):
            return [visit(value, ancestors, depth + 1) for value in item]
        if kind in (session.SessionRecord, models.ReviewTransaction):
            if kind is not model_allowed:
                raise ReviewInputError("hot checkpoint model placement changed")
            fields = (
                _RECORD_FIELDS if kind is session.SessionRecord else _TRANSACTION_FIELDS
            )
            raw = object.__getattribute__(item, "__dict__")
            if (
                type(raw) is not dict
                or any(type(key) is not str for key in raw)
                or set(raw) != fields
            ):
                raise ReviewInputError("hot checkpoint declared fields changed")
            item = raw
        elif kind is not dict:
            raise ReviewInputError("hot checkpoint graph contains non-original data")
        if any(type(key) is not str for key in item):
            raise ReviewInputError("hot checkpoint graph keys changed")
        return {
            key: visit(
                value,
                ancestors,
                depth + 1,
                models.ReviewTransaction
                if kind is session.SessionRecord and key == "transaction"
                else None,
            )
            for key, value in item.items()
        }

    return visit(record, set(), 0, session.SessionRecord)


from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
