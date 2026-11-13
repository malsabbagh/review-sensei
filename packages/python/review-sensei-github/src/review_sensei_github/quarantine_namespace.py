"""Separate a fully represented predecessor from the fresh finding namespace.

This is a read-only consumer of original source authentication. It neither
removes the predecessor reference nor completes any historical native effect.
"""

from __future__ import annotations

import json
from typing import Any, cast

from review_sensei.active_finding_window import (
    ActiveWindowRoundState,
    obligation_from_comment,
)
from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.context import finding_lifecycle_for_comment
from review_sensei.errors import ReviewInputError
from review_sensei.finding_drift import drift_obligation_from_comment
from review_sensei.human_assessment import finding_instance_fingerprint
from review_sensei.models import ReviewResult


def inherited_namespace_is_historical(ledger, record, state, document):
    if record.quarantine_lineage is None:
        return False
    from .quarantine_origin import (
        _AUTHENTICATED,
        AuthenticatedQuarantine,
        read_predecessor,
    )

    authenticated = getattr(ledger, "_quarantine_authenticated", None)
    caller = getattr(ledger, "caller", None)
    if (
        type(authenticated) is not AuthenticatedQuarantine
        or authenticated not in _AUTHENTICATED
        or caller is None
        or authenticated.original_caller is not caller
        or authenticated.original_identity != record.identity()
        or canonical_bytes(record.quarantine_lineage) != authenticated.lineage_bytes
    ):
        raise ReviewInputError(
            "quarantine finding namespace lacks authenticated origins"
        )
    cast(Any, caller)._require_ledger(ledger, record.identity())
    if authenticated.record_sha256 != record.record_sha256:
        from .quarantine_checkpoint import confirmed_checkpoint_capacity

        receipt = confirmed_checkpoint_capacity(caller, ledger, record.identity())
        if receipt.record.to_dict() != record.to_dict():
            raise ReviewInputError("quarantine finding namespace current root changed")
    predecessor = read_predecessor(authenticated.archive)
    if (
        predecessor.identity() != record.identity()
        or predecessor.record_sha256
        != record.quarantine_lineage["predecessor_record_sha256"]
    ):
        raise ReviewInputError("quarantine finding namespace predecessor conflicts")
    if record.finding_lifecycle != predecessor.finding_lifecycle:
        return False  # A separately committed current inventory, not old inheritance.
    if predecessor.finding_lifecycle is None or predecessor.transaction is None:
        raise ReviewInputError(
            "quarantine finding namespace original lifecycle is absent"
        )
    if (
        state.head_sha != predecessor.transaction.head_sha
        or state.base_sha != predecessor.transaction.base_sha
    ):
        raise ReviewInputError(
            "quarantine finding namespace original snapshot conflicts"
        )
    # Validate all source inventories and decision membership, not only the
    # current pending subset: a legitimate historical decision must not rename
    # the immutable criterion or make its original source disappear.
    authenticated.pending
    if document["unknown_trust_effect"] or any(
        effect["status"] == "pending" for effect in document["effects"]
    ):
        # Source isolation does not by itself classify unknown native effects.
        # Keep their explicit hold; never replay or silently settle them in the
        # independently funded current finding namespace.
        raise ReviewInputError(
            "quarantine finding namespace has unclassified native liabilities"
        )
    inventories = authenticated.inventories
    sources = json.loads(authenticated.source_documents_bytes)
    covered = set()
    archived = set()
    for origin in authenticated.archive["assessment_origins"]["origins"]:
        inventory = inventories[origin["scope_sha256"]]["assessments"]
        pairs = {(item["instance"], item["concern"]) for item in inventory}
        source = sources[origin["kind"]]
        if (
            source["schema_version"]
            == "quarantine-lifecycle-published-assessment-source-v1"
        ):
            covered.update(pairs)
            archived.update(pairs)
        elif source["schema_version"] == "quarantine-failed-result-source-v1":
            if source["predecessor_record_sha256"] != predecessor.record_sha256:
                raise ReviewInputError(
                    "quarantine finding namespace retained source conflicts"
                )
            result = ReviewResult.from_dict(source["retained_result"])
            if (
                result.transaction is None
                or not result.transaction.logical_identity_matches(
                    predecessor.transaction
                )
                or result.content_digest() != predecessor.transaction.result_sha256
            ):
                raise ReviewInputError(
                    "quarantine finding namespace original result conflicts"
                )
            for comment in result.comments:
                assessed = (
                    finding_instance_fingerprint(comment),
                    finding_lifecycle_for_comment(comment).concern,
                ) in pairs
                # Admission flags cannot change either identity digest. The
                # origin reader already proved which original comments require
                # an assessment. Do not infer that membership from raw flags.
                criterion = (
                    obligation_from_comment(comment)
                    if isinstance(state, ActiveWindowRoundState)
                    else drift_obligation_from_comment(
                        comment, head_sha=predecessor.transaction.head_sha
                    ).criterion
                )
                pair = (criterion.obligation_id, criterion.resolution_criterion)
                archived.add(pair)
                if assessed:
                    covered.add(pair)
    obligations: tuple[Any, ...] = (
        state.authoritative_decision_inventory.obligations
        if isinstance(state, ActiveWindowRoundState)
        else state.inventory.obligations
    )
    for obligation in obligations:
        criterion = (
            obligation
            if isinstance(state, ActiveWindowRoundState)
            else obligation.criterion
        )
        represented = covered if obligation.blocks_approval_now else archived
        if (
            obligation.unresolved
            and (criterion.obligation_id, criterion.resolution_criterion)
            not in represented
        ):
            raise ReviewInputError(
                "quarantine finding namespace has an unrepresented original criterion"
            )
    return True
