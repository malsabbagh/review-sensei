from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import threading
from collections import OrderedDict, deque
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping

from .errors import ContextLoadError, ReviewInputError
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
from .validation import validate_bounded_text, validate_repository_path

MAX_CONTEXT_FILES = MAX_REVIEW_CONTEXT_FILES
MAX_CONTEXT_FILE_BYTES = MAX_REVIEW_DOCUMENT_BYTES
MAX_CONTEXT_TOTAL_BYTES = MAX_REVIEW_CONTEXT_TOTAL_BYTES
MAX_ALLOWED_CONTEXT_PATTERNS = 64
MAX_CACHE_METADATA_ITEMS = MAX_CONTEXT_FILES * 8
MAX_CACHE_METADATA_ITEM_BYTES = 512
MAX_CACHE_METADATA_TOTAL_BYTES = 4096
MAX_CACHE_REPOSITORY_BYTES = 512

# Relationship expansion is deliberately bounded independently of the byte
# and file budgets.  A single file can import a large number of modules (or be
# paired with many test locations), so allowing the pending queue to grow with
# repository size would make context selection an unbounded filesystem walk.
MAX_PENDING_SOURCE_CONTEXT_CANDIDATES = MAX_CONTEXT_FILES * 8
MAX_RELATION_CANDIDATES_PER_FILE = 64
# Keep import resolution bounded even when a source file contains malformed or
# adversarially large dotted names.  These limits are independent from the
# context file/byte budgets because filesystem probes happen before a file can
# be admitted to either budget.
MAX_IMPORT_MODULE_PARTS = 32
MAX_IMPORT_ALIASES_PER_NODE = 64
MAX_IMPORT_NODES_PER_FILE = 256
MAX_IMPORT_FILESYSTEM_PROBES_PER_FILE = 1024
MAX_CALLER_DIRECTORY_ENTRIES = 64
SUPPORTED_SYMBOL_LANGUAGES = ("python",)
_PYTHON_SOURCE_SUFFIXES = frozenset({".py", ".pyi"})
_FALLBACK_SOURCE_SUFFIXES = frozenset({".js", ".jsx", ".ts", ".tsx"})
_SOURCE_SUFFIXES = _PYTHON_SOURCE_SUFFIXES | _FALLBACK_SOURCE_SUFFIXES
_INTERFACE_BASE_NAMES = frozenset({"ABC", "Protocol"})
_INTERFACE_NAME_SUFFIXES = ("ABC", "Interface", "Protocol")


@dataclass
class _ImportResolutionBudget:
    """Mutable per-file budget used while resolving local imports."""

    probes: int = 0
    truncated: bool = False

    def consume_probe(self) -> bool:
        if self.probes >= MAX_IMPORT_FILESYSTEM_PROBES_PER_FILE:
            self.truncated = True
            return False
        self.probes += 1
        return True

    @property
    def exhausted(self) -> bool:
        return self.probes >= MAX_IMPORT_FILESYSTEM_PROBES_PER_FILE


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
_SHA1 = re.compile(r"^[a-f0-9]{40}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


def _git_blob_oid(content: str) -> str:
    """Return the Git blob object id for UTF-8 source text."""

    data = content.encode("utf-8")
    return hashlib.sha1(
        b"blob %d\0" % len(data) + data, usedforsecurity=False
    ).hexdigest()


def _slice_lines(content: str, start_line: int, end_line: int) -> str:
    """Return an inclusive 1-based line slice, or the original text."""

    lines = content.splitlines(keepends=True)
    if start_line < 1 or end_line < start_line or end_line > len(lines):
        return content
    sliced = "".join(lines[start_line - 1 : end_line])
    return sliced if sliced.strip() else content


def _symbol_span(node: ast.AST) -> tuple[int, int] | None:
    start = getattr(node, "lineno", None)
    end = getattr(node, "end_lineno", None)
    if not isinstance(start, int) or start < 1:
        return None
    if not isinstance(end, int) or end < start:
        end = start
    return start, end


def _base_identifier(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_interface_class(node: ast.ClassDef) -> bool:
    if node.name.endswith(_INTERFACE_NAME_SUFFIXES):
        return True
    return any(_base_identifier(base) in _INTERFACE_BASE_NAMES for base in node.bases)


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


def _supports_root_relative_open() -> bool:
    """Return whether root-relative ``openat`` reads are available."""

    return os.name != "nt" and hasattr(os, "O_DIRECTORY")


def _read_file_under_root_fallback(
    root: Path, path: Path, *, max_bytes: int
) -> bytes | None:
    """Read a repository file when ``openat`` is unavailable."""

    if _has_symlink_component(root, path):
        return None
    try:
        path.relative_to(root)
    except ValueError:
        return None
    try:
        with open(path, "rb") as stream:
            data = stream.read(max_bytes + 1)
    except OSError:
        return None
    if len(data) > max_bytes:
        return None
    return data


def _opened_path_within_root(root: Path, descriptor: int) -> bool:
    """Verify an opened descriptor still resolves inside ``root`` on Linux."""

    if not os.path.exists("/proc/self/fd"):
        # Non-Linux platforms rely on the root-relative O_NOFOLLOW openat walk.
        return True
    proc_entry = f"/proc/self/fd/{descriptor}"
    try:
        resolved = Path(os.readlink(proc_entry)).resolve()
        resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _read_file_under_root(root: Path, path: Path, *, max_bytes: int) -> bytes | None:
    """Read a repository file without following symlinks outside ``root``."""

    if not _supports_root_relative_open():
        return _read_file_under_root_fallback(root, path, max_bytes=max_bytes)

    try:
        relative = path.relative_to(root)
    except ValueError:
        return None
    if not relative.parts:
        return None

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    file_flags = os.O_RDONLY | nofollow
    # Every path component is opened without following symlinks.
    directory_flags = os.O_RDONLY | directory_flag | nofollow
    try:
        root_fd = os.open(root, os.O_RDONLY | directory_flag)
    except OSError:
        return None

    intermediate_fds: list[int] = []
    file_fd: int | None = None
    try:
        current_fd = root_fd
        parts = relative.parts
        for part in parts[:-1]:
            try:
                next_fd = os.open(part, directory_flags, dir_fd=current_fd)
            except OSError:
                return None
            if not _opened_path_within_root(root, next_fd):
                os.close(next_fd)
                return None
            if current_fd != root_fd:
                intermediate_fds.append(current_fd)
            current_fd = next_fd
        try:
            file_fd = os.open(parts[-1], file_flags, dir_fd=current_fd)
        except OSError:
            return None
        if not _opened_path_within_root(root, file_fd):
            return None
        try:
            if os.fstat(file_fd).st_size > max_bytes:
                return None
            with os.fdopen(file_fd, "rb") as stream:
                file_fd = None
                data = stream.read(max_bytes + 1)
            if len(data) > max_bytes:
                return None
            return data
        except OSError:
            return None
    finally:
        if file_fd is not None:
            try:
                os.close(file_fd)
            except OSError:
                pass
        for fd in intermediate_fds:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.close(root_fd)
        except OSError:
            pass


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
    source_context: SourceContextSelection | None = None


@dataclass(frozen=True)
class ContextSnapshot:
    """Immutable identity for the trusted base source tree used for context."""

    revision: str = "0" * 40
    kind: str = "base"

    def __post_init__(self) -> None:
        if not isinstance(self.revision, str) or not _SHA1.fullmatch(self.revision):
            raise ContextLoadError("context snapshot revision must be a commit SHA")
        if self.kind != "base":
            raise ContextLoadError("context snapshot kind must be base")

    def to_prompt_dict(self) -> dict[str, str]:
        return {"revision": self.revision, "kind": self.kind}


@dataclass(frozen=True)
class SourceContextExcerpt:
    """A bounded source excerpt with exact snapshot and symbol provenance."""

    path: str
    content: str
    snapshot: ContextSnapshot = ContextSnapshot()
    start_line: int = 1
    end_line: int = 1
    reason: str = "changed-file"
    sha256: str = ""
    blob_oid: str = ""

    def __post_init__(self) -> None:
        try:
            # Keep excerpt validation on the provider-neutral validation seam,
            # without constructing a second ReviewDocument just to validate a
            # path and content that are not being stored as one.
            validate_repository_path(
                self.path,
                label="source context excerpt path",
            )
            validate_bounded_text(
                self.content,
                MAX_CONTEXT_FILE_BYTES,
                label="source context excerpt content",
                allow_empty=False,
            )
            if not self.content.strip():
                raise ReviewInputError(
                    "source context excerpt content must contain non-whitespace text"
                )
        except (ReviewInputError, TypeError, ValueError) as exc:
            raise ContextLoadError(
                "source context excerpt path/content is invalid"
            ) from exc
        if not isinstance(self.snapshot, ContextSnapshot):
            raise ContextLoadError("source context excerpt snapshot is invalid")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in (self.start_line, self.end_line)
        ):
            raise ContextLoadError("source context excerpt line range is invalid")
        if self.end_line < self.start_line:
            raise ContextLoadError("source context excerpt line range is invalid")
        expected_lines = self.end_line - self.start_line + 1
        actual_lines = max(1, len(self.content.splitlines()))
        if actual_lines != expected_lines:
            raise ContextLoadError("source context excerpt line range is invalid")
        try:
            validate_bounded_text(
                self.reason,
                256,
                label="source context excerpt reason",
                allow_empty=False,
            )
            if not self.reason.strip():
                raise ReviewInputError(
                    "source context excerpt reason must contain non-whitespace text"
                )
        except ReviewInputError as exc:
            raise ContextLoadError("source context excerpt reason is required") from exc
        if not isinstance(self.sha256, str):
            raise ContextLoadError("source context excerpt digest is invalid")
        expected = hashlib.sha256(self.content.encode("utf-8")).hexdigest()
        if self.sha256 and self.sha256 != expected:
            raise ContextLoadError(
                "source context excerpt digest does not match content"
            )
        object.__setattr__(self, "sha256", expected)
        if not isinstance(self.blob_oid, str):
            raise ContextLoadError("source context excerpt blob identity is invalid")
        if self.blob_oid:
            if not _SHA1.fullmatch(self.blob_oid):
                raise ContextLoadError(
                    "source context excerpt blob identity is invalid"
                )
        else:
            object.__setattr__(self, "blob_oid", _git_blob_oid(self.content))

    def to_prompt_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "snapshot": self.snapshot.to_prompt_dict(),
            "blob_oid": self.blob_oid,
            "sha256": self.sha256,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "reason": self.reason,
            "content": self.content,
        }


@dataclass(frozen=True)
class SourceContextSelection:
    """Selected excerpts and an explicit completeness result.

    ``complete`` is conservative: an excluded, unsupported, budget-exhausted,
    syntax-invalid, or depth-limited changed/related candidate makes the
    selection incomplete so downstream approval/evaluation cannot mistake a
    bounded subset for full context coverage.
    """

    excerpts: tuple[SourceContextExcerpt, ...] = ()
    outcomes: tuple[tuple[str, str], ...] = ()
    complete: bool = True
    snapshot: ContextSnapshot = ContextSnapshot()

    def to_prompt_dict(self) -> dict[str, object]:
        return {
            "kind": "symbol-aware-source-context",
            "policy": "trusted-base",
            "untrusted_data": True,
            "languages": list(SUPPORTED_SYMBOL_LANGUAGES),
            "complete": self.complete,
            "snapshot": self.snapshot.to_prompt_dict(),
            "outcomes": [
                {"path": path, "status": status} for path, status in self.outcomes
            ],
            "excerpts": [excerpt.to_prompt_dict() for excerpt in self.excerpts],
        }

    def coverage(
        self, *, untrusted_head_sha: str | None = None
    ) -> SourceContextCoverage:
        return SourceContextCoverage(
            enabled=True,
            complete=self.complete,
            snapshot=self.snapshot,
            outcomes=self.outcomes,
            excerpt_count=len(self.excerpts),
            untrusted_head_sha=untrusted_head_sha,
        )


@dataclass(frozen=True)
class SourceContextCoverage:
    """Publisher-facing coverage for opt-in symbol-aware context.

    This record never includes source text.  Exhausted budgets, unsupported
    languages, and ambiguous parses stay visible so a bounded subset cannot be
    mistaken for complete understanding.
    """

    enabled: bool
    complete: bool
    snapshot: ContextSnapshot
    outcomes: tuple[tuple[str, str], ...] = ()
    excerpt_count: int = 0
    languages: tuple[str, ...] = SUPPORTED_SYMBOL_LANGUAGES
    untrusted_head_sha: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool) or not isinstance(self.complete, bool):
            raise ContextLoadError("source context coverage flags are invalid")
        if not isinstance(self.snapshot, ContextSnapshot):
            raise ContextLoadError("source context coverage snapshot is invalid")
        if (
            isinstance(self.excerpt_count, bool)
            or not isinstance(self.excerpt_count, int)
            or self.excerpt_count < 0
        ):
            raise ContextLoadError("source context coverage excerpt_count is invalid")
        if self.languages != SUPPORTED_SYMBOL_LANGUAGES:
            raise ContextLoadError(
                "source context coverage language set is unsupported"
            )
        if not isinstance(self.outcomes, tuple):
            raise ContextLoadError("source context coverage outcomes are invalid")
        if len(self.outcomes) > MAX_PENDING_SOURCE_CONTEXT_CANDIDATES:
            raise ContextLoadError("source context coverage has too many outcomes")
        for path, status in self.outcomes:
            try:
                validate_repository_path(path, label="source context coverage path")
                validate_bounded_text(
                    status,
                    64,
                    label="source context coverage status",
                    allow_empty=False,
                )
            except ReviewInputError as exc:
                raise ContextLoadError(
                    "source context coverage outcomes are invalid"
                ) from exc
        if self.untrusted_head_sha is not None and (
            not isinstance(self.untrusted_head_sha, str)
            or not _SHA1.fullmatch(self.untrusted_head_sha)
        ):
            raise ContextLoadError("untrusted head SHA must be a commit SHA")

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "enabled": self.enabled,
            "complete": self.complete,
            "snapshot": self.snapshot.to_prompt_dict(),
            "languages": list(self.languages),
            "excerpt_count": self.excerpt_count,
            "outcomes": [
                {"path": path, "status": status} for path, status in self.outcomes
            ],
        }
        if self.untrusted_head_sha is not None:
            value["untrusted_head_sha"] = self.untrusted_head_sha
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> SourceContextCoverage:
        if not isinstance(value, Mapping):
            raise ContextLoadError("source context coverage must be an object")
        snapshot_value = value.get("snapshot")
        if not isinstance(snapshot_value, Mapping):
            raise ContextLoadError("source context coverage snapshot is invalid")
        revision = snapshot_value.get("revision")
        kind = snapshot_value.get("kind")
        if not isinstance(revision, str) or not isinstance(kind, str):
            raise ContextLoadError("source context coverage snapshot is invalid")
        raw_outcomes = value.get("outcomes")
        if not isinstance(raw_outcomes, (list, tuple)):
            raise ContextLoadError("source context coverage outcomes are invalid")
        outcomes: list[tuple[str, str]] = []
        for item in raw_outcomes:
            if not isinstance(item, Mapping):
                raise ContextLoadError("source context coverage outcomes are invalid")
            path = item.get("path")
            status = item.get("status")
            if not isinstance(path, str) or not isinstance(status, str):
                raise ContextLoadError("source context coverage outcomes are invalid")
            outcomes.append((path, status))
        # The schema requires these fields and constrains their types, so the
        # publisher-facing boundary rejects anything else instead of coercing a
        # malformed document into a plausible-looking record.
        enabled = value.get("enabled")
        complete = value.get("complete")
        if not isinstance(enabled, bool) or not isinstance(complete, bool):
            raise ContextLoadError("source context coverage flags are invalid")
        excerpt_count = value.get("excerpt_count")
        if isinstance(excerpt_count, bool) or not isinstance(excerpt_count, int):
            raise ContextLoadError("source context coverage excerpt_count is invalid")
        languages = value.get("languages")
        if not isinstance(languages, (list, tuple)) or not all(
            isinstance(item, str) for item in languages
        ):
            raise ContextLoadError(
                "source context coverage language set is unsupported"
            )
        head_sha = value.get("untrusted_head_sha")
        if head_sha is not None and not isinstance(head_sha, str):
            raise ContextLoadError("untrusted head SHA must be a commit SHA")
        return cls(
            enabled=enabled,
            complete=complete,
            snapshot=ContextSnapshot(revision, kind),
            outcomes=tuple(outcomes),
            excerpt_count=excerpt_count,
            languages=tuple(languages),
            untrusted_head_sha=head_sha,
        )


@dataclass(frozen=True)
class SymbolAwareContextPolicy:
    """Opt-in trusted-base policy for symbol-aware source selection.

    Disabled by default so reviews keep the existing document/learning context
    boundary until an operator explicitly enables this selector.  Head source
    remains untrusted data and is never used as the snapshot or configuration.
    """

    enabled: bool = False
    allowed_paths: tuple[str, ...] = ("**",)
    max_files: int = 16
    max_bytes: int = 128 * 1024
    max_depth: int = 1
    languages: tuple[str, ...] = SUPPORTED_SYMBOL_LANGUAGES
    fail_closed_on_exhaustion: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ContextLoadError("symbol-aware context enabled flag is invalid")
        if not isinstance(self.fail_closed_on_exhaustion, bool):
            raise ContextLoadError("symbol-aware context fail-closed flag is invalid")
        if self.languages != SUPPORTED_SYMBOL_LANGUAGES:
            raise ContextLoadError("symbol-aware context language set is unsupported")
        if isinstance(self.allowed_paths, (str, bytes)) or not isinstance(
            self.allowed_paths, tuple
        ):
            raise ContextLoadError("symbol-aware context allowed_paths must be a tuple")
        if not self.allowed_paths:
            raise ContextLoadError(
                "symbol-aware context allowed_paths must be non-empty"
            )
        if len(self.allowed_paths) > MAX_ALLOWED_CONTEXT_PATTERNS:
            raise ContextLoadError("source context has too many path patterns")
        for pattern in self.allowed_paths:
            if not isinstance(pattern, str):
                raise ContextLoadError(
                    "source context allowed path patterns must be strings"
                )
            try:
                validate_repository_path(
                    pattern,
                    pattern=True,
                    label="source context allowed path",
                )
            except ReviewInputError as exc:
                raise ContextLoadError(
                    "source context allowed path pattern is invalid"
                ) from exc
        if not (
            1 <= self.max_files <= MAX_CONTEXT_FILES
            and 1 <= self.max_bytes <= MAX_CONTEXT_TOTAL_BYTES
        ):
            raise ContextLoadError("source context budgets are invalid")
        if not (0 <= self.max_depth <= 4):
            raise ContextLoadError("source context depth is invalid")


class SymbolAwareContextSelector:
    """Deterministic, bounded selector for local Python source relationships.

    Selection is intentionally static: it parses source text and never executes
    imports, hooks, tests, package managers, or language-server plugins.
    """

    def __init__(
        self,
        root: Path,
        *,
        snapshot: ContextSnapshot | None = None,
        allowed_paths: Iterable[str] = ("**",),
        max_files: int = 16,
        max_bytes: int = 128 * 1024,
        max_depth: int = 1,
    ) -> None:
        self.store = RepositoryContextStore(root)
        self.snapshot = snapshot or ContextSnapshot()
        if isinstance(allowed_paths, (str, bytes)):
            raise ContextLoadError("source context allowed_paths must be iterable")
        patterns: list[str] = []
        try:
            for pattern in allowed_paths:
                if len(patterns) >= MAX_ALLOWED_CONTEXT_PATTERNS:
                    raise ContextLoadError("source context has too many path patterns")
                if not isinstance(pattern, str):
                    raise ContextLoadError(
                        "source context allowed path patterns must be strings"
                    )
                try:
                    validate_repository_path(
                        pattern,
                        pattern=True,
                        label="source context allowed path",
                    )
                except ReviewInputError as exc:
                    raise ContextLoadError(
                        "source context allowed path pattern is invalid"
                    ) from exc
                patterns.append(pattern)
        except TypeError as exc:
            raise ContextLoadError(
                "source context allowed_paths must be iterable"
            ) from exc
        if not patterns:
            raise ContextLoadError("source context allowed_paths must be non-empty")
        self.allowed_paths = tuple(patterns)
        self.max_files = max_files
        self.max_bytes = max_bytes
        self.max_depth = max_depth
        if not (
            1 <= max_files <= MAX_CONTEXT_FILES
            and 1 <= max_bytes <= MAX_CONTEXT_TOTAL_BYTES
        ):
            raise ContextLoadError("source context budgets are invalid")
        if not (0 <= max_depth <= 4):
            raise ContextLoadError("source context depth is invalid")

    def _allowed(self, path: str) -> bool:
        return any(_matches(path, pattern) for pattern in self.allowed_paths)

    def _read(self, path: Path) -> str | None:
        if _is_secret_like(path) or path.suffix.lower() not in _SOURCE_SUFFIXES:
            return None
        try:
            path.relative_to(self.store.root)
        except ValueError:
            return None
        try:
            raw = _read_file_under_root(
                self.store.root,
                path,
                max_bytes=MAX_CONTEXT_FILE_BYTES,
            )
        except (OSError, UnicodeError, ValueError):
            return None
        if raw is None:
            return None
        try:
            content = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return None
        if not content.strip():
            return None
        return content

    def _relative_path(self, path: Path) -> str | None:
        """Return a canonical repository-relative path for a candidate."""

        try:
            relative = path.relative_to(self.store.root).as_posix()
            validate_repository_path(relative, label="source context path")
        except (ValueError, ReviewInputError):
            return None
        return relative

    def _package_root(self, source: Path) -> Path:
        """Find the nearest conventional Python package source root.

        ``src/pkg/module.py`` should resolve ``from pkg import sibling`` under
        ``src`` rather than looking only at the repository root.  Walking
        package ``__init__`` markers is bounded by the source path depth and
        does not inspect unrelated repository directories.
        """

        current = source.parent
        while current != self.store.root:
            if any(
                (current / marker).is_file()
                for marker in ("__init__.py", "__init__.pyi")
            ):
                current = current.parent
                continue
            break
        return current

    def _module_roots(self, source: Path) -> tuple[Path, ...]:
        """Return a small deterministic set of import search roots."""

        roots: list[Path] = [self.store.root]
        package_root = self._package_root(source)
        if package_root not in roots:
            roots.append(package_root)
        # Common source layouts are explicit candidates, not recursive scans.
        for name in ("src", "lib", "python"):
            candidate = self.store.root / name
            if candidate.is_dir() and candidate not in roots:
                roots.append(candidate)
        return tuple(roots)

    @staticmethod
    def _module_parts(module: str) -> tuple[str, ...]:
        """Return at most the safe expansion prefix of a dotted module name."""

        if not isinstance(module, str):
            return ()
        parts: list[str] = []
        for part in module.split("."):
            if not part:
                continue
            if len(parts) >= MAX_IMPORT_MODULE_PARTS:
                break
            parts.append(part)
        return tuple(parts)

    @staticmethod
    def _bounded_module_parts(module: str) -> tuple[tuple[str, ...], bool]:
        """Return module parts and whether the dotted name exceeded its cap."""

        if not isinstance(module, str):
            return (), True
        parts: list[str] = []
        for part in module.split("."):
            if not part:
                continue
            if len(parts) >= MAX_IMPORT_MODULE_PARTS:
                return tuple(parts), True
            parts.append(part)
        return tuple(parts), False

    def _module_candidate_paths(
        self,
        source: Path,
        module: str,
        *,
        budget: _ImportResolutionBudget | None = None,
        roots: tuple[Path, ...] | None = None,
    ) -> tuple[str, ...]:
        """Resolve a dotted module to existing local source files.

        Both regular modules and package ``__init__`` files are considered.
        Prefix modules are included so ``import pkg.sub.mod`` also supplies the
        package initializers that can define the imported symbol.
        """

        resolution = budget or _ImportResolutionBudget()
        parts, parts_truncated = self._bounded_module_parts(module)
        if parts_truncated:
            resolution.truncated = True
            return ()
        if not parts:
            return ()
        candidates: set[str] = set()
        search_roots = roots if roots is not None else self._module_roots(source)
        for prefix_length in range(1, len(parts) + 1):
            prefix = parts[:prefix_length]
            module_path = Path(*prefix)
            for root in search_roots:
                for candidate in (
                    root / module_path.with_suffix(".py"),
                    root / module_path.with_suffix(".pyi"),
                    root / module_path / "__init__.py",
                    root / module_path / "__init__.pyi",
                ):
                    if not resolution.consume_probe():
                        return tuple(sorted(candidates))
                    try:
                        if candidate.is_symlink():
                            continue
                        if not candidate.is_file():
                            continue
                    except OSError:
                        continue
                    relative = self._relative_path(candidate)
                    if relative is not None:
                        candidates.add(relative)
        return tuple(sorted(candidates))

    def _relative_module_candidates(
        self,
        source: Path,
        *,
        level: int,
        module: str | None,
        aliases: Iterable[str] = (),
        budget: _ImportResolutionBudget | None = None,
        roots: tuple[Path, ...] | None = None,
    ) -> tuple[str, ...]:
        """Resolve an ``ast.ImportFrom`` node, including relative imports."""

        resolution = budget or _ImportResolutionBudget()
        package_root = self._package_root(source)
        try:
            package_parts = source.parent.relative_to(package_root).parts
        except ValueError:
            package_parts = ()
        module_parts, module_truncated = self._bounded_module_parts(module or "")
        if module_truncated:
            resolution.truncated = True
            return ()
        alias_parts: list[tuple[str, ...]] = []
        for index, alias in enumerate(aliases):
            if index >= MAX_IMPORT_ALIASES_PER_NODE:
                resolution.truncated = True
                break
            parts, parts_truncated = self._bounded_module_parts(alias)
            if parts_truncated:
                resolution.truncated = True
                continue
            alias_parts.append(parts)
        if level == 0:
            # Absolute ``from pkg import name`` follows the same roots as a
            # regular ``import pkg`` and does not use the current package.
            base_parts: tuple[str, ...] = ()
        else:
            # ``from .x`` starts at the current package, while ``from ..x``
            # moves one package parent upward.  A level beyond the package
            # hierarchy is invalid and is intentionally ignored rather than
            # guessed.
            parent_count = level - 1
            if parent_count > len(package_parts):
                return ()
            base_parts = package_parts[: len(package_parts) - parent_count]
        names = [module_parts] if module_parts else []
        names.extend((*module_parts, *parts) for parts in alias_parts)
        # ``from . import helper`` has no module component, so aliases become
        # direct modules under the resolved package.  For ``from .pkg import
        # helper``, checking ``pkg.helper`` additionally handles submodules
        # exposed through a package without making any recursive search.
        if not module_parts:
            names = list(alias_parts)
        candidates: set[str] = set()
        for suffix in names:
            if not suffix:
                continue
            dotted = ".".join((*base_parts, *suffix))
            candidates.update(
                self._module_candidate_paths(
                    source,
                    dotted,
                    budget=resolution,
                    roots=roots,
                )
            )
            if resolution.exhausted:
                break
        return tuple(sorted(candidates))

    def _import_candidates(
        self, source: Path, tree: ast.AST
    ) -> tuple[tuple[str, ...], bool, dict[str, frozenset[str]]]:
        """Collect bounded, deterministic local candidates from Python AST.

        The second return value tells the caller that the per-file relation
        ceiling discarded candidates, so the final selection cannot claim
        complete coverage.
        """

        candidates: set[str] = set()
        resolution = _ImportResolutionBudget()
        roots = self._module_roots(source)
        relation_nodes = 0
        candidate_cap_reached = False

        named_symbols: dict[str, set[str]] = {}

        def add_candidates(values: Iterable[str], names: Iterable[str] = ()) -> bool:
            nonlocal candidate_cap_reached
            name_set = {
                name for name in names if isinstance(name, str) and name and name != "*"
            }
            for candidate in values:
                if candidate in candidates:
                    if name_set:
                        named_symbols.setdefault(candidate, set()).update(name_set)
                    continue
                if len(candidates) >= MAX_RELATION_CANDIDATES_PER_FILE:
                    candidate_cap_reached = True
                    resolution.truncated = True
                    return False
                candidates.add(candidate)
                if name_set:
                    named_symbols.setdefault(candidate, set()).update(name_set)
            return True

        for node in ast.walk(tree):
            if candidate_cap_reached or resolution.exhausted:
                resolution.truncated = True
                break
            if isinstance(node, ast.Import):
                relation_nodes += 1
                if relation_nodes > MAX_IMPORT_NODES_PER_FILE:
                    resolution.truncated = True
                    break
                for index, alias in enumerate(node.names):
                    if index >= MAX_IMPORT_ALIASES_PER_NODE:
                        resolution.truncated = True
                        break
                    if resolution.exhausted:
                        break
                    if not add_candidates(
                        self._module_candidate_paths(
                            source,
                            alias.name,
                            budget=resolution,
                            roots=roots,
                        )
                    ):
                        break
            elif isinstance(node, ast.ImportFrom):
                relation_nodes += 1
                if relation_nodes > MAX_IMPORT_NODES_PER_FILE:
                    resolution.truncated = True
                    break
                imported_names = tuple(alias.name for alias in node.names)
                if not add_candidates(
                    self._relative_module_candidates(
                        source,
                        level=node.level,
                        module=node.module,
                        aliases=imported_names,
                        budget=resolution,
                        roots=roots,
                    ),
                    imported_names,
                ):
                    break
            if resolution.exhausted or candidate_cap_reached:
                break
        return (
            tuple(sorted(candidates)),
            resolution.truncated,
            {path: frozenset(names) for path, names in sorted(named_symbols.items())},
        )

    def _associated_test_candidates(self, source: Path) -> tuple[str, ...]:
        """Return fixed-cost conventional test paths for ``source``.

        This intentionally avoids ``Path.rglob``.  Test locations are derived
        from the source's package path and a small set of conventional roots,
        keeping selection work bounded even in very large repositories.
        """

        stem = source.stem
        names = (f"test_{stem}.py", f"{stem}_test.py")
        package_root = self._package_root(source)
        try:
            package_relative = source.parent.relative_to(package_root)
        except ValueError:
            package_relative = Path()
        directories: list[Path] = [
            source.parent,
            source.parent / "tests",
            source.parent / "test",
            package_root / "tests" / package_relative,
            package_root / "test" / package_relative,
            self.store.root / "tests" / package_relative,
            self.store.root / "test" / package_relative,
            self.store.root / "tests",
            self.store.root / "test",
            self.store.root,
        ]
        candidates: set[str] = set()
        for directory in directories:
            for name in names:
                candidate = directory / name
                try:
                    if candidate.is_symlink():
                        continue
                    if not candidate.is_file():
                        continue
                except OSError:
                    continue
                relative = self._relative_path(candidate)
                if relative is not None:
                    candidates.add(relative)
        return tuple(sorted(candidates))

    def _importable_module_names(self, source: Path) -> frozenset[str]:
        """Return names other local files might use to import ``source``."""

        names = {source.stem}
        if source.stem == "__init__":
            names.add(source.parent.name)
        try:
            package_root = self._package_root(source)
            relative = source.with_suffix("").relative_to(package_root)
            dotted = ".".join(relative.parts)
            if dotted:
                names.add(dotted)
                names.add(relative.parts[-1])
        except ValueError:
            pass
        return frozenset(name for name in names if name and name != "__init__")

    def _file_imports_names(self, content: str, names: frozenset[str]) -> bool:
        """Return whether static import nodes mention any of ``names``."""

        if not names:
            return False
        try:
            tree = ast.parse(content)
        except (SyntaxError, ValueError):
            return False
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported = alias.name
                    if imported in names or imported.split(".")[-1] in names:
                        return True
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module in names or module.split(".")[-1] in names:
                    return True
                for alias in node.names:
                    if alias.name in names:
                        return True
        return False

    def _caller_candidates(self, source: Path) -> tuple[tuple[str, ...], str | None]:
        """Return bounded same-directory callers discovered by static imports.

        Directory listing is capped independently of repository size.  Only
        Python sources count toward ``MAX_CALLER_DIRECTORY_ENTRIES`` so a
        directory padded with data, fixture, or compiled files does not report
        truncation when every Python file in it was inspected.  The returned
        reason distinguishes a capped directory listing from a capped candidate
        set so coverage records which bound was reached.  The selector never
        executes the scanned files.
        """

        names = self._importable_module_names(source)
        directory = source.parent
        try:
            entries = sorted(os.listdir(directory))
        except OSError:
            return (), None
        truncated: str | None = None
        candidates: set[str] = set()
        inspected = 0
        for name in entries:
            suffix = Path(name).suffix.lower()
            if suffix not in _PYTHON_SOURCE_SUFFIXES:
                continue
            if inspected >= MAX_CALLER_DIRECTORY_ENTRIES:
                truncated = "directory-truncated"
                break
            inspected += 1
            candidate = directory / name
            if candidate == source:
                continue
            try:
                if candidate.is_symlink() or not candidate.is_file():
                    continue
            except OSError:
                continue
            content = self._read(candidate)
            if content is None:
                continue
            if self._file_imports_names(content, names):
                relative = self._relative_path(candidate)
                if relative is not None:
                    candidates.add(relative)
                    if len(candidates) >= MAX_RELATION_CANDIDATES_PER_FILE:
                        truncated = "relations-truncated"
                        break
        return tuple(sorted(candidates)), truncated

    def _named_symbol_span(
        self, tree: ast.AST, names: frozenset[str]
    ) -> tuple[int, int, bool] | None:
        """Return the union of matching top-level symbols and interface-ness."""

        if not names:
            return None
        spans: list[tuple[int, int]] = []
        is_interface = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name in names:
                span = _symbol_span(node)
                if span is not None:
                    spans.append(span)
                    is_interface = is_interface or _is_interface_class(node)
            elif (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in names
            ):
                span = _symbol_span(node)
                if span is not None:
                    spans.append(span)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in names:
                        span = _symbol_span(node)
                        if span is not None:
                            spans.append(span)
        if not spans:
            return None
        return (
            min(span[0] for span in spans),
            max(span[1] for span in spans),
            is_interface,
        )

    def _enclosing_symbol_span(
        self, tree: ast.AST, changed_lines: frozenset[int]
    ) -> tuple[int, int] | None:
        """Return enclosing function/class range covering changed lines."""

        if not changed_lines:
            return None
        spans: list[tuple[int, int]] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                span = _symbol_span(node)
                if span is None:
                    continue
                start, end = span
                if any(start <= line <= end for line in changed_lines):
                    spans.append(span)
        if not spans:
            return None
        return min(span[0] for span in spans), max(span[1] for span in spans)

    def _bounded_excerpt(
        self,
        *,
        path: str,
        content: str,
        reason: str,
        tree: ast.AST | None,
        names: frozenset[str],
        changed_lines: frozenset[int],
    ) -> tuple[str, int, int, str]:
        """Choose a bounded excerpt range without executing source."""

        lines = content.splitlines()
        start_line = 1
        end_line = max(1, len(lines))
        excerpt_reason = reason
        excerpt_content = content
        if tree is None:
            return excerpt_content, start_line, end_line, excerpt_reason
        span: tuple[int, int] | None = None
        if reason == "changed-file":
            span = self._enclosing_symbol_span(tree, changed_lines)
            if span is not None:
                excerpt_reason = "enclosing-symbol"
        elif names:
            named = self._named_symbol_span(tree, names)
            if named is not None:
                span = (named[0], named[1])
                if named[2]:
                    excerpt_reason = "interface"
        if span is not None:
            sliced = _slice_lines(content, span[0], span[1])
            if sliced.strip():
                start_line, end_line = span
                excerpt_content = sliced
        return excerpt_content, start_line, end_line, excerpt_reason

    def select(
        self,
        changed_paths: Iterable[str],
        *,
        changed_lines: Mapping[str, Iterable[int]] | None = None,
    ) -> SourceContextSelection:
        if isinstance(changed_paths, (str, bytes)):
            raise ContextLoadError("changed source paths must be iterable")
        changed_values: set[str] = set()
        changed_overflow = False
        invalid_paths = False
        inspected_changed_paths = 0
        try:
            for path in changed_paths:
                # Count every item pulled from the caller, including duplicate,
                # empty, and non-string values.  Capping only unique strings
                # lets an endless duplicate generator run forever before the
                # selector can produce its conservative incomplete result.
                if inspected_changed_paths >= MAX_PENDING_SOURCE_CONTEXT_CANDIDATES:
                    changed_overflow = True
                    break
                inspected_changed_paths += 1
                if isinstance(path, str) and path:
                    if len(changed_values) >= MAX_PENDING_SOURCE_CONTEXT_CANDIDATES:
                        changed_overflow = True
                        break
                    changed_values.add(path)
                elif path is not None and path != "":
                    invalid_paths = True
        except TypeError as exc:
            raise ContextLoadError("changed source paths must be iterable") from exc
        line_map: dict[str, frozenset[int]] = {}
        if changed_lines is not None:
            if isinstance(changed_lines, (str, bytes)) or not hasattr(
                changed_lines, "items"
            ):
                raise ContextLoadError("changed source lines must be a mapping")
            try:
                for index, (path, lines) in enumerate(changed_lines.items()):
                    if index >= MAX_PENDING_SOURCE_CONTEXT_CANDIDATES:
                        changed_overflow = True
                        break
                    if not isinstance(path, str) or not path:
                        invalid_paths = True
                        continue
                    selected_lines: set[int] = set()
                    try:
                        for line in lines:
                            if (
                                isinstance(line, int)
                                and not isinstance(line, bool)
                                and line > 0
                            ):
                                selected_lines.add(line)
                    except TypeError:
                        invalid_paths = True
                        continue
                    line_map[path] = frozenset(selected_lines)
            except (TypeError, AttributeError) as exc:
                raise ContextLoadError(
                    "changed source lines must be a mapping"
                ) from exc
        changed = tuple(sorted(changed_values))
        queue: deque[tuple[str, int, str, frozenset[str]]] = deque()
        queued: set[str] = set()
        seen: set[str] = set()
        excerpts: list[SourceContextExcerpt] = []
        outcomes: dict[str, str] = {}
        total = 0
        incomplete = changed_overflow or invalid_paths

        def enqueue(
            path: str,
            depth: int,
            reason: str,
            names: frozenset[str] = frozenset(),
        ) -> bool:
            nonlocal incomplete
            if path in seen or path in queued:
                return True
            if len(queue) >= MAX_PENDING_SOURCE_CONTEXT_CANDIDATES:
                incomplete = True
                return False
            queued.add(path)
            queue.append((path, depth, reason, names))
            return True

        for path in changed:
            if not isinstance(path, str):
                incomplete = True
                continue
            try:
                validate_repository_path(path, label="changed source path")
            except ReviewInputError:
                outcomes[path] = "unsupported"
                incomplete = True
                continue
            enqueue(path, 0, "changed-file")
        while queue:
            path, depth, reason, names = queue.popleft()
            queued.discard(path)
            if path in seen:
                continue
            seen.add(path)
            if not self._allowed(path):
                outcomes[path] = "excluded-by-policy"
                incomplete = True
                continue
            source = self.store.root / path
            content = self._read(source)
            if content is None:
                outcomes[path] = "unsupported"
                incomplete = True
                continue
            suffix = source.suffix.lower()
            tree: ast.AST | None = None
            if suffix in _PYTHON_SOURCE_SUFFIXES:
                try:
                    tree = ast.parse(content, filename=path)
                except SyntaxError:
                    tree = None
                    incomplete = True
            excerpt_content, start_line, end_line, excerpt_reason = (
                self._bounded_excerpt(
                    path=path,
                    content=content,
                    reason=reason,
                    tree=tree,
                    names=names,
                    changed_lines=line_map.get(path, frozenset()),
                )
            )
            encoded = excerpt_content.encode("utf-8")
            if len(excerpts) >= self.max_files or total + len(encoded) > self.max_bytes:
                outcomes[path] = "budget-exhausted"
                incomplete = True
                continue
            excerpt = SourceContextExcerpt(
                path=path,
                content=excerpt_content,
                snapshot=self.snapshot,
                start_line=start_line,
                end_line=end_line,
                reason=excerpt_reason,
                blob_oid=_git_blob_oid(content),
            )
            excerpts.append(excerpt)
            total += len(encoded)
            if suffix not in _PYTHON_SOURCE_SUFFIXES:
                outcomes[path] = "unsupported-language"
                incomplete = True
                continue
            if tree is None:
                outcomes[path] = "partially-reviewed"
                incomplete = True
                continue
            outcomes[path] = "reviewed"
            import_candidates, imports_truncated, imported_names = (
                self._import_candidates(source, tree)
            )
            associated_tests = self._associated_test_candidates(source)
            callers, callers_truncated = self._caller_candidates(source)
            related = tuple(
                sorted(set(import_candidates) | set(associated_tests) | set(callers))
            )
            # Record which bound was reached rather than collapsing every
            # truncation into one status.  Both causes are reported when both
            # trigger so an operator cannot read a capped graph as a complete
            # one.
            truncation_reasons: set[str] = set()
            if imports_truncated:
                truncation_reasons.add("relations-truncated")
            if callers_truncated is not None:
                truncation_reasons.add(callers_truncated)
            if truncation_reasons:
                outcomes[path] = ",".join(sorted(truncation_reasons))
                incomplete = True
            if depth >= self.max_depth:
                # We intentionally include the changed/selected file but mark
                # the selection incomplete when bounded traversal discovered
                # related files that could not be inspected at this depth.
                if any(
                    candidate not in seen and candidate not in queued
                    for candidate in related
                ):
                    if not truncation_reasons:
                        outcomes[path] = "partially-reviewed"
                    incomplete = True
                continue
            for candidate in related:
                if candidate in associated_tests:
                    reason_for_candidate = "associated-test"
                    candidate_names: frozenset[str] = frozenset()
                elif candidate in callers:
                    reason_for_candidate = "direct-caller"
                    candidate_names = frozenset()
                else:
                    reason_for_candidate = "direct-import"
                    candidate_names = imported_names.get(candidate, frozenset())
                if not enqueue(
                    candidate, depth + 1, reason_for_candidate, candidate_names
                ):
                    if not truncation_reasons:
                        outcomes[path] = "partially-reviewed"
                    incomplete = True
        ordered_outcomes = tuple(sorted(outcomes.items()))
        complete = not incomplete
        return SourceContextSelection(
            tuple(excerpts), ordered_outcomes, complete, self.snapshot
        )


def _normalized_finding_parts(
    *,
    evidence_id: str | None = None,
    path: str | None = None,
    symbol: str | None = None,
    defect_kind: str | None = None,
    evidence: str | None = None,
) -> dict[str, str]:
    if isinstance(path, PurePosixPath):
        raw_path = path.as_posix()
    elif path is None:
        raw_path = ""
    elif isinstance(path, str):
        raw_path = path
    else:
        raise ContextLoadError("finding fingerprint path must be a string")
    if raw_path:
        try:
            validate_repository_path(
                raw_path,
                label="finding fingerprint path",
            )
        except ReviewInputError as exc:
            raise ContextLoadError("finding fingerprint path is invalid") from exc
    normalized_path = PurePosixPath(raw_path).as_posix() if raw_path else ""
    return {
        "evidence_id": evidence_id or "",
        "path": normalized_path,
        "symbol": " ".join((symbol or "").split()),
        "defect_kind": " ".join((defect_kind or "").lower().split()),
        "evidence": " ".join((evidence or "").split()),
    }


def _finding_identity_digest(parts: dict[str, str]) -> str:
    return hashlib.sha256(
        json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def stable_concern_identity(
    *,
    evidence_id: str | None = None,
    path: str | None = None,
    symbol: str | None = None,
    defect_kind: str | None = None,
) -> str:
    """Hash stable concern identity without prose evidence."""

    parts = _normalized_finding_parts(
        evidence_id=evidence_id,
        path=path,
        symbol=symbol,
        defect_kind=defect_kind,
        evidence=None,
    )
    parts["evidence"] = ""
    if not any(parts.values()):
        raise ContextLoadError("finding concern identity requires stable evidence")
    return _finding_identity_digest(parts)


def stable_finding_fingerprint(
    *,
    evidence_id: str | None = None,
    path: str | None = None,
    symbol: str | None = None,
    defect_kind: str | None = None,
    evidence: str | None = None,
) -> str:
    """Hash stable concern identity, excluding line numbers and prose wording."""

    parts = _normalized_finding_parts(
        evidence_id=evidence_id,
        path=path,
        symbol=symbol,
        defect_kind=defect_kind,
        evidence=evidence,
    )
    if not any(parts.values()):
        raise ContextLoadError("finding fingerprint requires stable evidence")
    return _finding_identity_digest(parts)


@dataclass(frozen=True)
class FindingLifecycle:
    fingerprint: str
    state: str
    evidence: str | None = None

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.fingerprint):
            raise ContextLoadError("finding lifecycle fingerprint is invalid")
        if self.state not in {"new", "still-present", "fixed", "outdated", "uncertain"}:
            raise ContextLoadError("finding lifecycle state is invalid")


def reconcile_finding_lifecycle(
    previous: FindingLifecycle | None,
    current_fingerprint: str,
    *,
    current_concern: str | None = None,
    previous_concern: str | None = None,
    evidence_confirmed: bool = False,
    review_complete: bool = True,
) -> FindingLifecycle:
    """Reconcile a finding without treating model omission as proof of a fix."""

    if not _SHA256.fullmatch(current_fingerprint):
        raise ContextLoadError("current finding fingerprint is invalid")
    if previous is None:
        return FindingLifecycle(current_fingerprint, "new")
    if previous.fingerprint == current_fingerprint:
        return FindingLifecycle(current_fingerprint, "still-present")
    if previous.state in {"fixed", "outdated"}:
        return previous
    if not review_complete:
        return FindingLifecycle(previous.fingerprint, "uncertain", previous.evidence)
    if (
        evidence_confirmed
        and previous_concern is not None
        and current_concern is not None
        and previous_concern == current_concern
    ):
        return FindingLifecycle(previous.fingerprint, "fixed", previous.evidence)
    return FindingLifecycle(previous.fingerprint, "outdated", previous.evidence)


@dataclass(frozen=True)
class ReviewContextCacheKey:
    repository: str
    pull_request: int
    base_sha: str
    head_sha: str
    engine: str
    model: str
    profile: str
    stage_digest: str
    context_digest: str
    learning_digest: str

    def __post_init__(self) -> None:
        try:
            validate_bounded_text(
                self.repository,
                MAX_CACHE_REPOSITORY_BYTES,
                label="cache repository",
                allow_empty=False,
            )
        except ReviewInputError as exc:
            raise ContextLoadError("cache repository is invalid") from exc
        if (
            isinstance(self.pull_request, bool)
            or not isinstance(self.pull_request, int)
            or self.pull_request < 1
        ):
            raise ContextLoadError("cache pull request must be a positive integer")
        for label, value in (("base_sha", self.base_sha), ("head_sha", self.head_sha)):
            if not isinstance(value, str) or not _SHA1.fullmatch(value):
                raise ContextLoadError(f"cache {label} must be a commit SHA")
        for label, value in (
            ("engine", self.engine),
            ("model", self.model),
            ("profile", self.profile),
        ):
            try:
                validate_bounded_text(
                    value, 256, label=f"cache {label}", allow_empty=False
                )
            except ReviewInputError as exc:
                raise ContextLoadError(f"cache {label} is invalid") from exc
        for label, value in (
            ("stage_digest", self.stage_digest),
            ("context_digest", self.context_digest),
            ("learning_digest", self.learning_digest),
        ):
            if not isinstance(value, str) or not _SHA256.fullmatch(value):
                raise ContextLoadError(f"cache {label} must be a SHA-256 digest")

    def digest(self) -> str:
        payload = json.dumps(self.__dict__, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


class ReviewContextCache:
    """Bounded in-memory cache for metadata only (never raw prompts/responses)."""

    def __init__(self, *, max_entries: int = 128) -> None:
        if max_entries < 1:
            raise ContextLoadError("context cache max_entries must be positive")
        self._max_entries = max_entries
        self._values: OrderedDict[str, tuple[object, ...]] = OrderedDict()
        self._lock = threading.RLock()

    def get(self, key: ReviewContextCacheKey) -> tuple[object, ...] | None:
        with self._lock:
            digest = key.digest()
            value = self._values.get(digest)
            if value is not None:
                self._values.move_to_end(digest)
            return value

    def _bounded_cache_metadata(self, metadata: Iterable[object]) -> tuple[object, ...]:
        items: list[object] = []
        total_bytes = 0
        for index, item in enumerate(metadata):
            if index >= MAX_CACHE_METADATA_ITEMS:
                raise ContextLoadError(
                    "context cache metadata exceeds the bounded item limit"
                )
            if isinstance(item, str):
                try:
                    validate_bounded_text(
                        item,
                        MAX_CACHE_METADATA_ITEM_BYTES,
                        label="context cache metadata item",
                        allow_empty=True,
                    )
                except ReviewInputError as exc:
                    raise ContextLoadError(
                        "context cache metadata item is invalid"
                    ) from exc
                item_bytes = len(item.encode("utf-8"))
            elif isinstance(item, (bool, int, float)) or item is None:
                item_bytes = len(repr(item).encode("utf-8"))
            else:
                raise ContextLoadError("context cache metadata item type is invalid")
            total_bytes += item_bytes
            if total_bytes > MAX_CACHE_METADATA_TOTAL_BYTES:
                raise ContextLoadError(
                    "context cache metadata exceeds the bounded byte limit"
                )
            items.append(item)
        return tuple(items)

    def put(self, key: ReviewContextCacheKey, metadata: Iterable[object]) -> None:
        value = self._bounded_cache_metadata(metadata)
        with self._lock:
            digest = key.digest()
            self._values[digest] = value
            self._values.move_to_end(digest)
            while len(self._values) > self._max_entries:
                self._values.popitem(last=False)

    def invalidate(self, key: ReviewContextCacheKey) -> None:
        with self._lock:
            self._values.pop(key.digest(), None)

    def clear(self) -> None:
        with self._lock:
            self._values.clear()


def build_review_context_selection(
    categories: Iterable[ReviewCategory],
    *,
    changed_paths: Iterable[str],
    learnings: Iterable[LearningEntry] = (),
    context_store: RepositoryContextStore | None = None,
    source_context_policy: SymbolAwareContextPolicy | None = None,
    snapshot: ContextSnapshot | None = None,
    changed_lines: Mapping[str, Iterable[int]] | None = None,
) -> ReviewContextSelection:
    """Resolve applicable lenses and their bounded learnings/documents.

    Symbol-aware source selection is opt-in through ``source_context_policy``.
    The default remains document/learning context only.  When enabled, the
    selector reads the trusted base snapshot exclusively; head source is never
    used as configuration or as the snapshot identity.
    """

    category_values = tuple(categories)
    if any(not isinstance(category, ReviewCategory) for category in category_values):
        raise ContextLoadError("review categories contain an invalid value")
    category_ids = [category.id for category in category_values]
    if len(category_ids) != len(set(category_ids)):
        raise ContextLoadError("review categories contain duplicate ids")
    if source_context_policy is not None and not isinstance(
        source_context_policy, SymbolAwareContextPolicy
    ):
        raise ContextLoadError("source context policy is invalid")
    if snapshot is not None and not isinstance(snapshot, ContextSnapshot):
        raise ContextLoadError("source context snapshot is invalid")

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

    source_context: SourceContextSelection | None = None
    policy = source_context_policy or SymbolAwareContextPolicy()
    if policy.enabled:
        if context_store is None:
            raise ContextLoadError(
                "symbol-aware context requires a trusted context root"
            )
        if snapshot is None:
            raise ContextLoadError(
                "symbol-aware context requires a trusted base snapshot"
            )
        source_context = SymbolAwareContextSelector(
            context_store.root,
            snapshot=snapshot,
            allowed_paths=policy.allowed_paths,
            max_files=policy.max_files,
            max_bytes=policy.max_bytes,
            max_depth=policy.max_depth,
        ).select(paths, changed_lines=changed_lines)
        if policy.fail_closed_on_exhaustion and any(
            status == "budget-exhausted" for _, status in source_context.outcomes
        ):
            raise ContextLoadError(
                "symbol-aware context exhausted its configured budget"
            )

    return ReviewContextSelection(
        active_category_ids=tuple(active_ids),
        lens_contexts=tuple(lens_contexts),
        source_context=source_context,
    )
