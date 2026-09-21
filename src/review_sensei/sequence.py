"""Offline sequential replay for issue #136 C7 evaluation.

C7 compares the current compatible default (``legacy`` publication) with an
operator policy on frozen synthetic sequences. It does not change the
installed default or emit GitHub events. Shadow mode is observation-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Sequence

from .convergence import (
    DEFAULT_REVIEW_MODE,
    ReviewConvergencePolicy,
    detect_no_progress,
    resolve_review_convergence_policy,
)
from .errors import ReviewInputError
from .models import ProviderResponse, ReviewRequest
from .schemas import validate_public_document
from .service import ReviewService
from .session import (
    InMemorySessionLedger,
    LocalSessionLedger,
    SessionIdentity,
    complete_session_round,
    prepare_session_round,
    session_reservation_id,
)

PUBLIC_SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class SequenceStep:
    """One recorded review/verification attempt. No raw source."""

    head_sha: str
    blocking_identities: tuple[str, ...] = ()
    coverage_complete: bool = True
    independently_approval_eligible: bool = False
    latest_head_reviewed: bool = True
    label: str = "step"

    def __post_init__(self) -> None:
        if not isinstance(self.head_sha, str) or not self.head_sha.strip():
            raise ReviewInputError("sequence step head_sha is invalid")
        if any(
            not isinstance(item, str) or not item for item in self.blocking_identities
        ):
            raise ReviewInputError("blocking identity must be a non-empty string")


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
        payload = {
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


@dataclass(frozen=True)
class ObservedSequenceEvent:
    """One actual service-to-publication attempt captured by the F6 harness."""

    label: str
    provider_calls: int
    publication_status: str

    def to_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "provider_calls": self.provider_calls,
            "publication_status": self.publication_status,
        }


@dataclass(frozen=True)
class ObservedSequenceReport:
    """Bounded evidence from real internal components and mocked host edges."""

    mode: str
    events: tuple[ObservedSequenceEvent, ...]
    approval_events: int | None
    cap_created_approval: bool | None
    limitations: tuple[str, ...] = (
        "External model and GitHub APIs are mocked; internal admission and publication run normally.",
        "Approval metrics are unknown because the mocked publisher does not execute a GitHub finalizer.",
        "This harness is evidence for deterministic component behavior, not real-world model recall.",
    )

    def to_dict(self) -> dict[str, object]:
        payload = {
            "schema_version": PUBLIC_SCHEMA_VERSION,
            "mode": self.mode,
            "events": [event.to_dict() for event in self.events],
            "approval_events": self.approval_events,
            "cap_created_approval": self.cap_created_approval,
            "limitations": list(self.limitations),
        }
        validate_public_document(payload, "observed-convergence-report")
        return payload


class _ObservedProvider:
    name = "observed-fixture"
    model = "observed-fixture-model"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        return ProviderResponse(
            text='{"summary":"Observed fixture review.","comments":[],"learning_proposals":[]}',
            provider=self.name,
            model=self.model,
        )


class _ObservedBroker:
    def request_oidc_token(self) -> str:
        return "observed-oidc"

    def exchange(self, token: str, *, capability: str | None = None) -> str:
        return f"observed-{capability or 'session'}-token"


class _ObservedPublisher:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def publish(self, **kwargs):
        from .hosting.github.publication import PublicationResult

        self.calls.append(kwargs)
        return PublicationResult(status="published", review_id=len(self.calls))


def run_observed_review_sequence(
    steps: Sequence[SequenceStep], policy: ReviewConvergencePolicy
) -> ObservedSequenceReport:
    """Exercise service, durable admission, and publication across fresh jobs.

    The harness deliberately owns only the provider and GitHub publisher fakes.
    Each loop creates a fresh service and application instance while retaining
    one ledger, which models the fresh-process boundary without claiming a
    live provider or GitHub result.
    """

    if not isinstance(policy, ReviewConvergencePolicy):
        raise ReviewInputError("review convergence policy is invalid")
    if not isinstance(steps, Sequence) or not steps:
        raise ReviewInputError("observed sequence requires at least one step")
    from .hosting.github.application import GitHubApplication, GitHubWriteOptions

    temporary_root = TemporaryDirectory(prefix="reviewsensei-observed-")
    ledger_root = Path(temporary_root.name)
    publisher = _ObservedPublisher()
    events: list[ObservedSequenceEvent] = []
    for index, step in enumerate(steps):
        if not isinstance(step, SequenceStep):
            raise ReviewInputError("sequence step is invalid")
        provider = _ObservedProvider()
        service = ReviewService(provider)
        result = service.review(
            ReviewRequest(
                diff=(
                    "diff --git a/src/observed.py b/src/observed.py\n"
                    "--- a/src/observed.py\n+++ b/src/observed.py\n"
                    "@@ -1 +1 @@\n-old\n+new\n"
                )
            )
        )
        application = GitHubApplication(
            broker=_ObservedBroker(),
            http=None,
            reviewer=publisher,
            learner=object(),
            replier=object(),
            # Construct a new adapter for every event: only its on-disk record
            # crosses the logical process boundary.
            session_ledger=LocalSessionLedger(ledger_root),
        )
        outcome = application.publish_review(
            options=GitHubWriteOptions(auto_review=True, github_writes=True),
            oidc_token="observed-oidc",
            repository="owner/repo",
            repository_id=136,
            pull_request=136,
            head_sha=step.head_sha,
            base_branch="main",
            base_sha="f" * 40,
            result=result,
            diff="diff --git a/src/observed.py b/src/observed.py\n--- a/src/observed.py\n+++ b/src/observed.py\n@@ -1 +1 @@\n-old\n+new\n",
            app_slug="reviewsensei[bot]",
            convergence_policy=policy,
        )
        events.append(
            ObservedSequenceEvent(
                label=step.label or f"step-{index + 1}",
                provider_calls=provider.calls,
                publication_status=outcome.status,
            )
        )
    report = ObservedSequenceReport(
        mode=policy.mode,
        events=tuple(events),
        approval_events=None,
        cap_created_approval=None,
    )
    temporary_root.cleanup()
    return report


def replay_review_sequence(
    steps: tuple[SequenceStep, ...] | list[SequenceStep],
    policy: ReviewConvergencePolicy,
    *,
    repository: str = "owner/repo",
    pull_request: int = 136,
    now: datetime | None = None,
) -> SequenceReport:
    """Replay bounded review attempts against C1/C5 admission."""

    if not isinstance(policy, ReviewConvergencePolicy):
        raise ReviewInputError("review convergence policy is invalid")
    ledger = InMemorySessionLedger()
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


def compare_sequence_policies(
    steps: tuple[SequenceStep, ...] | list[SequenceStep],
    *,
    current: ReviewConvergencePolicy | None = None,
    proposed: ReviewConvergencePolicy | None = None,
) -> dict[str, object]:
    """Compare the compatible default with a proposed operator policy.

    Observation-only. Neither report publishes GitHub events, and
    ``cap_created_approval`` stays false on both arms.
    """

    current_policy = current or resolve_review_convergence_policy()
    proposed_policy = proposed or resolve_review_convergence_policy(
        mode="merge-focused"
    )
    current_report = replay_review_sequence(steps, current_policy)
    proposed_report = replay_review_sequence(steps, proposed_policy)
    return {
        "publication_default": DEFAULT_REVIEW_MODE,
        "current": current_report.to_dict(),
        "proposed": proposed_report.to_dict(),
        "cap_created_approval": bool(
            current_report.cap_created_approval or proposed_report.cap_created_approval
        ),
    }


__all__ = [
    "ObservedSequenceEvent",
    "ObservedSequenceReport",
    "SequenceReport",
    "SequenceStep",
    "SequenceStepOutcome",
    "compare_sequence_policies",
    "replay_review_sequence",
    "run_observed_review_sequence",
]
