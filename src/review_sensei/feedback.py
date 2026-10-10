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
FEEDBACK_SELECTOR_VERSION = "feedback-selector-v1"
MAX_FEEDBACK_SELECTOR_BYTES = 32 * 1024
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
        "feedback_selector_invalid",
        "feedback_selector_oversized",
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

    @property
    def event_key(self) -> str:
        """Stable journal lookup; changed content must conflict, not re-enroll.

        The host scopes the journal to its original inventory. This key excludes
        timestamps, body, actor, selected sources/targets and mutable snapshots;
        the complete selection digest binds those facts to the operation.
        """
        return feedback_event_key(
            repository=self.repository,
            pull_request=self.pull_request,
            trigger=self.trigger,
        )

    def authorization_document(self) -> dict[str, object]:
        """Exact metadata for a dedicated live host/broker source attestation.

        This serializable reference is neither a grant nor authenticated input.
        Each consuming mutation must independently reload the canonical sources
        and compare all fields, then obtain its own scoped broker authorization.
        Raw bodies are omitted here; their complete lengths/digests remain bound.
        """
        return {
            "interface": FEEDBACK_INTERFACE_VERSION,
            "repository": self.repository,
            "pull_request": self.pull_request,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "event_key": self.event_key,
            "selection_digest": self.digest,
            "trigger": self.trigger.to_dict(),
            "target_ids": list(self.target_ids),
            "total_bytes": self.total_bytes,
            "sources": [
                {key: value for key, value in item.to_dict().items() if key != "body"}
                for item in self.sources
            ],
        }

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


def feedback_event_key(
    *, repository: str, pull_request: int, trigger: FeedbackReference
) -> str:
    # Reuse exact repository/PR validation without introducing a snapshot into
    # the identity. A timestamp remains required source metadata, but is omitted.
    validate_feedback_context(
        repository=repository,
        pull_request=pull_request,
        base_sha="0" * 40,
        head_sha="0" * 40,
        target_ids=(),
    )
    if not isinstance(trigger, FeedbackReference):
        raise FeedbackAdmissionError("feedback_selection_invalid")
    return evidence_digest(
        {
            "domain": "reviewsensei:feedback-event:v1",
            "repository": repository,
            "pull_request": pull_request,
            "trigger": {"kind": trigger.kind, "comment_id": trigger.comment_id},
        }
    )


@dataclass(frozen=True)
class FeedbackSelector:
    """Explicit operator metadata, never source text or evidence authority.

    All bodies/authors/associations are reloaded from canonical host endpoints.
    The selector binds the exact reviewed snapshot and ordered source timestamps.
    Multiple sources require explicit full targets; the single-trigger default
    can retain the ordinary complete-inventory assessment scope.
    """

    repository: str
    pull_request: int
    base_sha: str
    head_sha: str
    trigger: FeedbackReference
    references: tuple[FeedbackReference, ...]
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
            raise FeedbackAdmissionError("feedback_selector_invalid")
        validate_feedback_references(self.references, trigger=self.trigger)
        if len(self.references) > 1 and not self.target_ids:
            raise FeedbackAdmissionError("feedback_targets_invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "interface": FEEDBACK_SELECTOR_VERSION,
            "repository": self.repository,
            "pull_request": self.pull_request,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "trigger": self.trigger.to_dict(),
            "sources": [item.to_dict() for item in self.references],
            "target_ids": list(self.target_ids),
        }

    @classmethod
    def from_json(cls, raw: bytes) -> FeedbackSelector:
        """Parse one bounded closed document supplied by a trusted operator.

        No file/URL resolution, schema coercion, body-derived selection or new
        command syntax. Duplicate keys, unknown fields and prefixes refuse.
        """
        if not isinstance(raw, bytes):
            raise FeedbackAdmissionError("feedback_selector_invalid")
        if len(raw) > MAX_FEEDBACK_SELECTOR_BYTES:
            raise FeedbackAdmissionError("feedback_selector_oversized")

        def closed_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
            document: dict[str, object] = {}
            for key, value in pairs:
                if key in document:
                    raise FeedbackAdmissionError("feedback_selector_invalid")
                document[key] = value
            return document

        def reference(value: object) -> FeedbackReference:
            if not isinstance(value, dict) or set(value) != {
                "kind",
                "comment_id",
                "updated_at",
            }:
                raise FeedbackAdmissionError("feedback_selector_invalid")
            return FeedbackReference(**value)

        try:
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=closed_pairs)
            if (
                not isinstance(value, dict)
                or set(value)
                != {
                    "interface",
                    "repository",
                    "pull_request",
                    "base_sha",
                    "head_sha",
                    "trigger",
                    "sources",
                    "target_ids",
                }
                or value["interface"] != FEEDBACK_SELECTOR_VERSION
                or not isinstance(value["sources"], list)
                or not 1 <= len(value["sources"]) <= MAX_FEEDBACK_SOURCES
                or not isinstance(value["target_ids"], list)
                or len(value["target_ids"]) > MAX_FEEDBACK_TARGETS
            ):
                raise FeedbackAdmissionError("feedback_selector_invalid")
            return cls(
                repository=value["repository"],
                pull_request=value["pull_request"],
                base_sha=value["base_sha"],
                head_sha=value["head_sha"],
                trigger=reference(value["trigger"]),
                references=tuple(reference(item) for item in value["sources"]),
                target_ids=tuple(value["target_ids"]),
            )
        except (ValueError, UnicodeError, TypeError, RecursionError) as exc:
            raise FeedbackAdmissionError("feedback_selector_invalid") from exc
