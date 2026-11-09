"""Conservative aggregate review triage, with one targeted follow-up at most."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from .budgets import MAX_TOTAL_OUTPUT_BYTES, MAX_TOTAL_PROMPT_BYTES
from .context import finding_lifecycle_for_comment
from .decision_configuration import DecisionConfiguration
from .decision_execution import DecisionExecution
from .decision_provider import canonical, digest
from .diff import analyze_diff
from .errors import ProviderError, ReviewInputError
from .models import ProviderRequest, ReviewRequest, ReviewResult
from .outcomes import ResourceBudgetTracker
from .providers.base import ReviewProvider
from .review_decision_contract import (
    MAX_PACKET_BYTES,
    ReviewDecisionProvider,
    ReviewDecisionQuestion,
    ReviewDecisionRequest,
    strict_json,
)

if TYPE_CHECKING:
    from .context import IncrementalReviewPlan

POLICY_VERSION = "conservative-review-decisions-v1"
_QUESTIONS = (
    (
        "kind",
        "Classify the concern; mandatory rules and observed defects cannot be waived.",
        ("behavioral_defect", "optional_improvement", "clarification", "uncertain"),
    ),
    (
        "evidence",
        "Assess supplied evidence, abstaining when missing. Assessment is not proof.",
        ("supported", "contradicted", "insufficient_evidence"),
    ),
    (
        "next",
        "Choose one bounded application step; never commands or authority.",
        ("continue", "verify_further", "human_review"),
    ),
    (
        "duplicate",
        "Compare only supplied matches. Similar prose alone is not the same concern.",
        ("same_concern", "different_concern", "uncertain"),
    ),
)


def _packets(
    request: ReviewRequest,
    result: ReviewResult,
    incremental: IncrementalReviewPlan | None,
) -> list[dict]:
    analysis = analyze_diff(
        request.diff,
        limits=request.limits,
        allow_incomplete=True,
        max_bytes=request.work_budget.max_total_diff_bytes,
        max_lines=request.work_budget.max_total_diff_lines,
        max_files=request.work_budget.max_total_files,
        max_hunks=request.work_budget.max_total_hunks,
    )
    packets: list[dict] = []
    for index, comment in enumerate(result.comments):
        identity = finding_lifecycle_for_comment(comment, generation=0).fingerprint
        # Include whole relevant hunks or explicitly report their absence. Never
        # truncate a hunk or imply that a diff excerpt is a complete source file.
        hunks = [
            h.text
            for record in analysis.file_records
            if comment.path in record.coverage_paths
            for h in record.hunks
            if comment.line is not None
            and comment.line
            in (h.deleted_lines if comment.side == "LEFT" else h.added_lines)
        ]
        excerpts = [
            {"sha256": digest(h), "kind": "reviewed_diff_hunk", "text": h}
            for h in hunks
            if len(h.encode("utf-8")) <= 8192
        ]
        matches = [
            {"candidate_id": p["candidate_id"], "fingerprint": p["fingerprint"]}
            for p in packets
            if p["original"]["path"] == comment.path
            and p["original"].get("symbol") == comment.symbol
        ][-2:]
        prior_matches = (
            []
            if incremental is None
            else [
                {
                    "fingerprint": p.fingerprint,
                    "concern": p.concern,
                    "state": p.state,
                    "head_sha": incremental.previous_key.head_sha,
                }
                for p in incremental.previous_findings
                if p.path == comment.path
            ]
        )
        packets.append(
            {
                "candidate_id": f"c{index:04d}",
                "fingerprint": identity,
                "original": comment.to_dict(),
                "evidence": excerpts,
                "missing": [
                    "independent_behavioral_verification",
                    "explicit_failure_conditions",
                    "assumptions",
                ]
                + ([] if excerpts else ["bounded_source_excerpt"]),
                "matches": matches,
                "prior_matches": prior_matches[:2],
                "prior_matches_total": len(prior_matches),
                "prior_state": "not_supplied"
                if incremental is None
                else "existing_lifecycle_retained_by_review_engine",
            }
        )
    return packets


def run_decisions(
    request: ReviewRequest,
    result: ReviewResult,
    *,
    provider: ReviewDecisionProvider,
    review_provider: ReviewProvider,
    settings: DecisionConfiguration,
    tracker: ResourceBudgetTracker,
    incremental: IncrementalReviewPlan | None = None,
    total_input_limit: int = MAX_TOTAL_PROMPT_BYTES,
    total_output_limit: int = MAX_TOTAL_OUTPUT_BYTES,
    analysis_complete: bool = True,
) -> ReviewResult:
    packets = _packets(request, result, incremental)
    work = DecisionExecution(
        tracker,
        settings,
        min(tracker.budget.max_provider_calls, request.work_budget.max_provider_calls),
        total_input_limit=total_input_limit,
        total_output_limit=total_output_limit,
    )
    records = [
        {
            "candidate_id": p["candidate_id"],
            "fingerprint": p["fingerprint"],
            "action": "human_review",
            "status": "unprocessed",
            "assessments": {},
            "suspected_duplicate": False,
            "verification": "not_requested",
        }
        for p in packets
    ]
    native = []
    context = {
        "repository": request.repository,
        "base_sha": request.base_sha,
        "head_sha": request.head_sha,
        "diff_sha256": digest(request.diff),
        "policy": POLICY_VERSION,
        "inventory_sha256": digest([c.to_dict() for c in result.comments]),
    }
    verify = []
    batches = (
        [packets[i : i + 3] for i in range(0, len(packets), 3)]
        if analysis_complete
        else []
    )
    for batch in batches:
        state = canonical({**context, "candidates": batch})
        if len(state.encode("utf-8")) > MAX_PACKET_BYTES:
            continue
        questions = tuple(
            ReviewDecisionQuestion(
                p["candidate_id"] + "_" + key, p["candidate_id"], instruction, choices
            )
            for p in batch
            for key, instruction, choices in _QUESTIONS
        )
        decision_request = ReviewDecisionRequest(
            state, questions, provider.name, provider.model
        )
        before = work.calls
        try:
            response = provider.evaluate(decision_request, work)
            response.validate(decision_request)
            if work.calls != before + 1:
                raise ReviewInputError(
                    "decision adapter did not use one admitted dispatch"
                )
        except (ProviderError, ReviewInputError, ValueError, TypeError):
            continue
        native.append(response.metadata())
        answers = {a.name: a.choice for a in response.answers}
        for packet in batch:
            index = int(packet["candidate_id"][1:])
            record = records[index]
            choices = {
                key: answers[packet["candidate_id"] + "_" + key]
                for key, _, _ in _QUESTIONS
            }
            record["assessments"] = choices
            record["status"] = "assessed"
            record["suspected_duplicate"] = choices[
                "duplicate"
            ] == "same_concern" and bool(packet["matches"] or packet["prior_matches"])
            comment = result.comments[index]
            needs_verify = (
                not packet["evidence"]
                or choices["evidence"] != "supported"
                or choices["next"] == "verify_further"
                or choices["kind"] == "uncertain"
            )
            if choices["next"] == "human_review" or (
                choices["kind"] in {"optional_improvement", "clarification"}
                and comment.blocks_approval
            ):
                record["action"] = "human_review"
            elif needs_verify:
                record["action"] = "verify_further"
                verify.append(packet)
            else:
                record["action"] = (
                    "optional_suggestion"
                    if choices["kind"] in {"optional_improvement", "clarification"}
                    else "continue"
                )
    verification_called = False
    if verify:
        # One full targeted batch. Oversize work remains unresolved; no recursive
        # analysis or second decision/verification loop is available.
        prompt = canonical(
            {
                "instructions": "Verify only these candidate IDs against supplied evidence. Return exactly an object keyed by candidate ID, each with outcome (supported, contradicted, insufficient_evidence) and evidence_sha256 (array of supplied hunk hashes). No new findings, commands, dispositions or prose. Model agreement is not independent proof.",
                **context,
                "candidates": verify,
            }
        )
        before = work.calls
        try:

            def send(timeout: float, maximum: int) -> str:
                return review_provider.complete(
                    ProviderRequest(
                        prompt=prompt,
                        model=(request.model or review_provider.model)
                        if getattr(review_provider, "allow_model_override", True)
                        else review_provider.model,
                        max_prompt_bytes=min(
                            request.limits.max_prompt_bytes,
                            tracker.budget.max_prompt_bytes,
                        ),
                        max_response_bytes=maximum,
                        max_output_tokens=1024,
                        timeout_seconds=timeout,
                        limits=request.limits,
                    )
                ).text

            value = strict_json(work.execute(prompt, send))
            if not isinstance(value, dict) or set(value) != {
                p["candidate_id"] for p in verify
            }:
                raise ReviewInputError("verification candidate inventory conflicts")
            for packet in verify:
                answer = value[packet["candidate_id"]]
                if (
                    not isinstance(answer, dict)
                    or set(answer) != {"outcome", "evidence_sha256"}
                    or answer["outcome"]
                    not in {"supported", "contradicted", "insufficient_evidence"}
                    or not isinstance(answer["evidence_sha256"], list)
                    or len(answer["evidence_sha256"]) > 8
                    or any(
                        not isinstance(h, str)
                        or h not in {e["sha256"] for e in packet["evidence"]}
                        for h in answer["evidence_sha256"]
                    )
                ):
                    raise ReviewInputError("verification evidence is invalid")
            for packet in verify:
                answer = value[packet["candidate_id"]]
                record = records[int(packet["candidate_id"][1:])]
                record["verification"] = answer["outcome"]
                record["action"] = (
                    "continue"
                    if answer["outcome"] == "supported" and answer["evidence_sha256"]
                    else "human_review"
                )
        except (ProviderError, ReviewInputError, ValueError, TypeError):
            for packet in verify:
                record = records[int(packet["candidate_id"][1:])]
                record["action"] = "human_review"
                record["verification"] = "unavailable"
        verification_called = work.calls > before
    complete = analysis_complete and all(
        r["action"] in {"continue", "optional_suggestion"} for r in records
    )
    receipt = {
        "schema_version": "1.0",
        "policy_digest": settings.digest(),
        "input_sha256": result.content_digest(),
        "inventory_sha256": context["inventory_sha256"],
        "status": "complete" if complete else "incomplete",
        "provider": provider.name,
        "model": provider.model,
        "models": native,
        "candidates": records,
        "calls": work.calls,
        "input_bytes": work.input_bytes,
        "output_bytes": work.output_bytes,
        "output_reserved": work.output_reserved,
        "elapsed_ms": tracker.elapsed_ms() - work.started_ms,
        "verification_called": verification_called,
    }
    counts = {
        action: sum(r["action"] == action for r in records)
        for action in ("continue", "optional_suggestion", "human_review")
    }
    summary = (
        result.summary
        + "\n\nDecision triage: "
        + ", ".join(f"{key}={value}" for key, value in counts.items())
        + ". Original candidates and blocking proposals retained."
    )
    try:
        return replace(
            result,
            summary=summary,
            decision_pass=receipt,
            review_status=result.review_status if complete else "partial",
            bounded_work_receipt=None,
            coverage_only_partial=False,
        )
    except ReviewInputError:
        # Optional presentation cannot discard an already valid inventory. Try
        # retaining the receipt without its summary appendix, then withhold
        # completeness if even that receipt exceeds the original result bound.
        try:
            return replace(
                result,
                decision_pass=receipt,
                review_status=result.review_status if complete else "partial",
                bounded_work_receipt=None,
                coverage_only_partial=False,
            )
        except ReviewInputError:
            return replace(
                result,
                review_status="partial",
                bounded_work_receipt=None,
                coverage_only_partial=False,
            )
