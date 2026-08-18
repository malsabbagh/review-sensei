from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Mapping, cast

from .errors import ReviewInputError
from .validation import (
    DEFAULT_REVIEW_LIMITS,
    ReviewLimits,
    utf8_size,
    validate_bounded_text,
    validate_repository_path,
)

_LEARNING_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
MAX_REVIEW_CONTEXT_FILES = 64
MAX_REVIEW_DOCUMENT_BYTES = 128 * 1024
MAX_REVIEW_CONTEXT_TOTAL_BYTES = 512 * 1024


def _validate_learning_scope(scope: tuple[str, ...]) -> None:
    if not scope:
        raise ReviewInputError("learning scope must contain at least one pattern")
    for pattern in scope:
        validate_repository_path(
            pattern,
            pattern=True,
            label="learning scope pattern",
        )


def _optional_learning_text(label: str, value: object) -> None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ReviewInputError(f"learning {label} must be a non-empty string")
    if value is not None:
        utf8_size(value, label=f"learning {label}")


@dataclass(frozen=True)
class ReviewDocument:
    """A validated repository document supplied as review context."""

    path: str
    content: str
    sha256: str

    def __post_init__(self) -> None:
        validate_repository_path(self.path, label="review document path")
        if not isinstance(self.content, str) or not self.content.strip():
            raise ReviewInputError("review document content must be a non-empty string")
        validate_bounded_text(
            self.content,
            MAX_REVIEW_DOCUMENT_BYTES,
            label="review document content",
            allow_empty=False,
        )
        if not isinstance(self.sha256, str) or not _SHA256.fullmatch(self.sha256):
            raise ReviewInputError(
                "review document sha256 must be a lowercase SHA-256 digest"
            )
        if self.sha256 != hashlib.sha256(self.content.encode("utf-8")).hexdigest():
            raise ReviewInputError("review document sha256 must match its content")

    def to_prompt_dict(self) -> dict[str, str]:
        return {"path": self.path, "sha256": self.sha256, "content": self.content}


@dataclass(frozen=True)
class ReviewLensContext:
    """Validated learnings and documents selected for one review lens."""

    category_id: str
    learnings: tuple[LearningEntry, ...] = ()
    documents: tuple[ReviewDocument, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.category_id, str) or not _LEARNING_ID.fullmatch(
            self.category_id
        ):
            raise ReviewInputError(
                "lens context category_id must be a lowercase safe identifier"
            )
        if not isinstance(self.learnings, tuple) or any(
            not isinstance(learning, LearningEntry) for learning in self.learnings
        ):
            raise ReviewInputError(
                "lens context learnings must be LearningEntry values"
            )
        if any(learning.status != "active" for learning in self.learnings):
            raise ReviewInputError("lens context learnings must be active")
        if not isinstance(self.documents, tuple) or any(
            not isinstance(document, ReviewDocument) for document in self.documents
        ):
            raise ReviewInputError(
                "lens context documents must be ReviewDocument values"
            )

    def to_prompt_dict(self) -> dict[str, object]:
        return {
            "category_id": self.category_id,
            "learnings": [learning.to_prompt_dict() for learning in self.learnings],
            "documents": [document.to_prompt_dict() for document in self.documents],
        }


@dataclass(frozen=True)
class ReviewRequest:
    """Provider-neutral input to a code review."""

    diff: str
    repository: str | None = None
    pull_request_number: int | None = None
    title: str | None = None
    instructions: str | None = None
    model: str | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)
    learnings: tuple[LearningEntry, ...] = ()
    propose_learnings: bool = True
    active_category_ids: tuple[str, ...] | None = None
    lens_contexts: tuple[ReviewLensContext, ...] = ()
    limits: ReviewLimits = DEFAULT_REVIEW_LIMITS

    def __post_init__(self) -> None:
        if not isinstance(self.diff, str) or not self.diff.strip():
            raise ReviewInputError("diff must be a non-empty string")
        # Encode once here so lone surrogates and other invalid Unicode fail
        # before prompt construction, while diff size/count limits remain the
        # responsibility of the bounded diff preflight.
        utf8_size(self.diff, label="diff")
        if not isinstance(self.limits, ReviewLimits):
            raise ReviewInputError("review limits must be a ReviewLimits value")
        for label, value, maximum in (
            ("repository", self.repository, self.limits.max_repository_bytes),
            ("title", self.title, self.limits.max_title_bytes),
            ("instructions", self.instructions, self.limits.max_instructions_bytes),
            ("model", self.model, self.limits.max_model_bytes),
        ):
            if value is not None:
                validate_bounded_text(value, maximum, label=f"review {label}")
        if not isinstance(self.metadata, Mapping):
            raise ReviewInputError("review metadata must be a mapping")
        try:
            metadata_items = tuple(self.metadata.items())
        except (AttributeError, TypeError) as exc:
            raise ReviewInputError("review metadata must be a mapping") from exc
        if len(metadata_items) > self.limits.max_metadata_items:
            raise ReviewInputError("review metadata contains too many items")
        for key, value in metadata_items:
            validate_bounded_text(
                key,
                self.limits.max_metadata_key_bytes,
                label="review metadata key",
                allow_empty=False,
            )
            validate_bounded_text(
                value,
                self.limits.max_metadata_value_bytes,
                label="review metadata value",
            )
        if self.pull_request_number is not None and (
            isinstance(self.pull_request_number, bool)
            or not isinstance(self.pull_request_number, int)
            or self.pull_request_number < 1
        ):
            raise ReviewInputError("pull_request_number must be a positive integer")
        if not isinstance(self.propose_learnings, bool):
            raise ReviewInputError("propose_learnings must be a boolean")
        if not isinstance(self.learnings, tuple) or any(
            not isinstance(learning, LearningEntry) for learning in self.learnings
        ):
            raise ReviewInputError("learnings must be a tuple of LearningEntry values")
        if any(learning.status != "active" for learning in self.learnings):
            raise ReviewInputError("review learnings must be active")
        if self.active_category_ids is not None:
            if not isinstance(self.active_category_ids, tuple) or any(
                not isinstance(category_id, str)
                or not _LEARNING_ID.fullmatch(category_id)
                for category_id in self.active_category_ids
            ):
                raise ReviewInputError(
                    "active_category_ids must be a tuple of lowercase safe identifiers"
                )
            if len(self.active_category_ids) != len(set(self.active_category_ids)):
                raise ReviewInputError("active_category_ids must be unique")
            if len(self.active_category_ids) > self.limits.max_active_categories:
                raise ReviewInputError("review contains too many active categories")
        if not isinstance(self.lens_contexts, tuple) or any(
            not isinstance(context, ReviewLensContext) for context in self.lens_contexts
        ):
            raise ReviewInputError(
                "lens_contexts must be a tuple of ReviewLensContext values"
            )
        nested_learning_count = sum(
            len(context.learnings) for context in self.lens_contexts
        )
        if (
            len(self.learnings) + nested_learning_count
            > self.limits.max_learning_entries
        ):
            raise ReviewInputError("review contains too many learning entries")
        if len(self.lens_contexts) > self.limits.max_lens_contexts:
            raise ReviewInputError("review contains too many lens contexts")
        context_ids = [context.category_id for context in self.lens_contexts]
        if len(context_ids) != len(set(context_ids)):
            raise ReviewInputError("lens_contexts must have unique category ids")
        if self.active_category_ids is not None and not set(context_ids).issubset(
            self.active_category_ids
        ):
            raise ReviewInputError(
                "lens_contexts must belong to active review categories"
            )
        documents_by_path: dict[str, ReviewDocument] = {}
        for context in self.lens_contexts:
            for document in context.documents:
                existing = documents_by_path.setdefault(document.path, document)
                if existing != document:
                    raise ReviewInputError(
                        "review document paths must resolve to consistent content"
                    )
        if len(documents_by_path) > MAX_REVIEW_CONTEXT_FILES:
            raise ReviewInputError("review context contains too many documents")
        if (
            sum(
                len(document.content.encode("utf-8"))
                for document in documents_by_path.values()
            )
            > MAX_REVIEW_CONTEXT_TOTAL_BYTES
        ):
            raise ReviewInputError("review context exceeds the total size limit")


@dataclass(frozen=True)
class LearningEntry:
    """An approved repository fact used as context for future reviews."""

    id: str
    title: str
    rule: str
    scope: tuple[str, ...] = ("*",)
    rationale: str | None = None
    category: str | None = None
    source: str | None = None
    status: str = "active"

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _LEARNING_ID.fullmatch(self.id):
            raise ReviewInputError(
                "learning id must be a lowercase repository-safe identifier"
            )
        for label, value in (("title", self.title), ("rule", self.rule)):
            if not isinstance(value, str) or not value.strip():
                raise ReviewInputError(f"learning {label} must be a non-empty string")
            utf8_size(value, label=f"learning {label}")
        if not isinstance(self.scope, tuple):
            raise ReviewInputError("learning scope must be a tuple of patterns")
        _validate_learning_scope(self.scope)
        optional_values: tuple[tuple[str, str | None], ...] = (
            ("rationale", self.rationale),
            ("category", self.category),
            ("source", self.source),
        )
        for optional_label, optional_content in optional_values:
            _optional_learning_text(optional_label, optional_content)
        if self.status not in {"active", "retired"}:
            raise ReviewInputError("learning status must be active or retired")

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "LearningEntry":
        if not isinstance(value, Mapping):
            raise ReviewInputError("learning entry must be a JSON object")
        raw_scope = value.get("scope", ["*"])
        if not isinstance(raw_scope, list) or not all(
            isinstance(pattern, str) for pattern in raw_scope
        ):
            raise ReviewInputError("learning scope must be a JSON array of strings")
        return cls(
            id=cast(str, value["id"]),
            title=cast(str, value["title"]),
            rule=cast(str, value["rule"]),
            scope=tuple(raw_scope),
            rationale=cast(str | None, value.get("rationale")),
            category=cast(str | None, value.get("category")),
            source=cast(str | None, value.get("source")),
            status=cast(str, value.get("status", "active")),
        )

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "id": self.id,
            "title": self.title,
            "rule": self.rule,
            "scope": list(self.scope),
            "status": self.status,
        }
        for label, content in (
            ("rationale", self.rationale),
            ("category", self.category),
            ("source", self.source),
        ):
            if content is not None:
                value[label] = content
        return value

    def to_prompt_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "id": self.id,
            "title": self.title,
            "rule": self.rule,
            "scope": list(self.scope),
        }
        if self.rationale is not None:
            value["rationale"] = self.rationale
        if self.category is not None:
            value["category"] = self.category
        return value


@dataclass(frozen=True)
class LearningProposal:
    """Unapproved durable-review-context proposed by a provider."""

    title: str
    rule: str
    scope: tuple[str, ...] = ("*",)
    rationale: str | None = None
    category: str | None = None

    def __post_init__(self) -> None:
        for label, value in (("title", self.title), ("rule", self.rule)):
            if not isinstance(value, str) or not value.strip():
                raise ReviewInputError(
                    f"learning proposal {label} must be a non-empty string"
                )
            utf8_size(value, label=f"learning proposal {label}")
        if not isinstance(self.scope, tuple):
            raise ReviewInputError(
                "learning proposal scope must be a tuple of patterns"
            )
        _validate_learning_scope(self.scope)
        optional_values: tuple[tuple[str, str | None], ...] = (
            ("rationale", self.rationale),
            ("category", self.category),
        )
        for optional_label, optional_content in optional_values:
            _optional_learning_text(optional_label, optional_content)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "LearningProposal":
        if not isinstance(value, Mapping):
            raise ReviewInputError("learning proposal must be a JSON object")
        raw_scope = value.get("scope", ["*"])
        if not isinstance(raw_scope, list) or not all(
            isinstance(pattern, str) for pattern in raw_scope
        ):
            raise ReviewInputError(
                "learning proposal scope must be a JSON array of strings"
            )
        return cls(
            title=cast(str, value["title"]),
            rule=cast(str, value["rule"]),
            scope=tuple(raw_scope),
            rationale=cast(str | None, value.get("rationale")),
            category=cast(str | None, value.get("category")),
        )

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "title": self.title,
            "rule": self.rule,
            "scope": list(self.scope),
        }
        for label, content in (
            ("rationale", self.rationale),
            ("category", self.category),
        ):
            if content is not None:
                value[label] = content
        return value

    def to_entry(self, *, id: str, source: str | None = None) -> LearningEntry:
        """Materialize a proposal after a publisher assigns identity and evidence."""

        return LearningEntry(
            id=id,
            title=self.title,
            rule=self.rule,
            scope=self.scope,
            rationale=self.rationale,
            category=self.category,
            source=source,
        )


@dataclass(frozen=True)
class ReviewComment:
    """A proposed inline review comment on an added or modified line."""

    path: str
    line: int
    body: str
    severity: str | None = None
    category: str | None = None

    def __post_init__(self) -> None:
        validate_repository_path(self.path, label="comment path")
        if (
            isinstance(self.line, bool)
            or not isinstance(self.line, int)
            or self.line < 1
        ):
            raise ReviewInputError("comment line must be a positive integer")
        if self.line > DEFAULT_REVIEW_LIMITS.max_line_number:
            raise ReviewInputError("comment line exceeds the configured limit")
        if not isinstance(self.body, str) or not self.body.strip():
            raise ReviewInputError("comment body must be a non-empty string")
        validate_bounded_text(
            self.body,
            DEFAULT_REVIEW_LIMITS.max_comment_body_bytes,
            label="comment body",
            allow_empty=False,
        )
        for label, value in (("severity", self.severity), ("category", self.category)):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ReviewInputError(f"comment {label} must be a non-empty string")
            if value is not None:
                utf8_size(value, label=f"comment {label}")

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "path": self.path,
            "line": self.line,
            "body": self.body,
        }
        if self.severity is not None:
            value["severity"] = self.severity
        if self.category is not None:
            value["category"] = self.category
        return value


@dataclass(frozen=True)
class ReviewResult:
    """Validated review output safe for a publisher adapter to consume."""

    summary: str
    comments: tuple[ReviewComment, ...]
    provider: str
    model: str | None = None
    learning_proposals: tuple[LearningProposal, ...] = ()
    limits: ReviewLimits = DEFAULT_REVIEW_LIMITS

    def __post_init__(self) -> None:
        if not isinstance(self.limits, ReviewLimits):
            raise ReviewInputError("review limits must be a ReviewLimits value")
        validate_bounded_text(
            self.summary,
            self.limits.max_summary_bytes,
            label="review summary",
            allow_empty=False,
        )
        if not isinstance(self.comments, tuple) or any(
            not isinstance(comment, ReviewComment) for comment in self.comments
        ):
            raise ReviewInputError(
                "review comments must be a tuple of ReviewComment values"
            )
        if not isinstance(self.learning_proposals, tuple) or any(
            not isinstance(proposal, LearningProposal)
            for proposal in self.learning_proposals
        ):
            raise ReviewInputError(
                "review learning_proposals must be a tuple of LearningProposal values"
            )
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise ReviewInputError("review provider must be a non-empty string")
        validate_bounded_text(
            self.provider,
            self.limits.max_model_bytes,
            label="review provider",
            allow_empty=False,
        )
        if self.model is not None:
            validate_bounded_text(
                self.model,
                self.limits.max_model_bytes,
                label="review model",
                allow_empty=False,
            )
        if len(self.learning_proposals) > self.limits.max_learning_proposals:
            raise ReviewInputError("review contains too many learning proposals")

        # Exact location/body duplicates are publisher-noise.  Keep the first
        # occurrence in stage order while preserving distinct comments that
        # happen to share a path and line.
        unique_comments: list[ReviewComment] = []
        seen: set[tuple[str, int, str]] = set()
        for comment in self.comments:
            key = (comment.path, comment.line, comment.body)
            if key in seen:
                continue
            seen.add(key)
            unique_comments.append(comment)
        object.__setattr__(self, "comments", tuple(unique_comments))
        if len(self.comments) > self.limits.max_comments:
            raise ReviewInputError("review contains too many comments")
        for comment in self.comments:
            if comment.line > self.limits.max_line_number:
                raise ReviewInputError(
                    "review comment line exceeds the configured limit"
                )
            validate_bounded_text(
                comment.body,
                self.limits.max_comment_body_bytes,
                label="review comment body",
                allow_empty=False,
            )

        for proposal in self.learning_proposals:
            size = utf8_size(
                _json_compact(proposal.to_dict()),
                label="learning proposal",
            )
            if size > self.limits.max_learning_proposal_bytes:
                raise ReviewInputError(
                    "learning proposal exceeds the configured size limit"
                )
        proposal_total = utf8_size(
            _json_compact([proposal.to_dict() for proposal in self.learning_proposals]),
            label="learning proposals",
        )
        if proposal_total > self.limits.max_learning_proposals_total_bytes:
            raise ReviewInputError("learning proposals exceed the aggregate size limit")
        if (
            utf8_size(_json_compact(self.to_dict()), label="review result")
            > self.limits.max_result_bytes
        ):
            raise ReviewInputError("review result exceeds the configured size limit")

    def to_dict(self) -> dict[str, object]:
        return {
            "summary": self.summary,
            "comments": [comment.to_dict() for comment in self.comments],
            "provider": self.provider,
            "model": self.model,
            "learning_proposals": [
                proposal.to_dict() for proposal in self.learning_proposals
            ],
        }


@dataclass(frozen=True)
class ProviderRequest:
    """Provider-facing request; providers can map this to their native API."""

    prompt: str
    model: str | None = None
    json_mode: bool = True
    max_prompt_bytes: int = DEFAULT_REVIEW_LIMITS.max_prompt_bytes
    max_response_bytes: int = DEFAULT_REVIEW_LIMITS.max_provider_response_bytes
    limits: ReviewLimits = DEFAULT_REVIEW_LIMITS

    def __post_init__(self) -> None:
        if not isinstance(self.limits, ReviewLimits):
            raise ReviewInputError("provider limits must be a ReviewLimits value")
        if self.max_prompt_bytes == DEFAULT_REVIEW_LIMITS.max_prompt_bytes:
            object.__setattr__(self, "max_prompt_bytes", self.limits.max_prompt_bytes)
        if self.max_response_bytes == DEFAULT_REVIEW_LIMITS.max_provider_response_bytes:
            object.__setattr__(
                self,
                "max_response_bytes",
                self.limits.max_provider_response_bytes,
            )
        if isinstance(self.max_prompt_bytes, bool) or not isinstance(
            self.max_prompt_bytes, int
        ):
            raise ReviewInputError(
                "provider max_prompt_bytes must be a positive integer"
            )
        if isinstance(self.max_response_bytes, bool) or not isinstance(
            self.max_response_bytes, int
        ):
            raise ReviewInputError(
                "provider max_response_bytes must be a positive integer"
            )
        if (
            self.max_prompt_bytes < 1
            or self.max_prompt_bytes > self.limits.max_prompt_bytes
        ):
            raise ReviewInputError(
                "provider max_prompt_bytes exceeds the configured limit"
            )
        if (
            self.max_response_bytes < 1
            or self.max_response_bytes > self.limits.max_provider_response_bytes
        ):
            raise ReviewInputError(
                "provider max_response_bytes exceeds the configured limit"
            )
        validate_bounded_text(
            self.prompt,
            self.max_prompt_bytes,
            label="provider prompt",
            allow_empty=False,
        )
        if self.model is not None:
            validate_bounded_text(
                self.model,
                self.limits.max_model_bytes,
                label="provider model",
                allow_empty=False,
            )


@dataclass(frozen=True)
class ProviderResponse:
    """Normalized provider response without retaining raw request context."""

    text: str
    provider: str
    model: str | None = None
    limits: ReviewLimits = DEFAULT_REVIEW_LIMITS

    @property
    def max_response_bytes(self) -> int:
        return self.limits.max_provider_response_bytes

    def __post_init__(self) -> None:
        if not isinstance(self.limits, ReviewLimits):
            raise ReviewInputError(
                "provider response limits must be a ReviewLimits value"
            )
        validate_bounded_text(
            self.text,
            self.limits.max_provider_response_bytes,
            label="provider response",
            allow_empty=False,
        )
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise ReviewInputError(
                "provider response provider must be a non-empty string"
            )
        if self.model is not None:
            validate_bounded_text(
                self.model,
                self.limits.max_model_bytes,
                label="provider response model",
                allow_empty=False,
            )


def _json_compact(value: object) -> str:
    # Local import avoids making JSON part of the provider-neutral model module's
    # public surface while keeping size accounting deterministic.
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
