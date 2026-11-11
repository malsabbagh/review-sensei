"""Cross-file finding location and native thread drift lifecycle (G16).

Tracks three orthogonal bindings for every review obligation across commits:
1. ``OriginalFindingCriterion``: immutable origin identity, resolution
   criterion, defect kind, and blocking/human requirement.
2. ``PresentLocationAnchor``: current file/line/side/symbol anchor and explicit
   drift classification across ``file_rename``, ``file_copy``,
   ``cross_file_move``, ``line_deletion``, ``duplicate_code``, and
   ``whole_file_rewrite``.
3. ``NativeThreadBinding``: GitHub thread/comment binding and ``PublishedSlot``
   state, independent of whether the host UI marked an anchor outdated or a
   collaborator clicked "Resolve conversation".

Key invariants:
- Outdated/deleted diff anchors or host-resolved UI threads never satisfy an
  obligation without positive ``ConfirmedFixEvidence``, ``AuthorizedHumanOverride``,
  or a complete head-bound ``AuthorizedRescan(mode="full")``.
- Multi-target drift (file copies, split cross-file moves, duplicate code)
  holds the obligation in ``ambiguous-multi-target`` and withholds approval
  with explicit recovery routes (``verify``, ``override``, ``accept-risk``,
  ``rescan``).
- Uniquely drifted active-window findings reuse their ``obligation_id`` and
  existing native thread in-place rather than minting duplicate active findings.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

from .active_finding_window import (
    DEFAULT_PROPOSAL_RANK,
    EVIDENCE_RANKS,
    IMPACT_RANKS,
    PUBLIC_WINDOW_SCHEMA_VERSION,
    ActiveWindowRecoveryStep,
    AuthorizedCoverageAcceptance,
    AuthorizedHumanOverride,
    ChangedFileCoverageState,
    ConfirmedFixEvidence,
    assert_no_deferred_leakage,
    extract_decision_proposals,
)
from .baseline import (
    BaselineFinding,
    resolution_criterion_digest,
)
from .bounded_evidence import canonical_bytes
from .context import comment_defect_kind, finding_lifecycle_for_comment
from .convergence import ReviewConvergencePolicy
from .errors import ReviewInputError
from .human_assessment import finding_instance_fingerprint
from .models import ReviewComment, ReviewResult
from .placement import (
    ANCHOR_LOCATIONS,
    BODY_PLACEMENT,
    INLINE_PLACEMENT,
    HostPlacementFacts,
    placement_note,
    plan_finding_placement_detail,
)
from .publication_selection import PublishedSlot
from .rescan import AuthorizedRescan
from .validation import DEFAULT_REVIEW_LIMITS, validate_repository_path

_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_COMMIT_SHA_RE = re.compile(r"^[a-f0-9]{40,64}$")
_COPY_FROM_RE = re.compile(r"^copy from (.+)$", re.MULTILINE)
_COPY_TO_RE = re.compile(r"^copy to (.+)$", re.MULTILINE)

DRIFT_KINDS = frozenset(
    {
        "unchanged",
        "file_rename",
        "file_copy",
        "cross_file_move",
        "line_deletion",
        "duplicate_code",
        "whole_file_rewrite",
    }
)

DRIFT_UNRESOLVED_STATES = frozenset(
    {
        "active-unresolved",
        "omitted-uncertain",
        "ambiguous-multi-target",
    }
)
DRIFT_RESOLVED_STATES = frozenset(
    {
        "verified-fixed",
        "authorized-override",
        "rescan-superseded",
    }
)
DRIFT_LIFECYCLE_STATES = DRIFT_UNRESOLVED_STATES | DRIFT_RESOLVED_STATES

DRIFT_WITHHELD_REASON_CODES = frozenset(
    {
        "coverage-incomplete",
        "coverage-partial",
        "review-incomplete",
        "unresolved-obligations",
        "uncertain-finding-lifecycle",
        "ambiguous-drift-target",
        "uncertain-native-slot",
    }
)


def _require_bool(value: object, *, label: str) -> bool:
    if type(value) is not bool:
        raise ReviewInputError(f"{label} must be a boolean")
    return value


def _require_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ReviewInputError(f"{label} must be a 64-character lowercase hex SHA-256")
    return value


def _require_commit_sha(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _COMMIT_SHA_RE.fullmatch(value) is None:
        raise ReviewInputError(f"{label} must be a commit SHA")
    return value


def _require_target_id(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 256
        or not value.isprintable()
    ):
        raise ReviewInputError(f"{label} is invalid")
    return value


@dataclass(frozen=True)
class FileDriftDescriptor:
    """Explicit or diff-derived structural file/symbol movement fact."""

    drift_kind: str
    source_path: str
    target_paths: tuple[str, ...] = ()
    moved_symbols: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.drift_kind not in DRIFT_KINDS - {"unchanged"}:
            raise ReviewInputError("file drift descriptor drift_kind is invalid")
        validate_repository_path(
            self.source_path, allow_glob_chars=True, label="drift source_path"
        )
        if not isinstance(self.target_paths, tuple):
            raise ReviewInputError("drift target_paths must be a tuple")
        for target in self.target_paths:
            validate_repository_path(
                target, allow_glob_chars=True, label="drift target_path"
            )
        if self.drift_kind in {"file_rename", "file_copy", "cross_file_move"} and not (
            self.target_paths
        ):
            raise ReviewInputError(
                f"{self.drift_kind} descriptor requires at least one target_path"
            )
        if self.drift_kind == "file_rename" and len(self.target_paths) != 1:
            raise ReviewInputError("file_rename descriptor requires one target_path")
        for sym in self.moved_symbols:
            if not isinstance(sym, str) or not sym.strip():
                raise ReviewInputError("moved_symbols entry is invalid")


def extract_drift_descriptors_from_diff(diff: str) -> tuple[FileDriftDescriptor, ...]:
    """Extract structural rename, copy, deletion, and whole-rewrite descriptors."""

    from .diff import analyze_diff

    analysis = analyze_diff(diff)
    descriptors: list[FileDriftDescriptor] = []

    renamed_pairs = set(analysis.renamed_paths)
    for old_path, new_path in analysis.renamed_paths:
        descriptors.append(
            FileDriftDescriptor(
                drift_kind="file_rename",
                source_path=old_path,
                target_paths=(new_path,),
            )
        )

    # Detect Git copy headers if present in raw diff blocks.
    for chunk in diff.split("diff --git "):
        if not chunk.strip():
            continue
        copy_from = _COPY_FROM_RE.search(chunk)
        copy_to = _COPY_TO_RE.search(chunk)
        if copy_from is not None and copy_to is not None:
            src = copy_from.group(1).strip()
            dst = copy_to.group(1).strip()
            descriptors.append(
                FileDriftDescriptor(
                    drift_kind="file_copy",
                    source_path=src,
                    target_paths=(dst,),
                )
            )

    for record in analysis.file_records:
        if (
            record.old_path is not None
            and record.new_path is None
            and all(src != record.old_path for src, _ in renamed_pairs)
        ):
            descriptors.append(
                FileDriftDescriptor(
                    drift_kind="line_deletion",
                    source_path=record.old_path,
                    target_paths=(),
                )
            )
        elif (
            record.old_path is not None
            and record.new_path == record.old_path
            and record.deleted_lines
            and record.added_lines
        ):
            # Whole-file rewrite heuristic: single hunk starting at line 1
            # replacing >= 5 lines with zero preserved context lines.
            if (
                len(record.hunks) == 1
                and record.hunks[0].old_start == 1
                and record.hunks[0].new_start == 1
                and len(record.deleted_lines) >= 3
                and len(record.deleted_lines) == record.hunks[0].old_count
                and len(record.added_lines) == record.hunks[0].new_count
            ):
                descriptors.append(
                    FileDriftDescriptor(
                        drift_kind="whole_file_rewrite",
                        source_path=record.old_path,
                        target_paths=(record.new_path,),
                    )
                )
    return tuple(descriptors)


@dataclass(frozen=True)
class OriginalFindingCriterion:
    """Immutable original concern captured when an obligation first enters inventory."""

    obligation_id: str
    original_fingerprint: str
    resolution_criterion: str
    origin_head_sha: str
    origin_path: str | None
    origin_line: int | None
    origin_side: str
    origin_symbol: str | None
    defect_kind: str
    concern: str | None
    blocking: bool
    needs_human: bool
    impact: str | None
    evidence: str

    def __post_init__(self) -> None:
        _require_sha256(self.obligation_id, label="obligation_id")
        _require_sha256(self.original_fingerprint, label="original_fingerprint")
        _require_sha256(self.resolution_criterion, label="resolution_criterion")
        _require_commit_sha(self.origin_head_sha, label="origin_head_sha")
        if self.origin_path is not None:
            validate_repository_path(
                self.origin_path, allow_glob_chars=True, label="origin_path"
            )
        if self.origin_line is not None and (
            type(self.origin_line) is not int or self.origin_line < 1
        ):
            raise ReviewInputError("origin_line must be a positive integer")
        if self.origin_side not in {"LEFT", "RIGHT"}:
            raise ReviewInputError("origin_side must be LEFT or RIGHT")
        if self.origin_symbol is not None and (
            not isinstance(self.origin_symbol, str) or not self.origin_symbol.strip()
        ):
            raise ReviewInputError("origin_symbol is invalid")
        if not isinstance(self.defect_kind, str) or not self.defect_kind.strip():
            raise ReviewInputError("defect_kind is invalid")
        if self.concern is not None:
            _require_sha256(self.concern, label="concern")
        _require_bool(self.blocking, label="blocking")
        _require_bool(self.needs_human, label="needs_human")
        if self.impact not in IMPACT_RANKS:
            raise ReviewInputError("impact is invalid")
        if self.evidence not in EVIDENCE_RANKS:
            raise ReviewInputError("evidence is invalid")
        if self.evidence == "advisory" and (
            self.blocking or self.needs_human or self.impact is not None
        ):
            raise ReviewInputError("advisory criterion cannot be blocking or ranked")

    @property
    def required(self) -> bool:
        return self.blocking or self.needs_human


@dataclass(frozen=True)
class PresentLocationAnchor:
    """Present diff/repository anchor separated from the original criterion."""

    head_sha: str
    path: str | None
    line: int | None
    side: str
    symbol: str | None
    anchor_location: str
    drift_kind: str = "unchanged"
    candidate_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_commit_sha(self.head_sha, label="anchor head_sha")
        if self.path is not None:
            validate_repository_path(
                self.path, allow_glob_chars=True, label="anchor path"
            )
        if self.line is not None and (type(self.line) is not int or self.line < 1):
            raise ReviewInputError("anchor line must be a positive integer")
        if self.side not in {"LEFT", "RIGHT", "FILE"}:
            raise ReviewInputError("anchor side is invalid")
        if self.symbol is not None and (
            not isinstance(self.symbol, str) or not self.symbol.strip()
        ):
            raise ReviewInputError("anchor symbol is invalid")
        if self.anchor_location not in ANCHOR_LOCATIONS:
            raise ReviewInputError("anchor_location is invalid")
        if self.drift_kind not in DRIFT_KINDS:
            raise ReviewInputError("drift_kind is invalid")
        if self.anchor_location in {"left", "right"} and (
            self.path is None or self.line is None
        ):
            raise ReviewInputError("inline anchor_location requires path and line")
        for candidate in self.candidate_paths:
            validate_repository_path(
                candidate, allow_glob_chars=True, label="candidate_path"
            )

    @property
    def inline_anchorable(self) -> bool:
        return self.anchor_location in {"left", "right"}


@dataclass(frozen=True)
class NativeThreadBinding:
    """Native review thread/comment binding independent of host UI resolution."""

    thread_id: str
    comment_id: int
    placement: str
    body_reason: str | None
    slot_state: str
    proof_sha256: str
    host_marked_outdated: bool = False
    host_marked_resolved: bool = False

    def __post_init__(self) -> None:
        _require_target_id(self.thread_id, label="thread_id")
        if type(self.comment_id) is not int or self.comment_id < 1:
            raise ReviewInputError("comment_id must be a positive integer")
        if self.placement not in {INLINE_PLACEMENT, BODY_PLACEMENT}:
            raise ReviewInputError("placement is invalid")
        if self.placement == INLINE_PLACEMENT and self.body_reason is not None:
            raise ReviewInputError("inline placement cannot carry body_reason")
        if self.placement == BODY_PLACEMENT and (
            not isinstance(self.body_reason, str) or not self.body_reason.strip()
        ):
            raise ReviewInputError("body placement requires body_reason")
        if self.slot_state not in {"active", "contingent", "uncertain", "released"}:
            raise ReviewInputError("slot_state is invalid")
        _require_sha256(self.proof_sha256, label="proof_sha256")
        _require_bool(self.host_marked_outdated, label="host_marked_outdated")
        _require_bool(self.host_marked_resolved, label="host_marked_resolved")

    def to_slot(self) -> PublishedSlot:
        return PublishedSlot(
            target=self.thread_id,
            state=self.slot_state,
            proof_sha256=self.proof_sha256,
        )


@dataclass(frozen=True)
class DriftTrackedObligation:
    """Authoritative obligation separating criterion, present anchor, and thread."""

    criterion: OriginalFindingCriterion
    present_anchor: PresentLocationAnchor
    lifecycle_state: str
    generation: int = 0
    thread_binding: NativeThreadBinding | None = None
    resolution_proof_sha256: str | None = None
    proposal_rank: int = DEFAULT_PROPOSAL_RANK
    latest_comment: ReviewComment | None = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if type(self.criterion) is not OriginalFindingCriterion:
            raise ReviewInputError("criterion must be an OriginalFindingCriterion")
        if type(self.present_anchor) is not PresentLocationAnchor:
            raise ReviewInputError("present_anchor must be a PresentLocationAnchor")
        if self.lifecycle_state not in DRIFT_LIFECYCLE_STATES:
            raise ReviewInputError("drift lifecycle_state is invalid")
        if type(self.generation) is not int or self.generation < 0:
            raise ReviewInputError("generation is invalid")
        if type(self.proposal_rank) is not int or not 0 <= self.proposal_rank <= 99:
            raise ReviewInputError("proposal_rank is invalid")
        if (
            self.thread_binding is not None
            and type(self.thread_binding) is not NativeThreadBinding
        ):
            raise ReviewInputError("thread_binding must be a NativeThreadBinding")
        if self.lifecycle_state in DRIFT_RESOLVED_STATES:
            _require_sha256(
                self.resolution_proof_sha256, label="resolution_proof_sha256"
            )
        elif self.resolution_proof_sha256 is not None:
            raise ReviewInputError(
                "unresolved drift obligation cannot carry resolution_proof_sha256"
            )

    @property
    def obligation_id(self) -> str:
        return self.criterion.obligation_id

    @property
    def unresolved(self) -> bool:
        return self.lifecycle_state in DRIFT_UNRESOLVED_STATES

    @property
    def blocks_approval_now(self) -> bool:
        if not self.unresolved:
            return False
        return self.criterion.required or self.lifecycle_state in {
            "omitted-uncertain",
            "ambiguous-multi-target",
        }

    @property
    def implicated_paths(self) -> tuple[str, ...]:
        paths: list[str] = []
        for candidate in (
            *self.present_anchor.candidate_paths,
            self.present_anchor.path,
            self.criterion.origin_path,
        ):
            if candidate is not None and candidate not in paths:
                paths.append(candidate)
        return tuple(paths)

    @property
    def priority_key(self) -> tuple[int, int, int, int, str]:
        return (
            int(not self.criterion.required),
            IMPACT_RANKS[self.criterion.impact],
            EVIDENCE_RANKS[self.criterion.evidence],
            self.proposal_rank,
            self.criterion.obligation_id,
        )

    def synthetic_comment_for_placement(self) -> ReviewComment:
        if self.latest_comment is not None:
            return replace(
                self.latest_comment,
                blocking=self.criterion.blocking,
                needs_human=self.criterion.needs_human
                or self.lifecycle_state == "ambiguous-multi-target",
                effective_blocking=self.blocks_approval_now,
            )
        anchor_path = (
            self.present_anchor.path or self.criterion.origin_path or "UNANCHORED"
        )
        anchor_line = (
            self.present_anchor.line if self.present_anchor.inline_anchorable else None
        )
        anchor_side = (
            self.present_anchor.side
            if self.present_anchor.inline_anchorable
            else "FILE"
        )
        return ReviewComment(
            path=anchor_path,
            line=anchor_line,
            side=anchor_side,
            body=(
                f"Preserved obligation ({self.criterion.defect_kind}) "
                f"from {self.criterion.origin_path or anchor_path}."
            ),
            blocking=self.criterion.blocking,
            needs_human=self.criterion.needs_human
            or self.lifecycle_state == "ambiguous-multi-target",
            effective_blocking=self.blocks_approval_now,
            symbol=self.present_anchor.symbol or self.criterion.origin_symbol,
            defect_kind=self.criterion.defect_kind,
        )

    def to_baseline_finding(self) -> BaselineFinding:
        return BaselineFinding(
            fingerprint=self.criterion.original_fingerprint,
            resolution_criterion=self.criterion.resolution_criterion,
            concern=self.criterion.concern,
            path=self.present_anchor.path or self.criterion.origin_path,
            symbol=self.present_anchor.symbol or self.criterion.origin_symbol,
            defect_kind=self.criterion.defect_kind,
            generation=self.generation,
            blocking=self.criterion.blocking
            or self.lifecycle_state == "ambiguous-multi-target",
        )


def drift_obligation_from_comment(
    comment: ReviewComment,
    *,
    head_sha: str,
    generation: int = 0,
    proposal_rank: int = DEFAULT_PROPOSAL_RANK,
    impact: str | None = None,
    evidence: str | None = None,
    obligation_id: str | None = None,
) -> DriftTrackedObligation:
    """Create a ``DriftTrackedObligation`` from an initial ``ReviewComment``."""

    if not isinstance(comment, ReviewComment):
        raise ReviewInputError("drift obligation requires a ReviewComment")
    _require_commit_sha(head_sha, label="head_sha")
    lifecycle = finding_lifecycle_for_comment(comment, generation=generation)
    defect_kind = comment_defect_kind(comment)
    required = comment.blocks_approval or comment.needs_human
    resolved_evidence = (
        evidence
        if evidence is not None
        else ("supported-source" if required else "advisory")
    )
    if resolved_evidence == "advisory":
        resolved_impact = None
    elif impact is not None:
        resolved_impact = impact
    else:
        sev = (comment.severity or "").strip().lower()
        resolved_impact = (
            sev
            if sev in {"critical", "high", "medium", "low"}
            else ("high" if required else "low")
        )
    oid = (
        obligation_id
        if obligation_id is not None
        else hashlib.sha256(
            b"reviewsensei:drift-obligation:v1:"
            + canonical_bytes(
                {
                    "fingerprint": lifecycle.fingerprint,
                    "instance": finding_instance_fingerprint(comment),
                    "origin_head_sha": head_sha,
                }
            )
        ).hexdigest()
    )
    origin_side = comment.side if comment.side in {"LEFT", "RIGHT"} else "RIGHT"
    criterion = OriginalFindingCriterion(
        obligation_id=oid,
        original_fingerprint=lifecycle.fingerprint,
        resolution_criterion=resolution_criterion_digest(
            fingerprint=lifecycle.fingerprint,
            evidence_id=comment.evidence_id,
            defect_kind=defect_kind,
            path=comment.path,
            symbol=comment.symbol,
        ),
        origin_head_sha=head_sha,
        origin_path=comment.path,
        origin_line=comment.line,
        origin_side=origin_side,
        origin_symbol=comment.symbol,
        defect_kind=defect_kind,
        concern=lifecycle.concern,
        blocking=comment.blocks_approval,
        needs_human=comment.needs_human,
        impact=resolved_impact,
        evidence=resolved_evidence,
    )
    if (
        comment.path is not None
        and comment.line is not None
        and comment.side
        in {
            "LEFT",
            "RIGHT",
        }
    ):
        anchor_loc = "left" if comment.side == "LEFT" else "right"
        anchor_side = comment.side
    elif comment.path is not None:
        anchor_loc = "file"
        anchor_side = "FILE"
    else:
        anchor_loc = "summary"
        anchor_side = "FILE"
    anchor = PresentLocationAnchor(
        head_sha=head_sha,
        path=comment.path,
        line=comment.line if anchor_loc in {"left", "right"} else None,
        side=anchor_side,
        symbol=comment.symbol,
        anchor_location=anchor_loc,
        drift_kind="unchanged",
        candidate_paths=(comment.path,) if comment.path is not None else (),
    )
    return DriftTrackedObligation(
        criterion=criterion,
        present_anchor=anchor,
        lifecycle_state="active-unresolved",
        generation=generation,
        proposal_rank=proposal_rank,
        latest_comment=comment,
    )


@dataclass(frozen=True)
class DriftInventory:
    """Complete authoritative drift inventory across displayed and hidden items."""

    obligations: tuple[DriftTrackedObligation, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.obligations, tuple)
            or len(self.obligations) > DEFAULT_REVIEW_LIMITS.max_comments * 4
            or any(type(o) is not DriftTrackedObligation for o in self.obligations)
        ):
            raise ReviewInputError("drift inventory is invalid")
        seen: set[str] = set()
        active_threads: set[str] = set()
        for item in self.obligations:
            if item.obligation_id in seen:
                raise ReviewInputError("duplicate obligation_id in drift inventory")
            seen.add(item.obligation_id)
            if item.thread_binding is not None and item.unresolved:
                tid = item.thread_binding.thread_id
                if tid in active_threads:
                    raise ReviewInputError(
                        "multiple unresolved obligations share one thread_id"
                    )
                active_threads.add(tid)

    @property
    def unresolved_obligations(self) -> tuple[DriftTrackedObligation, ...]:
        return tuple(item for item in self.obligations if item.unresolved)

    @property
    def blocking_obligations(self) -> tuple[DriftTrackedObligation, ...]:
        return tuple(item for item in self.obligations if item.blocks_approval_now)

    @property
    def has_omitted_uncertainty(self) -> bool:
        return any(
            item.lifecycle_state == "omitted-uncertain" for item in self.obligations
        )

    @property
    def has_ambiguous_drift(self) -> bool:
        return any(
            item.lifecycle_state == "ambiguous-multi-target"
            for item in self.obligations
        )


@dataclass(frozen=True)
class DriftActiveEntry:
    """Displayed active-window entry carrying drift and placement routing."""

    obligation_id: str
    original_fingerprint: str
    origin_path: str | None
    present_path: str | None
    present_line: int | None
    present_side: str
    drift_kind: str
    lifecycle_state: str
    placement: str
    body_reason: str | None
    thread_id: str | None
    thread_action: str

    def to_public_dict(self) -> dict[str, object]:
        return {
            "obligation_id": self.obligation_id,
            "original_fingerprint": self.original_fingerprint,
            "origin_path": self.origin_path,
            "present_path": self.present_path,
            "present_line": self.present_line,
            "present_side": self.present_side,
            "drift_kind": self.drift_kind,
            "lifecycle_state": self.lifecycle_state,
            "placement": self.placement,
            "body_reason": self.body_reason,
            "thread_id": self.thread_id,
            "thread_action": self.thread_action,
        }


@dataclass(frozen=True)
class DriftEvaluationResult:
    """Complete immutable round result for drift-aware active-window evaluation."""

    head_sha: str
    base_sha: str
    round_number: int
    review_complete: bool
    max_active_findings: int
    changed_file_coverage: ChangedFileCoverageState
    inventory: DriftInventory
    active_entries: tuple[DriftActiveEntry, ...]
    slots: tuple[PublishedSlot, ...]
    approved: bool
    withheld_reasons: tuple[str, ...]
    recovery_steps: tuple[ActiveWindowRecoveryStep, ...]
    body_placement_note: str | None

    def to_public_status(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": PUBLIC_WINDOW_SCHEMA_VERSION,
            "head_sha": self.head_sha,
            "base_sha": self.base_sha,
            "round_number": self.round_number,
            "coverage_state": self.changed_file_coverage.approval_state,
            "max_active_findings": self.max_active_findings,
            "active_findings": [
                entry.to_public_dict() for entry in self.active_entries
            ],
            "approval_eligible": self.approved,
            "outcome": "approved" if self.approved else "withheld",
            "withheld_reasons": list(self.withheld_reasons),
            "recovery_routes": [step.to_public_dict() for step in self.recovery_steps],
            "body_placement_note": self.body_placement_note,
        }
        assert_no_deferred_leakage(payload)
        return payload


def _expected_paths_for_prior(
    prior: DriftTrackedObligation,
    descriptors: Sequence[FileDriftDescriptor],
) -> tuple[str, tuple[str, ...]]:
    """Determine expected drift kind and candidate target paths for one prior."""

    current_path = prior.present_anchor.path or prior.criterion.origin_path
    if current_path is None:
        return "unchanged", ()

    matching = [d for d in descriptors if d.source_path == current_path]
    if not matching:
        return prior.present_anchor.drift_kind, (current_path,)

    # Prioritize symbol-specific cross-file moves first if symbol matches.
    prior_symbol = prior.present_anchor.symbol or prior.criterion.origin_symbol
    symbol_moves = [
        d
        for d in matching
        if d.drift_kind == "cross_file_move"
        and (not d.moved_symbols or (prior_symbol in d.moved_symbols))
    ]
    if symbol_moves:
        targets: list[str] = []
        for desc in symbol_moves:
            for t in desc.target_paths:
                if t not in targets:
                    targets.append(t)
        return "cross_file_move", tuple(targets)

    copies = [d for d in matching if d.drift_kind == "file_copy"]
    if copies:
        copy_targets = [current_path]
        for desc in copies:
            for t in desc.target_paths:
                if t not in copy_targets:
                    copy_targets.append(t)
        return "file_copy", tuple(copy_targets)

    duplicates = [d for d in matching if d.drift_kind == "duplicate_code"]
    if duplicates:
        dup_targets = [current_path]
        for desc in duplicates:
            for t in desc.target_paths:
                if t not in dup_targets:
                    dup_targets.append(t)
        return "duplicate_code", tuple(dup_targets)

    renames = [d for d in matching if d.drift_kind == "file_rename"]
    if renames:
        return "file_rename", renames[0].target_paths

    rewrites = [d for d in matching if d.drift_kind == "whole_file_rewrite"]
    if rewrites:
        return "whole_file_rewrite", (current_path,)

    deletions = [d for d in matching if d.drift_kind == "line_deletion"]
    if deletions:
        return "line_deletion", ()

    return "unchanged", (current_path,)


def _comment_matches_prior_with_drift(
    comment: ReviewComment,
    prior: DriftTrackedObligation,
    expected_paths: Sequence[str],
) -> bool:
    """Return whether ``comment`` matches ``prior`` across path/symbol drift."""

    lifecycle = finding_lifecycle_for_comment(comment)
    defect_kind = comment_defect_kind(comment)
    prior_symbol = prior.present_anchor.symbol or prior.criterion.origin_symbol
    prior_path = prior.present_anchor.path or prior.criterion.origin_path

    if lifecycle.fingerprint == prior.criterion.original_fingerprint:
        return True
    if (
        lifecycle.concern is not None
        and prior.criterion.concern is not None
        and lifecycle.concern == prior.criterion.concern
    ):
        return True

    allowed_paths = set(expected_paths)
    if prior_path is not None:
        allowed_paths.add(prior_path)
    if prior.criterion.origin_path is not None:
        allowed_paths.add(prior.criterion.origin_path)

    # Cross-file symbol + defect_kind identity survives path changes!
    if (
        prior_symbol
        and comment.symbol == prior_symbol
        and defect_kind == prior.criterion.defect_kind
        and defect_kind != "unknown"
    ):
        return True

    # Within expected rename/copy/rewrite target path, unique defect_kind matches.
    if (
        comment.path is not None
        and comment.path in allowed_paths
        and defect_kind == prior.criterion.defect_kind
        and defect_kind != "unknown"
    ):
        return True

    return False


def evaluate_drift_round(
    result: ReviewResult,
    *,
    head_sha: str,
    base_sha: str,
    max_active_findings: int,
    round_number: int = 1,
    coverage_mode: str = "full",
    prior_inventory: DriftInventory | None = None,
    drift_descriptors: Sequence[FileDriftDescriptor] = (),
    diff_text: str | None = None,
    host_outdated_threads: frozenset[str] = frozenset(),
    host_resolved_threads: frozenset[str] = frozenset(),
    confirmed_fixes: Sequence[ConfirmedFixEvidence] = (),
    human_overrides: Sequence[AuthorizedHumanOverride] = (),
    authorized_rescan: AuthorizedRescan | None = None,
    coverage_acceptance: AuthorizedCoverageAcceptance | None = None,
    placement_facts: HostPlacementFacts | None = None,
    convergence_policy: ReviewConvergencePolicy | None = None,
    advisory: bool = False,
) -> DriftEvaluationResult:
    """Evaluate one bounded review round across cross-file and thread drift."""

    if not isinstance(result, ReviewResult):
        raise ReviewInputError("evaluate_drift_round requires a ReviewResult")
    _require_commit_sha(head_sha, label="head_sha")
    _require_commit_sha(base_sha, label="base_sha")
    if (
        type(max_active_findings) is not int
        or not 1 <= max_active_findings <= DEFAULT_REVIEW_LIMITS.max_comments
    ):
        raise ReviewInputError("max_active_findings is invalid")
    if type(round_number) is not int or round_number < 1:
        raise ReviewInputError("round_number must be >= 1")

    facts = placement_facts or HostPlacementFacts()
    policy = convergence_policy or ReviewConvergencePolicy(mode="merge-focused")

    if authorized_rescan is not None:
        if not isinstance(authorized_rescan, AuthorizedRescan):
            raise ReviewInputError("authorized_rescan must be an AuthorizedRescan")
        if authorized_rescan.source.head_sha != head_sha:
            raise ReviewInputError("authorized_rescan head_sha mismatch")

    descriptors: list[FileDriftDescriptor] = list(drift_descriptors)
    if diff_text is not None:
        for extracted in extract_drift_descriptors_from_diff(diff_text):
            if extracted not in descriptors:
                descriptors.append(extracted)

    coverage_state = ChangedFileCoverageState.from_manifest(
        result.coverage,
        coverage_mode=coverage_mode,
        head_sha=head_sha,
        coverage_acceptance=coverage_acceptance,
    )
    proposals = extract_decision_proposals(result)
    proposal_by_idx = {idx: p for idx, p in enumerate(proposals)}

    fixes_by_id: dict[str, ConfirmedFixEvidence] = {}
    for fix in confirmed_fixes:
        if not isinstance(fix, ConfirmedFixEvidence) or fix.head_sha != head_sha:
            raise ReviewInputError("confirmed fix is invalid or stale")
        if fix.obligation_id in fixes_by_id:
            raise ReviewInputError("duplicate confirmed fix for one obligation_id")
        fixes_by_id[fix.obligation_id] = fix

    overrides_by_id: dict[str, AuthorizedHumanOverride] = {}
    for override in human_overrides:
        if (
            not isinstance(override, AuthorizedHumanOverride)
            or override.head_sha != head_sha
        ):
            raise ReviewInputError("human override is invalid or stale")
        if override.obligation_id in overrides_by_id:
            raise ReviewInputError("duplicate human override for one obligation_id")
        overrides_by_id[override.obligation_id] = override

    prior_items = () if prior_inventory is None else prior_inventory.obligations
    unresolved_priors = [item for item in prior_items if item.unresolved]
    reopenable_priors = [
        item
        for item in prior_items
        if item.lifecycle_state == "verified-fixed"
        and item.thread_binding is not None
        and item.thread_binding.slot_state == "released"
    ]
    prior_by_id = {item.obligation_id: item for item in prior_items}

    for oid in set(fixes_by_id) | set(overrides_by_id):
        if oid not in prior_by_id or not prior_by_id[oid].unresolved:
            raise ReviewInputError(
                "resolution references unknown or already-resolved obligation_id"
            )

    review_complete = (
        result.review_status == "complete" and coverage_state.fully_reviewed
    )
    full_rescan_qualified = (
        authorized_rescan is not None
        and authorized_rescan.mode == "full"
        and review_complete
    )
    rescan_proof = (
        hashlib.sha256(
            canonical_bytes(
                {
                    "kind": authorized_rescan.kind,
                    "request_id": authorized_rescan.request_id,
                    "head_sha": head_sha,
                    "transaction_id": authorized_rescan.source.transaction_id,
                }
            )
        ).hexdigest()
        if full_rescan_qualified and authorized_rescan is not None
        else None
    )

    drift_plan: dict[str, tuple[str, tuple[str, ...]]] = {
        prior.obligation_id: _expected_paths_for_prior(prior, descriptors)
        for prior in unresolved_priors
    }

    # Match current comments against unresolved prior obligations.
    priors_for_comment: dict[int, list[DriftTrackedObligation]] = {
        idx: [] for idx in range(len(result.comments))
    }
    comments_for_prior: dict[str, list[int]] = {
        prior.obligation_id: [] for prior in unresolved_priors
    }
    for idx, comment in enumerate(result.comments):
        for prior in unresolved_priors:
            _dkind, expected_paths = drift_plan[prior.obligation_id]
            if _comment_matches_prior_with_drift(comment, prior, expected_paths):
                priors_for_comment[idx].append(prior)
                comments_for_prior[prior.obligation_id].append(idx)

    ambiguous_prior_ids: set[str] = set()
    ambiguous_comment_indices: set[int] = set()
    for idx, matched_priors in priors_for_comment.items():
        if len(matched_priors) > 1:
            ambiguous_comment_indices.add(idx)
            for p in matched_priors:
                ambiguous_prior_ids.add(p.obligation_id)
    for oid, matched_indices in comments_for_prior.items():
        if len(matched_indices) > 1:
            ambiguous_prior_ids.add(oid)
            ambiguous_comment_indices.update(matched_indices)

    # Check regressions of previously fixed obligations on unmatched comments.
    reopened_by_prior: dict[str, int] = {}
    for idx, comment in enumerate(result.comments):
        if priors_for_comment[idx]:
            continue
        reopen_matches = [
            p
            for p in reopenable_priors
            if _comment_matches_prior_with_drift(
                comment, p, (p.present_anchor.path,) if p.present_anchor.path else ()
            )
        ]
        if len(reopen_matches) == 1:
            rp = reopen_matches[0]
            if rp.obligation_id not in reopened_by_prior:
                reopened_by_prior[rp.obligation_id] = idx

    reviewed_set = set(coverage_state.reviewed_paths)
    next_obligations: list[DriftTrackedObligation] = []

    for item in prior_items:
        if item.unresolved:
            continue
        if item.obligation_id in reopened_by_prior:
            c_idx = reopened_by_prior[item.obligation_id]
            comment = result.comments[c_idx]
            anchor_loc = (
                "left"
                if comment.side == "LEFT" and comment.line is not None
                else ("right" if comment.line is not None else "file")
            )
            next_obligations.append(
                replace(
                    item,
                    present_anchor=PresentLocationAnchor(
                        head_sha=head_sha,
                        path=comment.path,
                        line=comment.line if anchor_loc in {"left", "right"} else None,
                        side=comment.side
                        if anchor_loc in {"left", "right"}
                        else "FILE",
                        symbol=comment.symbol or item.present_anchor.symbol,
                        anchor_location=anchor_loc,
                        drift_kind=item.present_anchor.drift_kind,
                        candidate_paths=(comment.path,) if comment.path else (),
                    ),
                    lifecycle_state="active-unresolved",
                    resolution_proof_sha256=None,
                    latest_comment=comment,
                )
            )
        else:
            next_obligations.append(item)

    for prior in unresolved_priors:
        drift_kind, expected_paths = drift_plan[prior.obligation_id]
        binding = prior.thread_binding
        if binding is not None:
            binding = replace(
                binding,
                host_marked_outdated=(
                    binding.host_marked_outdated
                    or binding.thread_id in host_outdated_threads
                    or drift_kind
                    in {
                        "file_rename",
                        "cross_file_move",
                        "line_deletion",
                        "whole_file_rewrite",
                    }
                ),
                host_marked_resolved=(
                    binding.host_marked_resolved
                    or binding.thread_id in host_resolved_threads
                ),
            )

        prior_override = overrides_by_id.get(prior.obligation_id)
        prior_fix = fixes_by_id.get(prior.obligation_id)

        if prior_override is not None:
            if binding is not None:
                if binding.slot_state == "uncertain":
                    raise ReviewInputError(
                        "uncertain thread slot must reconcile before override release"
                    )
                binding = replace(
                    binding,
                    slot_state=(
                        "released" if prior_override.slot_released else "contingent"
                    ),
                    proof_sha256=prior_override.proof_sha256,
                )
            next_obligations.append(
                replace(
                    prior,
                    present_anchor=replace(
                        prior.present_anchor,
                        head_sha=head_sha,
                        drift_kind=drift_kind,
                        candidate_paths=expected_paths,
                    ),
                    lifecycle_state="authorized-override",
                    thread_binding=binding,
                    resolution_proof_sha256=prior_override.proof_sha256,
                )
            )
            continue

        # Positive current-head fix proof bound to the ORIGINAL resolution_criterion!
        if prior_fix is not None:
            if prior_fix.resolution_criterion != prior.criterion.resolution_criterion:
                raise ReviewInputError(
                    "confirmed fix criterion does not match original criterion"
                )
            required_paths = (
                expected_paths
                if expected_paths
                else (
                    (prior.present_anchor.path,)
                    if prior.present_anchor.path is not None
                    else ()
                )
            )
            paths_verified = all(
                p in prior_fix.verified_paths and p in reviewed_set
                for p in required_paths
            )
            if (
                review_complete
                and paths_verified
                and not comments_for_prior[prior.obligation_id]
            ):
                if binding is not None:
                    if binding.slot_state == "uncertain":
                        raise ReviewInputError(
                            "uncertain thread slot must reconcile before fix release"
                        )
                    binding = replace(
                        binding,
                        slot_state="released",
                        proof_sha256=prior_fix.evidence_sha256,
                    )
                next_obligations.append(
                    replace(
                        prior,
                        present_anchor=replace(
                            prior.present_anchor,
                            head_sha=head_sha,
                            path=(
                                expected_paths[0]
                                if len(expected_paths) == 1
                                else prior.present_anchor.path
                            ),
                            drift_kind=drift_kind,
                            candidate_paths=expected_paths,
                        ),
                        lifecycle_state="verified-fixed",
                        thread_binding=binding,
                        resolution_proof_sha256=prior_fix.evidence_sha256,
                    )
                )
                continue

        # Multi-target ambiguity from structural file_copy / split move / duplicate_code
        # OR multiple current comments claiming the same prior criterion!
        matched_indices = comments_for_prior[prior.obligation_id]
        structural_multi_target = len(expected_paths) > 1 and drift_kind in {
            "file_copy",
            "cross_file_move",
            "duplicate_code",
        }
        if prior.obligation_id in ambiguous_prior_ids or structural_multi_target:
            cand_paths = list(expected_paths)
            for idx in matched_indices:
                c_path = result.comments[idx].path
                if c_path is not None and c_path not in cand_paths:
                    cand_paths.append(c_path)
            effective_drift = (
                drift_kind if drift_kind != "unchanged" else "duplicate_code"
            )
            unanchored = PresentLocationAnchor(
                head_sha=head_sha,
                path=cand_paths[0] if cand_paths else prior.present_anchor.path,
                line=None,
                side="FILE",
                symbol=prior.present_anchor.symbol or prior.criterion.origin_symbol,
                anchor_location="file" if cand_paths else "summary",
                drift_kind=effective_drift,
                candidate_paths=tuple(cand_paths),
            )
            if (
                full_rescan_qualified
                and rescan_proof is not None
                and not matched_indices
            ):
                if binding is not None and binding.slot_state != "uncertain":
                    binding = replace(
                        binding, slot_state="released", proof_sha256=rescan_proof
                    )
                next_obligations.append(
                    replace(
                        prior,
                        present_anchor=unanchored,
                        lifecycle_state="rescan-superseded",
                        thread_binding=binding,
                        resolution_proof_sha256=rescan_proof,
                    )
                )
            else:
                next_obligations.append(
                    replace(
                        prior,
                        criterion=replace(
                            prior.criterion, blocking=True, needs_human=True
                        ),
                        present_anchor=unanchored,
                        lifecycle_state="ambiguous-multi-target",
                        thread_binding=binding,
                    )
                )
            continue

        if len(matched_indices) == 1:
            c_idx = matched_indices[0]
            comment = result.comments[c_idx]
            proposal = proposal_by_idx.get(c_idx)
            inferred_drift = drift_kind
            prior_path = prior.present_anchor.path or prior.criterion.origin_path
            if (
                inferred_drift == "unchanged"
                and comment.path is not None
                and prior_path is not None
                and comment.path != prior_path
            ):
                inferred_drift = "cross_file_move"
            if (
                comment.path is not None
                and comment.line is not None
                and comment.side in {"LEFT", "RIGHT"}
            ):
                anchor_loc = "left" if comment.side == "LEFT" else "right"
                anchor_side = comment.side
                anchor_line = comment.line
            elif comment.path is not None:
                anchor_loc = "file"
                anchor_side = "FILE"
                anchor_line = None
            else:
                anchor_loc = "summary"
                anchor_side = "FILE"
                anchor_line = None

            downgraded = prior.criterion.blocking and not comment.blocks_approval
            next_obligations.append(
                replace(
                    prior,
                    criterion=replace(
                        prior.criterion,
                        blocking=prior.criterion.blocking or comment.blocks_approval,
                        needs_human=prior.criterion.needs_human or comment.needs_human,
                    ),
                    present_anchor=PresentLocationAnchor(
                        head_sha=head_sha,
                        path=comment.path,
                        line=anchor_line,
                        side=anchor_side,
                        symbol=comment.symbol or prior.present_anchor.symbol,
                        anchor_location=anchor_loc,
                        drift_kind=inferred_drift,
                        candidate_paths=(comment.path,) if comment.path else (),
                    ),
                    lifecycle_state=(
                        "omitted-uncertain" if downgraded else "active-unresolved"
                    ),
                    thread_binding=binding,
                    proposal_rank=(
                        proposal.proposal_rank
                        if proposal is not None
                        else prior.proposal_rank
                    ),
                    latest_comment=comment,
                )
            )
            continue

        # Prior obligation was omitted from result.comments and has no confirmed fix.
        if full_rescan_qualified and rescan_proof is not None:
            if binding is not None and binding.slot_state != "uncertain":
                binding = replace(
                    binding, slot_state="released", proof_sha256=rescan_proof
                )
            next_obligations.append(
                replace(
                    prior,
                    present_anchor=replace(
                        prior.present_anchor,
                        head_sha=head_sha,
                        drift_kind=drift_kind,
                        candidate_paths=expected_paths,
                    ),
                    lifecycle_state="rescan-superseded",
                    thread_binding=binding,
                    resolution_proof_sha256=rescan_proof,
                )
            )
            continue

        # Outdated anchor / line deletion / rename / rewrite / host UI resolve
        # NEVER resolves the concern! Unanchor cleanly if lines were deleted or moved.
        target_path = (
            expected_paths[0]
            if len(expected_paths) == 1
            else (None if drift_kind == "line_deletion" else prior.present_anchor.path)
        )
        lost_inline_anchor = drift_kind in {
            "line_deletion",
            "whole_file_rewrite",
            "file_rename",
            "cross_file_move",
        } or (binding is not None and binding.host_marked_outdated)
        new_anchor = PresentLocationAnchor(
            head_sha=head_sha,
            path=target_path,
            line=None if lost_inline_anchor else prior.present_anchor.line,
            side=("FILE" if lost_inline_anchor else prior.present_anchor.side),
            symbol=prior.present_anchor.symbol,
            anchor_location=(
                ("file" if target_path is not None else "summary")
                if lost_inline_anchor
                else prior.present_anchor.anchor_location
            ),
            drift_kind=drift_kind,
            candidate_paths=expected_paths,
        )
        implicated_reviewed = (
            drift_kind != "unchanged"
            or coverage_mode == "fallback-full"
            or target_path is None
            or target_path in reviewed_set
            or (
                prior.criterion.origin_path is not None
                and prior.criterion.origin_path in reviewed_set
            )
        )
        next_obligations.append(
            replace(
                prior,
                present_anchor=new_anchor,
                lifecycle_state=(
                    "omitted-uncertain" if implicated_reviewed else "active-unresolved"
                ),
                thread_binding=binding,
            )
        )

    # Admit genuinely new comments that did not match a unique unresolved/reopened prior.
    # If a comment triggered multi-target ambiguity on an existing prior, do not
    # mint redundant active-slot duplicates when the prior already holds ambiguity,
    # unless multiple brand-new comments duplicate each other without any prior!
    matched_to_any_prior = {
        idx for idx, priors in priors_for_comment.items() if priors
    } | set(reopened_by_prior.values())

    unmatched_comments = [
        (idx, result.comments[idx])
        for idx in range(len(result.comments))
        if idx not in matched_to_any_prior
    ]
    # Detect brand-new duplicate code findings introduced in the same round!
    new_group_keys: dict[tuple[str, str], list[int]] = {}
    for idx, comment in unmatched_comments:
        dk = comment_defect_kind(comment)
        if comment.symbol and dk != "unknown":
            new_group_keys.setdefault((comment.symbol, dk), []).append(idx)

    duplicate_new_groups = {
        key: indices for key, indices in new_group_keys.items() if len(indices) > 1
    }
    consumed_new_duplicates: set[int] = set()
    for (_sym, _dk), indices in duplicate_new_groups.items():
        consumed_new_duplicates.update(indices)
        primary_idx = indices[0]
        primary_comment = result.comments[primary_idx]
        dup_cand_paths: list[str] = []
        for i in indices:
            cp = result.comments[i].path
            if cp is not None and cp not in dup_cand_paths:
                dup_cand_paths.append(cp)
        created = drift_obligation_from_comment(
            primary_comment,
            head_sha=head_sha,
            generation=round_number - 1,
            proposal_rank=(
                proposal_by_idx[primary_idx].proposal_rank
                if primary_idx in proposal_by_idx
                else DEFAULT_PROPOSAL_RANK
            ),
        )
        created = replace(
            created,
            criterion=replace(created.criterion, blocking=True, needs_human=True),
            present_anchor=PresentLocationAnchor(
                head_sha=head_sha,
                path=primary_comment.path,
                line=None,
                side="FILE",
                symbol=primary_comment.symbol,
                anchor_location="file" if primary_comment.path else "summary",
                drift_kind="duplicate_code",
                candidate_paths=tuple(dup_cand_paths),
            ),
            lifecycle_state="ambiguous-multi-target",
        )
        next_obligations.append(created)

    for idx, comment in unmatched_comments:
        if idx in consumed_new_duplicates:
            continue
        proposal = proposal_by_idx.get(idx)
        next_obligations.append(
            drift_obligation_from_comment(
                comment,
                head_sha=head_sha,
                generation=round_number - 1,
                proposal_rank=(
                    proposal.proposal_rank
                    if proposal is not None
                    else DEFAULT_PROPOSAL_RANK
                ),
            )
        )

    inventory = DriftInventory(obligations=tuple(next_obligations))

    # Build bounded priority-first active window reusing existing threads first!
    slot_by_id: dict[str, PublishedSlot] = {}
    for item in inventory.obligations:
        if item.thread_binding is not None:
            slot = item.thread_binding.to_slot()
            slot_by_id[slot.target] = slot
    slots = tuple(slot_by_id.values())
    reserved_count = sum(s.reserved for s in slots)
    if reserved_count > max_active_findings:
        raise ReviewInputError("reserved native threads exceed max_active_findings")

    incumbents: list[DriftTrackedObligation] = []
    challengers: list[DriftTrackedObligation] = []
    for item in sorted(inventory.unresolved_obligations, key=lambda o: o.priority_key):
        if item.thread_binding is not None:
            st = item.thread_binding.slot_state
            if st == "uncertain":
                continue
            if st in {"active", "contingent"}:
                incumbents.append(item)
                continue
        challengers.append(item)

    active_entries: list[DriftActiveEntry] = []
    body_reasons: list[str] = []

    def build_entry(
        item: DriftTrackedObligation, *, consumes_new: bool
    ) -> DriftActiveEntry:
        placement, reason = plan_finding_placement_detail(
            item.synthetic_comment_for_placement(),
            anchor=item.present_anchor.anchor_location,
            facts=facts,
            policy=policy,
            advisory=advisory,
        )
        if reason is not None:
            body_reasons.append(reason)
        tb = item.thread_binding
        if tb is None:
            t_action = "create" if placement == INLINE_PLACEMENT else "body-only"
            tid = None
        elif tb.slot_state == "released" or tb.host_marked_resolved:
            t_action = "reopen"
            tid = tb.thread_id
        else:
            t_action = "update"
            tid = tb.thread_id
        return DriftActiveEntry(
            obligation_id=item.obligation_id,
            original_fingerprint=item.criterion.original_fingerprint,
            origin_path=item.criterion.origin_path,
            present_path=item.present_anchor.path,
            present_line=item.present_anchor.line,
            present_side=item.present_anchor.side,
            drift_kind=item.present_anchor.drift_kind,
            lifecycle_state=item.lifecycle_state,
            placement=placement,
            body_reason=reason,
            thread_id=tid,
            thread_action=t_action if not consumes_new or tb is not None else t_action,
        )

    for item in incumbents:
        active_entries.append(build_entry(item, consumes_new=False))

    for item in challengers:
        if reserved_count >= max_active_findings:
            break
        entry = build_entry(item, consumes_new=True)
        active_entries.append(entry)
        reserved_count += 1

    # Compute approval verdict + Universal Review Recovery Guarantee steps.
    reasons: list[str] = []
    steps: list[ActiveWindowRecoveryStep] = []

    def add_step(route: str, code: str, scope: str) -> None:
        mapped_code = (
            "ambiguous-finding-identity" if code == "ambiguous-drift-target" else code
        )
        step = ActiveWindowRecoveryStep(
            route=route, reason_code=mapped_code, target_scope=scope
        )
        if step not in steps:
            steps.append(step)

    if not coverage_state.coverage_satisfied:
        if coverage_state.approval_state == "partial":
            reasons.append("coverage-partial")
            add_step("rescan", "coverage-partial", "pull-request")
            add_step("accept-coverage-uncertainty", "coverage-partial", "pull-request")
        else:
            reasons.append("coverage-incomplete")
            add_step("rescan", "coverage-incomplete", "pull-request")

    if not review_complete and not coverage_state.coverage_uncertainty_accepted:
        reasons.append("review-incomplete")
        add_step("verify", "review-incomplete", "pull-request")
        add_step("rescan", "review-incomplete", "pull-request")

    if any(s.state == "uncertain" for s in slots):
        reasons.append("uncertain-native-slot")
        add_step("verify", "uncertain-native-slot", "pull-request")

    if inventory.has_ambiguous_drift:
        reasons.append("ambiguous-drift-target")
        add_step("override", "ambiguous-drift-target", "active-window")
        add_step("accept-risk", "ambiguous-drift-target", "active-window")
        add_step("rescan", "ambiguous-drift-target", "pull-request")

    if inventory.has_omitted_uncertainty:
        reasons.append("uncertain-finding-lifecycle")
        add_step("verify", "uncertain-finding-lifecycle", "pull-request")
        add_step("override", "uncertain-finding-lifecycle", "active-window")
        add_step("rescan", "uncertain-finding-lifecycle", "pull-request")

    if inventory.blocking_obligations:
        reasons.append("unresolved-obligations")
        scope = "active-window" if active_entries else "pull-request"
        add_step("verify", "unresolved-obligations", scope)
        add_step("override", "unresolved-obligations", scope)
        add_step("accept-risk", "unresolved-obligations", scope)

    note = placement_note(body_reasons) if body_reasons else None
    return DriftEvaluationResult(
        head_sha=head_sha,
        base_sha=base_sha,
        round_number=round_number,
        review_complete=review_complete,
        max_active_findings=max_active_findings,
        changed_file_coverage=coverage_state,
        inventory=inventory,
        active_entries=tuple(active_entries),
        slots=slots,
        approved=not reasons,
        withheld_reasons=tuple(reasons),
        recovery_steps=tuple(steps),
        body_placement_note=note,
    )


@dataclass(frozen=True)
class NativeThreadRecord:
    """Synthetic native GitHub review thread record used to qualify publication."""

    thread_id: str
    comment_id: int
    obligation_id: str
    anchor_path: str | None
    anchor_line: int | None
    anchor_side: str
    body: str
    resolved: bool = False
    outdated: bool = False
    update_count: int = 0
    reopen_count: int = 0


@dataclass(frozen=True)
class DriftFinalizationReceipt:
    """Deterministic native publication + finalization receipt for one round."""

    head_sha: str
    review_event: str
    check_conclusion: str
    approved: bool
    summary_body: str
    active_thread_ids: tuple[str, ...]
    resolved_thread_ids: tuple[str, ...]
    created_threads: int
    updated_threads: int
    reopened_threads: int
    resolved_threads: int
    public_status: Mapping[str, object]


class SyntheticNativeDriftPublisher:
    """Deterministic stateful native publisher/finalizer qualifying G16 drift."""

    def __init__(self) -> None:
        self._next_number = 1
        self.threads: dict[str, NativeThreadRecord] = {}
        self.review_events: list[DriftFinalizationReceipt] = []

    def mark_host_outdated(self, thread_id: str) -> None:
        rec = self.threads[thread_id]
        self.threads[thread_id] = replace(rec, outdated=True)

    def mark_host_resolved_by_human(self, thread_id: str) -> None:
        rec = self.threads[thread_id]
        self.threads[thread_id] = replace(rec, resolved=True)

    def publish_and_finalize(
        self, evaluation: DriftEvaluationResult
    ) -> tuple[DriftEvaluationResult, DriftFinalizationReceipt]:
        """Apply native thread creates/updates/reopens/resolves and finalize."""

        if not isinstance(evaluation, DriftEvaluationResult):
            raise ReviewInputError(
                "publish_and_finalize requires DriftEvaluationResult"
            )

        created = 0
        updated = 0
        reopened = 0
        resolved_count = 0

        entries_by_oid = {e.obligation_id: e for e in evaluation.active_entries}
        updated_obligations: list[DriftTrackedObligation] = []
        body_lines: list[str] = [
            f"Review round {evaluation.round_number} at {evaluation.head_sha[:12]}."
        ]
        if evaluation.body_placement_note is not None:
            body_lines.append(evaluation.body_placement_note)

        for item in evaluation.inventory.obligations:
            entry = entries_by_oid.get(item.obligation_id)

            # Resolve native threads whose obligations are now resolved + released!
            if (
                not item.unresolved
                and item.thread_binding is not None
                and item.thread_binding.slot_state == "released"
            ):
                tid = item.thread_binding.thread_id
                if tid in self.threads and not self.threads[tid].resolved:
                    self.threads[tid] = replace(self.threads[tid], resolved=True)
                    resolved_count += 1
                updated_obligations.append(item)
                continue

            if entry is None:
                updated_obligations.append(item)
                continue

            rendered = (
                f"[{item.criterion.defect_kind}] "
                f"origin={item.criterion.origin_path}:{item.criterion.origin_line} "
                f"present={item.present_anchor.path}:{item.present_anchor.line} "
                f"drift={item.present_anchor.drift_kind} "
                f"state={item.lifecycle_state}"
            )
            if entry.placement == BODY_PLACEMENT:
                body_lines.append(rendered)

            if entry.thread_action == "create":
                num = self._next_number
                self._next_number += 1
                tid = f"gh-drift-thread-{num}"
                cid = 10_000 + num
                self.threads[tid] = NativeThreadRecord(
                    thread_id=tid,
                    comment_id=cid,
                    obligation_id=item.obligation_id,
                    anchor_path=entry.present_path,
                    anchor_line=entry.present_line,
                    anchor_side=entry.present_side,
                    body=rendered,
                    resolved=False,
                    outdated=False,
                )
                created += 1
                proof = hashlib.sha256(
                    canonical_bytes(
                        {
                            "thread_id": tid,
                            "comment_id": cid,
                            "head_sha": evaluation.head_sha,
                            "obligation_id": item.obligation_id,
                        }
                    )
                ).hexdigest()
                binding = NativeThreadBinding(
                    thread_id=tid,
                    comment_id=cid,
                    placement=entry.placement,
                    body_reason=entry.body_reason,
                    slot_state="active",
                    proof_sha256=proof,
                )
                updated_obligations.append(replace(item, thread_binding=binding))
            elif entry.thread_action in {"update", "reopen"}:
                assert item.thread_binding is not None
                tid = item.thread_binding.thread_id
                existing = self.threads[tid]
                was_reopen = entry.thread_action == "reopen" or existing.resolved
                self.threads[tid] = replace(
                    existing,
                    body=rendered,
                    resolved=False,
                    update_count=existing.update_count + (0 if was_reopen else 1),
                    reopen_count=existing.reopen_count + (1 if was_reopen else 0),
                )
                if was_reopen:
                    reopened += 1
                else:
                    updated += 1
                proof = hashlib.sha256(
                    canonical_bytes(
                        {
                            "thread_id": tid,
                            "head_sha": evaluation.head_sha,
                            "obligation_id": item.obligation_id,
                            "action": "reopen" if was_reopen else "update",
                        }
                    )
                ).hexdigest()
                binding = replace(
                    item.thread_binding,
                    placement=entry.placement,
                    body_reason=entry.body_reason,
                    slot_state="active",
                    proof_sha256=proof,
                    host_marked_resolved=False,
                )
                updated_obligations.append(replace(item, thread_binding=binding))
            else:
                # Unanchored obligation with no prior inline thread stays body-only
                # while still holding its active-window slot and blocking approval!
                updated_obligations.append(item)

        new_inventory = DriftInventory(obligations=tuple(updated_obligations))
        new_slots = tuple(
            item.thread_binding.to_slot()
            for item in new_inventory.obligations
            if item.thread_binding is not None
        )
        refreshed_entries = tuple(
            replace(
                entry,
                thread_id=(
                    next(
                        o.thread_binding.thread_id
                        for o in new_inventory.obligations
                        if o.obligation_id == entry.obligation_id
                        and o.thread_binding is not None
                    )
                    if entry.thread_action == "create"
                    else entry.thread_id
                ),
                thread_action=(
                    "update"
                    if entry.thread_action in {"create", "reopen"}
                    else entry.thread_action
                ),
            )
            for entry in evaluation.active_entries
        )
        committed = replace(
            evaluation,
            inventory=new_inventory,
            active_entries=refreshed_entries,
            slots=new_slots,
        )
        review_event = "APPROVE" if committed.approved else "COMMENT"
        check_conclusion = "success" if committed.approved else "action_required"
        public_status = committed.to_public_status()

        active_tids = tuple(
            sorted(tid for tid, rec in self.threads.items() if not rec.resolved)
        )
        resolved_tids = tuple(
            sorted(tid for tid, rec in self.threads.items() if rec.resolved)
        )
        if len(active_tids) > committed.max_active_findings:
            raise ReviewInputError("published open threads exceed max_active_findings")
        if review_event == "APPROVE" and (
            active_tids or committed.inventory.blocking_obligations
        ):
            raise ReviewInputError(
                "APPROVE forbidden while blocking obligations remain"
            )

        receipt = DriftFinalizationReceipt(
            head_sha=committed.head_sha,
            review_event=review_event,
            check_conclusion=check_conclusion,
            approved=committed.approved,
            summary_body="\n".join(body_lines),
            active_thread_ids=active_tids,
            resolved_thread_ids=resolved_tids,
            created_threads=created,
            updated_threads=updated,
            reopened_threads=reopened,
            resolved_threads=resolved_count,
            public_status=public_status,
        )
        self.review_events.append(receipt)
        return committed, receipt


__all__ = [
    "DRIFT_KINDS",
    "DRIFT_LIFECYCLE_STATES",
    "DRIFT_RESOLVED_STATES",
    "DRIFT_UNRESOLVED_STATES",
    "DRIFT_WITHHELD_REASON_CODES",
    "DriftActiveEntry",
    "DriftEvaluationResult",
    "DriftFinalizationReceipt",
    "DriftInventory",
    "DriftTrackedObligation",
    "FileDriftDescriptor",
    "NativeThreadBinding",
    "NativeThreadRecord",
    "OriginalFindingCriterion",
    "PresentLocationAnchor",
    "SyntheticNativeDriftPublisher",
    "drift_obligation_from_comment",
    "evaluate_drift_round",
    "extract_drift_descriptors_from_diff",
]
