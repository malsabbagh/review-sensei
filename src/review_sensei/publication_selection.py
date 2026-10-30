"""Pure strict-cap selection; authenticated occupancy is supplied by the host.

This module neither discovers GitHub state nor grants mutation authority. The
host must freeze this plan in the accepted operation and revalidate its exact
occupancy before effects. A selected existing target means an in-place update,
never permission to create a replacement comment.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Mapping, cast

from .bounded_evidence import canonical_bytes
from .errors import ReviewInputError
from .human_assessment import finding_instance_fingerprint
from .models import ReviewComment
from .validation import DEFAULT_REVIEW_LIMITS

if TYPE_CHECKING:
    from .convergence import ReviewConvergencePolicy
    from .models import ReviewResult

_DIGEST = re.compile(r"[a-f0-9]{64}")
_STATES = frozenset({"active", "contingent", "uncertain", "released"})
_IMPACT = {"critical": 0, "high": 1, "medium": 2, "low": 3, None: 4}
_EVIDENCE = {
    "causal-source": 0,
    "supported-source": 1,
    "human-judgment": 2,
    "advisory": 3,
}


def _digest(value: object) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


@dataclass(frozen=True)
class PublishedSlot:
    """One authenticated physical target or uncertain creation reservation.

    A confirmed authorized override with confirmed native closure can release
    occupancy while its obligation and expiry remain. Only an outstanding
    automatic reactivation liability is ``contingent``. Later reopening must
    reacquire a slot and reuse the existing target. Absence, timeout, resolved
    UI state alone and provider omission cannot establish release.
    ``target`` is the stable host target/operation identity, not a line number.
    """

    target: str
    state: str
    proof_sha256: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.target, str)
            or not self.target
            or len(self.target.encode()) > 256
            or not self.target.isprintable()
            or not isinstance(self.state, str)
            or self.state not in _STATES
            or not _digest(self.proof_sha256)
        ):
            raise ReviewInputError("published finding slot is invalid")

    @property
    def reserved(self) -> bool:
        return self.state != "released"


@dataclass(frozen=True)
class PublicationCandidate:
    """Trusted admission facts, before the candidate becomes an obligation.

    Impact/evidence come from the completed verifier/admission contract. The
    model's severity label is deliberately not used for priority. Matching an
    existing target requires independently authenticated same-concern facts.
    Required means exactly blocking or human-decision admission; a mismatch is
    rejected before selection rather than reinterpreted as a new requirement.
    """

    comment: ReviewComment
    required: bool
    impact: str | None
    evidence: str
    existing_target: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.comment, ReviewComment)
            or type(self.required) is not bool
            or self.impact not in _IMPACT
            or self.evidence not in _EVIDENCE
            or self.existing_target is not None
            and not isinstance(self.existing_target, str)
            or self.required
            != (self.comment.blocks_approval or self.comment.needs_human)
            or self.comment.effective_blocking is None
            or self.evidence == "advisory"
            and self.required
            or self.impact is not None
            and self.evidence == "advisory"
        ):
            raise ReviewInputError("publication candidate admission is invalid")

    @property
    def instance(self) -> str:
        return finding_instance_fingerprint(self.comment)

    @property
    def priority(self) -> tuple[int, int, int, str]:
        return (
            int(not self.required),
            _IMPACT[self.impact],
            _EVIDENCE[self.evidence],
            self.instance,
        )


@dataclass(frozen=True)
class SelectedFinding:
    candidate: PublicationCandidate
    consumes_slot: bool


@dataclass(frozen=True)
class PublicationSelection:
    max_active_findings: int
    occupancy_sha256: str
    selected: tuple[SelectedFinding, ...]
    # Any required candidate gates the review even when it is not selected.
    # This is deliberately independent of the selected comment inventory.
    required_verdict: bool

    def to_document(self) -> dict[str, object]:
        return {
            "schema_version": "strict-publication-selection-v1",
            "max_active_findings": self.max_active_findings,
            "occupancy_sha256": self.occupancy_sha256,
            "required_verdict": self.required_verdict,
            "selected": [
                {
                    "instance": item.candidate.instance,
                    "existing_target": item.candidate.existing_target,
                    "consumes_slot": item.consumes_slot,
                    "comment_sha256": hashlib.sha256(
                        canonical_bytes(item.candidate.comment.to_dict())
                    ).hexdigest(),
                }
                for item in self.selected
            ],
        }


def select_publication(
    candidates: tuple[PublicationCandidate, ...],
    *,
    slots: tuple[PublishedSlot, ...],
    max_active_findings: int,
) -> PublicationSelection:
    """Select once, reserving possible reopenings and retaining active targets.

    The caller separately certifies the numeric policy and finite lifecycle.
    This pure count proof makes no assertion about request or byte capacity.
    """
    if (
        type(max_active_findings) is not int
        or max_active_findings < 1
        or not isinstance(slots, tuple)
        or any(type(slot) is not PublishedSlot for slot in slots)
        or not isinstance(candidates, tuple)
        or any(type(candidate) is not PublicationCandidate for candidate in candidates)
    ):
        raise ReviewInputError("publication selection inputs are invalid")
    by_target = {slot.target: slot for slot in slots}
    if len(by_target) != len(slots):
        raise ReviewInputError("publication occupancy has duplicate targets")
    reserved = sum(slot.reserved for slot in slots)
    if reserved > max_active_findings:
        raise ReviewInputError(
            "existing publication reservations exceed the strict cap"
        )
    unique: dict[str, PublicationCandidate] = {}
    matched: set[str] = set()
    for candidate in candidates:
        previous = unique.get(candidate.instance)
        if previous is not None:
            if previous != candidate:
                raise ReviewInputError("duplicate candidate has conflicting admission")
            continue
        target = candidate.existing_target
        if target is not None:
            if target not in by_target or target in matched:
                raise ReviewInputError(
                    "publication target match is missing or ambiguous"
                )
            if by_target[target].state == "uncertain":
                raise ReviewInputError(
                    "uncertain publication must reconcile before selection"
                )
            matched.add(target)
        unique[candidate.instance] = candidate
    selected = []
    for candidate in sorted(unique.values(), key=lambda item: item.priority):
        target = candidate.existing_target
        consumes = target is None or not by_target[target].reserved
        if consumes and reserved == max_active_findings:
            continue
        selected.append(SelectedFinding(candidate, consumes))
        reserved += int(consumes)
    occupancy = [
        {"target": slot.target, "state": slot.state, "proof_sha256": slot.proof_sha256}
        for slot in sorted(slots, key=lambda item: item.target)
    ]
    return PublicationSelection(
        max_active_findings=max_active_findings,
        occupancy_sha256=hashlib.sha256(canonical_bytes(occupancy)).hexdigest(),
        selected=tuple(selected),
        required_verdict=any(candidate.required for candidate in unique.values()),
    )


def _occupancy_digest(slots: tuple[PublishedSlot, ...]) -> str:
    if (
        not isinstance(slots, tuple)
        or any(type(slot) is not PublishedSlot for slot in slots)
        or len({slot.target for slot in slots}) != len(slots)
    ):
        raise ReviewInputError("publication occupancy is invalid")
    return hashlib.sha256(
        canonical_bytes(
            [
                {
                    "target": slot.target,
                    "state": slot.state,
                    "proof_sha256": slot.proof_sha256,
                }
                for slot in sorted(slots, key=lambda item: item.target)
            ]
        )
    ).hexdigest()


@dataclass(frozen=True)
class PublicationAction:
    """Inactive target intent. Reopening always needs separate effect authority."""

    instance: str
    comment_sha256: str
    action: str
    consumes_slot: bool
    existing_target: str | None = None
    target_state: str | None = None
    target_proof_sha256: str | None = None

    def __post_init__(self) -> None:
        if (
            not _digest(self.instance)
            or not _digest(self.comment_sha256)
            or type(self.consumes_slot) is not bool
        ):
            raise ReviewInputError("publication action is invalid")
        if self.existing_target is None:
            valid = (
                self.action == "create"
                and self.consumes_slot
                and self.target_state is None
                and self.target_proof_sha256 is None
            )
        else:
            PublishedSlot(
                self.existing_target,
                cast(str, self.target_state),
                cast(str, self.target_proof_sha256),
            )
            expected = {
                "active": ("reuse", False),
                "contingent": ("reopen", False),
                "released": ("reopen", True),
            }.get(cast(str, self.target_state))
            valid = expected == (self.action, self.consumes_slot)
        if not valid:
            raise ReviewInputError("publication action conflicts with target proof")

    def to_document(self) -> dict[str, object]:
        return {
            name: getattr(self, name)
            for name in (
                "instance",
                "comment_sha256",
                "action",
                "consumes_slot",
                "existing_target",
                "target_state",
                "target_proof_sha256",
            )
        }

    @classmethod
    def from_document(cls, value: object) -> PublicationAction:
        fields = {
            "instance",
            "comment_sha256",
            "action",
            "consumes_slot",
            "existing_target",
            "target_state",
            "target_proof_sha256",
        }
        if not isinstance(value, Mapping) or set(value) != fields:
            raise ReviewInputError("publication action document is invalid")
        return cls(**value)


@dataclass(frozen=True)
class PublicationSelectionHandoff:
    """Closed, content-bound selection record; never publication authority.

    Occupancy and admission are caller-supplied trusted facts. Matching their
    digests does not authenticate arbitrary dispositions or a complete census.
    Current publishers reject every record, including create-only selections.
    """

    source_result_sha256: str
    selected_result_sha256: str
    policy_digest: str
    criteria_sha256: str
    occupancy_sha256: str
    max_active_findings: int
    actions: tuple[PublicationAction, ...]

    def __post_init__(self) -> None:
        if (
            not all(
                _digest(value)
                for value in (
                    self.source_result_sha256,
                    self.selected_result_sha256,
                    self.policy_digest,
                    self.criteria_sha256,
                    self.occupancy_sha256,
                )
            )
            or type(self.max_active_findings) is not int
            or self.max_active_findings < 1
            or not isinstance(self.actions, tuple)
            or len(self.actions) > DEFAULT_REVIEW_LIMITS.max_comments
            or any(type(item) is not PublicationAction for item in self.actions)
        ):
            raise ReviewInputError("publication selection handoff is invalid")
        instances = [item.instance for item in self.actions]
        targets = [
            item.existing_target
            for item in self.actions
            if item.existing_target is not None
        ]
        if len(set(instances)) != len(instances) or len(set(targets)) != len(targets):
            raise ReviewInputError("publication selection has duplicate actions")
        if sum(item.consumes_slot for item in self.actions) > self.max_active_findings:
            raise ReviewInputError("publication actions exceed the strict cap")

    def to_document(self) -> dict[str, object]:
        return {
            "schema_version": "inactive-publication-selection-v1",
            "source_result_sha256": self.source_result_sha256,
            "selected_result_sha256": self.selected_result_sha256,
            "policy_digest": self.policy_digest,
            "criteria_sha256": self.criteria_sha256,
            "occupancy_sha256": self.occupancy_sha256,
            "max_active_findings": self.max_active_findings,
            "actions": [item.to_document() for item in self.actions],
        }

    @classmethod
    def from_document(cls, value: object) -> PublicationSelectionHandoff:
        fields = {
            "schema_version",
            "source_result_sha256",
            "selected_result_sha256",
            "policy_digest",
            "criteria_sha256",
            "occupancy_sha256",
            "max_active_findings",
            "actions",
        }
        if (
            not isinstance(value, Mapping)
            or set(value) != fields
            or value["schema_version"] != "inactive-publication-selection-v1"
            or not isinstance(value["actions"], list)
            or len(value["actions"]) > DEFAULT_REVIEW_LIMITS.max_comments
        ):
            raise ReviewInputError("publication selection document is invalid")
        return cls(
            source_result_sha256=value["source_result_sha256"],
            selected_result_sha256=value["selected_result_sha256"],
            policy_digest=value["policy_digest"],
            criteria_sha256=value["criteria_sha256"],
            occupancy_sha256=value["occupancy_sha256"],
            max_active_findings=value["max_active_findings"],
            actions=tuple(
                PublicationAction.from_document(item) for item in value["actions"]
            ),
        )

    def require_occupancy(self, slots: tuple[PublishedSlot, ...]) -> None:
        if _occupancy_digest(slots) != self.occupancy_sha256:
            raise ReviewInputError("publication occupancy changed")
        if (
            sum(slot.reserved for slot in slots)
            + sum(item.consumes_slot for item in self.actions)
            > self.max_active_findings
        ):
            raise ReviewInputError("publication reservations exceed the strict cap")
        by_target = {slot.target: slot for slot in slots}
        for item in self.actions:
            if item.existing_target is not None and by_target.get(
                item.existing_target
            ) != PublishedSlot(
                item.existing_target,
                cast(str, item.target_state),
                cast(str, item.target_proof_sha256),
            ):
                raise ReviewInputError("publication target proof changed")

    def require_result(self, result: ReviewResult) -> None:
        if (
            result.review_verdict is None
            or result.review_verdict.policy_digest != self.policy_digest
            or result._content_digest_without_selection() != self.selected_result_sha256
            or len(result.comments) != len(self.actions)
        ):
            raise ReviewInputError("publication selection result binding changed")
        for comment, action in zip(result.comments, self.actions, strict=True):
            if (
                finding_instance_fingerprint(comment) != action.instance
                or hashlib.sha256(canonical_bytes(comment.to_dict())).hexdigest()
                != action.comment_sha256
            ):
                raise ReviewInputError("publication selection action binding changed")


def freeze_publication_selection(
    source: ReviewResult,
    *,
    candidates: tuple[PublicationCandidate, ...],
    slots: tuple[PublishedSlot, ...],
    max_active_findings: int,
    policy: ReviewConvergencePolicy,
) -> ReviewResult:
    """Freeze an inactive deterministic plan before accepted-result retention.

    No caller, default cap, census, network read or native writer is enabled by
    this seam. The original source and trusted criteria remain necessary for
    independent validation; the document cannot manufacture that provenance.
    """
    from .convergence import ReviewConvergencePolicy
    from .models import ReviewResult
    from .review_verdict import project_review_comments

    if (
        not isinstance(source, ReviewResult)
        or not isinstance(policy, ReviewConvergencePolicy)
        or source.review_verdict is None
        or source.publication_selection is not None
        or source.transaction is not None
        and source.transaction.result_sha256 is not None
    ):
        raise ReviewInputError(
            "publication selection requires an unsealed admitted source"
        )
    source.review_verdict.require_policy(policy)
    plan = select_publication(
        candidates, slots=slots, max_active_findings=max_active_findings
    )
    if sorted(canonical_bytes(row.comment.to_dict()) for row in candidates) != sorted(
        canonical_bytes(comment.to_dict()) for comment in source.comments
    ) or any(row.comment not in source.comments for row in candidates):
        raise ReviewInputError(
            "publication candidates must cover the admitted source exactly"
        )
    selected = project_review_comments(
        source, tuple(item.candidate.comment for item in plan.selected), policy=policy
    )
    by_target = {slot.target: slot for slot in slots}
    actions = []
    for item in plan.selected:
        target = (
            by_target.get(item.candidate.existing_target)
            if item.candidate.existing_target is not None
            else None
        )
        actions.append(
            PublicationAction(
                instance=item.candidate.instance,
                comment_sha256=hashlib.sha256(
                    canonical_bytes(item.candidate.comment.to_dict())
                ).hexdigest(),
                action="create"
                if target is None
                else "reuse"
                if target.state == "active"
                else "reopen",
                consumes_slot=item.consumes_slot,
                existing_target=target.target if target else None,
                target_state=target.state if target else None,
                target_proof_sha256=target.proof_sha256 if target else None,
            )
        )
    handoff = PublicationSelectionHandoff(
        source.content_digest(),
        selected.content_digest(),
        policy.digest(),
        hashlib.sha256(
            canonical_bytes(
                sorted(
                    (
                        {
                            "comment_sha256": hashlib.sha256(
                                canonical_bytes(row.comment.to_dict())
                            ).hexdigest(),
                            "instance": row.instance,
                            "required": row.required,
                            "impact": row.impact,
                            "evidence": row.evidence,
                            "existing_target": row.existing_target,
                        }
                        for row in candidates
                    ),
                    key=lambda row: canonical_bytes(row),
                )
            )
        ).hexdigest(),
        plan.occupancy_sha256,
        max_active_findings,
        tuple(actions),
    )
    handoff.require_occupancy(slots)
    return replace(selected, publication_selection=handoff)


def validate_publication_selection_source(
    source: ReviewResult,
    handoff: PublicationSelectionHandoff,
    *,
    candidates: tuple[PublicationCandidate, ...],
    slots: tuple[PublishedSlot, ...],
    policy: ReviewConvergencePolicy,
) -> None:
    """Compare independent original source/criteria with the frozen intent."""
    if type(handoff) is not PublicationSelectionHandoff:
        raise ReviewInputError("publication selection handoff is invalid")
    expected = freeze_publication_selection(
        source,
        candidates=candidates,
        slots=slots,
        max_active_findings=handoff.max_active_findings,
        policy=policy,
    )
    if expected.publication_selection != handoff:
        raise ReviewInputError("publication selection source or criteria changed")
