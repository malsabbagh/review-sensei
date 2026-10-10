"""Complete selected human feedback, separate from bounded ordinary discussion.

These immutable values are reference data. A digest detects changes; only the
host's authenticated source reads and current authorization admit the selection.
Persistence and target resolution belong to the shared authority/queue readers.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from .errors import ReviewInputError
from .evidence import evidence_digest
from .validation import DEFAULT_REVIEW_LIMITS, validate_bounded_text

FEEDBACK_INTERFACE_VERSION = "feedback-v1"
MAX_FEEDBACK_SOURCES = 32
MAX_FEEDBACK_SOURCE_BYTES = 64 * 1024
MAX_FEEDBACK_BYTES = 256 * 1024
MAX_FEEDBACK_TARGETS = 250

FEEDBACK_DIAGNOSTICS = frozenset(
    {
        "feedback_selection_invalid",
        "feedback_trigger_missing",
        "feedback_source_missing",
        "feedback_source_lookup_failed",
        "feedback_source_identity_invalid",
        "feedback_source_association_invalid",
        "feedback_source_unauthorized",
        "feedback_trigger_mention_missing",
        "feedback_source_changed",
        "feedback_source_invalid",
        "feedback_source_oversized",
        "feedback_total_oversized",
        "feedback_targets_invalid",
        "feedback_snapshot_invalid",
        "feedback_read_budget_exhausted",
        "feedback_prompt_oversized",
        "feedback_prompt_invalid",
    }
)


class FeedbackAdmissionError(ReviewInputError):
    """Closed diagnostic; never includes comment text, tokens or response data."""

    def __init__(self, diagnostic: str) -> None:
        reason = (
            diagnostic
            if diagnostic in FEEDBACK_DIAGNOSTICS
            else "feedback_source_invalid"
        )
        super().__init__(f"complete feedback refused (reason={reason})")
        self.diagnostic = reason


def _positive_id(value: object) -> bool:
    return type(value) is int and 0 < value <= 2**63 - 1


def _text(value: object, maximum: int) -> None:
    try:
        validate_bounded_text(
            value, maximum, label="feedback metadata", allow_empty=False
        )
    except ReviewInputError as exc:
        raise FeedbackAdmissionError("feedback_source_invalid") from exc


@dataclass(frozen=True)
class FeedbackReference:
    """A host-selected canonical endpoint and exact edit timestamp, never a URL."""

    kind: str
    comment_id: int
    updated_at: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.kind, str)
            or self.kind not in {"issue", "inline"}
            or not _positive_id(self.comment_id)
        ):
            raise FeedbackAdmissionError("feedback_selection_invalid")
        _text(self.updated_at, 128)

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "comment_id": self.comment_id,
            "updated_at": self.updated_at,
        }


def validate_feedback_references(
    references: tuple[FeedbackReference, ...], *, trigger: FeedbackReference
) -> None:
    if (
        not isinstance(references, tuple)
        or not 1 <= len(references) <= MAX_FEEDBACK_SOURCES
        or any(not isinstance(item, FeedbackReference) for item in references)
        or len({(item.kind, item.comment_id) for item in references}) != len(references)
    ):
        raise FeedbackAdmissionError("feedback_selection_invalid")
    if trigger not in references:
        raise FeedbackAdmissionError("feedback_trigger_missing")


def validate_feedback_context(
    *,
    repository: str,
    pull_request: int,
    base_sha: str,
    head_sha: str,
    target_ids: tuple[str, ...],
) -> None:
    if (
        not isinstance(repository, str)
        or len(repository) > 512
        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
        or any(segment in {".", ".."} for segment in repository.split("/"))
        or not _positive_id(pull_request)
        or any(
            not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{40}", value)
            for value in (base_sha, head_sha)
        )
    ):
        raise FeedbackAdmissionError("feedback_snapshot_invalid")
    if (
        not isinstance(target_ids, tuple)
        or len(target_ids) > MAX_FEEDBACK_TARGETS
        or any(
            not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value)
            for value in target_ids
        )
        or len(set(target_ids)) != len(target_ids)
    ):
        raise FeedbackAdmissionError("feedback_targets_invalid")


@dataclass(frozen=True)
class FeedbackSource:
    reference: FeedbackReference
    author: str
    author_id: int
    association: str
    body: str
    # Inline thread association is included in edit/move fences.
    root_comment_id: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.reference, FeedbackReference):
            raise FeedbackAdmissionError("feedback_source_invalid")
        _text(self.author, 256)
        if not _positive_id(self.author_id):
            raise FeedbackAdmissionError("feedback_source_identity_invalid")
        if not isinstance(self.association, str) or self.association not in {
            "OWNER",
            "MEMBER",
            "COLLABORATOR",
        }:
            raise FeedbackAdmissionError("feedback_source_unauthorized")
        if (self.reference.kind == "issue" and self.root_comment_id is not None) or (
            self.reference.kind == "inline" and not _positive_id(self.root_comment_id)
        ):
            raise FeedbackAdmissionError("feedback_source_identity_invalid")
        if not isinstance(self.body, str):
            raise FeedbackAdmissionError("feedback_source_invalid")
        try:
            raw = self.body.encode("utf-8")
        except UnicodeError as exc:
            raise FeedbackAdmissionError("feedback_source_invalid") from exc
        if not raw:
            raise FeedbackAdmissionError("feedback_source_invalid")
        if len(raw) > MAX_FEEDBACK_SOURCE_BYTES:
            raise FeedbackAdmissionError("feedback_source_oversized")

    @property
    def body_bytes(self) -> int:
        return len(self.body.encode("utf-8"))

    @property
    def body_sha256(self) -> str:
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            **self.reference.to_dict(),
            "author": self.author,
            "author_id": self.author_id,
            "association": self.association,
            "root_comment_id": self.root_comment_id,
            "body_bytes": self.body_bytes,
            "body_sha256": self.body_sha256,
            "body": self.body,
        }


@dataclass(frozen=True)
class FeedbackSelection:
    repository: str
    pull_request: int
    base_sha: str
    head_sha: str
    trigger: FeedbackReference
    sources: tuple[FeedbackSource, ...]
    target_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_feedback_context(
            repository=self.repository,
            pull_request=self.pull_request,
            base_sha=self.base_sha,
            head_sha=self.head_sha,
            target_ids=self.target_ids,
        )
        if not isinstance(self.trigger, FeedbackReference):
            raise FeedbackAdmissionError("feedback_snapshot_invalid")
        if not isinstance(self.sources, tuple) or any(
            not isinstance(item, FeedbackSource) for item in self.sources
        ):
            raise FeedbackAdmissionError("feedback_selection_invalid")
        validate_feedback_references(
            tuple(item.reference for item in self.sources), trigger=self.trigger
        )
        if self.total_bytes > MAX_FEEDBACK_BYTES:
            raise FeedbackAdmissionError("feedback_total_oversized")

    @property
    def total_bytes(self) -> int:
        return sum(item.body_bytes for item in self.sources)

    @property
    def digest(self) -> str:
        return evidence_digest(self.to_dict())

    def contains_excerpt(self, excerpt: str) -> bool:
        """Citations must occur in one complete original source, never framing."""
        return (
            isinstance(excerpt, str)
            and bool(excerpt)
            and any(excerpt in item.body for item in self.sources)
        )

    def render_prompt(self, *, prefix: str, suffix: str, max_prompt_bytes: int) -> str:
        """Preflight complete structured feedback plus all caller framing/data.

        The caller supplies its existing trusted downward-only prompt ceiling;
        admission ceilings never increase a provider allowance. No body is
        omitted, shortened or normalized to make the prompt fit.
        """
        if (
            type(max_prompt_bytes) is not int
            or not 1 <= max_prompt_bytes <= DEFAULT_REVIEW_LIMITS.max_prompt_bytes
            or not isinstance(prefix, str)
            or not isinstance(suffix, str)
        ):
            raise FeedbackAdmissionError("feedback_prompt_invalid")
        prompt = (
            prefix
            + json.dumps(
                self.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + suffix
        )
        try:
            size = len(prompt.encode("utf-8"))
        except UnicodeError as exc:
            raise FeedbackAdmissionError("feedback_prompt_invalid") from exc
        if size > max_prompt_bytes:
            raise FeedbackAdmissionError("feedback_prompt_oversized")
        return prompt

    def to_dict(self) -> dict[str, object]:
        return {
            "interface": FEEDBACK_INTERFACE_VERSION,
            "repository": self.repository,
            "pull_request": self.pull_request,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "trigger": self.trigger.to_dict(),
            "target_ids": list(self.target_ids),
            "total_bytes": self.total_bytes,
            "sources": [item.to_dict() for item in self.sources],
        }
