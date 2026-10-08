"""Reassessment adapter for the shared evidence planner and executor."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from .budgets import ProviderCapabilities, ReviewWorkBudgets, provider_output_tokens
from .errors import ReviewInputError
from .evidence import EvidenceBundle, evidence_digest
from .execution import WorkExecution, execute_plan
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
from .planning import WorkBatch, WorkRequirement, plan_continuation, plan_work
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


@dataclass(frozen=True)
class HumanAssessmentWork:
    reply: HumanAssessmentReply
    execution: WorkExecution[HumanAssessmentReply]
    inventory: PendingHumanReview
    scope_execution: WorkExecution[HumanAssessmentReply] | None = None
    discovery: ReviewRun | None = None


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
) -> HumanAssessmentWork:
    validate_bounded_text(
        source_body, MAX_HUMAN_SOURCE_BYTES, label="human reply", allow_empty=False
    )
    if bundle.snapshot.base_sha != pending.base_sha or not bundle.snapshot.head_sha:
        raise ReviewInputError("human assessment exact snapshot is missing")
    tracker = tracker or ResourceBudgetTracker(ResourceBudget.create())
    if capabilities is not None:
        selected_model = (
            model if getattr(provider, "allow_model_override", True) else None
        ) or provider.model
        capabilities.require_identity(provider_name=provider.name, model=selected_model)
    budgets = work_budgets.effective(
        limits=DEFAULT_REVIEW_LIMITS,
        resource=tracker.budget,
        mode="reassessment",
        capabilities=capabilities,
        output_tokens=provider_output_tokens(provider),
    )
    records = bundle.by_path()

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
        )

    def validate(response: ProviderResponse, batch: WorkBatch) -> HumanAssessmentReply:
        return HumanAssessmentService._parse(
            response,
            pending=inventory(batch),
            source_body=source_body,
            diff_context=batch.diff_context,
            allow_context_requests=True,
            allowed_context_paths=set(records),
        )

    def encode_reply(value: HumanAssessmentReply) -> object:
        return {
            "body": value.body,
            "assessments": [item.to_dict() for item in value.decisions],
            "context_requests": [item.to_dict() for item in value.context_requests],
        }

    plan = plan_work(
        "reassessment",
        bundle,
        requirements,
        budgets=budgets,
        render=render,
        max_batches=max(1, tracker.budget.max_provider_calls),
        correction=_CORRECTION,
        authority_digest=authority_digest,
    )
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
    if requests and scope_execution is None:
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
            requirements_for(current_inventory),
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
    body = f"Reassessed {len(decisions)} findings for the current head: {len(updated.resolved) - len(pending.resolved)} addressed or safely dismissed; {len(updated.pending)} remain pending. All approval requirements still apply."
    if execution.pending:
        body += " Some required evidence or provider work could not be completed within the configured bounds; those findings remain pending."
    reply = HumanAssessmentReply(body, decisions)
    if scope_execution is not None and any(
        request.kind == "discovery"
        for item in scope_execution.completed
        for request in item.value.context_requests
    ):
        body += " Broader discovery was requested; the affected concerns remain pending until a new full result is reconciled and published through the trusted review flow."
        reply = HumanAssessmentReply(body, decisions)
    result = HumanAssessmentWork(
        reply, execution, current_inventory, scope_execution, discovery
    )
    validate_work(
        result,
        pending=pending,
        bundle=bundle,
        source_body=source_body,
        authority_digest=authority_digest,
    )
    return result


def validate_work(
    work: HumanAssessmentWork,
    *,
    pending: PendingHumanReview,
    bundle: EvidenceBundle,
    source_body: str,
    authority_digest: str,
) -> None:
    """Revalidate every accepted decision against its original complete batch."""
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
            )
            decisions.append(decision)
    if tuple(decisions) != work.reply.decisions:
        raise ReviewInputError("human assessment aggregate decisions conflict")
    pending.apply(work.reply.decisions)
