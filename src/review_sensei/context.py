from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import threading
from collections import deque
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Iterable

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
        except ReviewInputError as exc:
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
        if not isinstance(self.blob_sha, str):
            raise ContextLoadError("source context excerpt blob identity is invalid")
        if not self.blob_sha:
            object.__setattr__(self, "blob_sha", _git_blob_sha(self.content))
        if not _SHA1.fullmatch(self.blob_sha):
            raise ContextLoadError("source context excerpt blob identity is invalid")
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
        if _has_symlink_component(self.store.root, path):
            return None
        if _is_secret_like(path) or path.suffix.lower() not in {
            ".py",
            ".pyi",
            ".js",
            ".jsx",
            ".ts",
            ".tsx",
        }:
            return None
        try:
            path.relative_to(self.store.root)
        except ValueError:
            return None
        descriptor: int | None = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = None
                raw = stream.read(MAX_CONTEXT_FILE_BYTES + 1)
        except (OSError, UnicodeError, ValueError):
            return None
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        if len(raw) > MAX_CONTEXT_FILE_BYTES:
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
                        exists = candidate.exists()
                    except OSError:
                        exists = False
                    is_link = False
                    if not exists:
                        if not resolution.consume_probe():
                            return tuple(sorted(candidates))
                        try:
                            is_link = candidate.is_symlink()
                        except OSError:
                            is_link = False
                        if not is_link:
                            continue
                    if not exists and not is_link:
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
    ) -> tuple[tuple[str, ...], bool]:
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

        def add_candidates(values: Iterable[str]) -> bool:
            nonlocal candidate_cap_reached
            for candidate in values:
                if candidate in candidates:
                    continue
                if len(candidates) >= MAX_RELATION_CANDIDATES_PER_FILE:
                    candidate_cap_reached = True
                    resolution.truncated = True
                    return False
                candidates.add(candidate)
            return True

        for node in ast.walk(tree):
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
                if not add_candidates(
                    self._relative_module_candidates(
                        source,
                        level=node.level,
                        module=node.module,
                        aliases=(alias.name for alias in node.names),
                        budget=resolution,
                        roots=roots,
                    )
                ):
                    break
            if resolution.exhausted or candidate_cap_reached:
                break
        return tuple(sorted(candidates)), resolution.truncated

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
                if not candidate.exists() and not candidate.is_symlink():
                    continue
                relative = self._relative_path(candidate)
                if relative is not None:
                    candidates.add(relative)
        return tuple(sorted(candidates))

    def select(self, changed_paths: Iterable[str]) -> SourceContextSelection:
        if isinstance(changed_paths, (str, bytes)):
            raise ContextLoadError("changed source paths must be iterable")
        changed_values: set[str] = set()
        changed_overflow = False
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
                    changed_values.add(path)
        except TypeError as exc:
            raise ContextLoadError("changed source paths must be iterable") from exc
        changed = tuple(sorted(changed_values))
        queue: deque[tuple[str, int, str]] = deque()
        queued: set[str] = set()
        seen: set[str] = set()
        excerpts: list[SourceContextExcerpt] = []
        outcomes: dict[str, str] = {}
        total = 0
        incomplete = changed_overflow

        def enqueue(path: str, depth: int, reason: str) -> bool:
            nonlocal incomplete
            if path in seen or path in queued:
                return True
            if len(queue) >= MAX_PENDING_SOURCE_CONTEXT_CANDIDATES:
                incomplete = True
                return False
            queued.add(path)
            queue.append((path, depth, reason))
            return True

        for path in changed:
            try:
                validate_repository_path(path, label="changed source path")
            except ReviewInputError:
                outcomes[path] = "unsupported"
                incomplete = True
                continue
            enqueue(path, 0, "changed-file")
        while queue:
            path, depth, reason = queue.popleft()
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
            encoded = content.encode("utf-8")
            if len(excerpts) >= self.max_files or total + len(encoded) > self.max_bytes:
                outcomes[path] = "budget-exhausted"
                incomplete = True
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
            if source.suffix.lower() not in {".py", ".pyi"}:
                continue
            try:
                tree = ast.parse(content, filename=path)
            except SyntaxError:
                outcomes[path] = "partially-reviewed"
                incomplete = True
                continue
            import_candidates, imports_truncated = self._import_candidates(source, tree)
            associated_tests = self._associated_test_candidates(source)
            related = tuple(sorted(set(import_candidates) | set(associated_tests)))
            if imports_truncated:
                outcomes[path] = "partially-reviewed"
                incomplete = True
            if depth >= self.max_depth:
                # We intentionally include the changed/selected file but mark
                # the selection incomplete when bounded traversal discovered
                # related files that could not be inspected at this depth.
                if any(
                    candidate not in seen and candidate not in queued
                    for candidate in related
                ):
                    outcomes[path] = "partially-reviewed"
                    incomplete = True
                continue
            for candidate in related:
                reason_for_candidate = (
                    "associated-test"
                    if candidate in associated_tests
                    else "direct-import"
                )
                if not enqueue(candidate, depth + 1, reason_for_candidate):
                    outcomes[path] = "partially-reviewed"
                    incomplete = True
        ordered_outcomes = tuple(sorted(outcomes.items()))
        complete = not incomplete
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
    return hashlib.sha256(
        json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


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
