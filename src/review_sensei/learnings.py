from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Iterable

from .errors import LearningLoadError, ReviewInputError
from .models import LearningEntry

DEFAULT_LEARNING_DIRECTORY = Path(".github/review-sensei/learnings")
MAX_LEARNING_FILES = 100
MAX_LEARNING_FILE_BYTES = 64 * 1024
MAX_SCOPE_WITNESSES = 256
_GLOB_TOKEN = re.compile(r"\*|\?|\[[^\]]*\]")


@dataclass(frozen=True)
class LearningDiagnostic:
    """Advisory lifecycle signal; diagnostics never mutate approved entries."""

    code: str
    entry_id: str
    related_ids: tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class LearningFeedback:
    """Opt-in, non-authoritative finding feedback for evaluation."""

    learning_id: str
    finding_id: str
    outcome: str
    note: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in {"useful", "incorrect", "obsolete", "unverified"}:
            raise LearningLoadError("learning feedback outcome is invalid")


def _glob_witnesses(patterns: Iterable[str]) -> tuple[str, ...]:
    """Generate a small, bounded set of concrete paths for glob intersection."""

    pattern_values = tuple(patterns)
    literal_segments = {
        segment
        for pattern in pattern_values
        for segment in pattern.split("/")
        if segment and not _GLOB_TOKEN.search(segment) and segment != "**"
    }
    replacements = tuple(dict.fromkeys(("x", "foo", "bar", *literal_segments)))[:8]
    witnesses: set[str] = set()
    for pattern in pattern_values:
        segment_options: list[tuple[str, ...]] = []
        for segment in pattern.split("/"):
            if segment == "**":
                segment_options.append(("", *replacements))
                continue
            if not _GLOB_TOKEN.search(segment):
                segment_options.append((segment,))
                continue
            variants = {segment}
            for token_match in _GLOB_TOKEN.finditer(segment):
                next_variants: set[str] = set()
                for variant in variants:
                    start, end = token_match.span()
                    for replacement in replacements:
                        next_variants.add(variant[:start] + replacement + variant[end:])
                variants = next_variants
                if len(variants) > MAX_SCOPE_WITNESSES:
                    variants = set(sorted(variants)[:MAX_SCOPE_WITNESSES])
            segment_options.append(tuple(sorted(variants)))
        candidates = {""}
        for options in segment_options:
            candidates = {
                "/".join(part for part in (prefix, option) if part)
                for prefix in candidates
                for option in options
            }
            if len(candidates) > MAX_SCOPE_WITNESSES:
                candidates = set(sorted(candidates)[:MAX_SCOPE_WITNESSES])
        witnesses.update(candidates)
        if len(witnesses) > MAX_SCOPE_WITNESSES:
            witnesses = set(sorted(witnesses)[:MAX_SCOPE_WITNESSES])
    return tuple(sorted(witnesses))


def _scopes_overlap(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    """Check glob scopes using concrete witnesses, never pattern-string matches."""

    patterns = (*left, *right)
    for candidate in _glob_witnesses(patterns):
        if any(fnmatchcase(candidate, left_pattern) for left_pattern in left) and any(
            fnmatchcase(candidate, right_pattern) for right_pattern in right
        ):
            return True
    return False


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
        self.all_entries = normalized
        self.entries = tuple(entry for entry in normalized if entry.status == "active")

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
            if entry.superseded_by is None
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

    def diagnostics(
        self,
        *,
        now: datetime | None = None,
        stale_after: timedelta = timedelta(days=365),
    ) -> tuple[LearningDiagnostic, ...]:
        """Return deterministic, bounded advisory diagnostics for maintainers."""

        instant = now or datetime.now(timezone.utc)
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        diagnostics: list[LearningDiagnostic] = []
        by_id = {entry.id: entry for entry in self.all_entries}
        for entry in self.all_entries:
            if entry.superseded_by and entry.superseded_by not in by_id:
                diagnostics.append(
                    LearningDiagnostic(
                        "missing-superseder", entry.id, (entry.superseded_by,)
                    )
                )
            if entry.expires_at:
                expires = datetime.fromisoformat(
                    entry.expires_at.replace("Z", "+00:00")
                )
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                if expires <= instant:
                    diagnostics.append(
                        LearningDiagnostic("stale", entry.id, detail="expired")
                    )
            elif entry.reviewed_at:
                reviewed = datetime.fromisoformat(
                    entry.reviewed_at.replace("Z", "+00:00")
                )
                if reviewed.tzinfo is None:
                    reviewed = reviewed.replace(tzinfo=timezone.utc)
                if reviewed + stale_after <= instant:
                    diagnostics.append(
                        LearningDiagnostic(
                            "stale", entry.id, detail="review date exceeded"
                        )
                    )

        # Overlapping scopes with materially different rules are advisory conflicts.
        for index, left in enumerate(self.all_entries):
            for right in self.all_entries[index + 1 :]:
                if left.rule == right.rule:
                    continue
                if (
                    left.category
                    and right.category
                    and left.category != right.category
                ):
                    # Different categories may coexist on overlapping scopes.
                    continue
                if _scopes_overlap(left.scope, right.scope):
                    diagnostics.append(
                        LearningDiagnostic("conflict", left.id, (right.id,))
                    )

        # Detect supersession cycles with linear-time color-state traversal;
        # retaining every trail would revisit exponentially many paths.
        graph = {entry.id: tuple(entry.supersedes) for entry in self.all_entries}
        color: dict[str, int] = {}
        for start in sorted(graph):
            if color.get(start, 0):
                continue
            color[start] = 1
            path = [start]
            positions = {start: 0}
            stack: list[tuple[str, int]] = [(start, 0)]
            while stack:
                current, offset = stack[-1]
                targets = graph.get(current, ())
                if offset >= len(targets):
                    stack.pop()
                    color[current] = 2
                    positions.pop(current, None)
                    path.pop()
                    continue
                target = targets[offset]
                stack[-1] = (current, offset + 1)
                if target not in graph:
                    continue
                state = color.get(target, 0)
                if state == 0:
                    color[target] = 1
                    positions[target] = len(path)
                    path.append(target)
                    stack.append((target, 0))
                elif state == 1 and target in positions:
                    cycle = tuple(path[positions[target] :]) + (target,)
                    diagnostics.append(
                        LearningDiagnostic("supersession-cycle", target, cycle)
                    )
        # Stable de-duplication and a bounded diagnostic surface.
        unique = {
            (item.code, item.entry_id, item.related_ids): item for item in diagnostics
        }
        return tuple(unique[key] for key in sorted(unique)[:MAX_LEARNING_FILES])


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
        entries.append(entry)
    try:
        return LearningStore(entries)
    except (LearningLoadError, ReviewInputError) as exc:
        raise LearningLoadError("repository learnings could not be indexed") from exc
