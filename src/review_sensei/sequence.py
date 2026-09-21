"""Offline sequential replay for issue #136 C7 evaluation.

C7 compares the current compatible default (``legacy`` publication) with an
operator policy on frozen synthetic sequences. It does not change the
installed default or emit GitHub events. Shadow mode is observation-only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal, Mapping, Sequence, cast
from urllib.parse import urlparse

from .baseline import baseline_from_history_document, baseline_from_review
from .context import build_review_context_cache_key
from .convergence import (
    DEFAULT_REVIEW_MODE,
    ReviewConvergencePolicy,
    derive_blocker_candidate,
    detect_no_progress,
    resolve_review_convergence_policy,
)
from .errors import ReviewInputError
from .models import ProviderResponse, ReviewRequest, ReviewTransaction
from .schemas import validate_public_document
from .service import ReviewService
from .session import (
    InMemorySessionLedger,
    LocalSessionLedger,
    SessionIdentity,
    checkpoint_review_analysis,
    complete_session_round,
    next_session_generation,
    prepare_review_transaction,
    prepare_session_round,
    session_reservation_id,
    should_skip_automation,
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
    expected_material_finding_ids: tuple[str, ...] = ()
    expected_non_material_finding_ids: tuple[str, ...] = ()
    fixture_material_finding_ids: tuple[str, ...] = ()

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
                    and len(values) != len(set(values))
                )
            ):
                raise ReviewInputError(f"{label} identities are invalid")
        if set(self.expected_material_finding_ids) & set(
            self.expected_non_material_finding_ids
        ):
            raise ReviewInputError("expected finding labels are contradictory")


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


@dataclass(frozen=True)
class ObservedSequenceEvent:
    """One actual service-to-publication attempt captured by the F6 harness."""

    label: str
    provider_calls: int
    baseline_loaded: bool
    publication_status: str

    def to_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "provider_calls": self.provider_calls,
            "baseline_loaded": self.baseline_loaded,
            "publication_status": self.publication_status,
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
    duplicate_findings: int | None = None
    reopened_findings: int | None = None
    contradictions: int | None = None

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


class _ObservedProvider:
    name = "observed-fixture"
    model: str | None = "observed-fixture-model"

    def __init__(self, material_finding_ids: tuple[str, ...] = ()) -> None:
        self.calls = 0
        self.material_finding_ids = material_finding_ids

    def complete(self, request):
        self.calls += 1
        comments = [
            {
                "path": "src/observed.py",
                "line": 1,
                "body": f"fixture-material:{finding_id}",
                "blocking": True,
                "severity": "high",
                "fix_effort": "small",
                "category": "correctness",
            }
            for finding_id in self.material_finding_ids
        ]
        return ProviderResponse(
            text=json.dumps(
                {
                    "summary": "Observed fixture review.",
                    "comments": comments,
                    "learning_proposals": [],
                }
            ),
            provider=self.name,
            model=self.model,
        )


class _ObservedBroker:
    def request_oidc_token(self) -> str:
        return "observed-oidc"

    def exchange(self, token: str, *, capability: str | None = None) -> str:
        return f"observed-{capability or 'session'}-token"


class _ObservedHTTPResponse:
    """Small response shape consumed by the real bounded GitHub adapter."""

    def __init__(
        self, payload: dict[str, object] | Sequence[object], status: int = 200
    ):
        self.body = json.dumps(payload).encode("utf-8")
        self.status = status
        self.reason = "observed fixture"
        self.headers: dict[str, str] = {}

    def __enter__(self) -> _ObservedHTTPResponse:
        return self

    def __exit__(
        self, exc_type: object, exc_value: object, traceback: object
    ) -> Literal[False]:
        return False

    def read(self, size: int) -> bytes:
        return self.body[:size]


class _ObservedGitHub:
    """Stateful host double; the production HTTP and publisher code stay live."""

    def __init__(self) -> None:
        from .hosting.github.http import GitHubHttp

        self.head_sha = "0" * 40
        self.reviews: list[dict[str, object]] = []
        self.calls: list[tuple[str, str, dict[str, object] | None]] = []
        self.http = GitHubHttp(
            api_url="https://observed.github.invalid", opener=self._open, timeout=1
        )

    @property
    def approval_events(self) -> int:
        return sum(
            1
            for method, path, body in self.calls
            if method == "POST"
            and path.endswith("/reviews")
            and isinstance(body, dict)
            and body.get("event") == "APPROVE"
        )

    def _pr_payload(self) -> dict[str, object]:
        return {
            "state": "open",
            "draft": False,
            "user": {"login": "maintainer", "type": "User"},
            "head": {
                "sha": self.head_sha,
                "repo": {"full_name": "owner/repo", "fork": False},
            },
            "base": {
                "ref": "main",
                "sha": "f" * 40,
                "repo": {"id": 136, "full_name": "owner/repo", "fork": False},
            },
        }

    def _open(self, request: Any, timeout: int) -> _ObservedHTTPResponse:
        parsed = urlparse(request.full_url)
        path = parsed.path
        raw = request.data
        body = (
            json.loads(raw.decode("utf-8"))
            if isinstance(raw, (bytes, bytearray)) and raw
            else None
        )
        if body is not None and not isinstance(body, dict):
            raise ReviewInputError("observed GitHub request body is invalid")
        self.calls.append((request.method, path, body))
        if request.method == "GET" and path.endswith("/pulls/136"):
            return _ObservedHTTPResponse(self._pr_payload())
        if request.method == "GET" and path.endswith("/pulls/136/reviews"):
            return _ObservedHTTPResponse(self.reviews)
        if request.method == "POST" and path == "/graphql":
            return _ObservedHTTPResponse(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "nodes": [],
                                    "pageInfo": {
                                        "hasNextPage": False,
                                        "endCursor": None,
                                    },
                                }
                            }
                        }
                    }
                }
            )
        if request.method == "POST" and path.endswith("/pulls/136/reviews"):
            if body is None:
                raise ReviewInputError("observed review publication body is missing")
            review_id = len(self.reviews) + 1
            self.reviews.append(
                {
                    "id": review_id,
                    "body": body.get("body"),
                    "commit_id": body.get("commit_id"),
                    "state": body.get("event"),
                    "user": {"login": "reviewsensei[bot]"},
                }
            )
            return _ObservedHTTPResponse({"id": review_id})
        raise ReviewInputError(
            f"unexpected observed GitHub request: {request.method} {path}"
        )


def run_observed_review_sequence(
    steps: Sequence[SequenceStep],
    policy: ReviewConvergencePolicy,
    *,
    evidence_identity: ObservedEvidenceIdentity | None = None,
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
    report_identity = evidence_identity or ObservedEvidenceIdentity(
        source_identity="unavailable",
        package_identity="unavailable",
        workflow_identity="unavailable",
        configuration_digest=policy.digest(),
        fixture_identity="observed-convergence-fixture-v1",
        command="review-sensei evaluate-convergence --observed",
    )
    if not isinstance(report_identity, ObservedEvidenceIdentity):
        raise ReviewInputError("observed evidence identity is invalid")
    if report_identity.configuration_digest != policy.digest():
        raise ReviewInputError("observed evidence configuration is stale")
    from .hosting.github.application import GitHubApplication, GitHubWriteOptions

    github = _ObservedGitHub()
    events: list[ObservedSequenceEvent] = []
    baseline_events = 0
    command_events: list[str] = []
    expected_material_finding_ids: set[str] = set()
    expected_non_material_finding_ids: set[str] = set()
    observed_material_finding_ids: set[str] = set()
    observed_material_finding_occurrences: list[str] = []
    duplicate_findings = 0
    reopened_findings = 0
    contradictions = 0
    last_observed_step: dict[str, int] = {}
    handoffs = 0
    provider_calls = 0
    cap_created_approval: bool | None = None
    shadow_isolated = False
    with TemporaryDirectory(prefix="reviewsensei-observed-") as temporary_root:
        ledger_root = Path(temporary_root)
        for index, step in enumerate(steps):
            if not isinstance(step, SequenceStep):
                raise ReviewInputError("sequence step is invalid")
            provider = _ObservedProvider(step.fixture_material_finding_ids)
            service = ReviewService(provider)
            request = ReviewRequest(
                diff=(
                    "diff --git a/src/observed.py b/src/observed.py\n"
                    "--- a/src/observed.py\n+++ b/src/observed.py\n"
                    "@@ -1 +1 @@\n-old\n+new\n"
                ),
                repository="owner/repo",
                pull_request_number=136,
                model=provider.model,
                base_sha="f" * 40,
                head_sha=step.head_sha,
            )
            current_key = build_review_context_cache_key(
                request, provider_name=provider.name, stages=service.stages
            )
            if current_key is None:
                raise ReviewInputError("observed cache identity is unavailable")
            session_identity = SessionIdentity("owner/repo", 136, repository_id=136)
            ledger = LocalSessionLedger(ledger_root)
            prior_record = ledger.load(session_identity).record
            prior_history = (
                None if prior_record is None else prior_record.convergence_history
            )
            baseline_loaded = bool(
                isinstance(prior_history, Mapping)
                and prior_history.get("state") == "completed"
            )
            durable_baseline = None
            if baseline_loaded:
                if not isinstance(prior_history, Mapping):
                    raise ReviewInputError("observed durable baseline is invalid")
                durable_baseline = baseline_from_history_document(
                    prior_history.get("baseline")
                )
            configuration_context = {
                "provider": {
                    "name": provider.name,
                    "profile": None,
                    "base_url": None,
                    "timeout_seconds": None,
                    "max_output_tokens": None,
                    "allow_custom_endpoint": False,
                    "openrouter_policy": None,
                },
                "model": provider.model or "observed-fixture-model",
                "stages": [
                    {
                        "name": stage.name,
                        "outputs": list(stage.outputs),
                        "categories": [category.id for category in stage.categories],
                        "provider_profile": stage.provider_profile,
                    }
                    for stage in service.stages
                ],
                "category_policy": sorted(
                    {
                        category.id
                        for stage in service.stages
                        for category in stage.categories
                    }
                ),
                "orchestration": {"enabled": False, "continue_rounds": 0},
                "publication_mode": policy.mode,
            }
            evidence_context = {"evidence_policy": "legacy", "snapshot_sha256": None}
            reservation = session_reservation_id(
                repository="owner/repo",
                pull_request=136,
                head_sha=step.head_sha,
                kind="publish",
            )
            prepared = prepare_review_transaction(
                ledger,
                session_identity,
                policy,
                reservation_id=reservation,
                base_sha="f" * 40,
                head_sha=step.head_sha,
                configuration_digest=ReviewTransaction.compute_configuration_digest(
                    configuration_context
                ),
                evidence_digest=ReviewTransaction.compute_evidence_digest(
                    evidence_context
                ),
                coverage_complete=step.coverage_complete,
                latest_head_reviewed=step.latest_head_reviewed,
            )
            github.head_sha = step.head_sha
            approvals_before = github.approval_events
            if should_skip_automation(prepared.decision, inference=True):
                if prepared.decision.handoff_reason == "round-budget-exhausted":
                    cap_created_approval = github.approval_events > approvals_before
                handoffs += int(prepared.decision.handoff)
                events.append(
                    ObservedSequenceEvent(
                        label=step.label or f"step-{index + 1}",
                        provider_calls=0,
                        baseline_loaded=baseline_loaded,
                        publication_status="handoff",
                    )
                )
                continue
            result = service.review(
                request,
                current_key=current_key,
            )
            # These facts are the fixture's independently-checkable evidence
            # edge.  The production publisher still performs C2 admission;
            # no model ``blocking`` flag alone can turn into a blocker.
            blocker_candidates = tuple(
                derive_blocker_candidate(
                    comment,
                    on_changed_path=True,
                    evidence_locations_validated=True,
                    has_failure_condition=True,
                    has_specific_violation=True,
                )
                for comment in result.comments
            )
            provider_calls += provider.calls
            expected_material_finding_ids.update(step.expected_material_finding_ids)
            expected_non_material_finding_ids.update(
                step.expected_non_material_finding_ids
            )
            if expected_material_finding_ids & expected_non_material_finding_ids:
                raise ReviewInputError("expected finding labels are contradictory")
            material_ids = tuple(
                comment.body.removeprefix("fixture-material:")
                for comment in result.comments
                if comment.body.startswith("fixture-material:")
                and comment.severity in {"high", "critical"}
            )
            observed_material_finding_occurrences.extend(material_ids)
            observed_material_finding_ids.update(material_ids)
            duplicate_findings += len(material_ids) - len(set(material_ids))
            for finding_id in set(material_ids):
                previous_step = last_observed_step.get(finding_id)
                if previous_step is not None and previous_step < index - 1:
                    reopened_findings += 1
                last_observed_step[finding_id] = index
            contradictions += sum(
                finding_id in expected_non_material_finding_ids
                for finding_id in material_ids
            )
            checkpoint_baseline = baseline_from_review(
                result,
                cache_key=current_key,
                policy=policy,
                generation=next_session_generation(prepared.record),
            )
            result = checkpoint_review_analysis(
                ledger,
                session_identity,
                prepared,
                result,
                baseline=checkpoint_baseline,
            )
            baseline_events += 1
            from .hosting.github.publication import ReviewPublisher

            application = GitHubApplication(
                # The GitHub transport and model response are bounded fixture
                # edges. Publisher and finalizer remain production components.
                broker=cast(Any, _ObservedBroker()),
                http=github.http,
                reviewer=ReviewPublisher(http=github.http),
                learner=cast(Any, object()),
                replier=cast(Any, object()),
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
                baseline=durable_baseline,
                current_key=current_key,
                changed_paths=("src/observed.py",),
                blocker_candidates=blocker_candidates,
                configuration_context=configuration_context,
                evidence_context=evidence_context,
            )
            if (
                outcome.status == "handoff"
                and outcome.diagnostic == "round-budget-exhausted"
            ):
                cap_created_approval = github.approval_events > approvals_before
            if outcome.status == "handoff":
                handoffs += 1
            events.append(
                ObservedSequenceEvent(
                    label=step.label or f"step-{index + 1}",
                    provider_calls=provider.calls,
                    baseline_loaded=baseline_loaded,
                    publication_status=outcome.status,
                )
            )
        session_identity = SessionIdentity("owner/repo", 136, repository_id=136)
        ledger = LocalSessionLedger(ledger_root)
        loaded_record = ledger.load(session_identity).record
        completed_rounds = (
            0
            if loaded_record is None
            else loaded_record.completed_initial_reviews
            + loaded_record.completed_verification_rounds
        )
        failed_attempts = 0 if loaded_record is None else loaded_record.failed_attempts
        publisher_calls_before_shadow = len(github.calls)
        shadow_record_before = loaded_record
        # C7 replay keeps its own in-memory ledger. Running it here verifies
        # that the comparison fixture cannot mutate the enforced job state.
        compare_sequence_policies(
            tuple(steps),
            current=policy,
            proposed=ReviewConvergencePolicy(mode="strict"),
        )
        shadow_isolated = (
            len(github.calls) == publisher_calls_before_shadow
            and ledger.load(session_identity).record == shadow_record_before
        )
        command_application = GitHubApplication(
            broker=cast(Any, _ObservedBroker()),
            http=github.http,
            reviewer=ReviewPublisher(http=github.http),
            learner=cast(Any, object()),
            replier=cast(Any, object()),
            session_ledger=LocalSessionLedger(ledger_root),
        )
        pause = command_application.apply_maintainer_command(
            options=GitHubWriteOptions(auto_review=True, github_writes=True),
            oidc_token=None,
            repository="owner/repo",
            repository_id=136,
            pull_request=136,
            head_sha=steps[-1].head_sha,
            body="@sensei review pause",
            actor_login="maintainer",
            actor_type="User",
            association="OWNER",
            app_slug="reviewsensei[bot]",
        )
        command_events.append(
            f"{pause.action}:{'applied' if pause.applied else 'ignored'}"
        )
        continue_application = GitHubApplication(
            broker=cast(Any, _ObservedBroker()),
            http=github.http,
            reviewer=ReviewPublisher(http=github.http),
            learner=cast(Any, object()),
            replier=cast(Any, object()),
            session_ledger=LocalSessionLedger(ledger_root),
        )
        continued = continue_application.apply_maintainer_command(
            options=GitHubWriteOptions(auto_review=True, github_writes=True),
            oidc_token=None,
            repository="owner/repo",
            repository_id=136,
            pull_request=136,
            head_sha=steps[-1].head_sha,
            body="@sensei review continue --rounds 1",
            actor_login="maintainer",
            actor_type="User",
            association="OWNER",
            app_slug="reviewsensei[bot]",
        )
        command_events.append(
            f"{continued.action}:{'applied' if continued.applied else 'ignored'}"
        )
    cutover_unmet: list[str] = []
    required_rounds = (
        policy.max_completed_initial_reviews + policy.max_completed_verification_rounds
    )
    if cap_created_approval is not False:
        cutover_unmet.append(
            "the configured round cap was not observed with zero new approval events"
        )
    if completed_rounds < required_rounds or provider_calls < required_rounds:
        cutover_unmet.append(
            "the configured initial and verification round budget was not fully exercised"
        )
    if not events or events[-1].provider_calls != 0:
        cutover_unmet.append("an over-cap request did not prove zero new inference")
    if not any(event.baseline_loaded for event in events[1:]):
        cutover_unmet.append("no fresh job loaded a durable completed baseline")
    if not any(event.publication_status == "published" for event in events):
        cutover_unmet.append("no successful application publication was observed")
    if not shadow_isolated:
        cutover_unmet.append("shadow comparison isolation was not observed")
    if tuple(command_events) != ("pause:applied", "continue:applied"):
        cutover_unmet.append("durable maintainer command evidence is incomplete")
    if not expected_material_finding_ids:
        cutover_unmet.append("no maintainer-labelled material regression was supplied")
    if expected_material_finding_ids - observed_material_finding_ids:
        cutover_unmet.append("a labelled material regression was missed")
    if observed_material_finding_ids - expected_material_finding_ids:
        cutover_unmet.append("an unjustified material blocker was observed")
    if duplicate_findings or reopened_findings or contradictions:
        cutover_unmet.append(
            "duplicate, reopened, or contradictory finding evidence requires adjudication"
        )
    if "unavailable" in {
        report_identity.source_identity,
        report_identity.package_identity,
        report_identity.workflow_identity,
    }:
        cutover_unmet.append(
            "installed source, package, and workflow identities are unavailable"
        )
    report = ObservedSequenceReport(
        mode=policy.mode,
        events=tuple(events),
        baseline_events=baseline_events,
        command_events=tuple(command_events),
        finding_metrics=ObservedFindingMetrics(
            expected_material_findings=len(expected_material_finding_ids),
            observed_material_findings=len(observed_material_finding_occurrences),
            matched_material_findings=len(
                expected_material_finding_ids & observed_material_finding_ids
            ),
            missed_material_findings=len(
                expected_material_finding_ids - observed_material_finding_ids
            ),
            unjustified_late_blockers=len(
                observed_material_finding_ids - expected_material_finding_ids
            ),
            blocker_precision=(
                None
                if not observed_material_finding_occurrences
                else len(expected_material_finding_ids & observed_material_finding_ids)
                / len(observed_material_finding_occurrences)
            ),
            seeded_material_regressions_detected=len(
                expected_material_finding_ids & observed_material_finding_ids
            ),
            duplicate_findings=duplicate_findings,
            reopened_findings=reopened_findings,
            contradictions=contradictions,
        ),
        execution_metrics=ObservedExecutionMetrics(
            completed_rounds=completed_rounds,
            handoffs=handoffs,
            provider_calls=provider_calls,
            failed_attempts=failed_attempts,
        ),
        shadow_isolated=shadow_isolated,
        evidence_identity=report_identity,
        approval_events=github.approval_events,
        cap_created_approval=cap_created_approval,
        cutover_status="passed" if not cutover_unmet else "not_ready",
        unmet_criteria=tuple(cutover_unmet),
    )
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
    "run_observed_review_sequence",
]
