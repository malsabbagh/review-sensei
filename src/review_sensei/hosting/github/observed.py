"""Observed F6 convergence harness.

The provider-neutral sequence report types stay in ``review_sensei.sequence``.
This module lives on the GitHub hosting side because it drives
``GitHubApplication`` and ``GitHubHttp``. Core sequence replay does not import
it.
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal, Mapping, Sequence, cast
from urllib.parse import urlparse

from ...baseline import (
    ReviewBaseline,
    baseline_from_history_document,
    baseline_from_review,
    evaluate_baseline_compatibility,
)
from ...context import ReviewContextCacheKey, build_review_context_cache_key
from ...convergence import ReviewConvergencePolicy, derive_blocker_candidate
from ...errors import ReviewInputError
from ...models import ProviderResponse, ReviewRequest, ReviewTransaction
from ...sequence import (
    UNAVAILABLE_EVIDENCE_IDENTITY,
    ObservedEvidenceIdentity,
    ObservedExecutionMetrics,
    ObservedFindingMetrics,
    ObservedSequenceEvent,
    ObservedSequenceReport,
    SequenceStep,
    compare_sequence_policies,
)
from ...service import ReviewService
from ...session import (
    InMemorySessionLedger,
    LocalSessionLedger,
    SessionIdentity,
    checkpoint_review_analysis,
    next_session_generation,
    prepare_review_transaction,
    session_reservation_id,
    should_skip_automation,
)
from .application import GitHubApplication, GitHubWriteOptions
from .http import GitHubHttp
from .publication import ReviewPublisher, prepare_publication_review

_OBSERVED_DIFF = (
    "diff --git a/src/observed.py b/src/observed.py\n"
    "--- a/src/observed.py\n+++ b/src/observed.py\n"
    "@@ -1 +1 @@\n-old\n+new\n"
)


def restored_compatible_baseline(
    prior_history: object,
    *,
    current_key: ReviewContextCacheKey,
    policy: ReviewConvergencePolicy,
) -> ReviewBaseline | None:
    """Return a completed baseline only when it parses and is compatible.

    A history ``state`` of ``completed`` is not enough. A missing, tampered,
    or incompatible baseline document stays unloaded.
    """

    if (
        not isinstance(prior_history, Mapping)
        or prior_history.get("state") != "completed"
    ):
        return None
    try:
        restored = baseline_from_history_document(prior_history.get("baseline"))
        compatible = (
            evaluate_baseline_compatibility(
                restored,
                current_key=current_key,
                policy=policy,
            )
            is None
        )
    except ReviewInputError:
        return None
    if not compatible:
        return None
    return restored


def _handoff_reason(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _admitted_material_ids(comments: Sequence[Any]) -> tuple[str, ...]:
    """Material oracle ids that C2 actually admitted as blockers."""

    admitted: list[str] = []
    for comment in comments:
        body = comment.body
        if (
            comment.effective_blocking is True
            and isinstance(body, str)
            and body.startswith("fixture-material:")
        ):
            admitted.append(body.removeprefix("fixture-material:"))
    return tuple(admitted)


class _ObservedProvider:
    name = "observed-fixture"
    model: str | None = "observed-fixture-model"

    def __init__(self, material_finding_ids: tuple[str, ...] = ()) -> None:
        self.calls = 0
        self.material_finding_ids = material_finding_ids

    def complete(self, request: object) -> ProviderResponse:
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
        self._offset = 0

    def __enter__(self) -> _ObservedHTTPResponse:
        return self

    def __exit__(
        self, exc_type: object, exc_value: object, traceback: object
    ) -> Literal[False]:
        return False

    def read(self, size: int) -> bytes:
        start = self._offset
        self._offset = min(len(self.body), self._offset + max(size, 0))
        return self.body[start : self._offset]


class _ObservedGitHub:
    """Stateful host double; the production HTTP and publisher code stay live."""

    def __init__(self) -> None:
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


def _prove_shadow_isolation(
    *,
    github: _ObservedGitHub,
    ledger: LocalSessionLedger,
    session_identity: SessionIdentity,
    steps: tuple[SequenceStep, ...],
    policy: ReviewConvergencePolicy,
    record_before: object,
) -> bool:
    """Prove C7 replay used its own ledger and did not touch the host path.

    The comparison must construct distinct in-memory ledgers, leave the
    enforced local record and GitHub call log unchanged, and must not
    construct a GitHub application, a local ledger, or call the fixture
    provider.
    """

    calls_before = len(github.calls)
    approvals_before = github.approval_events
    reviews_before = len(github.reviews)
    memory_ledger_ids: list[int] = []
    local_builds = 0
    application_builds = 0
    provider_calls = 0
    enforced_ledger_id = id(ledger)
    original_memory_init: Any = InMemorySessionLedger.__init__
    original_local_init: Any = LocalSessionLedger.__init__
    original_app_init: Any = GitHubApplication.__init__
    original_complete: Any = _ObservedProvider.complete

    def memory_init(self: Any, *args: Any, **kwargs: Any) -> None:
        memory_ledger_ids.append(id(self))
        original_memory_init(self, *args, **kwargs)

    def local_init(self: Any, *args: Any, **kwargs: Any) -> None:
        nonlocal local_builds
        local_builds += 1
        original_local_init(self, *args, **kwargs)

    def app_init(self: Any, *args: Any, **kwargs: Any) -> None:
        nonlocal application_builds
        application_builds += 1
        original_app_init(self, *args, **kwargs)

    def complete(self: Any, request: object) -> ProviderResponse:
        nonlocal provider_calls
        provider_calls += 1
        return cast(ProviderResponse, original_complete(self, request))

    setattr(InMemorySessionLedger, "__init__", memory_init)
    setattr(LocalSessionLedger, "__init__", local_init)
    setattr(GitHubApplication, "__init__", app_init)
    setattr(_ObservedProvider, "complete", complete)
    try:
        compare_sequence_policies(
            steps,
            current=policy,
            proposed=ReviewConvergencePolicy(mode="strict"),
        )
    finally:
        setattr(InMemorySessionLedger, "__init__", original_memory_init)
        setattr(LocalSessionLedger, "__init__", original_local_init)
        setattr(GitHubApplication, "__init__", original_app_init)
        setattr(_ObservedProvider, "complete", original_complete)
    # compare_sequence_policies replays the current and proposed policies,
    # so the shadow path owns exactly two ledgers.
    distinct_shadow_ledgers = (
        len(memory_ledger_ids) == 2
        and len(set(memory_ledger_ids)) == 2
        and all(ledger_id != enforced_ledger_id for ledger_id in memory_ledger_ids)
    )
    return (
        distinct_shadow_ledgers
        and local_builds == 0
        and application_builds == 0
        and provider_calls == 0
        and len(github.calls) == calls_before
        and github.approval_events == approvals_before
        and len(github.reviews) == reviews_before
        and ledger.load(session_identity).record == record_before
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
    if not isinstance(steps, Sequence) or isinstance(steps, (str, bytes)) or not steps:
        raise ReviewInputError("observed sequence requires at least one step")
    report_identity = evidence_identity or ObservedEvidenceIdentity(
        source_identity=UNAVAILABLE_EVIDENCE_IDENTITY,
        package_identity=UNAVAILABLE_EVIDENCE_IDENTITY,
        workflow_identity=UNAVAILABLE_EVIDENCE_IDENTITY,
        configuration_digest=policy.digest(),
        fixture_identity="observed-convergence-fixture-v1",
        command="review-sensei evaluate-convergence --observed",
    )
    if not isinstance(report_identity, ObservedEvidenceIdentity):
        raise ReviewInputError("observed evidence identity is invalid")
    if report_identity.configuration_digest != policy.digest():
        raise ReviewInputError("observed evidence configuration is stale")
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
    cap_handoff_observations = 0
    approvals_during_cap_handoffs = 0
    shadow_isolated = False

    def note_cap(created_approvals: int) -> None:
        nonlocal cap_handoff_observations, approvals_during_cap_handoffs
        cap_handoff_observations += 1
        if created_approvals > 0:
            approvals_during_cap_handoffs += created_approvals

    with TemporaryDirectory(prefix="reviewsensei-observed-") as temporary_root:
        ledger_root = Path(temporary_root)
        for index, step in enumerate(steps):
            if not isinstance(step, SequenceStep):
                raise ReviewInputError("sequence step is invalid")
            provider = _ObservedProvider(step.fixture_material_finding_ids)
            service = ReviewService(provider)
            request = ReviewRequest(
                diff=_OBSERVED_DIFF,
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
            durable_baseline = restored_compatible_baseline(
                prior_history,
                current_key=current_key,
                policy=policy,
            )
            baseline_loaded = durable_baseline is not None
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
                created_approvals = github.approval_events - approvals_before
                if prepared.decision.handoff_reason == "round-budget-exhausted":
                    note_cap(created_approvals)
                handoffs += int(prepared.decision.handoff)
                events.append(
                    ObservedSequenceEvent(
                        label=step.label or f"step-{index + 1}",
                        provider_calls=0,
                        baseline_loaded=baseline_loaded,
                        publication_status="handoff",
                        handoff_reason=_handoff_reason(
                            prepared.decision.handoff_reason
                        ),
                        approval_events=created_approvals,
                    )
                )
                continue
            result = service.review(
                request,
                current_key=current_key,
            )
            # These facts are the fixture's independently-checkable evidence
            # edge. The production publisher still performs C2 admission;
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
            admitted = prepare_publication_review(
                result=result,
                diff=_OBSERVED_DIFF,
                head_sha=step.head_sha,
                evidence_policy="legacy",
                convergence_policy=policy,
                blocker_candidates=blocker_candidates,
                baseline=durable_baseline,
                current_key=current_key,
                changed_paths=("src/observed.py",),
            )
            material_ids = _admitted_material_ids(admitted.result.comments)
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
                diff=_OBSERVED_DIFF,
                app_slug="reviewsensei[bot]",
                convergence_policy=policy,
                baseline=durable_baseline,
                current_key=current_key,
                changed_paths=("src/observed.py",),
                blocker_candidates=blocker_candidates,
                configuration_context=configuration_context,
                evidence_context=evidence_context,
            )
            created_approvals = github.approval_events - approvals_before
            if (
                outcome.status == "handoff"
                and outcome.diagnostic == "round-budget-exhausted"
            ):
                note_cap(created_approvals)
            if outcome.status == "handoff":
                handoffs += 1
            events.append(
                ObservedSequenceEvent(
                    label=step.label or f"step-{index + 1}",
                    provider_calls=provider.calls,
                    baseline_loaded=baseline_loaded,
                    publication_status=outcome.status,
                    handoff_reason=(
                        _handoff_reason(outcome.diagnostic)
                        if outcome.status == "handoff"
                        else None
                    ),
                    approval_events=created_approvals,
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
        shadow_isolated = _prove_shadow_isolation(
            github=github,
            ledger=ledger,
            session_identity=session_identity,
            steps=tuple(steps),
            policy=policy,
            record_before=loaded_record,
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
    cap_created_approval = (
        None if cap_handoff_observations == 0 else approvals_during_cap_handoffs > 0
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
    last_event = events[-1] if events else None
    if (
        last_event is None
        or last_event.publication_status != "handoff"
        or last_event.handoff_reason != "round-budget-exhausted"
        or last_event.provider_calls != 0
        or last_event.approval_events != 0
    ):
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
    if UNAVAILABLE_EVIDENCE_IDENTITY in {
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
