"""Provider-neutral validation and bounded input helpers.

The functions in this module are deliberately independent of GitHub and model
provider transports.  They form the common trust boundary used by request,
diff, stage, and publisher-facing result contracts.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .errors import ReviewInputError

MAX_REPOSITORY_PATH_BYTES = 4_096
MAX_REPOSITORY_PATH_SEGMENT_BYTES = 255


@dataclass(frozen=True)
class ReviewLimits:
    """The public, downward-only resource profile for one review.

    Defaults are hard ceilings.  Embedders may construct a profile with lower
    values, but never with a value above the corresponding default or a
    non-positive/boolean value.
    """

    max_diff_bytes: int = 1_048_576
    max_diff_lines: int = 50_000
    max_diff_files: int = 500
    max_diff_hunks: int = 5_000
    max_repository_bytes: int = 512
    max_title_bytes: int = 4_096
    max_instructions_bytes: int = 65_536
    max_metadata_items: int = 64
    max_metadata_key_bytes: int = 128
    max_metadata_value_bytes: int = 4_096
    max_model_bytes: int = 256
    max_learning_entries: int = 100
    max_active_categories: int = 64
    max_lens_contexts: int = 64
    max_prompt_bytes: int = 4_194_304
    max_provider_response_bytes: int = 1_048_576
    max_summary_bytes: int = 32_768
    max_comments: int = 250
    max_comment_body_bytes: int = 16_384
    max_learning_proposals: int = 100
    max_learning_proposal_bytes: int = 16_384
    max_learning_proposals_total_bytes: int = 262_144
    max_result_bytes: int = 2_097_152
    max_line_number: int = 2_147_483_647

    @property
    def max_response_bytes(self) -> int:
        """Compatibility spelling for provider response ceilings."""

        return self.max_provider_response_bytes

    def __post_init__(self) -> None:
        for name, value in self.__dict__.items():
            default = getattr(type(self), name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ReviewInputError(
                    f"review limit {name} must be a positive integer"
                )
            if value > default:
                raise ReviewInputError(
                    f"review limit {name} exceeds the public ceiling"
                )


DEFAULT_REVIEW_LIMITS = ReviewLimits()


def _utf8_bytes(value: object, *, label: str) -> bytes:
    if not isinstance(value, str):
        raise ReviewInputError(f"{label} must be a string")
    try:
        return value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        # Do not expose the offending untrusted value or code point.
        raise ReviewInputError(f"{label} must be valid UTF-8") from exc


def utf8_size(value: object, *, label: str = "value") -> int:
    """Return the strict UTF-8 byte size of ``value``."""

    return len(_utf8_bytes(value, label=label))


def validate_bounded_text(
    value: object,
    maximum: int,
    *,
    label: str,
    allow_empty: bool = True,
) -> str:
    """Validate a UTF-8 string and return it without normalizing it."""

    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1:
        raise ReviewInputError(f"{label} size limit must be a positive integer")
    raw = _utf8_bytes(value, label=label)
    if not allow_empty and not raw:
        raise ReviewInputError(f"{label} must be non-empty")
    if len(raw) > maximum:
        raise ReviewInputError(f"{label} exceeds the configured size limit")
    return value  # type: ignore[return-value]


def _has_forbidden_codepoint(value: str) -> bool:
    return any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value
    )


def validate_repository_path(
    value: object,
    *,
    pattern: bool = False,
    allow_patterns: bool | None = None,
    allow_glob_chars: bool = False,
    label: str = "repository path",
) -> str:
    """Validate one canonical repository-relative POSIX path.

    No normalization is performed.  In particular, the caller must supply NFC
    text and must not use dot segments, repeated separators, backslashes, drive
    prefixes, or a root shorthand.  ``pattern=True`` marks a value as a glob
    pattern field.  ``allow_glob_chars=True`` permits literal glob metacharacters
    in non-pattern paths such as real Git filenames.
    """

    if allow_patterns is not None:
        pattern = allow_patterns
    raw = _utf8_bytes(value, label=label)
    path = cast(str, value)
    if not path:
        raise ReviewInputError(f"{label} must be repository-relative")
    if len(raw) > MAX_REPOSITORY_PATH_BYTES:
        raise ReviewInputError(f"{label} exceeds the maximum path length")
    if path != unicodedata.normalize("NFC", path):
        raise ReviewInputError(f"{label} must already be NFC")
    if _has_forbidden_codepoint(path):
        raise ReviewInputError(f"{label} contains a forbidden control character")
    if path != path.strip() or "\\" in path:
        raise ReviewInputError(f"{label} must use canonical repository separators")
    first_segment = path.split("/", 1)[0]
    if (
        path.startswith("/")
        or path.endswith("/")
        or re.match(r"^[A-Za-z]:", first_segment) is not None
    ):
        raise ReviewInputError(f"{label} must be repository-relative")
    segments = path.split("/")
    if any(not segment or segment in {".", ".."} for segment in segments):
        raise ReviewInputError(f"{label} must not contain dot or empty segments")
    if any(segment != segment.strip() for segment in segments):
        raise ReviewInputError(f"{label} must not contain surrounding whitespace")
    if any(
        len(segment.encode("utf-8")) > MAX_REPOSITORY_PATH_SEGMENT_BYTES
        for segment in segments
    ):
        raise ReviewInputError(f"{label} contains an oversized path segment")
    if not pattern and not allow_glob_chars:
        if any(token in path for token in ("*", "?", "[", "]")):
            raise ReviewInputError(f"{label} must not contain glob tokens")
    return path


def decode_git_c_quoted_path(
    value: object,
    *,
    allow_glob_chars: bool = False,
    label: str = "Git diff path",
) -> str:
    """Decode Git's strict C-quoted path representation and validate it.

    The decoder works at the byte level so octal escapes are never interpreted
    as Unicode code points by a shell parser.  Unquoted values are treated as
    already decoded UTF-8 text.
    """

    if not isinstance(value, str):
        raise ReviewInputError(f"{label} must be a string")
    if not value.startswith('"'):
        validate_repository_path(
            value,
            label=label,
            allow_glob_chars=allow_glob_chars,
        )
        return value
    if len(value) < 2 or not value.endswith('"'):
        raise ReviewInputError(f"{label} contains an invalid quoted path")

    content = value[1:-1]
    output = bytearray()
    index = 0
    named = {
        "a": 0x07,
        "b": 0x08,
        "t": 0x09,
        "n": 0x0A,
        "v": 0x0B,
        "f": 0x0C,
        "r": 0x0D,
        '"': 0x22,
        "\\": 0x5C,
    }
    try:
        while index < len(content):
            character = content[index]
            if character != "\\":
                # Git's quoted representation is ASCII for escaped bytes, but
                # tolerate literal Unicode and encode it strictly below.
                output.extend(character.encode("utf-8", errors="strict"))
                index += 1
                continue
            index += 1
            if index >= len(content):
                raise ValueError
            escaped = content[index]
            if escaped in named:
                output.append(named[escaped])
                index += 1
                continue
            if escaped not in "01234567" or index + 3 > len(content):
                raise ValueError
            digits = content[index : index + 3]
            if any(digit not in "01234567" for digit in digits):
                raise ValueError
            output.append(int(digits, 8))
            index += 3
    except (UnicodeEncodeError, ValueError) as exc:
        raise ReviewInputError(f"{label} contains an invalid quoted escape") from exc

    try:
        decoded = bytes(output).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ReviewInputError(f"{label} is not valid UTF-8") from exc
    validate_repository_path(
        decoded,
        label=label,
        allow_glob_chars=allow_glob_chars,
    )
    return decoded


def read_bounded_utf8(path: Path, *, maximum: int, label: str = "input") -> str:
    """Read at most ``maximum + 1`` bytes and decode strict UTF-8."""

    try:
        with Path(path).open("rb") as stream:
            content = stream.read(maximum + 1)
    except OSError as exc:
        raise ReviewInputError(f"{label} could not be read") from exc
    if len(content) > maximum:
        raise ReviewInputError(f"{label} exceeds the configured size limit")
    try:
        return content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ReviewInputError(f"{label} must be valid UTF-8") from exc


# Compatibility aliases for callers that prefer an explicit decoder/helper name.
decode_git_path = decode_git_c_quoted_path
decode_git_quoted_path = decode_git_c_quoted_path
canonical_repository_path = validate_repository_path
validate_canonical_path = validate_repository_path
read_bounded_text = read_bounded_utf8
bounded_read = read_bounded_utf8


__all__ = [
    "DEFAULT_REVIEW_LIMITS",
    "MAX_REPOSITORY_PATH_BYTES",
    "MAX_REPOSITORY_PATH_SEGMENT_BYTES",
    "ReviewLimits",
    "decode_git_c_quoted_path",
    "decode_git_path",
    "decode_git_quoted_path",
    "canonical_repository_path",
    "validate_canonical_path",
    "bounded_read",
    "read_bounded_text",
    "read_bounded_utf8",
    "utf8_size",
    "validate_bounded_text",
    "validate_repository_path",
]
