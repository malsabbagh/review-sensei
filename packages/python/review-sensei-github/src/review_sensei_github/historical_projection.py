"""Render historical finding evidence using its immutable admitted placement.

This projection never selects present-day placement or approval policy. Every
instance, classification and placement comes from the authenticated inventory.
"""

from dataclasses import replace

from review_sensei.context import finding_lifecycle_for_comment
from review_sensei.human_assessment import finding_instance_fingerprint
from review_sensei.presentation import (
    assign_finding_identifiers,
    build_finding_view,
    render_finding,
)

from .publication import (
    AssembledReview,
    BodyExplanation,
    _with_discussion_instruction,
    finding_instance_marker,
    finding_marker,
    parse_finding_index,
)
from .review_completion_recovery import _refuse


def historical_projection(result, store, identity, policy):
    snapshot = store._authenticated_snapshot
    if snapshot is None:
        _refuse("historical admission snapshot is missing")
    document = store._restore(snapshot[1])
    index = parse_finding_index(
        document["finding_index"],
        repository_id=identity.repository_id,
        pull_request=identity.pull_request,
        head_sha=store.context.head_sha,
    )
    if index is None or index.base_sha != store.context.base_sha:
        _refuse("historical admitted placement is unavailable")
    entries = {entry.instance: entry for entry in index.entries}
    identifiers = assign_finding_identifiers(entries)
    comments = {
        finding_instance_fingerprint(comment): comment for comment in result.comments
    }
    states = {item.fingerprint: item.state for item in result.finding_lifecycles}
    inline, body, views = [], [], []
    for entry in index.entries:
        comment = comments.get(entry.instance)
        if comment is None:
            _refuse("historical admitted explanation is missing")
        fingerprint = finding_lifecycle_for_comment(comment).fingerprint
        state = states.get(fingerprint, "new")
        if (
            fingerprint != entry.lifecycle
            or bool(comment.blocks_approval) != entry.blocking
            or bool(comment.needs_human) != entry.needs_human
            or state in {"fixed", "outdated"}
        ):
            _refuse("historical admitted classification conflicts")
        view = replace(
            build_finding_view(
                comment,
                fingerprint=fingerprint,
                identifier=identifiers[entry.instance],
                lifecycle_state=state,
                placement="body" if entry.body_placed else "inline",
                advisory=not policy.automatic_github_review_events,
            ),
            repeat=True,
        )
        views.append(view)
        if entry.body_placed:
            body.append(
                BodyExplanation(
                    entry.instance,
                    comment.severity,
                    bool(entry.blocking or entry.needs_human),
                    view,
                )
            )
        else:
            prose = (
                f"{_with_discussion_instruction(render_finding(view))}\n\n"
                f"{finding_instance_marker(comment)}\n\n"
                f"{finding_marker(repository_id=identity.repository_id, pull_request=identity.pull_request, head_sha=store.context.head_sha, base_sha=store.context.base_sha, result=result, blocking=comment.blocks_approval, fingerprint=fingerprint, state=state, needs_human=comment.needs_human)}"
            )
            inline.append((comment, fingerprint, prose, comment.side.lower()))
    return AssembledReview(
        inline_comments=tuple(inline),
        index_entries=index.entries,
        head_sha=store.context.head_sha,
        state="pending",
        reason=None,
        overview=result.summary,
        coverage_line=None,
        required_views=tuple(views),
        optional_views=(),
        limitations=(),
        placement_note=None,
        body_explanations=tuple(body),
        inline_blocker_instances=tuple(
            entry.instance
            for entry in index.entries
            if entry.blocking and not entry.body_placed
        ),
        requests_changes=any(entry.blocking for entry in index.entries),
    )
