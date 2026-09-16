from __future__ import annotations

import re
from dataclasses import dataclass, field

from .errors import ReviewInputError
from .validation import (
    DEFAULT_REVIEW_LIMITS,
    MAX_REPOSITORY_PATH_BYTES,
    ReviewLimits,
    decode_git_c_quoted_path,
    utf8_size,
    validate_repository_path,
)

_HUNK_HEADER = re.compile(
    r"^@@ -([0-9]+)(?:,([0-9]+))? \+([0-9]+)(?:,([0-9]+))? @@(?:.*)$"
)
_MAX_GIT_HEADER_BYTES = (MAX_REPOSITORY_PATH_BYTES * 4 * 2) + 64


@dataclass(frozen=True)
class _GitHeader:
    fields: tuple[str, str] | None = None
    unresolved_unquoted: str | None = None


@dataclass(frozen=True)
class DiffHunk:
    """One validated unified-diff hunk, including the exact reconstructed text."""

    index: int
    old_path: str | None
    new_path: str | None
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    text: str
    added_lines: frozenset[int]
    deleted_lines: frozenset[int]


@dataclass(frozen=True)
class DiffFileRecord:
    """One file-level diff record used for deterministic chunk packing."""

    old_path: str | None
    new_path: str | None
    text: str
    header: str
    added_lines: frozenset[int]
    deleted_lines: frozenset[int]
    hunks: tuple[DiffHunk, ...]
    binary: bool

    @property
    def coverage_paths(self) -> tuple[str, ...]:
        paths: list[str] = []
        if self.old_path:
            paths.append(self.old_path)
        if self.new_path and self.new_path != self.old_path:
            paths.append(self.new_path)
        return tuple(paths)

    @property
    def canonical_path(self) -> str | None:
        return self.new_path if self.new_path is not None else self.old_path


@dataclass(frozen=True)
class DiffAnalysis:
    """The bounded, single-pass facts extracted from a unified/Git diff."""

    changed_lines: dict[str, frozenset[int]]
    changed_paths: tuple[str, ...]
    diff_bytes: int
    diff_files: int
    diff_hunks: int
    diff_lines: int
    deleted_lines: dict[str, frozenset[int]] = field(default_factory=dict)
    binary_paths: tuple[str, ...] = ()
    renamed_paths: tuple[tuple[str, str], ...] = ()
    hunk_records: tuple[DiffHunk, ...] = ()
    file_records: tuple[DiffFileRecord, ...] = ()
    enumeration_complete: bool = True

    @property
    def lines(self) -> dict[str, frozenset[int]]:
        return self.changed_lines

    @property
    def paths(self) -> tuple[str, ...]:
        return self.changed_paths

    @property
    def files(self) -> int:
        return self.diff_files

    @property
    def hunks(self) -> int:
        return self.diff_hunks

    @property
    def byte_count(self) -> int:
        return self.diff_bytes

    @property
    def line_count(self) -> int:
        return self.diff_lines

    @property
    def file_count(self) -> int:
        return self.diff_files

    @property
    def hunk_count(self) -> int:
        return self.diff_hunks

    @property
    def added_lines(self) -> dict[str, frozenset[int]]:
        return self.changed_lines


def _invalid(message: str) -> ReviewInputError:
    # All messages are static and intentionally omit diff content and paths.
    return ReviewInputError(message)


def _scan_quoted_token(text: str, start: int) -> tuple[str, int]:
    """Return one quoted token (including quotes) and the next index."""

    if start >= len(text) or text[start] != '"':
        raise _invalid("diff contains an invalid quoted path")
    index = start + 1
    escaped = False
    while index < len(text):
        character = text[index]
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == '"':
            return text[start : index + 1], index + 1
        index += 1
    raise _invalid("diff contains an unterminated quoted path")


def _split_git_header(line: str) -> _GitHeader:
    rest = line[len("diff --git ") :]
    if '"' not in rest:
        # A unique boundary is directly resolvable.  For repeated ``b/``
        # segments, first test the only possible equal-path midpoint.  Any
        # remaining distinct-path form stays as one bounded raw string and is
        # resolved later against canonical markers/rename metadata.
        first_boundary = rest.find(" b/")
        if first_boundary >= 0:
            last_boundary = rest.rfind(" b/")
            if first_boundary == last_boundary:
                old_field = rest[:first_boundary]
                new_field = rest[first_boundary + 1 :]
                if old_field.startswith("a/") and new_field.startswith("b/"):
                    return _GitHeader(fields=(old_field, new_field))
            else:
                midpoint = (len(rest) + 1) // 2
                if (
                    rest.startswith("a/")
                    and midpoint > 2
                    and midpoint + 2 <= len(rest)
                    and rest[midpoint - 1] == " "
                    and rest[midpoint : midpoint + 2] == "b/"
                    and rest[2 : midpoint - 1] == rest[midpoint + 2 :]
                ):
                    return _GitHeader(fields=(rest[: midpoint - 1], rest[midpoint:]))
                return _GitHeader(unresolved_unquoted=rest)
    tokens: list[str] = []
    index = 0
    while index < len(rest):
        while index < len(rest) and rest[index].isspace():
            index += 1
        if index == len(rest):
            break
        if rest[index] == '"':
            token, index = _scan_quoted_token(rest, index)
        else:
            begin = index
            while index < len(rest) and not rest[index].isspace():
                index += 1
            token = rest[begin:index]
        tokens.append(token)
    if len(tokens) != 2:
        raise _invalid("diff contains an invalid Git header")
    return _GitHeader(fields=(tokens[0], tokens[1]))


def _decode_path_field(field: str, *, label: str, side: bool = False) -> str | None:
    if field == "/dev/null":
        return None
    decoded = decode_git_c_quoted_path(
        field,
        label=label,
        allow_glob_chars=True,
    )
    if side and (decoded.startswith("a/") or decoded.startswith("b/")):
        decoded = decoded[2:]
    if decoded == "/dev/null":
        return None
    validate_repository_path(
        decoded,
        label=label,
        allow_glob_chars=True,
    )
    return decoded


def _decode_git_header_fields(
    fields: tuple[str, str],
) -> tuple[str | None, str | None]:
    old_field, new_field = fields
    return (
        _decode_path_field(old_field, label="diff Git path", side=True),
        _decode_path_field(new_field, label="diff Git path", side=True),
    )


def _decode_marker(line: str, *, prefix: str, side: bool) -> str | None:
    if not line.startswith(prefix):
        raise _invalid("diff contains an invalid file marker")
    field = line[len(prefix) :].rstrip("\r")
    # Unified markers may carry a tab-separated timestamp.  Git C-quoted paths
    # are decoded before the marker's side prefix is removed.
    field = field.split("\t", 1)[0]
    if field.startswith('"'):
        token, end = _scan_quoted_token(field, 0)
        if field[end:].strip():
            raise _invalid("diff contains an invalid quoted file marker")
        field = token
    return _decode_path_field(field, label="diff file marker", side=side)


def _decode_rename_path(line: str, prefix: str) -> str | None:
    field = line[len(prefix) :].rstrip("\r")
    if field.startswith('"'):
        token, end = _scan_quoted_token(field, 0)
        if field[end:].strip():
            raise _invalid("diff contains an invalid rename path")
        field = token
    return _decode_path_field(field, label="diff rename path", side=False)


def _add_path(paths: dict[str, None], path: str | None, limits: ReviewLimits) -> None:
    if path is None:
        return
    paths.setdefault(path, None)
    if len(paths) > limits.max_diff_files:
        raise _invalid("diff contains too many files")


def analyze_diff(
    diff: str,
    *,
    limits: ReviewLimits = DEFAULT_REVIEW_LIMITS,
    allow_incomplete: bool = False,
    max_bytes: int | None = None,
    max_lines: int | None = None,
    max_files: int | None = None,
    max_hunks: int | None = None,
) -> DiffAnalysis:
    """Parse and validate a diff in one bounded pass.

    Both Git extended diffs and plain unified diffs are supported.  All path
    decoding and hunk validation occurs before a caller may construct a
    provider request.  Per-request ``limits`` remain fail-closed.  Optional
    inventory ceilings are used only when ``allow_incomplete`` is true, and
    overflow marks ``enumeration_complete`` false instead of silently
    truncating a review.
    """

    if not isinstance(limits, ReviewLimits):
        raise _invalid("diff limits must be a ReviewLimits value")
    if not isinstance(diff, str) or not diff.strip():
        raise _invalid("diff must be a non-empty string")
    if not isinstance(allow_incomplete, bool):
        raise _invalid("diff allow_incomplete must be a boolean")
    byte_count = utf8_size(diff, label="diff")
    byte_limit = (
        max_bytes
        if allow_incomplete and max_bytes is not None
        else limits.max_diff_bytes
    )
    line_limit = (
        max_lines
        if allow_incomplete and max_lines is not None
        else limits.max_diff_lines
    )
    file_limit = (
        max_files
        if allow_incomplete and max_files is not None
        else limits.max_diff_files
    )
    hunk_limit = (
        max_hunks
        if allow_incomplete and max_hunks is not None
        else limits.max_diff_hunks
    )
    enumeration_complete = True
    if byte_count > byte_limit:
        if not allow_incomplete:
            raise _invalid("diff exceeds the configured byte limit")
        enumeration_complete = False
    lines = diff.splitlines()
    if len(lines) > line_limit:
        if not allow_incomplete:
            raise _invalid("diff exceeds the configured line limit")
        enumeration_complete = False
        lines = lines[:line_limit]

    changed: dict[str, set[int]] = {}
    deleted: dict[str, set[int]] = {}
    paths: dict[str, None] = {}
    binary_paths: dict[str, None] = {}
    renamed_pairs: list[tuple[str, str]] = []
    hunk_records: list[DiffHunk] = []
    file_records: list[DiffFileRecord] = []
    current_path: str | None = None
    current_old_path: str | None = None
    new_line: int | None = None
    old_line: int | None = None
    pending_old_marker = False
    matched_file_marker = False
    hunk_count = 0
    saw_structure = False
    stopped_for_budget = False
    hunk_state: tuple[int, int, int, int, int, dict[str, set[int]]] | None = None
    pending_git_header: str | None = None
    pending_rename_from: str | None = None
    selected_git_header: tuple[str | None, str | None] | None = None
    rename_from_for_header: str | None = None
    in_file = False
    current_binary = False
    current_file_lines: list[str] = []
    current_header_lines: list[str] = []
    current_hunk_lines: list[str] = []
    current_hunk_added: set[int] = set()
    current_hunk_deleted: set[int] = set()
    current_file_hunks: list[DiffHunk] = []
    current_file_added: set[int] = set()
    current_file_deleted: set[int] = set()
    seen_hunk_in_file = False

    def record_path(path: str | None) -> bool:
        nonlocal enumeration_complete
        if path is None:
            return True
        if path in paths:
            return True
        if len(paths) >= file_limit:
            if allow_incomplete:
                enumeration_complete = False
                return False
            raise _invalid("diff contains too many files")
        paths[path] = None
        return True

    def close_hunk(*, require_complete: bool = True) -> None:
        nonlocal hunk_state, current_hunk_lines
        if hunk_state is None:
            return
        old_seen = next(iter(hunk_state[5]["old_seen"]))
        new_seen = next(iter(hunk_state[5]["new_seen"]))
        if old_seen != hunk_state[1] or new_seen != hunk_state[3]:
            if allow_incomplete and not require_complete:
                hunk_state = None
                current_hunk_lines = []
                current_hunk_added.clear()
                current_hunk_deleted.clear()
                return
            raise _invalid("diff hunk body does not match its header")
        text = "\n".join(current_hunk_lines) + "\n"
        record = DiffHunk(
            index=hunk_state[4],
            old_path=current_old_path,
            new_path=current_path,
            old_start=hunk_state[0],
            old_count=hunk_state[1],
            new_start=hunk_state[2],
            new_count=hunk_state[3],
            text=text,
            added_lines=frozenset(current_hunk_added),
            deleted_lines=frozenset(current_hunk_deleted),
        )
        hunk_records.append(record)
        current_file_hunks.append(record)
        hunk_state = None
        current_hunk_lines = []
        current_hunk_added.clear()
        current_hunk_deleted.clear()

    def finish() -> None:
        close_hunk(require_complete=True)

    def _reset_file_state() -> None:
        nonlocal in_file, current_binary, seen_hunk_in_file
        in_file = False
        current_binary = False
        seen_hunk_in_file = False
        current_file_lines.clear()
        current_header_lines.clear()
        current_file_hunks.clear()
        current_file_added.clear()
        current_file_deleted.clear()

    def abandon_file() -> None:
        close_hunk(require_complete=not allow_incomplete)
        if not in_file:
            return
        _reset_file_state()

    def close_file() -> None:
        close_hunk(require_complete=not allow_incomplete)
        if not in_file:
            return
        text = "\n".join(current_file_lines) + ("\n" if current_file_lines else "")
        header = "\n".join(current_header_lines) + (
            "\n" if current_header_lines else ""
        )
        file_records.append(
            DiffFileRecord(
                old_path=current_old_path,
                new_path=current_path,
                text=text,
                header=header,
                added_lines=frozenset(current_file_added),
                deleted_lines=frozenset(current_file_deleted),
                hunks=tuple(current_file_hunks),
                binary=current_binary,
            )
        )
        _reset_file_state()

    def start_file() -> None:
        nonlocal in_file
        close_file()
        in_file = True

    def append_file_line(line: str) -> None:
        if not in_file:
            return
        current_file_lines.append(line)
        if not seen_hunk_in_file:
            current_header_lines.append(line)

    def commit_git_header(candidate: tuple[str | None, str | None]) -> None:
        record_path(candidate[0])
        record_path(candidate[1])

    def select_git_header(header: _GitHeader) -> None:
        nonlocal pending_git_header, selected_git_header
        if header.unresolved_unquoted is not None:
            pending_git_header = header.unresolved_unquoted
            selected_git_header = None
            return
        if header.fields is None:
            raise _invalid("diff contains an invalid Git header")
        selected_git_header = _decode_git_header_fields(header.fields)
        commit_git_header(selected_git_header)
        pending_git_header = None

    def resolve_pending_git_header(
        old_path: str | None,
        new_path: str | None,
    ) -> None:
        nonlocal pending_git_header, selected_git_header
        if pending_git_header is None:
            return
        if old_path is None and new_path is None:
            raise _invalid("diff Git header could not be resolved")
        old_header_path = new_path if old_path is None else old_path
        new_header_path = old_path if new_path is None else new_path
        expected = f"a/{old_header_path} b/{new_header_path}"
        if pending_git_header != expected:
            raise _invalid("diff Git header could not be resolved")
        selected_git_header = (old_header_path, new_header_path)
        commit_git_header(selected_git_header)
        pending_git_header = None

    for line in lines:
        if line.startswith("diff --git "):
            finish()
            if pending_old_marker:
                raise _invalid("diff contains an unmatched file marker")
            if pending_git_header is not None:
                raise _invalid("diff Git header could not be resolved")
            if pending_rename_from is not None or rename_from_for_header is not None:
                raise _invalid("diff contains an unmatched rename marker")
            start_file()
            saw_structure = True
            if utf8_size(line, label="diff Git header") > _MAX_GIT_HEADER_BYTES:
                raise _invalid("diff Git header exceeds the configured size limit")
            select_git_header(_split_git_header(line))
            rename_from_for_header = None
            if selected_git_header is not None:
                current_old_path, current_path = selected_git_header
            else:
                current_path = None
                current_old_path = None
            new_line = None
            old_line = None
            pending_old_marker = False
            matched_file_marker = False
            append_file_line(line)
            continue

        if hunk_state is None and line.startswith("--- "):
            finish()
            saw_structure = True
            if pending_old_marker:
                raise _invalid("diff contains an unmatched file marker")
            if not in_file:
                start_file()
            # Retain the old marker only until its matching +++ line.  Deleted
            # files are still represented in ``changed_paths``.
            old_path = _decode_marker(line, prefix="--- ", side=True)
            current_path = old_path
            current_old_path = old_path
            new_line = None
            old_line = None
            pending_old_marker = True
            append_file_line(line)
            continue
        if hunk_state is None and line.startswith("+++ "):
            finish()
            saw_structure = True
            if not pending_old_marker:
                raise _invalid("diff contains an unmatched file marker")
            new_path = _decode_marker(line, prefix="+++ ", side=True)
            if pending_rename_from is not None:
                raise _invalid("diff contains conflicting rename metadata")
            resolve_pending_git_header(current_old_path, new_path)
            if (
                pending_git_header is None
                and selected_git_header is not None
                and current_old_path is not None
                and new_path is not None
                and selected_git_header != (current_old_path, new_path)
            ):
                raise _invalid("diff file markers conflict with the Git header")
            if not record_path(current_old_path) or not record_path(new_path):
                stopped_for_budget = True
                abandon_file()
                break
            if (
                current_old_path is not None
                and new_path is not None
                and current_old_path != new_path
            ):
                renamed_pairs.append((current_old_path, new_path))
            current_path = new_path
            pending_old_marker = False
            if new_path is not None:
                changed.setdefault(new_path, set())
            new_line = None
            matched_file_marker = True
            append_file_line(line)
            continue
        if hunk_state is None and (line.startswith("---") or line.startswith("+++")):
            raise _invalid("diff contains an invalid file marker")

        if line.startswith("rename from "):
            finish()
            if pending_old_marker:
                raise _invalid("diff contains an unmatched file marker")
            saw_structure = True
            rename_from = _decode_rename_path(line, "rename from ")
            if pending_git_header is not None:
                if pending_rename_from is not None:
                    raise _invalid("diff contains duplicate rename metadata")
                pending_rename_from = rename_from
            elif selected_git_header is not None:
                if rename_from_for_header is not None:
                    raise _invalid("diff contains duplicate rename metadata")
                rename_from_for_header = rename_from
            else:
                if not record_path(rename_from):
                    stopped_for_budget = True
                    abandon_file()
                    break
            append_file_line(line)
            continue
        if line.startswith("rename to "):
            finish()
            if pending_old_marker:
                raise _invalid("diff contains an unmatched file marker")
            saw_structure = True
            rename_to = _decode_rename_path(line, "rename to ")
            if pending_git_header is not None:
                if pending_rename_from is None:
                    raise _invalid("diff contains an unmatched rename marker")
                resolve_pending_git_header(pending_rename_from, rename_to)
                if not record_path(pending_rename_from) or not record_path(rename_to):
                    stopped_for_budget = True
                    abandon_file()
                    break
                if pending_rename_from is not None and rename_to is not None:
                    renamed_pairs.append((pending_rename_from, rename_to))
                pending_rename_from = None
            elif selected_git_header is not None:
                if (
                    rename_from_for_header != selected_git_header[0]
                    or rename_to != selected_git_header[1]
                ):
                    raise _invalid("diff rename metadata conflicts with the Git header")
                if (
                    rename_from_for_header is not None
                    and rename_to is not None
                    and rename_from_for_header != rename_to
                ):
                    renamed_pairs.append((rename_from_for_header, rename_to))
                rename_from_for_header = None
            else:
                if not record_path(rename_to):
                    stopped_for_budget = True
                    abandon_file()
                    break
            append_file_line(line)
            continue

        if line.startswith("@@"):
            finish()
            if pending_old_marker:
                raise _invalid("diff contains an unmatched file marker")
            if not matched_file_marker:
                raise _invalid("diff hunk has no matched file marker")
            saw_structure = True
            match = _HUNK_HEADER.fullmatch(line)
            if match is None:
                raise _invalid("diff contains an invalid hunk header")
            raw_numbers = tuple(match.group(index) or "1" for index in (1, 2, 3, 4))
            max_digits = len(str(limits.max_line_number))
            if any(len(number) > max_digits for number in raw_numbers):
                raise _invalid("diff hunk header exceeds the line limit")
            old_start, old_count, new_start, new_count = (
                int(number) for number in raw_numbers
            )
            if old_count < 0 or new_count < 0 or old_start < 0 or new_start < 0:
                raise _invalid("diff hunk header contains an invalid line number")
            if (old_start == 0 and old_count != 0) or (
                new_start == 0 and new_count != 0
            ):
                raise _invalid("diff hunk header contains an invalid zero line range")
            if old_count == 0 and new_count == 0:
                raise _invalid("diff hunk header must describe at least one line")
            if (
                old_start > limits.max_line_number
                or new_start > limits.max_line_number
                or old_count > limits.max_line_number
                or new_count > limits.max_line_number
            ):
                raise _invalid("diff hunk header exceeds the line limit")
            old_end = old_start if old_count == 0 else old_start + old_count - 1
            new_end = new_start if new_count == 0 else new_start + new_count - 1
            if old_end > limits.max_line_number or new_end > limits.max_line_number:
                raise _invalid("diff hunk header exceeds the line limit")
            hunk_count += 1
            if hunk_count > hunk_limit:
                if allow_incomplete:
                    enumeration_complete = False
                    stopped_for_budget = True
                    close_file()
                    break
                raise _invalid("diff contains too many hunks")
            state = {"old_seen": {0}, "new_seen": {0}}
            hunk_state = (old_start, old_count, new_start, new_count, hunk_count, state)
            new_line = new_start
            old_line = old_start
            seen_hunk_in_file = True
            current_hunk_lines = [line]
            append_file_line(line)
            continue

        if hunk_state is None:
            # Extended headers, binary markers, and index lines are valid.  A
            # bare malformed hunk marker was handled above.
            if line.startswith("Binary files ") or line.startswith("GIT binary patch"):
                current_binary = True
                for path in (current_old_path, current_path):
                    if path is not None:
                        binary_paths[path] = None
            append_file_line(line)
            continue

        old_seen = next(iter(hunk_state[5]["old_seen"]))
        new_seen = next(iter(hunk_state[5]["new_seen"]))
        if line.startswith("\\"):
            if not line.startswith("\\ No newline at end of file"):
                raise _invalid("diff contains an invalid hunk marker")
            current_hunk_lines.append(line)
            append_file_line(line)
            continue
        if not line:
            if old_seen >= hunk_state[1] and new_seen >= hunk_state[3]:
                finish()
                append_file_line(line)
                continue
            raise _invalid("diff hunk contains an invalid line")

        if line.startswith("+"):
            new_seen += 1
            target_line = new_line or 1
            if target_line > limits.max_line_number:
                raise _invalid("diff hunk exceeds the line limit")
            if current_path is not None:
                changed.setdefault(current_path, set()).add(target_line)
                current_file_added.add(target_line)
                current_hunk_added.add(target_line)
            new_line = (new_line or 1) + 1
        elif line.startswith("-"):
            old_seen += 1
            target_line = old_line or 1
            if target_line > limits.max_line_number:
                raise _invalid("diff hunk exceeds the line limit")
            if current_old_path is not None and hunk_state[1] > 0:
                deleted.setdefault(current_old_path, set()).add(target_line)
                current_file_deleted.add(target_line)
                current_hunk_deleted.add(target_line)
            old_line = (old_line or 1) + 1
        elif line.startswith(" "):
            old_seen += 1
            new_seen += 1
            if (new_line or 1) > limits.max_line_number:
                raise _invalid("diff hunk exceeds the line limit")
            if (old_line or 1) > limits.max_line_number:
                raise _invalid("diff hunk exceeds the line limit")
            new_line = (new_line or 1) + 1
            old_line = (old_line or 1) + 1
        else:
            # Some callers append non-patch text after a complete hunk.  Once
            # the declared body counts are satisfied, close the hunk and let
            # the surrounding unified-diff parser ignore that trailing text.
            if old_seen >= hunk_state[1] and new_seen >= hunk_state[3]:
                finish()
                append_file_line(line)
                continue
            raise _invalid("diff hunk contains an invalid line")
        current_hunk_lines.append(line)
        append_file_line(line)
        hunk_state[5]["old_seen"] = {old_seen}
        hunk_state[5]["new_seen"] = {new_seen}
        if new_line is not None and new_line > limits.max_line_number + 1:
            raise _invalid("diff hunk exceeds the line limit")
        if old_seen > hunk_state[1] or new_seen > hunk_state[3]:
            raise _invalid("diff hunk body exceeds its header")

    close_hunk(require_complete=not allow_incomplete)
    close_file()
    if not stopped_for_budget and pending_old_marker:
        raise _invalid("diff contains an unmatched file marker")
    if not stopped_for_budget and (
        pending_git_header is not None
        or pending_rename_from is not None
        or rename_from_for_header is not None
    ):
        raise _invalid("diff Git header could not be resolved")
    if not saw_structure:
        raise _invalid("diff contains no supported diff records")
    unique_renames: dict[tuple[str, str], None] = {}
    for pair in renamed_pairs:
        unique_renames.setdefault(pair, None)
    total_lines = len(diff.splitlines())
    return DiffAnalysis(
        changed_lines={path: frozenset(values) for path, values in changed.items()},
        changed_paths=tuple(paths),
        diff_bytes=byte_count,
        diff_lines=total_lines,
        diff_files=len(paths),
        diff_hunks=hunk_count,
        deleted_lines={path: frozenset(values) for path, values in deleted.items()},
        binary_paths=tuple(binary_paths),
        renamed_paths=tuple(unique_renames),
        hunk_records=tuple(hunk_records),
        file_records=tuple(file_records),
        enumeration_complete=enumeration_complete,
    )


def parse_changed_lines(
    diff: str,
    *,
    limits: ReviewLimits = DEFAULT_REVIEW_LIMITS,
) -> dict[str, frozenset[int]]:
    """Compatibility wrapper returning added/modified new-file lines."""

    return analyze_diff(diff, limits=limits).changed_lines


def parse_changed_paths(
    diff: str,
    *,
    limits: ReviewLimits = DEFAULT_REVIEW_LIMITS,
) -> tuple[str, ...]:
    """Compatibility wrapper returning old/new repository paths."""

    return analyze_diff(diff, limits=limits).changed_paths


__all__ = [
    "DiffAnalysis",
    "DiffFileRecord",
    "DiffHunk",
    "analyze_diff",
    "parse_changed_lines",
    "parse_changed_paths",
]
