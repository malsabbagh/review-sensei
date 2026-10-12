"""Full discovery adapter using the same planner/executor as reassessment."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING

from .budgets import provider_output_tokens
from .context import (
    PROMPT_CONTEXT_POLICY,
    IncrementalReviewPlan,
    PromptContextAdmission,
    PromptContextDeadline,
    ReviewContextCacheKey,
    admit_prompt_context,
)
from .diff import analyze_diff
from .document_context import DocumentSelectionReport
from .errors import ReviewFormatError, ReviewInputError
from .evidence import EvidenceBundle, EvidenceRecord, EvidenceSnapshot, evidence_digest
from .execution import CompletedBatch, execute_plan
from .hunk_subdivision import (
    HUNK_SUBDIVISION_POLICY,
    SubdivisionAllowance,
    subdivide_hunk,
)
from .models import (
    LearningProposal,
    ProviderRequest,
    ProviderResponse,
    ReviewComment,
    ReviewRequest,
    ReviewResult,
)
from .outcomes import ResourceBudgetTracker
from .planning import (
    WorkBatch,
    WorkRequirement,
    _classify_file,
    _coverage_for,
    _hunk_record,
    _reconcile_file_outcomes_from_hunks,
    plan_work,
)
from .providers.base import ReviewProvider
from .scope import CONTEXT_REQUEST_INSTRUCTION, ContextRequest, parse_context_requests
from .service import _PROVIDER_OUTPUT_CORRECTION, _ValidatedStageOutput

if TYPE_CHECKING:
    from .service import ReviewRun, ReviewService
    from .work_recovery import WorkRecoveryStore


@dataclass(frozen=True)
class DiscoveryBatchOutput(_ValidatedStageOutput):
    context_requests: tuple[ContextRequest, ...] = ()


PENDING_APPENDIX_RESERVE = 2048


def _context_digest_line(document: dict[str, object] | None) -> str:
    # Fixed size, so the published summary stays bound to the executed
    # document selection without growing with the call count.
    if document is None:
        return ""
    return f"\n\nDocument context admission sha256: {evidence_digest(document)}."


CONTEXT_DIGEST_LINE_BYTES = len(_context_digest_line({}).encode("utf-8"))


def context_admission_document(
    entries: list[dict[str, object]],
) -> dict[str, object] | None:
    if not entries:
        return None
    originals: dict[str, object] = {}
    batches = []
    for entry in entries:
        inventories = entry["original_inventories"]
        assert isinstance(inventories, list)
        for inventory in inventories:
            originals[evidence_digest(inventory)] = inventory
        batches.append(
            {
                key: value
                for key, value in entry.items()
                if key not in {"original_inventories", "selected_inventories"}
            }
            | {
                "executed_selection_sha256": evidence_digest(
                    entry["selected_inventories"]
                )
            }
        )
    return {
        "policy": PROMPT_CONTEXT_POLICY,
        "original_inventories": list(originals.values()),
        "batches": batches,
    }


def discover(
    service: ReviewService,
    request: ReviewRequest,
    *,
    snapshot_request: ReviewRequest,
    tracker: ResourceBudgetTracker,
    incremental: IncrementalReviewPlan | None,
    current_key: ReviewContextCacheKey | None,
    profile: str,
    provider: ReviewProvider,
    recovery: WorkRecoveryStore | None = None,
    allow_expansion: bool = True,
) -> ReviewRun:
    configured = {category.id for category in service.review_categories}
    if request.active_category_ids is not None and not set(
        request.active_category_ids
    ).issubset(configured):
        raise ReviewInputError(
            "active review category ids must be declared by a configured stage"
        )
    tracker.budget.validate_against_limits(request.limits)
    work = request.work_budget
    tracker.budget = replace(
        tracker.budget,
        max_provider_calls=min(
            tracker.budget.max_provider_calls, work.max_provider_calls
        ),
    )
    analysis = analyze_diff(
        request.diff,
        limits=request.limits,
        allow_incomplete=True,
        max_bytes=work.max_total_diff_bytes,
        max_lines=work.max_total_diff_lines,
        max_files=work.max_total_files,
        max_hunks=work.max_total_hunks,
    )
    snapshot = EvidenceSnapshot(
        request.repository,
        request.pull_request_number,
        snapshot_request.base_sha,
        snapshot_request.head_sha,
    )
    coverage_decision = service._coverage_decision(
        request, incremental=incremental, current_key=current_key, profile=profile
    )
    file_outcomes: dict[str, tuple[str, str | None]] = {}
    hunk_outcomes: dict[int, tuple[str, str | None]] = {}
    records = []
    for record in analysis.file_records:
        outcome, reason = _classify_file(
            record, analysis=analysis, limits=request.limits
        )
        for path in record.coverage_paths:
            file_outcomes[path] = (
                ("reviewed", None) if outcome == "reviewable" else (outcome, reason)
            )
        for hunk in record.hunks:
            hunk_outcomes[hunk.index] = (
                ("reviewed", None) if outcome == "reviewable" else (outcome, reason)
            )
        if outcome == "reviewable":
            records.append(record)
    summaries: list[str] = []
    comments: list[ReviewComment] = []
    proposals: list[LearningProposal] = []
    stage_summary: dict[str, str] = {}
    failed = False
    comment_stage = False
    scope_wave_used = not allow_expansion
    context_diagnostics: list[dict[str, object]] = []
    refusal_reasons: dict[str, int] = {}
    mandatory_refused_documents: dict[str, int] = {}
    context_enabled = any(
        context.documents or context.document_selection
        for context in request.lens_contexts
    )
    # Document-bearing unified runs use full current diff authority. A later
    # optional omission cannot inherit an earlier incremental closure/cache.
    if context_enabled and incremental is not None:
        coverage_decision = replace(
            coverage_decision,
            mode="fallback-full",
            skip_provider=False,
            reviewed_paths=analysis.changed_paths,
            reviewed_paths_bound=None,
            evidence_confirmed=(),
        )

    subdivision_allowance = SubdivisionAllowance()
    for stage in service.stages:
        categories = tuple(
            category
            for category in stage.categories
            if request.active_category_ids is None
            or category.id in request.active_category_ids
        )
        if stage.categories and not categories:
            stage_summary[stage.name] = "skipped"
            failed = True
            continue
        comment_stage = comment_stage or "comments" in stage.outputs
        stage_provider = service._provider_for_stage(stage, default=provider)
        if service.capabilities is not None:
            selected_model = (
                request.model
                if getattr(stage_provider, "allow_model_override", True)
                else None
            ) or stage_provider.model
            service.capabilities.require_provider(stage_provider, model=selected_model)
        budgets = service.work_budgets.effective(
            limits=request.limits,
            resource=tracker.budget,
            mode="discovery",
            capabilities=service.capabilities,
            output_tokens=provider_output_tokens(stage_provider),
        )
        suffix = "\n" + CONTEXT_REQUEST_INSTRUCTION
        correction_bytes = len(("\n\n" + _PROVIDER_OUTPUT_CORRECTION).encode("utf-8"))
        frame_bytes = len(suffix.encode("utf-8")) + correction_bytes
        admissions: dict[str, PromptContextAdmission] = {}

        def check_deadline() -> None:
            if tracker.remaining_seconds() <= 0:
                raise PromptContextDeadline(
                    "original review planning deadline exhausted"
                )

        def context_diagnostic(
            batch: WorkBatch, admission: PromptContextAdmission
        ) -> dict[str, object]:
            value = admission.diagnostic_document()
            return {"stage": stage.name, "batch_id": batch.batch_id, **value}

        def render(batch: WorkBatch) -> ProviderRequest:
            # All metadata/documents and JSON escaping are included before
            # admission. ReviewRequest also revalidates line/file/hunk limits.
            batch_request = replace(
                request, diff=batch.diff_context, orchestrate_large_changes=False
            )
            batch_analysis = analyze_diff(batch.diff_context, limits=request.limits)
            admission = admit_prompt_context(
                batch_request,
                active_category_ids=tuple(category.id for category in categories),
                changed_paths=batch.paths,
                render=lambda selected: (
                    service._format_prompt(
                        stage,
                        selected,
                        active_categories=categories,
                        coverage_mode=coverage_decision.mode,
                        reviewed_paths=batch.paths,
                        related_paths=coverage_decision.related_paths,
                        max_prompt_bytes=max(
                            0, budgets.batch_prompt_bytes - frame_bytes
                        ),
                        analysis=batch_analysis,
                    )
                    + suffix
                ),
                fits=lambda prompt: budgets.fits_prompt(
                    prompt + "\n\n" + _PROVIDER_OUTPUT_CORRECTION,
                    output_tokens=budgets.max_output_tokens,
                ),
                before_probe=check_deadline,
            )
            if len(admissions) >= 8:
                admissions.pop(next(iter(admissions)))
            admissions[batch.batch_id] = admission
            return ProviderRequest(
                prompt=admission.prompt,
                model=request.model,
                limits=request.limits,
                max_prompt_bytes=budgets.batch_prompt_bytes,
                max_response_bytes=budgets.batch_output_bytes,
                max_output_tokens=budgets.max_output_tokens,
            )

        evidence = []
        requirements = []
        for record in records:
            whole = EvidenceRecord.from_diff_record(record, snapshot)
            requirement = WorkRequirement(
                "file:" + whole.path,
                (whole.evidence_id,),
                tuple(hunk.index for hunk in record.hunks),
            )
            singleton = EvidenceBundle(
                snapshot, (whole,), analysis.enumeration_complete
            )
            probe = plan_work(
                "discovery",
                singleton,
                (requirement,),
                budgets=budgets,
                render=render,
                correction=_PROVIDER_OUTPUT_CORRECTION,
            )
            if probe.batches or not record.hunks:
                evidence.append(whole)
                requirements.append(requirement)
            else:
                # Discovery may split exhaustive authoritative diff hunks.
                # Reassessment never uses this path: stored v1 findings have
                # no authoritative hunk location.
                for hunk in record.hunks:
                    part = _hunk_record(record, hunk)
                    fragment = replace(
                        EvidenceRecord.from_diff_record(part, snapshot),
                        provenance="discovery-hunk",
                        supplied_complete=False,
                    )

                    def fragment_requirement(piece: EvidenceRecord) -> WorkRequirement:
                        identity = f"hunk:{hunk.index}:{piece.path}"
                        if piece.fragment is not None:
                            identity += ":segment:" + evidence_digest(
                                piece.fragment.to_dict()
                            )
                        return WorkRequirement(
                            identity, (piece.evidence_id,), (hunk.index,)
                        )

                    def admit_fragment(piece: EvidenceRecord) -> bool:
                        try:
                            check_deadline()
                        except PromptContextDeadline:
                            return False
                        return bool(
                            plan_work(
                                "discovery",
                                EvidenceBundle(
                                    snapshot, (piece,), analysis.enumeration_complete
                                ),
                                (fragment_requirement(piece),),
                                budgets=budgets,
                                render=render,
                                correction=_PROVIDER_OUTPUT_CORRECTION,
                            ).batches
                        )

                    # Preserve existing hunk identities when they already fit.
                    # Subdivision admission uses the exact renderer, including
                    # mandatory documents, framing and correction reserve.
                    parts: tuple[EvidenceRecord, ...]
                    if admit_fragment(fragment):
                        parts = (fragment,)
                    else:
                        parts = subdivide_hunk(
                            record,
                            hunk,
                            snapshot,
                            admit=admit_fragment,
                            before_probe=check_deadline,
                            allowance=subdivision_allowance,
                        )
                    evidence.extend(parts)
                    requirements.extend(fragment_requirement(piece) for piece in parts)
        bundle = EvidenceBundle(
            snapshot, tuple(evidence), analysis.enumeration_complete
        )
        plan = plan_work(
            "discovery",
            bundle,
            requirements,
            budgets=budgets,
            render=render,
            max_batches=min(work.max_chunks, max(1, tracker.budget.max_provider_calls)),
            correction=_PROVIDER_OUTPUT_CORRECTION,
            authority_digest=evidence_digest(
                {
                    "stage": stage.name,
                    "profile": profile,
                    "provider": stage_provider.name,
                    "model": request.model,
                    "stage_prompt": stage.prompt_template,
                    "stage_outputs": stage.outputs,
                    "categories": tuple(category.id for category in categories),
                    "cache": asdict(current_key) if current_key is not None else None,
                    "context_policy": PROMPT_CONTEXT_POLICY,
                    "hunk_subdivision_policy": HUNK_SUBDIVISION_POLICY,
                    "context_inventories": [
                        context.to_prompt_dict() for context in request.lens_contexts
                    ],
                }
            ),
        )

        def validate(
            response: ProviderResponse, batch: WorkBatch
        ) -> DiscoveryBatchOutput:
            value = json.loads(response.text)
            if not isinstance(value, dict) or not set(value) <= set(stage.outputs) | {
                "context_requests"
            }:
                raise ReviewFormatError("discovery output fields are invalid")
            scope = parse_context_requests(
                value.pop("context_requests", []),
                references=set(batch.paths),
                allowed_paths=set(analysis.changed_paths),
            )
            output = service._validated_stage_output(
                json.dumps(value),
                stage=stage,
                analysis=analyze_diff(batch.diff_context, limits=request.limits),
                active_categories=categories,
                propose_learnings=request.propose_learnings,
            )
            return DiscoveryBatchOutput(
                output.summary, output.comments, output.proposals, scope
            )

        def encode_output(value: DiscoveryBatchOutput) -> object:
            fields: dict[str, object] = {}
            if "summary" in stage.outputs:
                fields["summary"] = value.summary
            if "comments" in stage.outputs:
                fields["comments"] = [item.to_dict() for item in value.comments]
            if "learning_proposals" in stage.outputs:
                fields["learning_proposals"] = [
                    item.to_dict() for item in value.proposals
                ]
            fields["context_requests"] = [
                item.to_dict() for item in value.context_requests
            ]
            return fields

        def accept(item: CompletedBatch[DiscoveryBatchOutput]) -> bool:
            output = item.value
            try:
                admission = admissions.get(item.batch.batch_id)
                if admission is None:
                    render(item.batch)
                    admission = admissions[item.batch.batch_id]
                prospective_summary = "\n\n".join([*summaries, output.summary or ""])
                if (
                    len(prospective_summary.encode("utf-8"))
                    + (CONTEXT_DIGEST_LINE_BYTES if context_enabled else 0)
                    + PENDING_APPENDIX_RESERVE
                    > request.limits.max_summary_bytes
                ):
                    return False
                ReviewResult(
                    summary=prospective_summary or "Review complete.",
                    comments=tuple([*comments, *output.comments]),
                    provider=stage_provider.name,
                    model=request.model,
                    learning_proposals=tuple([*proposals, *output.proposals]),
                    limits=request.limits,
                    review_status="partial",
                )
            except ReviewInputError:
                return False
            if output.summary:
                summaries.append(output.summary)
            comments.extend(output.comments)
            proposals.extend(output.proposals)
            if context_enabled:
                # Optional file-budget, prompt-budget, and summary trims stay
                # on this record. Mandatory context that cannot fit never
                # reaches an accepted batch.
                context_diagnostics.append(context_diagnostic(item.batch, admission))
            return True

        if coverage_decision.skip_provider:
            stage_summary[stage.name] = "skipped"
            continue
        execution = execute_plan(
            plan,
            provider=stage_provider,
            render=render,
            validate=validate,
            tracker=tracker,
            budgets=budgets,
            correction=_PROVIDER_OUTPUT_CORRECTION,
            accept=accept,
            recovery=recovery,
            encode_recovery=encode_output,
            decode_recovery=lambda value, batch: validate(
                ProviderResponse(json.dumps(value), stage_provider.name), batch
            ),
            revalidate_cached=lambda value, batch: (
                validate(
                    ProviderResponse(
                        json.dumps(encode_output(value)), stage_provider.name
                    ),
                    batch,
                )
                == value
            ),
            repairable_validation=lambda exc: isinstance(
                exc, (ReviewFormatError, json.JSONDecodeError)
            ),
        )
        scopes = tuple(
            dict.fromkeys(
                scope
                for item in execution.completed
                for scope in item.value.context_requests
            )
        )
        bridge_pending = []
        if scopes:
            if scope_wave_used:
                bridge_pending = list(scopes)
            else:
                scope_wave_used = True
                whole_records = {
                    record.canonical_path: EvidenceRecord.from_diff_record(
                        record, snapshot
                    )
                    for record in records
                }
                bridge_requirements = []
                bridge_records = {}
                for scope in scopes:
                    paths = tuple(sorted({scope.reference, *scope.required_paths}))
                    if len(paths) > 8:
                        bridge_pending.append(scope)
                        continue
                    keys = []
                    for path in paths:
                        bridge_record = whole_records.get(path)
                        if bridge_record is None or not bridge_record.complete:
                            keys.append(evidence_digest({"missing_bridge": path}))
                        else:
                            keys.append(bridge_record.evidence_id)
                            bridge_records[bridge_record.evidence_id] = bridge_record
                    bridge_requirements.append(
                        WorkRequirement(
                            "bridge:" + evidence_digest(scope.to_dict()), tuple(keys)
                        )
                    )
                bridge_bundle = EvidenceBundle(
                    snapshot,
                    tuple(bridge_records.values()),
                    analysis.enumeration_complete,
                )
                bridge_plan = plan_work(
                    "discovery",
                    bridge_bundle,
                    bridge_requirements,
                    budgets=budgets,
                    render=render,
                    correction=_PROVIDER_OUTPUT_CORRECTION,
                    authority_digest=plan.authority_digest,
                )

                def validate_bridge(
                    response: ProviderResponse, batch: WorkBatch
                ) -> DiscoveryBatchOutput:
                    output = validate(response, batch)
                    if output.context_requests:
                        raise ReviewInputError("scope expansion wave exhausted")
                    return output

                bridge = execute_plan(
                    bridge_plan,
                    provider=stage_provider,
                    render=render,
                    validate=validate_bridge,
                    tracker=tracker,
                    budgets=budgets,
                    correction=_PROVIDER_OUTPUT_CORRECTION,
                    accept=accept,
                    repairable_validation=lambda exc: isinstance(
                        exc, (ReviewFormatError, json.JSONDecodeError)
                    ),
                    recovery=recovery,
                    encode_recovery=encode_output,
                    decode_recovery=lambda value, batch: validate_bridge(
                        ProviderResponse(json.dumps(value), stage_provider.name), batch
                    ),
                    revalidate_cached=lambda value, batch: (
                        validate_bridge(
                            ProviderResponse(
                                json.dumps(encode_output(value)), stage_provider.name
                            ),
                            batch,
                        )
                        == value
                    ),
                )
                failed_bridge_ids = set(bridge.pending_ids)
                bridge_pending.extend(
                    scope
                    for scope in scopes
                    if "bridge:" + evidence_digest(scope.to_dict()) in failed_bridge_ids
                )
            for scope in bridge_pending:
                failed = True
                for path in {scope.reference, *scope.required_paths}:
                    if path in file_outcomes:
                        file_outcomes[path] = ("budget-exhausted", "chunk-failed")
                    for file_record in analysis.file_records:
                        if path in file_record.coverage_paths:
                            for bridge_hunk in file_record.hunks:
                                hunk_outcomes[bridge_hunk.index] = (
                                    "budget-exhausted",
                                    "chunk-failed",
                                )
        stage_summary[stage.name] = "partial" if execution.pending else "complete"
        if bridge_pending:
            stage_summary[stage.name] = "partial"
        failed = failed or bool(execution.pending)
        for _, reason in execution.pending:
            refusal_reasons[reason] = refusal_reasons.get(reason, 0) + 1
        if any(
            reason == "mandatory-context-and-evidence-oversized"
            for _, reason in execution.pending
        ):
            category_ids = {category.id for category in categories}
            for context in request.lens_contexts:
                if context.category_id not in category_ids:
                    continue
                report = context.document_selection
                pinned = (
                    {item.path for item in report.decisions if item.reason == "pinned"}
                    if isinstance(report, DocumentSelectionReport)
                    else {doc.path for doc in context.documents}
                )
                for doc in context.documents:
                    if doc.path in pinned:
                        mandatory_refused_documents[doc.path] = len(
                            doc.content.encode("utf-8")
                        )
        by_id = {item.identity: item for item in plan.requirements}
        evidence_by_id = bundle.by_id()
        for identity, reason in execution.pending:
            item = by_id[identity]
            # Static reason strings only; coverage's bounded public vocabulary
            # is separate from the planner's internal pending diagnostics.
            diagnostic = (
                "provider-call-budget"
                if reason == "provider_call_limit"
                else "chunk-failed"
            )
            for key in item.evidence_ids:
                pending_record = evidence_by_id.get(key)
                if pending_record is not None:
                    file_outcomes[pending_record.path] = (
                        "budget-exhausted",
                        diagnostic,
                    )
            for index in item.hunk_indexes:
                hunk_outcomes[index] = ("budget-exhausted", diagnostic)
    if not comment_stage:
        failed = True
    for path in analysis.changed_paths:
        file_outcomes.setdefault(path, ("unsupported", "incomplete-enumeration"))
    _reconcile_file_outcomes_from_hunks(
        analysis, file_outcomes=file_outcomes, hunk_outcomes=hunk_outcomes
    )
    coverage = _coverage_for(
        analysis,
        file_outcomes=file_outcomes,
        hunk_outcomes=hunk_outcomes,
        limits=request.limits,
    )
    # A finished diff stays complete when only optional documents were
    # trimmed. Unfinished required work and incomplete diff coverage do not.
    status = "partial" if failed or not coverage.fully_reviewed else "complete"
    if not comment_stage:
        status = "summary-only"
    context_admission = context_admission_document(context_diagnostics)
    refusal_summary = (
        "\n\nRequired work pending: "
        + "; ".join(
            f"{reason} ({count})" for reason, count in sorted(refusal_reasons.items())
        )
        + "."
        if refusal_reasons
        else ""
    )
    if mandatory_refused_documents:
        refusal_summary += f"\nMandatory document overflow: {len(mandatory_refused_documents)} unique document(s), {sum(mandatory_refused_documents.values())} UTF-8 payload bytes."
    try:
        result = service._finalize_result(
            request,
            summary=(
                "\n\n".join(summaries)
                or "Review work could not be completed within the configured bounds."
            )
            + _context_digest_line(context_admission)
            + refusal_summary,
            comments=tuple(comments),
            proposals=tuple(proposals),
            provider=provider.name,
            model=request.model,
            review_status=status,
            coverage=coverage_decision,
            source_context_coverage=service._source_context_coverage(request),
        )
        result = service._attach_coverage(result, coverage)
        # Complete reviewable text (including an empty text set) may coexist
        # with binary obligations. Context/stage failures never gain this flag.
        from .human_file_review import coverage_only_binary

        if not failed and comment_stage and coverage_only_binary(coverage):
            result = replace(result, coverage_only_partial=True)
    except ReviewInputError:
        return service._finish_run(
            tracker=tracker,
            status="provider_failed",
            stage_summary=stage_summary,
            diagnostic="invalid_provider_output",
            repository=request.repository,
            pull_request_number=request.pull_request_number,
            context_admission=context_admission,
        )
    service._provider_calls = tracker.provider_calls
    return service._finish_run(
        tracker=tracker,
        status=service._run_outcome_for_result(result)[0],
        stage_summary=stage_summary,
        result=result,
        repository=request.repository,
        pull_request_number=request.pull_request_number,
        diagnostic="partial_coverage" if status != "complete" else None,
        context_admission=context_admission,
    )
