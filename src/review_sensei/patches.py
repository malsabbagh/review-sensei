"""Identity-bound, suggestion-only patch artifacts.

Patch suggestions are deliberately separate from review publication and have
no apply/commit operation.  A caller must explicitly accept a suggestion
through another, separately authorized workflow.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from .diff import analyze_diff
from .errors import ReviewInputError
from .validation import DEFAULT_REVIEW_LIMITS, validate_repository_path

_SHA = re.compile(r"^[a-f0-9]{40}$")
MAX_PATCH_BYTES = 128 * 1024
MAX_PATCH_FILES = 20
MAX_PATCH_METADATA_ITEMS = 32
MAX_PATCH_METADATA_BYTES = 8 * 1024


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

    def __post_init__(self) -> None:
        if not isinstance(self.finding_id, str) or not self.finding_id.strip() or len(self.finding_id) > 256:
            raise ReviewInputError("patch finding_id must be non-empty")
        for label, value in (("base_sha", self.base_sha), ("head_sha", self.head_sha)):
            if not isinstance(value, str) or not _SHA.fullmatch(value):
                raise ReviewInputError(f"patch {label} must be a lowercase commit SHA")
        if not isinstance(self.patch, str) or not self.patch.strip():
            raise ReviewInputError("patch content must be non-empty")
        if len(self.patch.encode("utf-8")) > MAX_PATCH_BYTES:
            raise ReviewInputError("patch exceeds the bounded size limit")
        if "GIT binary patch" in self.patch or "Binary files" in self.patch:
            raise ReviewInputError("binary patches are not supported")
        if "new file mode 120000" in self.patch or "old mode 120000" in self.patch or "new mode 120000" in self.patch:
            raise ReviewInputError("symlink patches are not supported")
        if not isinstance(self.affected_paths, tuple) or not self.affected_paths:
            raise ReviewInputError("patch must declare affected paths")
        if len(self.affected_paths) > MAX_PATCH_FILES:
            raise ReviewInputError("patch affects too many files")
        for path in self.affected_paths:
            validate_repository_path(path, label="patch affected path")
        if len(self.assumptions) > MAX_PATCH_METADATA_ITEMS or len(self.validation) > MAX_PATCH_METADATA_ITEMS:
            raise ReviewInputError("patch metadata has too many entries")
        if any(not isinstance(item, str) or not item.strip() or len(item) > 1024 for item in self.assumptions):
            raise ReviewInputError("patch assumptions must be non-empty strings")
        if any(not isinstance(item, str) or not item.strip() or len(item) > 1024 for item in self.validation):
            raise ReviewInputError("patch validation entries must be non-empty strings")
        if sum(len(item.encode("utf-8")) for item in (*self.assumptions, *self.validation)) > MAX_PATCH_METADATA_BYTES:
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
    assumptions: Iterable[str] = (),
    validation: Iterable[str] = (),
) -> PatchSuggestion:
    """Validate and construct a suggestion for one confirmed finding."""

    status = finding.get("status", finding.get("disposition"))
    if status not in {"confirmed", "verified"}:
        raise ReviewInputError("patch suggestions require a confirmed finding")
    finding_id = finding.get("id", finding.get("finding_id"))
    if not isinstance(finding_id, str) or not finding_id.strip():
        raise ReviewInputError("verified finding must have a stable id")
    allowed = tuple(dict.fromkeys(allowed_paths))
    if not allowed:
        raise ReviewInputError("patch allowed_paths must be non-empty")
    analysis = analyze_diff(patch, limits=DEFAULT_REVIEW_LIMITS)
    changed = set(analysis.changed_paths)
    if any(Path(path).is_symlink() for path in changed):
        raise ReviewInputError("patch cannot target symlink paths")
    if not changed.issubset(set(allowed)):
        raise ReviewInputError("patch touches a path outside the validated finding scope")
    if any(path.startswith("/") or ".." in path.split("/") for path in changed):
        raise ReviewInputError("patch contains an unsafe path")
    return PatchSuggestion(
        finding_id=finding_id,
        base_sha=base_sha,
        head_sha=head_sha,
        patch=patch,
        affected_paths=tuple(sorted(changed)),
        assumptions=tuple(assumptions),
        validation=tuple(validation),
    )


__all__ = ["PatchSuggestion", "create_patch_suggestion"]
