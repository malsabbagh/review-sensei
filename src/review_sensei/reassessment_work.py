"""Reassessment adapter for the shared evidence planner and executor."""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from .assessment_queue import AssessmentQueue
from .budgets import ProviderCapabilities, ReviewWorkBudgets, provider_output_tokens
from .errors import ReviewInputError
from .evidence import EvidenceBundle, evidence_digest
from .execution import WorkCheckpointStore, WorkExecution, execute_plan
from .human_assessment import (
    MAX_HUMAN_SOURCE_BYTES,
    HumanAssessmentReply,
    HumanAssessmentService,
    HumanAssessmentValidationError,
    PendingHumanReview,
    validate_assessment_evidence,
)
from .models import ProviderRequest, ProviderResponse
from .outcomes import ResourceBudget, ResourceBudgetTracker
from .planning import (
    ReviewWorkPlan,
    WorkBatch,
    WorkRequirement,
    plan_continuation,
    plan_work,
)
from .providers.base import ReviewProvider
from .validation import DEFAULT_REVIEW_LIMITS, validate_bounded_text

if TYPE_CHECKING:
    from .service import ReviewRun, ReviewService
    from .work_recovery import WorkRecoveryStore

_CORRECTION = "The previous response failed validation. Return fresh strict JSON using only this batch's finding fingerprints, with concrete rationale and verbatim evidence from the supplied human reply and current file patches. Unsupported findings remain unresolved."

_REPAIRABLE_VALIDATION = frozenset(
    {
        "human_assessment_invalid_json",
        "human_assessment_reply_fields_invalid",
        "human_assessment_reply_body_invalid",
        "human_assessment_decisions_invalid",
        "human_assessment_decision_fields_invalid",
        "human_assessment_decision_value_invalid",
        "human_assessment_decision_text_invalid",
    }
)


def _feedback_handoff(
    feedback: object | None,
    bundle: EvidenceBundle,
) -> tuple[dict[str, Any], str | None]:
    """Pass the frozen D value whole to B's reviewed assessment codecs.

    Optional import keeps legacy callers usable before the dependency rollout.
    Digests are bindings only; the host still authenticates every source.
    """
    if feedback is None:
        return {}, None
    try:
        model = importlib.import_module(".feedback", __package__).FeedbackSelection
    except ImportError:
        raise ReviewInputError("complete feedback assessment is unavailable") from None
    if not isinstance(feedback, model):
        raise ReviewInputError("complete feedback selection is invalid")
    selected: Any = feedback
    snapshot = bundle.snapshot
    if (
        selected.repository != snapshot.repository
        or selected.pull_request != snapshot.pull_request
        or selected.base_sha != snapshot.base_sha
        or selected.head_sha != snapshot.head_sha
    ):
        raise ReviewInputError("complete feedback snapshot authority changed")
    return {"feedback": selected}, selected.digest


def _feedback_authority(authority: str, feedback_digest: str | None) -> str:
    if feedback_digest is None:
        return authority
    return evidence_digest(
        {
            "domain": "reviewsensei:assessment-feedback:v1",
            "authority": authority,
            "feedback": feedback_digest,
        }
    )


@dataclass(frozen=True)
class HumanAssessmentWork:
    reply: HumanAssessmentReply
    execution: WorkExecution[HumanAssessmentReply]
    inventory: PendingHumanReview
    scope_execution: WorkExecution[HumanAssessmentReply] | None = None
    discovery: ReviewRun | None = None
    queue: AssessmentQueue | None = None
    selected_ids: tuple[str, ...] | None = None
    admitted_queue: AssessmentQueue | None = None
    feedback_digest: str | None = None

    def apply_to(self, current: PendingHumanReview) -> PendingHumanReview:
        """Union a revalidated receipt into latest resolutions without reopening.

        Hosts still validate_work against current authority and fence the write.
        Receipt replay can include a decision the latest inventory already has.
        """
        from .assessment_queue import inventory_digest

        if inventory_digest(current) != inventory_digest(self.inventory):
            raise ReviewInputError("assessment receipt inventory is stale")
        return current.apply(
            tuple(
                item
                for item in self.reply.decisions
                if item.fingerprint not in current.resolved
            )
        )


def reassess(
    *,
    provider: ReviewProvider,
    pending: PendingHumanReview,
    bundle: EvidenceBundle,
    source_body: str,
    authority_digest: str,
    work_budgets: ReviewWorkBudgets,
    model: str | None = None,
    capabilities: ProviderCapabilities | None = None,
    tracker: ResourceBudgetTracker | None = None,
    prior: HumanAssessmentWork | None = None,
    broader_service: ReviewService | None = None,
    recovery: WorkRecoveryStore | None = None,
    queue: AssessmentQueue | None = None,
    targets: tuple[str, ...] | None = None,
    checkpoint: WorkCheckpointStore | None = None,
    feedback: object | None = None,
) -> HumanAssessmentWork:
    feedback_options, feedback_digest = _feedback_handoff(feedback, bundle)
    original_authority = authority_digest
    authority_digest = _feedback_authority(authority_digest, feedback_digest)
    if feedback is None:
        validate_bounded_text(
            source_body, MAX_HUMAN_SOURCE_BYTES, label="human reply", allow_empty=False
        )
    else:
        selected_targets = feedback_options["feedback"].target_ids
        if selected_targets:
            if targets is not None and targets != selected_targets:
                raise ReviewInputError("complete feedback target selection changed")
            targets = selected_targets
    if bundle.snapshot.base_sha != pending.base_sha or not bundle.snapshot.head_sha:
        raise ReviewInputError("human assessment exact snapshot is missing")
    tracker = tracker or ResourceBudgetTracker(ResourceBudget.create())
    if capabilities is not None:
        selected_model = (
            model if getattr(provider, "allow_model_override", True) else None
        ) or provider.model
        capabilities.require_provider(provider, model=selected_model)
    budgets = work_budgets.effective(
        limits=DEFAULT_REVIEW_LIMITS,
        resource=tracker.budget,
        mode="reassessment",
        capabilities=capabilities,
        output_tokens=provider_output_tokens(provider),
    )
    records = bundle.by_path()
    latest_pending = pending
    latest_queue = queue
    if targets is not None and queue is None:
        raise ReviewInputError("targeted assessment requires a current complete queue")
    if queue is not None:
        prepare = getattr(checkpoint, "prepare_queue", None)
        if not callable(prepare):
            raise ReviewInputError(
                "queued assessment requires durable original admission"
            )
        queue = prepare(queue, pending, bundle.snapshot)
        pending = replace(pending, resolved=queue.resolved_ids)
        queue.require_current(pending, bundle.snapshot)
    selected_ids = (
        queue.select(
            targets=targets,
            limit=250,
        )
        if queue is not None
        else None
    )

    def requirements_for(current: PendingHumanReview) -> tuple[WorkRequirement, ...]:
        return tuple(
            WorkRequirement(
                item.fingerprint,
                tuple(
                    records[path].evidence_id
                    if path in records
                    else evidence_digest({"missing_path": path})
                    for path in item.evidence_paths
                ),
            )
            for item in current.pending
        )

    current_inventory = pending if prior is None else prior.inventory
    requirements = requirements_for(current_inventory)
    by_id = {item.fingerprint: item for item in current_inventory.pending}

    def inventory(batch: WorkBatch) -> PendingHumanReview:
        return PendingHumanReview(
            pending.base_sha,
            tuple(by_id[identity] for identity in batch.requirement_ids),
        )

    def render(batch: WorkBatch) -> ProviderRequest:
        assert bundle.snapshot.head_sha is not None
        return HumanAssessmentService._request(
            head_sha=bundle.snapshot.head_sha,
            pending=inventory(batch),
            source_body=source_body,
            diff_context=batch.diff_context,
            model=model,
            max_prompt_bytes=budgets.batch_prompt_bytes,
            max_response_bytes=budgets.batch_output_bytes,
            max_output_tokens=budgets.max_output_tokens,
            allow_context_requests=True,
            **feedback_options,
        )

    def validate(response: ProviderResponse, batch: WorkBatch) -> HumanAssessmentReply:
        return HumanAssessmentService._parse(
            response,
            pending=inventory(batch),
            source_body=source_body,
            diff_context=batch.diff_context,
            allow_context_requests=True,
            allowed_context_paths=set(records),
            **feedback_options,
        )

    def encode_reply(value: HumanAssessmentReply) -> object:
        return {
            "body": value.body,
            "assessments": [item.to_dict() for item in value.decisions],
            "context_requests": [item.to_dict() for item in value.context_requests],
        }

    admission_failures: dict[str, str] = {}
    if selected_ids is not None:
        eligible = []
        requirements_by_id = {item.identity: item for item in requirements}
        evidence_by_id = bundle.by_id()
        for identity in selected_ids:
            required = requirements_by_id[identity]
            if not bundle.enumeration_complete or any(
                key not in evidence_by_id or not evidence_by_id[key].complete
                for key in required.evidence_ids
            ):
                admission_failures[identity] = "required-evidence-missing"
                continue
            batch = WorkBatch(
                "reassessment",
                (required,),
                tuple(
                    sorted(
                        (evidence_by_id[key] for key in required.evidence_ids),
                        key=lambda item: item.path,
                    )
                ),
            )
            try:
                request = render(batch)
                fits = len(
                    batch.diff_context.encode("utf-8")
                ) <= budgets.batch_diff_bytes and budgets.fits_prompt(
                    request.prompt + "\n\n" + _CORRECTION,
                    output_tokens=request.max_output_tokens,
                )
            except ReviewInputError:
                fits = False
            if not fits:
                admission_failures[identity] = "required-evidence-oversized"
                continue
            eligible.append(identity)
        # Admission-blocked obligations remain unvisited in the complete queue,
        # but cannot occupy a dispatch window and starve admissible late work.
        selected_ids = tuple(
            eligible[
                : budgets.max_findings_per_batch * tracker.budget.max_provider_calls
            ]
        )

    plan = plan_work(
        "reassessment",
        bundle,
        tuple(
            item
            for item in requirements
            if selected_ids is None or item.identity in selected_ids
        ),
        budgets=budgets,
        render=render,
        max_batches=max(1, tracker.budget.max_provider_calls),
        correction=_CORRECTION,
        authority_digest=authority_digest,
    )

    def retain_complete_plan(selected: ReviewWorkPlan) -> ReviewWorkPlan:
        if selected_ids is None:
            return selected
        return replace(
            selected,
            requirements=tuple(
                sorted(
                    requirements_for(current_inventory), key=lambda item: item.identity
                )
            ),
            unprocessed=tuple(
                sorted(
                    (
                        *selected.unprocessed,
                        *(
                            (
                                item.fingerprint,
                                admission_failures.get(
                                    item.fingerprint,
                                    "target-not-selected"
                                    if targets is not None
                                    else "queue-deferred",
                                ),
                            )
                            for item in current_inventory.pending
                            if selected_ids is not None
                            and item.fingerprint not in selected_ids
                        ),
                    )
                )
            ),
        )

    plan = retain_complete_plan(plan)
    execution = execute_plan(
        plan,
        provider=provider,
        render=render,
        validate=validate,
        tracker=tracker,
        budgets=budgets,
        correction=_CORRECTION,
        prior=prior.execution if prior is not None else None,
        recovery=recovery,
        encode_recovery=encode_reply,
        decode_recovery=lambda value, batch: validate(
            ProviderResponse(json.dumps(value), provider.name), batch
        ),
        checkpoint=checkpoint,
        repairable_validation=lambda exc: (
            isinstance(exc, HumanAssessmentValidationError)
            and exc.diagnostic in _REPAIRABLE_VALIDATION
        ),
        revalidate_cached=lambda value, batch: (
            validate(
                ProviderResponse(
                    json.dumps(
                        {
                            "body": value.body,
                            "assessments": [
                                decision.to_dict() for decision in value.decisions
                            ],
                            "context_requests": [
                                item.to_dict() for item in value.context_requests
                            ],
                        }
                    ),
                    provider.name,
                ),
                batch,
            )
            == value
        ),
    )
    scope_execution = prior.scope_execution if prior is not None else None
    discovery = None
    requests = tuple(
        request
        for item in execution.completed
        for request in item.value.context_requests
    )
    if requests and scope_execution is None and checkpoint is None:
        scope_execution = execution
        requested = {item.reference: item for item in requests}
        findings = []
        for finding in current_inventory.findings:
            request_scope = requested.get(finding.fingerprint)
            if request_scope is not None:
                paths = tuple(
                    sorted(
                        set(finding.evidence_paths) | set(request_scope.required_paths)
                    )
                )
                finding = replace(finding, required_paths=paths)
            findings.append(finding)
        current_inventory = replace(current_inventory, findings=tuple(findings))
        by_id = {item.fingerprint: item for item in current_inventory.pending}
        expanded = plan_continuation(
            execution.plan,
            bundle,
            tuple(
                item
                for item in requirements_for(current_inventory)
                if selected_ids is None or item.identity in selected_ids
            ),
            reusable=tuple(
                item.batch
                for item in execution.completed
                if not item.value.context_requests
            ),
            budgets=budgets,
            render=render,
            correction=_CORRECTION,
            deferred={
                item.reference: "broader-discovery-required"
                for item in requests
                if item.kind == "discovery"
            },
        )
        expanded = retain_complete_plan(expanded)

        def validate_expansion(
            response: ProviderResponse, batch: WorkBatch
        ) -> HumanAssessmentReply:
            value = validate(response, batch)
            if value.context_requests:
                raise ReviewInputError("scope expansion wave exhausted")
            return value

        execution = execute_plan(
            expanded,
            provider=provider,
            render=render,
            validate=validate_expansion,
            tracker=tracker,
            budgets=budgets,
            correction=_CORRECTION,
            prior=scope_execution,
            recovery=recovery,
            encode_recovery=encode_reply,
            decode_recovery=lambda value, batch: validate_expansion(
                ProviderResponse(json.dumps(value), provider.name), batch
            ),
            revalidate_cached=lambda value, batch: (
                not value.context_requests
                and validate(
                    ProviderResponse(
                        json.dumps(
                            {
                                "body": value.body,
                                "assessments": [
                                    item.to_dict() for item in value.decisions
                                ],
                            }
                        ),
                        provider.name,
                    ),
                    batch,
                )
                == value
            ),
            repairable_validation=lambda exc: (
                isinstance(exc, HumanAssessmentValidationError)
                and exc.diagnostic in _REPAIRABLE_VALIDATION
            ),
        )
        if (
            broader_service is not None
            and bundle.enumeration_complete
            and all(record.complete for record in bundle.records)
            and any(item.kind == "discovery" for item in requests)
        ):
            from .models import ReviewRequest

            if broader_service.work_budgets != work_budgets:
                raise ReviewInputError("broader discovery work policy changed")
            discovery = broader_service.run(
                ReviewRequest(
                    diff="".join(record.diff for record in bundle.records),
                    repository=bundle.snapshot.repository,
                    pull_request_number=bundle.snapshot.pull_request,
                    base_sha=bundle.snapshot.base_sha,
                    head_sha=bundle.snapshot.head_sha,
                ),
                tracker=tracker,
                work_recovery=recovery,
                allow_work_expansion=False,
            )
    decisions = tuple(
        decision
        for completed in execution.completed
        for decision in completed.value.decisions
    )
    updated = current_inventory.apply(decisions)
    updated_queue = None
    if queue is not None:
        updated_queue = queue.advance(
            visited=execution.attempted_ids, resolved=updated.resolved
        )
        if (
            updated_queue.inventory_digest != queue.inventory_digest
            or current_inventory != pending
        ):
            updated_queue = updated_queue.reconcile(updated)
        if latest_queue is not None:
            # Replays return old accepted results but must never reopen or
            # duplicate a concurrent resolution already in latest authority.
            updated_queue = latest_queue.advance(
                visited=tuple(
                    item
                    for item in execution.attempted_ids
                    if item in latest_queue.pending_order
                ),
                resolved=tuple(
                    sorted(set(latest_pending.resolved) | set(updated.resolved))
                ),
            )
    latest_resolved = set(latest_pending.resolved) | set(updated.resolved)
    body = f"Reassessed {len(decisions)} findings for the current head: {len(latest_resolved) - len(latest_pending.resolved)} addressed or safely dismissed; {len(current_inventory.findings) - len(latest_resolved)} remain pending. All approval requirements still apply."
    if not bundle.enumeration_complete:
        body = "Current changed-file enumeration is incomplete. " + body
    if execution.pending:
        body += " Some required evidence or provider work could not be completed within the configured bounds; those findings remain pending."
    if selected_ids is not None:
        assessed = {item.fingerprint for item in decisions}
        body += f" {len(current_inventory.pending) - len(assessed)} pending findings were not assessed in this operation."
    if requests and checkpoint is not None:
        body += " Additional scope was requested; durable scope integration is required before those concerns can be reassessed."
    reply = HumanAssessmentReply(body, decisions)
    if scope_execution is not None and any(
        request.kind == "discovery"
        for item in scope_execution.completed
        for request in item.value.context_requests
    ):
        body += " Broader discovery was requested; the affected concerns remain pending until a new full result is reconciled and published through the trusted review flow."
        reply = HumanAssessmentReply(body, decisions)
    result = HumanAssessmentWork(
        reply,
        execution,
        current_inventory,
        scope_execution,
        discovery,
        updated_queue,
        selected_ids,
        queue,
        feedback_digest,
    )
    validate_work(
        result,
        pending=latest_pending,
        bundle=bundle,
        source_body=source_body,
        authority_digest=original_authority,
        feedback=feedback,
    )
    return result


def validate_work(
    work: HumanAssessmentWork,
    *,
    pending: PendingHumanReview,
    bundle: EvidenceBundle,
    source_body: str,
    authority_digest: str,
    feedback: object | None = None,
) -> None:
    """Revalidate every accepted decision against its original complete batch."""
    feedback_options, feedback_digest = _feedback_handoff(feedback, bundle)
    if work.feedback_digest != feedback_digest:
        raise ReviewInputError("complete feedback receipt authority changed")
    authority_digest = _feedback_authority(authority_digest, feedback_digest)
    latest = pending
    if work.admitted_queue is not None:
        from .assessment_queue import inventory_digest

        if (
            work.admitted_queue.snapshot != bundle.snapshot
            or work.admitted_queue.inventory_digest != inventory_digest(pending)
            or not set(work.admitted_queue.resolved_ids) <= set(pending.resolved)
            or work.queue is None
            or work.selected_ids is None
            or len(set(work.selected_ids)) != len(work.selected_ids)
            or not set(work.selected_ids) <= set(work.admitted_queue.pending_order)
            or any(
                not set(item.requirement_ids) <= set(work.selected_ids)
                for item in work.execution.plan.batches
            )
        ):
            raise ReviewInputError("assessment queue admission authority is invalid")
        pending = replace(pending, resolved=work.admitted_queue.resolved_ids)
    elif work.queue is not None or work.selected_ids is not None:
        raise ReviewInputError("assessment queue admission receipt is missing")
    plan = work.execution.plan
    if (
        plan.mode != "reassessment"
        or plan.bundle != bundle
        or plan.authority_digest != authority_digest
        or (
            not bundle.enumeration_complete
            and any(item.decision != "unresolved" for item in work.reply.decisions)
        )
    ):
        raise ReviewInputError(
            "human assessment aggregate evidence identity is invalid"
        )
    if (
        work.inventory.base_sha != pending.base_sha
        or work.inventory.resolved != pending.resolved
        or len(work.inventory.findings) != len(pending.findings)
    ):
        raise ReviewInputError("human assessment continuation inventory is invalid")
    widened = {item.fingerprint: set(item.evidence_paths) for item in pending.findings}
    if work.scope_execution is not None:
        if (
            work.scope_execution.plan.bundle != bundle
            or work.scope_execution.plan.authority_digest != authority_digest
            or work.scope_execution.plan.mode != "reassessment"
            or work.scope_execution.plan.budget_digest != plan.budget_digest
            or work.scope_execution.tracker_identity != work.execution.tracker_identity
        ):
            raise ReviewInputError("human assessment scope authority is invalid")
        original = {item.fingerprint: item for item in pending.pending}
        for completed in work.scope_execution.completed:
            subset = PendingHumanReview(
                pending.base_sha,
                tuple(original[key] for key in completed.batch.requirement_ids),
            )
            validated = HumanAssessmentService._parse(
                ProviderResponse(
                    json.dumps(
                        {
                            "body": completed.value.body,
                            "assessments": [
                                item.to_dict() for item in completed.value.decisions
                            ],
                            "context_requests": [
                                item.to_dict()
                                for item in completed.value.context_requests
                            ],
                        }
                    ),
                    "retained",
                ),
                pending=subset,
                source_body=source_body,
                diff_context=completed.batch.diff_context,
                allow_context_requests=True,
                allowed_context_paths=set(bundle.by_path()),
                **feedback_options,
            )
            if validated != completed.value:
                raise ReviewInputError("human assessment scope receipt is invalid")
            for request in validated.context_requests:
                widened[request.reference].update(request.required_paths)
    for old, new in zip(pending.findings, work.inventory.findings, strict=True):
        if (
            old.fingerprint != new.fingerprint
            or old.path != new.path
            or old.body != new.body
            or set(new.evidence_paths) != widened[old.fingerprint]
        ):
            raise ReviewInputError(
                "human assessment continuation changed finding authority"
            )
    by_id = {item.fingerprint: item for item in work.inventory.pending}
    if set(item.identity for item in plan.requirements) != set(by_id):
        raise ReviewInputError("human assessment aggregate inventory is invalid")
    evidence_by_id = bundle.by_id()
    for requirement in plan.requirements:
        present_paths = {
            evidence_by_id[key].path
            for key in requirement.evidence_ids
            if key in evidence_by_id
        }
        expected_paths = set(by_id[requirement.identity].evidence_paths)
        if not present_paths <= expected_paths or len(requirement.evidence_ids) != len(
            expected_paths
        ):
            raise ReviewInputError("human assessment required file group is invalid")
    decisions = []
    completed_ids: set[str] = set()
    for completed in work.execution.completed:
        batch = completed.batch
        if batch not in plan.batches or completed_ids.intersection(
            batch.requirement_ids
        ):
            raise ReviewInputError("human assessment aggregate batches conflict")
        completed_ids.update(batch.requirement_ids)
        if any(
            not record.complete or record.provenance != "github-files"
            for record in batch.records
        ):
            raise ReviewInputError("human assessment complete file evidence is missing")
        selected = {record.path: record for record in batch.records}
        for decision in completed.value.decisions:
            if decision.fingerprint not in batch.requirement_ids or not set(
                by_id[decision.fingerprint].evidence_paths
            ) <= set(selected):
                raise ReviewInputError("human assessment batch finding is invalid")
            validate_assessment_evidence(
                decision,
                by_id[decision.fingerprint],
                source_body=source_body,
                diff_context=batch.diff_context,
                **feedback_options,
            )
            decisions.append(decision)
    if tuple(decisions) != work.reply.decisions:
        raise ReviewInputError("human assessment aggregate decisions conflict")
    pending.apply(work.reply.decisions)
    if work.queue is not None and work.admitted_queue is not None:
        resolved = tuple(
            sorted(
                set(latest.resolved) | set(pending.apply(work.reply.decisions).resolved)
            )
        )
        if (
            work.queue.inventory_ids != work.admitted_queue.inventory_ids
            or work.queue.snapshot != bundle.snapshot
            or work.queue.resolved_ids != resolved
            or work.queue.inventory_digest != work.admitted_queue.inventory_digest
        ):
            raise ReviewInputError("assessment queue progress changed obligations")
