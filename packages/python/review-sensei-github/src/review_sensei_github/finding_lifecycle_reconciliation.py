"""Selected-root reconciliation from fresh authenticated native readback only."""

from __future__ import annotations

import hashlib

from review_sensei.active_finding_window import reconcile_uncertain_slot
from review_sensei.errors import ReviewInputError
from review_sensei.finding_drift import reconcile_uncertain_drift_thread

from .finding_lifecycle_consumers import finding_lifecycle_consumer
from .publication import NativeFindingReceipt


def reconcile_finding_lifecycle(
    reviewer,
    ledger,
    identity,
    *,
    mode,
    target,
    resolved_state,
    proof_sha256,
    token,
    app_slug,
    confirmed_target=None,
):
    """Authenticate a retained native receipt, then durably reconcile its slot.

    This is not a missing-create recovery protocol: without an authenticated
    retained receipt the original publication journal must reconcile its intent.
    Host resolution alone never supplies positive finding-resolution evidence.
    """
    if mode not in {"window", "drift"} or resolved_state not in {"active", "released"}:
        raise ReviewInputError(
            "finding lifecycle reconciliation mode/state unsupported"
        )
    if (
        not isinstance(token, str)
        or not token
        or not isinstance(app_slug, str)
        or not app_slug
    ):
        raise ReviewInputError(
            "finding lifecycle reconciliation requires readback identity"
        )
    context = finding_lifecycle_consumer(
        reviewer,
        ledger,
        identity,
        head_sha="",
        app_slug=app_slug,
        review_token=token,
    )
    context.restore_only()
    if not context.enrolled:
        raise ReviewInputError("durable_finding_inventory_recovery_required")
    record = context.record
    binding = record.finding_lifecycle["manifest"]["binding"]
    context.head_sha = binding["head_sha"]
    key = (identity.repository_id, identity.pull_request)
    states = (
        reviewer._active_window_states if mode == "window" else reviewer._drift_states
    )
    state = states.get(key)
    if state is None:
        raise ReviewInputError("finding lifecycle reconciliation mode conflicts")
    destination = target if confirmed_target is None else confirmed_target
    records = [
        r
        for r in reviewer.native_finding_receipts(
            repository_id=identity.repository_id,
            pull_request=identity.pull_request,
        )
        if r["thread_id"] == destination
    ]
    if not records or destination != target:
        raise ReviewInputError(
            "finding lifecycle original publication replay required: no retained native target"
        )
    obligations = (
        state.authoritative_decision_inventory.obligations
        if mode == "window"
        else state.inventory.obligations
    )
    matched = [
        ob
        for ob in obligations
        if (
            ob.native_target == target
            if mode == "window"
            else ob.thread_binding is not None and ob.thread_binding.thread_id == target
        )
    ]
    if len(matched) != 1:
        raise ReviewInputError("finding lifecycle native target obligation conflicts")
    if resolved_state == "released" and matched[0].unresolved:
        raise ReviewInputError(
            "finding lifecycle host resolution is not positive resolution evidence"
        )
    # Reuse the caller's existing shared evidence allowance; do not introduce an
    # uncharged finalizer census or a fresh budget to evade a depleted one.
    previous_budget = reviewer.finalizer.evidence_budget
    budget = getattr(ledger, "evidence_budget", None)
    if budget is None:
        budget = getattr(ledger, "_operation_budget", None)
    if budget is None:
        raise ReviewInputError(
            "finding lifecycle reconciliation requires original read budget"
        )
    reviewer.finalizer.evidence_budget = budget
    try:
        reviewer._published_finding_suppression(
            token=token,
            repository=identity.repository,
            repository_id=identity.repository_id,
            pull_request=identity.pull_request,
            head_sha=binding["head_sha"],
            base_sha=binding["base_sha"],
            app_slug=app_slug,
        )
    finally:
        reviewer.finalizer.evidence_budget = previous_budget
    nodes = [
        n
        for n in getattr(reviewer, "_native_binding_nodes", ())
        if n.get("id") == target
    ]
    if len(nodes) != 1 or nodes[0].get("isResolved") is not (
        resolved_state == "released"
    ):
        raise ReviewInputError("finding lifecycle native target readback conflicts")
    comments = nodes[0].get("comments", {}).get("nodes")
    if (
        not isinstance(comments, list)
        or not comments
        or not isinstance(comments[0], dict)
    ):
        raise ReviewInputError("finding lifecycle native root readback missing")
    root = comments[0]
    author = root.get("author")
    matching = [
        r
        for r in records
        if (
            root.get("databaseId") == r["comment_id"]
            and isinstance(root.get("body"), str)
            and hashlib.sha256(root["body"].encode()).hexdigest() == r["body_sha256"]
            and root.get("edited_by_human") is not True
            and root.get("pullRequestReview") == {"databaseId": r["review_id"]}
            and root.get("commit") == {"oid": r["native_head_sha"]}
            and isinstance(author, dict)
            and author.get("databaseId") == r["producer_id"]
            and author.get("login") == app_slug
            and author.get("__typename") == "Bot"
        )
    ]
    if len(matching) != 1:
        raise ReviewInputError("finding lifecycle native receipt readback conflicts")
    receipt = NativeFindingReceipt(
        **{k: v for k, v in matching[0].items() if k != "schema_version"}
    )
    if proof_sha256 != receipt.proof_sha256:
        raise ReviewInputError("finding lifecycle native receipt proof conflicts")
    with context:
        if context.record.record_sha256 != record.record_sha256:
            raise ReviewInputError(
                "finding lifecycle reconciliation selected root changed"
            )
        state = states[key]
        if mode == "window":
            slots = [
                s for s in state.display_active_findings.slots if s.target == target
            ]
            slot_state = slots[0].state if len(slots) == 1 else None
        else:
            slot_state = matched[0].thread_binding.slot_state
        if slot_state == resolved_state:
            return state  # Exact authenticated replay, no new inference or effects.
        if mode == "window":
            updated = reconcile_uncertain_slot(
                state,
                target=target,
                resolved_state=resolved_state,
                proof_sha256=receipt.proof_sha256,
            )
        else:
            updated_drift = reconcile_uncertain_drift_thread(
                state,
                thread_id=target,
                resolved_state=resolved_state,
                proof_sha256=receipt.proof_sha256,
            )
            states[key] = updated_drift
            return updated_drift
        states[key] = updated
        return updated
