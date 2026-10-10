"""Explicit human review of unsupported blobs; never model authorization.

Existing AI coverage stays intact. These records are reference data until a
host authenticates the current review, immutable blobs and live human sources.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from html import escape
from typing import Mapping, cast

from .coverage import CoverageManifest
from .errors import ReviewInputError
from .evidence import evidence_digest
from .validation import validate_repository_path

MAX_FILES = 32
MAX_DOCUMENT_BYTES = 32 * 1024
MAX_SOURCE_BYTES = 8192
_DIGEST = re.compile(r"[a-f0-9]{64}")
_SHA = re.compile(r"[a-f0-9]{40}")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def positive(value: object) -> bool:
    return type(value) is int and value > 0


def digest(value: object) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def sha(value: object) -> bool:
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def display_path(path: str) -> str:
    """Display exact names without making mentions, links or Markdown commands."""
    return (
        "<code>"
        + escape(canonical(path)).replace("@", "&#64;").replace("`", "&#96;")
        + "</code>"
    )


def _closed(value: object, fields: set[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ReviewInputError("human file review document shape is invalid")
    return value


@dataclass(frozen=True)
class UnsupportedFile:
    """One exact old/new regular blob change, including rename or deletion."""

    path: str
    change: str
    old_blob: str | None
    new_blob: str | None
    old_path: str | None = None
    old_mode: str | None = None
    new_mode: str | None = None

    def __post_init__(self) -> None:
        validate_repository_path(
            self.path, label="human file path", allow_glob_chars=True
        )
        if not isinstance(self.change, str) or self.change not in {
            "added",
            "modified",
            "removed",
            "renamed",
        }:
            raise ReviewInputError("human file change type is invalid")
        for value in (self.old_blob, self.new_blob):
            if value is not None and not sha(value):
                raise ReviewInputError("human file blob identity is invalid")
        for mode, blob in (
            (self.old_mode, self.old_blob),
            (self.new_mode, self.new_blob),
        ):
            if mode is not None and (
                not isinstance(mode, str)
                or mode not in {"100644", "100755"}
                or blob is None
            ):
                raise ReviewInputError("human file mode is invalid")
        if self.change == "added" and (
            self.old_blob is not None or self.new_blob is None
        ):
            raise ReviewInputError("human added file evidence is invalid")
        if self.change == "removed" and (
            self.old_blob is None or self.new_blob is not None
        ):
            raise ReviewInputError("human deleted file evidence is invalid")
        if self.change in {"modified", "renamed"} and (
            self.old_blob is None or self.new_blob is None
        ):
            raise ReviewInputError("human changed file evidence is missing")
        if self.change == "renamed":
            if not isinstance(self.old_path, str) or self.old_path == self.path:
                raise ReviewInputError("human renamed file evidence is invalid")
            validate_repository_path(
                self.old_path, label="human previous path", allow_glob_chars=True
            )
        elif self.old_path is not None:
            raise ReviewInputError("human file previous path is unexpected")

    @property
    def paths(self) -> tuple[str, ...]:
        return (self.old_path, self.path) if self.old_path is not None else (self.path,)

    @property
    def file_id(self) -> str:
        return evidence_digest(
            {"domain": "reviewsensei:unsupported-file:v1", **self.to_dict()}
        )

    def to_dict(self) -> dict[str, object]:
        return dict(
            path=self.path,
            change=self.change,
            old_blob=self.old_blob,
            new_blob=self.new_blob,
            old_path=self.old_path,
            old_mode=self.old_mode,
            new_mode=self.new_mode,
        )

    @classmethod
    def from_dict(cls, value: object) -> UnsupportedFile:
        x = _closed(
            value,
            {
                "path",
                "change",
                "old_blob",
                "new_blob",
                "old_path",
                "old_mode",
                "new_mode",
            },
        )
        if not isinstance(x["path"], str) or not isinstance(x["change"], str):
            raise ReviewInputError("human file fields are invalid")
        optional = {
            k: x[k]
            for k in ("old_blob", "new_blob", "old_path", "old_mode", "new_mode")
        }
        if any(v is not None and not isinstance(v, str) for v in optional.values()):
            raise ReviewInputError("human file fields are invalid")
        return cls(x["path"], x["change"], **optional)  # type: ignore[arg-type]


@dataclass(frozen=True)
class FileReviewRequest:
    repository: str
    repository_id: int
    pull_request: int
    base_sha: str
    head_sha: str
    review_id: int
    result_digest: str
    files: tuple[UnsupportedFile, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.repository, str)
            or _REPOSITORY.fullmatch(self.repository) is None
            or not all(
                positive(v)
                for v in (self.repository_id, self.pull_request, self.review_id)
            )
            or not sha(self.base_sha)
            or not sha(self.head_sha)
            or not digest(self.result_digest)
        ):
            raise ReviewInputError("human file review snapshot is invalid")
        if any(part in {".", ".."} for part in self.repository.split("/")):
            raise ReviewInputError("human file review repository is invalid")
        if (
            not isinstance(self.files, tuple)
            or not 1 <= len(self.files) <= MAX_FILES
            or any(not isinstance(item, UnsupportedFile) for item in self.files)
            or len({item.file_id for item in self.files}) != len(self.files)
        ):
            raise ReviewInputError(
                "human file inventory is invalid or exceeds the bound"
            )
        paths = [path for item in self.files for path in item.paths]
        if len(set(paths)) != len(paths):
            raise ReviewInputError("human file inventory paths conflict")
        if len(canonical(self.to_dict()).encode("utf-8")) > MAX_DOCUMENT_BYTES:
            raise ReviewInputError("human file inventory exceeds the document bound")

    @property
    def digest(self) -> str:
        return evidence_digest(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return dict(
            schema_version="human-file-review-v1",
            repository=self.repository,
            repository_id=self.repository_id,
            pull_request=self.pull_request,
            base_sha=self.base_sha,
            head_sha=self.head_sha,
            review_id=self.review_id,
            result_digest=self.result_digest,
            files=[item.to_dict() for item in self.files],
        )

    @classmethod
    def from_dict(cls, value: object) -> FileReviewRequest:
        x = _closed(
            value,
            {
                "schema_version",
                "repository",
                "repository_id",
                "pull_request",
                "base_sha",
                "head_sha",
                "review_id",
                "result_digest",
                "files",
            },
        )
        if x["schema_version"] != "human-file-review-v1" or not isinstance(
            x["files"], list
        ):
            raise ReviewInputError("human file request schema is invalid")
        return cls(
            repository=cast(str, x["repository"]),
            repository_id=cast(int, x["repository_id"]),
            pull_request=cast(int, x["pull_request"]),
            base_sha=cast(str, x["base_sha"]),
            head_sha=cast(str, x["head_sha"]),
            review_id=cast(int, x["review_id"]),
            result_digest=cast(str, x["result_digest"]),
            files=tuple(UnsupportedFile.from_dict(item) for item in x["files"]),
        )

    def render(self) -> str:
        lines = [
            "Human review requested for unsupported binary files (not AI-reviewed).",
            f"Snapshot: base `{self.base_sha}`, head `{self.head_sha}`.",
            "A maintainer with write access may confirm only files they personally reviewed.",
        ]
        for item in self.files:
            # JSON quoting keeps file names from introducing mentions/Markdown instructions.
            lines.append(
                f"- {display_path(item.path)} ({item.change}): file ID `{item.file_id}`; "
                f"old blob `{item.old_blob or 'absent'}`, new blob `{item.new_blob or 'absent'}`"
                f"; modes `{item.old_mode or 'absent'}` → `{item.new_mode or 'absent'}`"
                + (
                    f"; previous path {display_path(item.old_path)}"
                    if item.old_path
                    else ""
                )
            )
        lines.extend(
            [
                "",
                "Reply with the request digest and selected full file IDs:",
                f"`@sensei media-reviewed {self.digest} <file-id> [<file-id> ...]`",
                "This confirms human file review only. Text findings and other approval checks remain open.",
                "Any snapshot or review-result change requires a fresh confirmation.",
            ]
        )
        return "\n".join(lines)


def parse_confirmation(
    body: object, request: FileReviewRequest
) -> tuple[str, ...] | None:
    """A whole literal command only; prose, code fences, URLs and wildcards refuse."""
    if not isinstance(body, str):
        return None
    try:
        if len(body.encode("utf-8", errors="strict")) > MAX_SOURCE_BYTES:
            return None
    except UnicodeError:
        return None
    words = re.split(r"[ \t\r\n]+", body.strip(" \t\r\n"))
    if (
        len(words) < 4
        or words[0] not in {"@sensei", "@reviewsensei"}
        or words[1] != "media-reviewed"
        or words[2] != request.digest
    ):
        return None
    ids = tuple(words[3:])
    allowed = {item.file_id for item in request.files}
    if (
        len(ids) > MAX_FILES
        or len(set(ids)) != len(ids)
        or any(key not in allowed for key in ids)
    ):
        return None
    return ids


def coverage_only_binary(coverage: CoverageManifest) -> bool:
    """Only unsupported binary coverage can be satisfied by this feature."""
    return (
        coverage.enumeration_complete
        and bool(coverage.files)
        and any(x.outcome == "unsupported" for x in coverage.files)
        and all(
            x.outcome == "reviewed"
            or (x.outcome == "unsupported" and x.reason == "binary")
            for x in coverage.files
        )
        and all(x.outcome == "reviewed" for x in coverage.hunks)
    )


def validate_inventory(request: FileReviewRequest, coverage: CoverageManifest) -> None:
    expected = {
        x.path
        for x in coverage.files
        if x.outcome == "unsupported" and x.reason == "binary"
    }
    actual = {path for item in request.files for path in item.paths}
    if not coverage.enumeration_complete or not expected or expected != actual:
        raise ReviewInputError(
            "human file inventory must cover exactly all unsupported binary paths"
        )
    if any(x.path in actual and x.outcome != "reviewed" for x in coverage.hunks):
        raise ReviewInputError(
            "human file confirmation cannot replace text hunk review"
        )


@dataclass(frozen=True)
class FileReviewReceipt:
    """Metadata-only audit of one explicit source; raw records confer no authority."""

    request_digest: str
    request_comment_id: int
    source_comment_id: int
    source_updated_at: str
    source_sha256: str
    actor_id: int
    actor: str
    selected_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not digest(self.request_digest)
            or not digest(self.source_sha256)
            or not all(
                positive(v)
                for v in (
                    self.request_comment_id,
                    self.source_comment_id,
                    self.actor_id,
                )
            )
            or not isinstance(self.actor, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", self.actor) is None
            or not isinstance(self.source_updated_at, str)
            or re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", self.source_updated_at)
            is None
            or not isinstance(self.selected_ids, tuple)
            or not 1 <= len(self.selected_ids) <= MAX_FILES
            or not all(digest(v) for v in self.selected_ids)
            or len(set(self.selected_ids)) != len(self.selected_ids)
        ):
            raise ReviewInputError("human file receipt is invalid")
        try:
            datetime.strptime(self.source_updated_at, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            raise ReviewInputError("human file source timestamp is invalid") from None

    def to_dict(self) -> dict[str, object]:
        return dict(
            schema_version="human-file-receipt-v1",
            request_digest=self.request_digest,
            request_comment_id=self.request_comment_id,
            source_comment_id=self.source_comment_id,
            source_updated_at=self.source_updated_at,
            source_sha256=self.source_sha256,
            actor_id=self.actor_id,
            actor=self.actor,
            selected_ids=list(self.selected_ids),
        )

    @classmethod
    def from_dict(cls, value: object) -> FileReviewReceipt:
        x = dict(
            _closed(
                value,
                {
                    "schema_version",
                    "request_digest",
                    "request_comment_id",
                    "source_comment_id",
                    "source_updated_at",
                    "source_sha256",
                    "actor_id",
                    "actor",
                    "selected_ids",
                },
            )
        )
        if x.pop("schema_version") != "human-file-receipt-v1" or not isinstance(
            x["selected_ids"], list
        ):
            raise ReviewInputError("human file receipt schema is invalid")
        x["selected_ids"] = tuple(x["selected_ids"])
        return cls(**x)  # type: ignore[arg-type]
