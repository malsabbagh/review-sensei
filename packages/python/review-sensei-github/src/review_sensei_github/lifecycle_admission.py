"""Pure original-criterion admission restoration, never a source capability.

Callers must independently authenticate the complete lifecycle and exact current
transaction/root. This helper only accepts initial, unbound original criteria.
"""

import re
from dataclasses import replace

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError


def restore_original_lifecycle_admission(result, state, *, head_sha, base_sha):
    from review_sensei.active_finding_window import (
        ActiveWindowRoundState,
        obligation_from_comment,
    )
    from review_sensei.finding_drift import (
        DriftEvaluationResult,
        drift_obligation_from_comment,
    )

    restored = {}
    if state.head_sha != head_sha or state.base_sha != base_sha:
        raise ReviewInputError("original lifecycle admission snapshot conflicts")
    if not 0 < len(result.comments) <= 2:
        raise ReviewInputError("original lifecycle admission membership conflicts")
    if type(state) is DriftEvaluationResult:
        if state.slots or len(state.inventory.obligations) != len(result.comments):
            raise ReviewInputError("original lifecycle admission membership conflicts")
        expected_drift = {
            drift_obligation_from_comment(
                comment, head_sha=head_sha
            ).obligation_id: comment
            for comment in result.comments
        }
        if len(expected_drift) != len(result.comments):
            raise ReviewInputError("original lifecycle admission membership conflicts")
        for obligation in state.inventory.obligations:
            comment = expected_drift.get(obligation.obligation_id)
            if comment is None:
                raise ReviewInputError(
                    "original lifecycle admission membership conflicts"
                )
            identity = drift_obligation_from_comment(comment, head_sha=head_sha)
            if any(
                getattr(obligation.criterion, field)
                != getattr(identity.criterion, field)
                for field in (
                    "obligation_id",
                    "original_fingerprint",
                    "resolution_criterion",
                    "origin_head_sha",
                    "origin_path",
                    "origin_line",
                    "origin_side",
                    "origin_symbol",
                    "defect_kind",
                    "concern",
                )
            ) or (
                obligation.present_anchor != identity.present_anchor
                or obligation.generation != 0
                or obligation.lifecycle_state != "active-unresolved"
                or obligation.thread_binding is not None
                or obligation.resolution_proof_sha256 is not None
                or obligation.latest_comment is None
                or canonical_bytes(obligation.latest_comment.to_dict())
                != canonical_bytes(comment.to_dict())
            ):
                raise ReviewInputError(
                    "original lifecycle admission membership conflicts"
                )
            restored[canonical_bytes(comment.to_dict())] = replace(
                comment,
                effective_blocking=obligation.criterion.blocking,
                needs_human=obligation.criterion.needs_human,
            )
        obligations = ()
    elif type(state) is ActiveWindowRoundState:
        obligations = state.authoritative_decision_inventory.obligations
        if (
            len(obligations) != len(result.comments)
            or state.display_active_findings.slots
        ):
            raise ReviewInputError("original lifecycle admission membership conflicts")
    else:
        raise ReviewInputError("original lifecycle admission membership conflicts")
    expected = {}
    for comment in result.comments:
        created = obligation_from_comment(comment)
        if created.obligation_id in expected:
            raise ReviewInputError("original lifecycle admission membership conflicts")
        expected[created.obligation_id] = comment
    for obligation in obligations:
        comment = expected.get(obligation.obligation_id)
        if comment is None:
            raise ReviewInputError("original lifecycle admission membership conflicts")
        identity = obligation_from_comment(comment)
        if any(
            getattr(obligation, field) != getattr(identity, field)
            for field in (
                "obligation_id",
                "fingerprint",
                "resolution_criterion",
                "path",
                "line",
                "side",
                "symbol",
                "defect_kind",
                "concern",
            )
        ) or (
            obligation.generation != 0
            or obligation.lifecycle_state != "active-unresolved"
            or obligation.native_target is not None
            or obligation.resolution_proof_sha256 is not None
        ):
            raise ReviewInputError("original lifecycle admission membership conflicts")
        # Public result decoding intentionally omits runtime admission flags.
        # Preserve the authenticated inventory's classification/evidence; never
        # replace those facts with the raw result's advisory defaults.
        if obligation.comment is None or canonical_bytes(
            obligation.comment.to_dict()
        ) != canonical_bytes(comment.to_dict()):
            raise ReviewInputError("original lifecycle admission membership conflicts")
        restored[canonical_bytes(comment.to_dict())] = replace(
            comment,
            effective_blocking=obligation.blocking,
            needs_human=obligation.needs_human,
        )
    return replace(
        result,
        comments=tuple(
            restored[canonical_bytes(comment.to_dict())] for comment in result.comments
        ),
    )


def initial_check_effects(effects, repository):
    """Only initial native check receipts, never unknown or finding mutations."""
    prefix = f"/repos/{repository}/check-runs"
    return all(
        effect["status"] in {"confirmed", "rejected"}
        and (
            effect["method"] == "POST"
            and effect["path"] == prefix
            or effect["method"] == "PATCH"
            and re.fullmatch(re.escape(prefix) + r"/[1-9][0-9]*", effect["path"])
        )
        for effect in effects
    )
