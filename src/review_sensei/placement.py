"""Host-fact placement planning for published findings.

Placement is decided from the host's own facts — whether the target branch
enforces conversation resolution — rather than from a finding's label alone.
A non-blocking label cannot override GitHub's conversation-resolution rule, so
non-enforced feedback goes to the review body. Unknown host facts fail closed
to body placement, which never creates an unresolvable thread, and an
unanchored required finding keeps its required-fix effect from the body.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .convergence import ReviewConvergencePolicy
from .models import ReviewComment, ReviewInputError

CONVERSATION_RESOLUTION_REQUIRED = "required"
CONVERSATION_RESOLUTION_NOT_REQUIRED = "not-required"
CONVERSATION_RESOLUTION_UNKNOWN = "unknown"
CONVERSATION_RESOLUTION_STATES = frozenset(
    (
        CONVERSATION_RESOLUTION_REQUIRED,
        CONVERSATION_RESOLUTION_NOT_REQUIRED,
        CONVERSATION_RESOLUTION_UNKNOWN,
    )
)

INLINE_PLACEMENT = "inline"
BODY_PLACEMENT = "body"

ANCHOR_LOCATIONS = frozenset(("left", "right", "file", "summary"))

# Why a finding is explained in the body instead of an inline thread. The
# published summary explains placement once, so the reason must be known
# without re-deriving the planner's rule order.
BODY_REASON_ADVISORY = "advisory"
BODY_REASON_UNANCHORED = "unanchored"
BODY_REASON_HUMAN_ASSESSMENT = "human-assessment"
BODY_REASON_OPTIONAL_POLICY = "optional-policy"
BODY_REASON_CONVERSATION_RESOLUTION = "conversation-resolution"

BODY_PLACEMENT_NOTE = (
    "Findings that cannot be enforced as inline threads are explained in this "
    "review body; GitHub conversation resolution does not apply to them."
)
ADVISORY_PLACEMENT_NOTE = (
    "Advisory mode does not open inline threads, so every finding is explained "
    "in this review body; GitHub conversation resolution does not apply to them."
)
CONVERSATION_RESOLUTION_PLACEMENT_NOTE = (
    "Optional feedback is explained in this review body because this branch "
    "requires conversation resolution, so an inline thread would have to be "
    "resolved before the pull request can merge."
)


@dataclass(frozen=True)
class HostPlacementFacts:
    """Placement-relevant host facts read through the adapter.

    ``conversation_resolution`` is ``unknown`` whenever the requirement could
    not be read or parsed; unknown facts never permit optional inline threads.
    """

    conversation_resolution: str = CONVERSATION_RESOLUTION_UNKNOWN

    def __post_init__(self) -> None:
        if self.conversation_resolution not in CONVERSATION_RESOLUTION_STATES:
            raise ReviewInputError("conversation resolution state is invalid")

    @property
    def optional_threads_permitted(self) -> bool:
        """Return whether optional inline threads cannot become merge blockers."""

        return self.conversation_resolution == CONVERSATION_RESOLUTION_NOT_REQUIRED


def plan_finding_placement(
    comment: ReviewComment,
    *,
    anchor: str,
    facts: HostPlacementFacts,
    policy: ReviewConvergencePolicy,
    advisory: bool,
) -> str:
    """Return the placement for one finding given host facts and policy."""

    return plan_finding_placement_detail(
        comment, anchor=anchor, facts=facts, policy=policy, advisory=advisory
    )[0]


def plan_finding_placement_detail(
    comment: ReviewComment,
    *,
    anchor: str,
    facts: HostPlacementFacts,
    policy: ReviewConvergencePolicy,
    advisory: bool,
) -> tuple[str, str | None]:
    """Return the placement and, for body placement, the deciding rule."""

    if not isinstance(comment, ReviewComment):
        raise ReviewInputError("placement requires a review comment")
    if anchor not in ANCHOR_LOCATIONS:
        raise ReviewInputError("placement anchor is invalid")
    if not isinstance(facts, HostPlacementFacts):
        raise ReviewInputError("placement requires host facts")
    if not isinstance(policy, ReviewConvergencePolicy):
        raise ReviewInputError("placement requires a convergence policy")
    if not isinstance(advisory, bool):
        raise ReviewInputError("placement advisory flag is invalid")
    if advisory:
        # Advisory mode imposes no thread and no gate; its findings are body
        # content even when a valid inline location exists.
        return BODY_PLACEMENT, BODY_REASON_ADVISORY
    if anchor not in ("left", "right"):
        # No publishable inline location: the body retains a required fix's
        # gate effect, and location fallback alone does not make the analysis
        # incomplete.
        return BODY_PLACEMENT, BODY_REASON_UNANCHORED
    if comment.needs_human:
        # Unresolved uncertainty is labeled for human assessment rather than
        # asserted as a proven defect, so it never becomes an enforcement
        # thread.
        return BODY_PLACEMENT, BODY_REASON_HUMAN_ASSESSMENT
    if comment.blocks_approval:
        return INLINE_PLACEMENT, None
    if not policy.inline_advisory_threads:
        return BODY_PLACEMENT, BODY_REASON_OPTIONAL_POLICY
    if not facts.optional_threads_permitted:
        return BODY_PLACEMENT, BODY_REASON_CONVERSATION_RESOLUTION
    return INLINE_PLACEMENT, None


def placement_note(body_reasons: Sequence[str]) -> str:
    """Explain body placement once, using the rule that decided it."""

    if BODY_REASON_ADVISORY in body_reasons:
        # Advisory mode is a run-level condition: it decides placement for
        # every finding, so it wins over any per-finding rule.
        return ADVISORY_PLACEMENT_NOTE
    if BODY_REASON_CONVERSATION_RESOLUTION in body_reasons:
        return CONVERSATION_RESOLUTION_PLACEMENT_NOTE
    return BODY_PLACEMENT_NOTE


__all__ = [
    "ADVISORY_PLACEMENT_NOTE",
    "ANCHOR_LOCATIONS",
    "BODY_PLACEMENT",
    "BODY_PLACEMENT_NOTE",
    "BODY_REASON_ADVISORY",
    "BODY_REASON_CONVERSATION_RESOLUTION",
    "BODY_REASON_HUMAN_ASSESSMENT",
    "BODY_REASON_OPTIONAL_POLICY",
    "BODY_REASON_UNANCHORED",
    "CONVERSATION_RESOLUTION_NOT_REQUIRED",
    "CONVERSATION_RESOLUTION_PLACEMENT_NOTE",
    "CONVERSATION_RESOLUTION_REQUIRED",
    "CONVERSATION_RESOLUTION_STATES",
    "CONVERSATION_RESOLUTION_UNKNOWN",
    "INLINE_PLACEMENT",
    "HostPlacementFacts",
    "placement_note",
    "plan_finding_placement",
    "plan_finding_placement_detail",
]
