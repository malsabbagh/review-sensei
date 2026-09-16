from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Iterable

from .errors import LearningLoadError, ReviewInputError
from .models import LearningEntry
from .schemas import validate_public_document
from .validation import read_bounded_utf8

DEFAULT_LEARNING_DIRECTORY = Path(".github/review-sensei/learnings")
MAX_LEARNING_FILES = 100
MAX_LEARNING_FILE_BYTES = 64 * 1024
MAX_FEEDBACK_FILE_BYTES = 256 * 1024
MAX_FEEDBACK_RECORDS = 256
MAX_FEEDBACK_FINDING_ID_BYTES = 256
MAX_FEEDBACK_NOTE_BYTES = 512
MAX_SCOPE_WITNESSES = 256
FEEDBACK_SCHEMA_VERSION = "1.0"
FEEDBACK_OUTCOMES = ("useful", "incorrect", "obsolete", "unverified")
_GLOB_TOKEN = re.compile(r"\*|\?|\[[^\]]*\]")
_LEARNING_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


@dataclass(frozen=True)
class LearningDiagnostic:
    """Advisory lifecycle signal; diagnostics never mutate approved entries."""

    code: str
    entry_id: str
    related_ids: tuple[str, ...] = ()
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {"code": self.code, "entry_id": self.entry_id}
        if self.related_ids:
            value["related_ids"] = list(self.related_ids)
        if self.detail:
            value["detail"] = self.detail
        return value


@dataclass(frozen=True)
class LearningFeedback:
    """Opt-in, non-authoritative finding feedback for evaluation."""

    learning_id: str
    finding_id: str
    outcome: str
    note: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.learning_id, str) or not _LEARNING_ID.fullmatch(
            self.learning_id
        ):
            raise LearningLoadError("learning feedback learning_id is invalid")
        if not isinstance(self.finding_id, str) or not self.finding_id.strip():
            raise LearningLoadError("learning feedback finding_id is invalid")
        # The byte bound is authoritative; the schema's maxLength counts
        # characters, so a multibyte value can satisfy the schema and fail here.
        if len(self.finding_id.encode("utf-8")) > MAX_FEEDBACK_FINDING_ID_BYTES:
            raise LearningLoadError(
                "learning feedback finding_id exceeds "
                f"{MAX_FEEDBACK_FINDING_ID_BYTES} UTF-8 bytes"
            )
        if self.outcome not in FEEDBACK_OUTCOMES:
            raise LearningLoadError("learning feedback outcome is invalid")
        if self.note is not None:
            if not isinstance(self.note, str) or not self.note.strip():
                raise LearningLoadError("learning feedback note is invalid")
            if len(self.note.encode("utf-8")) > MAX_FEEDBACK_NOTE_BYTES:
                raise LearningLoadError(
                    "learning feedback note exceeds "
                    f"{MAX_FEEDBACK_NOTE_BYTES} UTF-8 bytes"
                )

    @classmethod
    def from_dict(cls, value: object) -> "LearningFeedback":
        if not isinstance(value, dict):
            raise LearningLoadError("learning feedback record must be a JSON object")
        allowed = {"learning_id", "finding_id", "outcome", "note"}
        if any(key not in allowed for key in value):
            raise LearningLoadError(
                "learning feedback record contains an unsupported field"
            )
        # Raw values reach __post_init__ so direct construction fails closed the
        # same way as the schema path instead of being coerced or dropped.
        return cls(
            learning_id=value.get("learning_id", ""),
            finding_id=value.get("finding_id", ""),
            outcome=value.get("outcome", ""),
            note=value.get("note"),
        )

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "learning_id": self.learning_id,
            "finding_id": self.finding_id,
            "outcome": self.outcome,
        }
        if self.note is not None:
            value["note"] = self.note
        return value


def _aware_utc(value: str) -> datetime:
    """Parse an ISO-8601 timestamp and normalize it to UTC."""

    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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
                        if len(next_variants) >= MAX_SCOPE_WITNESSES:
                            break
                    if len(next_variants) >= MAX_SCOPE_WITNESSES:
                        break
                variants = next_variants
            segment_options.append(tuple(sorted(variants)))
        candidates = {""}
        for options in segment_options:
            next_candidates: set[str] = set()
            for prefix in candidates:
                for option in options:
                    next_candidates.add(
                        "/".join(part for part in (prefix, option) if part)
                    )
                    if len(next_candidates) >= MAX_SCOPE_WITNESSES:
                        break
                if len(next_candidates) >= MAX_SCOPE_WITNESSES:
                    break
            candidates = next_candidates
        witnesses.update(candidates)
        if len(witnesses) > MAX_SCOPE_WITNESSES:
            witnesses = set(sorted(witnesses)[:MAX_SCOPE_WITNESSES])
    return tuple(sorted(witnesses))


def _scopes_overlap(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    """Check glob scopes using bounded concrete witnesses.

    This is a conservative, advisory heuristic: it may miss rare overlaps and
    should never be treated as an exact glob-intersection proof.
    """

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
        sorted_entries = tuple(sorted(raw_entries, key=lambda entry: entry.id))
        identifiers = [entry.id for entry in sorted_entries]
        if len(identifiers) != len(set(identifiers)):
            raise LearningLoadError("repository learnings contain duplicate ids")
        self.all_entries = sorted_entries
        self.entries = tuple(
            entry for entry in sorted_entries if entry.status == "active"
        )
        # Single selection rule for entries that may reach a review or an
        # evaluation comparison, so those paths cannot drift apart.
        self.selectable_entries = tuple(
            entry for entry in self.entries if entry.superseded_by is None
        )

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
            for entry in self.selectable_entries
            if (
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
        diagnostic_entries = tuple(
            entry for entry in self.all_entries if entry.status == "active"
        )
        for entry in self.all_entries:
            if entry.superseded_by and entry.superseded_by not in by_id:
                diagnostics.append(
                    LearningDiagnostic(
                        "missing-superseder", entry.id, (entry.superseded_by,)
                    )
                )
            if entry.expires_at:
                expires = _aware_utc(entry.expires_at)
                if expires <= instant:
                    diagnostics.append(
                        LearningDiagnostic("stale", entry.id, detail="expired")
                    )
            elif entry.reviewed_at:
                reviewed = _aware_utc(entry.reviewed_at)
                if reviewed + stale_after <= instant:
                    diagnostics.append(
                        LearningDiagnostic(
                            "stale", entry.id, detail="review date exceeded"
                        )
                    )

        # Overlapping scopes with materially different rules are advisory conflicts.
        for index, left in enumerate(diagnostic_entries):
            for right in diagnostic_entries[index + 1 :]:
                if left.rule == right.rule:
                    continue
                if left.category and right.category and left.category != right.category:
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


def learning_digest(entries: Iterable[LearningEntry]) -> str:
    """Return a SHA-256 digest of canonical approved learning content."""

    payload = [entry.to_dict() for entry in sorted(entries, key=lambda item: item.id)]
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def load_learning_feedback(path: Path) -> tuple[LearningFeedback, ...]:
    """Load opt-in finding feedback; records never become trusted review context."""

    try:
        text = read_bounded_utf8(
            path, maximum=MAX_FEEDBACK_FILE_BYTES, label="learning feedback"
        )
        value = json.loads(text)
    except (OSError, UnicodeError, ValueError, ReviewInputError) as exc:
        raise LearningLoadError("learning feedback could not be loaded") from exc
    try:
        validate_public_document(value, "learning-feedback")
    except ReviewInputError as exc:
        raise LearningLoadError("learning feedback failed schema validation") from exc
    if not isinstance(value, dict):
        raise LearningLoadError("learning feedback must be a JSON object")
    # Re-checked here so the loader stays fail-closed independent of the schema.
    if value.get("schema_version") != FEEDBACK_SCHEMA_VERSION:
        raise LearningLoadError("learning feedback schema_version is unsupported")
    records = value.get("records")
    if not isinstance(records, list) or len(records) > MAX_FEEDBACK_RECORDS:
        raise LearningLoadError("learning feedback records are invalid")
    return tuple(LearningFeedback.from_dict(item) for item in records)


def summarize_learning_feedback(
    records: Iterable[LearningFeedback],
    *,
    known_learning_ids: Iterable[str] | None = None,
) -> dict[str, object]:
    """Count feedback outcomes without treating silence as approval.

    ``known_learning_ids`` of ``None`` means no approved store was loaded. The
    summary then reports ``known_learning_ids_scope="unset"`` so an empty
    ``known_learning_ids_without_feedback`` cannot be read as "every known
    learning has feedback".
    """

    items = tuple(records)
    by_outcome = {outcome: 0 for outcome in FEEDBACK_OUTCOMES}
    seen_ids: set[str] = set()
    for item in items:
        by_outcome[item.outcome] += 1
        seen_ids.add(item.learning_id)
    known = tuple(dict.fromkeys(known_learning_ids or ()))
    without_feedback = [
        identifier for identifier in known if identifier not in seen_ids
    ]
    return {
        "record_count": len(items),
        "by_outcome": by_outcome,
        "absence_is_not_approval": True,
        "known_learning_ids_scope": "unset" if known_learning_ids is None else "store",
        "known_learning_ids_without_feedback": without_feedback,
        "trusted_for_review": False,
    }


def build_learning_diagnostic_report(
    store: LearningStore,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    """Serialize bounded lifecycle diagnostics for maintainer review."""

    diagnostics = store.diagnostics(now=now)
    return {
        "schema_version": "1.0",
        "entry_count": len(store.all_entries),
        "active_count": len(store.entries),
        "learning_digest": learning_digest(store.all_entries),
        "diagnostics": [item.to_dict() for item in diagnostics],
        "automatic_mutation": False,
        "human_decision_required": bool(diagnostics),
    }


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
