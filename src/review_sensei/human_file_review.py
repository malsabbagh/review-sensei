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

from .coverage import CoverageManifest, FileCoverage, HunkCoverage
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
            "Human review requested for files ReviewSensei did not assess "
            "(unsupported binary files, or generated files excluded by policy; "
            "not AI-reviewed).",
            f"Snapshot: base `{self.base_sha}`, head `{self.head_sha}`.",
            "A maintainer or admin who reviewed these files may approve them in one reply.",
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
                "Reply with this exact sentence to approve every file in this request:",
                "`@reviewsensei I reviewed the media files and I approve`",
                "A maintainer or admin may also name up to 10 findings in one comment, "
                "separated by spaces or commas, for example "
                "`@reviewsensei override RS-ABCDEF RS-123456 acceptable risk`.",
                "This confirms human file review only. Text findings and other approval checks remain open.",
                "Any snapshot or review-result change requires a fresh confirmation.",
            ]
        )
        return "\n".join(lines)


@dataclass(frozen=True)
class FileReviewRequestGroup:
    """One exact inventory composed of unchanged, bounded v1 request parts.

    The group is one request identity, never separate authority for a subset.
    Its complete canonical document keeps the existing document byte ceiling.
    """

    parts: tuple[FileReviewRequest, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.parts, tuple)
            or not 2 <= len(self.parts) <= MAX_FILES
            or any(not isinstance(part, FileReviewRequest) for part in self.parts)
        ):
            raise ReviewInputError("human file request group is invalid")
        first = self.parts[0]
        for part in self.parts:
            if any(
                getattr(part, field) != getattr(first, field)
                for field in (
                    "repository",
                    "repository_id",
                    "pull_request",
                    "base_sha",
                    "head_sha",
                    "review_id",
                    "result_digest",
                )
            ):
                raise ReviewInputError("human file request group snapshots conflict")
        files = self.files
        if (
            any(len(part.files) != MAX_FILES for part in self.parts[:-1])
            or files != tuple(sorted(files, key=lambda item: item.path))
            or len({item.file_id for item in files}) != len(files)
            or len({path for item in files for path in item.paths})
            != sum(len(item.paths) for item in files)
        ):
            raise ReviewInputError("human file request group parts conflict")
        if len(canonical(self.to_dict()).encode("utf-8")) > MAX_DOCUMENT_BYTES:
            raise ReviewInputError(
                "human file request group exceeds the document bound"
            )

    @property
    def files(self) -> tuple[UnsupportedFile, ...]:
        return tuple(item for part in self.parts for item in part.files)

    @property
    def repository(self) -> str:
        return self.parts[0].repository

    @property
    def repository_id(self) -> int:
        return self.parts[0].repository_id

    @property
    def pull_request(self) -> int:
        return self.parts[0].pull_request

    @property
    def base_sha(self) -> str:
        return self.parts[0].base_sha

    @property
    def head_sha(self) -> str:
        return self.parts[0].head_sha

    @property
    def review_id(self) -> int:
        return self.parts[0].review_id

    @property
    def result_digest(self) -> str:
        return self.parts[0].result_digest

    @property
    def digest(self) -> str:
        return evidence_digest(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "human-file-review-group-v1",
            "parts": [part.to_dict() for part in self.parts],
        }

    @classmethod
    def from_dict(cls, value: object) -> FileReviewRequestGroup:
        fields = _closed(value, {"schema_version", "parts"})
        if fields["schema_version"] != "human-file-review-group-v1" or not isinstance(
            fields["parts"], list
        ):
            raise ReviewInputError("human file request group schema is invalid")
        if not 2 <= len(fields["parts"]) <= MAX_FILES:
            raise ReviewInputError("human file request group exceeds its part bound")
        return cls(tuple(FileReviewRequest.from_dict(part) for part in fields["parts"]))

    def render(self) -> str:
        header = (
            f"Human review requested for one complete inventory in {len(self.parts)} "
            f"bounded parts ({len(self.files)} file changes); not AI-reviewed.\n"
            f"Snapshot: base `{self.base_sha}`, head `{self.head_sha}`.\n"
            f"Request digest: `{self.digest}`.\n"
        )
        listing = "\n".join(
            f"- {display_path(item.path)} ({item.change}): file ID `{item.file_id}`; "
            f"old blob `{item.old_blob or 'absent'}`, new blob `{item.new_blob or 'absent'}`; "
            f"modes `{item.old_mode or 'absent'}` → `{item.new_mode or 'absent'}`"
            + (
                f"; previous path {display_path(item.old_path)}"
                if item.old_path
                else ""
            )
            for item in self.files
        )
        return (
            header
            + listing
            + (
                "\n\nA maintainer or admin who reviewed every file in this complete "
                "group may reply with this exact sentence:\n"
                "`@reviewsensei I reviewed the media files and I approve`\n"
                "The sentence selects every file in the entire group. "
                "Text findings and other approval checks remain open. "
                "Any snapshot or review-result change requires a fresh confirmation."
            )
        )


FileReviewRequestValue = FileReviewRequest | FileReviewRequestGroup


def build_request_group(
    repository: str,
    repository_id: int,
    pull_request: int,
    base_sha: str,
    head_sha: str,
    review_id: int,
    result_digest: str,
    files: tuple[UnsupportedFile, ...],
) -> FileReviewRequestValue:
    """Preserve v1 identity, or partition a larger complete inventory."""
    if not isinstance(files, tuple) or any(
        not isinstance(item, UnsupportedFile) for item in files
    ):
        raise ReviewInputError("human file inventory is invalid")
    context = (
        repository,
        repository_id,
        pull_request,
        base_sha,
        head_sha,
        review_id,
        result_digest,
    )
    if len(files) <= MAX_FILES:
        return FileReviewRequest(*context, files)
    if len(files) > MAX_FILES * MAX_FILES:
        raise ReviewInputError("human file request group exceeds its part bound")
    ordered = tuple(sorted(files, key=lambda item: item.path))
    return FileReviewRequestGroup(
        tuple(
            FileReviewRequest(*context, ordered[start : start + MAX_FILES])
            for start in range(0, len(ordered), MAX_FILES)
        )
    )


def parse_request(value: object) -> FileReviewRequestValue:
    if (
        isinstance(value, Mapping)
        and value.get("schema_version") == "human-file-review-group-v1"
    ):
        return FileReviewRequestGroup.from_dict(value)
    return FileReviewRequest.from_dict(value)


def is_blanket_media_approval(body: object) -> bool:
    """The whole comment approves every file on the current request."""

    if not isinstance(body, str):
        return False
    match = re.fullmatch(
        r"(?:@sensei|(?ai:@reviewsensei))[ \t\r\n]+(.*)",
        body.strip(" \t\r\n"),
    )
    if match is None:
        return False
    phrase = re.sub(r"[ \t\r\n]+", " ", match.group(1)).strip(" ")
    return phrase.casefold() == "i reviewed the media files and i approve"


def parse_confirmation(
    body: object, request: FileReviewRequestValue
) -> tuple[str, ...] | None:
    """A whole literal command only; prose, code fences, URLs and wildcards refuse."""
    if not isinstance(body, str):
        return None
    try:
        if len(body.encode("utf-8", errors="strict")) > MAX_SOURCE_BYTES:
            return None
    except UnicodeError:
        return None
    if is_blanket_media_approval(body):
        if not request.files:
            return None
        return tuple(item.file_id for item in request.files)
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


def is_human_confirmable_file(entry: FileCoverage) -> bool:
    return (entry.outcome == "unsupported" and entry.reason == "binary") or (
        entry.outcome == "excluded-by-policy" and entry.reason == "generated"
    )


def is_human_confirmable_hunk(entry: HunkCoverage) -> bool:
    return entry.outcome == "excluded-by-policy" and entry.reason == "generated"


def human_confirmation_reason(coverage: CoverageManifest) -> str:
    """Name the files a maintainer can confirm and the existing approval command."""

    binary = [
        display_path(entry.path)
        for entry in coverage.files
        if entry.outcome == "unsupported" and entry.reason == "binary"
    ]
    generated = [
        display_path(entry.path)
        for entry in coverage.files
        if entry.outcome == "excluded-by-policy" and entry.reason == "generated"
    ]
    groups: list[str] = []
    if binary:
        groups.append("unsupported (binary) " + ", ".join(binary))
    if generated:
        groups.append("excluded-by-policy (generated) " + ", ".join(generated))
    listing = "; ".join(groups) if groups else "the files in this review"
    return (
        "human confirmation is still required for "
        + listing
        + ". A maintainer or admin who reviewed these files replies "
        "`@reviewsensei I reviewed the media files and I approve`"
    )


def coverage_only_binary(coverage: CoverageManifest) -> bool:
    """Return whether human file review can satisfy the remaining coverage.

    Unsupported binary files and generated files excluded by policy qualify.
    Reviewable text hunks do not.
    """
    if not coverage.enumeration_complete or not coverage.files:
        return False
    confirmable_paths = {
        entry.path for entry in coverage.files if is_human_confirmable_file(entry)
    }
    excluded_paths = {
        entry.path
        for entry in coverage.files
        if entry.outcome == "excluded-by-policy" and entry.reason == "generated"
    }
    return (
        bool(confirmable_paths)
        and all(
            entry.outcome == "reviewed" or is_human_confirmable_file(entry)
            for entry in coverage.files
        )
        and all(
            entry.outcome == "reviewed"
            or (entry.path in excluded_paths and is_human_confirmable_hunk(entry))
            for entry in coverage.hunks
        )
    )


def validate_inventory(
    request: FileReviewRequestValue, coverage: CoverageManifest
) -> None:
    expected = {
        entry.path for entry in coverage.files if is_human_confirmable_file(entry)
    }
    excluded_paths = {
        entry.path
        for entry in coverage.files
        if entry.outcome == "excluded-by-policy" and entry.reason == "generated"
    }
    actual = {path for item in request.files for path in item.paths}
    if not coverage.enumeration_complete or not expected or expected != actual:
        raise ReviewInputError(
            "human file inventory must cover exactly all human-confirmable paths"
        )
    if any(
        entry.outcome != "reviewed"
        and not (entry.path in excluded_paths and is_human_confirmable_hunk(entry))
        for entry in coverage.hunks
    ):
        raise ReviewInputError(
            "human file confirmation cannot replace reviewable text hunk review"
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


@dataclass(frozen=True)
class FileReviewReceiptGroup:
    """One human source event bound to one complete grouped request identity."""

    request_digest: str
    parts: tuple[FileReviewReceipt, ...]

    def __post_init__(self) -> None:
        if (
            not digest(self.request_digest)
            or not isinstance(self.parts, tuple)
            or not 1 <= len(self.parts) <= MAX_FILES
            or any(not isinstance(part, FileReviewReceipt) for part in self.parts)
        ):
            raise ReviewInputError("human file receipt group is invalid")
        first = self.parts[0]
        if (
            self.parts
            != tuple(sorted(self.parts, key=lambda part: part.request_digest))
            or len({part.request_digest for part in self.parts}) != len(self.parts)
            or len(set(self.selected_ids)) != len(self.selected_ids)
            or any(
                getattr(part, field) != getattr(first, field)
                for part in self.parts
                for field in (
                    "request_comment_id",
                    "source_comment_id",
                    "source_updated_at",
                    "source_sha256",
                    "actor_id",
                    "actor",
                )
            )
        ):
            raise ReviewInputError("human file receipt group parts conflict")
        if len(canonical(self.to_dict()).encode("utf-8")) > MAX_DOCUMENT_BYTES:
            raise ReviewInputError(
                "human file receipt group exceeds the document bound"
            )

    @property
    def request_comment_id(self) -> int:
        return self.parts[0].request_comment_id

    @property
    def source_comment_id(self) -> int:
        return self.parts[0].source_comment_id

    @property
    def source_updated_at(self) -> str:
        return self.parts[0].source_updated_at

    @property
    def source_sha256(self) -> str:
        return self.parts[0].source_sha256

    @property
    def actor_id(self) -> int:
        return self.parts[0].actor_id

    @property
    def actor(self) -> str:
        return self.parts[0].actor

    @property
    def selected_ids(self) -> tuple[str, ...]:
        return tuple(
            identifier for part in self.parts for identifier in part.selected_ids
        )

    @property
    def digest(self) -> str:
        return evidence_digest(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "human-file-receipt-group-v1",
            "request_digest": self.request_digest,
            "parts": [part.to_dict() for part in self.parts],
        }

    @classmethod
    def from_dict(cls, value: object) -> FileReviewReceiptGroup:
        fields = _closed(value, {"schema_version", "request_digest", "parts"})
        if (
            fields["schema_version"] != "human-file-receipt-group-v1"
            or not isinstance(fields["request_digest"], str)
            or not isinstance(fields["parts"], list)
        ):
            raise ReviewInputError("human file receipt group schema is invalid")
        if not 1 <= len(fields["parts"]) <= MAX_FILES:
            raise ReviewInputError("human file receipt group exceeds its part bound")
        return cls(
            fields["request_digest"],
            tuple(FileReviewReceipt.from_dict(part) for part in fields["parts"]),
        )


FileReviewReceiptValue = FileReviewReceipt | FileReviewReceiptGroup


def build_receipt(
    request: FileReviewRequestValue,
    *,
    request_comment_id: int,
    source_comment_id: int,
    source_updated_at: str,
    source_sha256: str,
    actor_id: int,
    actor: str,
    selected_ids: tuple[str, ...],
) -> FileReviewReceiptValue:
    """Bind a source to exact part identities without enlarging v1 receipts."""
    if (
        not isinstance(request, (FileReviewRequest, FileReviewRequestGroup))
        or not isinstance(selected_ids, tuple)
        or not selected_ids
        or not all(digest(identifier) for identifier in selected_ids)
        or len(set(selected_ids)) != len(selected_ids)
        or any(
            identifier not in {item.file_id for item in request.files}
            for identifier in selected_ids
        )
    ):
        raise ReviewInputError("human file receipt selection is invalid")
    context = (
        request_comment_id,
        source_comment_id,
        source_updated_at,
        source_sha256,
        actor_id,
        actor,
    )
    if isinstance(request, FileReviewRequest):
        return FileReviewReceipt(request.digest, *context, selected_ids)
    parts = tuple(
        FileReviewReceipt(
            part.digest,
            *context,
            tuple(item.file_id for item in part.files if item.file_id in selected_ids),
        )
        for part in request.parts
        if any(item.file_id in selected_ids for item in part.files)
    )
    return FileReviewReceiptGroup(
        request.digest, tuple(sorted(parts, key=lambda part: part.request_digest))
    )


def parse_receipt(value: object) -> FileReviewReceiptValue:
    if (
        isinstance(value, Mapping)
        and value.get("schema_version") == "human-file-receipt-group-v1"
    ):
        return FileReviewReceiptGroup.from_dict(value)
    return FileReviewReceipt.from_dict(value)
