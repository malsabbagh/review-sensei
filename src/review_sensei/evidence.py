"""Immutable exact-snapshot evidence shared by discovery and reassessment."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from functools import cached_property
from typing import Mapping

from .bounded_evidence import canonical_bytes
from .diff import DiffAnalysis, DiffFileRecord, analyze_diff
from .errors import ReviewInputError
from .validation import (
    DEFAULT_TOTAL_WORK_BUDGET,
    utf8_size,
    validate_bounded_text,
    validate_repository_path,
)

_SHA = re.compile(r"^[a-f0-9]{40}$")


def evidence_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class EvidenceSnapshot:
    repository: str | None = None
    pull_request: int | None = None
    base_sha: str | None = None
    head_sha: str | None = None

    def __post_init__(self) -> None:
        if self.repository is not None:
            validate_bounded_text(
                self.repository, 512, label="evidence repository", allow_empty=False
            )
        if self.pull_request is not None and (
            isinstance(self.pull_request, bool)
            or not isinstance(self.pull_request, int)
            or self.pull_request < 1
        ):
            raise ReviewInputError("evidence pull request is invalid")
        for value in (self.base_sha, self.head_sha):
            if value is not None and (
                not isinstance(value, str) or not _SHA.fullmatch(value)
            ):
                raise ReviewInputError("evidence snapshot SHA is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "repository": self.repository,
            "pull_request": self.pull_request,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
        }


def patch_diff(path: str, patch: str, *, old_path: str | None = None) -> str:
    """Frame exact API hunks with validated Git paths, without line rewriting."""
    validate_repository_path(path, label="evidence path", allow_glob_chars=True)
    if old_path is not None:
        validate_repository_path(
            old_path, label="evidence old path", allow_glob_chars=True
        )
    old = old_path or path
    old_name = json.dumps("a/" + old, ensure_ascii=False)
    new_name = json.dumps("b/" + path, ensure_ascii=False)
    return f"diff --git {old_name} {new_name}\n--- {old_name}\n+++ {new_name}\n{patch.rstrip(chr(10))}\n"


@dataclass(frozen=True)
class EvidenceRecord:
    path: str
    patch: str
    snapshot: EvidenceSnapshot
    supplied_complete: bool = True
    provenance: str = "github-files"
    old_path: str | None = None
    original_diff: str | None = None
    expected_additions: int | None = None
    expected_deletions: int | None = None

    def __post_init__(self) -> None:
        validate_repository_path(
            self.path, label="evidence path", allow_glob_chars=True
        )
        if self.old_path is not None:
            validate_repository_path(
                self.old_path, label="evidence old path", allow_glob_chars=True
            )
        if not isinstance(self.snapshot, EvidenceSnapshot) or not isinstance(
            self.supplied_complete, bool
        ):
            raise ReviewInputError(
                "evidence record snapshot or completeness is invalid"
            )
        if self.provenance not in {"github-files", "canonical-diff", "discovery-hunk"}:
            raise ReviewInputError("evidence record provenance is invalid")
        for count in (self.expected_additions, self.expected_deletions):
            if count is not None and (
                isinstance(count, bool)
                or not isinstance(count, int)
                or not 0 <= count <= DEFAULT_TOTAL_WORK_BUDGET.max_total_diff_lines
            ):
                raise ReviewInputError("evidence change count is invalid")
        validate_bounded_text(
            self.patch,
            DEFAULT_TOTAL_WORK_BUDGET.max_total_diff_bytes,
            label="evidence patch",
        )
        if self.original_diff is not None:
            if self.provenance == "github-files" or self.original_diff != self.patch:
                raise ReviewInputError("canonical evidence must bind the exact diff")
            validate_bounded_text(
                self.original_diff,
                DEFAULT_TOTAL_WORK_BUDGET.max_total_diff_bytes,
                label="evidence canonical diff",
                allow_empty=False,
            )

    @cached_property
    def diff(self) -> str:
        return (
            self.original_diff
            if self.original_diff is not None
            else patch_diff(self.path, self.patch, old_path=self.old_path)
        )

    @cached_property
    def analysis(self) -> DiffAnalysis | None:
        """Canonical bounded syntax/count facts, independent of supplied claims."""
        try:
            return analyze_diff(
                self.diff,
                allow_incomplete=True,
                max_bytes=DEFAULT_TOTAL_WORK_BUDGET.max_total_diff_bytes,
                max_lines=DEFAULT_TOTAL_WORK_BUDGET.max_total_diff_lines,
                max_files=DEFAULT_TOTAL_WORK_BUDGET.max_total_files,
                max_hunks=DEFAULT_TOTAL_WORK_BUDGET.max_total_hunks,
            )
        except ReviewInputError:
            return None

    @cached_property
    def complete(self) -> bool:
        analysis = self.analysis
        if (
            not self.supplied_complete
            or not self.patch.strip()
            or self.provenance == "discovery-hunk"
            or analysis is None
        ):
            return False
        # A syntactically valid duplicate/overlapping hunk is not an exhaustive
        # canonical patch. Set-based line counts alone can hide duplicated ranges.
        old_end = new_end = 0
        for hunk in analysis.hunk_records:
            old_begin = hunk.old_start - (1 if hunk.old_count else 0)
            new_begin = hunk.new_start - (1 if hunk.new_count else 0)
            if old_begin < old_end or new_begin < new_end:
                return False
            old_end = hunk.old_start + hunk.old_count - (1 if hunk.old_count else 0)
            new_end = hunk.new_start + hunk.new_count - (1 if hunk.new_count else 0)
        return (
            analysis.enumeration_complete
            and len(analysis.file_records) == 1
            and self.path in analysis.changed_paths
            and not analysis.binary_paths
            and (self.original_diff is not None or bool(analysis.hunk_records))
            and (
                self.expected_additions is None
                or sum(len(record.added_lines) for record in analysis.file_records)
                == self.expected_additions
            )
            and (
                self.expected_deletions is None
                or sum(len(record.deleted_lines) for record in analysis.file_records)
                == self.expected_deletions
            )
        )

    @cached_property
    def patch_sha256(self) -> str:
        return hashlib.sha256(self.patch.encode("utf-8")).hexdigest()

    @cached_property
    def evidence_id(self) -> str:
        return evidence_digest({"domain": "reviewsensei:evidence:v1", **self.to_dict()})

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "old_path": self.old_path,
            "patch_sha256": self.patch_sha256,
            "diff_sha256": hashlib.sha256(self.diff.encode("utf-8")).hexdigest(),
            "snapshot": self.snapshot.to_dict(),
            "supplied_complete": self.supplied_complete,
            "provenance": self.provenance,
            "expected_additions": self.expected_additions,
            "expected_deletions": self.expected_deletions,
        }

    @classmethod
    def from_diff_record(
        cls, record: DiffFileRecord, snapshot: EvidenceSnapshot
    ) -> EvidenceRecord:
        path = record.canonical_path
        if path is None:
            raise ReviewInputError("canonical evidence path is missing")
        return cls(
            path,
            record.text,
            snapshot,
            supplied_complete=not record.binary,
            provenance="canonical-diff",
            old_path=record.old_path,
            original_diff=record.text,
        )


@dataclass(frozen=True)
class EvidenceBundle:
    snapshot: EvidenceSnapshot
    records: tuple[EvidenceRecord, ...]
    enumeration_complete: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, EvidenceSnapshot) or not isinstance(
            self.records, tuple
        ):
            raise ReviewInputError("evidence bundle is invalid")
        if not isinstance(self.enumeration_complete, bool):
            raise ReviewInputError("evidence enumeration completeness is invalid")
        if len(self.records) > DEFAULT_TOTAL_WORK_BUDGET.max_total_hunks:
            raise ReviewInputError("evidence bundle contains too many records")
        by_path: dict[str, EvidenceRecord] = {}
        by_id: dict[str, EvidenceRecord] = {}
        for record in self.records:
            if (
                not isinstance(record, EvidenceRecord)
                or record.snapshot != self.snapshot
            ):
                raise ReviewInputError("evidence records span conflicting snapshots")
            previous = by_path.setdefault(record.path, record)
            if previous != record and not (
                previous.provenance == record.provenance == "discovery-hunk"
            ):
                raise ReviewInputError("evidence record paths conflict")
            by_id[record.evidence_id] = record
        ordered = tuple(
            sorted(by_id.values(), key=lambda item: (item.path, item.evidence_id))
        )
        if (
            sum(utf8_size(record.diff, label="evidence diff") for record in ordered)
            > DEFAULT_TOTAL_WORK_BUDGET.max_total_diff_bytes
        ):
            raise ReviewInputError("evidence bundle exceeds the total-work limit")
        object.__setattr__(self, "records", ordered)

    @cached_property
    def digest(self) -> str:
        return evidence_digest(
            {
                "snapshot": self.snapshot.to_dict(),
                "records": [record.to_dict() for record in self.records],
                "enumeration_complete": self.enumeration_complete,
            }
        )

    def by_path(self) -> Mapping[str, EvidenceRecord]:
        if any(record.provenance == "discovery-hunk" for record in self.records):
            raise ReviewInputError("fragment evidence requires identity lookup")
        return {record.path: record for record in self.records}

    def by_id(self) -> Mapping[str, EvidenceRecord]:
        return {record.evidence_id: record for record in self.records}


@dataclass(frozen=True)
class EvidenceGroup:
    """An exhaustive atomic group, independent of transport or prompt packing.

    Group digests are integrity identities, not authenticated read or assessment
    receipts. A caller must authenticate the shared partition root and current
    snapshot before using a restored group. Parts never become complete files.
    """

    bundle: EvidenceBundle
    required_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.bundle, EvidenceBundle)
            or self.bundle.snapshot.base_sha is None
            or self.bundle.snapshot.head_sha is None
            or not isinstance(self.required_paths, tuple)
            or not 1 <= len(self.required_paths) <= 64
            or any(not isinstance(path, str) for path in self.required_paths)
            or len(set(self.required_paths)) != len(self.required_paths)
        ):
            raise ReviewInputError("evidence group scope is invalid")
        for path in self.required_paths:
            validate_repository_path(
                path, label="evidence group path", allow_glob_chars=True
            )
        # Reject fragment bundles; no historical/discovery hunk can stand in for
        # one of the required canonical current files.
        self.bundle.by_path()
        object.__setattr__(self, "required_paths", tuple(sorted(self.required_paths)))

    @cached_property
    def records(self) -> tuple[EvidenceRecord, ...]:
        available = self.bundle.by_path()
        return tuple(
            available[path] for path in self.required_paths if path in available
        )

    @cached_property
    def pending(self) -> tuple[tuple[str, str], ...]:
        available = self.bundle.by_path()
        pending = []
        for path in self.required_paths:
            if not self.bundle.enumeration_complete:
                reason = "incomplete-enumeration"
            elif path not in available:
                reason = "required-evidence-missing"
            elif not available[path].complete:
                reason = "required-evidence-incomplete"
            else:
                continue
            pending.append((path, reason))
        return tuple(pending)

    @property
    def complete(self) -> bool:
        return not self.pending

    def coverage_document(self) -> dict[str, object]:
        return {
            "snapshot": self.bundle.snapshot.to_dict(),
            "enumeration_complete": self.bundle.enumeration_complete,
            "required_paths": list(self.required_paths),
            "files": [
                {
                    **record.to_dict(),
                    "evidence_id": record.evidence_id,
                    "complete": record.complete,
                    "hunks": len(record.analysis.hunk_records)
                    if record.analysis is not None
                    else None,
                    "additions": sum(
                        len(hunk.added_lines) for hunk in record.analysis.hunk_records
                    )
                    if record.analysis is not None
                    else None,
                    "deletions": sum(
                        len(hunk.deleted_lines) for hunk in record.analysis.hunk_records
                    )
                    if record.analysis is not None
                    else None,
                }
                for record in self.records
            ],
            "pending": [list(item) for item in self.pending],
        }

    @cached_property
    def group_id(self) -> str:
        return evidence_digest(
            {"domain": "reviewsensei:evidence-group:v1", **self.coverage_document()}
        )

    @property
    def diff_context(self) -> str:
        if not self.complete:
            raise ReviewInputError("evidence group is incomplete")
        return "\n".join(
            f"path={record.path}\n{record.patch}" for record in self.records
        )

    def to_document(self) -> dict[str, object]:
        """Materialize whole exact records for the common bounded part codec.

        The 2 MiB canonical document bound matches the proposed common decoded
        envelope; it does not raise the existing 8 MiB whole-diff work bound.
        Pending groups are retained losslessly, but cannot authorize a decision.
        """
        document: dict[str, object] = {
            "schema_version": "1",
            "group_id": self.group_id,
            "coverage": self.coverage_document(),
            "records": [
                {
                    "path": record.path,
                    "patch": record.patch,
                    "old_path": record.old_path,
                    "original_diff": record.original_diff,
                    "supplied_complete": record.supplied_complete,
                    "provenance": record.provenance,
                    "expected_additions": record.expected_additions,
                    "expected_deletions": record.expected_deletions,
                }
                for record in self.records
            ],
        }
        if len(canonical_bytes(document)) > 2 * 1024 * 1024:
            raise ReviewInputError(
                "evidence group exceeds the materialization byte bound"
            )
        return document

    @classmethod
    def from_document(cls, value: object) -> EvidenceGroup:
        """Restore only a closed, exact whole document; no external lookups."""
        try:
            if not isinstance(value, dict) or set(value) != {
                "schema_version",
                "group_id",
                "coverage",
                "records",
            }:
                raise ValueError("shape")
            if (
                value["schema_version"] != "1"
                or len(canonical_bytes(value)) > 2 * 1024 * 1024
            ):
                raise ValueError("bound")
            coverage = value["coverage"]
            if not isinstance(coverage, dict) or not isinstance(
                coverage.get("snapshot"), dict
            ):
                raise ValueError("coverage")
            snapshot = EvidenceSnapshot(**coverage["snapshot"])
            records = value["records"]
            paths = coverage.get("required_paths")
            enumeration = coverage.get("enumeration_complete")
            if (
                not isinstance(records, list)
                or not isinstance(paths, list)
                or len(records) > 64
                or not isinstance(enumeration, bool)
            ):
                raise ValueError("records")
            restored = cls(
                EvidenceBundle(
                    snapshot,
                    tuple(
                        EvidenceRecord(snapshot=snapshot, **record)
                        for record in records
                    ),
                    enumeration,
                ),
                tuple(paths),
            )
            if canonical_bytes(restored.to_document()) != canonical_bytes(value):
                raise ValueError("identity")
            return restored
        except (
            TypeError,
            ValueError,
            KeyError,
            ReviewInputError,
            RecursionError,
        ) as exc:
            raise ReviewInputError("materialized evidence group is invalid") from exc
