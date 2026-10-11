"""Closed host-derived mixed AI and human coverage decision.

Recording a confirmation is not approval. This module does not call GitHub,
does not edit a review result, and does not mark AI status, baseline, or cache
complete. Public APPROVE integration is still pending: the shared finalizer
can call ``evaluate_mixed_coverage`` later.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .coverage import CoverageManifest
from .errors import ReviewInputError
from .evidence import evidence_digest
from .human_file_review import UnsupportedFile, coverage_only_binary, validate_inventory
from .models import ReviewResult

MIXED_COVERAGE_SCHEMA = "mixed-coverage-v1"
_RECEIPT_DOMAIN = "reviewsensei:human-file-receipt-identity:v1"


@dataclass(frozen=True)
class BinaryConfirmation:
    """One already-authenticated confirmation of an exact binary change.

    The host supplies validity, permission, and freshness. This object does not
    read GitHub and does not itself prove those facts.
    """

    file: UnsupportedFile
    event_id: str
    valid: bool = True
    permission_current: bool = True
    current: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.file, UnsupportedFile):
            raise ReviewInputError("human file confirmation identity is invalid")
        if not isinstance(self.event_id, str) or not self.event_id:
            raise ReviewInputError("human file confirmation event is invalid")
        for label, value in (
            ("valid", self.valid),
            ("permission_current", self.permission_current),
            ("current", self.current),
        ):
            if not isinstance(value, bool):
                raise ReviewInputError(f"human file confirmation {label} is invalid")


@dataclass(frozen=True)
class MixedCoverageDecision:
    """Approval decision object. The AI result status is reported unchanged."""

    approved: bool
    coverage_satisfied: bool
    coverage_only_partial: bool
    ai_review_status: str
    blockers: tuple[str, ...]
    receipt_id: str | None
    minted_receipt: bool
    binary_blocker_remains: bool
    publishes_approve: bool = False
    baseline_completed: bool = False
    cache_completed: bool = False

    def __post_init__(self) -> None:
        if self.publishes_approve or self.baseline_completed or self.cache_completed:
            raise ReviewInputError(
                "mixed coverage cannot publish approval or complete retained state"
            )
        if self.approved and (
            not self.coverage_satisfied or self.ai_review_status != "partial"
        ):
            raise ReviewInputError(
                "mixed approval cannot complete the AI result or skip coverage"
            )
        if self.coverage_satisfied and not self.coverage_only_partial:
            raise ReviewInputError(
                "mixed coverage satisfaction lacks binary provenance"
            )
        if self.coverage_only_partial and self.ai_review_status != "partial":
            raise ReviewInputError("coverage-only partial provenance is invalid")
        if self.binary_blocker_remains and self.coverage_satisfied:
            raise ReviewInputError(
                "binary coverage blocker disagrees with satisfaction"
            )


def receipt_identity(
    *,
    event_id: str,
    inventory: tuple[UnsupportedFile, ...],
    selected_ids: tuple[str, ...],
) -> str:
    """Stable receipt identity for one confirmation event. Replay returns it."""

    return evidence_digest(
        {
            "domain": _RECEIPT_DOMAIN,
            "schema_version": "human-file-receipt-v1",
            "event_id": event_id,
            "selected_ids": list(selected_ids),
            "files": [item.to_dict() for item in inventory],
        }
    )


def _unique(items: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def _coverage_causes(
    coverage: CoverageManifest | None,
    *,
    unknown_file: bool,
    failed_stage: bool,
    provider_output_incomplete: bool,
) -> list[str]:
    causes: list[str] = []
    if unknown_file or coverage is None or not coverage.enumeration_complete:
        causes.append("unknown-file")
    if coverage is None:
        if failed_stage:
            causes.append("failed-stage")
        if provider_output_incomplete:
            causes.append("incomplete-provider-output")
        return causes
    for entry in coverage.files:
        if entry.outcome == "reviewed":
            continue
        if entry.outcome == "unsupported" and entry.reason == "binary":
            continue
        if entry.outcome == "unsupported":
            causes.append("non-binary-unsupported")
        elif entry.reason == "provider-call-budget":
            causes.append("incomplete-provider-output")
        else:
            causes.append("failed-stage")
    if any(entry.outcome != "reviewed" for entry in coverage.hunks):
        causes.append("failed-stage")
    if failed_stage:
        causes.append("failed-stage")
    if provider_output_incomplete:
        causes.append("incomplete-provider-output")
    return causes


def _source_context_incomplete(result: ReviewResult, explicit: bool) -> bool:
    if explicit:
        return True
    source = result.source_context_coverage
    if source is None:
        return False
    enabled = getattr(source, "enabled", False)
    complete = getattr(source, "complete", False)
    return bool(enabled) and not bool(complete)


def _check_reason_metadata(
    coverage: CoverageManifest | None, reason_metadata: Mapping[str, str] | None
) -> None:
    if reason_metadata is None:
        return
    if not isinstance(reason_metadata, Mapping):
        raise ReviewInputError("contradictory reason metadata")
    known: dict[str, str | None] = {}
    if coverage is not None:
        known = {entry.path: entry.reason for entry in coverage.files}
    for path, reason in reason_metadata.items():
        if path not in known or known[path] != reason:
            raise ReviewInputError("contradictory reason metadata")


def evaluate_mixed_coverage(
    result: ReviewResult,
    inventory: tuple[UnsupportedFile, ...],
    confirmations: tuple[BinaryConfirmation, ...],
    *,
    text_completed: bool,
    allow_confirmations: bool,
    event_id: str,
    prior_receipt_id: str | None = None,
    mandatory_document_context_lost: bool = False,
    source_context_incomplete: bool = False,
    unknown_file: bool = False,
    failed_stage: bool = False,
    provider_output_incomplete: bool = False,
    qualification: str = "not-required",
    unresolved_threads: bool | None = False,
    reason_metadata: Mapping[str, str] | None = None,
    schema_version: str = MIXED_COVERAGE_SCHEMA,
) -> MixedCoverageDecision:
    """Return a mixed-coverage approval decision without mutating ``result``.

    ``coverage_satisfied`` is completed AI text and a current valid human
    confirmation for every required regular binary. ``coverage_only_partial``
    is reported only when that binary gap is the complete remaining coverage
    cause. AI status, baseline, and cache stay untouched.
    """

    if schema_version != MIXED_COVERAGE_SCHEMA:
        raise ReviewInputError("mixed coverage schema is unsupported")
    if not isinstance(result, ReviewResult):
        raise ReviewInputError("mixed coverage requires a validated review result")
    if not isinstance(text_completed, bool) or not isinstance(
        allow_confirmations, bool
    ):
        raise ReviewInputError("mixed coverage inputs are invalid")
    if not isinstance(event_id, str) or not event_id:
        raise ReviewInputError("human file confirmation event is invalid")
    if qualification not in {"not-required", "qualified", "missing", "unverified"}:
        raise ReviewInputError("mixed coverage qualification is invalid")
    if unresolved_threads is not None and not isinstance(unresolved_threads, bool):
        raise ReviewInputError("mixed coverage review threads are invalid")
    _check_reason_metadata(result.coverage, reason_metadata)
    if not isinstance(inventory, tuple) or any(
        not isinstance(item, UnsupportedFile) for item in inventory
    ):
        raise ReviewInputError("human file inventory is invalid")
    if not isinstance(confirmations, tuple) or any(
        not isinstance(item, BinaryConfirmation) for item in confirmations
    ):
        raise ReviewInputError("human file confirmations are invalid")

    ai_review_status = result.review_status
    causes = _coverage_causes(
        result.coverage,
        unknown_file=unknown_file,
        failed_stage=failed_stage,
        provider_output_incomplete=provider_output_incomplete,
    )
    if not text_completed:
        causes.append("text-incomplete")
    if ai_review_status != "partial":
        causes.append("review-not-partial")
    if result.persistence_status is not None:
        causes.append("persistence-capacity")
    if mandatory_document_context_lost:
        causes.append("document-context-lost")
    if _source_context_incomplete(result, source_context_incomplete):
        causes.append("source-context-incomplete")
    inventory_matches = False
    if result.coverage is not None and not causes:
        try:
            validate_inventory(
                _inventory_request(result, inventory),
                result.coverage,
            )
        except ReviewInputError:
            causes.append("unknown-file")
        else:
            inventory_matches = True
    elif result.coverage is not None and inventory:
        try:
            validate_inventory(_inventory_request(result, inventory), result.coverage)
        except ReviewInputError:
            if "unknown-file" not in causes:
                causes.append("unknown-file")

    binary_only = (
        text_completed
        and ai_review_status == "partial"
        and result.persistence_status is None
        and inventory_matches
        and result.coverage is not None
        and coverage_only_binary(result.coverage)
        and not causes
    )
    required_ids = tuple(item.file_id for item in inventory)
    confirmed: set[str] = set()
    gates: list[str] = []
    unknown_confirmation = False
    permission_revoked = False
    stale_receipt = False
    if allow_confirmations:
        known = set(required_ids)
        for item in confirmations:
            if item.file.file_id not in known or item.file not in inventory:
                unknown_confirmation = True
                continue
            if not item.valid or not item.permission_current:
                permission_revoked = True
                continue
            if not item.current:
                stale_receipt = True
                continue
            if item.event_id != event_id:
                stale_receipt = True
                continue
            confirmed.add(item.file.file_id)
    selected = tuple(item_id for item_id in required_ids if item_id in confirmed)
    coverage_satisfied = (
        binary_only
        and allow_confirmations
        and set(selected) == set(required_ids)
        and bool(required_ids)
    )
    if binary_only and not coverage_satisfied:
        gates.append("binary-coverage")
    if not allow_confirmations and binary_only:
        gates.append("human-file-policy-disabled")
    if unknown_confirmation:
        gates.append("unknown-file")
    if permission_revoked:
        gates.append("permission-revoked")
    if stale_receipt:
        gates.append("stale-receipt")
    if any(
        comment.blocks_approval or comment.needs_human for comment in result.comments
    ):
        gates.append("unresolved-finding")
    if unresolved_threads is None:
        gates.append("review-threads-incomplete")
    elif unresolved_threads:
        gates.append("unresolved-thread")
    if qualification == "missing":
        gates.append("qualification-missing")
    elif qualification == "unverified":
        gates.append("qualification-unverified")

    blockers = _unique([*causes, *gates])
    coverage_files = result.coverage.files if result.coverage is not None else ()
    binary_blocker_remains = not coverage_satisfied and (
        binary_only or any(item.reason == "binary" for item in coverage_files)
    )
    receipt_id = None
    minted_receipt = False
    if allow_confirmations and selected:
        receipt_id = receipt_identity(
            event_id=event_id,
            inventory=inventory,
            selected_ids=selected,
        )
        if prior_receipt_id is None:
            minted_receipt = True
        elif prior_receipt_id != receipt_id:
            raise ReviewInputError("human file replay receipt identity conflicts")
    return MixedCoverageDecision(
        approved=coverage_satisfied and not blockers,
        coverage_satisfied=coverage_satisfied,
        coverage_only_partial=binary_only,
        ai_review_status=ai_review_status,
        blockers=blockers,
        receipt_id=receipt_id,
        minted_receipt=minted_receipt,
        binary_blocker_remains=binary_blocker_remains,
    )


def _inventory_request(result: ReviewResult, inventory: tuple[UnsupportedFile, ...]):
    from .human_file_review import FileReviewRequest

    return FileReviewRequest(
        "owner/repo",
        1,
        1,
        "a" * 40,
        "b" * 40,
        1,
        result.content_digest(),
        inventory,
    )
