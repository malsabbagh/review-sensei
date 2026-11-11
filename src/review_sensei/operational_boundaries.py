"""Explicit operational boundary classifications and bounded recovery contracts.

This module formalizes ReviewSensei's fail-closed operational limits and the
deterministic, bounded recovery actions available when a run hits one of those
limits without silently truncating evidence, rebinding authority, or expanding
approved resource budgets.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .context import PromptContextOverflow
from .coverage import COVERAGE_OUTCOMES, COVERAGE_REASONS, CoverageManifest
from .diff import DiffAnalysis, DiffFileRecord, DiffHunk
from .disposition import incomplete_coverage_from_result
from .document_context import DocumentSelectionReport
from .errors import ReviewInputError
from .models import ReviewRequest, ReviewResult
from .planning import LargeChangePlan, ReviewWorkPlan
from .validation import (
    utf8_size,
    validate_bounded_text,
    validate_repository_path,
)

INDIVISIBLE_UNIT_KINDS = frozenset(
    {
        "oversized_hunk",
        "oversized_hunkless_file",
        "mandatory_prompt_overflow",
        "binary_file",
        "generated_file",
        "unsupported_lockfile",
        "incomplete_enumeration",
        "budget_exhausted",
        "partial_chunk_failure",
    }
)

PERMITTED_RECOVERY_ROUTES = frozenset(
    {
        "accept_coverage_uncertainty",
        "human_file_review",
        "split_oversized_change",
        "review_remaining",
    }
)

RECOVERY_COMMAND_TEMPLATES = {
    "accept_coverage_uncertainty": (
        "@reviewsensei accept-coverage-uncertainty --reason <reason>"
    ),
    "human_file_review": "record authenticated human_file_review receipt for blob",
    "split_oversized_change": (
        "split the oversized change or narrow pinned context on a new head commit"
    ),
    "review_remaining": "@reviewsensei review remaining",
}


def _positive_int(value: object, *, label: str, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        qualifier = "non-negative" if allow_zero else "positive"
        raise ReviewInputError(f"{label} must be a {qualifier} integer")
    return value


@dataclass(frozen=True)
class IndivisibleWorkUnit:
    """One unsupported, excluded, or deferred work unit and its bounded recovery."""

    unit_kind: str
    path: str | None
    hunk_index: int | None
    unit_bytes: int | None
    limit_bytes: int | None
    coverage_outcome: str
    coverage_reason: str | None
    indivisible: bool
    permitted_routes: tuple[str, ...]
    diagnostic: str
    mandatory_documents: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.unit_kind not in INDIVISIBLE_UNIT_KINDS:
            raise ReviewInputError("indivisible work unit_kind is unsupported")
        if self.path is not None:
            validate_repository_path(
                self.path, label="indivisible unit path", allow_glob_chars=True
            )
        if self.hunk_index is not None:
            _positive_int(self.hunk_index, label="indivisible hunk_index")
        if self.unit_bytes is not None:
            _positive_int(
                self.unit_bytes, label="indivisible unit_bytes", allow_zero=True
            )
        if self.limit_bytes is not None:
            _positive_int(self.limit_bytes, label="indivisible limit_bytes")
        if self.coverage_outcome not in COVERAGE_OUTCOMES | {"incomplete"}:
            raise ReviewInputError("indivisible coverage_outcome is unsupported")
        if (
            self.coverage_reason is not None
            and self.coverage_reason not in COVERAGE_REASONS
        ):
            raise ReviewInputError("indivisible coverage_reason is unsupported")
        if not isinstance(self.indivisible, bool):
            raise ReviewInputError("indivisible flag must be a boolean")
        if (
            not isinstance(self.permitted_routes, tuple)
            or not self.permitted_routes
            or any(
                route not in PERMITTED_RECOVERY_ROUTES
                for route in self.permitted_routes
            )
            or len(set(self.permitted_routes)) != len(self.permitted_routes)
        ):
            raise ReviewInputError("permitted_routes must be a non-empty unique tuple")
        if self.indivisible and "review_remaining" in self.permitted_routes:
            raise ReviewInputError(
                "indivisible units must never suggest review_remaining retries"
            )
        if not isinstance(self.mandatory_documents, tuple) or any(
            not isinstance(doc, str) for doc in self.mandatory_documents
        ):
            raise ReviewInputError("mandatory_documents must be repository paths")
        for doc in self.mandatory_documents:
            validate_repository_path(
                doc, label="mandatory document path", allow_glob_chars=True
            )
        validate_bounded_text(
            self.diagnostic,
            2048,
            label="indivisible unit diagnostic",
            allow_empty=False,
        )

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "unit_kind": self.unit_kind,
            "path": self.path,
            "hunk_index": self.hunk_index,
            "unit_bytes": self.unit_bytes,
            "limit_bytes": self.limit_bytes,
            "coverage_outcome": self.coverage_outcome,
            "coverage_reason": self.coverage_reason,
            "indivisible": self.indivisible,
            "permitted_routes": list(self.permitted_routes),
            "diagnostic": self.diagnostic,
        }
        if self.mandatory_documents:
            payload["mandatory_documents"] = list(self.mandatory_documents)
        return payload


@dataclass(frozen=True)
class IndivisibleWorkReport:
    """Bounded G09 report over change coverage or mandatory prompt overflow."""

    fully_reviewed: bool
    enumeration_complete: bool
    approval_state: str
    truncated_mandatory_context: bool
    unbounded_retry_suggested: bool
    units: tuple[IndivisibleWorkUnit, ...]
    reviewed_paths: tuple[str, ...] = ()
    incomplete_coverage_tokens: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for flag_name, flag_val in (
            ("fully_reviewed", self.fully_reviewed),
            ("enumeration_complete", self.enumeration_complete),
            ("truncated_mandatory_context", self.truncated_mandatory_context),
            ("unbounded_retry_suggested", self.unbounded_retry_suggested),
        ):
            if not isinstance(flag_val, bool):
                raise ReviewInputError(f"{flag_name} must be a boolean")
        if self.approval_state not in {"reviewed", "partial", "incomplete", "unknown"}:
            raise ReviewInputError("approval_state is unsupported")
        if self.truncated_mandatory_context:
            raise ReviewInputError("mandatory context truncation is forbidden")
        if self.unbounded_retry_suggested:
            raise ReviewInputError("unbounded retry suggestions are forbidden")
        if not isinstance(self.units, tuple) or any(
            not isinstance(unit, IndivisibleWorkUnit) for unit in self.units
        ):
            raise ReviewInputError("units must be a tuple of IndivisibleWorkUnit")
        if self.fully_reviewed and (not self.enumeration_complete or self.units):
            raise ReviewInputError(
                "fully_reviewed cannot be true when enumeration is incomplete or unsupported units remain"
            )

    def summary_diagnostic(self) -> str:
        if self.fully_reviewed:
            return (
                f"Complete coverage verified across {len(self.reviewed_paths)} "
                "changed path(s); no indivisible overflow."
            )
        fragments = [unit.diagnostic for unit in self.units]
        routes: list[str] = []
        for unit in self.units:
            for route in unit.permitted_routes:
                template = RECOVERY_COMMAND_TEMPLATES[route]
                if template not in routes:
                    routes.append(template)
        joined_units = "; ".join(fragments)
        joined_routes = " | ".join(routes)
        return (
            f"Coverage {self.approval_state} ({len(self.units)} non-reviewed unit(s)): "
            f"{joined_units}. Permitted bounded recovery: {joined_routes}."
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "fully_reviewed": self.fully_reviewed,
            "enumeration_complete": self.enumeration_complete,
            "approval_state": self.approval_state,
            "truncated_mandatory_context": self.truncated_mandatory_context,
            "unbounded_retry_suggested": self.unbounded_retry_suggested,
            "reviewed_paths": list(self.reviewed_paths),
            "incomplete_coverage_tokens": list(self.incomplete_coverage_tokens),
            "units": [unit.to_dict() for unit in self.units],
            "summary": self.summary_diagnostic(),
        }


def _record_map(analysis: DiffAnalysis) -> dict[str, DiffFileRecord]:
    mapping: dict[str, DiffFileRecord] = {}
    for record in analysis.file_records or ():
        for path in record.coverage_paths:
            mapping[path] = record
    return mapping


def _hunk_map(analysis: DiffAnalysis) -> dict[tuple[str, int], DiffHunk]:
    mapping: dict[tuple[str, int], DiffHunk] = {}
    for record in analysis.file_records or ():
        for hunk in record.hunks:
            for path in record.coverage_paths:
                mapping[(path, hunk.index)] = hunk
    return mapping


def _unit_for_reason(
    *,
    path: str,
    hunk_index: int | None,
    outcome: str,
    reason: str | None,
    unit_bytes: int | None,
    limit_bytes: int | None,
) -> IndivisibleWorkUnit:
    target = f"{path} hunk #{hunk_index}" if hunk_index is not None else path
    if reason == "too-large-hunk":
        size_note = (
            f" ({unit_bytes} UTF-8 bytes > {limit_bytes}-byte per-request diff ceiling)"
            if unit_bytes is not None and limit_bytes is not None
            else ""
        )
        return IndivisibleWorkUnit(
            unit_kind="oversized_hunk",
            path=path,
            hunk_index=hunk_index,
            unit_bytes=unit_bytes,
            limit_bytes=limit_bytes,
            coverage_outcome=outcome,
            coverage_reason=reason,
            indivisible=True,
            permitted_routes=("split_oversized_change", "accept_coverage_uncertainty"),
            diagnostic=(
                f"{target} exceeds indivisible hunk bounds{size_note} without truncation; "
                "split the hunk or run @reviewsensei accept-coverage-uncertainty --reason <reason>"
            ),
        )
    if reason == "too-large-file":
        size_note = (
            f" ({unit_bytes} UTF-8 bytes > {limit_bytes}-byte per-request diff ceiling)"
            if unit_bytes is not None and limit_bytes is not None
            else ""
        )
        return IndivisibleWorkUnit(
            unit_kind="oversized_hunkless_file",
            path=path,
            hunk_index=hunk_index,
            unit_bytes=unit_bytes,
            limit_bytes=limit_bytes,
            coverage_outcome=outcome,
            coverage_reason=reason,
            indivisible=True,
            permitted_routes=("split_oversized_change", "accept_coverage_uncertainty"),
            diagnostic=(
                f"{target} is an oversized hunkless file{size_note}; "
                "split the file change or run @reviewsensei accept-coverage-uncertainty --reason <reason>"
            ),
        )
    if reason == "binary":
        return IndivisibleWorkUnit(
            unit_kind="binary_file",
            path=path,
            hunk_index=hunk_index,
            unit_bytes=unit_bytes,
            limit_bytes=None,
            coverage_outcome=outcome,
            coverage_reason=reason,
            indivisible=True,
            permitted_routes=("human_file_review", "accept_coverage_uncertainty"),
            diagnostic=(
                f"{target} is a binary blob unsupported by text diff inference; "
                "confirm via human_file_review or @reviewsensei accept-coverage-uncertainty --reason <reason>"
            ),
        )
    if reason == "generated":
        return IndivisibleWorkUnit(
            unit_kind="generated_file",
            path=path,
            hunk_index=hunk_index,
            unit_bytes=unit_bytes,
            limit_bytes=None,
            coverage_outcome=outcome,
            coverage_reason=reason,
            indivisible=True,
            permitted_routes=("accept_coverage_uncertainty", "split_oversized_change"),
            diagnostic=(
                f"{target} is excluded by generated-file policy; "
                "run @reviewsensei accept-coverage-uncertainty --reason <reason> or isolate generated output"
            ),
        )
    if reason == "lockfile-format":
        return IndivisibleWorkUnit(
            unit_kind="unsupported_lockfile",
            path=path,
            hunk_index=hunk_index,
            unit_bytes=unit_bytes,
            limit_bytes=None,
            coverage_outcome=outcome,
            coverage_reason=reason,
            indivisible=True,
            permitted_routes=("accept_coverage_uncertainty", "human_file_review"),
            diagnostic=(
                f"{target} uses an unsupported lockfile diff format; "
                "run @reviewsensei accept-coverage-uncertainty --reason <reason> or confirm manually"
            ),
        )
    if reason == "provider-call-budget" or outcome == "budget-exhausted":
        return IndivisibleWorkUnit(
            unit_kind="budget_exhausted",
            path=path,
            hunk_index=hunk_index,
            unit_bytes=unit_bytes,
            limit_bytes=limit_bytes,
            coverage_outcome=outcome,
            coverage_reason=reason,
            indivisible=False,
            permitted_routes=("review_remaining", "accept_coverage_uncertainty"),
            diagnostic=(
                f"{target} deferred by per-run provider-call budget; "
                "continue on the exact snapshot with @reviewsensei review remaining "
                "or @reviewsensei accept-coverage-uncertainty --reason <reason>"
            ),
        )
    if reason == "incomplete-enumeration":
        return IndivisibleWorkUnit(
            unit_kind="incomplete_enumeration",
            path=path,
            hunk_index=hunk_index,
            unit_bytes=unit_bytes,
            limit_bytes=limit_bytes,
            coverage_outcome=outcome,
            coverage_reason=reason,
            indivisible=True,
            permitted_routes=("split_oversized_change", "accept_coverage_uncertainty"),
            diagnostic=(
                f"{target} has incomplete diff enumeration; "
                "split the PR or run @reviewsensei accept-coverage-uncertainty --reason <reason>"
            ),
        )
    return IndivisibleWorkUnit(
        unit_kind="partial_chunk_failure",
        path=path,
        hunk_index=hunk_index,
        unit_bytes=unit_bytes,
        limit_bytes=limit_bytes,
        coverage_outcome=outcome,
        coverage_reason=reason,
        indivisible=False,
        permitted_routes=("review_remaining", "accept_coverage_uncertainty"),
        diagnostic=(
            f"{target} remained {outcome} ({reason or 'unreviewed'}); "
            "run @reviewsensei review remaining or @reviewsensei accept-coverage-uncertainty --reason <reason>"
        ),
    )


def classify_change_coverage(
    plan: LargeChangePlan,
    *,
    result: ReviewResult | None = None,
) -> IndivisibleWorkReport:
    """Classify every non-reviewed unit in ``plan`` (or ``result.coverage``) for G09."""

    if not isinstance(plan, LargeChangePlan):
        raise ReviewInputError("plan must be a LargeChangePlan")
    if result is not None and not isinstance(result, ReviewResult):
        raise ReviewInputError("result must be a ReviewResult when supplied")

    coverage: CoverageManifest = (
        result.coverage
        if result is not None and result.coverage is not None
        else plan.coverage
    )
    limits = coverage.limits
    records_by_path = _record_map(plan.analysis)
    hunks_by_key = _hunk_map(plan.analysis)

    units: list[IndivisibleWorkUnit] = []
    hunk_reported_paths: set[str] = set()

    for hcov in coverage.hunks:
        if hcov.outcome == "reviewed":
            continue
        record = records_by_path.get(hcov.path)
        hunk = hunks_by_key.get((hcov.path, hcov.index))
        hunk_bytes: int | None = None
        if record is not None and hunk is not None:
            hunk_bytes = utf8_size(f"{record.header}{hunk.text}", label="hunk diff")
        units.append(
            _unit_for_reason(
                path=hcov.path,
                hunk_index=hcov.index,
                outcome=hcov.outcome,
                reason=hcov.reason,
                unit_bytes=hunk_bytes,
                limit_bytes=limits.max_diff_bytes,
            )
        )
        hunk_reported_paths.add(hcov.path)

    reviewed_paths: list[str] = []
    for fcov in coverage.files:
        if fcov.outcome == "reviewed":
            reviewed_paths.append(fcov.path)
            continue
        if fcov.path in hunk_reported_paths:
            continue
        record = records_by_path.get(fcov.path)
        file_bytes = (
            utf8_size(record.text, label="file diff")
            if record is not None and not record.binary
            else None
        )
        units.append(
            _unit_for_reason(
                path=fcov.path,
                hunk_index=None,
                outcome=fcov.outcome,
                reason=fcov.reason,
                unit_bytes=file_bytes,
                limit_bytes=limits.max_diff_bytes,
            )
        )

    if not coverage.enumeration_complete:
        units.append(
            IndivisibleWorkUnit(
                unit_kind="incomplete_enumeration",
                path=None,
                hunk_index=None,
                unit_bytes=plan.analysis.diff_bytes,
                limit_bytes=plan.work_budget.max_total_diff_bytes,
                coverage_outcome="incomplete",
                coverage_reason="incomplete-enumeration",
                indivisible=True,
                permitted_routes=(
                    "split_oversized_change",
                    "accept_coverage_uncertainty",
                ),
                diagnostic=(
                    "Diff enumeration is incomplete against total-work bounds; "
                    "missing inventory cannot be treated as zero changes. "
                    "Split the PR or run @reviewsensei accept-coverage-uncertainty --reason <reason>"
                ),
            )
        )

    tokens = incomplete_coverage_from_result(result) if result is not None else ()
    return IndivisibleWorkReport(
        fully_reviewed=coverage.fully_reviewed,
        enumeration_complete=coverage.enumeration_complete,
        approval_state=coverage.approval_state(),
        truncated_mandatory_context=False,
        unbounded_retry_suggested=False,
        units=tuple(units),
        reviewed_paths=tuple(sorted(reviewed_paths)),
        incomplete_coverage_tokens=tokens,
    )


def classify_mandatory_prompt_overflow(
    request: ReviewRequest,
    *,
    active_category_ids: Sequence[str],
    max_prompt_bytes: int,
    rendered_prompt_bytes: int | None = None,
    error: PromptContextOverflow | None = None,
    result: ReviewResult | None = None,
) -> IndivisibleWorkReport:
    """Classify a mandatory prompt-context overflow without truncating pinned rules."""

    if not isinstance(request, ReviewRequest):
        raise ReviewInputError("request must be a ReviewRequest")
    _positive_int(max_prompt_bytes, label="max_prompt_bytes")
    if rendered_prompt_bytes is not None:
        _positive_int(rendered_prompt_bytes, label="rendered_prompt_bytes")
    if error is not None and not isinstance(error, PromptContextOverflow):
        raise ReviewInputError("error must be a PromptContextOverflow when supplied")
    if result is not None and not isinstance(result, ReviewResult):
        raise ReviewInputError("result must be a ReviewResult when supplied")

    active = set(active_category_ids)
    mandatory_sizes: dict[str, int] = {}
    for context in request.lens_contexts:
        if context.category_id not in active:
            continue
        report = context.document_selection
        by_path = (
            {item.path: item for item in report.decisions}
            if isinstance(report, DocumentSelectionReport)
            else {}
        )
        for document in context.documents:
            decision = by_path.get(document.path)
            if decision is None or decision.reason == "pinned":
                mandatory_sizes[document.path] = utf8_size(
                    document.content, label="mandatory document"
                )

    mandatory_docs = tuple(sorted(mandatory_sizes))
    payload_bytes = sum(mandatory_sizes.values())
    observed_bytes = (
        rendered_prompt_bytes if rendered_prompt_bytes is not None else payload_bytes
    )
    primary_path = mandatory_docs[0] if len(mandatory_docs) == 1 else None
    doc_label = (
        ", ".join(mandatory_docs)
        if mandatory_docs
        else "required instructions and complete diff evidence"
    )
    unit = IndivisibleWorkUnit(
        unit_kind="mandatory_prompt_overflow",
        path=primary_path,
        hunk_index=None,
        unit_bytes=observed_bytes,
        limit_bytes=max_prompt_bytes,
        coverage_outcome="unsupported",
        coverage_reason="chunk-preflight-failed",
        indivisible=True,
        mandatory_documents=mandatory_docs,
        permitted_routes=("split_oversized_change", "accept_coverage_uncertainty"),
        diagnostic=(
            f"Mandatory prompt context ({doc_label}; {observed_bytes} UTF-8 bytes) "
            f"exceeds the {max_prompt_bytes}-byte rendered prompt ceiling; "
            "mandatory rules and diff evidence are never truncated. "
            "Narrow the change/pinned rules on a new head or run "
            "@reviewsensei accept-coverage-uncertainty --reason <reason>"
        ),
    )
    tokens = incomplete_coverage_from_result(result) if result is not None else ()
    return IndivisibleWorkReport(
        fully_reviewed=False,
        enumeration_complete=True,
        approval_state="partial",
        truncated_mandatory_context=False,
        unbounded_retry_suggested=False,
        units=(unit,),
        reviewed_paths=(),
        incomplete_coverage_tokens=tokens,
    )


def classify_work_plan_unprocessed(
    work_plan: ReviewWorkPlan,
    *,
    max_diff_bytes: int,
    max_prompt_bytes: int,
) -> tuple[IndivisibleWorkUnit, ...]:
    """Classify unprocessed requirements from ``plan_work`` into bounded G09 units."""

    if not isinstance(work_plan, ReviewWorkPlan):
        raise ReviewInputError("work_plan must be a ReviewWorkPlan")
    _positive_int(max_diff_bytes, label="max_diff_bytes")
    _positive_int(max_prompt_bytes, label="max_prompt_bytes")

    by_id = {req.identity: req for req in work_plan.requirements}
    records = work_plan.bundle.by_id()
    classified: list[IndivisibleWorkUnit] = []

    for identity, reason in work_plan.unprocessed:
        req = by_id[identity]
        matched_records = [records[eid] for eid in req.evidence_ids if eid in records]
        primary_path = matched_records[0].path if matched_records else None
        primary_hunk = req.hunk_indexes[0] if req.hunk_indexes else None
        evidence_bytes = (
            sum(utf8_size(rec.diff, label="evidence diff") for rec in matched_records)
            if matched_records
            else None
        )
        if reason == "mandatory-context-and-evidence-oversized":
            classified.append(
                IndivisibleWorkUnit(
                    unit_kind="mandatory_prompt_overflow",
                    path=primary_path,
                    hunk_index=primary_hunk,
                    unit_bytes=evidence_bytes,
                    limit_bytes=max_prompt_bytes,
                    coverage_outcome="unsupported",
                    coverage_reason="chunk-preflight-failed",
                    indivisible=True,
                    permitted_routes=(
                        "split_oversized_change",
                        "accept_coverage_uncertainty",
                    ),
                    diagnostic=(
                        f"Requirement {identity} failed rendered prompt admission "
                        f"against {max_prompt_bytes}-byte ceiling without truncating "
                        "mandatory context; split the unit or run "
                        "@reviewsensei accept-coverage-uncertainty --reason <reason>"
                    ),
                )
            )
        elif reason == "provider-call-budget":
            classified.append(
                IndivisibleWorkUnit(
                    unit_kind="budget_exhausted",
                    path=primary_path,
                    hunk_index=primary_hunk,
                    unit_bytes=evidence_bytes,
                    limit_bytes=max_diff_bytes,
                    coverage_outcome="budget-exhausted",
                    coverage_reason="provider-call-budget",
                    indivisible=False,
                    permitted_routes=(
                        "review_remaining",
                        "accept_coverage_uncertainty",
                    ),
                    diagnostic=(
                        f"Requirement {identity} deferred by provider-call budget; "
                        "continue via @reviewsensei review remaining or "
                        "@reviewsensei accept-coverage-uncertainty --reason <reason>"
                    ),
                )
            )
        elif reason == "required-evidence-missing":
            classified.append(
                IndivisibleWorkUnit(
                    unit_kind="incomplete_enumeration",
                    path=primary_path,
                    hunk_index=primary_hunk,
                    unit_bytes=evidence_bytes,
                    limit_bytes=max_diff_bytes,
                    coverage_outcome="unsupported",
                    coverage_reason="incomplete-enumeration",
                    indivisible=True,
                    permitted_routes=(
                        "split_oversized_change",
                        "accept_coverage_uncertainty",
                    ),
                    diagnostic=(
                        f"Requirement {identity} lacks complete diff evidence; "
                        "split the change or run "
                        "@reviewsensei accept-coverage-uncertainty --reason <reason>"
                    ),
                )
            )
        else:
            classified.append(
                IndivisibleWorkUnit(
                    unit_kind="oversized_hunk",
                    path=primary_path,
                    hunk_index=primary_hunk,
                    unit_bytes=evidence_bytes,
                    limit_bytes=max_diff_bytes,
                    coverage_outcome="unsupported",
                    coverage_reason="too-large-hunk",
                    indivisible=True,
                    permitted_routes=(
                        "split_oversized_change",
                        "accept_coverage_uncertainty",
                    ),
                    diagnostic=(
                        f"Requirement {identity} ({reason}) exceeds {max_diff_bytes}-byte "
                        "batch ceiling as an indivisible unit; split the change or run "
                        "@reviewsensei accept-coverage-uncertainty --reason <reason>"
                    ),
                )
            )

    return tuple(classified)


__all__ = [
    "INDIVISIBLE_UNIT_KINDS",
    "PERMITTED_RECOVERY_ROUTES",
    "RECOVERY_COMMAND_TEMPLATES",
    "IndivisibleWorkReport",
    "IndivisibleWorkUnit",
    "classify_change_coverage",
    "classify_mandatory_prompt_overflow",
    "classify_work_plan_unprocessed",
]
