"""Deterministic large-change planning with separate total-work budgets.

Per-request ``ReviewLimits`` stay fail-closed.  This module adds aggregate
ceilings so a change can be partitioned into bounded chunks without raising
those per-request limits or silently truncating work.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Callable, Sequence

from .budgets import EffectiveWorkBudget
from .coverage import (
    COVERAGE_OUTCOMES,
    CoverageManifest,
    FileCoverage,
    HunkCoverage,
    _validate_reason,
)
from .dependencies import lockfile_kind
from .diff import DiffAnalysis, DiffFileRecord, DiffHunk, analyze_diff
from .errors import ReviewInputError
from .evidence import EvidenceBundle, EvidenceRecord, EvidenceSnapshot, evidence_digest
from .validation import (
    DEFAULT_REVIEW_LIMITS,
    DEFAULT_TOTAL_WORK_BUDGET,
    ReviewLimits,
    TotalWorkBudget,
    utf8_size,
    validate_bounded_text,
    validate_repository_path,
)


@dataclass(frozen=True)
class WorkRequirement:
    identity: str
    evidence_ids: tuple[str, ...]
    hunk_indexes: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        validate_bounded_text(
            self.identity, 4096, label="work requirement identity", allow_empty=False
        )
        if (
            not isinstance(self.evidence_ids, tuple)
            or not 1 <= len(self.evidence_ids) <= 64
        ):
            raise ReviewInputError("work requirement evidence is invalid")
        if any(
            not isinstance(item, str)
            or len(item) != 64
            or any(c not in "0123456789abcdef" for c in item)
            for item in self.evidence_ids
        ):
            raise ReviewInputError("work requirement evidence identity is invalid")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ReviewInputError("work requirement evidence is duplicated")
        if (
            not isinstance(self.hunk_indexes, tuple)
            or len(self.hunk_indexes) > DEFAULT_TOTAL_WORK_BUDGET.max_total_hunks
            or any(
                isinstance(index, bool) or not isinstance(index, int) or index < 1
                for index in self.hunk_indexes
            )
        ):
            raise ReviewInputError("work requirement hunk identity is invalid")
        if len(set(self.hunk_indexes)) != len(self.hunk_indexes):
            raise ReviewInputError("work requirement hunk identities are duplicated")


@dataclass(frozen=True)
class WorkBatch:
    mode: str
    requirements: tuple[WorkRequirement, ...]
    records: tuple[EvidenceRecord, ...]

    def __post_init__(self) -> None:
        if (
            self.mode not in {"discovery", "reassessment"}
            or not isinstance(self.requirements, tuple)
            or not isinstance(self.records, tuple)
            or not self.requirements
        ):
            raise ReviewInputError("work batch is invalid")
        if any(
            not isinstance(item, WorkRequirement) for item in self.requirements
        ) or any(not isinstance(item, EvidenceRecord) for item in self.records):
            raise ReviewInputError("work batch types are invalid")
        if len(set(self.requirement_ids)) != len(self.requirements) or len(
            set(self.evidence_ids)
        ) != len(self.records):
            raise ReviewInputError("work batch identities conflict")
        if {key for item in self.requirements for key in item.evidence_ids} != set(
            self.evidence_ids
        ) or len({record.snapshot for record in self.records}) != 1:
            raise ReviewInputError("work batch evidence identity is invalid")

    @property
    def requirement_ids(self) -> tuple[str, ...]:
        return tuple(item.identity for item in self.requirements)

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        return tuple(record.evidence_id for record in self.records)

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(sorted({record.path for record in self.records}))

    @property
    def hunk_indexes(self) -> tuple[int, ...]:
        return tuple(
            sorted({index for item in self.requirements for index in item.hunk_indexes})
        )

    @property
    def diff_context(self) -> str:
        if self.mode == "reassessment":
            return "\n".join(
                f"path={record.path}\n{record.patch}" for record in self.records
            )
        return "".join(record.diff for record in self.records)

    @property
    def batch_id(self) -> str:
        return evidence_digest(
            {
                "domain": "reviewsensei:work-batch:v1",
                "mode": self.mode,
                "requirements": self.requirement_ids,
                "evidence": self.evidence_ids,
                "hunks": self.hunk_indexes,
            }
        )


@dataclass(frozen=True)
class ReviewWorkPlan:
    mode: str
    bundle: EvidenceBundle
    requirements: tuple[WorkRequirement, ...]
    batches: tuple[WorkBatch, ...]
    unprocessed: tuple[tuple[str, str], ...]
    authority_digest: str = ""
    budget_digest: str = ""

    def __post_init__(self) -> None:
        if self.mode not in {"discovery", "reassessment"} or not isinstance(
            self.bundle, EvidenceBundle
        ):
            raise ReviewInputError("work plan is invalid")
        if (
            not isinstance(self.requirements, tuple)
            or len(self.requirements) > DEFAULT_TOTAL_WORK_BUDGET.max_total_hunks
            or any(not isinstance(item, WorkRequirement) for item in self.requirements)
        ):
            raise ReviewInputError("work plan requirements are invalid")
        expected = {item.identity: item for item in self.requirements}
        evidence = self.bundle.by_id()
        assigned: list[str] = []
        for batch in self.batches:
            if (
                batch.mode != self.mode
                or any(
                    expected.get(item.identity) != item for item in batch.requirements
                )
                or any(
                    evidence.get(record.evidence_id) != record
                    for record in batch.records
                )
            ):
                raise ReviewInputError("work plan batches are invalid")
            assigned.extend(batch.requirement_ids)
        assigned.extend(identity for identity, _ in self.unprocessed)
        if (
            len(expected) != len(self.requirements)
            or len(set(assigned)) != len(assigned)
            or set(assigned) != set(expected)
        ):
            raise ReviewInputError(
                "work plan does not account for every requirement exactly once"
            )

    @property
    def plan_id(self) -> str:
        return evidence_digest(
            {
                "domain": "reviewsensei:work-plan:v1",
                "mode": self.mode,
                "evidence": self.bundle.digest,
                "requirements": [
                    (item.identity, item.evidence_ids, item.hunk_indexes)
                    for item in self.requirements
                ],
                "batches": [batch.batch_id for batch in self.batches],
                "unprocessed": self.unprocessed,
                "authority": self.authority_digest,
                "budgets": self.budget_digest,
            }
        )


def plan_work(
    mode: str,
    bundle: EvidenceBundle,
    requirements: Sequence[WorkRequirement],
    *,
    budgets: EffectiveWorkBudget,
    render: Callable[[WorkBatch], object],
    max_batches: int = 8,
    correction: str = "",
    authority_digest: str = "",
) -> ReviewWorkPlan:
    """Pack both modes with one deterministic, rendered-request admission loop.

    Each requirement is atomic: discovery may supply exhaustive hunk requirements;
    reassessment must supply complete file groups. Missing work stays represented.
    """
    from .models import ProviderRequest

    if mode not in {"discovery", "reassessment"} or not isinstance(
        bundle, EvidenceBundle
    ):
        raise ReviewInputError("work plan mode or evidence is invalid")
    if (
        isinstance(max_batches, bool)
        or not isinstance(max_batches, int)
        or not 1 <= max_batches <= 8
    ):
        raise ReviewInputError("work plan batch budget is invalid")
    if budgets.resource_budget is not None:
        max_batches = min(max_batches, budgets.resource_budget.max_provider_calls)
    items = tuple(requirements)
    if any(not isinstance(item, WorkRequirement) for item in items) or len(
        {item.identity for item in items}
    ) != len(items):
        raise ReviewInputError("work requirement identities conflict")
    records = bundle.by_id()
    items = tuple(
        sorted(
            items,
            key=lambda item: (
                tuple(
                    sorted(
                        records[key].path if key in records else key
                        for key in item.evidence_ids
                    )
                ),
                item.identity,
            ),
        )
    )
    batches: list[WorkBatch] = []
    unprocessed: list[tuple[str, str]] = []
    current: tuple[WorkRequirement, ...] = ()

    def batch_for(candidates: tuple[WorkRequirement, ...]) -> WorkBatch:
        ids = {key for item in candidates for key in item.evidence_ids}
        selected = tuple(
            sorted(
                (records[key] for key in ids),
                key=lambda record: (record.path, record.evidence_id),
            )
        )
        return WorkBatch(mode, candidates, selected)

    def fits(candidates: tuple[WorkRequirement, ...]) -> bool:
        batch = batch_for(candidates)
        if mode == "reassessment" and len(candidates) > budgets.max_findings_per_batch:
            return False
        if (
            utf8_size(batch.diff_context, label="batch evidence")
            > budgets.batch_diff_bytes
        ):
            return False
        try:
            request = render(batch)
        except ReviewInputError:
            return False
        if not isinstance(request, ProviderRequest):
            raise ReviewInputError("work renderer must return a ProviderRequest")
        prompt = request.prompt + ("\n\n" + correction if correction else "")
        return budgets.fits_prompt(prompt, output_tokens=request.max_output_tokens)

    def flush() -> None:
        nonlocal current
        if current:
            if len(batches) < max_batches:
                batches.append(batch_for(current))
            else:
                unprocessed.extend(
                    (item.identity, "provider-call-budget") for item in current
                )
            current = ()

    for item in items:
        required = [records.get(key) for key in item.evidence_ids]
        if any(record is None for record in required) or (
            mode == "reassessment"
            and (
                not bundle.enumeration_complete
                or any(
                    record is not None and not record.complete for record in required
                )
            )
        ):
            unprocessed.append((item.identity, "required-evidence-missing"))
            continue
        if not fits((item,)):
            unprocessed.append((item.identity, "required-evidence-oversized"))
            continue
        if current and not fits(current + (item,)):
            flush()
        current += (item,)
    flush()
    return ReviewWorkPlan(
        mode,
        bundle,
        items,
        tuple(batches),
        tuple(sorted(unprocessed)),
        authority_digest,
        budgets.digest,
    )


def plan_continuation(
    previous: ReviewWorkPlan,
    bundle: EvidenceBundle,
    requirements: Sequence[WorkRequirement],
    *,
    reusable: Sequence[WorkBatch],
    budgets: EffectiveWorkBudget,
    render: Callable[[WorkBatch], object],
    correction: str = "",
    deferred: dict[str, str] | None = None,
) -> ReviewWorkPlan:
    """Keep compatible receipts fixed and pack remaining work with plan_work."""
    if (
        bundle.snapshot != previous.bundle.snapshot
        or budgets.digest != previous.budget_digest
    ):
        raise ReviewInputError("continuation snapshot or budget changed")
    by_id = {item.identity: item for item in requirements}
    records = bundle.by_id()
    kept = tuple(
        batch
        for batch in reusable
        if batch in previous.batches
        and all(by_id.get(item.identity) == item for item in batch.requirements)
        and all(records.get(record.evidence_id) == record for record in batch.records)
    )
    deferred = deferred or {}
    if not set(deferred) <= set(by_id):
        raise ReviewInputError("continuation deferred identities are invalid")
    kept = tuple(
        batch for batch in kept if not set(batch.requirement_ids) & set(deferred)
    )
    assigned = {identity for batch in kept for identity in batch.requirement_ids}
    remaining = plan_work(
        previous.mode,
        bundle,
        tuple(
            item
            for item in requirements
            if item.identity not in assigned and item.identity not in deferred
        ),
        budgets=budgets,
        render=render,
        correction=correction,
        authority_digest=previous.authority_digest,
    )
    return ReviewWorkPlan(
        previous.mode,
        bundle,
        tuple(requirements),
        kept + remaining.batches,
        tuple(sorted((*remaining.unprocessed, *deferred.items()))),
        previous.authority_digest,
        budgets.digest,
    )


# Related names use the existing per-request metadata item ceiling. This is
# an admission policy, not a claim that every 64-path payload fits: the normal
# instruction/prompt limits and the durable byte-fit check still apply.
MAX_RELATED_PATHS = DEFAULT_REVIEW_LIMITS.max_metadata_items

GENERATED_SUFFIXES = (".min.js", ".min.css", ".min.map", ".map")
GENERATED_PATH_PREFIXES = ("dist/", "vendor/", "node_modules/", "generated/")


def is_generated_path(path: str) -> bool:
    """Return whether ``path`` matches the explicit generated-file policy."""

    lowered = path.lower()
    if any(lowered.endswith(suffix) for suffix in GENERATED_SUFFIXES):
        return True
    return any(
        lowered == prefix[:-1] or lowered.startswith(prefix)
        for prefix in GENERATED_PATH_PREFIXES
    )


@dataclass(frozen=True)
class ReviewChunk:
    """One per-request slice of a larger change."""

    index: int
    diff: str
    paths: tuple[str, ...]
    related_paths: tuple[str, ...] = ()
    hunk_indexes: tuple[int, ...] = ()


@dataclass(frozen=True)
class LargeChangePlan:
    """Deterministic coverage plan for one change, with optional review chunks."""

    analysis: DiffAnalysis
    coverage: CoverageManifest
    chunks: tuple[ReviewChunk, ...]
    work_budget: TotalWorkBudget = field(default_factory=TotalWorkBudget)
    orchestrated: bool = False

    @property
    def reviewable_chunks(self) -> tuple[ReviewChunk, ...]:
        return self.chunks


def _file_bytes(record: DiffFileRecord) -> int:
    return utf8_size(record.text, label="diff file")


def _fits(
    records: tuple[DiffFileRecord, ...],
    candidate: DiffFileRecord,
    *,
    limits: ReviewLimits,
) -> bool:
    combined = records + (candidate,)
    text = "".join(record.text for record in combined)
    try:
        utf8_size(text, label="chunk diff")
    except ReviewInputError:
        return False
    if len(text.encode("utf-8")) > limits.max_diff_bytes:
        return False
    if sum(record.text.count("\n") for record in combined) > limits.max_diff_lines:
        return False
    paths: set[str] = set()
    hunks = 0
    for record in combined:
        paths.update(record.coverage_paths)
        hunks += len(record.hunks)
    return len(paths) <= limits.max_diff_files and hunks <= limits.max_diff_hunks


def _hunk_record(
    file_record: DiffFileRecord,
    hunk: DiffHunk,
) -> DiffFileRecord:
    text = f"{file_record.header}{hunk.text}"
    return DiffFileRecord(
        old_path=file_record.old_path,
        new_path=file_record.new_path,
        text=text,
        header=file_record.header,
        added_lines=hunk.added_lines,
        deleted_lines=hunk.deleted_lines,
        hunks=(hunk,),
        binary=False,
    )


def _classify_file(
    record: DiffFileRecord,
    *,
    analysis: DiffAnalysis,
    limits: ReviewLimits = DEFAULT_REVIEW_LIMITS,
) -> tuple[str, str | None]:
    paths = record.coverage_paths
    if record.binary or any(path in analysis.binary_paths for path in paths):
        return "unsupported", "binary"
    if not EvidenceRecord.from_diff_record(record, EvidenceSnapshot()).complete:
        return "unsupported", "incomplete-enumeration"
    if any(lockfile_kind(path) == "unsupported" for path in paths):
        return "unsupported", "lockfile-format"
    # npm lockfiles are material review input, even below a generated directory.
    # Keep the raw hunks rather than treating a derived summary as coverage.
    if any(lockfile_kind(path) == "npm" for path in paths):
        return "reviewable", None
    if any(is_generated_path(path) for path in paths):
        return "excluded-by-policy", "generated"
    # Hunkless records cannot be split further; oversized payloads fail closed here.
    # Multi-hunk files are classified as reviewable and handled by chunk packing.
    if not record.hunks and _file_bytes(record) > limits.max_diff_bytes:
        return "unsupported", "too-large-file"
    return "reviewable", None


def _related_paths(path: str, changed: tuple[str, ...]) -> tuple[str, ...]:
    parent = path.rsplit("/", 1)[0] if "/" in path else ""
    related: list[str] = []
    for candidate in changed:
        if candidate == path:
            continue
        candidate_parent = candidate.rsplit("/", 1)[0] if "/" in candidate else ""
        if candidate_parent == parent:
            related.append(candidate)
        if len(related) >= MAX_RELATED_PATHS:
            break
    return tuple(related)


def related_paths_for_change(changed_paths: Sequence[str]) -> tuple[str, ...]:
    """Return bounded same-directory siblings among the changed paths.

    Overflow truncates at ``MAX_RELATED_PATHS`` in changed-path order, matching
    the chunk helper's sibling heuristic: this is derived context, so a large
    change set narrows it instead of failing the review.

    Issue #136 C4 reuses this directory relationship as cross-file impact
    when a caller has not supplied symbol-aware related paths from #40.
    """

    if isinstance(changed_paths, (str, bytes)):
        raise ReviewInputError("changed paths must be a sequence of paths")
    changed = tuple(changed_paths)
    related: list[str] = []
    for path in changed:
        validate_repository_path(path, label="changed path")
    for path in changed:
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        for candidate in changed:
            if candidate == path:
                continue
            candidate_parent = candidate.rsplit("/", 1)[0] if "/" in candidate else ""
            if candidate_parent == parent and candidate not in related:
                related.append(candidate)
                if len(related) >= MAX_RELATED_PATHS:
                    break
        if len(related) >= MAX_RELATED_PATHS:
            break
    return tuple(related)


def _chunk_from_records(
    index: int,
    records: tuple[DiffFileRecord, ...],
    *,
    changed_paths: tuple[str, ...],
) -> ReviewChunk:
    diff = "".join(record.text for record in records)
    paths: list[str] = []
    hunk_indexes: list[int] = []
    for record in records:
        for path in record.coverage_paths:
            if path not in paths:
                paths.append(path)
        hunk_indexes.extend(hunk.index for hunk in record.hunks)
    related: list[str] = []
    for path in paths:
        for candidate in _related_paths(path, changed_paths):
            if candidate not in paths and candidate not in related:
                related.append(candidate)
            if len(related) >= MAX_RELATED_PATHS:
                break
    return ReviewChunk(
        index=index,
        diff=diff,
        paths=tuple(paths),
        related_paths=tuple(related[:MAX_RELATED_PATHS]),
        hunk_indexes=tuple(hunk_indexes),
    )


def _pack_records(
    records: tuple[DiffFileRecord, ...],
    *,
    limits: ReviewLimits,
    work_budget: TotalWorkBudget,
    changed_paths: tuple[str, ...],
) -> tuple[tuple[ReviewChunk, ...], tuple[DiffFileRecord, ...], str | None]:
    """Pack reviewable records into bounded chunks.

    Returns packed chunks, records that could not be packed, and a static
    overflow reason when the total-work chunk budget is exhausted.
    """

    chunks: list[ReviewChunk] = []
    current: tuple[DiffFileRecord, ...] = ()
    overflow: list[DiffFileRecord] = []
    overflow_reason: str | None = None
    chunk_budget_exhausted = False

    def flush() -> None:
        nonlocal current, chunk_budget_exhausted, overflow_reason
        if not current:
            return
        if len(chunks) >= work_budget.max_chunks:
            overflow_reason = overflow_reason or "provider-call-budget"
            overflow.extend(current)
            current = ()
            chunk_budget_exhausted = True
            return
        chunks.append(
            _chunk_from_records(len(chunks) + 1, current, changed_paths=changed_paths)
        )
        current = ()

    for record in records:
        if chunk_budget_exhausted:
            overflow.append(record)
            continue
        pieces: tuple[DiffFileRecord, ...]
        if _fits((), record, limits=limits):
            pieces = (record,)
        elif record.hunks and not record.binary:
            pieces = tuple(_hunk_record(record, hunk) for hunk in record.hunks)
            if any(not _fits((), piece, limits=limits) for piece in pieces):
                overflow.append(record)
                continue
        else:
            overflow.append(record)
            continue
        for piece in pieces:
            if current and not _fits(current, piece, limits=limits):
                flush()
            if len(chunks) >= work_budget.max_chunks:
                overflow_reason = "provider-call-budget"
                overflow.append(piece)
                chunk_budget_exhausted = True
                break
            current = current + (piece,)
    flush()
    return tuple(chunks), tuple(overflow), overflow_reason


def _coverage_for(
    analysis: DiffAnalysis,
    *,
    file_outcomes: dict[str, tuple[str, str | None]],
    hunk_outcomes: dict[int, tuple[str, str | None]],
    limits: ReviewLimits,
) -> CoverageManifest:
    files = tuple(
        FileCoverage(path=path, outcome=outcome, reason=reason)
        for path, (outcome, reason) in sorted(file_outcomes.items())
    )
    enumerated = set(analysis.changed_paths)
    hunks: list[HunkCoverage] = []
    for hunk in analysis.hunk_records:
        path = None
        for candidate in (hunk.new_path, hunk.old_path):
            if candidate is not None and candidate in enumerated:
                path = candidate
                break
        if path is None:
            continue
        if hunk.index in hunk_outcomes:
            outcome, reason = hunk_outcomes[hunk.index]
        else:
            file_outcome = file_outcomes.get(path)
            if file_outcome is not None and file_outcome[0] != "reviewed":
                outcome, reason = file_outcome
            else:
                outcome, reason = ("unsupported", "incomplete-enumeration")
        hunks.append(
            HunkCoverage(index=hunk.index, path=path, outcome=outcome, reason=reason)
        )
    complete = analysis.enumeration_complete and enumerated == set(file_outcomes)
    if not complete:
        # Incomplete enumeration can never be reported as fully reviewed.
        pass
    return CoverageManifest(
        files=files,
        hunks=tuple(hunks),
        enumeration_complete=complete,
        enumerated_paths=analysis.changed_paths,
        limits=limits,
    )


def _reconcile_file_outcomes_from_hunks(
    analysis: DiffAnalysis,
    *,
    file_outcomes: dict[str, tuple[str, str | None]],
    hunk_outcomes: dict[int, tuple[str, str | None]],
) -> None:
    """Align file outcomes with mixed per-hunk coverage within one path."""

    for path in analysis.changed_paths:
        hunk_indexes = [
            hunk.index
            for hunk in analysis.hunk_records
            if (hunk.new_path or hunk.old_path) == path
        ]
        if not hunk_indexes:
            continue
        states = [
            hunk_outcomes.get(index, ("unsupported", "incomplete-enumeration"))[0]
            for index in hunk_indexes
        ]
        if any(state == "reviewed" for state in states) and any(
            state != "reviewed" for state in states
        ):
            reason = file_outcomes.get(path, (None, None))[1]
            file_outcomes[path] = ("partially-reviewed", reason)
        elif states and all(state == "reviewed" for state in states):
            file_outcomes[path] = ("reviewed", None)


def plan_change(
    diff: str,
    *,
    limits: ReviewLimits = DEFAULT_REVIEW_LIMITS,
    work_budget: TotalWorkBudget = DEFAULT_TOTAL_WORK_BUDGET,
    orchestrate: bool = False,
) -> LargeChangePlan:
    """Build a coverage plan, optionally partitioning into bounded chunks."""

    if not isinstance(work_budget, TotalWorkBudget):
        raise ReviewInputError("work budget must be a TotalWorkBudget value")
    if not isinstance(orchestrate, bool):
        raise ReviewInputError("orchestrate must be a boolean")
    if orchestrate:
        analysis = analyze_diff(
            diff,
            limits=limits,
            allow_incomplete=True,
            max_bytes=work_budget.max_total_diff_bytes,
            max_lines=work_budget.max_total_diff_lines,
            max_files=work_budget.max_total_files,
            max_hunks=work_budget.max_total_hunks,
        )
    else:
        analysis = analyze_diff(diff, limits=limits)

    file_outcomes: dict[str, tuple[str, str | None]] = {}
    hunk_outcomes: dict[int, tuple[str, str | None]] = {}
    reviewable: list[DiffFileRecord] = []
    records = analysis.file_records
    if not records and analysis.changed_paths:
        raise ReviewInputError("diff file records are incomplete")
    records = records or ()

    for record in records:
        outcome, reason = _classify_file(record, analysis=analysis, limits=limits)
        for path in record.coverage_paths or (
            (record.canonical_path,) if record.canonical_path else ()
        ):
            file_outcomes[path] = (
                (outcome, reason)
                if outcome != "reviewable"
                else (
                    "reviewed",
                    None,
                )
            )
        if outcome != "reviewable":
            for hunk in record.hunks:
                hunk_outcomes[hunk.index] = (outcome, reason)
            continue
        reviewable.append(record)

    chunks: tuple[ReviewChunk, ...] = ()
    if orchestrate:
        packed, overflow, overflow_reason = _pack_records(
            tuple(reviewable),
            limits=limits,
            work_budget=work_budget,
            changed_paths=analysis.changed_paths,
        )
        chunks = packed
        packed_paths = {path for chunk in packed for path in chunk.paths}
        packed_hunks = {index for chunk in packed for index in chunk.hunk_indexes}
        overflow_paths = {path for record in overflow for path in record.coverage_paths}
        for record in overflow:
            reason = overflow_reason or (
                "too-large-hunk" if record.hunks else "too-large-file"
            )
            outcome = (
                "budget-exhausted"
                if overflow_reason == "provider-call-budget"
                else "unsupported"
            )
            for path in record.coverage_paths:
                if path in packed_paths:
                    file_outcomes[path] = ("partially-reviewed", reason)
                else:
                    file_outcomes[path] = (outcome, reason)
            for hunk in record.hunks:
                if hunk.index not in packed_hunks:
                    hunk_outcomes[hunk.index] = (outcome, reason)
        for record in reviewable:
            for path in record.coverage_paths:
                # A path can appear in both a packed chunk and an overflow
                # record when only some of its hunks fit the per-request budget.
                # Treat that as partial coverage rather than fully reviewed.
                if path in packed_paths and path not in overflow_paths:
                    file_outcomes[path] = ("reviewed", None)
            for hunk in record.hunks:
                if hunk.index in packed_hunks:
                    hunk_outcomes[hunk.index] = ("reviewed", None)
        _reconcile_file_outcomes_from_hunks(
            analysis,
            file_outcomes=file_outcomes,
            hunk_outcomes=hunk_outcomes,
        )
    else:
        if not analysis.enumeration_complete:
            if analysis.diff_bytes > limits.max_diff_bytes:
                raise ReviewInputError("diff exceeds the configured byte limit")
            if analysis.diff_lines > limits.max_diff_lines:
                raise ReviewInputError("diff exceeds the configured line limit")
            if analysis.diff_files > limits.max_diff_files:
                raise ReviewInputError("diff contains too many files")
            if analysis.diff_hunks > limits.max_diff_hunks:
                raise ReviewInputError("diff contains too many hunks")
            raise ReviewInputError("diff exceeds the configured byte limit")
        # Single-request reviews still emit coverage.  Files that fit the
        # per-request inventory are reviewed; the parser has already failed
        # closed when they would not fit.
        chunks = (
            ReviewChunk(
                index=1,
                diff=diff if diff.endswith("\n") else f"{diff}\n",
                paths=analysis.changed_paths,
                related_paths=(),
                hunk_indexes=tuple(hunk.index for hunk in analysis.hunk_records),
            ),
        )
        for hunk in analysis.hunk_records:
            hunk_path = hunk.new_path or hunk.old_path
            if hunk_path is None:
                continue
            if hunk_path not in file_outcomes:
                continue
            if file_outcomes[hunk_path][0] == "reviewed":
                hunk_outcomes[hunk.index] = ("reviewed", None)

    for path in analysis.changed_paths:
        file_outcomes.setdefault(
            path,
            (
                "budget-exhausted"
                if not analysis.enumeration_complete
                else "unsupported",
                "incomplete-enumeration",
            ),
        )

    coverage = _coverage_for(
        analysis,
        file_outcomes=file_outcomes,
        hunk_outcomes=hunk_outcomes,
        limits=limits,
    )
    return LargeChangePlan(
        analysis=analysis,
        coverage=coverage,
        chunks=chunks,
        work_budget=work_budget,
        orchestrated=orchestrate,
    )


_COVERAGE_OUTCOME_RANK = {
    "reviewed": 0,
    "excluded-by-policy": 1,
    "partially-reviewed": 2,
    "unsupported": 3,
    "budget-exhausted": 4,
}


def _worse_coverage_outcome(
    left: tuple[str, str | None],
    right: tuple[str, str | None],
) -> tuple[str, str | None]:
    left_rank = _COVERAGE_OUTCOME_RANK.get(left[0], len(_COVERAGE_OUTCOME_RANK))
    right_rank = _COVERAGE_OUTCOME_RANK.get(right[0], len(_COVERAGE_OUTCOME_RANK))
    if left_rank >= right_rank:
        return left
    return right


def merge_chunk_coverage(
    aggregate: CoverageManifest,
    chunk: CoverageManifest,
    *,
    paths: tuple[str, ...],
    hunk_indexes: tuple[int, ...],
    limits: ReviewLimits = DEFAULT_REVIEW_LIMITS,
) -> CoverageManifest:
    """Merge chunk-scoped coverage into an aggregate manifest conservatively."""

    path_set = set(paths)
    hunk_set = set(hunk_indexes)
    file_map = {entry.path: entry for entry in aggregate.files}
    for entry in chunk.files:
        if entry.path not in path_set:
            continue
        if entry.path in file_map:
            outcome, reason = _worse_coverage_outcome(
                (file_map[entry.path].outcome, file_map[entry.path].reason),
                (entry.outcome, entry.reason),
            )
            file_map[entry.path] = replace(
                file_map[entry.path], outcome=outcome, reason=reason
            )
        else:
            file_map[entry.path] = entry
    for path in aggregate.enumerated_paths:
        if path not in file_map:
            file_map[path] = FileCoverage(
                path=path,
                outcome="unsupported",
                reason="incomplete-enumeration",
            )
    files = tuple(sorted(file_map.values(), key=lambda entry: entry.path))
    hunk_map = {(entry.index, entry.path): entry for entry in aggregate.hunks}
    for chunk_hunk in chunk.hunks:
        if chunk_hunk.index not in hunk_set:
            continue
        key = (chunk_hunk.index, chunk_hunk.path)
        if key in hunk_map:
            outcome, reason = _worse_coverage_outcome(
                (hunk_map[key].outcome, hunk_map[key].reason),
                (chunk_hunk.outcome, chunk_hunk.reason),
            )
            hunk_map[key] = replace(hunk_map[key], outcome=outcome, reason=reason)
        else:
            hunk_map[key] = chunk_hunk
    hunks = tuple(sorted(hunk_map.values(), key=lambda hunk: (hunk.index, hunk.path)))
    return CoverageManifest(
        files=files,
        hunks=hunks,
        enumeration_complete=aggregate.enumeration_complete,
        enumerated_paths=aggregate.enumerated_paths,
        limits=limits,
    )


def apply_chunk_outcomes(
    coverage: CoverageManifest,
    *,
    paths: tuple[str, ...],
    hunk_indexes: tuple[int, ...],
    outcome: str,
    reason: str | None = None,
    limits: ReviewLimits = DEFAULT_REVIEW_LIMITS,
) -> CoverageManifest:
    """Return a copy of ``coverage`` with the selected files/hunks updated."""

    if outcome not in COVERAGE_OUTCOMES:
        raise ReviewInputError("coverage outcome is invalid")
    _validate_reason(reason)
    enumerated = set(coverage.enumerated_paths)
    unknown_paths = set(paths) - enumerated
    if unknown_paths:
        raise ReviewInputError("coverage paths are outside the enumerated set")
    known_hunk_indexes = {entry.index for entry in coverage.hunks}
    unknown_hunks = set(hunk_indexes) - known_hunk_indexes
    if unknown_hunks:
        raise ReviewInputError("coverage hunk indexes are outside the manifest")
    updated_paths = set(paths)
    files: list[FileCoverage] = []
    for file_entry in coverage.files:
        if file_entry.path in updated_paths:
            files.append(replace(file_entry, outcome=outcome, reason=reason))
            updated_paths.discard(file_entry.path)
        else:
            files.append(file_entry)
    for path in sorted(updated_paths):
        files.append(FileCoverage(path=path, outcome=outcome, reason=reason))
    hunks: list[HunkCoverage] = []
    for hunk_entry in coverage.hunks:
        if hunk_entry.index in hunk_indexes:
            hunks.append(replace(hunk_entry, outcome=outcome, reason=reason))
        else:
            hunks.append(hunk_entry)
    return CoverageManifest(
        files=tuple(files),
        hunks=tuple(hunks),
        enumeration_complete=coverage.enumeration_complete,
        enumerated_paths=coverage.enumerated_paths,
        limits=limits,
    )


__all__ = [
    "DEFAULT_TOTAL_WORK_BUDGET",
    "LargeChangePlan",
    "ReviewWorkPlan",
    "WorkBatch",
    "WorkRequirement",
    "plan_work",
    "ReviewChunk",
    "TotalWorkBudget",
    "apply_chunk_outcomes",
    "is_generated_path",
    "merge_chunk_coverage",
    "plan_change",
    "related_paths_for_change",
]
