from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .errors import ReviewInputError
from .validation import validate_repository_path

MAX_STAGE_FILES = 32
MAX_STAGE_FILE_BYTES = 128 * 1024
MAX_CATEGORY_FILES = 64
MAX_CATEGORY_FILE_BYTES = 64 * 1024
MAX_CATEGORIES_PER_STAGE = 16
MAX_FOCUS_ITEMS_PER_CATEGORY = 16
MAX_PATTERNS_PER_FIELD = 32

_CATEGORY_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_PROMPT_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")
_SUPPORTED_OUTPUTS = frozenset({"summary", "comments", "learning_proposals"})
SUPPORTED_PROMPT_PLACEHOLDERS = frozenset(
    {
        "context_text",
        "diff",
        "instructions",
        "learnings",
        "proposal_instruction",
        "pull_request",
        "repository",
        "review_categories",
        "review_context",
        "title",
    }
)


def _reject_unknown_fields(
    value: Mapping[str, Any],
    *,
    allowed: frozenset[str],
    label: str,
) -> None:
    if any(not isinstance(field, str) or field not in allowed for field in value):
        raise ReviewInputError(f"{label} contains an unsupported field")


def _validate_repository_pattern(value: object, *, label: str) -> None:
    validate_repository_path(value, pattern=True, label=label)


def _parse_patterns(value: object, *, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ReviewInputError(f"{label} must be a list of strings")
    if not value:
        raise ReviewInputError(f"{label} must contain at least one pattern")
    if len(value) > MAX_PATTERNS_PER_FIELD:
        raise ReviewInputError(f"{label} contains too many patterns")
    patterns = tuple(value)
    for pattern in patterns:
        _validate_repository_pattern(pattern, label=label)
    return patterns


@dataclass(frozen=True)
class ContextDocumentSource:
    """Trusted repository-relative document source for one review lens."""

    path: str
    include: tuple[str, ...] = ("**/*.md",)
    exclude: tuple[str, ...] = ()
    required: bool = False

    def __post_init__(self) -> None:
        validate_repository_path(self.path, label="context document path")
        if not isinstance(self.include, tuple) or not self.include:
            raise ReviewInputError("context document include must be a non-empty tuple")
        if not isinstance(self.exclude, tuple):
            raise ReviewInputError("context document exclude must be a tuple")
        if (
            len(self.include) > MAX_PATTERNS_PER_FIELD
            or len(self.exclude) > MAX_PATTERNS_PER_FIELD
        ):
            raise ReviewInputError("context document source contains too many patterns")
        for pattern in (*self.include, *self.exclude):
            _validate_repository_pattern(pattern, label="context document pattern")
        if not isinstance(self.required, bool):
            raise ReviewInputError("context document required must be a boolean")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ContextDocumentSource:
        if not isinstance(value, Mapping):
            raise ReviewInputError("context document source must be a JSON object")
        _reject_unknown_fields(
            value,
            allowed=frozenset({"path", "include", "exclude", "required"}),
            label="context document source",
        )
        path = value.get("path")
        include = value.get("include", ["**/*.md"])
        exclude = value.get("exclude", [])
        required = value.get("required", False)
        if not isinstance(path, str):
            raise ReviewInputError("context document path must be a string")
        if not isinstance(include, list) or not all(
            isinstance(item, str) for item in include
        ):
            raise ReviewInputError("context document include must be a list of strings")
        if not isinstance(exclude, list) or not all(
            isinstance(item, str) for item in exclude
        ):
            raise ReviewInputError("context document exclude must be a list of strings")
        return cls(
            path=path,
            include=tuple(include),
            exclude=tuple(exclude),
            required=required,
        )


@dataclass(frozen=True)
class ReviewCategory:
    """A stable review category and the concrete concerns it should examine."""

    id: str
    title: str
    focus: tuple[str, ...]
    applies_to: tuple[str, ...] = ("**",)
    learning_categories: tuple[str, ...] = ()
    include_uncategorized_learnings: bool = False
    document_sources: tuple[ContextDocumentSource, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _CATEGORY_ID.fullmatch(self.id):
            raise ReviewInputError(
                "review category id must be a lowercase repository-safe identifier"
            )
        if not isinstance(self.title, str) or not self.title.strip():
            raise ReviewInputError("review category title must be a non-empty string")
        if not isinstance(self.focus, tuple):
            raise ReviewInputError("review category focus must be a tuple")
        if not self.focus:
            raise ReviewInputError(
                "review category focus must contain at least one item"
            )
        if len(self.focus) > MAX_FOCUS_ITEMS_PER_CATEGORY:
            raise ReviewInputError("review category contains too many focus items")
        if any(not isinstance(item, str) or not item.strip() for item in self.focus):
            raise ReviewInputError(
                "review category focus items must be non-empty strings"
            )
        if not isinstance(self.applies_to, tuple) or not self.applies_to:
            raise ReviewInputError(
                "review category applies_to must be a non-empty tuple"
            )
        if len(self.applies_to) > MAX_PATTERNS_PER_FIELD:
            raise ReviewInputError(
                "review category applies_to contains too many patterns"
            )
        for pattern in self.applies_to:
            _validate_repository_pattern(pattern, label="review category applies_to")
        if not isinstance(self.learning_categories, tuple) or any(
            not isinstance(category, str) or not _CATEGORY_ID.fullmatch(category)
            for category in self.learning_categories
        ):
            raise ReviewInputError(
                "review category learning categories must be lowercase repository-safe ids"
            )
        if len(self.learning_categories) != len(set(self.learning_categories)):
            raise ReviewInputError("review category learning categories must be unique")
        if len(self.learning_categories) > MAX_PATTERNS_PER_FIELD:
            raise ReviewInputError(
                "review category contains too many learning categories"
            )
        if not isinstance(self.include_uncategorized_learnings, bool):
            raise ReviewInputError(
                "review category include_uncategorized learnings must be a boolean"
            )
        if not isinstance(self.document_sources, tuple) or any(
            not isinstance(source, ContextDocumentSource)
            for source in self.document_sources
        ):
            raise ReviewInputError(
                "review category document sources must be ContextDocumentSource values"
            )
        if len(self.document_sources) > MAX_PATTERNS_PER_FIELD:
            raise ReviewInputError("review category contains too many document sources")

    @property
    def uses_context(self) -> bool:
        return bool(
            self.learning_categories
            or self.include_uncategorized_learnings
            or self.document_sources
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ReviewCategory:
        """Parse a review category from JSON-compatible configuration."""

        if not isinstance(value, Mapping):
            raise ReviewInputError("review category must be a JSON object")
        _reject_unknown_fields(
            value,
            allowed=frozenset({"id", "title", "focus", "applies_to", "context"}),
            label="review category",
        )
        category_id = value.get("id")
        title = value.get("title")
        focus = value.get("focus")
        applies_to = value.get("applies_to", ["**"])
        context = value.get("context", {})
        if not isinstance(category_id, str):
            raise ReviewInputError("review category id must be a string")
        if not isinstance(title, str):
            raise ReviewInputError("review category title must be a string")
        if not isinstance(focus, list) or not all(
            isinstance(item, str) for item in focus
        ):
            raise ReviewInputError("review category focus must be a list of strings")
        if not isinstance(context, Mapping):
            raise ReviewInputError("review category context must be a JSON object")
        _reject_unknown_fields(
            context,
            allowed=frozenset({"learnings", "documents"}),
            label="review category context",
        )
        learning_context = context.get("learnings", {})
        if not isinstance(learning_context, Mapping):
            raise ReviewInputError(
                "review category context learnings must be a JSON object"
            )
        _reject_unknown_fields(
            learning_context,
            allowed=frozenset({"categories", "include_uncategorized"}),
            label="review category context learnings",
        )
        learning_categories = learning_context.get("categories", [])
        if not isinstance(learning_categories, list) or not all(
            isinstance(category, str) for category in learning_categories
        ):
            raise ReviewInputError(
                "review category context learning categories must be a list of strings"
            )
        include_uncategorized = learning_context.get("include_uncategorized", False)
        documents = context.get("documents", [])
        if not isinstance(documents, list):
            raise ReviewInputError("review category context documents must be a list")
        return cls(
            id=category_id,
            title=title.strip(),
            focus=tuple(item.strip() for item in focus),
            applies_to=_parse_patterns(applies_to, label="review category applies_to"),
            learning_categories=tuple(learning_categories),
            include_uncategorized_learnings=include_uncategorized,
            document_sources=tuple(
                ContextDocumentSource.from_dict(item) for item in documents
            ),
        )

    def to_prompt_dict(self) -> dict[str, object]:
        return {"id": self.id, "title": self.title, "focus": list(self.focus)}


class ReviewCategoryCatalog:
    """Validated reusable review categories addressable by stable id."""

    def __init__(self, categories: Iterable[ReviewCategory] = ()) -> None:
        try:
            entries = tuple(categories)
        except TypeError as exc:
            raise ReviewInputError("review category catalog must be iterable") from exc
        if any(not isinstance(category, ReviewCategory) for category in entries):
            raise ReviewInputError(
                "review category catalog must contain only ReviewCategory values"
            )
        identifiers = [category.id for category in entries]
        if len(identifiers) != len(set(identifiers)):
            raise ReviewInputError("review category catalog contains duplicate ids")
        self.categories = tuple(sorted(entries, key=lambda category: category.id))
        self._by_id = {category.id: category for category in self.categories}

    def resolve(self, category_ids: Iterable[str]) -> tuple[ReviewCategory, ...]:
        """Resolve category ids in caller-supplied order."""

        if isinstance(category_ids, (str, bytes)):
            raise ReviewInputError("stage category_ids must be an iterable of strings")
        try:
            identifiers = tuple(category_ids)
        except TypeError as exc:
            raise ReviewInputError("stage category_ids must be iterable") from exc
        if any(not isinstance(category_id, str) for category_id in identifiers):
            raise ReviewInputError("stage category_ids must contain only strings")
        if len(identifiers) != len(set(identifiers)):
            raise ReviewInputError("stage category_ids must not contain duplicates")
        try:
            return tuple(self._by_id[category_id] for category_id in identifiers)
        except KeyError as exc:
            raise ReviewInputError(
                f"stage references unknown review category '{exc.args[0]}'"
            ) from exc


@dataclass(frozen=True)
class Stage:
    """A single configured review stage."""

    name: str
    prompt_template: str
    outputs: tuple[str, ...]
    categories: tuple[ReviewCategory, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ReviewInputError("stage name must be a non-empty string")
        if (
            not isinstance(self.prompt_template, str)
            or not self.prompt_template.strip()
        ):
            raise ReviewInputError("stage prompt_template must be a non-empty string")
        if not isinstance(self.outputs, tuple):
            raise ReviewInputError("stage outputs must be a tuple")
        if not self.outputs:
            raise ReviewInputError(
                "stage outputs must contain at least one output type"
            )
        if any(not isinstance(out, str) for out in self.outputs):
            raise ReviewInputError("stage outputs must contain only strings")
        if len(self.outputs) != len(set(self.outputs)):
            raise ReviewInputError("stage outputs must not contain duplicates")
        for out in self.outputs:
            if out not in _SUPPORTED_OUTPUTS:
                raise ReviewInputError(
                    f"invalid stage output '{out}'. "
                    "Supported outputs are: 'summary', 'comments', 'learning_proposals'"
                )
        if not isinstance(self.categories, tuple) or any(
            not isinstance(category, ReviewCategory) for category in self.categories
        ):
            raise ReviewInputError(
                "stage categories must be a tuple of ReviewCategory values"
            )
        if len(self.categories) > MAX_CATEGORIES_PER_STAGE:
            raise ReviewInputError("stage contains too many review categories")
        category_ids = [category.id for category in self.categories]
        if len(category_ids) != len(set(category_ids)):
            raise ReviewInputError("stage review category ids must be unique")

        placeholders = set(_PROMPT_PLACEHOLDER.findall(self.prompt_template))
        unknown = placeholders - SUPPORTED_PROMPT_PLACEHOLDERS
        if unknown:
            raise ReviewInputError(
                f"stage prompt_template contains unsupported placeholder '{{{sorted(unknown)[0]}}}'"
            )
        if self.categories and "review_categories" not in placeholders:
            raise ReviewInputError(
                "stage prompt_template must include {review_categories} when categories are configured"
            )
        if (
            any(category.uses_context for category in self.categories)
            and "review_context" not in placeholders
        ):
            raise ReviewInputError(
                "stage prompt_template must include {review_context} when category context is configured"
            )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        category_catalog: ReviewCategoryCatalog | None = None,
    ) -> Stage:
        """Parse a stage from a dictionary configuration."""
        if not isinstance(value, Mapping):
            raise ReviewInputError("stage configuration must be a JSON object")
        _reject_unknown_fields(
            value,
            allowed=frozenset(
                {"name", "prompt_template", "outputs", "categories", "category_ids"}
            ),
            label="stage configuration",
        )
        if category_catalog is not None and not isinstance(
            category_catalog, ReviewCategoryCatalog
        ):
            raise ReviewInputError(
                "stage category_catalog must be a ReviewCategoryCatalog or None"
            )

        name = value.get("name")
        prompt_template = value.get("prompt_template")
        outputs_list = value.get("outputs")
        categories_list = value.get("categories", [])
        category_ids = value.get("category_ids", [])

        if not isinstance(name, str):
            raise ReviewInputError("stage name must be a string")
        if not isinstance(prompt_template, str):
            raise ReviewInputError("stage prompt_template must be a string")
        if not isinstance(outputs_list, list) or not all(
            isinstance(out, str) for out in outputs_list
        ):
            raise ReviewInputError("stage outputs must be a list of strings")
        if not isinstance(categories_list, list):
            raise ReviewInputError("stage categories must be a list")
        if not isinstance(category_ids, list) or not all(
            isinstance(category_id, str) for category_id in category_ids
        ):
            raise ReviewInputError("stage category_ids must be a list of strings")
        if "categories" in value and "category_ids" in value:
            raise ReviewInputError(
                "stage cannot define both categories and category_ids"
            )
        if category_ids and category_catalog is None:
            raise ReviewInputError(
                "stage category_ids require a review category catalog"
            )

        categories = (
            category_catalog.resolve(category_ids)
            if category_ids and category_catalog is not None
            else tuple(ReviewCategory.from_dict(item) for item in categories_list)
        )

        return cls(
            name=name.strip(),
            prompt_template=prompt_template,
            outputs=tuple(outputs_list),
            categories=categories,
        )


def load_review_categories_from_dir(directory: Path) -> ReviewCategoryCatalog:
    """Load reusable review-category JSON files from a trusted directory."""

    path_obj = Path(directory)
    if not path_obj.exists() or not path_obj.is_dir():
        raise OSError(
            f"Review categories directory '{directory}' does not exist or is not a directory"
        )

    files = sorted(path_obj.glob("*.json"))
    if not files:
        raise ReviewInputError(
            "review categories directory must contain at least one JSON file"
        )
    if len(files) > MAX_CATEGORY_FILES:
        raise ReviewInputError(
            "review categories directory contains too many JSON files"
        )

    categories: list[ReviewCategory] = []
    for path in files:
        if path.is_symlink() or not path.is_file():
            raise ReviewInputError(
                "review category configuration files must be regular files"
            )
        if path.stat().st_size > MAX_CATEGORY_FILE_BYTES:
            raise ReviewInputError(
                f"review category configuration file '{path.name}' exceeds the size limit"
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            categories.append(ReviewCategory.from_dict(data))
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            ValueError,
            KeyError,
            TypeError,
        ) as exc:
            raise ReviewInputError(
                f"failed to load review category configuration file '{path.name}'"
            ) from exc
    return ReviewCategoryCatalog(categories)


def load_stages_from_dir(
    directory: Path,
    *,
    category_catalog: ReviewCategoryCatalog | None = None,
) -> list[Stage]:
    """Load and parse stage configuration files from a directory.

    Files are read and sorted alphabetically to maintain a stable execution order.
    """
    path_obj = Path(directory)
    if not path_obj.exists() or not path_obj.is_dir():
        raise OSError(
            f"Stages directory '{directory}' does not exist or is not a directory"
        )

    files = sorted(path_obj.glob("*.json"))
    if not files:
        raise ReviewInputError("stages directory must contain at least one JSON file")
    if len(files) > MAX_STAGE_FILES:
        raise ReviewInputError("stages directory contains too many JSON files")

    stages: list[Stage] = []
    for path in files:
        if path.is_symlink() or not path.is_file():
            raise ReviewInputError("stage configuration files must be regular files")
        if path.stat().st_size > MAX_STAGE_FILE_BYTES:
            raise ReviewInputError(
                f"stage configuration file '{path.name}' exceeds the size limit"
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            stages.append(Stage.from_dict(data, category_catalog=category_catalog))
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            ValueError,
            KeyError,
            TypeError,
        ) as exc:
            raise ReviewInputError(
                f"failed to load stage configuration file '{path.name}'"
            ) from exc
    stage_names = [stage.name for stage in stages]
    if len(stage_names) != len(set(stage_names)):
        raise ReviewInputError("stage names must be unique")
    return stages
