from __future__ import annotations

import json
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Iterable

from .errors import LearningLoadError, ReviewInputError
from .models import LearningEntry

DEFAULT_LEARNING_DIRECTORY = Path(".github/review-sensei/learnings")
MAX_LEARNING_FILES = 100
MAX_LEARNING_FILE_BYTES = 64 * 1024


class LearningStore:
    """Approved repository learnings available to the review service."""

    def __init__(self, entries: Iterable[LearningEntry] = ()) -> None:
        raw_entries = tuple(entries)
        if any(not isinstance(entry, LearningEntry) for entry in raw_entries):
            raise LearningLoadError("repository learnings contain an invalid entry")
        normalized = tuple(sorted(raw_entries, key=lambda entry: entry.id))
        identifiers = [entry.id for entry in normalized]
        if len(identifiers) != len(set(identifiers)):
            raise LearningLoadError("repository learnings contain duplicate ids")
        self.entries = normalized

    def for_paths(
        self,
        paths: Iterable[str],
        *,
        limit: int = MAX_LEARNING_FILES,
    ) -> tuple[LearningEntry, ...]:
        """Select active entries relevant to the changed repository paths."""

        changed_paths = tuple(path for path in paths if path)
        selected = tuple(
            entry
            for entry in self.entries
            if entry.status == "active"
            and (
                (not changed_paths and "*" in entry.scope)
                or any(
                    fnmatchcase(path, pattern)
                    for path in changed_paths
                    for pattern in entry.scope
                )
            )
        )
        if len(selected) > limit:
            raise LearningLoadError(
                "too many repository learnings apply to this review"
            )
        return selected


def load_repository_learnings(
    root: Path,
    *,
    directory: Path | str = DEFAULT_LEARNING_DIRECTORY,
) -> LearningStore:
    """Load active JSON learnings from a checked-out target-branch repository."""

    repository_root = Path(root).resolve()
    if not repository_root.is_dir():
        raise LearningLoadError("learning root must be an existing directory")

    relative_directory = Path(directory)
    if relative_directory.is_absolute() or ".." in relative_directory.parts:
        raise LearningLoadError("learning directory must stay inside the repository")
    learning_directory = (repository_root / relative_directory).resolve()
    try:
        learning_directory.relative_to(repository_root)
    except ValueError as exc:
        raise LearningLoadError(
            "learning directory must stay inside the repository"
        ) from exc
    if not learning_directory.exists():
        return LearningStore()
    if not learning_directory.is_dir():
        raise LearningLoadError("learning directory is not a directory")

    files = sorted(learning_directory.glob("*.json"))
    if len(files) > MAX_LEARNING_FILES:
        raise LearningLoadError("repository contains too many learning files")

    entries: list[LearningEntry] = []
    for path in files:
        if path.is_symlink() or not path.is_file():
            raise LearningLoadError("learning files must be regular files")
        try:
            path.resolve().relative_to(repository_root)
        except ValueError as exc:
            raise LearningLoadError(
                "learning file must stay inside the repository"
            ) from exc
        if path.stat().st_size > MAX_LEARNING_FILE_BYTES:
            raise LearningLoadError("learning file exceeds the size limit")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("root must be a JSON object")
            entry = LearningEntry.from_dict(value)
        except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
            raise LearningLoadError(f"invalid learning file: {path.name}") from exc
        if entry.status == "active":
            entries.append(entry)
    try:
        return LearningStore(entries)
    except (LearningLoadError, ReviewInputError) as exc:
        raise LearningLoadError("repository learnings could not be indexed") from exc
