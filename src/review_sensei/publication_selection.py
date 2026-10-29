"""Pure strict-cap selection; authenticated occupancy is supplied by the host.

This module neither discovers GitHub state nor grants mutation authority. The
host must freeze this plan in the accepted operation and revalidate its exact
occupancy before effects. A selected existing target means an in-place update,
never permission to create a replacement comment.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .bounded_evidence import canonical_bytes
from .errors import ReviewInputError
from .human_assessment import finding_instance_fingerprint
from .models import ReviewComment

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
    # A bounded current-review verdict, not a queue of unselected findings.
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
