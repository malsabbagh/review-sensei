"""Reassessment adapter for the shared evidence planner and executor."""

from __future__ import annotations

import json
from dataclasses import dataclass

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
from .planning import WorkBatch, WorkRequirement, plan_work
from .providers.base import ReviewProvider
from .validation import DEFAULT_REVIEW_LIMITS, validate_bounded_text

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
        "human_assessment_unknown_finding",
        "human_assessment_duplicate_decision",
    }
)


@dataclass(frozen=True)
class HumanAssessmentWork:
    reply: HumanAssessmentReply
    execution: WorkExecution[HumanAssessmentReply]


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
    requirements = tuple(
        WorkRequirement(
            item.fingerprint,
            tuple(
                records[path].evidence_id
                if path in records
                else evidence_digest({"missing_path": path})
                for path in item.evidence_paths
            ),
        )
        for item in pending.pending
    )
    by_id = {item.fingerprint: item for item in pending.pending}

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
        )

    def validate(response: ProviderResponse, batch: WorkBatch) -> HumanAssessmentReply:
        return HumanAssessmentService._parse(
            response,
            pending=inventory(batch),
            source_body=source_body,
            diff_context=batch.diff_context,
        )

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
                        }
                    ),
                    provider.name,
                ),
                batch,
            )
            == value
        ),
    )
    decisions = tuple(
        decision
        for completed in execution.completed
        for decision in completed.value.decisions
    )
    updated = pending.apply(decisions)
    body = f"Reassessed {len(decisions)} findings for the current head: {len(updated.resolved) - len(pending.resolved)} addressed or safely dismissed; {len(updated.pending)} remain pending. All approval requirements still apply."
    if execution.pending:
        body += " Some required evidence or provider work could not be completed within the configured bounds; those findings remain pending."
    reply = HumanAssessmentReply(body, decisions)
    result = HumanAssessmentWork(reply, execution)
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
    by_id = {item.fingerprint: item for item in pending.pending}
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
