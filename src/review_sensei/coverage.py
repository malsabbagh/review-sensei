"""Explicit per-file and per-hunk review coverage.

Coverage is a first-class review artifact.  A change is never reported as
fully reviewed when enumeration is incomplete, when any changed path lacks an
outcome, or when an outcome is anything other than ``reviewed``.  Missing
coverage is distinct from partial coverage so approval can fail closed.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Mapping

from .errors import ReviewInputError
from .schemas import validate_public_document
from .validation import DEFAULT_REVIEW_LIMITS, ReviewLimits, validate_repository_path

COVERAGE_OUTCOMES = frozenset(
    {
        "reviewed",
        "partially-reviewed",
        "excluded-by-policy",
        "unsupported",
        "budget-exhausted",
    }
)
COVERAGE_REASONS = frozenset(
    {
        "binary",
        "generated",
        "too-large-hunk",
        "too-large-file",
        "chunk-failed",
        "chunk-preflight-failed",
        "provider-call-budget",
        "incomplete-enumeration",
        "cross-file-relationship",
        "deleted",
        "renamed",
    }
)
PUBLIC_SCHEMA_VERSION = "1.0"
MAX_COVERAGE_REASON_LENGTH = 128
MAX_COVERAGE_ENTRIES = 4_000
MAX_COVERAGE_HUNKS = 40_000


def _is_public_text(value: str) -> bool:
    return value.isprintable() and not any(
        unicodedata.category(character).startswith("C")
        or unicodedata.category(character) == "Cn"
        for character in value
    )


def _validate_reason(reason: str | None) -> None:
    if reason is None:
        return
    if (
        not isinstance(reason, str)
        or not reason
        or len(reason) > MAX_COVERAGE_REASON_LENGTH
        or not _is_public_text(reason)
        or reason not in COVERAGE_REASONS
    ):
        raise ReviewInputError("coverage reason is invalid")


@dataclass(frozen=True)
class FileCoverage:
    """Coverage outcome for one enumerated changed path."""

    path: str
    outcome: str
    reason: str | None = None

    def __post_init__(self) -> None:
        validate_repository_path(self.path, label="coverage path")
        if self.outcome not in COVERAGE_OUTCOMES:
            raise ReviewInputError("coverage outcome is unsupported")
        _validate_reason(self.reason)

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {"path": self.path, "outcome": self.outcome}
        if self.reason is not None:
            value["reason"] = self.reason
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "FileCoverage":
        if not isinstance(value, Mapping):
            raise ReviewInputError("file coverage must be a JSON object")
        path = value.get("path")
        outcome = value.get("outcome")
        reason = value.get("reason")
        if not isinstance(path, str) or not isinstance(outcome, str):
            raise ReviewInputError("file coverage has an invalid shape")
        if reason is not None and not isinstance(reason, str):
            raise ReviewInputError("file coverage reason must be a string")
        return cls(path=path, outcome=outcome, reason=reason)


@dataclass(frozen=True)
class HunkCoverage:
    """Coverage outcome for one enumerated diff hunk."""

    index: int
    path: str
    outcome: str
    reason: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.index, bool) or not isinstance(self.index, int) or self.index < 1:
            raise ReviewInputError("coverage hunk index must be a positive integer")
        validate_repository_path(self.path, label="coverage hunk path")
        if self.outcome not in COVERAGE_OUTCOMES:
            raise ReviewInputError("coverage outcome is unsupported")
        _validate_reason(self.reason)

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "index": self.index,
            "path": self.path,
            "outcome": self.outcome,
        }
        if self.reason is not None:
            value["reason"] = self.reason
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "HunkCoverage":
        if not isinstance(value, Mapping):
            raise ReviewInputError("hunk coverage must be a JSON object")
        index = value.get("index")
        path = value.get("path")
        outcome = value.get("outcome")
        reason = value.get("reason")
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or not isinstance(path, str)
            or not isinstance(outcome, str)
        ):
            raise ReviewInputError("hunk coverage has an invalid shape")
        if reason is not None and not isinstance(reason, str):
            raise ReviewInputError("hunk coverage reason must be a string")
        return cls(index=index, path=path, outcome=outcome, reason=reason)


@dataclass(frozen=True)
class CoverageManifest:
    """Publisher-facing coverage for every enumerated changed file and hunk.

    ``enumeration_complete`` is true only when every changed path discovered in
    the bounded inventory has an outcome.  Incomplete enumeration can never be
    reported as fully reviewed.
    """

    files: tuple[FileCoverage, ...]
    hunks: tuple[HunkCoverage, ...] = ()
    enumeration_complete: bool = True
    schema_version: str = PUBLIC_SCHEMA_VERSION
    limits: ReviewLimits = DEFAULT_REVIEW_LIMITS

    def __post_init__(self) -> None:
        if not isinstance(self.limits, ReviewLimits):
            raise ReviewInputError("coverage limits must be a ReviewLimits value")
        if self.schema_version != PUBLIC_SCHEMA_VERSION:
            raise ReviewInputError("coverage schema_version is unsupported")
        if not isinstance(self.enumeration_complete, bool):
            raise ReviewInputError("coverage enumeration_complete must be a boolean")
        if not isinstance(self.files, tuple) or any(
            not isinstance(entry, FileCoverage) for entry in self.files
        ):
            raise ReviewInputError("coverage files must be FileCoverage values")
        if not isinstance(self.hunks, tuple) or any(
            not isinstance(entry, HunkCoverage) for entry in self.hunks
        ):
            raise ReviewInputError("coverage hunks must be HunkCoverage values")
        if len(self.files) > MAX_COVERAGE_ENTRIES:
            raise ReviewInputError("coverage contains too many files")
        if len(self.hunks) > MAX_COVERAGE_HUNKS:
            raise ReviewInputError("coverage contains too many hunks")
        paths = [entry.path for entry in self.files]
        if len(paths) != len(set(paths)):
            raise ReviewInputError("coverage files must be unique")
        hunk_keys = [(entry.index, entry.path) for entry in self.hunks]
        if len(hunk_keys) != len(set(hunk_keys)):
            raise ReviewInputError("coverage hunks must be unique")
        validate_public_document(self.to_dict(), "coverage-manifest")

    @property
    def fully_reviewed(self) -> bool:
        if not self.enumeration_complete:
            return False
        return all(entry.outcome == "reviewed" for entry in self.files) and all(
            entry.outcome == "reviewed" for entry in self.hunks
        )

    def approval_state(self) -> str:
        """Return ``reviewed``, ``partial``, or ``incomplete`` for approval."""

        if not self.enumeration_complete:
            return "incomplete"
        if self.fully_reviewed:
            return "reviewed"
        return "partial"

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "enumeration_complete": self.enumeration_complete,
            "fully_reviewed": self.fully_reviewed,
            "files": [entry.to_dict() for entry in self.files],
            "hunks": [entry.to_dict() for entry in self.hunks],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "CoverageManifest":
        if not isinstance(value, Mapping):
            raise ReviewInputError("coverage manifest must be a JSON object")
        files = value.get("files")
        hunks = value.get("hunks", [])
        enumeration_complete = value.get("enumeration_complete", False)
        schema_version = value.get("schema_version", PUBLIC_SCHEMA_VERSION)
        if not isinstance(files, list):
            raise ReviewInputError("coverage files must be an array")
        if not isinstance(hunks, list):
            raise ReviewInputError("coverage hunks must be an array")
        if not isinstance(enumeration_complete, bool):
            raise ReviewInputError("coverage enumeration_complete must be a boolean")
        if not isinstance(schema_version, str):
            raise ReviewInputError("coverage schema_version must be a string")
        parsed_files = tuple(
            FileCoverage.from_dict(item) if isinstance(item, Mapping) else item
            for item in files
        )
        parsed_hunks = tuple(
            HunkCoverage.from_dict(item) if isinstance(item, Mapping) else item
            for item in hunks
        )
        return cls(
            files=parsed_files,
            hunks=parsed_hunks,
            enumeration_complete=enumeration_complete,
            schema_version=schema_version,
        )


def coverage_approval_state(coverage: CoverageManifest | None) -> str:
    """Map missing or incomplete coverage onto the approval vocabulary."""

    if coverage is None:
        return "unknown"
    return coverage.approval_state()


def file_coverage_map(coverage: CoverageManifest) -> dict[str, FileCoverage]:
    return {entry.path: entry for entry in coverage.files}


__all__ = [
    "COVERAGE_OUTCOMES",
    "COVERAGE_REASONS",
    "CoverageManifest",
    "FileCoverage",
    "HunkCoverage",
    "PUBLIC_SCHEMA_VERSION",
    "coverage_approval_state",
    "file_coverage_map",
]
