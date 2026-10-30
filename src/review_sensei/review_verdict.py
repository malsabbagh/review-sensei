"""Bounded current-review gates for required findings outside publication selection.

Selected findings retain their own resolution/override inventory. This document
contains no candidate identities, prose, counts, or future publication queue.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Mapping

from .errors import ReviewInputError
from .validation import DEFAULT_REVIEW_LIMITS

if TYPE_CHECKING:
    from .convergence import ReviewConvergencePolicy
    from .models import ReviewComment, ReviewResult


@dataclass(frozen=True)
class ReviewVerdict:
    policy_digest: str
    selected_comments_sha256: str
    unselected_blocking_defect: bool = False
    unselected_human_decision: bool = False
    selected_admission: str = ""

    def __post_init__(self) -> None:
        if (
            not isinstance(self.policy_digest, str)
            or re.fullmatch(r"[a-f0-9]{64}", self.policy_digest) is None
            or not isinstance(self.selected_comments_sha256, str)
            or re.fullmatch(r"[a-f0-9]{64}", self.selected_comments_sha256) is None
            or type(self.unselected_blocking_defect) is not bool
            or type(self.unselected_human_decision) is not bool
            or not isinstance(self.selected_admission, str)
            or len(self.selected_admission) > DEFAULT_REVIEW_LIMITS.max_comments
            or re.fullmatch(r"[ABH]*", self.selected_admission) is None
        ):
            raise ReviewInputError("review verdict is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "policy_digest": self.policy_digest,
            "selected_comments_sha256": self.selected_comments_sha256,
            "selected_admission": self.selected_admission,
            "unselected_blocking_defect": self.unselected_blocking_defect,
            "unselected_human_decision": self.unselected_human_decision,
        }

    @classmethod
    def from_dict(cls, value: object) -> ReviewVerdict:
        if (
            not isinstance(value, Mapping)
            or set(value)
            != {
                "schema_version",
                "policy_digest",
                "selected_comments_sha256",
                "selected_admission",
                "unselected_blocking_defect",
                "unselected_human_decision",
            }
            or value.get("schema_version") != "1.0"
        ):
            raise ReviewInputError("review verdict must contain the documented fields")
        return cls(
            policy_digest=value["policy_digest"],
            selected_comments_sha256=value["selected_comments_sha256"],
            unselected_blocking_defect=value["unselected_blocking_defect"],
            unselected_human_decision=value["unselected_human_decision"],
            selected_admission=value["selected_admission"],
        )

    def with_verified_comments(
        self, comments: tuple[ReviewComment, ...]
    ) -> ReviewVerdict:
        """Rebind after evidence verification, preserving excluded requirements."""
        return replace(
            self,
            selected_comments_sha256=comment_selection_digest(comments),
            selected_admission=comment_admission(comments),
        )

    def restore_comments(
        self, comments: tuple[ReviewComment, ...]
    ) -> tuple[ReviewComment, ...]:
        """Restore selected admission from the content-bound host verdict."""
        if (
            len(comments) != len(self.selected_admission)
            or comment_selection_digest(comments) != self.selected_comments_sha256
        ):
            raise ReviewInputError("review verdict selected comments conflict")
        return tuple(
            replace(comment, effective_blocking=state == "B", needs_human=state == "H")
            for comment, state in zip(comments, self.selected_admission)
        )

    def require_policy(self, policy: ReviewConvergencePolicy) -> None:
        if (
            policy.quality != "high-signal"
            or policy.enforcement != "publication"
            or policy.digest() != self.policy_digest
        ):
            raise ReviewInputError("review verdict policy conflicts")


def comment_selection_digest(comments: tuple[ReviewComment, ...]) -> str:
    from .models import ReviewComment

    if not isinstance(comments, tuple) or any(
        not isinstance(comment, ReviewComment) for comment in comments
    ):
        raise ReviewInputError("review verdict comments are invalid")
    # Match the existing selected-comment JSON digest, including ASCII escaping.
    # ReviewComment validates its serialized fields before trusted admission.
    payload = json.dumps(
        [comment.to_dict() for comment in comments],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def comment_admission(comments: tuple[ReviewComment, ...]) -> str:
    """One bounded classification byte per selected comment, in bound order."""
    if any(
        type(comment.effective_blocking) is not bool
        or type(comment.needs_human) is not bool
        or (comment.effective_blocking and comment.needs_human)
        for comment in comments
    ):
        raise ReviewInputError("review verdict requires trusted finding admission")
    return "".join(
        "B" if comment.effective_blocking else "H" if comment.needs_human else "A"
        for comment in comments
    )


def bind_review_verdict(
    result: ReviewResult, *, policy: ReviewConvergencePolicy
) -> ReviewResult:
    """Freeze trusted verdict provenance before accepted-result retention."""
    if result.review_verdict is not None:
        result.review_verdict.require_policy(policy)
    if policy.quality != "high-signal" or policy.enforcement != "publication":
        return result
    return project_review_comments(result, result.comments, policy=policy)


def project_review_comments(
    result: ReviewResult,
    selected: tuple[ReviewComment, ...],
    *,
    policy: ReviewConvergencePolicy,
) -> ReviewResult:
    """Project already-admitted candidates before freezing accepted result bytes.

    Classification comes only from trusted admission, never model severity.
    Reprojection/replay cannot clear a positive current-review verdict. A new
    qualifying review starts from a new result, independently of prior flags.
    """
    from .convergence import ReviewConvergencePolicy
    from .human_assessment import finding_instance_fingerprint
    from .models import ReviewComment, ReviewResult

    if (
        not isinstance(result, ReviewResult)
        or not isinstance(policy, ReviewConvergencePolicy)
        or not isinstance(selected, tuple)
    ):
        raise ReviewInputError("review verdict projection is invalid")
    if policy.quality != "high-signal" or policy.enforcement != "publication":
        raise ReviewInputError(
            "review verdict projection requires trusted high-signal publication"
        )
    prior = result.review_verdict or ReviewVerdict(
        policy.digest(), comment_selection_digest(result.comments)
    )
    prior.require_policy(policy)
    originals: dict[str, ReviewComment] = {}
    for comment in result.comments:
        if (
            type(comment.effective_blocking) is not bool
            or type(comment.needs_human) is not bool
            or (comment.effective_blocking and comment.needs_human)
        ):
            raise ReviewInputError("review verdict requires trusted finding admission")
        instance = finding_instance_fingerprint(comment)
        if instance in originals and originals[instance] != comment:
            raise ReviewInputError("review verdict has conflicting finding admission")
        originals[instance] = comment
    selected_instances = set()
    for comment in selected:
        if not isinstance(comment, ReviewComment):
            raise ReviewInputError("review verdict selected finding is invalid")
        instance = finding_instance_fingerprint(comment)
        if originals.get(instance) != comment:
            raise ReviewInputError(
                "review verdict selection changed an admitted finding"
            )
        selected_instances.add(instance)
    omitted = tuple(
        comment
        for instance, comment in originals.items()
        if instance not in selected_instances
    )
    from .context import finding_lifecycle_for_comment

    selected_concerns = {
        finding_lifecycle_for_comment(comment).fingerprint for comment in selected
    }
    omitted_concerns = {
        finding_lifecycle_for_comment(comment).fingerprint for comment in omitted
    } - selected_concerns
    if any(
        record.fingerprint in omitted_concerns and record.state != "new"
        for record in result.finding_lifecycles
    ):
        raise ReviewInputError(
            "review verdict selection cannot remove a retained obligation"
        )
    lifecycles = tuple(
        record
        for record in result.finding_lifecycles
        if record.fingerprint not in omitted_concerns
    )
    verdict = ReviewVerdict(
        policy_digest=policy.digest(),
        selected_comments_sha256=comment_selection_digest(selected),
        selected_admission=comment_admission(selected),
        unselected_blocking_defect=prior.unselected_blocking_defect
        or any(comment.effective_blocking is True for comment in omitted),
        unselected_human_decision=prior.unselected_human_decision
        or any(comment.needs_human is True for comment in omitted),
    )
    return replace(
        result, comments=selected, finding_lifecycles=lifecycles, review_verdict=verdict
    )


def preserve_verified_verdict(
    previous: ReviewResult, verified: ReviewResult, *, policy: ReviewConvergencePolicy
) -> ReviewResult:
    """Rebind verified facts without losing a selected retained obligation."""
    from .context import finding_lifecycle_for_comment
    from .human_assessment import finding_instance_fingerprint

    prior = previous.review_verdict
    if prior is None:
        return verified
    prior.require_policy(policy)
    required = {
        finding_instance_fingerprint(comment): comment
        for comment in previous.comments
        if comment.effective_blocking
        or comment.needs_human
        or any(
            record.fingerprint == finding_lifecycle_for_comment(comment).fingerprint
            and record.state != "new"
            for record in previous.finding_lifecycles
        )
    }
    current = {
        finding_instance_fingerprint(comment): comment for comment in verified.comments
    }
    if any(
        instance not in current or current[instance].to_dict() != comment.to_dict()
        for instance, comment in required.items()
    ):
        raise ReviewInputError(
            "verified selection cannot remove or change a retained obligation"
        )
    comments = tuple(
        replace(
            comment,
            effective_blocking=required[instance].effective_blocking,
            needs_human=required[instance].needs_human,
        )
        if (instance := finding_instance_fingerprint(comment)) in required
        else comment
        for comment in verified.comments
    )
    # Confirmed verification can remove only optional new observations. Retain
    # all prior lifecycle obligations and both excluded current-review gates.
    optional_removed = {
        finding_lifecycle_for_comment(comment).fingerprint
        for comment in previous.comments
        if finding_instance_fingerprint(comment) not in current
    }
    prior_lifecycles = tuple(
        record
        for record in previous.finding_lifecycles
        if record.fingerprint not in optional_removed
    )
    previous_fingerprints = {
        record.fingerprint for record in previous.finding_lifecycles
    }
    added_lifecycles = tuple(
        record
        for record in verified.finding_lifecycles
        if record.fingerprint not in previous_fingerprints
    )
    return replace(
        verified,
        comments=comments,
        review_verdict=prior.with_verified_comments(comments),
        finding_lifecycles=prior_lifecycles + added_lifecycles,
    )
