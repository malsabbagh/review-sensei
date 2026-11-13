"""Read-only whole-publication sizing, never native write authority.

The journal remains the only intent/receipt writer. These private shadows
price every success prefix and its uncertain/rejected stop without changing
selected state, receipts, original accounting, or pending effects.
"""

from __future__ import annotations

import copy
from typing import Any

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError


def _capacity_request_identity(method, path, body):
    """Bound the real body producer's compact size, never a runtime intent."""
    from .finding_lifecycle import _request_identity
    from .publication import MAX_PUBLISHED_REVIEW_BODY_BYTES

    request = _request_identity(body)
    # A check warning may cross a decimal-width boundary after initial framing.
    # The actual writer enforces this cap; hashes and status shape stay exact.
    if (
        method == "POST"
        and path.endswith("/reviews")
        and isinstance(request, dict)
        and isinstance(request.get("body"), dict)
    ):
        request["body"]["utf8_bytes"] = MAX_PUBLISHED_REVIEW_BODY_BYTES
    return request


def price_prefixes(journal, requests, *, native_calls):
    """Build complete durable envelopes from an authenticated producer plan.

    This low-level pure projection is intentionally not an admission API. The
    ordinary publisher must independently bind the request plan and read tail
    to its selected current source before consuming the returned documents.
    """
    from .finding_lifecycle import (
        MAX_EFFECTS,
        _digest,
        _pack_journal,
        _require_effect_scope,
    )

    if (
        not isinstance(requests, tuple)
        or not requests
        or (len(journal.effects) + len(requests) > MAX_EFFECTS)
    ):
        raise ReviewInputError("ordinary publication effect envelope is unsupported")
    if type(native_calls) is not int or native_calls < len(requests):
        raise ReviewInputError("ordinary publication native tail is invalid")
    if any(effect["status"] == "pending" for effect in journal.effects):
        raise ReviewInputError("ordinary publication has an uncertain prior effect")
    shadow = copy.copy(journal)
    shadow.effects = copy.deepcopy(journal.effects)
    documents: list[dict[str, Any]] = []
    stops: list[dict[str, Any]] = []
    for method, path, body, precondition in requests:
        request = _capacity_request_identity(method, path, body)
        effect = {
            "method": method,
            "path": path,
            "request": request,
            "request_sha256": _digest(
                {"method": method, "path": path, "request": request}
            ),
            "status": "pending",
            "response": None,
        }
        if precondition is not None:
            effect["check_update_precondition"] = precondition
        matches = [
            previous
            for previous in shadow.effects
            if previous["request_sha256"] == effect["request_sha256"]
        ]
        if matches:
            if len(matches) != 1 or any(
                canonical_bytes(matches[0][field]) != canonical_bytes(effect[field])
                for field in ("method", "path", "request")
            ):
                raise ReviewInputError("ordinary publication replay identity conflicts")
            # Capacity only: retain the authenticated effect and its receipt.
            # Runtime still independently reads mutable native state before use.
            # Keep both checkpoint slots and the original native/read quote;
            # replay does not grant a discount or a cached response capability.
            _require_effect_scope(
                shadow._document(), journal.identity, journal.head_sha
            )
            retained = _pack_journal(shadow._document())
            documents.extend((retained, retained))
            stops.append(retained)
            continue
        shadow.effects.append(effect)
        _require_effect_scope(shadow._document(), journal.identity, journal.head_sha)
        pending, future, confirmed = shadow._preflight_effect(capacity_only=True)
        # The all-stored-loss branch must durably record authenticated
        # readback before resuming its producer or entering the next effect.
        # Normal success may coalesce receipts into the next intent; it does
        # not erase the independently reachable readback checkpoint liability.
        documents.extend((pending, future))
        stops.extend((pending, future))
        shadow.effects = confirmed
    # Any stop can require the last receipt checkpoint. Size its complete
    # envelope, not merely the last happy-path result or current visible count.
    documents.append(max(stops, key=lambda value: len(canonical_bytes(value))))
    return tuple(documents), len(shadow.effects) + 1


def request_branches(
    publisher,
    journal,
    *,
    frame,
    comments,
    outcome,
    failure_outcomes,
    approval_body,
    review_event="COMMENT",
):
    """Closed ordinary producer shapes; future numeric identities are sizing only."""
    from .checks import ReviewCheckPublisher
    from .publication import ReviewPublisher

    if (
        type(publisher) is not ReviewPublisher
        or journal.reviewer is not publisher
        or type(publisher.checks) is not ReviewCheckPublisher
        or getattr(publisher.checks._mutation_body, "__func__", None)
        is not ReviewCheckPublisher._mutation_body
        or publisher.checks._state_fields is not ReviewCheckPublisher._state_fields
    ):
        raise ReviewInputError("ordinary publication request producer is unqualified")
    if review_event not in {"COMMENT", "REQUEST_CHANGES"}:
        raise ReviewInputError("ordinary publication review event is unqualified")
    repository = journal.identity.repository
    check_path = f"/repos/{repository}/check-runs"
    review_path = f"/repos/{repository}/pulls/{journal.identity.pull_request}/reviews"
    maximum_id = 9_223_372_036_854_775_807
    precondition = {
        "check_run_id": maximum_id,
        "head_sha": journal.head_sha,
        "app_id": maximum_id,
        "app_slug": journal.app_slug.removesuffix("[bot]"),
    }

    def check(outcome, *, create):
        fields = publisher.checks._state_fields(outcome)
        body = publisher.checks._mutation_body(
            repository=repository, head_sha=journal.head_sha, create=create, **fields
        )
        return (
            "POST" if create else "PATCH",
            check_path if create else f"{check_path}/{maximum_id}",
            body,
            None if create else precondition,
        )

    # New/previous stable checks share the exact source body producer. The
    # planner does not infer the unknown first lookup's native ID or ownership.
    complete = check(outcome, create=False)
    failed = tuple(
        check(failure_outcome, create=create)
        for failure_outcome in failure_outcomes
        for create in (True, False)
    )
    review = (
        "POST",
        review_path,
        {
            "body": frame,
            "event": review_event,
            "commit_id": journal.head_sha,
            "comments": list(comments),
        },
        None,
    )
    approve = (
        (
            "POST",
            review_path,
            {"body": approval_body, "event": "APPROVE", "commit_id": journal.head_sha},
            None,
        )
        if approval_body is not None
        else None
    )
    branches = []
    for create in (True, False):
        initial = (check(None, create=create), complete, review)
        branches.append(initial + ((approve,) if approve is not None else ()))
        # A definitively failed publication can complete its non-passing check.
        # Unknown native effects stop with their pending receipt, never retry.
        for count in range(1, len(initial) + 1):
            branches.extend(initial[:count] + (failure,) for failure in failed)
    return tuple(branches)


def admit_prefix_branches(journal, branches, *, read_calls):
    """Consume a source-qualified native read quote and bound all durable stops.

    Caller owns source qualification. No branch is executed here; only the
    original ledger/budget can admit the complete known publication envelope.
    """
    from review_sensei.finding_lifecycle_store import preflight_finding_lifecycle

    if (
        not isinstance(branches, tuple)
        or not branches
        or type(read_calls) is not int
        or read_calls < 0
    ):
        raise ReviewInputError("ordinary publication source plan is invalid")
    prepared = [
        price_prefixes(journal, branch, native_calls=read_calls + len(branch))
        for branch in branches
    ]
    # These are closed maximum-native-allocation envelopes from the actual
    # journal producer. At each checkpoint select the largest reachable prefix,
    # including the branch's final uncertain/rejected receipt envelope.
    documents = []
    for index in range(max(len(documents) for documents, _ in prepared)):
        candidates = [
            documents[min(index, len(documents) - 1)] for documents, _ in prepared
        ]
        documents.append(max(candidates, key=lambda value: len(canonical_bytes(value))))
    native_calls = read_calls + max(len(branch) for branch in branches)
    return preflight_finding_lifecycle(
        journal.ledger,
        journal._record,
        document=documents[0],
        future_documents=tuple(documents[1:]),
        operation_id=journal.operation_id,
        base_sha=journal.base_sha,
        head_sha=journal.head_sha,
        policy_digest=journal.policy_digest,
        configuration_digest=journal.configuration_digest,
        item_count=max(count for _, count in prepared),
        reserved_tail_calls=native_calls + 6,
    )


def current_request_branches(
    publisher,
    journal,
    *,
    result,
    assembled,
    eligibility,
    marker,
    outcome,
    policy,
    complete_inventory,
    preserve_explanations=False,
    window_non_inline_blocking=False,
):
    """Pure shared framing from the actual source producers; no admission."""
    from dataclasses import replace

    from .approval import evaluate_approval_facts
    from .checks import review_check_outcome_for_publication_failure
    from .publication import (
        _approval_body,
        _disposition_frame_reserve,
        _framed_review_body,
        _published_summary_byte_limit,
        approval_eligibility_marker,
        approval_marker,
        fit_assembled_review,
        review_result_digest,
    )

    pending_eligibility = replace(
        eligibility,
        facts=replace(
            eligibility.facts,
            check_published=False,
            has_blocking_findings=(
                any(
                    entry.blocking and entry.body_placed
                    for entry in assembled.index_entries
                )
                or window_non_inline_blocking
            ),
        ),
    )
    summary, index = fit_assembled_review(
        assembled,
        review_marker=marker,
        eligibility_marker=approval_eligibility_marker(pending_eligibility),
        repository_id=journal.identity.repository_id,
        pull_request=journal.identity.pull_request,
        head_sha=journal.head_sha,
        base_sha=journal.base_sha,
        result_digest=review_result_digest(result),
        summary_limit=_published_summary_byte_limit(result),
        complete_inventory=complete_inventory,
        reserved_bytes=_disposition_frame_reserve(eligibility, assembled.index_entries),
        preserve_explanations=preserve_explanations,
    )
    frame = _framed_review_body(
        summary,
        review_marker=marker,
        index_marker=index,
        eligibility_marker=approval_eligibility_marker(pending_eligibility),
    )
    decision = evaluate_approval_facts(
        replace(eligibility.facts, has_open_review_threads=False, check_published=True)
    )
    approval = None
    if decision.approved:
        approval = _approval_body(
            approval_marker(
                repository_id=journal.identity.repository_id,
                pull_request=journal.identity.pull_request,
                head_sha=journal.head_sha,
                base_sha=journal.base_sha,
            ),
            eligibility,
            decision,
        )
    event, _ = publisher._ordinary_review_event(
        approval_enabled=eligibility.facts.enabled,
        requests_changes=assembled.requests_changes,
        retained_blocking=False,
        app_authored=False,
    )
    return request_branches(
        publisher,
        journal,
        frame=frame,
        comments=tuple(
            publisher._inline_comment_payload(comment, body, anchor)
            for comment, _fingerprint, body, anchor in assembled.inline_comments
        ),
        review_event=event,
        outcome=outcome,
        failure_outcomes=tuple(
            review_check_outcome_for_publication_failure(
                policy=policy, transient=temporary
            )
            for temporary in (False, True)
        ),
        approval_body=approval,
    )


def admit_current(
    publisher,
    observer,
    journal,
    *,
    original_result,
    result,
    assembled,
    eligibility,
    marker,
    outcome,
    policy,
    complete_inventory,
    checks_enabled,
    grouped,
    planned_window=None,
    planned_drift=None,
    convergence_policy=None,
    auto_approval=False,
    preserve_explanations=False,
    window_non_inline_blocking=False,
):
    """The closed ordinary source route, before its first business effect."""
    if convergence_policy is not None:
        assembled = publisher._adjust_assembled_for_drift(
            publisher._adjust_assembled_for_window(
                assembled,
                planned_window,
                convergence_policy=convergence_policy,
                auto_approval=auto_approval,
            ),
            planned_drift,
            convergence_policy=convergence_policy,
            auto_approval=auto_approval,
        )
    witness = observer.qualify(
        record=journal._record,
        result=original_result,
        projected_result=result,
        assembled=assembled,
        eligibility=eligibility,
        checks_enabled=checks_enabled,
        cleanup_targets=(),
        grouped=grouped,
    )
    if witness is None:
        return None  # This route is not admitted by the closed-source contract.
    witness.require(journal, publisher)
    branches = current_request_branches(
        publisher,
        journal,
        result=result,
        assembled=assembled,
        eligibility=eligibility,
        marker=marker,
        outcome=outcome,
        policy=policy,
        complete_inventory=complete_inventory,
        preserve_explanations=preserve_explanations,
        window_non_inline_blocking=window_non_inline_blocking,
    )
    plan = admit_prefix_branches(journal, branches, read_calls=witness.read_calls)
    witness.require(journal, publisher)
    return plan
