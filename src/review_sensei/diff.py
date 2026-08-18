from __future__ import annotations

import re
from dataclasses import dataclass

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
class DiffAnalysis:
    """The bounded, single-pass facts extracted from a unified/Git diff."""

    changed_lines: dict[str, frozenset[int]]
    changed_paths: tuple[str, ...]
    diff_bytes: int
    diff_lines: int
    diff_files: int
    diff_hunks: int

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
) -> DiffAnalysis:
    """Parse and validate a diff in one bounded pass.

    Both Git extended diffs and plain unified diffs are supported.  All path
    decoding and hunk validation occurs before a caller may construct a
    provider request.
    """

    if not isinstance(limits, ReviewLimits):
        raise _invalid("diff limits must be a ReviewLimits value")
    if not isinstance(diff, str) or not diff.strip():
        raise _invalid("diff must be a non-empty string")
    byte_count = utf8_size(diff, label="diff")
    if byte_count > limits.max_diff_bytes:
        raise _invalid("diff exceeds the configured byte limit")
    lines = diff.splitlines()
    if len(lines) > limits.max_diff_lines:
        raise _invalid("diff exceeds the configured line limit")

    changed: dict[str, set[int]] = {}
    paths: dict[str, None] = {}
    current_path: str | None = None
    new_line: int | None = None
    pending_old_marker = False
    matched_file_marker = False
    hunk_count = 0
    saw_structure = False
    hunk_state: tuple[int, int, int, int, int, dict[str, set[int]]] | None = None
    pending_git_header: str | None = None
    pending_rename_from: str | None = None
    selected_git_header: tuple[str | None, str | None] | None = None
    rename_from_for_header: str | None = None

    def finish() -> None:
        nonlocal hunk_state
        if hunk_state is not None:
            old_seen = next(iter(hunk_state[5]["old_seen"]))
            new_seen = next(iter(hunk_state[5]["new_seen"]))
            if old_seen != hunk_state[1] or new_seen != hunk_state[3]:
                raise _invalid("diff hunk body does not match its header")
            hunk_state = None

    def commit_git_header(candidate: tuple[str | None, str | None]) -> None:
        _add_path(paths, candidate[0], limits)
        _add_path(paths, candidate[1], limits)

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
            saw_structure = True
            if utf8_size(line, label="diff Git header") > _MAX_GIT_HEADER_BYTES:
                raise _invalid("diff Git header exceeds the configured size limit")
            select_git_header(_split_git_header(line))
            rename_from_for_header = None
            current_path = None
            new_line = None
            pending_old_marker = False
            matched_file_marker = False
            continue

        if hunk_state is None and line.startswith("--- "):
            finish()
            saw_structure = True
            if pending_old_marker:
                raise _invalid("diff contains an unmatched file marker")
            # Retain the old marker only until its matching +++ line.  Deleted
            # files are still represented in ``changed_paths``.
            old_path = _decode_marker(line, prefix="--- ", side=True)
            current_path = old_path
            new_line = None
            pending_old_marker = True
            continue
        if hunk_state is None and line.startswith("+++ "):
            finish()
            saw_structure = True
            if not pending_old_marker:
                raise _invalid("diff contains an unmatched file marker")
            new_path = _decode_marker(line, prefix="+++ ", side=True)
            if pending_rename_from is not None:
                raise _invalid("diff contains conflicting rename metadata")
            resolve_pending_git_header(current_path, new_path)
            if (
                pending_git_header is None
                and selected_git_header is not None
                and current_path is not None
                and new_path is not None
                and selected_git_header != (current_path, new_path)
            ):
                raise _invalid("diff file markers conflict with the Git header")
            _add_path(paths, current_path, limits)
            _add_path(paths, new_path, limits)
            current_path = new_path
            pending_old_marker = False
            if new_path is not None:
                changed.setdefault(new_path, set())
            new_line = None
            matched_file_marker = True
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
                _add_path(paths, rename_from, limits)
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
                _add_path(paths, pending_rename_from, limits)
                _add_path(paths, rename_to, limits)
                pending_rename_from = None
            elif selected_git_header is not None:
                if (
                    rename_from_for_header != selected_git_header[0]
                    or rename_to != selected_git_header[1]
                ):
                    raise _invalid("diff rename metadata conflicts with the Git header")
                rename_from_for_header = None
            else:
                _add_path(paths, rename_to, limits)
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
            if hunk_count > limits.max_diff_hunks:
                raise _invalid("diff contains too many hunks")
            state = {"old_seen": {0}, "new_seen": {0}}
            hunk_state = (old_start, old_count, new_start, new_count, hunk_count, state)
            new_line = new_start
            continue

        if hunk_state is None:
            # Extended headers, binary markers, and index lines are valid.  A
            # bare malformed hunk marker was handled above.
            continue

        old_seen = next(iter(hunk_state[5]["old_seen"]))
        new_seen = next(iter(hunk_state[5]["new_seen"]))
        if line.startswith("\\"):
            if not line.startswith("\\ No newline at end of file"):
                raise _invalid("diff contains an invalid hunk marker")
            continue
        if not line:
            if old_seen >= hunk_state[1] and new_seen >= hunk_state[3]:
                finish()
                continue
            raise _invalid("diff hunk contains an invalid line")

        if line.startswith("+"):
            new_seen += 1
            target_line = new_line or 1
            if target_line > limits.max_line_number:
                raise _invalid("diff hunk exceeds the line limit")
            if current_path is not None:
                changed.setdefault(current_path, set()).add(target_line)
            new_line = (new_line or 1) + 1
        elif line.startswith("-"):
            old_seen += 1
        elif line.startswith(" "):
            old_seen += 1
            new_seen += 1
            if (new_line or 1) > limits.max_line_number:
                raise _invalid("diff hunk exceeds the line limit")
            new_line = (new_line or 1) + 1
        else:
            # Some callers append non-patch text after a complete hunk.  Once
            # the declared body counts are satisfied, close the hunk and let
            # the surrounding unified-diff parser ignore that trailing text.
            if old_seen >= hunk_state[1] and new_seen >= hunk_state[3]:
                finish()
                continue
            raise _invalid("diff hunk contains an invalid line")
        hunk_state[5]["old_seen"] = {old_seen}
        hunk_state[5]["new_seen"] = {new_seen}
        if new_line is not None and new_line > limits.max_line_number + 1:
            raise _invalid("diff hunk exceeds the line limit")
        if old_seen > hunk_state[1] or new_seen > hunk_state[3]:
            raise _invalid("diff hunk body exceeds its header")

    finish()
    if pending_old_marker:
        raise _invalid("diff contains an unmatched file marker")
    if (
        pending_git_header is not None
        or pending_rename_from is not None
        or rename_from_for_header is not None
    ):
        raise _invalid("diff Git header could not be resolved")
    if not saw_structure:
        raise _invalid("diff contains no supported diff records")
    return DiffAnalysis(
        changed_lines={path: frozenset(lines) for path, lines in changed.items()},
        changed_paths=tuple(paths),
        diff_bytes=byte_count,
        diff_lines=len(lines),
        diff_files=len(paths),
        diff_hunks=hunk_count,
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


__all__ = ["DiffAnalysis", "analyze_diff", "parse_changed_lines", "parse_changed_paths"]
