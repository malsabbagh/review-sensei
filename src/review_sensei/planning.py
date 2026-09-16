"""Deterministic large-change planning with separate total-work budgets.

Per-request ``ReviewLimits`` stay fail-closed.  This module adds aggregate
ceilings so a change can be partitioned into bounded chunks without raising
those per-request limits or silently truncating work.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .coverage import CoverageManifest, FileCoverage, HunkCoverage
from .diff import DiffAnalysis, DiffFileRecord, DiffHunk, analyze_diff
from .errors import ReviewInputError
from .validation import (
    DEFAULT_REVIEW_LIMITS,
    DEFAULT_TOTAL_WORK_BUDGET,
    ReviewLimits,
    TotalWorkBudget,
    utf8_size,
)

MAX_RELATED_PATHS = 32

GENERATED_FILE_NAMES = frozenset(
    {
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "cargo.lock",
        "poetry.lock",
        "composer.lock",
        "go.sum",
        "gemfile.lock",
    }
)
GENERATED_SUFFIXES = (".min.js", ".min.css", ".min.map", ".map")
GENERATED_PATH_PREFIXES = ("dist/", "vendor/", "node_modules/", "generated/")


def is_generated_path(path: str) -> bool:
    """Return whether ``path`` matches the explicit generated-file policy."""

    lowered = path.lower()
    name = lowered.rsplit("/", 1)[-1]
    if name in GENERATED_FILE_NAMES:
        return True
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
) -> tuple[str, str | None]:
    paths = record.coverage_paths
    if record.binary or any(path in analysis.binary_paths for path in paths):
        return "unsupported", "binary"
    if any(is_generated_path(path) for path in paths):
        return "excluded-by-policy", "generated"
    if _file_bytes(record) > DEFAULT_REVIEW_LIMITS.max_diff_bytes and not record.hunks:
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

    def flush() -> None:
        nonlocal current
        if not current:
            return
        chunks.append(
            _chunk_from_records(len(chunks) + 1, current, changed_paths=changed_paths)
        )
        current = ()

    for record in records:
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
            if not current and len(chunks) >= work_budget.max_chunks:
                overflow_reason = "provider-call-budget"
                overflow.append(piece)
                continue
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
    hunks: list[HunkCoverage] = []
    for hunk in analysis.hunk_records:
        path = hunk.new_path or hunk.old_path
        if path is None:
            continue
        outcome, reason = hunk_outcomes.get(
            hunk.index,
            file_outcomes.get(path, ("unsupported", "incomplete-enumeration")),
        )
        hunks.append(
            HunkCoverage(index=hunk.index, path=path, outcome=outcome, reason=reason)
        )
    enumerated = set(analysis.changed_paths)
    complete = analysis.enumeration_complete and enumerated == set(file_outcomes)
    if not complete:
        # Incomplete enumeration can never be reported as fully reviewed.
        pass
    return CoverageManifest(
        files=files,
        hunks=tuple(hunks),
        enumeration_complete=complete,
        limits=limits,
    )


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
    records = analysis.file_records or (
        DiffFileRecord(
            old_path=None,
            new_path=analysis.changed_paths[0] if analysis.changed_paths else None,
            text=diff if diff.endswith("\n") else f"{diff}\n",
            header="",
            added_lines=frozenset(),
            deleted_lines=frozenset(),
            hunks=analysis.hunk_records,
            binary=bool(analysis.binary_paths),
        ),
    )

    for record in records:
        outcome, reason = _classify_file(record, analysis=analysis)
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
    else:
        if not analysis.enumeration_complete:
            raise ReviewInputError(
                "non-orchestrated reviews require complete diff enumeration"
            )
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

    files: list[FileCoverage] = []
    for file_entry in coverage.files:
        if file_entry.path in paths:
            files.append(replace(file_entry, outcome=outcome, reason=reason))
        else:
            files.append(file_entry)
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
        limits=limits,
    )


__all__ = [
    "DEFAULT_TOTAL_WORK_BUDGET",
    "LargeChangePlan",
    "ReviewChunk",
    "TotalWorkBudget",
    "apply_chunk_outcomes",
    "is_generated_path",
    "plan_change",
]
