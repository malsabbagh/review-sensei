"""Closed citation for one regular file unchanged between two snapshots.

``kind`` is ``unchanged-source-v1``. This record is not a diff hunk. An absent
diff is not evidence and is never consulted. Parsing and ``bind_snapshot`` are
pure: they do not fetch URLs, open paths, follow links, or infer that a missing
hunk means the file was unchanged.

Permitted citations name one repository path, regular mode ``100644`` or
``100755``, and the same Git blob object at base and head. The cited line
range, its SHA-256, and its byte length must match caller-supplied snapshot
bytes. Symlinks, submodules, secret-like paths, excluded paths, foreign
objects, truncated ranges, and the same blob cited from another path refuse.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping, cast

from .context import _is_secret_like
from .errors import ReviewInputError
from .planning import is_generated_path
from .validation import validate_repository_path

KIND = "unchanged-source-v1"
MAX_CONTENT_BYTES = 65_536
_REGULAR_MODES = frozenset({"100644", "100755"})
_FIELDS = frozenset(
    {
        "kind",
        "repository",
        "pull_request",
        "base_sha",
        "head_sha",
        "path",
        "mode",
        "base_blob_sha",
        "head_blob_sha",
        "range_start",
        "range_end",
        "content_sha256",
        "content_bytes",
    }
)
_GIT_SHA = re.compile(r"[a-f0-9]{40}")
_SHA256 = re.compile(r"[a-f0-9]{64}")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_MAX_REPOSITORY_BYTES = 256
_MAX_PULL_REQUEST = 2_147_483_647
# Directory names from the generated-path policy. Nested copies refuse even
# when the path does not start with the prefix.
_EXCLUDED_DIRECTORY_NAMES = frozenset(
    {"dist", "generated", "node_modules", "vendor"}
)
_PATH_BINDING_PREFIX = b"unchanged-source-v1\0"


def git_blob_sha(data: bytes) -> str:
    """Return the Git blob object id for exact file bytes."""

    if type(data) is not bytes or len(data) > MAX_CONTENT_BYTES:
        raise ReviewInputError("unchanged-source blob exceeds the closed byte ceiling")
    header = b"blob %d\0" % len(data)
    return hashlib.sha1(header + data, usedforsecurity=False).hexdigest()


def snapshot_path_key(snapshot_sha: str, path: str) -> str:
    """Address one snapshot path.

    The result is a SHA-256 binding of the commit and the repository path.
    It is not a Git object id. The same blob bytes at another path have a
    different address and are not evidence for this citation.
    """

    if type(snapshot_sha) is not str or _GIT_SHA.fullmatch(snapshot_sha) is None:
        raise ReviewInputError("unchanged-source snapshot sha is invalid")
    if type(path) is not str:
        raise ReviewInputError("unchanged-source path is invalid")
    return hashlib.sha256(
        _PATH_BINDING_PREFIX
        + snapshot_sha.encode("ascii")
        + b"\0"
        + path.encode("utf-8")
    ).hexdigest()


def _exact_git_sha(value: object, message: str) -> str:
    if type(value) is not str or _GIT_SHA.fullmatch(value) is None:
        raise ReviewInputError(message)
    return value


def _exact_sha256(value: object, message: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ReviewInputError(message)
    return value


def _exact_int(value: object, minimum: int, maximum: int, message: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ReviewInputError(message)
    return value


def _secret_like(path: str) -> bool:
    # PurePosixPath uses the path text only. It does not resolve or follow links.
    return bool(_is_secret_like(cast(Any, PurePosixPath(path))))


def _excluded_path(path: str) -> bool:
    if is_generated_path(path):
        return True
    return any(
        segment in _EXCLUDED_DIRECTORY_NAMES for segment in path.lower().split("/")
    )


def _lines(blob: bytes) -> list[bytes]:
    if not blob:
        return []
    pieces = blob.split(b"\n")
    if blob.endswith(b"\n"):
        return [piece + b"\n" for piece in pieces[:-1]]
    lines = [piece + b"\n" for piece in pieces[:-1]]
    lines.append(pieces[-1])
    return lines


@dataclass(frozen=True)
class UnchangedSourceCitation:
    """One closed unchanged-source citation. Fields are exact; extras refuse."""

    kind: str
    repository: str
    pull_request: int
    base_sha: str
    head_sha: str
    path: str
    mode: str
    base_blob_sha: str
    head_blob_sha: str
    range_start: int
    range_end: int
    content_sha256: str
    content_bytes: int

    def __post_init__(self) -> None:
        _reject_invalid(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "repository": self.repository,
            "pull_request": self.pull_request,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "path": self.path,
            "mode": self.mode,
            "base_blob_sha": self.base_blob_sha,
            "head_blob_sha": self.head_blob_sha,
            "range_start": self.range_start,
            "range_end": self.range_end,
            "content_sha256": self.content_sha256,
            "content_bytes": self.content_bytes,
        }


def _reject_invalid(record: UnchangedSourceCitation) -> None:
    if type(record.kind) is not str or record.kind != KIND:
        raise ReviewInputError("unchanged-source citation kind is invalid")
    if (
        type(record.repository) is not str
        or _REPOSITORY.fullmatch(record.repository) is None
        or len(record.repository.encode("utf-8")) > _MAX_REPOSITORY_BYTES
    ):
        raise ReviewInputError("unchanged-source repository is invalid")
    _exact_int(
        record.pull_request,
        1,
        _MAX_PULL_REQUEST,
        "unchanged-source pull request is invalid",
    )
    _exact_git_sha(record.base_sha, "unchanged-source snapshot sha is invalid")
    _exact_git_sha(record.head_sha, "unchanged-source snapshot sha is invalid")
    if type(record.path) is not str:
        raise ReviewInputError("unchanged-source path is invalid")
    path = validate_repository_path(record.path, label="unchanged-source path")
    if _secret_like(path):
        raise ReviewInputError("unchanged-source path is secret-like")
    if _excluded_path(path):
        raise ReviewInputError("unchanged-source path is excluded")
    if type(record.mode) is not str or record.mode not in _REGULAR_MODES:
        raise ReviewInputError("unchanged-source mode is invalid")
    base_blob = _exact_git_sha(
        record.base_blob_sha, "unchanged-source blob sha is invalid"
    )
    head_blob = _exact_git_sha(
        record.head_blob_sha, "unchanged-source blob sha is invalid"
    )
    if base_blob != head_blob:
        raise ReviewInputError("unchanged-source content changed")
    start = _exact_int(
        record.range_start, 1, MAX_CONTENT_BYTES, "unchanged-source range is invalid"
    )
    end = _exact_int(
        record.range_end, 1, MAX_CONTENT_BYTES, "unchanged-source range is invalid"
    )
    if start > end:
        raise ReviewInputError("unchanged-source range is invalid")
    _exact_sha256(record.content_sha256, "unchanged-source content sha is invalid")
    content_bytes = _exact_int(
        record.content_bytes,
        0,
        MAX_CONTENT_BYTES,
        "unchanged-source content length is invalid",
    )
    # Every cited line occupies at least one byte. A shorter claim is truncated.
    if end - start + 1 > content_bytes:
        raise ReviewInputError("unchanged-source range is truncated")


def parse_unchanged_source(value: object) -> UnchangedSourceCitation:
    """Parse one closed citation.

    Only a builtin ``dict`` is accepted. Subclasses and other mappings are
    accessors and are rejected before any item is read, so a value cannot
    fetch a URL or open a path while it is being validated.
    """

    if type(value) is not dict:
        raise ReviewInputError("unchanged-source citation must be a closed object")
    if set(value) != _FIELDS:
        raise ReviewInputError("unchanged-source citation fields are closed")
    return UnchangedSourceCitation(
        kind=value["kind"],
        repository=value["repository"],
        pull_request=value["pull_request"],
        base_sha=value["base_sha"],
        head_sha=value["head_sha"],
        path=value["path"],
        mode=value["mode"],
        base_blob_sha=value["base_blob_sha"],
        head_blob_sha=value["head_blob_sha"],
        range_start=value["range_start"],
        range_end=value["range_end"],
        content_sha256=value["content_sha256"],
        content_bytes=value["content_bytes"],
    )


def _mapped_blob(blobs: dict[str, object], key: str) -> bytes | None:
    if key not in blobs:
        return None
    value = blobs[key]
    if type(value) is not bytes:
        raise ReviewInputError("unchanged-source snapshot blob is invalid")
    if len(value) > MAX_CONTENT_BYTES:
        raise ReviewInputError("unchanged-source blob exceeds the closed byte ceiling")
    return value


def bind_snapshot(
    record: UnchangedSourceCitation, blobs: Mapping[str, bytes]
) -> bytes:
    """Return the cited range after both snapshots resolve to that blob.

    ``blobs`` maps a Git blob SHA, and each snapshot path address from
    ``snapshot_path_key``, to the exact file bytes. Both blob SHA lookups and
    both snapshot path lookups must return the same bytes. Those bytes must be
    the Git object named by the blob SHA, and the inclusive 1-based line range
    must hash to ``content_sha256`` with length ``content_bytes``.

    A missing blob refuses. Bytes present only under a different path refuse.
    Nothing is read from the network or from an absent diff.
    """

    if type(record) is not UnchangedSourceCitation or type(blobs) is not dict:
        raise ReviewInputError("unchanged-source snapshot mapping is invalid")
    snapshot = cast(dict[str, object], blobs)
    _reject_invalid(record)
    base_blob = _mapped_blob(snapshot, record.base_blob_sha)
    head_blob = _mapped_blob(snapshot, record.head_blob_sha)
    if base_blob is None or head_blob is None:
        raise ReviewInputError("unchanged-source blob is missing")
    if base_blob != head_blob:
        raise ReviewInputError("unchanged-source snapshots differ")
    if git_blob_sha(base_blob) != record.base_blob_sha:
        raise ReviewInputError("unchanged-source blob sha is foreign")
    base_at_path = _mapped_blob(
        snapshot, snapshot_path_key(record.base_sha, record.path)
    )
    head_at_path = _mapped_blob(
        snapshot, snapshot_path_key(record.head_sha, record.path)
    )
    if base_at_path is None or head_at_path is None:
        raise ReviewInputError(
            "unchanged-source path is not bound to the snapshot blob"
        )
    if base_at_path != head_at_path:
        raise ReviewInputError("unchanged-source snapshots differ")
    if base_at_path != base_blob:
        raise ReviewInputError(
            "unchanged-source path is not bound to the snapshot blob"
        )
    lines = _lines(base_blob)
    if record.range_end > len(lines):
        raise ReviewInputError("unchanged-source range is truncated")
    cited = b"".join(lines[record.range_start - 1 : record.range_end])
    if len(cited) != record.content_bytes:
        raise ReviewInputError("unchanged-source range is truncated")
    if hashlib.sha256(cited).hexdigest() != record.content_sha256:
        raise ReviewInputError("unchanged-source content sha does not match")
    return cited
