"""Strict progress witnesses for authenticated remaining-work checkpoints.

This is accounting evidence, never approval or a fresh execution allowance.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import TYPE_CHECKING

from .errors import ReviewInputError
from .evidence import evidence_digest
from .models import ReviewResult

if TYPE_CHECKING:
    from .session import SessionIdentity, SessionRecord


def coverage_units(coverage):
    entries = [(("file", item.path, -1), item.outcome) for item in coverage.files]
    entries.extend(
        (("hunk", item.path, item.index), item.outcome) for item in coverage.hunks
    )
    return {key for key, _ in entries}, {
        key for key, outcome in entries if outcome == "reviewed"
    }


def execution_receipt(result, *, execution_identity, provider_calls):
    """Only trusted service execution calls this, never the provider parser."""
    inventory, reviewed = coverage_units(result.coverage)
    return {
        "schema_version": "1.0",
        "outcome": "bounded-stop",
        "execution_identity": execution_identity,
        "provider_calls": provider_calls,
        "scope_digest": result.continuation_scope_digest,
        "inventory_digest": evidence_digest(sorted(inventory)),
        "reviewed_digest": evidence_digest(sorted(reviewed)),
    }


def validate_execution_receipt(result):
    receipt = result.bounded_work_receipt
    if (
        not isinstance(receipt, dict)
        or set(receipt)
        != {
            "schema_version",
            "outcome",
            "execution_identity",
            "provider_calls",
            "scope_digest",
            "inventory_digest",
            "reviewed_digest",
        }
        or receipt.get("schema_version") != "1.0"
        or receipt.get("outcome") != "bounded-stop"
        or not isinstance(receipt.get("execution_identity"), str)
        or re.fullmatch(r"[a-f0-9]{32}", receipt["execution_identity"]) is None
        or type(receipt.get("provider_calls")) is not int
        or not 0 < receipt["provider_calls"] <= 64
        or result.review_status != "partial"
        or result.persistence_status is not None
        or result.coverage is None
        or not result.coverage.enumeration_complete
        or result.continuation_scope_digest is None
    ):
        raise ReviewInputError("bounded work execution receipt is invalid")
    expected = execution_receipt(
        result,
        execution_identity=receipt["execution_identity"],
        provider_calls=receipt["provider_calls"],
    )
    if receipt != expected:
        raise ReviewInputError(
            "bounded work execution receipt scope or coverage differs"
        )


def remaining_progress_proof(
    parent: ReviewResult, child: ReviewResult
) -> dict[str, object] | None:
    """Require a strict extension of the exact finite inventory and obligations."""
    before, after = parent.coverage, child.coverage
    if (
        child.bounded_work_receipt is None
        or parent.review_status != "partial"
        or child.review_status != "partial"
        or before is None
        or after is None
        or not before.enumeration_complete
        or not after.enumeration_complete
        or parent.continuation_scope_digest is None
        or parent.continuation_scope_digest != child.continuation_scope_digest
        or (parent.provider, parent.model, parent.work_policy_digest)
        != (child.provider, child.model, child.work_policy_digest)
    ):
        return None

    validate_execution_receipt(child)
    inventory, reviewed = coverage_units(before)
    next_inventory, next_reviewed = coverage_units(after)
    if (
        inventory != next_inventory
        or set(before.enumerated_paths) != set(after.enumerated_paths)
        or not reviewed < next_reviewed
    ):
        return None
    # Full canonical obligations, including classification and human proposals,
    # must survive; counts or a model's "resolved" assertion are insufficient.
    for field in ("comments", "learning_proposals", "finding_lifecycles"):
        old = Counter(
            evidence_digest(item.to_dict()) for item in getattr(parent, field)
        )
        new = Counter(evidence_digest(item.to_dict()) for item in getattr(child, field))
        if old - new:
            return None
    return {
        "schema_version": "1.0",
        "parent_digest": parent.content_digest(),
        "child_digest": child.content_digest(),
        "execution_digest": evidence_digest(dict(child.bounded_work_receipt)),
        "scope_digest": parent.continuation_scope_digest,
        "inventory_digest": evidence_digest(sorted(inventory)),
        "reviewed_before_digest": evidence_digest(sorted(reviewed)),
        "reviewed_after_digest": evidence_digest(sorted(next_reviewed)),
        "reviewed_before": len(reviewed),
        "reviewed_after": len(next_reviewed),
    }


def checkpoint_remaining_progress(
    ledger: object,
    identity: SessionIdentity,
    record: SessionRecord,
    result: ReviewResult,
) -> dict[str, object] | None:
    """Reconstruct from the consumed receipt and authenticated retained source."""
    from .errors import ReviewInputError
    from .models import ReviewTransaction

    if result.bounded_work_receipt is None or record.rescan_state is None:
        return None
    transaction = record.transaction
    if transaction is None:
        return None
    receipts = record.rescan_state["receipts"]
    assert isinstance(receipts, list)
    receipt = next(
        (
            item
            for item in receipts
            if item["reservation"] == transaction.reservation_id
        ),
        None,
    )
    if receipt is None or receipt.get("mode") != "remaining":
        return None
    source = ReviewTransaction.from_dict(receipt["source"])
    loader = getattr(ledger, "load_retained_checkpoint_result", None)
    parent = (
        loader(identity, transaction=source, head_sha=source.head_sha)
        if callable(loader)
        else None
    )
    if (
        not isinstance(parent, ReviewResult)
        or parent.transaction is None
        or not parent.transaction.logical_identity_matches(source)
        or parent.content_digest() != source.result_sha256
        or (
            source.repository,
            source.pull_request,
            source.base_sha,
            source.head_sha,
            source.policy_digest,
            source.configuration_digest,
            source.evidence_digest,
        )
        != (
            transaction.repository,
            transaction.pull_request,
            transaction.base_sha,
            transaction.head_sha,
            transaction.policy_digest,
            transaction.configuration_digest,
            transaction.evidence_digest,
        )
    ):
        raise ReviewInputError(
            "remaining progress requires the exact retained source; no accounting fallback"
        )
    return remaining_progress_proof(parent, result)
