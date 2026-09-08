"""Portable workflow composition helpers for ReviewSensei.

This module is deliberately independent of GitHub and model-provider SDKs.  It
validates untrusted workflow inputs and shells out to ``git`` with list-based
argv so refs and repository slugs are never interpolated into a shell.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .diff import analyze_diff
from .errors import ReviewInputError
from .validation import DEFAULT_REVIEW_LIMITS, ReviewLimits, read_bounded_utf8

_REF = re.compile(r"^[A-Za-z0-9._/-]+$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_VERSION = re.compile(r"^v?[0-9]+\.[0-9]+\.[0-9]+$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHELL_METACHARACTERS = frozenset(";&|<>`$!*?[]{}()\\\"'")


def _invalid(message: str) -> ReviewInputError:
    # Messages are static and intentionally omit untrusted values.
    return ReviewInputError(message)


def _validate_ref(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise _invalid(f"{label} must be a non-empty string")
    if value.startswith("-"):
        raise _invalid(f"{label} must not start with a dash")
    if value != unicodedata.normalize("NFC", value):
        raise _invalid(f"{label} must already be NFC")
    if any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value
    ):
        raise _invalid(f"{label} contains a forbidden control character")
    if any(character.isspace() for character in value):
        raise _invalid(f"{label} must not contain whitespace")
    if any(character in _SHELL_METACHARACTERS for character in value):
        raise _invalid(f"{label} contains a forbidden character")
    if not _REF.fullmatch(value):
        raise _invalid(f"{label} is not a supported Git ref")
    if any(segment in {".", ".."} or not segment for segment in value.split("/")):
        raise _invalid(f"{label} must not contain dot or empty segments")
    return value


def _validate_repository(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise _invalid("head_repository must be a non-empty string")
    if value.startswith("-"):
        raise _invalid("head_repository must not start with a dash")
    if value != unicodedata.normalize("NFC", value):
        raise _invalid("head_repository must already be NFC")
    if any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value
    ):
        raise _invalid("head_repository contains a forbidden control character")
    if any(character.isspace() for character in value):
        raise _invalid("head_repository must not contain whitespace")
    if any(character in _SHELL_METACHARACTERS for character in value):
        raise _invalid("head_repository contains a forbidden character")
    if not _REPOSITORY.fullmatch(value):
        raise _invalid("head_repository must be an owner/repo slug")
    if value.endswith(".git"):
        raise _invalid("head_repository must be an owner/repo slug")
    return value


def _validate_head_remote(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise _invalid("head_remote must be a non-empty string")
    if value.startswith("-"):
        raise _invalid("head_remote must not start with a dash")
    if value != unicodedata.normalize("NFC", value):
        raise _invalid("head_remote must already be NFC")
    if any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value
    ):
        raise _invalid("head_remote contains a forbidden control character")
    if any(character.isspace() for character in value):
        raise _invalid("head_remote must not contain whitespace")
    if any(character in _SHELL_METACHARACTERS for character in value):
        raise _invalid("head_remote contains a forbidden character")
    return value


def _positive_int(value: object, *, label: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _invalid(f"{label} must be a positive integer")
    ceiling = getattr(DEFAULT_REVIEW_LIMITS, label)
    if value > ceiling:
        raise _invalid(f"{label} exceeds the public ceiling")
    return value


def normalize_review_sensei_version(value: object) -> str:
    """Return a normalized ``X.Y.Z`` version or raise ``ReviewInputError``."""

    if not isinstance(value, str) or not value:
        raise _invalid("review_sensei_version must be a non-empty string")
    if value != unicodedata.normalize("NFC", value):
        raise _invalid("review_sensei_version must already be NFC")
    if any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value
    ):
        raise _invalid("review_sensei_version contains a forbidden control character")
    if not _VERSION.fullmatch(value):
        raise _invalid("review_sensei_version must be an exact X.Y.Z version")
    return value[1:] if value.startswith("v") else value


@dataclass(frozen=True)
class PrepareDiffArgs:
    """Validated arguments for :func:`prepare_diff`."""

    base_ref: str
    head_ref: str
    head_repository: str | None = None
    repository: Path = Path(".")
    output: Path = Path("pr.patch")
    max_diff_bytes: int = DEFAULT_REVIEW_LIMITS.max_diff_bytes
    max_diff_lines: int = DEFAULT_REVIEW_LIMITS.max_diff_lines
    max_diff_files: int = DEFAULT_REVIEW_LIMITS.max_diff_files
    max_diff_hunks: int = DEFAULT_REVIEW_LIMITS.max_diff_hunks

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "base_ref", _validate_ref(self.base_ref, label="base_ref")
        )
        object.__setattr__(
            self, "head_ref", _validate_ref(self.head_ref, label="head_ref")
        )
        object.__setattr__(
            self,
            "head_repository",
            _validate_repository(self.head_repository),
        )
        if not isinstance(self.repository, Path):
            raise _invalid("repository must be a path")
        if not isinstance(self.output, Path):
            raise _invalid("output must be a path")
        object.__setattr__(
            self,
            "max_diff_bytes",
            _positive_int(
                self.max_diff_bytes,
                label="max_diff_bytes",
                default=DEFAULT_REVIEW_LIMITS.max_diff_bytes,
            ),
        )
        object.__setattr__(
            self,
            "max_diff_lines",
            _positive_int(
                self.max_diff_lines,
                label="max_diff_lines",
                default=DEFAULT_REVIEW_LIMITS.max_diff_lines,
            ),
        )
        object.__setattr__(
            self,
            "max_diff_files",
            _positive_int(
                self.max_diff_files,
                label="max_diff_files",
                default=DEFAULT_REVIEW_LIMITS.max_diff_files,
            ),
        )
        object.__setattr__(
            self,
            "max_diff_hunks",
            _positive_int(
                self.max_diff_hunks,
                label="max_diff_hunks",
                default=DEFAULT_REVIEW_LIMITS.max_diff_hunks,
            ),
        )


def parse_prepare_diff_args(
    *,
    base_ref: object,
    head_ref: object,
    head_repository: object = None,
    repository: object = Path("."),
    output: object = Path("pr.patch"),
    max_diff_bytes: object = None,
    max_diff_lines: object = None,
    max_diff_files: object = None,
    max_diff_hunks: object = None,
) -> PrepareDiffArgs:
    """Validate prepare-diff inputs without invoking git."""

    validated_base = _validate_ref(base_ref, label="base_ref")
    validated_head = _validate_ref(head_ref, label="head_ref")
    validated_repository = _validate_repository(head_repository)
    if not isinstance(repository, Path):
        raise _invalid("repository must be a path")
    if not isinstance(output, Path):
        raise _invalid("output must be a path")
    validated_bytes = _positive_int(
        max_diff_bytes,
        label="max_diff_bytes",
        default=DEFAULT_REVIEW_LIMITS.max_diff_bytes,
    )
    validated_lines = _positive_int(
        max_diff_lines,
        label="max_diff_lines",
        default=DEFAULT_REVIEW_LIMITS.max_diff_lines,
    )
    validated_files = _positive_int(
        max_diff_files,
        label="max_diff_files",
        default=DEFAULT_REVIEW_LIMITS.max_diff_files,
    )
    validated_hunks = _positive_int(
        max_diff_hunks,
        label="max_diff_hunks",
        default=DEFAULT_REVIEW_LIMITS.max_diff_hunks,
    )
    return PrepareDiffArgs(
        base_ref=validated_base,
        head_ref=validated_head,
        head_repository=validated_repository,
        repository=repository,
        output=output,
        max_diff_bytes=validated_bytes,
        max_diff_lines=validated_lines,
        max_diff_files=validated_files,
        max_diff_hunks=validated_hunks,
    )


def _run_git(
    repository: Path,
    argv: Sequence[str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", *argv],
            cwd=repository,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise _invalid("git could not be executed") from exc
    if check and result.returncode != 0:
        raise _invalid("git command failed")
    return result


def _is_shallow(repository: Path) -> bool:
    return (repository / ".git" / "shallow").exists()


def _unshallow(repository: Path) -> None:
    result = _run_git(
        repository, ["fetch", "--unshallow", "--no-tags", "--prune", "--quiet"]
    )
    if result.returncode != 0:
        raise _invalid("repository is shallow and could not be unshallowed")


def _resolve_oid(repository: Path, ref: str) -> str:
    result = _run_git(
        repository,
        ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        check=False,
    )
    if result.returncode != 0:
        raise _invalid("requested ref could not be resolved")
    oid = result.stdout.strip()
    if not _SHA.fullmatch(oid):
        raise _invalid("requested ref did not resolve to a commit")
    return oid


def _temporary_ref(head_repository: str | None, head_ref: str) -> str:
    if head_repository is None:
        return f"refs/review-sensei/{head_ref}"
    safe_repository = head_repository.replace("/", "-")
    return f"refs/review-sensei/{safe_repository}/{head_ref}"


def _resolve_or_fetch_origin(
    repository: Path,
    *,
    ref: str,
    temporary_ref: str,
) -> str:
    try:
        return _resolve_oid(repository, ref)
    except ReviewInputError:
        return _fetch_ref(
            repository,
            remote="origin",
            ref=ref,
            temporary_ref=temporary_ref,
        )


def _fetch_ref(
    repository: Path,
    *,
    remote: str,
    ref: str,
    temporary_ref: str,
) -> str:
    result = _run_git(
        repository,
        [
            "fetch",
            "--no-tags",
            "--prune",
            "--quiet",
            remote,
            f"{ref}:{temporary_ref}",
        ],
        check=False,
    )
    if result.returncode != 0:
        raise _invalid("requested ref could not be fetched")
    return _resolve_oid(repository, temporary_ref)


def _stream_diff_to_patch(
    repository: Path,
    *,
    base_oid: str,
    head_oid: str,
    output: Path,
    limits: ReviewLimits,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix="review-sensei-diff-", dir=output.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            process = subprocess.Popen(
                ["git", "diff", f"{base_oid}...{head_oid}"],
                cwd=repository,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert process.stdout is not None
            try:
                total = 0
                while True:
                    chunk = process.stdout.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limits.max_diff_bytes:
                        process.kill()
                        process.wait()
                        raise _invalid("diff exceeds the configured byte limit")
                    stream.write(chunk)
                process.wait()
                if process.returncode != 0:
                    raise _invalid("git diff failed")
            finally:
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()
        diff = read_bounded_utf8(
            temporary,
            maximum=limits.max_diff_bytes,
            label="diff",
        )
        analyze_diff(diff, limits=limits)
        temporary.replace(output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def prepare_diff(
    *,
    base_ref: object,
    head_ref: object,
    head_repository: object = None,
    head_remote: object = None,
    repository: object = Path("."),
    output: object = Path("pr.patch"),
    max_diff_bytes: object = None,
    max_diff_lines: object = None,
    max_diff_files: object = None,
    max_diff_hunks: object = None,
) -> Path:
    """Validate inputs, fetch refs, and atomically publish a bounded diff."""

    args = parse_prepare_diff_args(
        base_ref=base_ref,
        head_ref=head_ref,
        head_repository=head_repository,
        repository=repository,
        output=output,
        max_diff_bytes=max_diff_bytes,
        max_diff_lines=max_diff_lines,
        max_diff_files=max_diff_files,
        max_diff_hunks=max_diff_hunks,
    )
    limits = ReviewLimits(
        max_diff_bytes=args.max_diff_bytes,
        max_diff_lines=args.max_diff_lines,
        max_diff_files=args.max_diff_files,
        max_diff_hunks=args.max_diff_hunks,
    )
    root = args.repository.resolve()
    if _is_shallow(root):
        _unshallow(root)

    base_oid = _resolve_or_fetch_origin(
        root,
        ref=args.base_ref,
        temporary_ref=f"refs/review-sensei/base/{args.base_ref}",
    )
    if args.head_repository is None:
        head_oid = _resolve_or_fetch_origin(
            root,
            ref=args.head_ref,
            temporary_ref=_temporary_ref(None, args.head_ref),
        )
    else:
        remote = _validate_head_remote(head_remote)
        if remote is None:
            remote = f"https://github.com/{args.head_repository}.git"
        head_oid = _fetch_ref(
            root,
            remote=remote,
            ref=args.head_ref,
            temporary_ref=_temporary_ref(args.head_repository, args.head_ref),
        )

    merge_base = _run_git(
        root,
        ["merge-base", base_oid, head_oid],
        check=False,
    )
    if merge_base.returncode != 0:
        raise _invalid("base and head commits do not share a merge base")

    _stream_diff_to_patch(
        root,
        base_oid=base_oid,
        head_oid=head_oid,
        output=args.output,
        limits=limits,
    )
    return args.output
