"""Identity-bound, suggestion-only patch artifacts.

Patch suggestions are deliberately separate from review publication and have
no apply/commit operation.  A caller must explicitly accept a suggestion
through another, separately authorized workflow.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Iterable, Mapping

from .diff import analyze_diff
from .errors import ReviewInputError
from .validation import DEFAULT_REVIEW_LIMITS, validate_repository_path

_SHA = re.compile(r"^[a-f0-9]{40}$")
MAX_PATCH_BYTES = 128 * 1024
MAX_PATCH_FILES = 20
MAX_PATCH_METADATA_ITEMS = 32
MAX_PATCH_METADATA_BYTES = 8 * 1024
_SYMLINK_MODE_LINE = re.compile(
    r"^(?:(?:old|new)(?: file)? mode|deleted file mode) 120000$"
)
_GIT_MODE = re.compile(r"^[0-7]{6}$")
_BINARY_FILES_MARKER = re.compile(r"^Binary files .+ and .+ differ$")


def _reject_unsupported_patch_content(patch: str) -> None:
    """Reject patch features that cannot be safely represented here."""

    # Binary markers are Git structure, not arbitrary text.  Restrict the
    # check to complete unprefixed marker lines so a source or documentation
    # hunk containing the phrase ``Binary files`` or ``GIT binary patch`` is
    # still a valid textual suggestion.
    for line in patch.splitlines():
        structural_line = line.rstrip("\r")
        if structural_line == "GIT binary patch" or _BINARY_FILES_MARKER.fullmatch(
            structural_line
        ):
            raise ReviewInputError("binary patches are not supported")
        if _SYMLINK_MODE_LINE.fullmatch(structural_line) or (
            structural_line.startswith("index ")
            and structural_line.split()[-1:] == ["120000"]
        ):
            raise ReviewInputError("symlink patches are not supported")


def _normalize_git_mode(value: object) -> str:
    """Validate one mode from the exact reviewed snapshot.

    Git encodes regular files as ``100xxx``.  Keeping the value as its
    canonical six-digit octal string avoids platform-dependent ``stat`` mode
    representations and makes the provenance serializable in the suggestion.
    """

    if not isinstance(value, str) or _GIT_MODE.fullmatch(value) is None:
        raise ReviewInputError(
            "patch snapshot modes must be six-digit Git mode strings"
        )
    if not value.startswith("100"):
        raise ReviewInputError("patch snapshot modes must identify regular files")
    return value


def _finding_scope_paths(finding: Mapping[str, object]) -> tuple[str, ...]:
    """Return the canonical path scope declared by a confirmed finding."""

    raw_paths = finding.get("affected_paths", finding.get("paths"))
    if raw_paths is None:
        single = finding.get("path")
        if single is None:
            raise ReviewInputError(
                "verified finding must declare affected repository paths"
            )
        raw_paths = (single,)
    if isinstance(raw_paths, (str, bytes)):
        raise ReviewInputError("finding path scope must be an iterable of paths")
    try:
        iterator = iter(raw_paths)
    except (TypeError, AttributeError) as exc:
        raise ReviewInputError(
            "finding path scope must be an iterable of paths"
        ) from exc
    scoped: dict[str, None] = {}
    for index, path in enumerate(iterator, start=1):
        if index > MAX_PATCH_FILES:
            raise ReviewInputError("finding path scope has too many entries")
        if not isinstance(path, str):
            raise ReviewInputError("finding path scope entries must be strings")
        validate_repository_path(path, label="finding path")
        scoped.setdefault(path, None)
    if not scoped:
        raise ReviewInputError(
            "verified finding must declare affected repository paths"
        )
    return tuple(scoped)


def _bounded_allowed_paths(values: Iterable[str]) -> tuple[str, ...]:
    """Read allowed paths once while bounding both unique and total inputs."""

    try:
        iterator = iter(values)
    except (TypeError, AttributeError) as exc:
        raise ReviewInputError(
            "patch allowed_paths must be an iterable of paths"
        ) from exc
    unique: dict[str, None] = {}
    consumed = 0
    for path in iterator:
        # Counting every item (including duplicates) is intentional: an
        # attacker-controlled generator yielding one path forever must not
        # bypass the input bound by relying on de-duplication.
        consumed += 1
        if consumed > MAX_PATCH_FILES:
            raise ReviewInputError("patch allowed_paths has too many entries")
        if not isinstance(path, str):
            raise ReviewInputError("patch allowed paths must be strings")
        validate_repository_path(path, label="patch allowed path")
        unique.setdefault(path, None)
    if not unique:
        raise ReviewInputError("patch allowed_paths must be non-empty")
    return tuple(unique)


def _bounded_metadata(values: Iterable[str], *, label: str) -> tuple[str, ...]:
    """Read metadata entries without materializing an unbounded iterable."""

    try:
        iterator = iter(values)
    except (TypeError, AttributeError) as exc:
        raise ReviewInputError(f"patch {label} must be an iterable of strings") from exc
    result: list[str] = []
    total_bytes = 0
    for index, item in enumerate(iterator, start=1):
        if index > MAX_PATCH_METADATA_ITEMS:
            raise ReviewInputError("patch metadata has too many entries")
        if not isinstance(item, str) or not item.strip() or len(item) > 1024:
            raise ReviewInputError(f"patch {label} entries must be non-empty strings")
        total_bytes += len(item.encode("utf-8"))
        if total_bytes > MAX_PATCH_METADATA_BYTES:
            raise ReviewInputError("patch metadata exceeds the bounded size limit")
        result.append(item)
    return tuple(result)


def _bounded_snapshot_modes(
    values: Mapping[str, str] | None,
    *,
    changed_paths: set[str],
) -> tuple[tuple[str, str], ...]:
    """Validate exact regular-file mode provenance for every changed path."""

    if not isinstance(values, Mapping):
        raise ReviewInputError(
            "patch requires trusted regular-file snapshot mode provenance"
        )
    try:
        iterator = iter(values.items())
    except (AttributeError, TypeError) as exc:
        raise ReviewInputError("patch snapshot modes must be a mapping") from exc
    normalized: dict[str, str] = {}
    for index, item in enumerate(iterator, start=1):
        if index > MAX_PATCH_FILES:
            raise ReviewInputError("patch snapshot modes have too many entries")
        try:
            path, mode = item
        except (TypeError, ValueError) as exc:
            raise ReviewInputError(
                "patch snapshot modes must map paths to modes"
            ) from exc
        if not isinstance(path, str):
            raise ReviewInputError("patch snapshot mode paths must be strings")
        validate_repository_path(path, label="patch snapshot mode path")
        if path in normalized:
            raise ReviewInputError("patch snapshot mode paths must be unique")
        normalized[path] = _normalize_git_mode(mode)
    if set(normalized) != changed_paths:
        raise ReviewInputError(
            "patch snapshot modes must cover exactly the changed paths"
        )
    return tuple(sorted(normalized.items()))


@dataclass(frozen=True)
class PatchSuggestion:
    finding_id: str
    base_sha: str
    head_sha: str
    patch: str
    affected_paths: tuple[str, ...]
    assumptions: tuple[str, ...] = ()
    validation: tuple[str, ...] = ()
    accepted: bool = False
    # Immutable mode provenance captured from the exact reviewed snapshot.
    # Every affected path must have a regular-file Git mode before a patch is
    # represented; this prevents a mode-less patch from targeting an existing
    # symlink when a later workflow applies it.
    snapshot_modes: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.finding_id, str)
            or not self.finding_id.strip()
            or len(self.finding_id) > 256
        ):
            raise ReviewInputError("patch finding_id must be non-empty")
        for label, value in (("base_sha", self.base_sha), ("head_sha", self.head_sha)):
            if not isinstance(value, str) or not _SHA.fullmatch(value):
                raise ReviewInputError(f"patch {label} must be a lowercase commit SHA")
        if not isinstance(self.patch, str) or not self.patch.strip():
            raise ReviewInputError("patch content must be non-empty")
        if len(self.patch.encode("utf-8")) > MAX_PATCH_BYTES:
            raise ReviewInputError("patch exceeds the bounded size limit")
        _reject_unsupported_patch_content(self.patch)
        if not isinstance(self.affected_paths, tuple) or not self.affected_paths:
            raise ReviewInputError("patch must declare affected paths")
        if len(self.affected_paths) > MAX_PATCH_FILES:
            raise ReviewInputError("patch affects too many files")
        if len(self.affected_paths) != len(set(self.affected_paths)):
            raise ReviewInputError("patch affected paths must be unique")
        for path in self.affected_paths:
            validate_repository_path(path, label="patch affected path")
        analysis = analyze_diff(self.patch, limits=DEFAULT_REVIEW_LIMITS)
        actual_paths = set(analysis.changed_paths)
        if actual_paths != set(self.affected_paths):
            raise ReviewInputError(
                "patch affected paths must match the paths changed by the patch"
            )
        if not isinstance(self.snapshot_modes, tuple) or not self.snapshot_modes:
            raise ReviewInputError(
                "patch requires trusted regular-file snapshot mode provenance"
            )
        if len(self.snapshot_modes) > MAX_PATCH_FILES:
            raise ReviewInputError("patch snapshot modes have too many entries")
        normalized_modes: dict[str, str] = {}
        for item in self.snapshot_modes:
            if not isinstance(item, tuple) or len(item) != 2:
                raise ReviewInputError("patch snapshot modes must be path/mode tuples")
            path, mode = item
            if not isinstance(path, str):
                raise ReviewInputError("patch snapshot mode paths must be strings")
            validate_repository_path(path, label="patch snapshot mode path")
            if path in normalized_modes:
                raise ReviewInputError("patch snapshot mode paths must be unique")
            normalized_modes[path] = _normalize_git_mode(mode)
        if set(normalized_modes) != set(self.affected_paths):
            raise ReviewInputError(
                "patch snapshot modes must cover exactly the changed paths"
            )
        if (
            not isinstance(self.assumptions, tuple)
            or not isinstance(self.validation, tuple)
            or len(self.assumptions) > MAX_PATCH_METADATA_ITEMS
            or len(self.validation) > MAX_PATCH_METADATA_ITEMS
        ):
            raise ReviewInputError("patch metadata has too many entries")
        if any(
            not isinstance(item, str) or not item.strip() or len(item) > 1024
            for item in self.assumptions
        ):
            raise ReviewInputError("patch assumptions must be non-empty strings")
        if any(
            not isinstance(item, str) or not item.strip() or len(item) > 1024
            for item in self.validation
        ):
            raise ReviewInputError("patch validation entries must be non-empty strings")
        if (
            sum(
                len(item.encode("utf-8"))
                for item in (*self.assumptions, *self.validation)
            )
            > MAX_PATCH_METADATA_BYTES
        ):
            raise ReviewInputError("patch metadata exceeds the bounded size limit")
        if self.accepted:
            raise ReviewInputError("patch suggestions cannot self-authorize acceptance")

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.patch.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "finding_id": self.finding_id,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "patch": self.patch,
            "affected_paths": list(self.affected_paths),
            "assumptions": list(self.assumptions),
            "validation": list(self.validation),
            "snapshot_modes": dict(self.snapshot_modes),
            "digest": self.digest,
            "accepted": False,
        }


def create_patch_suggestion(
    finding: Mapping[str, object],
    *,
    patch: str,
    base_sha: str,
    head_sha: str,
    allowed_paths: Iterable[str],
    snapshot_modes: Mapping[str, str] | None = None,
    assumptions: Iterable[str] = (),
    validation: Iterable[str] = (),
) -> PatchSuggestion:
    """Validate and construct a suggestion for one confirmed finding.

    ``snapshot_modes`` must be read from the exact reviewed base/head snapshot
    by a trusted caller and must cover every changed path.  Requiring this
    provenance is deliberate: unified patches commonly omit Git mode headers,
    so inspecting patch text alone cannot prevent an existing symlink from
    being modified by a later patch application workflow.
    """

    status = finding.get("status", finding.get("disposition"))
    if status not in {"confirmed", "verified"}:
        raise ReviewInputError("patch suggestions require a confirmed finding")
    finding_id = finding.get("id", finding.get("finding_id"))
    if (
        not isinstance(finding_id, str)
        or not finding_id.strip()
        or len(finding_id) > 256
    ):
        raise ReviewInputError("verified finding must have a stable id")
    if isinstance(allowed_paths, (str, bytes)):
        raise ReviewInputError("patch allowed_paths must be an iterable of paths")
    if isinstance(assumptions, (str, bytes)) or isinstance(validation, (str, bytes)):
        raise ReviewInputError("patch metadata must be iterables of strings")
    finding_scope = set(_finding_scope_paths(finding))
    allowed = set(_bounded_allowed_paths(allowed_paths))
    if allowed != finding_scope:
        raise ReviewInputError(
            "patch allowed_paths must exactly match the finding path scope"
        )
    analysis = analyze_diff(patch, limits=DEFAULT_REVIEW_LIMITS)
    changed: set[str] = set()
    for path in analysis.changed_paths:
        validate_repository_path(path, label="patch changed path")
        changed.add(path)
    if changed != allowed:
        raise ReviewInputError(
            "patch changed paths must exactly match the validated finding scope"
        )
    _reject_unsupported_patch_content(patch)
    mode_values = _bounded_snapshot_modes(snapshot_modes, changed_paths=changed)
    assumption_values = _bounded_metadata(assumptions, label="assumptions")
    validation_values = _bounded_metadata(validation, label="validation")
    return PatchSuggestion(
        finding_id=finding_id,
        base_sha=base_sha,
        head_sha=head_sha,
        patch=patch,
        affected_paths=tuple(sorted(changed)),
        assumptions=assumption_values,
        validation=validation_values,
        snapshot_modes=mode_values,
    )


__all__ = ["PatchSuggestion", "create_patch_suggestion"]
