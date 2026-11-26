"""Lossless canonical hunk subdivision; fragments never authorize resolution."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass

from .diff import DiffFileRecord, DiffHunk
from .errors import ReviewInputError
from .evidence import EvidenceRecord, EvidenceSnapshot, HunkFragment
from .validation import DEFAULT_TOTAL_WORK_BUDGET

HUNK_SUBDIVISION_POLICY = "canonical-line-segments-v2"
MAX_HUNK_SEGMENTS = 128
SUBDIVISION_STOP_REASONS = (
    "single-line-oversized",
    "per-hunk-segment-limit",
    "run-subdivision-allowance-exhausted",
    "planning-deadline-exhausted",
)

_TOP_LEVEL_DECL_PREFIXES = (
    "def ",
    "async def ",
    "class ",
    "@",
    "function ",
    "async function ",
    "export ",
    "func ",
    "fn ",
    "pub ",
    "struct ",
    "impl ",
    "interface ",
    "enum ",
)
_BLOCK_DECL_PREFIXES = ("def ", "async def ", "class ", "@")
_CONTINUATION_PREFIXES = (
    ")",
    "]",
    "}",
    ",",
    ".",
    ":",
    "elif ",
    "elif:",
    "else:",
    "else ",
    "except",
    "finally:",
    "catch",
    "finally",
    "and ",
    "or ",
    '"""',
    "'''",
)


def _source_line(unit: str) -> str | None:
    first = unit.split("\n", 1)[0].rstrip("\r") if unit else ""
    if not first or first[0] not in "+- ":
        return None
    return first[1:]


def _boundary_tier(units: list[str], idx: int) -> int | None:
    """Rank structural boundary quality before ``units[idx]`` (lower is stronger)."""
    curr = _source_line(units[idx])
    prev = _source_line(units[idx - 1])
    if curr is None or prev is None:
        return None
    curr_stripped = curr.lstrip(" \t")
    if not curr_stripped:
        return None
    prev_stripped = prev.lstrip(" \t")
    prev_is_blank = not prev_stripped
    if prev_stripped.startswith("@") or prev.rstrip().endswith("\\"):
        return None
    if curr_stripped.startswith(_CONTINUATION_PREFIXES):
        return None
    curr_indent = len(curr) - len(curr_stripped)
    if curr_indent == 0:
        if curr_stripped.startswith(_TOP_LEVEL_DECL_PREFIXES):
            return 0
        if prev_is_blank:
            return 1
        return 3
    if curr_stripped.startswith(_BLOCK_DECL_PREFIXES):
        return 2
    if prev_is_blank:
        return 4
    return None


def _choose_split_index(
    units: list[str],
    offsets: list[int],
    begin: int,
    end: int,
) -> int:
    """Pick the closest balanced structural boundary within ±25% of midpoint."""
    span = end - begin
    middle = (begin + end) // 2
    if span < 4:
        return middle
    radius = span // 4
    low = max(begin + 1, middle - radius)
    high = min(end - 1, middle + radius)
    total_bytes = offsets[end] - offsets[begin]
    mid_left = offsets[middle] - offsets[begin]
    mid_right = offsets[end] - offsets[middle]
    mid_balanced = total_bytes == 0 or min(mid_left, mid_right) * 4 >= total_bytes

    best_key: tuple[int, int, int, int] | None = None
    best_idx = middle
    for idx in range(low, high + 1):
        tier = _boundary_tier(units, idx)
        if tier is None:
            continue
        left_bytes = offsets[idx] - offsets[begin]
        right_bytes = offsets[end] - offsets[idx]
        if (
            mid_balanced
            and total_bytes > 0
            and min(left_bytes, right_bytes) * 4 < total_bytes
        ):
            continue
        key = (tier, abs(idx - middle), 0 if idx >= middle else 1, idx)
        if best_key is None or key < best_key:
            best_key = key
            best_idx = idx
    return best_idx


@dataclass
class SubdivisionAllowance:
    """One bounded subdivision liability shared across hunks in a stage."""

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
    unadmitted_reasons: dict[str, str] | None = None,
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
    # Split at the same physical LF boundaries as the diff parser, retaining
    # terminators and source separators for exact fragment bytes and offsets.
    lines = body.split("\n")
    for index, line in enumerate(lines):
        if index < len(lines) - 1:
            line += "\n"
        elif not line:
            continue
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
        if expired:
            if unadmitted_reasons is not None:
                unadmitted_reasons[part.evidence_id] = "planning-deadline-exhausted"
            result.append(part)
        elif admit(part):
            result.append(part)
        elif end - begin == 1:
            if unadmitted_reasons is not None:
                unadmitted_reasons[part.evidence_id] = "single-line-oversized"
            result.append(part)
        elif remaining == 0:
            if unadmitted_reasons is not None:
                unadmitted_reasons[part.evidence_id] = "per-hunk-segment-limit"
            result.append(part)
        elif not allowance.take():
            if unadmitted_reasons is not None:
                unadmitted_reasons[part.evidence_id] = (
                    "run-subdivision-allowance-exhausted"
                )
            result.append(part)
        else:
            remaining -= 1
            split = _choose_split_index(units, offsets, begin, end)
            stack.extend(((split, end), (begin, split)))
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
