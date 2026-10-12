"""Lossless canonical hunk subdivision; fragments never authorize resolution."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass

from .diff import DiffFileRecord, DiffHunk
from .errors import ReviewInputError
from .evidence import EvidenceRecord, EvidenceSnapshot, HunkFragment
from .validation import DEFAULT_TOTAL_WORK_BUDGET

HUNK_SUBDIVISION_POLICY = "canonical-line-segments-v1"
MAX_HUNK_SEGMENTS = 128


@dataclass
class SubdivisionAllowance:
    """One bounded subdivision liability shared by every hunk and stage."""

    remaining: int = MAX_HUNK_SEGMENTS - 1

    def __post_init__(self) -> None:
        if (
            isinstance(self.remaining, bool)
            or not isinstance(self.remaining, int)
            or not 0 <= self.remaining < MAX_HUNK_SEGMENTS
        ):
            raise ReviewInputError("hunk subdivision allowance is invalid")

    def take(self) -> bool:
        if self.remaining == 0:
            return False
        self.remaining -= 1
        return True


def subdivide_hunk(
    record: DiffFileRecord,
    hunk: DiffHunk,
    snapshot: EvidenceSnapshot,
    *,
    admit: Callable[[EvidenceRecord], bool],
    max_parts: int = MAX_HUNK_SEGMENTS,
    before_probe: Callable[[], None] | None = None,
    allowance: SubdivisionAllowance | None = None,
) -> tuple[EvidenceRecord, ...]:
    """Probe a balanced partition, retaining every unadmitted terminal range.

    Split only between whole diff lines, keeping no-newline markers attached to
    the preceding line. Each derived header uses exact original old/new cursors.
    A giant line, expired admission deadline, or exhausted partition allowance
    remains one unadmitted obligation. No prefix, suffix or summary replaces it.
    At most ``2 * max_parts - 1`` admission probes are made. Callers still use
    the ordinary planner/executor and their original cumulative resource budget.
    """
    if (
        record.binary
        or hunk not in record.hunks
        or isinstance(max_parts, bool)
        or not isinstance(max_parts, int)
        or not 1 <= max_parts <= DEFAULT_TOTAL_WORK_BUDGET.max_total_hunks
    ):
        raise ReviewInputError("hunk subdivision scope or allowance is invalid")
    source = EvidenceRecord.from_diff_record(record, snapshot)
    original_header, _, body = hunk.text.partition("\n")
    units: list[str] = []
    for line in body.splitlines(keepends=True):
        if line.startswith("\\"):
            if not units:
                raise ReviewInputError("hunk subdivision marker has no source line")
            units[-1] += line
        else:
            units.append(line)
    if not units:
        raise ReviewInputError("hunk subdivision source body is missing")
    offsets, old, new = [0], [0], [0]
    for unit in units:
        offsets.append(offsets[-1] + len(unit.encode("utf-8")))
        old.append(old[-1] + int(unit[0] in " -"))
        new.append(new[-1] + int(unit[0] in " +"))
    if old[-1] != hunk.old_count or new[-1] != hunk.new_count:
        raise ReviewInputError("hunk subdivision source counts conflict")
    source_hash = hashlib.sha256(hunk.text.encode("utf-8")).hexdigest()
    # The function/section suffix is exact source evidence too.
    suffix = original_header.split("@@", 2)[2]

    def fragment(begin: int, end: int) -> EvidenceRecord:
        old_count, new_count = old[end] - old[begin], new[end] - new[begin]
        old_start = hunk.old_start + old[begin]
        new_start = hunk.new_start + new[begin]
        # Unified zero-length ranges point immediately before the next line.
        if old_count == 0 and hunk.old_count:
            old_start -= 1
        if new_count == 0 and hunk.new_count:
            new_start -= 1
        piece_body = "".join(units[begin:end])
        proof = HunkFragment(
            source.evidence_id,
            source_hash,
            hunk.index,
            offsets[begin],
            offsets[end],
            offsets[-1],
            hashlib.sha256(piece_body.encode("utf-8")).hexdigest(),
        )
        note = (
            "reviewsensei canonical-hunk-segment "
            f"source={source_hash} body-bytes={proof.body_start}:{proof.body_end}/{proof.body_bytes}\n"
        )
        text = (
            record.header
            + note
            + f"@@ -{old_start},{old_count} +{new_start},{new_count} @@{suffix}\n"
            + piece_body
        )
        return EvidenceRecord(
            source.path,
            text,
            snapshot,
            supplied_complete=False,
            provenance="discovery-hunk",
            old_path=record.old_path,
            original_diff=text,
            fragment=proof,
        )

    remaining = max_parts - 1
    allowance = allowance if allowance is not None else SubdivisionAllowance()
    result: list[EvidenceRecord] = []
    stack = [(0, len(units))]
    expired = False
    while stack:
        begin, end = stack.pop()
        part = fragment(begin, end)
        if before_probe is not None and not expired:
            try:
                before_probe()
            except ReviewInputError as exc:
                if exc.error_category != "planning_deadline_exhausted":
                    raise
                expired = True
        if (
            expired
            or admit(part)
            or end - begin == 1
            or remaining == 0
            or not allowance.take()
        ):
            result.append(part)
        else:
            remaining -= 1
            middle = (begin + end) // 2
            stack.extend(((middle, end), (begin, middle)))
    # Conservation is checked before a fragment bundle or provider call exists.
    cursor = 0
    source_body = body.encode("utf-8")
    for part in result:
        proof = part.fragment
        assert proof is not None
        if proof.body_start != cursor:
            raise ReviewInputError("hunk subdivision body inventory is incomplete")
        parsed = part.analysis
        if parsed is None or len(parsed.hunk_records) != 1:
            raise ReviewInputError("hunk subdivision derived range is invalid")
        piece_body = parsed.hunk_records[0].text.partition("\n")[2].encode("utf-8")
        if (
            piece_body != source_body[proof.body_start : proof.body_end]
            or hashlib.sha256(piece_body).hexdigest() != proof.body_sha256
        ):
            raise ReviewInputError("hunk subdivision derived body differs from source")
        cursor = proof.body_end
    if cursor != offsets[-1]:
        raise ReviewInputError("hunk subdivision body inventory is incomplete")
    return tuple(result)
