"""Offline sequential replay for issue #136 C7 evaluation.

C7 compares the current compatible default (``legacy`` publication) with an
operator policy on frozen synthetic sequences. It does not change the
installed default or emit GitHub events. Shadow mode is observation-only.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from .convergence import (
    DEFAULT_REVIEW_MODE,
    ReviewConvergencePolicy,
    detect_no_progress,
    resolve_review_convergence_policy,
)
from .errors import ReviewInputError
from .schemas import validate_public_document
from .session import (
    InMemorySessionLedger,
    SessionIdentity,
    complete_session_round,
    prepare_session_round,
    session_reservation_id,
)

PUBLIC_SCHEMA_VERSION = "1.0"
# Exact sentinel the observed CLI emits when a GitHub Actions identity is absent.
UNAVAILABLE_EVIDENCE_IDENTITY = "unavailable"
_OBSERVED_PUBLICATION_STATUSES = frozenset(
    {"published", "handoff", "disabled", "already_published"}
)


def identity_is_unavailable(value: str) -> bool:
    """Return whether an evidence identity is the exact unavailable sentinel."""

    return value == UNAVAILABLE_EVIDENCE_IDENTITY


@dataclass(frozen=True)
class SequenceStep:
    """One recorded review/verification attempt. No raw source."""

    head_sha: str
    blocking_identities: tuple[str, ...] = ()
    coverage_complete: bool = True
    independently_approval_eligible: bool = False
    latest_head_reviewed: bool = True
    label: str = "step"
    expected_material_finding_ids: tuple[str, ...] = ()
    expected_non_material_finding_ids: tuple[str, ...] = ()
    fixture_material_finding_ids: tuple[str, ...] = ()
    fixture_unqualified_finding_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.head_sha, str) or not self.head_sha.strip():
            raise ReviewInputError("sequence step head_sha is invalid")
        if any(
            not isinstance(item, str) or not item for item in self.blocking_identities
        ):
            raise ReviewInputError("blocking identity must be a non-empty string")
        for label, values in (
            ("expected material finding", self.expected_material_finding_ids),
            ("expected non-material finding", self.expected_non_material_finding_ids),
            ("fixture material finding", self.fixture_material_finding_ids),
            ("fixture unqualified finding", self.fixture_unqualified_finding_ids),
        ):
            if (
                not isinstance(values, tuple)
                or len(values) > 16
                or any(
                    not isinstance(item, str)
                    or not item.strip()
                    or len(item.encode("utf-8")) > 128
                    for item in values
                )
                or (
                    label != "fixture material finding"
                    and label != "fixture unqualified finding"
                    and len(values) != len(set(values))
                )
            ):
                raise ReviewInputError(f"{label} identities are invalid")
        if set(self.expected_material_finding_ids) & set(
            self.expected_non_material_finding_ids
        ):
            raise ReviewInputError("expected finding labels are contradictory")
        if set(self.fixture_material_finding_ids) & set(
            self.fixture_unqualified_finding_ids
        ):
            raise ReviewInputError("fixture finding labels are contradictory")


@dataclass(frozen=True)
class SequenceStepOutcome:
    label: str
    admit: bool
    handoff: bool
    handoff_reason: str | None
    may_emit_approve: bool
    no_progress: bool
    round_kind: str

    def to_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "admit": self.admit,
            "handoff": self.handoff,
            "handoff_reason": self.handoff_reason,
            "may_emit_approve": self.may_emit_approve,
            "no_progress": self.no_progress,
            "round_kind": self.round_kind,
        }


@dataclass(frozen=True)
class SequenceReport:
    """Metrics for one replayed sequence under one policy."""

    schema_version: str = PUBLIC_SCHEMA_VERSION
    mode: str = "legacy"
    steps: tuple[SequenceStepOutcome, ...] = ()
    completed_initial_reviews: int = 0
    completed_verification_rounds: int = 0
    handoffs: int = 0
    no_progress_events: int = 0
    cap_created_approval: bool = False
    limitations: tuple[str, ...] = (
        "Synthetic sequences are not a claim of zero missed defects.",
        "Default publication remains legacy until an authorized migration.",
    )

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "mode": self.mode,
            "steps": [step.to_dict() for step in self.steps],
            "completed_initial_reviews": self.completed_initial_reviews,
            "completed_verification_rounds": self.completed_verification_rounds,
            "handoffs": self.handoffs,
            "no_progress_events": self.no_progress_events,
            "cap_created_approval": self.cap_created_approval,
            "limitations": list(self.limitations),
        }
        validate_public_document(payload, "convergence-sequence-report")
        return payload


# Observed report types are provider-neutral evidence documents. The runner
# that produces them lives beside the host adapter, so this module stays free
# of host imports and can be imported without that adapter.
@dataclass(frozen=True)
class ObservedSequenceEvent:
    """One actual service-to-publication attempt captured by the F6 harness."""

    label: str
    provider_calls: int
    baseline_loaded: bool
    publication_status: str
    handoff_reason: str | None = None
    approval_events: int = 0

    def __post_init__(self) -> None:
        if (
            not isinstance(self.label, str)
            or not self.label.strip()
            or len(self.label.encode("utf-8")) > 128
        ):
            raise ReviewInputError("observed event label is invalid")
        if not _bounded_count(self.provider_calls, maximum=32):
            raise ReviewInputError("observed event provider calls are invalid")
        if not isinstance(self.baseline_loaded, bool):
            raise ReviewInputError("observed event baseline flag is invalid")
        if self.publication_status not in _OBSERVED_PUBLICATION_STATUSES:
            raise ReviewInputError("observed event publication status is invalid")
        if self.handoff_reason is not None and (
            not isinstance(self.handoff_reason, str)
            or not self.handoff_reason.strip()
            or len(self.handoff_reason.encode("utf-8")) > 128
        ):
            raise ReviewInputError("observed event handoff reason is invalid")
        if not _bounded_count(self.approval_events, maximum=32):
            raise ReviewInputError("observed event approval events are invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "provider_calls": self.provider_calls,
            "baseline_loaded": self.baseline_loaded,
            "publication_status": self.publication_status,
            "handoff_reason": self.handoff_reason,
            "approval_events": self.approval_events,
        }


@dataclass(frozen=True)
class ObservedSequenceReport:
    """Bounded evidence from real internal components and mocked host edges."""

    mode: str
    events: tuple[ObservedSequenceEvent, ...]
    baseline_events: int
    command_events: tuple[str, ...]
    finding_metrics: ObservedFindingMetrics
    execution_metrics: ObservedExecutionMetrics
    shadow_isolated: bool
    evidence_identity: ObservedEvidenceIdentity
    approval_events: int | None
    cap_created_approval: bool | None
    cutover_status: str
    unmet_criteria: tuple[str, ...]
    limitations: tuple[str, ...] = (
        "External model and GitHub APIs are mocked; service, admission, publisher, and finalizer run normally.",
        "A cap-created approval is unknown until a supplied sequence reaches the relevant round cap.",
        "This harness is evidence for deterministic component behavior, not real-world model recall.",
    )

    def __post_init__(self) -> None:
        if (
            not isinstance(self.events, tuple)
            or not 1 <= len(self.events) <= 32
            or any(
                not isinstance(event, ObservedSequenceEvent) for event in self.events
            )
        ):
            raise ReviewInputError("observed report events are invalid")
        if not _bounded_count(self.baseline_events, maximum=32):
            raise ReviewInputError("observed report baseline events are invalid")
        if (
            not isinstance(self.command_events, tuple)
            or not 2 <= len(self.command_events) <= 8
            or any(
                not isinstance(item, str)
                or not item.strip()
                or len(item.encode("utf-8")) > 128
                for item in self.command_events
            )
        ):
            raise ReviewInputError("observed report command events are invalid")
        if self.mode not in {"advisory", "merge-focused", "strict"}:
            raise ReviewInputError("observed report mode is invalid")
        if self.cutover_status not in {"passed", "not_ready"}:
            raise ReviewInputError("observed report cutover status is invalid")
        if not _bounded_count(self.approval_events, maximum=32, allow_none=True):
            raise ReviewInputError("observed report approval events are invalid")
        if self.cap_created_approval is not None and not isinstance(
            self.cap_created_approval, bool
        ):
            raise ReviewInputError("observed report cap approval is invalid")
        if (
            not isinstance(self.unmet_criteria, tuple)
            or len(self.unmet_criteria) > 16
            or any(
                not isinstance(item, str)
                or not item.strip()
                or len(item.encode("utf-8")) > 256
                for item in self.unmet_criteria
            )
        ):
            raise ReviewInputError("observed report unmet criteria are invalid")
        if (
            not isinstance(self.limitations, tuple)
            or not 1 <= len(self.limitations) <= 8
        ):
            raise ReviewInputError("observed report limitations are invalid")
        for item in self.limitations:
            if (
                not isinstance(item, str)
                or not item.strip()
                or len(item.encode("utf-8")) > 256
            ):
                raise ReviewInputError("observed report limitations are invalid")

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": PUBLIC_SCHEMA_VERSION,
            "mode": self.mode,
            "events": [event.to_dict() for event in self.events],
            "baseline_events": self.baseline_events,
            "command_events": list(self.command_events),
            "finding_metrics": self.finding_metrics.to_dict(),
            "execution_metrics": self.execution_metrics.to_dict(),
            "shadow_isolated": self.shadow_isolated,
            "evidence_identity": self.evidence_identity.to_dict(),
            "approval_events": self.approval_events,
            "cap_created_approval": self.cap_created_approval,
            "cutover_status": self.cutover_status,
            "unmet_criteria": list(self.unmet_criteria),
            "limitations": list(self.limitations),
        }
        validate_public_document(payload, "observed-convergence-report")
        return payload


@dataclass(frozen=True)
class ObservedFindingMetrics:
    """Finding-quality counters from labels and the fixture model output."""

    expected_material_findings: int
    observed_material_findings: int
    matched_material_findings: int
    missed_material_findings: int
    unjustified_late_blockers: int
    blocker_precision: float | None
    seeded_material_regressions_detected: int
    duplicate_findings: int = 0
    reopened_findings: int = 0
    contradictions: int = 0

    def __post_init__(self) -> None:
        for label in (
            "expected_material_findings",
            "observed_material_findings",
            "matched_material_findings",
            "missed_material_findings",
            "unjustified_late_blockers",
            "seeded_material_regressions_detected",
            "duplicate_findings",
            "reopened_findings",
            "contradictions",
        ):
            if not _bounded_count(getattr(self, label), maximum=512):
                raise ReviewInputError(f"observed {label.replace('_', ' ')} is invalid")
        precision = self.blocker_precision
        if precision is not None and (
            isinstance(precision, bool)
            or not isinstance(precision, (int, float))
            or not 0 <= precision <= 1
        ):
            raise ReviewInputError("observed blocker precision is invalid")

    def to_dict(self) -> dict[str, int | float | None]:
        return {
            "expected_material_findings": self.expected_material_findings,
            "observed_material_findings": self.observed_material_findings,
            "matched_material_findings": self.matched_material_findings,
            "missed_material_findings": self.missed_material_findings,
            "unjustified_late_blockers": self.unjustified_late_blockers,
            "blocker_precision": self.blocker_precision,
            "seeded_material_regressions_detected": self.seeded_material_regressions_detected,
            "duplicate_findings": self.duplicate_findings,
            "reopened_findings": self.reopened_findings,
            "contradictions": self.contradictions,
        }


@dataclass(frozen=True)
class ObservedExecutionMetrics:
    """Round and work consumption loaded from the durable fixture ledger."""

    completed_rounds: int
    handoffs: int
    provider_calls: int
    failed_attempts: int

    def to_dict(self) -> dict[str, int]:
        return {
            "completed_rounds": self.completed_rounds,
            "handoffs": self.handoffs,
            "provider_calls": self.provider_calls,
            "failed_attempts": self.failed_attempts,
        }


@dataclass(frozen=True)
class ObservedEvidenceIdentity:
    """Bounded identifiers that make an observed run independently auditable."""

    source_identity: str
    package_identity: str
    workflow_identity: str
    configuration_digest: str
    fixture_identity: str
    command: str

    def __post_init__(self) -> None:
        for label, value in (
            ("source identity", self.source_identity),
            ("package identity", self.package_identity),
            ("workflow identity", self.workflow_identity),
            ("configuration digest", self.configuration_digest),
            ("fixture identity", self.fixture_identity),
            ("command", self.command),
        ):
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value.encode("utf-8")) > 256
            ):
                raise ReviewInputError(f"observed {label} is invalid")

    def to_dict(self) -> dict[str, str]:
        return {
            "source_identity": self.source_identity,
            "package_identity": self.package_identity,
            "workflow_identity": self.workflow_identity,
            "configuration_digest": self.configuration_digest,
            "fixture_identity": self.fixture_identity,
            "command": self.command,
        }


def replay_review_sequence(
    steps: tuple[SequenceStep, ...] | list[SequenceStep],
    policy: ReviewConvergencePolicy,
    *,
    repository: str = "owner/repo",
    pull_request: int = 136,
    now: datetime | None = None,
    ledger_factory: Callable[[], InMemorySessionLedger] | None = None,
) -> SequenceReport:
    """Replay bounded review attempts against C1/C5 admission."""

    if not isinstance(policy, ReviewConvergencePolicy):
        raise ReviewInputError("review convergence policy is invalid")
    if ledger_factory is None:
        ledger: InMemorySessionLedger = InMemorySessionLedger()
    else:
        if not callable(ledger_factory):
            raise ReviewInputError("sequence replay ledger factory is invalid")
        ledger = ledger_factory()
        if not isinstance(ledger, InMemorySessionLedger):
            raise ReviewInputError("sequence replay ledger must be in-memory")
    identity = SessionIdentity(repository, pull_request)
    current = now or datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
    outcomes: list[SequenceStepOutcome] = []
    previous: tuple[str, ...] = ()
    earlier: tuple[str, ...] = ()
    handoffs = 0
    no_progress_events = 0
    for index, step in enumerate(steps):
        if not isinstance(step, SequenceStep):
            raise ReviewInputError("sequence step is invalid")
        no_progress = detect_no_progress(
            previous_blocking=previous,
            current_blocking=step.blocking_identities,
            earlier_blocking=earlier,
        )
        reservation = session_reservation_id(
            repository=repository,
            pull_request=pull_request,
            head_sha=step.head_sha,
            kind="round",
        )
        prepared = prepare_session_round(
            ledger,
            identity,
            policy,
            reservation_id=reservation,
            now=current,
            no_progress=no_progress,
            coverage_complete=step.coverage_complete,
            independently_approval_eligible=step.independently_approval_eligible,
            latest_head_reviewed=step.latest_head_reviewed,
        )
        decision = prepared.decision
        if decision.handoff:
            handoffs += 1
        if no_progress:
            no_progress_events += 1
        outcomes.append(
            SequenceStepOutcome(
                label=step.label or f"step-{index + 1}",
                admit=decision.admit,
                handoff=decision.handoff,
                handoff_reason=decision.handoff_reason,
                may_emit_approve=decision.may_emit_approve,
                no_progress=no_progress,
                round_kind=decision.round_kind,
            )
        )
        if decision.admit and prepared.reservation_id is not None:
            complete_session_round(
                ledger, identity, prepared, published=True, now=current
            )
        earlier = previous
        previous = step.blocking_identities
    loaded = ledger.load(identity, now=current)
    record = loaded.record
    return SequenceReport(
        mode=policy.mode,
        steps=tuple(outcomes),
        completed_initial_reviews=(
            record.completed_initial_reviews if record is not None else 0
        ),
        completed_verification_rounds=(
            record.completed_verification_rounds if record is not None else 0
        ),
        handoffs=handoffs,
        no_progress_events=no_progress_events,
        cap_created_approval=False,
    )


def _bounded_count(value: object, *, maximum: int, allow_none: bool = False) -> bool:
    if allow_none and value is None:
        return True
    return (
        not isinstance(value, bool) and isinstance(value, int) and 0 <= value <= maximum
    )


def compare_sequence_policies(
    steps: tuple[SequenceStep, ...] | list[SequenceStep],
    *,
    current: ReviewConvergencePolicy | None = None,
    proposed: ReviewConvergencePolicy | None = None,
    ledger_factory: Callable[[], InMemorySessionLedger] | None = None,
) -> dict[str, object]:
    """Compare the compatible default with a proposed operator policy.

    Observation-only. Neither report publishes GitHub events, and
    ``cap_created_approval`` stays false on both arms. Each factory call
    must return a fresh in-memory ledger; a repeated instance fails closed.
    """

    current_policy = current or resolve_review_convergence_policy()
    proposed_policy = proposed or resolve_review_convergence_policy(
        mode="merge-focused"
    )
    created: list[InMemorySessionLedger] = []

    def isolated_factory() -> InMemorySessionLedger:
        if ledger_factory is None:
            ledger = InMemorySessionLedger()
        else:
            if not callable(ledger_factory):
                raise ReviewInputError("sequence replay ledger factory is invalid")
            ledger = ledger_factory()
        if not isinstance(ledger, InMemorySessionLedger):
            raise ReviewInputError("sequence replay ledger must be in-memory")
        if any(ledger is item for item in created):
            raise ReviewInputError("sequence replay ledgers must be distinct")
        created.append(ledger)
        return ledger

    current_report = replay_review_sequence(
        steps, current_policy, ledger_factory=isolated_factory
    )
    proposed_report = replay_review_sequence(
        steps, proposed_policy, ledger_factory=isolated_factory
    )
    return {
        "publication_default": DEFAULT_REVIEW_MODE,
        "current": current_report.to_dict(),
        "proposed": proposed_report.to_dict(),
        "cap_created_approval": bool(
            current_report.cap_created_approval or proposed_report.cap_created_approval
        ),
    }


__all__ = [
    "UNAVAILABLE_EVIDENCE_IDENTITY",
    "ObservedEvidenceIdentity",
    "ObservedExecutionMetrics",
    "ObservedFindingMetrics",
    "ObservedSequenceEvent",
    "ObservedSequenceReport",
    "SequenceReport",
    "SequenceStep",
    "SequenceStepOutcome",
    "compare_sequence_policies",
    "replay_review_sequence",
]
