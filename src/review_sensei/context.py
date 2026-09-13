from __future__ import annotations

import ast
import hashlib
import json
import re
import threading
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
_SHA1 = re.compile(r"^[a-f0-9]{40}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


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


@dataclass(frozen=True)
class ContextSnapshot:
    """Immutable identity for the trusted source tree used for context."""

    revision: str = "0" * 40
    kind: str = "base"

    def __post_init__(self) -> None:
        if not isinstance(self.revision, str) or not _SHA1.fullmatch(self.revision):
            raise ContextLoadError("context snapshot revision must be a commit SHA")
        if self.kind not in {"base", "head"}:
            raise ContextLoadError("context snapshot kind must be base or head")


@dataclass(frozen=True)
class SourceContextExcerpt:
    """A bounded source excerpt with exact snapshot and symbol provenance."""

    path: str
    content: str
    snapshot: ContextSnapshot = ContextSnapshot()
    blob_sha: str = ""
    start_line: int = 1
    end_line: int = 1
    reason: str = "changed-file"
    sha256: str = ""

    def __post_init__(self) -> None:
        validate_path = self.path
        try:
            # Reuse the same canonical path policy as ReviewDocument.
            ReviewDocument(path=validate_path, content=self.content, sha256=hashlib.sha256(self.content.encode("utf-8")).hexdigest())
        except Exception as exc:
            raise ContextLoadError("source context excerpt path/content is invalid") from exc
        if not isinstance(self.snapshot, ContextSnapshot):
            raise ContextLoadError("source context excerpt snapshot is invalid")
        if not self.blob_sha:
            object.__setattr__(self, "blob_sha", _git_blob_sha(self.content))
        if not _SHA1.fullmatch(self.blob_sha):
            raise ContextLoadError("source context excerpt blob identity is invalid")
        if self.start_line < 1 or self.end_line < self.start_line:
            raise ContextLoadError("source context excerpt line range is invalid")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ContextLoadError("source context excerpt reason is required")
        expected = hashlib.sha256(self.content.encode("utf-8")).hexdigest()
        if self.sha256 and self.sha256 != expected:
            raise ContextLoadError("source context excerpt digest does not match content")
        object.__setattr__(self, "sha256", expected)


@dataclass(frozen=True)
class SourceContextSelection:
    excerpts: tuple[SourceContextExcerpt, ...] = ()
    outcomes: tuple[tuple[str, str], ...] = ()
    complete: bool = True


def _git_blob_sha(content: str) -> str:
    payload = content.encode("utf-8")
    return hashlib.sha1(f"blob {len(payload)}\0".encode() + payload).hexdigest()


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
        self.allowed_paths = tuple(allowed_paths)
        self.max_files = max_files
        self.max_bytes = max_bytes
        self.max_depth = max_depth
        if not (1 <= max_files <= MAX_CONTEXT_FILES and 1 <= max_bytes <= MAX_CONTEXT_TOTAL_BYTES):
            raise ContextLoadError("source context budgets are invalid")
        if not (0 <= max_depth <= 4):
            raise ContextLoadError("source context depth is invalid")

    def _allowed(self, path: str) -> bool:
        return any(_matches(path, pattern) for pattern in self.allowed_paths)

    def _read(self, path: Path) -> str | None:
        if _has_symlink_component(self.store.root, path) or not path.is_file():
            return None
        if _is_secret_like(path) or path.suffix.lower() not in {".py", ".pyi", ".js", ".jsx", ".ts", ".tsx"}:
            return None
        try:
            path.relative_to(self.store.root)
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError, ValueError):
            return None
        if not content.strip() or len(content.encode("utf-8")) > MAX_CONTEXT_FILE_BYTES:
            return None
        return content

    def select(self, changed_paths: Iterable[str]) -> SourceContextSelection:
        changed = tuple(sorted({path for path in changed_paths if isinstance(path, str) and path}))
        queue: list[tuple[str, int, str]] = [(path, 0, "changed-file") for path in changed]
        seen: set[str] = set()
        excerpts: list[SourceContextExcerpt] = []
        outcomes: dict[str, str] = {}
        total = 0
        while queue:
            path, depth, reason = queue.pop(0)
            if path in seen:
                continue
            seen.add(path)
            if not self._allowed(path):
                outcomes[path] = "excluded-by-policy"
                continue
            source = self.store.root / path
            content = self._read(source)
            if content is None:
                outcomes[path] = "unsupported"
                continue
            encoded = content.encode("utf-8")
            if len(excerpts) >= self.max_files or total + len(encoded) > self.max_bytes:
                outcomes[path] = "budget-exhausted"
                continue
            lines = content.splitlines()
            excerpt = SourceContextExcerpt(
                path=path,
                content=content,
                snapshot=self.snapshot,
                start_line=1,
                end_line=max(1, len(lines)),
                reason=reason,
            )
            excerpts.append(excerpt)
            total += len(encoded)
            outcomes[path] = "reviewed"
            if depth >= self.max_depth or source.suffix.lower() not in {".py", ".pyi"}:
                continue
            try:
                tree = ast.parse(content, filename=path)
            except SyntaxError:
                outcomes[path] = "partially-reviewed"
                continue
            imports: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.add(node.module.split(".")[0])
            for module in sorted(imports):
                candidate = self.store.root / (module.replace(".", "/") + ".py")
                try:
                    relative = candidate.relative_to(self.store.root).as_posix()
                except ValueError:
                    continue
                if relative not in seen:
                    queue.append((relative, depth + 1, "direct-import"))
            stem = source.stem
            for candidate in sorted(self.store.root.rglob(f"test_{stem}.py")):
                try:
                    relative = candidate.relative_to(self.store.root).as_posix()
                except ValueError:
                    continue
                if relative not in seen:
                    queue.append((relative, depth + 1, "associated-test"))
        ordered_outcomes = tuple(sorted(outcomes.items()))
        complete = all(value in {"reviewed", "partially-reviewed"} for _, value in ordered_outcomes)
        return SourceContextSelection(tuple(excerpts), ordered_outcomes, complete)


def stable_finding_fingerprint(
    *,
    evidence_id: str | None = None,
    path: str | None = None,
    symbol: str | None = None,
    defect_kind: str | None = None,
    evidence: str | None = None,
) -> str:
    """Hash stable concern identity, excluding line numbers and prose wording."""

    parts = {
        "evidence_id": evidence_id or "",
        "path": PurePosixPath(path or "").as_posix(),
        "symbol": " ".join((symbol or "").split()),
        "defect_kind": " ".join((defect_kind or "").lower().split()),
        "evidence": " ".join((evidence or "").split()),
    }
    if not any(parts.values()):
        raise ContextLoadError("finding fingerprint requires stable evidence")
    return hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


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
    if not review_complete:
        return FindingLifecycle(previous.fingerprint, "uncertain", previous.evidence)
    if evidence_confirmed:
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

    def digest(self) -> str:
        payload = json.dumps(self.__dict__, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


class ReviewContextCache:
    """Bounded in-memory cache for metadata only (never raw prompts/responses)."""

    def __init__(self, *, max_entries: int = 128) -> None:
        if max_entries < 1:
            raise ContextLoadError("context cache max_entries must be positive")
        self._max_entries = max_entries
        self._values: dict[str, tuple[object, ...]] = {}
        self._lock = threading.RLock()

    def get(self, key: ReviewContextCacheKey) -> tuple[object, ...] | None:
        with self._lock:
            return self._values.get(key.digest())

    def put(self, key: ReviewContextCacheKey, metadata: Iterable[object]) -> None:
        value = tuple(metadata)
        with self._lock:
            self._values[key.digest()] = value
            while len(self._values) > self._max_entries:
                self._values.pop(next(iter(self._values)))

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
