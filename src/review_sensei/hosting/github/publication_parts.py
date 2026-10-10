"""Deterministic complete prose parts; adapters activate authority separately.

This module performs no writes and grants no approval. The coordinator must
wire the shared authenticated partition reader before enabling host writers.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Sequence

from ...context import finding_lifecycle_for_comment
from ...errors import ReviewInputError
from ...human_assessment import finding_instance_fingerprint
from ...models import ReviewComment
from ...presentation import (
    assign_finding_identifiers,
    build_finding_view,
    render_finding,
)
from ...validation import validate_bounded_text, validate_repository_path

PROSE_PART_VERSION = "1"
MAX_PROSE_PART_BYTES = 65_536
MAX_PROSE_PART_PATHS = 64
MAX_PROSE_PARTS = 32
MAX_PROSE_TOTAL_BYTES = 1_048_576


@dataclass(frozen=True)
class FindingProsePart:
    index: int
    count: int
    body: str
    instances: tuple[str, ...]
    paths: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            type(self.index) is not int
            or type(self.count) is not int
            or not 0 <= self.index < self.count <= MAX_PROSE_PARTS
            or not isinstance(self.instances, tuple)
            or not 1 <= len(self.instances) <= 250
            or len(set(self.instances)) != len(self.instances)
            or any(
                not isinstance(item, str)
                or len(item) != 64
                or any(character not in "0123456789abcdef" for character in item)
                for item in self.instances
            )
            or not isinstance(self.paths, tuple)
            or not 1 <= len(self.paths) <= MAX_PROSE_PART_PATHS
            or len(set(self.paths)) != len(self.paths)
        ):
            raise ReviewInputError("finding prose part metadata is invalid")
        validate_bounded_text(
            self.body,
            MAX_PROSE_PART_BYTES,
            label="finding prose part",
            allow_empty=False,
        )
        if "<!-- reviewsensei:" in self.body:
            raise ReviewInputError("staged prose cannot carry authority markers")
        for path in self.paths:
            validate_repository_path(
                path, label="finding prose path", allow_glob_chars=True
            )

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()

    @property
    def byte_length(self) -> int:
        return len(self.body.encode("utf-8"))


def _header(index: int, count: int, head_sha: str) -> str:
    return (
        f"## ReviewSensei finding explanations — part {index + 1} of {count}\n\n"
        f"Reviewed head: `{head_sha}`\n\n"
        "See the main review for links to all parts, coverage, remaining human "
        "assessments and approval status.\n\n"
    )


def prepare_finding_prose_parts(
    comments: Sequence[ReviewComment], *, head_sha: str
) -> tuple[FindingProsePart, ...]:
    """Pack complete explanations and paths under literal UTF-8 piece limits.

    Ordering is instance-based and stable across provider/stage order. Only
    exact validated duplicates coalesce. Every retained body is rendered in
    full, even when a lifecycle concern was present in an earlier review.
    """
    if (
        not isinstance(head_sha, str)
        or len(head_sha) != 40
        or any(character not in "0123456789abcdef" for character in head_sha)
        or len(comments) > 250
    ):
        raise ReviewInputError("finding prose publication identity is invalid")
    instances: dict[str, ReviewComment] = {}
    for comment in comments:
        instance = finding_instance_fingerprint(comment)
        previous = instances.get(instance)
        if previous is not None and (
            previous.path,
            previous.line,
            previous.side,
            previous.body,
        ) != (comment.path, comment.line, comment.side, comment.body):
            raise ReviewInputError("finding prose instance identity conflicts")
        # Runtime admission is authoritative. Conflicting admission for the
        # same closed comment is not an exact publication duplicate.
        if previous is not None and (
            previous.blocks_approval != comment.blocks_approval
            or previous.needs_human != comment.needs_human
        ):
            raise ReviewInputError("finding prose classification conflicts")
        if previous is None:
            instances[instance] = comment
    identifiers = assign_finding_identifiers(instances)
    # Reserve the longest supported navigation header before any host write.
    header_reserve = len(
        _header(MAX_PROSE_PARTS - 1, MAX_PROSE_PARTS, head_sha).encode()
    )
    groups: list[list[tuple[str, str, str]]] = []
    group: list[tuple[str, str, str]] = []
    paths: set[str] = set()
    group_bytes = header_reserve
    for instance, comment in sorted(instances.items()):
        view = build_finding_view(
            comment,
            fingerprint=finding_lifecycle_for_comment(comment).fingerprint,
            identifier=identifiers[instance],
            placement="body",
        )
        explanation = render_finding(view)
        size = len(explanation.encode("utf-8"))
        if header_reserve + size > MAX_PROSE_PART_BYTES:
            raise ReviewInputError("one complete finding exceeds prose part capacity")
        if group and (
            group_bytes + 2 + size > MAX_PROSE_PART_BYTES
            or len(paths | {comment.path}) > MAX_PROSE_PART_PATHS
        ):
            groups.append(group)
            group, paths, group_bytes = [], set(), header_reserve
        group_bytes += (2 if group else 0) + size
        group.append((instance, comment.path, explanation))
        paths.add(comment.path)
    if group:
        groups.append(group)
    if len(groups) > MAX_PROSE_PARTS:
        raise ReviewInputError("finding prose part count exceeds capacity")
    parts = tuple(
        FindingProsePart(
            index,
            len(groups),
            _header(index, len(groups), head_sha)
            + "\n\n".join(item[2] for item in items),
            tuple(item[0] for item in items),
            tuple(sorted({item[1] for item in items})),
        )
        for index, items in enumerate(groups)
    )
    if sum(part.byte_length for part in parts) > MAX_PROSE_TOTAL_BYTES:
        raise ReviewInputError("complete finding prose exceeds aggregate capacity")
    return parts


@dataclass(frozen=True)
class FindingProseReadback:
    """Observed host metadata, supplied by a trusted adapter after a GET."""

    storage_id: int
    producer_id: int
    head_sha: str
    body: str


def verify_finding_prose_readbacks(
    parts: tuple[FindingProsePart, ...],
    observed: tuple[FindingProseReadback, ...],
    *,
    producer_id: int,
    head_sha: str,
) -> tuple[dict[str, object], ...]:
    """Return ordered exact-body receipts; any incomplete read withholds.

    These receipts are domain checks, not authentication by themselves. The
    host must bind them to its repository/PR/base/head/result/generation root
    and revalidate them before root activation and delayed finalization.
    """
    if (
        isinstance(producer_id, bool)
        or not isinstance(producer_id, int)
        or producer_id <= 0
        or len(parts) != len(observed)
        or len(parts) > MAX_PROSE_PARTS
        or any(not isinstance(part, FindingProsePart) for part in parts)
        or sum(part.byte_length for part in parts) > MAX_PROSE_TOTAL_BYTES
        or len({instance for part in parts for instance in part.instances})
        != sum(len(part.instances) for part in parts)
        or sum(len(part.instances) for part in parts) > 250
        or not isinstance(head_sha, str)
        or len(head_sha) != 40
        or any(character not in "0123456789abcdef" for character in head_sha)
    ):
        raise ReviewInputError("finding prose readback is incomplete")
    receipts = []
    seen: set[int] = set()
    for index, (part, readback) in enumerate(zip(parts, observed, strict=True)):
        if (
            not isinstance(readback, FindingProseReadback)
            or isinstance(readback.storage_id, bool)
            or not isinstance(readback.storage_id, int)
            or readback.storage_id <= 0
            or readback.storage_id in seen
            or isinstance(readback.producer_id, bool)
            or readback.producer_id != producer_id
            or readback.head_sha != head_sha
            or part.index != index
            or part.count != len(parts)
            or readback.body != part.body
            or part.byte_length > MAX_PROSE_PART_BYTES
            or len(part.paths) > MAX_PROSE_PART_PATHS
        ):
            raise ReviewInputError("finding prose readback authority conflicts")
        seen.add(readback.storage_id)
        receipts.append(
            {
                "storage_id": readback.storage_id,
                "index": index,
                "sha256": part.sha256,
                "bytes": part.byte_length,
                "instances": list(part.instances),
            }
        )
    return tuple(receipts)
