from __future__ import annotations

import hashlib
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Iterable

from .errors import ContextLoadError
from .learnings import LearningStore
from .models import (
    MAX_REVIEW_CONTEXT_FILES,
    MAX_REVIEW_CONTEXT_TOTAL_BYTES,
    MAX_REVIEW_DOCUMENT_BYTES,
    LearningEntry,
    ReviewDocument,
    ReviewLensContext,
)
from .stages import ContextDocumentSource, ReviewCategory

MAX_CONTEXT_FILES = MAX_REVIEW_CONTEXT_FILES
MAX_CONTEXT_FILE_BYTES = MAX_REVIEW_DOCUMENT_BYTES
MAX_CONTEXT_TOTAL_BYTES = MAX_REVIEW_CONTEXT_TOTAL_BYTES

_ALLOWED_SUFFIXES = frozenset(
    {".adoc", ".json", ".md", ".rst", ".toml", ".txt", ".yaml", ".yml"}
)
_ALLOWED_EXTENSIONLESS_NAMES = frozenset(
    {"AGENTS", "CONTRIBUTING", "LICENSE", "README", "SECURITY"}
)
_SECRET_SUFFIXES = frozenset({".key", ".p12", ".pem", ".pfx"})
_SECRET_NAMES = frozenset(
    {
        "credential",
        "credentials",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "secret",
        "secrets",
    }
)


def _matches(path: str, pattern: str) -> bool:
    """Match repository paths with ``**`` spanning zero or more segments."""

    path_parts = PurePosixPath(path).parts
    pattern_parts = PurePosixPath(pattern).parts

    def globstar_closure(states: set[int]) -> set[int]:
        closed = set(states)
        pending = list(states)
        while pending:
            index = pending.pop()
            if (
                index < len(pattern_parts)
                and pattern_parts[index] == "**"
                and index + 1 not in closed
            ):
                closed.add(index + 1)
                pending.append(index + 1)
        return closed

    states = globstar_closure({0})
    for path_part in path_parts:
        next_states: set[int] = set()
        for pattern_index in states:
            if pattern_index == len(pattern_parts):
                continue
            pattern_part = pattern_parts[pattern_index]
            if pattern_part == "**":
                next_states.add(pattern_index)
            elif fnmatchcase(path_part, pattern_part):
                next_states.add(pattern_index + 1)
        states = globstar_closure(next_states)
        if not states:
            return False
    return len(pattern_parts) in globstar_closure(states)


def _is_secret_like(path: Path) -> bool:
    name = path.name.lower()
    return (
        name == ".env"
        or name.startswith(".env.")
        or name in _SECRET_NAMES
        or path.stem.lower() in _SECRET_NAMES
        or path.suffix.lower() in _SECRET_SUFFIXES
    )


def _is_supported_text_path(path: Path) -> bool:
    if path.suffix:
        return path.suffix.lower() in _ALLOWED_SUFFIXES
    return path.name.upper() in _ALLOWED_EXTENSIONLESS_NAMES


def _has_symlink_component(root: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


class RepositoryContextStore:
    """Load bounded text context from one trusted target-branch checkout."""

    def __init__(self, root: Path) -> None:
        repository_root = Path(root).resolve()
        if not repository_root.is_dir():
            raise ContextLoadError("context root must be an existing directory")
        self.root = repository_root

    def for_sources(
        self,
        sources: Iterable[ContextDocumentSource],
    ) -> tuple[ReviewDocument, ...]:
        raw_sources = tuple(sources)
        if any(not isinstance(source, ContextDocumentSource) for source in raw_sources):
            raise ContextLoadError(
                "context sources must contain only ContextDocumentSource values"
            )

        selected: dict[str, Path] = {}
        for source in raw_sources:
            unresolved = self.root / source.path
            if _has_symlink_component(self.root, unresolved):
                raise ContextLoadError("context sources must not be symlinks")
            source_path = unresolved.resolve()
            try:
                source_path.relative_to(self.root)
            except ValueError as exc:
                raise ContextLoadError(
                    "context source must stay inside the repository"
                ) from exc

            if not source_path.exists():
                if source.required:
                    raise ContextLoadError("required context source does not exist")
                continue

            candidates: list[Path]
            if source_path.is_file():
                candidates = [source_path]
            elif source_path.is_dir():
                candidates = sorted(
                    {
                        candidate
                        for pattern in source.include
                        for candidate in source_path.glob(pattern)
                        if candidate.is_file()
                    }
                )
                candidates = [
                    candidate
                    for candidate in candidates
                    if not any(
                        _matches(candidate.relative_to(source_path).as_posix(), pattern)
                        for pattern in source.exclude
                    )
                ]
                if source.required and not candidates:
                    raise ContextLoadError(
                        "required context source did not match any files"
                    )
            else:
                raise ContextLoadError("context source must be a file or directory")

            for candidate in candidates:
                if _has_symlink_component(self.root, candidate):
                    raise ContextLoadError("context documents must not be symlinks")
                resolved = candidate.resolve()
                try:
                    relative = resolved.relative_to(self.root).as_posix()
                except ValueError as exc:
                    raise ContextLoadError(
                        "context document must stay inside the repository"
                    ) from exc
                if _is_secret_like(resolved):
                    raise ContextLoadError(
                        "context document looks like a secret-bearing file"
                    )
                if not _is_supported_text_path(resolved):
                    raise ContextLoadError(
                        "context document must use a supported text format"
                    )
                selected[relative] = resolved

        if len(selected) > MAX_CONTEXT_FILES:
            raise ContextLoadError("review context contains too many documents")

        documents: list[ReviewDocument] = []
        total_bytes = 0
        for relative, path in sorted(selected.items()):
            try:
                size = path.stat().st_size
            except OSError as exc:
                raise ContextLoadError(
                    "context document metadata is not readable"
                ) from exc
            if size > MAX_CONTEXT_FILE_BYTES:
                raise ContextLoadError(
                    "context document exceeds the per-file size limit"
                )
            total_bytes += size
            if total_bytes > MAX_CONTEXT_TOTAL_BYTES:
                raise ContextLoadError("review context exceeds the total size limit")
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise ContextLoadError(
                    "context document must be readable UTF-8 text"
                ) from exc
            if not content.strip():
                raise ContextLoadError("context document must not be empty")
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            documents.append(
                ReviewDocument(path=relative, content=content, sha256=digest)
            )
        return tuple(documents)


@dataclass(frozen=True)
class ReviewContextSelection:
    """Applicable category ids and their resolved supplemental context."""

    active_category_ids: tuple[str, ...]
    lens_contexts: tuple[ReviewLensContext, ...]


def build_review_context_selection(
    categories: Iterable[ReviewCategory],
    *,
    changed_paths: Iterable[str],
    learnings: Iterable[LearningEntry] = (),
    context_store: RepositoryContextStore | None = None,
) -> ReviewContextSelection:
    """Resolve applicable lenses and their bounded learnings/documents."""

    category_values = tuple(categories)
    if any(not isinstance(category, ReviewCategory) for category in category_values):
        raise ContextLoadError("review categories contain an invalid value")
    category_ids = [category.id for category in category_values]
    if len(category_ids) != len(set(category_ids)):
        raise ContextLoadError("review categories contain duplicate ids")

    paths = tuple(path for path in changed_paths if isinstance(path, str) and path)
    relevant_learnings = LearningStore(learnings).for_paths(paths)
    active_ids: list[str] = []
    lens_contexts: list[ReviewLensContext] = []
    unique_documents: dict[str, ReviewDocument] = {}
    for category in category_values:
        if not any(
            _matches(path, pattern) for path in paths for pattern in category.applies_to
        ):
            continue
        active_ids.append(category.id)
        if not category.uses_context:
            continue

        selected_learnings = tuple(
            learning
            for learning in relevant_learnings
            if (
                learning.category in category.learning_categories
                or (
                    learning.category is None
                    and category.include_uncategorized_learnings
                )
            )
        )
        if context_store is None:
            if any(source.required for source in category.document_sources):
                raise ContextLoadError(
                    "a context root is required for a required lens document source"
                )
            documents: tuple[ReviewDocument, ...] = ()
        else:
            documents = context_store.for_sources(category.document_sources)
        for document in documents:
            existing = unique_documents.setdefault(document.path, document)
            if existing != document:
                raise ContextLoadError(
                    "a context document path resolved to inconsistent content"
                )
        if len(unique_documents) > MAX_CONTEXT_FILES:
            raise ContextLoadError("review context contains too many documents")
        if (
            sum(
                len(document.content.encode("utf-8"))
                for document in unique_documents.values()
            )
            > MAX_CONTEXT_TOTAL_BYTES
        ):
            raise ContextLoadError("review context exceeds the total size limit")
        lens_contexts.append(
            ReviewLensContext(
                category_id=category.id,
                learnings=selected_learnings,
                documents=documents,
            )
        )

    return ReviewContextSelection(
        active_category_ids=tuple(active_ids),
        lens_contexts=tuple(lens_contexts),
    )
