"""Full discovery adapter using the same planner/executor as reassessment."""

from __future__ import annotations

import json
from collections.abc import Sequence
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
    stage_applies_to_paths,
)
from .diff import DiffFileRecord, analyze_diff
from .document_context import DocumentSelectionReport
from .errors import ReviewFormatError, ReviewInputError
from .evidence import EvidenceBundle, EvidenceRecord, EvidenceSnapshot, evidence_digest
from .execution import CompletedBatch, execute_plan
from .generated_mirrors import VerifiedMirrorAttestation, mirror_path_matches
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
    MAX_RELATED_PATHS,
    PromptOverflowDetail,
    WorkBatch,
    WorkRequirement,
    _bounded_scope_manifest,
    _classify_file,
    _coverage_for,
    _format_scope_label,
    _format_scoped_summary,
    _hunk_record,
    _reconcile_file_outcomes_from_hunks,
    apply_chunk_outcomes,
    extract_prior_reviewed_coverage,
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


def _batch_scope_note(
    batch: WorkBatch,
    *,
    changed_paths: tuple[str, ...],
    max_other_paths_bytes: int = 8192,
) -> str:
    """Return bounded PR-wide and split-segment scope context for a partial batch."""
    batch_covered = {
        path
        for record in batch.records
        for path in (record.path, record.old_path)
        if path
    }
    other_paths = sorted(set(changed_paths) - batch_covered)
    has_partial_records = any(
        record.fragment is not None or not record.supplied_complete
        for record in batch.records
    )
    if not other_paths and not has_partial_records:
        return ""
    in_batch_manifest, _ = _bounded_scope_manifest(
        batch.paths,
        max_paths=MAX_RELATED_PATHS,
        max_bytes=max(512, max_other_paths_bytes // 2),
    )
    lines = [
        "<review-batch-scope>",
        "This request is one partial slice of a multi-batch PR review; sibling batches review the remaining changed files and/or remaining segments of split files.",
        f"Files in this batch: {in_batch_manifest or '(none)'}",
    ]
    if other_paths:
        other_manifest, _ = _bounded_scope_manifest(
            other_paths,
            max_paths=MAX_RELATED_PATHS,
            max_bytes=max_other_paths_bytes,
            total_count=len(other_paths),
        )
        lines.append(f"Other changed PR paths not in this batch: {other_manifest}")
    segment_lines: list[str] = []
    for record in batch.records:
        if record.fragment is not None:
            proof = record.fragment
            line_range = ""
            if record.analysis is not None and record.analysis.hunk_records:
                piece = record.analysis.hunk_records[0]
                if piece.new_count:
                    new_end = piece.new_start + piece.new_count - 1
                    line_range = f" lines +{piece.new_start}..+{new_end}"
                else:
                    line_range = f" lines +{piece.new_start}"
            segment_lines.append(
                f"- {record.path} hunk #{proof.source_hunk_index}{line_range} "
                f"(bytes {proof.body_start}..{proof.body_end}/{proof.body_bytes}): "
                "Lossless canonical hunk segment; preceding/following lines are reviewed in sibling batches, NOT truncated or missing from the PR."
            )
        elif not record.supplied_complete:
            hunk_desc = ""
            if record.analysis is not None and record.analysis.hunk_records:
                piece = record.analysis.hunk_records[0]
                if piece.new_count:
                    new_end = piece.new_start + piece.new_count - 1
                    hunk_desc = (
                        f" hunk #{piece.index} lines +{piece.new_start}..+{new_end}"
                    )
                else:
                    hunk_desc = f" hunk #{piece.index} lines +{piece.new_start}"
            segment_lines.append(
                f"- {record.path}{hunk_desc}: Partial file hunk slice; remaining hunks are reviewed in sibling batches, NOT truncated or missing from the PR."
            )
    if segment_lines:
        lines.append("Split-file segment scope:")
        lines.extend(segment_lines)
    lines.append(
        "Do NOT report absent sibling files, segment boundaries, or symbols defined in sibling segments/files as truncation, syntax errors, or blocking defects. "
        "Scope `summary` strictly to the files and segments supplied in THIS batch."
    )
    lines.append("</review-batch-scope>")
    return "\n\n" + "\n".join(lines)


def _batch_scope_items(batch: WorkBatch) -> tuple[str, ...]:
    """Return compact per-record scope labels for batch summary prefixes."""
    items: list[str] = []
    for record in batch.records:
        if (
            record.fragment is not None
            and record.analysis is not None
            and record.analysis.hunk_records
        ):
            piece = record.analysis.hunk_records[0]
            if piece.new_count:
                new_end = piece.new_start + piece.new_count - 1
                desc = f"{record.path} lines {piece.new_start}-{new_end}"
            else:
                old_end = piece.old_start + max(0, piece.old_count - 1)
                desc = f"{record.path} old lines {piece.old_start}-{old_end}"
        elif (
            not record.supplied_complete
            and record.analysis is not None
            and record.analysis.hunk_records
        ):
            piece = record.analysis.hunk_records[0]
            desc = f"{record.path} hunk #{piece.index}"
        else:
            desc = record.path
        items.append(desc)
    return tuple(items)


def context_admission_document(
    entries: list[dict[str, object]],
    *,
    overflow_diagnostics: Sequence[dict[str, object]] = (),
) -> dict[str, object] | None:
    if not entries and not overflow_diagnostics:
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
    document: dict[str, object] = {
        "policy": PROMPT_CONTEXT_POLICY,
        "original_inventories": list(originals.values()),
        "batches": batches,
    }
    if overflow_diagnostics:
        document["overflow_diagnostics"] = list(overflow_diagnostics)
    return document


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
    prior_partial: ReviewResult | None = None,
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
        snapshot_request,
        incremental=incremental,
        current_key=current_key,
        profile=profile,
    )
    prior_reviewed_files, prior_reviewed_hunks = extract_prior_reviewed_coverage(
        analysis, prior_partial, limits=request.limits
    )
    file_outcomes: dict[str, tuple[str, str | None]] = {}
    hunk_outcomes: dict[int, tuple[str, str | None]] = {}
    initial_classification: dict[int, tuple[str, str | None]] = {}
    for record in analysis.file_records:
        outcome, reason = _classify_file(
            record, analysis=analysis, limits=request.limits
        )
        initial_classification[id(record)] = (outcome, reason)
        for path in record.coverage_paths:
            file_outcomes[path] = (
                ("reviewed", None) if outcome == "reviewable" else (outcome, reason)
            )
        for hunk in record.hunks:
            hunk_outcomes[hunk.index] = (
                ("reviewed", None) if outcome == "reviewable" else (outcome, reason)
            )

    def _record_all_paths_match(
        record: DiffFileRecord, prefixes: Sequence[str]
    ) -> bool:
        return bool(record.coverage_paths) and all(
            any(mirror_path_matches(path, prefix) for prefix in prefixes)
            for path in record.coverage_paths
        )

    def _record_any_path_match(record: DiffFileRecord, prefixes: Sequence[str]) -> bool:
        return any(
            mirror_path_matches(path, prefix)
            for path in record.coverage_paths
            for prefix in prefixes
        )

    deferred_mirrors: list[
        tuple[
            VerifiedMirrorAttestation,
            tuple[DiffFileRecord, ...],
            tuple[DiffFileRecord, ...],
        ]
    ] = []
    deferred_mirror_ids: set[int] = set()
    for attestation in request.verified_mirrors:
        if not attestation.verified:
            continue
        dep_prefixes = (*attestation.canonical_sources, *attestation.generator_sources)
        dep_records = tuple(
            record
            for record in analysis.file_records
            if _record_any_path_match(record, dep_prefixes)
        )
        if not all(
            initial_classification.get(id(dep), ("unsupported", None))[0]
            == "reviewable"
            for dep in dep_records
        ):
            continue
        mirror_records = tuple(
            record
            for record in analysis.file_records
            if _record_all_paths_match(record, attestation.mirror_paths)
            and initial_classification.get(id(record), ("unsupported", None))[0]
            == "reviewable"
        )
        if not mirror_records:
            continue
        for mirror_record in mirror_records:
            deferred_mirror_ids.add(id(mirror_record))
            for path in mirror_record.coverage_paths:
                file_outcomes[path] = ("unsupported", "cross-file-relationship")
            for hunk in mirror_record.hunks:
                hunk_outcomes[hunk.index] = ("unsupported", "cross-file-relationship")
        deferred_mirrors.append((attestation, mirror_records, dep_records))

    records = []
    for record in analysis.file_records:
        if id(record) in deferred_mirror_ids:
            continue
        outcome, reason = initial_classification[id(record)]
        if outcome == "reviewable":
            if prior_reviewed_files or prior_reviewed_hunks:
                paths_done = bool(record.coverage_paths) and all(
                    path in prior_reviewed_files for path in record.coverage_paths
                )
                hunks_done = all(
                    hunk.index in prior_reviewed_hunks for hunk in record.hunks
                )
                if paths_done and hunks_done:
                    continue
            records.append(record)
    summaries: list[str] = []
    comments: list[ReviewComment] = []
    proposals: list[LearningProposal] = []
    if (prior_reviewed_files or prior_reviewed_hunks) and prior_partial is not None:
        base_summary = prior_partial.summary.split("\n\nContext admission digest:")[0]
        base_summary = base_summary.split("\n\nRequired work pending:")[0].strip()
        if base_summary:
            summaries.append(base_summary)
        comments.extend(prior_partial.comments)
        proposals.extend(prior_partial.learning_proposals)
    stage_summary: dict[str, str] = {}
    failed = False
    actual_failure = False
    clean_limit_stop = False
    comment_stage = False
    scope_wave_used = not allow_expansion
    context_diagnostics: list[dict[str, object]] = []
    overflow_details: list[PromptOverflowDetail] = []
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

    active_stage_count = sum(
        1
        for candidate_stage in service.stages
        if not candidate_stage.categories
        or any(
            request.active_category_ids is None
            or category.id in request.active_category_ids
            for category in candidate_stage.categories
        )
    )
    window_batch_limit = max(
        1,
        tracker.budget.max_provider_calls // max(1, active_stage_count),
    )
    skipped_applicable_stage = False
    for stage in service.stages:
        categories = tuple(
            category
            for category in stage.categories
            if request.active_category_ids is None
            or category.id in request.active_category_ids
        )
        if stage.categories and not categories:
            stage_summary[stage.name] = "skipped"
            if stage_applies_to_paths(stage, analysis.changed_paths):
                skipped_applicable_stage = True
                failed = True
                actual_failure = True
            continue
        comment_stage = comment_stage or "comments" in stage.outputs
        subdivision_allowance = SubdivisionAllowance()
        unadmitted_stop_reasons: dict[str, str] = {}
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
            max_other_paths_bytes = (
                min(2048, max(512, budgets.batch_prompt_bytes // 16))
                if budgets.batch_prompt_bytes <= 65_536
                else min(8192, max(512, budgets.batch_prompt_bytes // 4))
            )
            scope_note = _batch_scope_note(
                batch,
                changed_paths=analysis.changed_paths,
                max_other_paths_bytes=max_other_paths_bytes,
            )
            batch_frame_bytes = frame_bytes + len(scope_note.encode("utf-8"))
            prompt_ceiling_bytes, clamped_by_legacy_48k = (
                budgets.effective_prompt_ceiling_bytes(
                    output_tokens=budgets.max_output_tokens
                )
            )
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
                            0, budgets.batch_prompt_bytes - batch_frame_bytes
                        ),
                        analysis=batch_analysis,
                    )
                    + scope_note
                    + suffix
                ),
                fits=lambda prompt: budgets.fits_prompt(
                    prompt + "\n\n" + _PROVIDER_OUTPUT_CORRECTION,
                    output_tokens=budgets.max_output_tokens,
                ),
                before_probe=check_deadline,
                prompt_ceiling_bytes=prompt_ceiling_bytes,
                extra_prompt_bytes=correction_bytes,
                clamped_by_legacy_48k=clamped_by_legacy_48k,
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
                    if hunk.index in prior_reviewed_hunks:
                        continue
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
                            unadmitted_reasons=unadmitted_stop_reasons,
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
            max_batches=min(work.max_chunks, window_batch_limit),
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
            subdivision_stop_reasons=unadmitted_stop_reasons,
        )
        batch_labels: dict[str, str] = {}
        if len(plan.batches) > 1:
            total_batches = len(plan.batches)
            for idx, planned_batch in enumerate(plan.batches, start=1):
                batch_labels[planned_batch.batch_id] = _format_scope_label(
                    "Batch",
                    idx,
                    total_batches,
                    _batch_scope_items(planned_batch),
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
                batch_summary = _format_scoped_summary(
                    output.summary,
                    label=batch_labels.get(item.batch.batch_id),
                )
                prospective_summary = "\n\n".join(
                    [*summaries, *([batch_summary] if batch_summary else [])]
                )
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
            if batch_summary:
                summaries.append(batch_summary)
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
                if len(bridge_plan.batches) > 1 or len(plan.batches) > 1:
                    bridge_total = len(bridge_plan.batches)
                    for idx, bridge_batch in enumerate(bridge_plan.batches, start=1):
                        batch_labels[bridge_batch.batch_id] = _format_scope_label(
                            "Scope batch",
                            idx,
                            bridge_total,
                            _batch_scope_items(bridge_batch),
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
                actual_failure = True
                failed = True
                for path in {scope.reference, *scope.required_paths}:
                    if path in file_outcomes:
                        file_outcomes[path] = ("budget-exhausted", "chunk-failed")
                    for file_record in analysis.file_records:
                        if path in file_record.coverage_paths:
                            for cov_path in file_record.coverage_paths:
                                if cov_path in file_outcomes:
                                    file_outcomes[cov_path] = (
                                        "budget-exhausted",
                                        "chunk-failed",
                                    )
                            for bridge_hunk in file_record.hunks:
                                hunk_outcomes[bridge_hunk.index] = (
                                    "budget-exhausted",
                                    "chunk-failed",
                                )
        stage_summary[stage.name] = "partial" if execution.pending else "complete"
        if bridge_pending:
            stage_summary[stage.name] = "partial"
        clean_limit_stop = clean_limit_stop or bool(
            execution.pending and execution.clean_bounded_outcome
        )
        actual_failure = actual_failure or not execution.clean_bounded_outcome
        failed = failed or bool(execution.pending)
        for _, reason in execution.pending:
            refusal_reasons[reason] = refusal_reasons.get(reason, 0) + 1
        pending_overflow_ids = {
            identity
            for identity, reason in execution.pending
            if reason == "mandatory-context-and-evidence-oversized"
        }
        if pending_overflow_ids:
            overflow_details.extend(
                detail
                for detail in plan.unprocessed_diagnostics
                if detail.requirement_identity in pending_overflow_ids
            )
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
                if reason in {"provider_call_limit", "provider-call-budget"}
                else "chunk-failed"
            )
            for key in item.evidence_ids:
                pending_record = evidence_by_id.get(key)
                if pending_record is not None:
                    for pending_path in (pending_record.path, pending_record.old_path):
                        if pending_path is not None and pending_path in file_outcomes:
                            file_outcomes[pending_path] = (
                                "budget-exhausted",
                                diagnostic,
                            )
            for index in item.hunk_indexes:
                hunk_outcomes[index] = ("budget-exhausted", diagnostic)
    for path in analysis.changed_paths:
        file_outcomes.setdefault(path, ("unsupported", "incomplete-enumeration"))
    _reconcile_file_outcomes_from_hunks(
        analysis, file_outcomes=file_outcomes, hunk_outcomes=hunk_outcomes
    )
    for _, mirror_records, dep_records in deferred_mirrors:
        deps_reviewed = not skipped_applicable_stage and all(
            all(
                file_outcomes.get(path, ("unsupported", None))[0] == "reviewed"
                for path in dep.coverage_paths
            )
            and all(
                hunk_outcomes.get(hunk.index, ("unsupported", None))[0] == "reviewed"
                for hunk in dep.hunks
            )
            for dep in dep_records
        )
        if not deps_reviewed:
            continue
        for mirror_record in mirror_records:
            for path in mirror_record.coverage_paths:
                file_outcomes[path] = ("reviewed", "verified-generated-mirror")
            for hunk in mirror_record.hunks:
                hunk_outcomes[hunk.index] = ("reviewed", "verified-generated-mirror")
    coverage = _coverage_for(
        analysis,
        file_outcomes=file_outcomes,
        hunk_outcomes=hunk_outcomes,
        limits=request.limits,
    )
    if not comment_stage:
        failed = True
        coverage = apply_chunk_outcomes(
            coverage,
            paths=tuple(
                entry.path for entry in coverage.files if entry.outcome == "reviewed"
            ),
            hunk_indexes=tuple(
                entry.index for entry in coverage.hunks if entry.outcome == "reviewed"
            ),
            outcome="unsupported",
            reason="no-review-stage",
            limits=request.limits,
        )
    # A finished diff stays complete when only optional documents were
    # trimmed. Unfinished required work and incomplete diff coverage do not.
    status = "partial" if failed or not coverage.fully_reviewed else "complete"
    if not comment_stage and not skipped_applicable_stage:
        status = "summary-only"
    overflow_diagnostic_dicts = tuple(detail.to_dict() for detail in overflow_details)
    context_admission = context_admission_document(
        context_diagnostics, overflow_diagnostics=overflow_diagnostic_dicts
    )
    refusal_summary = (
        "\n\nRequired work pending: "
        + "; ".join(
            f"{reason} ({count})" for reason, count in sorted(refusal_reasons.items())
        )
        + "."
        if refusal_reasons
        else ""
    )
    max_refusal_bytes = max(
        256, PENDING_APPENDIX_RESERVE - CONTEXT_DIGEST_LINE_BYTES - 180
    )
    if overflow_details:
        primary = max(
            overflow_details,
            key=lambda item: (
                item.over_budget_bytes if item.over_budget_bytes is not None else -1,
                item.rendered_prompt_bytes
                if item.rendered_prompt_bytes is not None
                else -1,
            ),
        )
        if (
            primary.rendered_prompt_bytes is not None
            and primary.prompt_ceiling_bytes is not None
        ):
            overage = (
                primary.over_budget_bytes
                if primary.over_budget_bytes is not None
                else max(
                    0, primary.rendered_prompt_bytes - primary.prompt_ceiling_bytes
                )
            )
            budget_parts: list[str] = [f"+{overage} overage"]
            if primary.evidence_bytes is not None:
                budget_parts.append(f"evidence {primary.evidence_bytes} bytes")
            if primary.framing_bytes is not None:
                budget_parts.append(f"framing {primary.framing_bytes} bytes")
            if primary.clamped_by_legacy_48k:
                budget_parts.append("legacy 48 KiB ceiling")
            line = (
                f"\nPrompt budget overflow: rendered {primary.rendered_prompt_bytes} bytes "
                f"> {primary.prompt_ceiling_bytes}-byte ceiling ({'; '.join(budget_parts)})."
            )
            if len((refusal_summary + line).encode("utf-8")) <= max_refusal_bytes:
                refusal_summary += line
    if mandatory_refused_documents:
        doc_line = (
            f"\nMandatory doc payload: {len(mandatory_refused_documents)} unique document(s), "
            f"{sum(mandatory_refused_documents.values())} UTF-8 payload bytes (payload size, not overage)."
        )
        if len((refusal_summary + doc_line).encode("utf-8")) <= max_refusal_bytes:
            refusal_summary += doc_line
    elif overflow_details:
        doc_paths = tuple(
            dict.fromkeys(
                path
                for detail in overflow_details
                for path in detail.mandatory_doc_paths
            )
        )
        max_doc_bytes = max(
            (detail.mandatory_doc_bytes or 0 for detail in overflow_details),
            default=0,
        )
        if doc_paths or max_doc_bytes:
            doc_line = (
                f"\nMandatory doc payload: {len(doc_paths)} unique document(s), "
                f"{max_doc_bytes} UTF-8 payload bytes (payload size, not overage)."
            )
            if len((refusal_summary + doc_line).encode("utf-8")) <= max_refusal_bytes:
                refusal_summary += doc_line
    if overflow_details:
        unit_labels: list[str] = []
        for detail in overflow_details:
            stop_suffix = (
                f" [{detail.subdivision_stop_reason}]"
                if detail.subdivision_stop_reason
                else ""
            )
            if detail.hunk_ranges:
                for hunk_range in detail.hunk_ranges:
                    unit_labels.append(hunk_range.format_compact() + stop_suffix)
            elif detail.rejected_paths:
                for path in detail.rejected_paths:
                    unit_labels.append(f"{path}{stop_suffix}")
            else:
                unit_labels.append(f"{detail.requirement_identity}{stop_suffix}")
        deduped_labels = list(dict.fromkeys(unit_labels))
        prefix = "\nRejected units: "
        accepted_labels: list[str] = []
        for idx, label in enumerate(deduped_labels):
            remaining = len(deduped_labels) - (idx + 1)
            candidate_items = [*accepted_labels, label]
            suffix = f" (+{remaining} more)." if remaining > 0 else "."
            candidate_line = prefix + "; ".join(candidate_items) + suffix
            if (
                len((refusal_summary + candidate_line).encode("utf-8"))
                > max_refusal_bytes
            ):
                if accepted_labels:
                    more = len(deduped_labels) - len(accepted_labels)
                    refusal_summary += (
                        prefix + "; ".join(accepted_labels) + f" (+{more} more)."
                    )
                break
            accepted_labels.append(label)
        else:
            if accepted_labels:
                refusal_summary += prefix + "; ".join(accepted_labels) + "."
    no_progress_detected = False
    has_prior_partial = (
        prior_partial is not None
        and prior_partial.review_status == "partial"
        and prior_partial.coverage is not None
        and prior_partial.coverage.enumeration_complete
    )
    if (
        has_prior_partial or prior_reviewed_files or prior_reviewed_hunks
    ) and not coverage.fully_reviewed:
        now_reviewed_files = sum(
            1 for entry in coverage.files if entry.outcome == "reviewed"
        )
        now_reviewed_hunks = sum(
            1 for entry in coverage.hunks if entry.outcome == "reviewed"
        )
        if now_reviewed_files <= len(
            prior_reviewed_files
        ) and now_reviewed_hunks <= len(prior_reviewed_hunks):
            no_progress_detected = True
            refusal_summary += (
                "\nNo progress on remaining unreviewed coverage; next action: "
                "@reviewsensei accept-coverage-uncertainty --reason <reason> "
                "or @reviewsensei pause."
            )
    try:
        default_summary = (
            "Review complete."
            if not failed and coverage.fully_reviewed
            else "Review work could not be completed within the configured bounds."
        )
        result = service._finalize_result(
            request,
            summary=("\n\n".join(summaries) or default_summary)
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
    run_status = service._run_outcome_for_result(result)[0]
    run_diagnostic = "partial_coverage" if status != "complete" else None
    if no_progress_detected:
        run_diagnostic = "no-progress"
    return service._finish_run(
        tracker=tracker,
        status=run_status,
        stage_summary=stage_summary,
        result=result,
        repository=request.repository,
        pull_request_number=request.pull_request_number,
        diagnostic=run_diagnostic,
        context_admission=context_admission,
        bounded_stop=(
            status == "partial"
            and clean_limit_stop
            and not actual_failure
            and comment_stage
            and not no_progress_detected
            and coverage.enumeration_complete
        ),
    )
