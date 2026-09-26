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
from ...models import (
    ProviderResponse,
    ReviewRequest,
    ReviewTransaction,
    build_transaction_configuration_context,
    transaction_stage_identity,
)
from ...sequence import (
    UNAVAILABLE_EVIDENCE_IDENTITY,
    ObservedEvidenceIdentity,
    ObservedExecutionMetrics,
    ObservedFindingMetrics,
    ObservedSequenceEvent,
    ObservedSequenceReport,
    SequenceStep,
    compare_sequence_policies,
    identity_is_unavailable,
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
        if comment.effective_blocking is not True or not isinstance(body, str):
            continue
        if body.startswith("fixture-material:"):
            admitted.append(body.removeprefix("fixture-material:"))
        elif body.startswith("fixture-unqualified:"):
            # An admitted unqualified comment is not a material oracle match.
            admitted.append("unqualified:" + body.removeprefix("fixture-unqualified:"))
    return tuple(admitted)


def observed_publication_configuration(
    provider: Any,
    stages: Sequence[Any],
    policy: ReviewConvergencePolicy,
) -> dict[str, object]:
    """Configuration identity the production transaction digest also uses."""

    stage_identity, category_policy = transaction_stage_identity(stages)
    return build_transaction_configuration_context(
        provider={
            "name": provider.name,
            "profile": None,
            "base_url": None,
            "timeout_seconds": None,
            "max_output_tokens": None,
            "allow_custom_endpoint": False,
            "openrouter_policy": None,
        },
        model=provider.model or "observed-fixture-model",
        stages=stage_identity,
        category_policy=category_policy,
        publication_mode=policy.mode,
        orchestration_enabled=False,
    )


def _fixture_comment(finding_id: str, *, qualified: bool) -> dict[str, object]:
    prefix = "fixture-material" if qualified else "fixture-unqualified"
    return {
        "path": "src/observed.py",
        "line": 1,
        "body": f"{prefix}:{finding_id}",
        "blocking": True,
        "severity": "high",
        "fix_effort": "small",
        "category": "correctness",
    }


def _blocker_candidate_for_comment(comment: Any) -> Any:
    """Trusted evidence only for fixture-material comments.

    Unqualified fixture comments keep a high blocking proposal so C2 can
    reject them. Their evidence and attribution flags stay false.
    """

    qualified = isinstance(comment.body, str) and comment.body.startswith(
        "fixture-material:"
    )
    return derive_blocker_candidate(
        comment,
        on_changed_path=qualified,
        evidence_locations_validated=qualified,
        has_failure_condition=qualified,
        has_specific_violation=qualified,
    )


class _ObservedProvider:
    name = "observed-fixture"
    model: str | None = "observed-fixture-model"

    def __init__(
        self,
        material_finding_ids: tuple[str, ...] = (),
        unqualified_finding_ids: tuple[str, ...] = (),
    ) -> None:
        self.calls = 0
        self.material_finding_ids = material_finding_ids
        self.unqualified_finding_ids = unqualified_finding_ids

    def complete(self, request: object) -> ProviderResponse:
        self.calls += 1
        comments = [
            _fixture_comment(finding_id, qualified=True)
            for finding_id in self.material_finding_ids
        ] + [
            _fixture_comment(finding_id, qualified=False)
            for finding_id in self.unqualified_finding_ids
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


class _UnusedCollaborator:
    """Fail closed if a harness path reaches an adapter this fixture does not own."""

    def __getattr__(self, name: str) -> object:
        raise ReviewInputError(f"observed harness does not provide {name}")


class _ObservedHTTPResponse:
    """Small response shape consumed by the real bounded GitHub adapter."""

    def __init__(
        self, payload: dict[str, object] | Sequence[object], status: int = 200
    ):
        self.body = json.dumps(payload).encode("utf-8")
        self.status = status
        self.reason = "observed fixture"
        # GitHubHttp caps the body with read(MAX_GITHUB_RESPONSE_BYTES + 1).
        # It does not consult Content-Length; the header records the fixture size.
        self.headers = {"Content-Length": str(len(self.body))}
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

    def __init__(self, *, unresolved_blocking_thread: bool = False) -> None:
        self.head_sha = "0" * 40
        self.unresolved_blocking_thread = unresolved_blocking_thread
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
            nodes: list[dict[str, object]] = []
            operation = body.get("operationName") if isinstance(body, dict) else None
            if operation == "ReviewThreads" and self.unresolved_blocking_thread:
                nodes.append(
                    {
                        "isResolved": False,
                        "comments": {
                            "nodes": [
                                {
                                    "body": "[🚫 Blocking] fixture open root",
                                    "path": "src/observed.py",
                                    "line": 1,
                                    "author": {"login": "reviewsensei[bot]"},
                                }
                            ]
                        },
                    }
                )
            return _ObservedHTTPResponse(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "nodes": nodes,
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


def _enforced_record_unchanged(
    ledger: LocalSessionLedger,
    session_identity: SessionIdentity,
    record_before: object,
) -> bool:
    loaded = ledger.load(session_identity)
    if record_before is None:
        return loaded.status == "missing" and loaded.record is None
    return loaded.record == record_before


def _shadow_is_isolated(
    *,
    github: _ObservedGitHub,
    ledger: LocalSessionLedger,
    session_identity: SessionIdentity,
    steps: tuple[SequenceStep, ...],
    policy: ReviewConvergencePolicy,
    record_before: object,
) -> bool:
    """Prove C7 replay used ledgers the harness injected.

    The comparison receives a factory and must build exactly two in-memory
    ledgers from it. Host call logs and the enforced file record stay
    unchanged. No class objects are patched.
    """

    created: list[InMemorySessionLedger] = []

    def ledger_factory() -> InMemorySessionLedger:
        shadow_ledger = InMemorySessionLedger()
        created.append(shadow_ledger)
        return shadow_ledger

    calls_before = len(github.calls)
    approvals_before = github.approval_events
    reviews_before = len(github.reviews)
    compare_sequence_policies(
        steps,
        current=policy,
        proposed=ReviewConvergencePolicy(mode="strict"),
        ledger_factory=ledger_factory,
    )
    return (
        len(created) == 2
        and len({id(item) for item in created}) == 2
        and all(id(item) != id(ledger) for item in created)
        and len(github.calls) == calls_before
        and github.approval_events == approvals_before
        and len(github.reviews) == reviews_before
        and _enforced_record_unchanged(ledger, session_identity, record_before)
    )


def observed_cutover_gaps(
    *,
    events: Sequence[ObservedSequenceEvent],
    shadow_isolated: bool,
    command_events: Sequence[str],
    expected_material_finding_ids: set[str],
    observed_material_finding_ids: set[str],
    duplicate_findings: int,
    reopened_findings: int,
    contradictions: int,
    source_identity: str,
    package_identity: str,
    workflow_identity: str,
) -> tuple[str, ...]:
    """Return the F6 cutover messages for one observed run."""

    gaps: list[str] = []
    if not any(event.baseline_loaded for event in events[1:]):
        gaps.append("no fresh job loaded a durable completed baseline")
    if not any(event.publication_status == "published" for event in events):
        gaps.append("no successful application publication was observed")
    if not shadow_isolated:
        gaps.append("shadow comparison isolation was not observed")
    if tuple(command_events) != ("pause:applied", "continue:applied"):
        gaps.append("durable maintainer command evidence is incomplete")
    if not expected_material_finding_ids:
        gaps.append("no maintainer-labelled material regression was supplied")
    if expected_material_finding_ids - observed_material_finding_ids:
        gaps.append("a labelled material regression was missed")
    if observed_material_finding_ids - expected_material_finding_ids:
        gaps.append("an unjustified material blocker was observed")
    if duplicate_findings or reopened_findings or contradictions:
        gaps.append(
            "duplicate, reopened, or contradictory finding evidence requires adjudication"
        )
    if any(
        identity_is_unavailable(identity)
        for identity in (source_identity, package_identity, workflow_identity)
    ):
        gaps.append(
            "installed source, package, and workflow identities are unavailable"
        )
    return tuple(gaps)


def run_observed_review_sequence(
    steps: Sequence[SequenceStep],
    policy: ReviewConvergencePolicy,
    *,
    evidence_identity: ObservedEvidenceIdentity | None = None,
    unresolved_blocking_thread: bool = False,
) -> ObservedSequenceReport:
    """Exercise service, durable admission, and publication across fresh jobs.

    The harness deliberately owns only the provider and GitHub publisher fakes.
    Each loop creates a fresh service and application instance while retaining
    one ledger, which models the fresh-process boundary without claiming a
    live provider or GitHub result.
    """

    if not isinstance(policy, ReviewConvergencePolicy):
        raise ReviewInputError("review convergence policy is invalid")
    if policy.mode not in {"advisory", "merge-focused", "strict"}:
        raise ReviewInputError("observed evidence requires an operator review mode")
    if not isinstance(steps, Sequence) or isinstance(steps, (str, bytes)) or not steps:
        raise ReviewInputError("observed sequence requires at least one step")
    if len(steps) > 32:
        raise ReviewInputError("observed sequence exceeds the event limit")
    expected_material_ids: set[str] = set()
    fixture_finding_ids: set[str] = set()
    for step in steps:
        if not isinstance(step, SequenceStep):
            raise ReviewInputError("sequence step is invalid")
        expected_material_ids.update(step.expected_material_finding_ids)
        fixture_finding_ids.update(step.fixture_material_finding_ids)
        fixture_finding_ids.update(step.fixture_unqualified_finding_ids)
    if len(expected_material_ids) > 512 or len(fixture_finding_ids) > 512:
        raise ReviewInputError("observed material findings exceed the metric limit")
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
    github = _ObservedGitHub(unresolved_blocking_thread=unresolved_blocking_thread)
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
    shadow_isolated = False

    with TemporaryDirectory(prefix="reviewsensei-observed-") as temporary_root:
        ledger_root = Path(temporary_root)
        for index, step in enumerate(steps):
            if not isinstance(step, SequenceStep):
                raise ReviewInputError("sequence step is invalid")
            provider = _ObservedProvider(
                step.fixture_material_finding_ids,
                step.fixture_unqualified_finding_ids,
            )
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
            # True only when this job restored a compatible baseline. That
            # same object is what publication consumes. A missing or
            # incompatible history stays unloaded.
            baseline_loaded = durable_baseline is not None
            configuration_context = observed_publication_configuration(
                provider, service.stages, policy
            )
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
            # fixture-material comments carry trusted evidence. Unqualified
            # comments stay high and blocking in the model output so C2 can
            # reject them instead of admitting every fixture body.
            blocker_candidates = tuple(
                _blocker_candidate_for_comment(comment) for comment in result.comments
            )
            # Fixture responses are structurally valid, so each logical step
            # records one provider completion. A structural retry is not part
            # of this harness.
            provider_calls += provider.calls
            # Material labels match across the sequence. A later admission
            # satisfies an earlier oracle, including the verification step
            # that expects material-a and emits nothing.
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
                learner=cast(Any, _UnusedCollaborator()),
                replier=cast(Any, _UnusedCollaborator()),
                # Construct a new adapter for every event: only its on-disk record
                # crosses the logical process boundary.
                session_ledger=LocalSessionLedger(ledger_root),
            )
            # Publication prepares this analysis result again through
            # ReviewPublisher.prepare. The harness admission above uses the
            # same result and the same inputs, so the counted comments are
            # the ones publication acts on.
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
        shadow_isolated = _shadow_is_isolated(
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
            learner=cast(Any, _UnusedCollaborator()),
            replier=cast(Any, _UnusedCollaborator()),
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
            learner=cast(Any, _UnusedCollaborator()),
            replier=cast(Any, _UnusedCollaborator()),
            session_ledger=LocalSessionLedger(ledger_root),
        )
        continued = continue_application.apply_maintainer_command(
            options=GitHubWriteOptions(auto_review=True, github_writes=True),
            oidc_token=None,
            repository="owner/repo",
            repository_id=136,
            pull_request=136,
            head_sha=steps[-1].head_sha,
            body="@sensei review continue",
            actor_login="maintainer",
            actor_type="User",
            association="OWNER",
            app_slug="reviewsensei[bot]",
        )
        command_events.append(
            f"{continued.action}:{'applied' if continued.applied else 'ignored'}"
        )
    cutover_unmet = observed_cutover_gaps(
        events=events,
        shadow_isolated=shadow_isolated,
        command_events=command_events,
        expected_material_finding_ids=expected_material_finding_ids,
        observed_material_finding_ids=observed_material_finding_ids,
        duplicate_findings=duplicate_findings,
        reopened_findings=reopened_findings,
        contradictions=contradictions,
        source_identity=report_identity.source_identity,
        package_identity=report_identity.package_identity,
        workflow_identity=report_identity.workflow_identity,
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
                if not observed_material_finding_ids
                else len(expected_material_finding_ids & observed_material_finding_ids)
                / len(observed_material_finding_ids)
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
        cutover_status="passed" if not cutover_unmet else "not_ready",
        unmet_criteria=tuple(cutover_unmet),
    )
    return report
