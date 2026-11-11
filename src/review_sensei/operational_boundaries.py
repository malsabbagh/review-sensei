"""Explicit operational boundary classifications and bounded recovery contracts.

This module formalizes ReviewSensei's fail-closed operational limits and the
deterministic, bounded recovery actions available when a run hits one of those
limits without silently truncating evidence, rebinding authority, or expanding
approved resource budgets.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
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
    DEFAULT_REVIEW_LIMITS,
    utf8_size,
    validate_bounded_text,
    validate_repository_path,
)

_HEX_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

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


TITLE_LIFECYCLE_PHASES = frozenset(
    {
        "before_admission",
        "during_inference",
        "before_publication",
        "after_publication",
        "during_partial_continuation",
    }
)

TITLE_FRESHNESS_OUTCOMES = frozenset(
    {
        "adopt_live_title",
        "refuse_conflicting_cli_title",
        "preserve_captured_snapshot_title",
        "retain_incremental_baseline_without_reinference",
        "refuse_mixed_title_partial_continuation",
    }
)


def _validate_sha(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _HEX_SHA_RE.fullmatch(value) is None:
        raise ReviewInputError(f"{label} must be a 40-character lowercase hex SHA")
    return value


@dataclass(frozen=True)
class TitleAttributionRecord:
    """Truthful attribution for a published review across post-capture title edits."""

    evaluated_title: str
    live_title: str
    base_sha: str
    head_sha: str
    evaluates_live_title: bool
    invalidates_settled_approval: bool
    attribution_note: str

    def __post_init__(self) -> None:
        validate_bounded_text(
            self.evaluated_title,
            DEFAULT_REVIEW_LIMITS.max_title_bytes,
            label="evaluated_title",
        )
        validate_bounded_text(
            self.live_title,
            DEFAULT_REVIEW_LIMITS.max_title_bytes,
            label="live_title",
        )
        _validate_sha(self.base_sha, label="base_sha")
        _validate_sha(self.head_sha, label="head_sha")
        if not isinstance(self.evaluates_live_title, bool) or not isinstance(
            self.invalidates_settled_approval, bool
        ):
            raise ReviewInputError("title attribution boolean flags must be booleans")
        if self.evaluates_live_title != (self.evaluated_title == self.live_title):
            raise ReviewInputError(
                "published review must never claim to have evaluated a changed live title"
            )
        if self.invalidates_settled_approval:
            raise ReviewInputError(
                "cosmetic title edits alone must not invalidate settled code approvals"
            )
        validate_bounded_text(
            self.attribution_note,
            2048,
            label="attribution_note",
            allow_empty=False,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "evaluated_title": self.evaluated_title,
            "live_title": self.live_title,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "evaluates_live_title": self.evaluates_live_title,
            "invalidates_settled_approval": self.invalidates_settled_approval,
            "attribution_note": self.attribution_note,
        }


@dataclass(frozen=True)
class TitleFreshnessDecision:
    """G19 decision for PR title edits across the five review lifecycle phases."""

    phase: str
    outcome: str
    admitted: bool
    effective_title: str | None
    live_title: str
    captured_title: str | None
    incremental_cache_valid: bool
    invalidates_settled_approval: bool
    requires_paid_reinference: bool
    recovery_route: str | None
    diagnostic: str

    def __post_init__(self) -> None:
        if self.phase not in TITLE_LIFECYCLE_PHASES:
            raise ReviewInputError("title lifecycle phase is unsupported")
        if self.outcome not in TITLE_FRESHNESS_OUTCOMES:
            raise ReviewInputError("title freshness outcome is unsupported")
        for flag_name, flag_val in (
            ("admitted", self.admitted),
            ("incremental_cache_valid", self.incremental_cache_valid),
            ("invalidates_settled_approval", self.invalidates_settled_approval),
            ("requires_paid_reinference", self.requires_paid_reinference),
        ):
            if not isinstance(flag_val, bool):
                raise ReviewInputError(f"{flag_name} must be a boolean")
        validate_bounded_text(
            self.live_title,
            DEFAULT_REVIEW_LIMITS.max_title_bytes,
            label="live_title",
        )
        if self.captured_title is not None:
            validate_bounded_text(
                self.captured_title,
                DEFAULT_REVIEW_LIMITS.max_title_bytes,
                label="captured_title",
            )
        if self.effective_title is not None:
            validate_bounded_text(
                self.effective_title,
                DEFAULT_REVIEW_LIMITS.max_title_bytes,
                label="effective_title",
            )
        if self.admitted and self.effective_title is None:
            raise ReviewInputError("admitted title decision requires effective_title")
        if not self.admitted and self.effective_title is not None:
            raise ReviewInputError(
                "refused title decision must not expose effective_title"
            )
        if self.invalidates_settled_approval:
            raise ReviewInputError(
                "title-only edits must never invalidate settled code approvals"
            )
        validate_bounded_text(
            self.diagnostic,
            2048,
            label="title freshness diagnostic",
            allow_empty=False,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "outcome": self.outcome,
            "admitted": self.admitted,
            "effective_title": self.effective_title,
            "live_title": self.live_title,
            "captured_title": self.captured_title,
            "incremental_cache_valid": self.incremental_cache_valid,
            "invalidates_settled_approval": self.invalidates_settled_approval,
            "requires_paid_reinference": self.requires_paid_reinference,
            "recovery_route": self.recovery_route,
            "diagnostic": self.diagnostic,
        }


def evaluate_title_freshness(
    *,
    phase: str,
    live_title: str,
    captured_title: str | None = None,
    explicit_cli_title: str | None = None,
) -> TitleFreshnessDecision:
    """Evaluate PR title freshness across the five G19 lifecycle timing points."""

    if phase not in TITLE_LIFECYCLE_PHASES:
        raise ReviewInputError("title lifecycle phase is unsupported")
    validate_bounded_text(
        live_title,
        DEFAULT_REVIEW_LIMITS.max_title_bytes,
        label="live_title",
    )
    if explicit_cli_title is not None:
        validate_bounded_text(
            explicit_cli_title,
            DEFAULT_REVIEW_LIMITS.max_title_bytes,
            label="explicit_cli_title",
        )
    if phase == "before_admission":
        if explicit_cli_title not in {None, "", live_title}:
            return TitleFreshnessDecision(
                phase=phase,
                outcome="refuse_conflicting_cli_title",
                admitted=False,
                effective_title=None,
                live_title=live_title,
                captured_title=captured_title,
                incremental_cache_valid=True,
                invalidates_settled_approval=False,
                requires_paid_reinference=False,
                recovery_route="omit conflicting --title or pass the live PR title",
                diagnostic=(
                    "Explicit --title conflicts with the live authenticated PR title "
                    "at bootstrap; refusing before authority or provider spend."
                ),
            )
        return TitleFreshnessDecision(
            phase=phase,
            outcome="adopt_live_title",
            admitted=True,
            effective_title=live_title,
            live_title=live_title,
            captured_title=live_title,
            incremental_cache_valid=True,
            invalidates_settled_approval=False,
            requires_paid_reinference=False,
            recovery_route=None,
            diagnostic=(
                "Admission binds the live authenticated PR title before provider work."
            ),
        )

    if captured_title is None:
        raise ReviewInputError(f"captured_title is required during phase {phase}")
    validate_bounded_text(
        captured_title,
        DEFAULT_REVIEW_LIMITS.max_title_bytes,
        label="captured_title",
    )

    if phase in {"during_inference", "before_publication"}:
        changed = captured_title != live_title
        detail = (
            "In-flight snapshot completes truthfully against the admission-captured "
            "title without claiming the post-capture live title was analyzed."
            if changed
            else "Bound snapshot title remains current through publication."
        )
        return TitleFreshnessDecision(
            phase=phase,
            outcome="preserve_captured_snapshot_title",
            admitted=True,
            effective_title=captured_title,
            live_title=live_title,
            captured_title=captured_title,
            incremental_cache_valid=True,
            invalidates_settled_approval=False,
            requires_paid_reinference=False,
            recovery_route="@reviewsensei rescan" if changed else None,
            diagnostic=detail,
        )

    if phase == "after_publication":
        changed = captured_title != live_title
        return TitleFreshnessDecision(
            phase=phase,
            outcome="retain_incremental_baseline_without_reinference",
            admitted=True,
            effective_title=captured_title,
            live_title=live_title,
            captured_title=captured_title,
            incremental_cache_valid=True,
            invalidates_settled_approval=False,
            requires_paid_reinference=False,
            recovery_route="@reviewsensei rescan" if changed else None,
            diagnostic=(
                "PR title is descriptive context in incremental cache keys: "
                "post-publication title edits do not trigger duplicate paid inference "
                "or invalidate settled approvals, and earlier reviews never claim to "
                "have evaluated the later title."
            ),
        )

    # phase == "during_partial_continuation"
    if captured_title != live_title:
        return TitleFreshnessDecision(
            phase=phase,
            outcome="refuse_mixed_title_partial_continuation",
            admitted=False,
            effective_title=None,
            live_title=live_title,
            captured_title=captured_title,
            incremental_cache_valid=True,
            invalidates_settled_approval=False,
            requires_paid_reinference=False,
            recovery_route="@reviewsensei rescan",
            diagnostic=(
                "Partial continuation (@reviewsensei review remaining) binds the exact "
                "original request scope including title; mixed-title continuation is "
                "refused before provider spend. Run @reviewsensei rescan to review "
                "cleanly under the updated PR title."
            ),
        )
    return TitleFreshnessDecision(
        phase=phase,
        outcome="preserve_captured_snapshot_title",
        admitted=True,
        effective_title=captured_title,
        live_title=live_title,
        captured_title=captured_title,
        incremental_cache_valid=True,
        invalidates_settled_approval=False,
        requires_paid_reinference=False,
        recovery_route=None,
        diagnostic=(
            "Partial continuation matches the original request scope and title."
        ),
    )


def verify_published_title_attribution(
    *,
    evaluated_title: str,
    live_title: str,
    base_sha: str,
    head_sha: str,
) -> TitleAttributionRecord:
    """Construct truthful title attribution for an already-captured/published review."""

    matches = evaluated_title == live_title
    note = (
        f"Review evaluated snapshot {base_sha[:12]}..{head_sha[:12]} with current PR title."
        if matches
        else (
            f"Review evaluated snapshot {base_sha[:12]}..{head_sha[:12]} under the "
            "earlier captured PR title; later PR title edits were not analyzed by "
            "that run, while settled code approvals remain intact."
        )
    )
    return TitleAttributionRecord(
        evaluated_title=evaluated_title,
        live_title=live_title,
        base_sha=base_sha,
        head_sha=head_sha,
        evaluates_live_title=matches,
        invalidates_settled_approval=False,
        attribution_note=note,
    )


HOSTED_BOOTSTRAP_MAX_TITLE_BYTES = 256
DIRECT_CORE_MAX_TITLE_BYTES = DEFAULT_REVIEW_LIMITS.max_title_bytes
TITLE_ENTRY_SURFACES = frozenset(
    {
        "routing_preflight",
        "hosted_bootstrap",
        "direct_core",
    }
)
HOSTED_ENTRY_PURPOSES = frozenset({"review", "command", "reply"})


@dataclass(frozen=True)
class TitleByteLimitEvaluation:
    """G20 UTF-8 byte-limit evaluation across routing, hosted bootstrap, and core."""

    surface: str
    hosted_purpose: str | None
    validated: bool
    admitted: bool
    utf8_bytes: int | None
    codepoint_length: int | None
    limit_bytes: int | None
    truncated: bool
    recovery_instruction: str | None
    diagnostic: str

    def __post_init__(self) -> None:
        if self.surface not in TITLE_ENTRY_SURFACES:
            raise ReviewInputError("title entry surface is unsupported")
        if self.surface == "hosted_bootstrap":
            if self.hosted_purpose not in HOSTED_ENTRY_PURPOSES:
                raise ReviewInputError(
                    "hosted_bootstrap evaluation requires a valid hosted_purpose"
                )
        elif self.hosted_purpose is not None:
            raise ReviewInputError(
                "hosted_purpose is only valid on the hosted_bootstrap surface"
            )
        for flag_name, flag_val in (
            ("validated", self.validated),
            ("admitted", self.admitted),
            ("truncated", self.truncated),
        ):
            if not isinstance(flag_val, bool):
                raise ReviewInputError(f"{flag_name} must be a boolean")
        if self.truncated:
            raise ReviewInputError("silent title truncation is forbidden")
        if self.surface == "routing_preflight" and self.validated:
            raise ReviewInputError(
                "routing_preflight must never claim title validation credit"
            )
        if self.utf8_bytes is not None:
            _positive_int(self.utf8_bytes, label="utf8_bytes", allow_zero=True)
        if self.codepoint_length is not None:
            _positive_int(
                self.codepoint_length, label="codepoint_length", allow_zero=True
            )
        if self.limit_bytes is not None:
            _positive_int(self.limit_bytes, label="limit_bytes")
        if not self.admitted and not self.recovery_instruction:
            raise ReviewInputError(
                "refused title evaluation requires an explicit recovery_instruction"
            )
        validate_bounded_text(
            self.diagnostic,
            2048,
            label="title byte-limit diagnostic",
            allow_empty=False,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "surface": self.surface,
            "hosted_purpose": self.hosted_purpose,
            "validated": self.validated,
            "admitted": self.admitted,
            "utf8_bytes": self.utf8_bytes,
            "codepoint_length": self.codepoint_length,
            "limit_bytes": self.limit_bytes,
            "truncated": self.truncated,
            "recovery_instruction": self.recovery_instruction,
            "diagnostic": self.diagnostic,
        }


def evaluate_title_byte_limit(
    title: object,
    *,
    surface: str,
    hosted_purpose: str | None = None,
    custom_core_limit: int | None = None,
) -> TitleByteLimitEvaluation:
    """Evaluate PR title UTF-8 byte bounds on ``surface`` without truncation."""

    if surface not in TITLE_ENTRY_SURFACES:
        raise ReviewInputError("title entry surface is unsupported")
    if surface == "hosted_bootstrap":
        purpose = hosted_purpose or "review"
        if purpose not in HOSTED_ENTRY_PURPOSES:
            raise ReviewInputError("hosted_purpose must be review, command, or reply")
        if custom_core_limit is not None:
            raise ReviewInputError(
                "custom_core_limit cannot override hosted_bootstrap 256-byte limit"
            )
        limit = HOSTED_BOOTSTRAP_MAX_TITLE_BYTES
    elif surface == "routing_preflight":
        if hosted_purpose is not None or custom_core_limit is not None:
            raise ReviewInputError(
                "routing_preflight does not accept hosted_purpose or custom_core_limit"
            )
        return TitleByteLimitEvaluation(
            surface=surface,
            hosted_purpose=None,
            validated=False,
            admitted=True,
            utf8_bytes=None,
            codepoint_length=None,
            limit_bytes=None,
            truncated=False,
            recovery_instruction=None,
            diagnostic=(
                "Routing-only preflight does not fetch or validate the live pull "
                "request title; zero title validation credit is claimed before "
                "metered bootstrap."
            ),
        )
    else:
        purpose = None
        if hosted_purpose is not None:
            raise ReviewInputError("hosted_purpose is invalid for direct_core")
        if custom_core_limit is not None:
            _positive_int(custom_core_limit, label="custom_core_limit")
            if custom_core_limit > DIRECT_CORE_MAX_TITLE_BYTES:
                raise ReviewInputError(
                    "custom_core_limit exceeds DIRECT_CORE_MAX_TITLE_BYTES"
                )
            limit = custom_core_limit
        else:
            limit = DIRECT_CORE_MAX_TITLE_BYTES

    if not isinstance(title, str):
        return TitleByteLimitEvaluation(
            surface=surface,
            hosted_purpose=purpose,
            validated=True,
            admitted=False,
            utf8_bytes=None,
            codepoint_length=None,
            limit_bytes=limit,
            truncated=False,
            recovery_instruction=(
                f"Provide a valid UTF-8 pull request title of <= {limit} UTF-8 bytes."
            ),
            diagnostic=(
                f"Pull request title on {surface} is not a UTF-8 string; "
                "refusing before authority, session enrollment, or provider spend."
            ),
        )
    try:
        encoded = title.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return TitleByteLimitEvaluation(
            surface=surface,
            hosted_purpose=purpose,
            validated=True,
            admitted=False,
            utf8_bytes=None,
            codepoint_length=len(title),
            limit_bytes=limit,
            truncated=False,
            recovery_instruction=(
                f"Remove invalid UTF-8 surrogates and keep the pull request title "
                f"<= {limit} UTF-8 bytes."
            ),
            diagnostic=(
                f"Pull request title on {surface} contains invalid UTF-8 surrogates; "
                "refusing without normalization or truncation."
            ),
        )

    byte_len = len(encoded)
    cp_len = len(title)
    if byte_len > limit:
        scope_label = (
            f"hosted_bootstrap ({purpose})" if purpose is not None else "direct_core"
        )
        return TitleByteLimitEvaluation(
            surface=surface,
            hosted_purpose=purpose,
            validated=True,
            admitted=False,
            utf8_bytes=byte_len,
            codepoint_length=cp_len,
            limit_bytes=limit,
            truncated=False,
            recovery_instruction=(
                f"Shorten the pull request title to <= {limit} UTF-8 bytes "
                f"(currently {byte_len} UTF-8 bytes across {cp_len} characters) "
                "and re-run."
            ),
            diagnostic=(
                f"Pull request title ({byte_len} UTF-8 bytes, {cp_len} characters) "
                f"exceeds {scope_label} {limit}-byte bound; refusing before business "
                "or provider work without truncating."
            ),
        )

    return TitleByteLimitEvaluation(
        surface=surface,
        hosted_purpose=purpose,
        validated=True,
        admitted=True,
        utf8_bytes=byte_len,
        codepoint_length=cp_len,
        limit_bytes=limit,
        truncated=False,
        recovery_instruction=None,
        diagnostic=(
            f"Pull request title ({byte_len} UTF-8 bytes <= {limit}-byte bound) "
            f"admitted on {surface}."
        ),
    )


POLICY_DRIFT_KINDS = frozenset(
    {
        "admitted",
        "unrelated_default_branch_advance",
        "policy_content_changed",
        "malformed_policy_ref",
    }
)


@dataclass(frozen=True)
class PolicyDriftClassification:
    """G21 pre-admission classification for frozen policy commit P vs live P_live."""

    drift_kind: str
    admitted: bool
    frozen_policy_commit: str
    live_policy_commit: str | None
    reviewed_base_sha: str
    reviewed_head_sha: str
    policy_distinct_from_reviewed_base: bool
    policy_content_identical: bool | None
    refuses_before_enrollment: bool
    zero_ledger_mutation: bool
    zero_provider_spend: bool
    rebinds_prior_accounting: bool
    recovery_policy_commit: str | None
    recovery_instruction: str | None
    diagnostic: str

    def __post_init__(self) -> None:
        if self.drift_kind not in POLICY_DRIFT_KINDS:
            raise ReviewInputError("policy drift_kind is unsupported")
        _validate_sha(self.frozen_policy_commit, label="frozen_policy_commit")
        if self.live_policy_commit is not None:
            _validate_sha(self.live_policy_commit, label="live_policy_commit")
        _validate_sha(self.reviewed_base_sha, label="reviewed_base_sha")
        _validate_sha(self.reviewed_head_sha, label="reviewed_head_sha")
        if self.recovery_policy_commit is not None:
            _validate_sha(self.recovery_policy_commit, label="recovery_policy_commit")
        for flag_name, flag_val in (
            ("admitted", self.admitted),
            (
                "policy_distinct_from_reviewed_base",
                self.policy_distinct_from_reviewed_base,
            ),
            ("refuses_before_enrollment", self.refuses_before_enrollment),
            ("zero_ledger_mutation", self.zero_ledger_mutation),
            ("zero_provider_spend", self.zero_provider_spend),
            ("rebinds_prior_accounting", self.rebinds_prior_accounting),
        ):
            if not isinstance(flag_val, bool):
                raise ReviewInputError(f"{flag_name} must be a boolean")
        if self.policy_content_identical is not None and not isinstance(
            self.policy_content_identical, bool
        ):
            raise ReviewInputError("policy_content_identical must be a boolean or None")
        if self.rebinds_prior_accounting:
            raise ReviewInputError(
                "prior session accounting must never be rebound to a changed policy commit"
            )
        if self.policy_distinct_from_reviewed_base != (
            self.frozen_policy_commit != self.reviewed_base_sha
        ):
            raise ReviewInputError(
                "policy_distinct_from_reviewed_base does not match P != B"
            )
        if not self.admitted:
            if not (
                self.refuses_before_enrollment
                and self.zero_ledger_mutation
                and self.zero_provider_spend
            ):
                raise ReviewInputError(
                    "refused policy drift must fail closed before enrollment and provider spend"
                )
            if not self.recovery_instruction:
                raise ReviewInputError(
                    "refused policy drift requires an explicit recovery_instruction"
                )
        validate_bounded_text(
            self.diagnostic,
            2048,
            label="policy drift diagnostic",
            allow_empty=False,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "drift_kind": self.drift_kind,
            "admitted": self.admitted,
            "frozen_policy_commit": self.frozen_policy_commit,
            "live_policy_commit": self.live_policy_commit,
            "reviewed_base_sha": self.reviewed_base_sha,
            "reviewed_head_sha": self.reviewed_head_sha,
            "policy_distinct_from_reviewed_base": (
                self.policy_distinct_from_reviewed_base
            ),
            "policy_content_identical": self.policy_content_identical,
            "refuses_before_enrollment": self.refuses_before_enrollment,
            "zero_ledger_mutation": self.zero_ledger_mutation,
            "zero_provider_spend": self.zero_provider_spend,
            "rebinds_prior_accounting": self.rebinds_prior_accounting,
            "recovery_policy_commit": self.recovery_policy_commit,
            "recovery_instruction": self.recovery_instruction,
            "diagnostic": self.diagnostic,
        }


def classify_policy_commit_drift(
    *,
    frozen_policy_commit: str,
    default_ref_object: object,
    reviewed_base_sha: str,
    reviewed_head_sha: str,
    frozen_policy_bytes: bytes | None = None,
    live_policy_bytes: bytes | None = None,
) -> PolicyDriftClassification:
    """Classify default-branch policy commit drift (P vs P_live) distinct from B and H."""

    _validate_sha(frozen_policy_commit, label="frozen_policy_commit")
    _validate_sha(reviewed_base_sha, label="reviewed_base_sha")
    _validate_sha(reviewed_head_sha, label="reviewed_head_sha")
    for bytes_label, bytes_val in (
        ("frozen_policy_bytes", frozen_policy_bytes),
        ("live_policy_bytes", live_policy_bytes),
    ):
        if bytes_val is not None and not isinstance(bytes_val, bytes):
            raise ReviewInputError(f"{bytes_label} must be bytes when supplied")

    distinct_from_base = frozen_policy_commit != reviewed_base_sha
    if (
        not isinstance(default_ref_object, Mapping)
        or default_ref_object.get("type") != "commit"
        or not isinstance(default_ref_object.get("sha"), str)
        or _HEX_SHA_RE.fullmatch(default_ref_object["sha"]) is None
    ):
        return PolicyDriftClassification(
            drift_kind="malformed_policy_ref",
            admitted=False,
            frozen_policy_commit=frozen_policy_commit,
            live_policy_commit=None,
            reviewed_base_sha=reviewed_base_sha,
            reviewed_head_sha=reviewed_head_sha,
            policy_distinct_from_reviewed_base=distinct_from_base,
            policy_content_identical=None,
            refuses_before_enrollment=True,
            zero_ledger_mutation=True,
            zero_provider_spend=True,
            rebinds_prior_accounting=False,
            recovery_policy_commit=None,
            recovery_instruction=(
                "Ensure the repository default branch resolves to a valid commit "
                "object and re-dispatch a fresh run."
            ),
            diagnostic=(
                "Repository default-branch ref object is missing or is not a commit "
                "object; refusing before policy load, session enrollment, or provider spend."
            ),
        )

    live_sha: str = default_ref_object["sha"]
    if live_sha == frozen_policy_commit:
        return PolicyDriftClassification(
            drift_kind="admitted",
            admitted=True,
            frozen_policy_commit=frozen_policy_commit,
            live_policy_commit=live_sha,
            reviewed_base_sha=reviewed_base_sha,
            reviewed_head_sha=reviewed_head_sha,
            policy_distinct_from_reviewed_base=distinct_from_base,
            policy_content_identical=True,
            refuses_before_enrollment=False,
            zero_ledger_mutation=False,
            zero_provider_spend=False,
            rebinds_prior_accounting=False,
            recovery_policy_commit=None,
            recovery_instruction=None,
            diagnostic=(
                f"Frozen policy commit {frozen_policy_commit[:12]} matches live "
                "default-branch commit (evaluated independently of reviewed base/head)."
            ),
        )

    if (
        frozen_policy_bytes is not None
        and live_policy_bytes is not None
        and frozen_policy_bytes == live_policy_bytes
    ):
        return PolicyDriftClassification(
            drift_kind="unrelated_default_branch_advance",
            admitted=False,
            frozen_policy_commit=frozen_policy_commit,
            live_policy_commit=live_sha,
            reviewed_base_sha=reviewed_base_sha,
            reviewed_head_sha=reviewed_head_sha,
            policy_distinct_from_reviewed_base=distinct_from_base,
            policy_content_identical=True,
            refuses_before_enrollment=True,
            zero_ledger_mutation=True,
            zero_provider_spend=True,
            rebinds_prior_accounting=False,
            recovery_policy_commit=live_sha,
            recovery_instruction=(
                "Re-dispatch or trigger the next event against the current "
                f"default-branch policy commit {live_sha}."
            ),
            diagnostic=(
                f"Default branch advanced from {frozen_policy_commit[:12]} to "
                f"{live_sha[:12]} before admission (with byte-identical "
                ".reviewsensei.yml); strict commit-identity guard refuses before "
                "enrollment or provider spend."
            ),
        )

    content_identical = (
        False
        if frozen_policy_bytes is not None and live_policy_bytes is not None
        else None
    )
    return PolicyDriftClassification(
        drift_kind="policy_content_changed",
        admitted=False,
        frozen_policy_commit=frozen_policy_commit,
        live_policy_commit=live_sha,
        reviewed_base_sha=reviewed_base_sha,
        reviewed_head_sha=reviewed_head_sha,
        policy_distinct_from_reviewed_base=distinct_from_base,
        policy_content_identical=content_identical,
        refuses_before_enrollment=True,
        zero_ledger_mutation=True,
        zero_provider_spend=True,
        rebinds_prior_accounting=False,
        recovery_policy_commit=live_sha,
        recovery_instruction=(
            "Re-dispatch or trigger a fresh run pinned to the updated "
            f"default-branch policy commit {live_sha}."
        ),
        diagnostic=(
            f"Default-branch policy commit drifted from {frozen_policy_commit[:12]} "
            f"to {live_sha[:12]} with modified or unverified policy content; "
            "refusing before enrollment or provider spend without rebinding prior accounting."
        ),
    )


__all__ = [
    "DIRECT_CORE_MAX_TITLE_BYTES",
    "HOSTED_BOOTSTRAP_MAX_TITLE_BYTES",
    "HOSTED_ENTRY_PURPOSES",
    "INDIVISIBLE_UNIT_KINDS",
    "PERMITTED_RECOVERY_ROUTES",
    "POLICY_DRIFT_KINDS",
    "RECOVERY_COMMAND_TEMPLATES",
    "TITLE_ENTRY_SURFACES",
    "TITLE_FRESHNESS_OUTCOMES",
    "TITLE_LIFECYCLE_PHASES",
    "IndivisibleWorkReport",
    "IndivisibleWorkUnit",
    "PolicyDriftClassification",
    "TitleAttributionRecord",
    "TitleByteLimitEvaluation",
    "TitleFreshnessDecision",
    "classify_change_coverage",
    "classify_mandatory_prompt_overflow",
    "classify_policy_commit_drift",
    "classify_work_plan_unprocessed",
    "evaluate_title_byte_limit",
    "evaluate_title_freshness",
    "verify_published_title_attribution",
]
