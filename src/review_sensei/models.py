from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, cast

from .coverage import CoverageManifest
from .errors import ReviewInputError
from .validation import (
    DEFAULT_REVIEW_LIMITS,
    DEFAULT_TOTAL_WORK_BUDGET,
    ReviewLimits,
    TotalWorkBudget,
    utf8_size,
    validate_bounded_text,
    validate_repository_path,
)

_LEARNING_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_GIT_SHA = re.compile(r"^[a-f0-9]{40}$")
FINDING_LIFECYCLE_STATES = frozenset(
    {"new", "still-present", "fixed", "outdated", "uncertain"}
)
COVERAGE_MODES = frozenset({"full", "incremental", "fallback-full"})
MAX_REVIEW_CONTEXT_FILES = 64
MAX_REVIEW_DOCUMENT_BYTES = 128 * 1024
MAX_REVIEW_CONTEXT_TOTAL_BYTES = 512 * 1024
MAX_CONVERSATION_MESSAGES = 20
MAX_CONVERSATION_FINDINGS = 20
MAX_CONVERSATION_CONTEXT_BYTES = 64 * 1024
MAX_CONVERSATION_REPLY_BYTES = 16 * 1024
MAX_CONVERSATION_PR_BODY_BYTES = 8 * 1024
MAX_CONVERSATION_DIFF_BYTES = 16 * 1024
TRANSACTION_PHASES = frozenset(
    {
        "analysis",
        "analysis_failed",
        "publication_pending",
        "publication_failed",
        "publication_succeeded",
    }
)
_TRANSACTION_PHASE_TRANSITIONS = {
    "analysis": frozenset({"analysis", "analysis_failed", "publication_pending"}),
    "analysis_failed": frozenset({"analysis_failed"}),
    "publication_pending": frozenset(
        {"publication_pending", "publication_failed", "publication_succeeded"}
    ),
    "publication_failed": frozenset(
        {"publication_failed", "publication_pending", "publication_succeeded"}
    ),
    "publication_succeeded": frozenset({"publication_succeeded"}),
}
_TRANSACTION_REPOSITORY = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?$"
)
_TRANSACTION_CONFIGURATION_KEYS = frozenset(
    {
        "provider",
        "model",
        "stages",
        "category_policy",
        "orchestration",
        "publication_mode",
    }
)


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
    source_context: object | None = None
    untrusted_head_sha: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    orchestrate_large_changes: bool = False
    work_budget: TotalWorkBudget = field(
        default_factory=lambda: DEFAULT_TOTAL_WORK_BUDGET
    )

    def __post_init__(self) -> None:
        if not isinstance(self.diff, str) or not self.diff.strip():
            raise ReviewInputError("diff must be a non-empty string")
        # Encode once here so lone surrogates and other invalid Unicode fail
        # before prompt construction, while diff size/count limits remain the
        # responsibility of the bounded diff preflight.
        utf8_size(self.diff, label="diff")
        if not isinstance(self.limits, ReviewLimits):
            raise ReviewInputError("review limits must be a ReviewLimits value")
        if not isinstance(self.orchestrate_large_changes, bool):
            raise ReviewInputError("orchestrate_large_changes must be a boolean")
        if not isinstance(self.work_budget, TotalWorkBudget):
            raise ReviewInputError("work_budget must be a TotalWorkBudget value")
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
        if self.source_context is not None:
            from .context import SourceContextSelection

            if not isinstance(self.source_context, SourceContextSelection):
                raise ReviewInputError(
                    "review source_context must be a SourceContextSelection"
                )
        if self.untrusted_head_sha is not None:
            if not isinstance(self.untrusted_head_sha, str) or not _GIT_SHA.fullmatch(
                self.untrusted_head_sha
            ):
                raise ReviewInputError("untrusted_head_sha must be a commit SHA")
        if self.base_sha is not None and (
            not isinstance(self.base_sha, str) or not _GIT_SHA.fullmatch(self.base_sha)
        ):
            raise ReviewInputError("review base_sha must be a Git commit sha")
        if self.head_sha is not None and (
            not isinstance(self.head_sha, str) or not _GIT_SHA.fullmatch(self.head_sha)
        ):
            raise ReviewInputError("review head_sha must be a Git commit sha")


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
    owner: str | None = None
    provenance: str | None = None
    reviewed_at: str | None = None
    expires_at: str | None = None
    supersedes: tuple[str, ...] = ()
    superseded_by: str | None = None

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
            ("owner", self.owner),
            ("provenance", self.provenance),
            ("reviewed_at", self.reviewed_at),
            ("expires_at", self.expires_at),
            ("superseded_by", self.superseded_by),
        )
        for optional_label, optional_content in optional_values:
            _optional_learning_text(optional_label, optional_content)
        if self.status not in {"active", "retired", "superseded"}:
            raise ReviewInputError(
                "learning status must be active, retired, or superseded"
            )
        if self.status == "superseded" and not self.superseded_by:
            raise ReviewInputError("learning superseded status requires superseded_by")
        if self.superseded_by is not None and not _LEARNING_ID.fullmatch(
            self.superseded_by
        ):
            raise ReviewInputError(
                "learning superseded_by must be a lowercase repository-safe identifier"
            )
        if self.superseded_by == self.id:
            raise ReviewInputError("learning superseded_by cannot reference itself")
        if not isinstance(self.supersedes, tuple) or any(
            not isinstance(identifier, str) or not _LEARNING_ID.fullmatch(identifier)
            for identifier in self.supersedes
        ):
            raise ReviewInputError("learning supersedes must contain safe identifiers")
        if len(self.supersedes) != len(set(self.supersedes)):
            raise ReviewInputError("learning supersedes must be unique")
        for label, timestamp in (
            ("reviewed_at", self.reviewed_at),
            ("expires_at", self.expires_at),
        ):
            if timestamp is not None:
                try:
                    datetime.fromisoformat(cast(str, timestamp).replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ReviewInputError(
                        f"learning {label} must be ISO-8601"
                    ) from exc

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "LearningEntry":
        if not isinstance(value, Mapping):
            raise ReviewInputError("learning entry must be a JSON object")
        allowed = {
            "id",
            "title",
            "rule",
            "scope",
            "rationale",
            "category",
            "source",
            "status",
            "owner",
            "provenance",
            "reviewed_at",
            "expires_at",
            "supersedes",
            "superseded_by",
        }
        if any(key not in allowed for key in value):
            raise ReviewInputError("learning entry contains an unsupported field")
        raw_scope = value.get("scope", ["*"])
        if not isinstance(raw_scope, list) or not all(
            isinstance(pattern, str) for pattern in raw_scope
        ):
            raise ReviewInputError("learning scope must be a JSON array of strings")
        raw_supersedes = value.get("supersedes", [])
        if not isinstance(raw_supersedes, list) or not all(
            isinstance(identifier, str) for identifier in raw_supersedes
        ):
            raise ReviewInputError(
                "learning supersedes must be a JSON array of strings"
            )
        return cls(
            id=cast(str, value["id"]),
            title=cast(str, value["title"]),
            rule=cast(str, value["rule"]),
            scope=tuple(raw_scope),
            rationale=cast(str | None, value.get("rationale")),
            category=cast(str | None, value.get("category")),
            source=cast(str | None, value.get("source")),
            status=cast(str, value.get("status", "active")),
            owner=cast(str | None, value.get("owner")),
            provenance=cast(str | None, value.get("provenance")),
            reviewed_at=cast(str | None, value.get("reviewed_at")),
            expires_at=cast(str | None, value.get("expires_at")),
            supersedes=tuple(raw_supersedes),
            superseded_by=cast(str | None, value.get("superseded_by")),
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
            ("owner", self.owner),
            ("provenance", self.provenance),
            ("reviewed_at", self.reviewed_at),
            ("expires_at", self.expires_at),
            ("superseded_by", self.superseded_by),
        ):
            if content is not None:
                value[label] = content
        if self.supersedes:
            value["supersedes"] = list(self.supersedes)
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


COMMENT_SIDES = frozenset({"LEFT", "RIGHT", "FILE"})


@dataclass(frozen=True)
class ReviewComment:
    """A proposed review finding bound to a left, right, or file location.

    ``effective_blocking`` and ``needs_human`` are C2 runtime-only admission
    fields. They are omitted from ``to_dict`` / ``ReviewResult.from_dict`` and
    from the closed v1 ``review-comment`` / ``review-result`` schemas.
    Reconstructing a result from JSON drops them; ``blocks_approval`` then
    falls back to the model proposal or legacy severity rule.
    """

    path: str
    line: int | None
    body: str
    blocking: bool | None = None
    severity: str | None = None
    category: str | None = None
    fix_effort: str | None = None
    symbol: str | None = None
    defect_kind: str | None = None
    evidence_id: str | None = None
    side: str = "RIGHT"
    effective_blocking: bool | None = None
    needs_human: bool = False

    @property
    def blocks_approval(self) -> bool:
        """Return the effective merge-impact classification for this finding.

        Trusted C2 admission sets ``effective_blocking`` and is authoritative.
        Until then an explicit model ``blocking`` value wins, and the legacy
        fallback only treats canonical severe labels as blocking.
        """

        if self.effective_blocking is not None:
            return self.effective_blocking
        if self.blocking is not None:
            return self.blocking
        return self.severity is not None and self.severity.lower() in {
            "critical",
            "high",
        }

    def __post_init__(self) -> None:
        validate_repository_path(self.path, label="comment path")
        if self.side not in COMMENT_SIDES:
            raise ReviewInputError("comment side must be LEFT, RIGHT, or FILE")
        if self.side == "FILE":
            if self.line is not None:
                raise ReviewInputError("file-level comments must omit line")
        else:
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
        for label, value in (
            ("severity", self.severity),
            ("category", self.category),
            ("fix_effort", self.fix_effort),
            ("symbol", self.symbol),
            ("defect_kind", self.defect_kind),
            ("evidence_id", self.evidence_id),
        ):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ReviewInputError(f"comment {label} must be a non-empty string")
            if value is not None:
                validate_bounded_text(
                    value,
                    DEFAULT_REVIEW_LIMITS.max_model_bytes,
                    label=f"comment {label}",
                    allow_empty=False,
                )
                if not value.isprintable():
                    raise ReviewInputError(
                        f"comment {label} contains a forbidden control character"
                    )
        if self.blocking is not None and not isinstance(self.blocking, bool):
            raise ReviewInputError("comment blocking must be a boolean")
        if self.effective_blocking is not None and not isinstance(
            self.effective_blocking, bool
        ):
            raise ReviewInputError("comment effective_blocking must be a boolean")
        if not isinstance(self.needs_human, bool):
            raise ReviewInputError("comment needs_human must be a boolean")

    def to_dict(self) -> dict[str, object]:
        """Serialize the publisher-facing v1 comment.

        Runtime admission state is intentionally omitted so the closed v1
        schema stays valid and trusted evaluator output is not persisted.
        """

        value: dict[str, object] = {
            "path": self.path,
            "body": self.body,
        }
        if self.side == "FILE":
            value["side"] = "FILE"
        else:
            value["line"] = self.line
            if self.side != "RIGHT":
                value["side"] = self.side
        if self.severity is not None:
            value["severity"] = self.severity
        if self.fix_effort is not None:
            value["fix_effort"] = self.fix_effort
        if self.category is not None:
            value["category"] = self.category
        if self.blocking is not None:
            value["blocking"] = self.blocking
        if self.symbol is not None:
            value["symbol"] = self.symbol
        if self.defect_kind is not None:
            value["defect_kind"] = self.defect_kind
        if self.evidence_id is not None:
            value["evidence_id"] = self.evidence_id
        return value


@dataclass(frozen=True)
class FindingLifecycleRecord:
    """Publisher-facing finding identity and lifecycle state."""

    fingerprint: str
    state: str

    def __post_init__(self) -> None:
        if not isinstance(self.fingerprint, str) or not _SHA256.fullmatch(
            self.fingerprint
        ):
            raise ReviewInputError(
                "finding lifecycle fingerprint must be a SHA-256 digest"
            )
        if self.state not in FINDING_LIFECYCLE_STATES:
            raise ReviewInputError("finding lifecycle state is invalid")

    def to_dict(self) -> dict[str, str]:
        return {"fingerprint": self.fingerprint, "state": self.state}


@dataclass(frozen=True)
class ReviewTransaction:
    """Identity-bound admission and publication state for one logical review.

    The transaction carries no review source, prompt, or finding data.  Its
    digest fields bind the validated analysis to the effective policy and
    configuration that the publication boundary independently recomputes.
    """

    transaction_id: str
    repository: str
    pull_request: int
    base_sha: str
    head_sha: str
    policy_digest: str
    configuration_digest: str
    evidence_digest: str
    reservation_id: str
    generation: int
    phase: str = "analysis"
    result_sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.transaction_id, str) or not _SHA256.fullmatch(
            self.transaction_id
        ):
            raise ReviewInputError("review transaction id must be a SHA-256 digest")
        if not isinstance(
            self.repository, str
        ) or not _TRANSACTION_REPOSITORY.fullmatch(self.repository):
            raise ReviewInputError("review transaction repository is invalid")
        if (
            isinstance(self.pull_request, bool)
            or not isinstance(self.pull_request, int)
            or self.pull_request < 1
            or self.pull_request > 2_147_483_647
        ):
            raise ReviewInputError("review transaction pull_request is invalid")
        for label, value in (
            ("base_sha", self.base_sha),
            ("head_sha", self.head_sha),
        ):
            if not isinstance(value, str) or not _GIT_SHA.fullmatch(value):
                raise ReviewInputError(f"review transaction {label} is invalid")
        for label, value in (
            ("policy_digest", self.policy_digest),
            ("configuration_digest", self.configuration_digest),
            ("evidence_digest", self.evidence_digest),
            ("reservation_id", self.reservation_id),
        ):
            if not isinstance(value, str) or not _SHA256.fullmatch(value):
                raise ReviewInputError(
                    f"review transaction {label} must be a SHA-256 digest"
                )
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 1
            or self.generation > 2_147_483_647
        ):
            raise ReviewInputError("review transaction generation is invalid")
        if self.phase not in TRANSACTION_PHASES:
            raise ReviewInputError("review transaction phase is invalid")
        if self.result_sha256 is not None and (
            not isinstance(self.result_sha256, str)
            or not _SHA256.fullmatch(self.result_sha256)
        ):
            raise ReviewInputError("review transaction result_sha256 is invalid")
        if self.phase == "analysis" and self.result_sha256 is not None:
            raise ReviewInputError(
                "analysis transaction cannot contain a result digest"
            )
        if self.phase == "analysis_failed" and self.result_sha256 is not None:
            raise ReviewInputError(
                "failed analysis transaction cannot contain a result digest"
            )
        if (
            self.phase
            in {
                "publication_pending",
                "publication_failed",
                "publication_succeeded",
            }
            and self.result_sha256 is None
        ):
            raise ReviewInputError("publication transaction requires a result digest")
        if self.transaction_id != self.derive_id(
            repository=self.repository,
            pull_request=self.pull_request,
            base_sha=self.base_sha,
            head_sha=self.head_sha,
            policy_digest=self.policy_digest,
            configuration_digest=self.configuration_digest,
            evidence_digest=self.evidence_digest,
            reservation_id=self.reservation_id,
        ):
            raise ReviewInputError("review transaction id does not match its identity")

    @staticmethod
    def _digest(value: Mapping[str, object]) -> str:
        # Sort object keys at every level so independently reconstructed
        # trusted contexts produce the same digest regardless of mapping
        # insertion order. Array order remains meaningful for ordered stages
        # and policies.
        import json

        canonical = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def derive_id(
        cls,
        *,
        repository: str,
        pull_request: int,
        base_sha: str,
        head_sha: str,
        policy_digest: str,
        configuration_digest: str,
        evidence_digest: str,
        reservation_id: str,
    ) -> str:
        return cls._digest(
            {
                "base_sha": base_sha,
                "configuration_digest": configuration_digest,
                "evidence_digest": evidence_digest,
                "head_sha": head_sha,
                "policy_digest": policy_digest,
                "pull_request": pull_request,
                "repository": repository,
                "reservation_id": reservation_id,
            }
        )

    @classmethod
    def create(
        cls,
        *,
        repository: str,
        pull_request: int,
        base_sha: str,
        head_sha: str,
        policy_digest: str,
        configuration_digest: str,
        evidence_digest: str,
        reservation_id: str,
        generation: int,
    ) -> "ReviewTransaction":
        return cls(
            transaction_id=cls.derive_id(
                repository=repository,
                pull_request=pull_request,
                base_sha=base_sha,
                head_sha=head_sha,
                policy_digest=policy_digest,
                configuration_digest=configuration_digest,
                evidence_digest=evidence_digest,
                reservation_id=reservation_id,
            ),
            repository=repository,
            pull_request=pull_request,
            base_sha=base_sha,
            head_sha=head_sha,
            policy_digest=policy_digest,
            configuration_digest=configuration_digest,
            evidence_digest=evidence_digest,
            reservation_id=reservation_id,
            generation=generation,
        )

    @staticmethod
    def compute_configuration_digest(value: Mapping[str, object]) -> str:
        """Digest only the documented, non-secret effective configuration."""

        if (
            not isinstance(value, Mapping)
            or set(value) != _TRANSACTION_CONFIGURATION_KEYS
        ):
            raise ReviewInputError(
                "review transaction configuration must contain the documented effective fields"
            )
        return ReviewTransaction._digest(dict(value))

    @staticmethod
    def compute_policy_digest(value: Mapping[str, object]) -> str:
        if not isinstance(value, Mapping):
            raise ReviewInputError("review transaction policy must be a mapping")
        return ReviewTransaction._digest(dict(value))

    @staticmethod
    def compute_evidence_digest(value: Mapping[str, object]) -> str:
        if not isinstance(value, Mapping):
            raise ReviewInputError("review transaction evidence must be a mapping")
        return ReviewTransaction._digest(dict(value))

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "transaction_id": self.transaction_id,
            "repository": self.repository,
            "pull_request": self.pull_request,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "policy_digest": self.policy_digest,
            "configuration_digest": self.configuration_digest,
            "evidence_digest": self.evidence_digest,
            "reservation_id": self.reservation_id,
            "generation": self.generation,
            "phase": self.phase,
        }
        if self.result_sha256 is not None:
            value["result_sha256"] = self.result_sha256
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ReviewTransaction":
        if not isinstance(value, Mapping):
            raise ReviewInputError("review transaction must be an object")
        required = {
            "transaction_id",
            "repository",
            "pull_request",
            "base_sha",
            "head_sha",
            "policy_digest",
            "configuration_digest",
            "evidence_digest",
            "reservation_id",
            "generation",
            "phase",
        }
        if set(value) - required - {"result_sha256"} or not required <= set(value):
            raise ReviewInputError("review transaction has an invalid shape")
        return cls(
            transaction_id=value["transaction_id"],  # type: ignore[arg-type]
            repository=value["repository"],  # type: ignore[arg-type]
            pull_request=value["pull_request"],  # type: ignore[arg-type]
            base_sha=value["base_sha"],  # type: ignore[arg-type]
            head_sha=value["head_sha"],  # type: ignore[arg-type]
            policy_digest=value["policy_digest"],  # type: ignore[arg-type]
            configuration_digest=value["configuration_digest"],  # type: ignore[arg-type]
            evidence_digest=value["evidence_digest"],  # type: ignore[arg-type]
            reservation_id=value["reservation_id"],  # type: ignore[arg-type]
            generation=value["generation"],  # type: ignore[arg-type]
            phase=value["phase"],  # type: ignore[arg-type]
            result_sha256=value.get("result_sha256"),  # type: ignore[arg-type]
        )

    def with_result(self, result_sha256: str) -> "ReviewTransaction":
        if not isinstance(result_sha256, str) or not _SHA256.fullmatch(result_sha256):
            raise ReviewInputError("review transaction result digest is invalid")
        return ReviewTransaction(
            transaction_id=self.transaction_id,
            repository=self.repository,
            pull_request=self.pull_request,
            base_sha=self.base_sha,
            head_sha=self.head_sha,
            policy_digest=self.policy_digest,
            configuration_digest=self.configuration_digest,
            evidence_digest=self.evidence_digest,
            reservation_id=self.reservation_id,
            generation=self.generation,
            phase="publication_pending",
            result_sha256=result_sha256,
        )

    def with_phase(self, phase: str) -> "ReviewTransaction":
        if phase not in _TRANSACTION_PHASE_TRANSITIONS[self.phase]:
            raise ReviewInputError(
                f"review transaction phase transition {self.phase}->{phase} is invalid"
            )
        return ReviewTransaction(
            transaction_id=self.transaction_id,
            repository=self.repository,
            pull_request=self.pull_request,
            base_sha=self.base_sha,
            head_sha=self.head_sha,
            policy_digest=self.policy_digest,
            configuration_digest=self.configuration_digest,
            evidence_digest=self.evidence_digest,
            reservation_id=self.reservation_id,
            generation=self.generation,
            phase=phase,
            result_sha256=self.result_sha256,
        )

    def identity_matches(
        self,
        *,
        repository: str,
        pull_request: int,
        base_sha: str,
        head_sha: str,
        policy_digest: str,
        configuration_digest: str,
        evidence_digest: str,
    ) -> bool:
        return (
            self.repository == repository
            and self.pull_request == pull_request
            and self.base_sha == base_sha
            and self.head_sha == head_sha
            and self.policy_digest == policy_digest
            and self.configuration_digest == configuration_digest
            and self.evidence_digest == evidence_digest
        )

    def logical_identity_matches(self, other: "ReviewTransaction") -> bool:
        """Compare the immutable transaction identity, excluding phase/CAS state."""

        return (
            isinstance(other, ReviewTransaction)
            and self.transaction_id == other.transaction_id
            and self.repository == other.repository
            and self.pull_request == other.pull_request
            and self.base_sha == other.base_sha
            and self.head_sha == other.head_sha
            and self.policy_digest == other.policy_digest
            and self.configuration_digest == other.configuration_digest
            and self.evidence_digest == other.evidence_digest
            and self.reservation_id == other.reservation_id
        )


@dataclass(frozen=True)
class ReviewResult:
    """Validated review output safe for a publisher adapter to consume."""

    summary: str
    comments: tuple[ReviewComment, ...]
    provider: str
    model: str | None = None
    learning_proposals: tuple[LearningProposal, ...] = ()
    # Provider/orchestrator status is carried into publication so an artifact
    # that only contains a partial or summary pass can never be mistaken for a
    # complete review eligible for an approval event.  The review service marks
    # its validated aggregate explicitly as ``complete``.
    review_status: str = "incomplete"
    limits: ReviewLimits = DEFAULT_REVIEW_LIMITS
    source_context_coverage: object | None = None
    coverage_mode: str = "full"
    finding_lifecycles: tuple[FindingLifecycleRecord, ...] = ()
    coverage: CoverageManifest | None = None
    # Directly constructed results are not proof that every configured stage
    # ran successfully.  The service marks its validated aggregate explicitly
    # as complete; callers reconstructing a legacy artifact without this field
    # are also classified as incomplete.
    # ``legacy`` is the compatible single-pass publication mode. ``confirmed``
    # means comments were selected by deterministic evidence verification.
    evidence_policy: str = "legacy"
    transaction: ReviewTransaction | None = None

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
        if not isinstance(self.review_status, str) or self.review_status not in {
            "complete",
            "partial",
            "incomplete",
            "summary-only",
        }:
            raise ReviewInputError(
                "review_status must be complete, partial, incomplete, or summary-only"
            )
        if self.source_context_coverage is not None:
            from .context import SourceContextCoverage

            if not isinstance(self.source_context_coverage, SourceContextCoverage):
                raise ReviewInputError(
                    "review source_context_coverage must be a SourceContextCoverage"
                )
        if not isinstance(self.evidence_policy, str) or self.evidence_policy not in {
            "legacy",
            "confirmed",
        }:
            raise ReviewInputError("evidence_policy must be legacy or confirmed")
        if self.coverage_mode not in COVERAGE_MODES:
            raise ReviewInputError(
                "coverage_mode must be full, incremental, or fallback-full"
            )
        if not isinstance(self.finding_lifecycles, tuple) or any(
            not isinstance(item, FindingLifecycleRecord)
            for item in self.finding_lifecycles
        ):
            raise ReviewInputError(
                "finding_lifecycles must be a tuple of FindingLifecycleRecord values"
            )
        if len(self.finding_lifecycles) > self.limits.max_comments:
            raise ReviewInputError("review contains too many finding lifecycles")
        if self.coverage is not None and not isinstance(
            self.coverage, CoverageManifest
        ):
            raise ReviewInputError("review coverage must be a CoverageManifest value")
        if self.transaction is not None and not isinstance(
            self.transaction, ReviewTransaction
        ):
            raise ReviewInputError("review transaction must be a ReviewTransaction")
        if len(self.learning_proposals) > self.limits.max_learning_proposals:
            raise ReviewInputError("review contains too many learning proposals")

        # Exact location/body duplicates are publisher-noise.  Keep the first
        # occurrence in stage order while preserving distinct comments that
        # happen to share a path and line.
        unique_comments: list[ReviewComment] = []
        seen: set[tuple[str, int | None, str, str]] = set()
        for comment in self.comments:
            key = (comment.path, comment.line, comment.side, comment.body)
            if key in seen:
                continue
            seen.add(key)
            unique_comments.append(comment)
        object.__setattr__(self, "comments", tuple(unique_comments))
        if len(self.comments) > self.limits.max_comments:
            raise ReviewInputError("review contains too many comments")
        for comment in self.comments:
            if comment.line is not None and comment.line > self.limits.max_line_number:
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
        if (
            self.transaction is not None
            and self.transaction.result_sha256 is not None
            and self.transaction.result_sha256 != self.content_digest()
        ):
            raise ReviewInputError("review transaction result digest does not match")

    def content_digest(self) -> str:
        """Digest the canonical v1 result without its transaction envelope.

        The serialized v1 result is the digest contract: adding, removing, or
        changing a published result field requires an explicit schema and
        compatibility update rather than silently changing this projection.
        """

        value = self.to_dict()
        value.pop("transaction", None)
        return hashlib.sha256(_json_compact(value).encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "summary": self.summary,
            "comments": [comment.to_dict() for comment in self.comments],
            "provider": self.provider,
            "model": self.model,
            "learning_proposals": [
                proposal.to_dict() for proposal in self.learning_proposals
            ],
        }
        # Explicit status is part of the operational v1 artifact.  Legacy
        # complete fixtures are normalized by evaluation, while publication
        # can distinguish an omitted legacy status (parsed as incomplete) from
        # a trusted service aggregate (explicitly complete).
        value["review_status"] = self.review_status
        if self.source_context_coverage is not None:
            from .context import SourceContextCoverage

            # ``__post_init__`` already rejects any other type, so the field is
            # always emitted once set and the document cannot drift from the
            # schema's required set on read-back.
            value["source_context"] = cast(
                SourceContextCoverage, self.source_context_coverage
            ).to_dict()
        # Omitted evidence_policy is the compatible single-pass mode. Confirmed
        # results serialize the policy so publishers cannot treat unverified
        # candidates as findings.
        value["evidence_policy"] = self.evidence_policy
        # ``full`` is the legacy-compatible default and stays omitted, but
        # lifecycle records are serialized whenever they exist: a full pass also
        # carries finding identities, and dropping them here would silently
        # reset the lifecycle on read-back.
        if self.coverage_mode != "full":
            value["coverage_mode"] = self.coverage_mode
        if self.finding_lifecycles:
            value["finding_lifecycles"] = [
                item.to_dict() for item in self.finding_lifecycles
            ]
        if self.coverage is not None:
            value["coverage"] = self.coverage.to_dict()
        if self.transaction is not None:
            value["transaction"] = self.transaction.to_dict()
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ReviewResult":
        """Reconstruct a fully validated review result from canonical JSON."""

        if not isinstance(value, Mapping):
            raise ReviewInputError("review result must be a JSON object")
        summary = value.get("summary")
        comments = value.get("comments")
        provider = value.get("provider")
        model = value.get("model")
        proposals = value.get("learning_proposals", [])
        # Missing status is a legacy/incomplete artifact and must fail closed
        # in publication/approval paths.
        review_status = value.get("review_status", "incomplete")
        evidence_policy = value.get("evidence_policy", "legacy")
        coverage_mode = value.get("coverage_mode", "full")
        lifecycles = value.get("finding_lifecycles", [])
        if not isinstance(summary, str):
            raise ReviewInputError("review result summary must be a string")
        if not isinstance(comments, list):
            raise ReviewInputError("review result comments must be an array")
        if not isinstance(provider, str):
            raise ReviewInputError("review result provider must be a string")
        if model is not None and not isinstance(model, str):
            raise ReviewInputError("review result model must be a string or null")
        if not isinstance(proposals, list):
            raise ReviewInputError("review result learning_proposals must be an array")
        if not isinstance(review_status, str):
            raise ReviewInputError("review result review_status must be a string")
        if not isinstance(evidence_policy, str):
            raise ReviewInputError("review result evidence_policy must be a string")
        if evidence_policy not in {"legacy", "confirmed"}:
            raise ReviewInputError(
                "review result evidence_policy must be legacy or confirmed"
            )
        if not isinstance(coverage_mode, str):
            raise ReviewInputError("review result coverage_mode must be a string")
        if not isinstance(lifecycles, list):
            raise ReviewInputError("review result finding_lifecycles must be an array")
        comment_values: list[ReviewComment] = []
        for index, comment in enumerate(comments):
            if not isinstance(comment, Mapping):
                raise ReviewInputError(
                    f"review result comment {index} must be an object"
                )
            path = comment.get("path")
            line = comment.get("line")
            body = comment.get("body")
            side = comment.get("side", "RIGHT")
            if not isinstance(path, str) or not isinstance(body, str):
                raise ReviewInputError(
                    f"review result comment {index} has an invalid shape"
                )
            if side is None:
                side = "RIGHT"
            if not isinstance(side, str):
                raise ReviewInputError(
                    f"review result comment {index} side must be a string"
                )
            if side == "FILE":
                if line is not None:
                    raise ReviewInputError(
                        f"review result comment {index} has an invalid shape"
                    )
            elif not isinstance(line, int) or isinstance(line, bool):
                raise ReviewInputError(
                    f"review result comment {index} has an invalid shape"
                )
            fix_effort = comment.get("fix_effort")
            if fix_effort is not None and not isinstance(fix_effort, str):
                raise ReviewInputError(
                    f"review result comment {index} fix_effort must be a string"
                )
            blocking = comment.get("blocking")
            if blocking is not None and not isinstance(blocking, bool):
                raise ReviewInputError(
                    f"review result comment {index} blocking must be a boolean"
                )
            symbol = comment.get("symbol")
            if symbol is not None and not isinstance(symbol, str):
                raise ReviewInputError(
                    f"review result comment {index} symbol must be a string"
                )
            defect_kind = comment.get("defect_kind")
            if defect_kind is not None and not isinstance(defect_kind, str):
                raise ReviewInputError(
                    f"review result comment {index} defect_kind must be a string"
                )
            evidence_id = comment.get("evidence_id")
            if evidence_id is not None and not isinstance(evidence_id, str):
                raise ReviewInputError(
                    f"review result comment {index} evidence_id must be a string"
                )
            # effective_blocking / needs_human are runtime-only and must not
            # be restored from a v1 document.
            comment_values.append(
                ReviewComment(
                    path=path,
                    line=line
                    if isinstance(line, int) and not isinstance(line, bool)
                    else None,
                    body=body,
                    side=side,
                    severity=(
                        comment.get("severity")
                        if isinstance(comment.get("severity"), str)
                        else None
                    ),
                    category=(
                        comment.get("category")
                        if isinstance(comment.get("category"), str)
                        else None
                    ),
                    fix_effort=fix_effort,
                    blocking=blocking,
                    symbol=symbol,
                    defect_kind=defect_kind,
                    evidence_id=evidence_id,
                )
            )
        parsed_proposals: list[LearningProposal] = []
        for index, proposal in enumerate(proposals):
            if not isinstance(proposal, Mapping):
                raise ReviewInputError(
                    f"review result learning proposal {index} must be an object"
                )
            parsed_proposals.append(LearningProposal.from_dict(proposal))
        source_context_coverage = None
        raw_source_context = value.get("source_context")
        if raw_source_context is not None:
            from .context import ContextLoadError, SourceContextCoverage

            try:
                if not isinstance(raw_source_context, Mapping):
                    raise ContextLoadError("source context coverage must be an object")
                source_context_coverage = SourceContextCoverage.from_dict(
                    raw_source_context
                )
            except ContextLoadError as exc:
                raise ReviewInputError(
                    "review result source_context is invalid"
                ) from exc
        parsed_lifecycles: list[FindingLifecycleRecord] = []
        for index, item in enumerate(lifecycles):
            if not isinstance(item, Mapping):
                raise ReviewInputError(
                    f"review result finding lifecycle {index} must be an object"
                )
            fingerprint = item.get("fingerprint")
            state = item.get("state")
            if not isinstance(fingerprint, str) or not isinstance(state, str):
                raise ReviewInputError(
                    f"review result finding lifecycle {index} has an invalid shape"
                )
            parsed_lifecycles.append(
                FindingLifecycleRecord(fingerprint=fingerprint, state=state)
            )
        coverage_value = value.get("coverage")
        coverage = (
            CoverageManifest.from_dict(coverage_value)
            if isinstance(coverage_value, Mapping)
            else None
        )
        if coverage_value is not None and coverage is None:
            raise ReviewInputError("review result coverage must be an object")
        transaction_value = value.get("transaction")
        transaction = (
            ReviewTransaction.from_dict(transaction_value)
            if isinstance(transaction_value, Mapping)
            else None
        )
        if transaction_value is not None and transaction is None:
            raise ReviewInputError("review result transaction must be an object")
        return cls(
            summary=summary,
            comments=tuple(comment_values),
            provider=provider,
            model=cast(str | None, model),
            learning_proposals=tuple(parsed_proposals),
            review_status=review_status,
            source_context_coverage=source_context_coverage,
            evidence_policy=evidence_policy,
            coverage_mode=coverage_mode,
            finding_lifecycles=tuple(parsed_lifecycles),
            coverage=coverage,
            transaction=transaction,
        )


@dataclass(frozen=True)
class ConversationMessage:
    """One bounded thread message used as untrusted conversation context."""

    author: str
    body: str
    created_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.author, str) or not self.author.strip():
            raise ReviewInputError("conversation author must be a non-empty string")
        utf8_size(self.author, label="conversation author")
        if not isinstance(self.body, str) or not self.body.strip():
            raise ReviewInputError("conversation body must be a non-empty string")
        validate_bounded_text(
            self.body,
            DEFAULT_REVIEW_LIMITS.max_comment_body_bytes,
            label="conversation body",
            allow_empty=False,
        )
        if not isinstance(self.created_at, str) or not self.created_at.strip():
            raise ReviewInputError("conversation created_at must be non-empty")
        utf8_size(self.created_at, label="conversation created_at")

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ConversationMessage":
        if not isinstance(value, Mapping):
            raise ReviewInputError("conversation message must be a JSON object")
        author = value.get("author")
        body = value.get("body")
        created_at = value.get("created_at")
        if (
            not isinstance(author, str)
            or not isinstance(body, str)
            or not isinstance(created_at, str)
        ):
            raise ReviewInputError("conversation message has an invalid shape")
        return cls(author=author, body=body, created_at=created_at)

    def to_dict(self) -> dict[str, str]:
        return {
            "author": self.author,
            "body": self.body,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class ConversationFinding:
    """One bounded prior App finding for the same pull-request head."""

    body: str
    path: str | None = None
    line: int | None = None

    def __post_init__(self) -> None:
        validate_bounded_text(
            self.body,
            DEFAULT_REVIEW_LIMITS.max_metadata_value_bytes,
            label="conversation finding body",
            allow_empty=False,
        )
        if self.path is not None:
            validate_repository_path(self.path, label="conversation finding path")
        if self.line is not None and (
            isinstance(self.line, bool)
            or not isinstance(self.line, int)
            or self.line < 1
            or self.line > DEFAULT_REVIEW_LIMITS.max_line_number
        ):
            raise ReviewInputError("conversation finding line is invalid")

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ConversationFinding":
        if not isinstance(value, Mapping):
            raise ReviewInputError("conversation finding must be a JSON object")
        body = value.get("body")
        path = value.get("path")
        line = value.get("line")
        if not isinstance(body, str):
            raise ReviewInputError("conversation finding body must be a string")
        if path is not None and not isinstance(path, str):
            raise ReviewInputError("conversation finding path must be a string")
        return cls(body=body, path=path, line=cast(int | None, line))

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {"body": self.body}
        if self.path is not None:
            value["path"] = self.path
        if self.line is not None:
            value["line"] = self.line
        return value


@dataclass(frozen=True)
class ConversationContext:
    """Bounded provider-neutral input to a mention-reply conversation."""

    messages: tuple[ConversationMessage, ...] = ()
    pull_request_number: int | None = None
    head_sha: str | None = None
    pull_request_title: str | None = None
    pull_request_body: str | None = None
    base_ref: str | None = None
    base_sha: str | None = None
    head_ref: str | None = None
    diff_context: str | None = None
    prior_findings: tuple[ConversationFinding, ...] = ()
    learnings: tuple[LearningEntry, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.messages, tuple) or any(
            not isinstance(message, ConversationMessage) for message in self.messages
        ):
            raise ReviewInputError(
                "conversation messages must be a tuple of ConversationMessage values"
            )
        if len(self.messages) > MAX_CONVERSATION_MESSAGES:
            raise ReviewInputError("conversation contains too many messages")
        if self.pull_request_number is not None and (
            isinstance(self.pull_request_number, bool)
            or not isinstance(self.pull_request_number, int)
            or self.pull_request_number < 1
        ):
            raise ReviewInputError("conversation pull_request_number is invalid")
        if self.head_sha is not None:
            if not isinstance(self.head_sha, str) or not _GIT_SHA.fullmatch(
                self.head_sha
            ):
                raise ReviewInputError("conversation head_sha must be a Git commit sha")
        if self.base_sha is not None:
            if not isinstance(self.base_sha, str) or not _GIT_SHA.fullmatch(
                self.base_sha
            ):
                raise ReviewInputError("conversation base_sha must be a Git commit sha")
        for label, value, maximum in (
            (
                "pull request title",
                self.pull_request_title,
                DEFAULT_REVIEW_LIMITS.max_title_bytes,
            ),
            (
                "pull request body",
                self.pull_request_body,
                MAX_CONVERSATION_PR_BODY_BYTES,
            ),
            ("base ref", self.base_ref, DEFAULT_REVIEW_LIMITS.max_repository_bytes),
            ("head ref", self.head_ref, DEFAULT_REVIEW_LIMITS.max_repository_bytes),
            ("diff context", self.diff_context, MAX_CONVERSATION_DIFF_BYTES),
        ):
            if value is not None:
                validate_bounded_text(
                    value,
                    maximum,
                    label=f"conversation {label}",
                    allow_empty=False,
                )
        if not isinstance(self.prior_findings, tuple) or any(
            not isinstance(finding, ConversationFinding)
            for finding in self.prior_findings
        ):
            raise ReviewInputError(
                "conversation prior_findings must be ConversationFinding values"
            )
        if len(self.prior_findings) > MAX_CONVERSATION_FINDINGS:
            raise ReviewInputError("conversation contains too many prior findings")
        if not isinstance(self.learnings, tuple) or any(
            not isinstance(learning, LearningEntry) for learning in self.learnings
        ):
            raise ReviewInputError(
                "conversation learnings must be a tuple of LearningEntry values"
            )
        if any(learning.status != "active" for learning in self.learnings):
            raise ReviewInputError("conversation learnings must be active")
        if len(self.learnings) > DEFAULT_REVIEW_LIMITS.max_learning_entries:
            raise ReviewInputError("conversation contains too many learnings")
        context_size = sum(
            utf8_size(message.author, label="conversation author")
            + utf8_size(message.body, label="conversation body")
            + utf8_size(message.created_at, label="conversation created_at")
            for message in self.messages
        )
        context_size += sum(
            utf8_size(value, label="conversation context value")
            for value in (
                self.pull_request_title,
                self.pull_request_body,
                self.base_ref,
                self.base_sha,
                self.head_ref,
                self.diff_context,
            )
            if value is not None
        )
        context_size += sum(
            utf8_size(_json_compact(finding.to_dict()), label="conversation finding")
            for finding in self.prior_findings
        )
        context_size += sum(
            utf8_size(
                _json_compact(learning.to_prompt_dict()), label="conversation learning"
            )
            for learning in self.learnings
        )
        if context_size > MAX_CONVERSATION_CONTEXT_BYTES:
            raise ReviewInputError("conversation context exceeds the size limit")

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ConversationContext":
        if not isinstance(value, Mapping):
            raise ReviewInputError("conversation context must be a JSON object")
        messages = value.get("messages", [])
        if not isinstance(messages, list):
            raise ReviewInputError("conversation messages must be an array")
        parsed_messages = tuple(
            ConversationMessage.from_dict(message) for message in messages
        )
        prior_findings = value.get("prior_findings", [])
        learnings = value.get("learnings", [])
        if not isinstance(prior_findings, list):
            raise ReviewInputError("conversation prior_findings must be an array")
        if not isinstance(learnings, list):
            raise ReviewInputError("conversation learnings must be an array")
        return cls(
            messages=parsed_messages,
            pull_request_number=cast(int | None, value.get("pull_request_number")),
            head_sha=cast(str | None, value.get("head_sha")),
            pull_request_title=cast(str | None, value.get("pull_request_title")),
            pull_request_body=cast(str | None, value.get("pull_request_body")),
            base_ref=cast(str | None, value.get("base_ref")),
            base_sha=cast(str | None, value.get("base_sha")),
            head_ref=cast(str | None, value.get("head_ref")),
            diff_context=cast(str | None, value.get("diff_context")),
            prior_findings=tuple(
                ConversationFinding.from_dict(finding) for finding in prior_findings
            ),
            learnings=tuple(
                LearningEntry.from_dict(learning) for learning in learnings
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "messages": [message.to_dict() for message in self.messages],
            "pull_request_number": self.pull_request_number,
            "head_sha": self.head_sha,
            "pull_request_title": self.pull_request_title,
            "pull_request_body": self.pull_request_body,
            "base_ref": self.base_ref,
            "base_sha": self.base_sha,
            "head_ref": self.head_ref,
            "diff_context": self.diff_context,
            "prior_findings": [finding.to_dict() for finding in self.prior_findings],
            "learnings": [learning.to_dict() for learning in self.learnings],
        }


@dataclass(frozen=True)
class ConversationReply:
    """Validated provider-produced reply and thread-resolution decision.

    ``resolve`` is deliberately opt-in: an omitted or false value keeps the
    review thread open.  Only the GitHub adapter can act on the decision after
    it revalidates the exact-head, ReviewSensei-owned thread.
    """

    body: str
    resolve: bool = False

    def __post_init__(self) -> None:
        validate_bounded_text(
            self.body,
            MAX_CONVERSATION_REPLY_BYTES,
            label="conversation reply",
            allow_empty=False,
        )
        if not isinstance(self.resolve, bool):
            raise ReviewInputError("conversation reply resolve must be a boolean")

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ConversationReply":
        if not isinstance(value, Mapping):
            raise ReviewInputError("conversation reply must be a JSON object")
        if set(value.keys()) - {"body", "resolve"}:
            raise ReviewInputError(
                "conversation reply must contain only body and resolve fields"
            )
        body = value.get("body")
        if not isinstance(body, str):
            raise ReviewInputError("conversation reply body must be a string")
        resolve = value.get("resolve", False)
        if not isinstance(resolve, bool):
            raise ReviewInputError("conversation reply resolve must be a boolean")
        return cls(body=body, resolve=resolve)

    def to_dict(self) -> dict[str, object]:
        return {"body": self.body, "resolve": self.resolve}


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
    revision: str | None = None

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
        if self.revision is not None:
            validate_bounded_text(
                self.revision,
                self.limits.max_revision_bytes,
                label="provider response revision",
                allow_empty=False,
            )


def _json_compact(value: object) -> str:
    # Local import avoids making JSON part of the provider-neutral model module's
    # public surface while keeping size accounting deterministic.
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
