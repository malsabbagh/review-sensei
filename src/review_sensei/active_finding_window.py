"""Bounded active finding window and four-variable lifecycle model (G10).

Separates four state variables that must never be collapsed:
1. ``changed_file_coverage``: per-file/hunk coverage of the reviewed diff.
2. ``authoritative_decision_inventory``: complete obligation ledger across
   rounds, retaining non-displayed, omitted-uncertain, ambiguous, fixed,
   overridden, and rescan-superseded obligations.
3. ``display_active_findings``: strict-cap priority-first active window that
   reuses compatible identities and native threads across rounds and promotes
   the next obligation only when an active slot is legitimately freed.
4. ``approval_eligibility``: truthful final verdict requiring both coverage
   satisfaction and complete authoritative obligation satisfaction, without
   exposing any public deferred-finding list or count.

Decision-provider receipts (#427) are accepted strictly as bounded proposals:
they may break ties within identical priority tiers, never create authority,
prove a fix, downgrade blocking state, or drop obligations.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace

from .baseline import (
    COVERAGE_MODES,
    BaselineFinding,
    ReviewBaseline,
    resolution_criterion_digest,
)
from .bounded_evidence import canonical_bytes
from .context import comment_defect_kind, finding_lifecycle_for_comment
from .coverage import CoverageManifest, coverage_approval_state
from .decision_provider import digest as decision_digest
from .disposition import _require_reason, authorized_override
from .errors import ReviewInputError
from .human_assessment import finding_instance_fingerprint
from .models import FindingLifecycleRecord, ReviewComment, ReviewResult
from .publication_selection import PublishedSlot
from .rescan import AuthorizedRescan
from .review_decision_receipt import validate_decision_receipt
from .validation import DEFAULT_REVIEW_LIMITS, validate_repository_path

PUBLIC_WINDOW_SCHEMA_VERSION = "bounded-active-window-public-v1"
AUTHORITATIVE_WINDOW_SCHEMA_VERSION = "bounded-active-window-authority-v1"

_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_COMMIT_SHA_RE = re.compile(r"^[a-f0-9]{40,64}$")

IMPACT_RANKS: Mapping[str | None, int] = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
    None: 4,
}
EVIDENCE_RANKS: Mapping[str, int] = {
    "causal-source": 0,
    "supported-source": 1,
    "human-judgment": 2,
    "advisory": 3,
}

UNRESOLVED_LIFECYCLE_STATES = frozenset(
    {
        "active-unresolved",
        "omitted-uncertain",
        "ambiguous-identity",
    }
)
RESOLVED_LIFECYCLE_STATES = frozenset(
    {
        "verified-fixed",
        "authorized-override",
        "rescan-superseded",
    }
)
OBLIGATION_LIFECYCLE_STATES = UNRESOLVED_LIFECYCLE_STATES | RESOLVED_LIFECYCLE_STATES

OVERRIDE_DISPOSITION_ACTIONS = frozenset({"override", "accept-risk", "dismiss"})
RECOVERY_ROUTE_KINDS = frozenset(
    {
        "verify",
        "override",
        "accept-risk",
        "rescan",
        "accept-coverage-uncertainty",
    }
)
WITHHELD_REASON_CODES = frozenset(
    {
        "coverage-incomplete",
        "coverage-partial",
        "review-incomplete",
        "unresolved-obligations",
        "uncertain-finding-lifecycle",
        "ambiguous-finding-identity",
        "uncertain-native-slot",
    }
)
FORBIDDEN_PUBLIC_DEFERRED_KEYS = frozenset(
    {
        "deferred",
        "deferred_count",
        "deferred_findings",
        "deferred_ids",
        "deferred_obligations",
        "hidden_count",
        "hidden_findings",
        "hidden_obligations",
        "queued_count",
        "queued_findings",
        "remaining_count",
        "remaining_findings",
        "total_obligations",
        "unselected_count",
        "unselected_findings",
    }
)
DEFAULT_PROPOSAL_RANK = 50


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
class ConfirmedFixEvidence:
    """Positive current-head fix proof bound to the original criterion."""

    obligation_id: str
    resolution_criterion: str
    head_sha: str
    evidence_sha256: str
    verified_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_sha256(self.obligation_id, label="fix obligation_id")
        _require_sha256(self.resolution_criterion, label="fix resolution_criterion")
        _require_commit_sha(self.head_sha, label="fix head_sha")
        _require_sha256(self.evidence_sha256, label="fix evidence_sha256")
        if not isinstance(self.verified_paths, tuple) or not self.verified_paths:
            raise ReviewInputError("confirmed fix evidence requires verified_paths")
        for path in self.verified_paths:
            validate_repository_path(
                path, allow_glob_chars=True, label="fix verified path"
            )


@dataclass(frozen=True)
class AuthorizedHumanOverride:
    """Explicit maintainer/admin disposition that resolves one obligation."""

    obligation_id: str
    action: str
    actor: str
    association: str
    head_sha: str
    reason: str
    proof_sha256: str
    permission: str | None = None
    slot_released: bool = True

    def __post_init__(self) -> None:
        _require_sha256(self.obligation_id, label="override obligation_id")
        if self.action not in OVERRIDE_DISPOSITION_ACTIONS:
            raise ReviewInputError(
                "human override action must be override, accept-risk, or dismiss"
            )
        if (
            not isinstance(self.actor, str)
            or not self.actor.strip()
            or len(self.actor.encode("utf-8")) > 256
        ):
            raise ReviewInputError("human override actor is invalid")
        if not authorized_override(
            association=self.association, permission=self.permission
        ):
            raise ReviewInputError(
                "human override requires maintainer or admin authority"
            )
        _require_commit_sha(self.head_sha, label="override head_sha")
        _require_reason(self.reason)
        _require_sha256(self.proof_sha256, label="override proof_sha256")
        _require_bool(self.slot_released, label="override slot_released")


@dataclass(frozen=True)
class AuthorizedCoverageAcceptance:
    """Explicit maintainer exception for partial coverage (never incomplete)."""

    actor: str
    association: str
    head_sha: str
    reason: str
    proof_sha256: str
    permission: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.actor, str)
            or not self.actor.strip()
            or len(self.actor.encode("utf-8")) > 256
        ):
            raise ReviewInputError("coverage acceptance actor is invalid")
        if not authorized_override(
            association=self.association, permission=self.permission
        ):
            raise ReviewInputError(
                "coverage uncertainty acceptance requires maintainer or admin authority"
            )
        _require_commit_sha(self.head_sha, label="coverage acceptance head_sha")
        _require_reason(self.reason)
        _require_sha256(self.proof_sha256, label="coverage acceptance proof_sha256")


@dataclass(frozen=True)
class DecisionProposal:
    """Bounded, non-authoritative #427 proposal attached to one candidate."""

    candidate_id: str
    fingerprint: str
    proposed_action: str
    verification: str
    suspected_duplicate: bool
    proposal_rank: int

    def __post_init__(self) -> None:
        _require_sha256(self.fingerprint, label="decision proposal fingerprint")
        if self.proposed_action not in {
            "continue",
            "optional_suggestion",
            "human_review",
            "verify_further",
        }:
            raise ReviewInputError("decision proposal action is invalid")
        if self.verification not in {
            "not_requested",
            "supported",
            "contradicted",
            "insufficient_evidence",
            "unavailable",
        }:
            raise ReviewInputError("decision proposal verification is invalid")
        _require_bool(self.suspected_duplicate, label="suspected_duplicate")
        if type(self.proposal_rank) is not int or not 0 <= self.proposal_rank <= 99:
            raise ReviewInputError("decision proposal_rank must be in 0..99")


def extract_decision_proposals(result: ReviewResult) -> tuple[DecisionProposal, ...]:
    """Validate and extract proposal-only #427 metadata from ``ReviewResult``."""

    if not isinstance(result, ReviewResult):
        raise ReviewInputError("decision proposal extraction requires a ReviewResult")
    receipt = result.decision_pass
    if receipt is None:
        return ()
    validate_decision_receipt(receipt)
    if not isinstance(receipt, Mapping):
        raise ReviewInputError("decision receipt must be a mapping")
    expected_inventory = decision_digest([c.to_dict() for c in result.comments])
    if receipt["inventory_sha256"] != expected_inventory:
        raise ReviewInputError("decision receipt inventory_sha256 does not match")
    candidates = receipt["candidates"]
    if not isinstance(candidates, list):
        raise ReviewInputError("decision receipt candidates must be a list")
    if len(candidates) != len(result.comments):
        raise ReviewInputError("decision receipt candidate count does not match")
    proposals: list[DecisionProposal] = []
    for index, (item, comment) in enumerate(
        zip(candidates, result.comments, strict=True)
    ):
        expected_fp = finding_lifecycle_for_comment(comment, generation=0).fingerprint
        if (
            item["candidate_id"] != f"c{index:04d}"
            or item["fingerprint"] != expected_fp
        ):
            raise ReviewInputError("decision receipt candidate binding conflicts")
        action = item["action"]
        verification = item["verification"]
        if action == "continue" and verification == "supported":
            rank = 10
        elif action == "continue":
            rank = 20
        elif action in {"human_review", "verify_further"}:
            rank = 30
        else:
            rank = 40
        proposals.append(
            DecisionProposal(
                candidate_id=item["candidate_id"],
                fingerprint=item["fingerprint"],
                proposed_action=action,
                verification=verification,
                suspected_duplicate=item["suspected_duplicate"],
                proposal_rank=rank,
            )
        )
    return tuple(proposals)


@dataclass(frozen=True)
class ChangedFileCoverageState:
    """State variable 1: changed-file coverage, distinct from obligations."""

    coverage_mode: str
    approval_state: str
    enumeration_complete: bool
    fully_reviewed: bool
    enumerated_paths: tuple[str, ...]
    reviewed_paths: tuple[str, ...]
    coverage_uncertainty_accepted: bool = False
    acceptance_proof_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.coverage_mode not in COVERAGE_MODES:
            raise ReviewInputError("coverage_mode is invalid")
        if self.approval_state not in {"reviewed", "partial", "incomplete", "unknown"}:
            raise ReviewInputError("coverage approval_state is invalid")
        _require_bool(self.enumeration_complete, label="enumeration_complete")
        _require_bool(self.fully_reviewed, label="fully_reviewed")
        _require_bool(
            self.coverage_uncertainty_accepted, label="coverage_uncertainty_accepted"
        )
        for path in (*self.enumerated_paths, *self.reviewed_paths):
            validate_repository_path(path, allow_glob_chars=True, label="coverage path")
        if self.fully_reviewed and self.approval_state != "reviewed":
            raise ReviewInputError("fully_reviewed conflicts with approval_state")
        if self.coverage_uncertainty_accepted:
            if self.approval_state != "partial":
                raise ReviewInputError(
                    "only partial coverage can be accepted by maintainer exception"
                )
            _require_sha256(
                self.acceptance_proof_sha256, label="acceptance_proof_sha256"
            )
        elif self.acceptance_proof_sha256 is not None:
            raise ReviewInputError(
                "acceptance_proof_sha256 requires coverage_uncertainty_accepted"
            )

    @property
    def coverage_satisfied(self) -> bool:
        return self.approval_state == "reviewed" or (
            self.approval_state == "partial" and self.coverage_uncertainty_accepted
        )

    @classmethod
    def from_manifest(
        cls,
        coverage: CoverageManifest | None,
        *,
        coverage_mode: str = "full",
        head_sha: str | None = None,
        coverage_acceptance: AuthorizedCoverageAcceptance | None = None,
    ) -> ChangedFileCoverageState:
        state = coverage_approval_state(coverage)
        enumerated = () if coverage is None else coverage.enumerated_paths
        reviewed = (
            ()
            if coverage is None
            else tuple(
                entry.path for entry in coverage.files if entry.outcome == "reviewed"
            )
        )
        accepted = False
        proof: str | None = None
        if coverage_acceptance is not None:
            if head_sha is None or coverage_acceptance.head_sha != head_sha:
                raise ReviewInputError("coverage acceptance head_sha does not match")
            if state != "partial":
                raise ReviewInputError(
                    "coverage uncertainty acceptance applies only to partial coverage"
                )
            accepted = True
            proof = coverage_acceptance.proof_sha256
        return cls(
            coverage_mode=coverage_mode,
            approval_state=state,
            enumeration_complete=False
            if coverage is None
            else coverage.enumeration_complete,
            fully_reviewed=False if coverage is None else coverage.fully_reviewed,
            enumerated_paths=enumerated,
            reviewed_paths=reviewed,
            coverage_uncertainty_accepted=accepted,
            acceptance_proof_sha256=proof,
        )


@dataclass(frozen=True)
class AuthoritativeObligation:
    """One durable finding obligation retained across rounds regardless of display."""

    obligation_id: str
    fingerprint: str
    resolution_criterion: str
    path: str | None
    line: int | None
    side: str
    symbol: str | None
    defect_kind: str
    concern: str | None
    blocking: bool
    needs_human: bool
    impact: str | None
    evidence: str
    lifecycle_state: str
    generation: int = 0
    native_target: str | None = None
    target_state: str | None = None
    target_proof_sha256: str | None = None
    resolution_proof_sha256: str | None = None
    proposal_rank: int = DEFAULT_PROPOSAL_RANK
    comment: ReviewComment | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        _require_sha256(self.obligation_id, label="obligation_id")
        _require_sha256(self.fingerprint, label="obligation fingerprint")
        _require_sha256(self.resolution_criterion, label="resolution_criterion")
        if self.path is not None:
            validate_repository_path(
                self.path, allow_glob_chars=True, label="obligation path"
            )
        if self.line is not None and (type(self.line) is not int or self.line < 1):
            raise ReviewInputError("obligation line must be a positive integer")
        if self.side not in {"LEFT", "RIGHT", "FILE"}:
            raise ReviewInputError("obligation side must be LEFT, RIGHT, or FILE")
        if self.side == "FILE" and self.line is not None:
            raise ReviewInputError("file-level obligation must omit line")
        if self.symbol is not None and (
            not isinstance(self.symbol, str) or not self.symbol.strip()
        ):
            raise ReviewInputError("obligation symbol is invalid")
        if not isinstance(self.defect_kind, str) or not self.defect_kind.strip():
            raise ReviewInputError("obligation defect_kind is invalid")
        if self.concern is not None:
            _require_sha256(self.concern, label="obligation concern")
        _require_bool(self.blocking, label="obligation blocking")
        _require_bool(self.needs_human, label="obligation needs_human")
        if self.impact not in IMPACT_RANKS:
            raise ReviewInputError("obligation impact is invalid")
        if self.evidence not in EVIDENCE_RANKS:
            raise ReviewInputError("obligation evidence is invalid")
        if self.evidence == "advisory" and (
            self.blocking or self.needs_human or self.impact is not None
        ):
            raise ReviewInputError("advisory obligation cannot be blocking or ranked")
        if self.lifecycle_state not in OBLIGATION_LIFECYCLE_STATES:
            raise ReviewInputError("obligation lifecycle_state is invalid")
        if type(self.generation) is not int or self.generation < 0:
            raise ReviewInputError("obligation generation is invalid")
        if type(self.proposal_rank) is not int or not 0 <= self.proposal_rank <= 99:
            raise ReviewInputError("obligation proposal_rank is invalid")
        if self.native_target is not None:
            _require_target_id(self.native_target, label="native_target")
            if self.target_state not in {
                "active",
                "contingent",
                "uncertain",
                "released",
            }:
                raise ReviewInputError(
                    "native_target requires a valid PublishedSlot target_state"
                )
            _require_sha256(self.target_proof_sha256, label="target_proof_sha256")
        elif self.target_state is not None or self.target_proof_sha256 is not None:
            raise ReviewInputError(
                "target_state and target_proof_sha256 require native_target"
            )
        if self.lifecycle_state in RESOLVED_LIFECYCLE_STATES:
            _require_sha256(
                self.resolution_proof_sha256,
                label="resolved obligation resolution_proof_sha256",
            )
        elif self.resolution_proof_sha256 is not None:
            raise ReviewInputError(
                "unresolved obligation cannot carry resolution_proof_sha256"
            )

    @property
    def required(self) -> bool:
        return self.blocking or self.needs_human

    @property
    def unresolved(self) -> bool:
        return self.lifecycle_state in UNRESOLVED_LIFECYCLE_STATES

    @property
    def blocks_approval_now(self) -> bool:
        if not self.unresolved:
            return False
        return self.required or self.lifecycle_state in {
            "omitted-uncertain",
            "ambiguous-identity",
        }

    @property
    def priority_key(self) -> tuple[int, int, int, int, str]:
        return (
            int(not self.required),
            IMPACT_RANKS[self.impact],
            EVIDENCE_RANKS[self.evidence],
            self.proposal_rank,
            self.obligation_id,
        )

    def to_baseline_finding(self) -> BaselineFinding:
        return BaselineFinding(
            fingerprint=self.fingerprint,
            resolution_criterion=self.resolution_criterion,
            concern=self.concern,
            path=self.path,
            symbol=self.symbol,
            defect_kind=self.defect_kind,
            generation=self.generation,
            blocking=self.blocking,
        )


def obligation_from_comment(
    comment: ReviewComment,
    *,
    impact: str | None = None,
    evidence: str | None = None,
    generation: int = 0,
    native_target: str | None = None,
    target_state: str | None = None,
    target_proof_sha256: str | None = None,
    proposal_rank: int = DEFAULT_PROPOSAL_RANK,
    obligation_id: str | None = None,
) -> AuthoritativeObligation:
    """Create a validated obligation from an admitted ``ReviewComment``."""

    if not isinstance(comment, ReviewComment):
        raise ReviewInputError("obligation requires a ReviewComment")
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
        severity = (comment.severity or "").strip().lower()
        resolved_impact = (
            severity
            if severity in {"critical", "high", "medium", "low"}
            else ("high" if required else "low")
        )
    oid = (
        obligation_id
        if obligation_id is not None
        else hashlib.sha256(
            b"reviewsensei:obligation:v1:"
            + canonical_bytes(
                {
                    "fingerprint": lifecycle.fingerprint,
                    "instance": finding_instance_fingerprint(comment),
                }
            )
        ).hexdigest()
    )
    return AuthoritativeObligation(
        obligation_id=oid,
        fingerprint=lifecycle.fingerprint,
        resolution_criterion=resolution_criterion_digest(
            fingerprint=lifecycle.fingerprint,
            evidence_id=comment.evidence_id,
            defect_kind=defect_kind,
            path=comment.path,
            symbol=comment.symbol,
        ),
        path=comment.path,
        line=comment.line,
        side=comment.side,
        symbol=comment.symbol,
        defect_kind=defect_kind,
        concern=lifecycle.concern,
        blocking=comment.blocks_approval,
        needs_human=comment.needs_human,
        impact=resolved_impact,
        evidence=resolved_evidence,
        lifecycle_state="active-unresolved",
        generation=generation,
        native_target=native_target,
        target_state=target_state,
        target_proof_sha256=target_proof_sha256,
        proposal_rank=proposal_rank,
        comment=comment,
    )


@dataclass(frozen=True)
class AuthoritativeDecisionInventory:
    """State variable 2: full durable inventory across active and hidden items."""

    obligations: tuple[AuthoritativeObligation, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.obligations, tuple)
            or len(self.obligations) > DEFAULT_REVIEW_LIMITS.max_comments * 4
            or any(
                type(item) is not AuthoritativeObligation for item in self.obligations
            )
        ):
            raise ReviewInputError("authoritative decision inventory is invalid")
        seen: set[str] = set()
        active_targets: set[str] = set()
        for item in self.obligations:
            if item.obligation_id in seen:
                raise ReviewInputError("duplicate obligation_id in inventory")
            seen.add(item.obligation_id)
            if item.native_target is not None and item.unresolved:
                if item.native_target in active_targets:
                    raise ReviewInputError(
                        "multiple unresolved obligations share one native_target"
                    )
                active_targets.add(item.native_target)

    @property
    def unresolved_obligations(self) -> tuple[AuthoritativeObligation, ...]:
        return tuple(item for item in self.obligations if item.unresolved)

    @property
    def blocking_obligations(self) -> tuple[AuthoritativeObligation, ...]:
        return tuple(item for item in self.obligations if item.blocks_approval_now)

    @property
    def has_omitted_uncertainty(self) -> bool:
        return any(
            item.lifecycle_state == "omitted-uncertain" for item in self.obligations
        )

    @property
    def has_ambiguous_identity(self) -> bool:
        return any(
            item.lifecycle_state == "ambiguous-identity" for item in self.obligations
        )

    @property
    def inventory_satisfied(self) -> bool:
        return not self.blocking_obligations

    def digest(self) -> str:
        return hashlib.sha256(
            canonical_bytes(
                [
                    {
                        "obligation_id": item.obligation_id,
                        "fingerprint": item.fingerprint,
                        "resolution_criterion": item.resolution_criterion,
                        "lifecycle_state": item.lifecycle_state,
                        "blocking": item.blocking,
                        "needs_human": item.needs_human,
                        "native_target": item.native_target,
                        "target_state": item.target_state,
                        "resolution_proof_sha256": item.resolution_proof_sha256,
                    }
                    for item in sorted(self.obligations, key=lambda o: o.obligation_id)
                ]
            )
        ).hexdigest()


@dataclass(frozen=True)
class ActiveWindowEntry:
    """One displayed finding inside the bounded active window."""

    obligation_id: str
    fingerprint: str
    path: str | None
    line: int | None
    side: str
    required: bool
    impact: str | None
    lifecycle_state: str
    native_target: str | None
    target_action: str
    consumes_new_slot: bool

    def __post_init__(self) -> None:
        _require_sha256(self.obligation_id, label="entry obligation_id")
        _require_sha256(self.fingerprint, label="entry fingerprint")
        if self.target_action not in {"create", "reuse", "reopen"}:
            raise ReviewInputError("entry target_action is invalid")
        _require_bool(self.consumes_new_slot, label="consumes_new_slot")
        if self.target_action == "create" and (
            self.native_target is not None or not self.consumes_new_slot
        ):
            raise ReviewInputError("create target_action conflicts with native_target")
        if self.target_action in {"reuse", "reopen"} and self.native_target is None:
            raise ReviewInputError("reuse/reopen target_action requires native_target")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "obligation_id": self.obligation_id,
            "fingerprint": self.fingerprint,
            "path": self.path,
            "line": self.line,
            "side": self.side,
            "required": self.required,
            "impact": self.impact,
            "lifecycle_state": self.lifecycle_state,
            "native_target": self.native_target,
            "target_action": self.target_action,
        }


@dataclass(frozen=True)
class DisplayActiveWindow:
    """State variable 3: bounded priority-first display window."""

    max_active_findings: int
    entries: tuple[ActiveWindowEntry, ...]
    slots: tuple[PublishedSlot, ...]

    def __post_init__(self) -> None:
        if (
            type(self.max_active_findings) is not int
            or not 1 <= self.max_active_findings <= DEFAULT_REVIEW_LIMITS.max_comments
        ):
            raise ReviewInputError("max_active_findings is invalid")
        if not isinstance(self.entries, tuple) or any(
            type(item) is not ActiveWindowEntry for item in self.entries
        ):
            raise ReviewInputError("display active entries are invalid")
        if len(self.entries) > self.max_active_findings:
            raise ReviewInputError("display active entries exceed max_active_findings")
        if not isinstance(self.slots, tuple) or any(
            type(slot) is not PublishedSlot for slot in self.slots
        ):
            raise ReviewInputError("display active slots are invalid")
        if len({slot.target for slot in self.slots}) != len(self.slots):
            raise ReviewInputError("display active slots contain duplicate targets")
        if len({item.obligation_id for item in self.entries}) != len(self.entries):
            raise ReviewInputError("display active entries contain duplicate IDs")
        reserved_slots = sum(slot.reserved for slot in self.slots)
        new_creations = sum(item.consumes_new_slot for item in self.entries)
        if reserved_slots + new_creations > self.max_active_findings:
            raise ReviewInputError("display active occupancy exceeds strict cap")

    @property
    def has_uncertain_slots(self) -> bool:
        return any(slot.state == "uncertain" for slot in self.slots)


def select_display_active_window(
    inventory: AuthoritativeDecisionInventory,
    *,
    slots: tuple[PublishedSlot, ...] = (),
    max_active_findings: int,
) -> DisplayActiveWindow:
    """Select bounded active findings, preserving active threads before promotion.

    Existing active/contingent slot holders that remain unresolved keep their
    slots first so active threads never thrash across rounds. New candidates or
    reopenings of released targets are promoted in strict priority order only
    when reserved capacity is below ``max_active_findings``.
    """

    if not isinstance(inventory, AuthoritativeDecisionInventory):
        raise ReviewInputError("active window selection requires an inventory")
    if (
        type(max_active_findings) is not int
        or not 1 <= max_active_findings <= DEFAULT_REVIEW_LIMITS.max_comments
    ):
        raise ReviewInputError("max_active_findings is invalid")
    if not isinstance(slots, tuple) or any(
        type(slot) is not PublishedSlot for slot in slots
    ):
        raise ReviewInputError("slots must be a tuple of PublishedSlot")
    by_target = {slot.target: slot for slot in slots}
    if len(by_target) != len(slots):
        raise ReviewInputError("duplicate target in PublishedSlot tuple")
    reserved = sum(slot.reserved for slot in slots)
    if reserved > max_active_findings:
        raise ReviewInputError("existing slot reservations exceed max_active_findings")

    incumbents: list[AuthoritativeObligation] = []
    challengers: list[AuthoritativeObligation] = []
    for item in sorted(inventory.unresolved_obligations, key=lambda o: o.priority_key):
        if item.native_target is not None:
            slot = by_target.get(item.native_target)
            if slot is None:
                raise ReviewInputError(
                    "unresolved obligation references an unknown native_target"
                )
            if slot.state == "uncertain":
                # Uncertain target keeps its physical reservation and cannot be
                # mutated or replaced until owned readback reconciles it.
                continue
            if slot.state in {"active", "contingent"}:
                incumbents.append(item)
                continue
        challengers.append(item)

    entries: list[ActiveWindowEntry] = []
    for item in incumbents:
        if item.native_target is None:
            raise ReviewInputError("incumbent obligation is missing native_target")
        slot = by_target[item.native_target]
        entries.append(
            ActiveWindowEntry(
                obligation_id=item.obligation_id,
                fingerprint=item.fingerprint,
                path=item.path,
                line=item.line,
                side=item.side,
                required=item.required,
                impact=item.impact,
                lifecycle_state=item.lifecycle_state,
                native_target=item.native_target,
                target_action="reuse" if slot.state == "active" else "reopen",
                consumes_new_slot=False,
            )
        )

    for item in challengers:
        if reserved >= max_active_findings:
            break
        action = "create" if item.native_target is None else "reopen"
        entries.append(
            ActiveWindowEntry(
                obligation_id=item.obligation_id,
                fingerprint=item.fingerprint,
                path=item.path,
                line=item.line,
                side=item.side,
                required=item.required,
                impact=item.impact,
                lifecycle_state=item.lifecycle_state,
                native_target=item.native_target,
                target_action=action,
                consumes_new_slot=True,
            )
        )
        reserved += 1

    return DisplayActiveWindow(
        max_active_findings=max_active_findings,
        entries=tuple(entries),
        slots=slots,
    )


@dataclass(frozen=True)
class ActiveWindowRecoveryStep:
    """Explicit bounded recovery step that advances a withheld review."""

    route: str
    reason_code: str
    target_scope: str

    def __post_init__(self) -> None:
        if self.route not in RECOVERY_ROUTE_KINDS:
            raise ReviewInputError("recovery step route is invalid")
        if self.reason_code not in WITHHELD_REASON_CODES:
            raise ReviewInputError("recovery step reason_code is invalid")
        if self.target_scope not in {"active-window", "pull-request"}:
            raise ReviewInputError("recovery step target_scope is invalid")

    def to_public_dict(self) -> dict[str, str]:
        return {
            "route": self.route,
            "reason_code": self.reason_code,
            "target_scope": self.target_scope,
        }


@dataclass(frozen=True)
class ActiveWindowApprovalEligibility:
    """State variable 4: approval verdict gated on coverage AND full inventory."""

    approved: bool
    outcome: str
    withheld_reasons: tuple[str, ...]
    recovery_steps: tuple[ActiveWindowRecoveryStep, ...]

    def __post_init__(self) -> None:
        _require_bool(self.approved, label="approved")
        if self.outcome not in {"approved", "withheld"}:
            raise ReviewInputError("approval outcome is invalid")
        if self.approved != (self.outcome == "approved"):
            raise ReviewInputError("approved boolean conflicts with outcome")
        if any(code not in WITHHELD_REASON_CODES for code in self.withheld_reasons):
            raise ReviewInputError("withheld_reasons contains an unknown code")
        if self.approved and (self.withheld_reasons or self.recovery_steps):
            raise ReviewInputError(
                "approved eligibility cannot carry withheld reasons or recovery steps"
            )
        if not self.approved and (not self.withheld_reasons or not self.recovery_steps):
            raise ReviewInputError(
                "withheld eligibility must expose reason codes and recovery steps"
            )


def evaluate_window_approval_eligibility(
    *,
    changed_file_coverage: ChangedFileCoverageState,
    authoritative_decision_inventory: AuthoritativeDecisionInventory,
    display_active_findings: DisplayActiveWindow,
    review_complete: bool,
) -> ActiveWindowApprovalEligibility:
    """Compute approval eligibility and universal bounded recovery routes."""

    _require_bool(review_complete, label="review_complete")
    reasons: list[str] = []
    steps: list[ActiveWindowRecoveryStep] = []

    def add_step(route: str, reason_code: str, scope: str) -> None:
        step = ActiveWindowRecoveryStep(
            route=route, reason_code=reason_code, target_scope=scope
        )
        if step not in steps:
            steps.append(step)

    if not changed_file_coverage.coverage_satisfied:
        if changed_file_coverage.approval_state == "partial":
            reasons.append("coverage-partial")
            add_step("rescan", "coverage-partial", "pull-request")
            add_step("accept-coverage-uncertainty", "coverage-partial", "pull-request")
        else:
            reasons.append("coverage-incomplete")
            add_step("rescan", "coverage-incomplete", "pull-request")

    if not review_complete and not changed_file_coverage.coverage_uncertainty_accepted:
        reasons.append("review-incomplete")
        add_step("verify", "review-incomplete", "pull-request")
        add_step("rescan", "review-incomplete", "pull-request")

    if display_active_findings.has_uncertain_slots:
        reasons.append("uncertain-native-slot")
        add_step("verify", "uncertain-native-slot", "pull-request")

    if authoritative_decision_inventory.has_ambiguous_identity:
        reasons.append("ambiguous-finding-identity")
        add_step("override", "ambiguous-finding-identity", "active-window")
        add_step("accept-risk", "ambiguous-finding-identity", "active-window")
        add_step("rescan", "ambiguous-finding-identity", "pull-request")

    if authoritative_decision_inventory.has_omitted_uncertainty:
        reasons.append("uncertain-finding-lifecycle")
        add_step("verify", "uncertain-finding-lifecycle", "pull-request")
        add_step("override", "uncertain-finding-lifecycle", "active-window")
        add_step("rescan", "uncertain-finding-lifecycle", "pull-request")

    if not authoritative_decision_inventory.inventory_satisfied:
        reasons.append("unresolved-obligations")
        scope = "active-window" if display_active_findings.entries else "pull-request"
        add_step("verify", "unresolved-obligations", scope)
        add_step("override", "unresolved-obligations", scope)
        add_step("accept-risk", "unresolved-obligations", scope)

    if not reasons:
        return ActiveWindowApprovalEligibility(
            approved=True,
            outcome="approved",
            withheld_reasons=(),
            recovery_steps=(),
        )
    return ActiveWindowApprovalEligibility(
        approved=False,
        outcome="withheld",
        withheld_reasons=tuple(reasons),
        recovery_steps=tuple(steps),
    )


def assert_no_deferred_leakage(public_status: Mapping[str, object]) -> None:
    """Fail closed if any public projection exposes deferred counts or lists."""

    def walk(node: object) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                if not isinstance(key, str) or key in FORBIDDEN_PUBLIC_DEFERRED_KEYS:
                    raise ReviewInputError(
                        "public status must never expose deferred finding counts or lists"
                    )
                if "deferred" in key.lower() or "unselected" in key.lower():
                    raise ReviewInputError(
                        "public status key leaks deferred/unselected state"
                    )
                walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(public_status)


@dataclass(frozen=True)
class ActiveWindowRoundState:
    """Complete immutable four-variable state for one bounded review round."""

    head_sha: str
    base_sha: str
    round_number: int
    review_complete: bool
    changed_file_coverage: ChangedFileCoverageState
    authoritative_decision_inventory: AuthoritativeDecisionInventory
    display_active_findings: DisplayActiveWindow
    approval_eligibility: ActiveWindowApprovalEligibility

    def __post_init__(self) -> None:
        _require_commit_sha(self.head_sha, label="round head_sha")
        _require_commit_sha(self.base_sha, label="round base_sha")
        if type(self.round_number) is not int or self.round_number < 1:
            raise ReviewInputError("round_number must be >= 1")
        _require_bool(self.review_complete, label="review_complete")

    def to_public_status(self) -> dict[str, object]:
        """Truthful public projection with zero deferred-finding list/count."""

        payload: dict[str, object] = {
            "schema_version": PUBLIC_WINDOW_SCHEMA_VERSION,
            "head_sha": self.head_sha,
            "base_sha": self.base_sha,
            "round_number": self.round_number,
            "coverage_state": self.changed_file_coverage.approval_state,
            "coverage_mode": self.changed_file_coverage.coverage_mode,
            "max_active_findings": self.display_active_findings.max_active_findings,
            "active_findings": [
                entry.to_public_dict() for entry in self.display_active_findings.entries
            ],
            "approval_eligible": self.approval_eligibility.approved,
            "outcome": self.approval_eligibility.outcome,
            "withheld_reasons": list(self.approval_eligibility.withheld_reasons),
            "recovery_routes": [
                step.to_public_dict()
                for step in self.approval_eligibility.recovery_steps
            ],
        }
        assert_no_deferred_leakage(payload)
        return payload

    def to_baseline(
        self, *, baseline_template: ReviewBaseline, generation: int | None = None
    ) -> ReviewBaseline:
        """Project unresolved authoritative obligations back to ``ReviewBaseline``."""

        gen = baseline_template.generation if generation is None else generation
        findings = tuple(
            item.to_baseline_finding()
            for item in self.authoritative_decision_inventory.unresolved_obligations
        )
        return replace(
            baseline_template,
            complete=self.changed_file_coverage.fully_reviewed
            and self.review_complete
            and not self.authoritative_decision_inventory.has_omitted_uncertainty
            and not self.authoritative_decision_inventory.has_ambiguous_identity,
            coverage_complete=self.changed_file_coverage.fully_reviewed,
            findings=findings,
            generation=gen,
        )


def _match_prior_obligations(
    comment: ReviewComment,
    priors: Sequence[AuthoritativeObligation],
) -> tuple[AuthoritativeObligation | None, tuple[AuthoritativeObligation, ...]]:
    """Match a current comment against prior obligations."""

    lifecycle = finding_lifecycle_for_comment(comment)
    defect_kind = comment_defect_kind(comment)
    key = (comment.path or "", comment.symbol or "", defect_kind)

    exact = [p for p in priors if (p.path or "", p.symbol or "", p.defect_kind) == key]
    if len(exact) > 1:
        return None, tuple(exact)

    by_fp = [p for p in priors if p.fingerprint == lifecycle.fingerprint]
    if len(by_fp) == 1:
        return by_fp[0], ()
    if len(by_fp) > 1:
        return None, tuple(by_fp)

    if lifecycle.concern is not None:
        by_concern = [
            p
            for p in priors
            if p.concern == lifecycle.concern
            and p.defect_kind == defect_kind
            and (p.path == comment.path or (p.symbol and p.symbol == comment.symbol))
        ]
        if len(by_concern) == 1:
            return by_concern[0], ()
        if len(by_concern) > 1:
            return None, tuple(by_concern)

    if len(exact) == 1:
        return exact[0], ()

    if defect_kind != "unknown" and comment.path is not None:
        by_path_kind = [
            p for p in priors if p.path == comment.path and p.defect_kind == defect_kind
        ]
        if len(by_path_kind) == 1:
            return by_path_kind[0], ()
        if len(by_path_kind) > 1:
            return None, tuple(by_path_kind)

    return None, ()


def evaluate_active_window_round(
    result: ReviewResult,
    *,
    head_sha: str,
    base_sha: str,
    max_active_findings: int,
    round_number: int = 1,
    coverage_mode: str = "full",
    prior_inventory: AuthoritativeDecisionInventory | None = None,
    slots: tuple[PublishedSlot, ...] = (),
    confirmed_fixes: Sequence[ConfirmedFixEvidence] = (),
    human_overrides: Sequence[AuthorizedHumanOverride] = (),
    authorized_rescan: AuthorizedRescan | None = None,
    coverage_acceptance: AuthorizedCoverageAcceptance | None = None,
) -> ActiveWindowRoundState:
    """Advance one deterministic review round with zero automatic loops.

    Enforces:
    - Complete diff coverage never clears omitted or ambiguous obligations.
    - Ordinary incremental and ``fallback-full`` omissions stay ``omitted-uncertain``.
    - Only positive ``ConfirmedFixEvidence`` (on a complete review), explicit
      ``AuthorizedHumanOverride``, or a complete head-bound ``AuthorizedRescan``
      with ``mode == "full"`` resolves prior obligations accordingly.
    - Active slots turn over to the next highest-priority obligation ONLY when
      a prior slot is legitimately released by a proven fix or authorized override.
    - ``#427`` decision receipts affect only intra-tier tiebreaking.
    """

    if not isinstance(result, ReviewResult):
        raise ReviewInputError("evaluate_active_window_round requires a ReviewResult")
    _require_commit_sha(head_sha, label="head_sha")
    _require_commit_sha(base_sha, label="base_sha")

    if authorized_rescan is not None:
        if not isinstance(authorized_rescan, AuthorizedRescan):
            raise ReviewInputError("authorized_rescan must be an AuthorizedRescan")
        if authorized_rescan.source.head_sha != head_sha:
            raise ReviewInputError(
                "authorized_rescan head_sha does not match current head"
            )

    coverage_state = ChangedFileCoverageState.from_manifest(
        result.coverage,
        coverage_mode=coverage_mode,
        head_sha=head_sha,
        coverage_acceptance=coverage_acceptance,
    )
    proposals = extract_decision_proposals(result)
    proposal_by_index = {index: p for index, p in enumerate(proposals)}

    fixes_by_id: dict[str, ConfirmedFixEvidence] = {}
    for fix in confirmed_fixes:
        if not isinstance(fix, ConfirmedFixEvidence) or fix.head_sha != head_sha:
            raise ReviewInputError("confirmed fix evidence is invalid or stale")
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
        and item.native_target is not None
        and item.target_state == "released"
    ]
    prior_by_id = {item.obligation_id: item for item in prior_items}

    slot_map: dict[str, PublishedSlot] = {slot.target: slot for slot in slots}
    if len(slot_map) != len(slots):
        raise ReviewInputError("duplicate target in supplied slots")

    review_complete = (
        result.review_status == "complete" and coverage_state.fully_reviewed
    )
    full_rescan_qualified = (
        authorized_rescan is not None
        and authorized_rescan.mode == "full"
        and review_complete
    )
    rescan_proof: str | None = None
    if full_rescan_qualified and authorized_rescan is not None:
        rescan_proof = hashlib.sha256(
            canonical_bytes(
                {
                    "kind": authorized_rescan.kind,
                    "request_id": authorized_rescan.request_id,
                    "head_sha": head_sha,
                    "transaction_id": authorized_rescan.source.transaction_id,
                }
            )
        ).hexdigest()

    # Match current comments against unresolved prior obligations first.
    claimed_by_prior: dict[str, list[int]] = {}
    ambiguous_prior_ids: set[str] = set()
    ambiguous_comment_indices: set[int] = set()
    matched_prior_for_comment: dict[int, AuthoritativeObligation] = {}
    reopen_claims: dict[str, list[int]] = {}
    ambiguous_reopen_prior_ids: set[str] = set()

    for index, comment in enumerate(result.comments):
        matched, candidates = _match_prior_obligations(comment, unresolved_priors)
        if candidates:
            ambiguous_comment_indices.add(index)
            for candidate in candidates:
                ambiguous_prior_ids.add(candidate.obligation_id)
        elif matched is not None:
            matched_prior_for_comment[index] = matched
            claimed_by_prior.setdefault(matched.obligation_id, []).append(index)
        elif reopenable_priors:
            comment_required = comment.blocks_approval or comment.needs_human
            comment_ev = "supported-source" if comment_required else "advisory"
            eligible_reopen_priors = [
                p
                for p in reopenable_priors
                if (not p.required or comment_required)
                and EVIDENCE_RANKS[comment_ev] <= EVIDENCE_RANKS[p.evidence]
            ]
            if eligible_reopen_priors:
                reopened, reopen_ambiguous = _match_prior_obligations(
                    comment, eligible_reopen_priors
                )
                if reopen_ambiguous:
                    ambiguous_comment_indices.add(index)
                    for candidate in reopen_ambiguous:
                        ambiguous_reopen_prior_ids.add(candidate.obligation_id)
                elif reopened is not None:
                    reopen_claims.setdefault(reopened.obligation_id, []).append(index)
                    matched_prior_for_comment[index] = reopened

    reopened_by_prior: dict[str, int] = {}
    for prior_id, indices in reopen_claims.items():
        if len(indices) > 1 or prior_id in ambiguous_reopen_prior_ids:
            ambiguous_reopen_prior_ids.add(prior_id)
            ambiguous_comment_indices.update(indices)
        else:
            reopened_by_prior[prior_id] = indices[0]

    for oid in sorted(set(fixes_by_id) | set(overrides_by_id)):
        if oid not in prior_by_id:
            raise ReviewInputError(
                "resolution references unknown or already-resolved obligation_id"
            )
        if not prior_by_id[oid].unresolved:
            if oid not in reopened_by_prior and oid not in ambiguous_reopen_prior_ids:
                raise ReviewInputError(
                    "resolution references unknown or already-resolved obligation_id"
                )
            if oid in fixes_by_id:
                raise ReviewInputError(
                    "confirmed fix evidence conflicts with active regression reopening obligation"
                )

    for prior_id, indices in claimed_by_prior.items():
        if len(indices) > 1:
            ambiguous_prior_ids.add(prior_id)
            ambiguous_comment_indices.update(indices)

    next_obligations: list[AuthoritativeObligation] = []
    # Retain already-resolved historical obligations unless a fixed target regressed.
    for item in prior_items:
        if item.unresolved:
            continue
        if item.obligation_id in ambiguous_reopen_prior_ids:
            reopen_proof = (
                hashlib.sha256(
                    canonical_bytes(
                        {
                            "obligation_id": item.obligation_id,
                            "target": item.native_target,
                            "head_sha": head_sha,
                            "state": "reopen-ambiguous",
                        }
                    )
                ).hexdigest()
                if item.native_target is not None
                else None
            )
            next_obligations.append(
                replace(
                    item,
                    blocking=True,
                    needs_human=True,
                    lifecycle_state="ambiguous-identity",
                    target_proof_sha256=reopen_proof,
                    resolution_proof_sha256=None,
                )
            )
        elif item.obligation_id in reopened_by_prior:
            c_idx = reopened_by_prior[item.obligation_id]
            comment = result.comments[c_idx]
            proposal = proposal_by_index.get(c_idx)
            reopen_override = overrides_by_id.get(item.obligation_id)
            if reopen_override is not None:
                new_target_state = item.target_state
                if item.native_target is not None:
                    current_slot = slot_map.get(item.native_target)
                    if current_slot is None:
                        raise ReviewInputError("override obligation slot is missing")
                    if current_slot.state == "uncertain":
                        raise ReviewInputError(
                            "uncertain native slot must reconcile before slot release"
                        )
                    new_target_state = (
                        "released" if reopen_override.slot_released else "contingent"
                    )
                    slot_map[item.native_target] = PublishedSlot(
                        target=item.native_target,
                        state=new_target_state,
                        proof_sha256=reopen_override.proof_sha256,
                    )
                next_obligations.append(
                    replace(
                        item,
                        path=comment.path,
                        line=comment.line,
                        side=comment.side,
                        symbol=comment.symbol or item.symbol,
                        lifecycle_state="authorized-override",
                        target_state=new_target_state,
                        target_proof_sha256=(
                            reopen_override.proof_sha256
                            if item.native_target is not None
                            else None
                        ),
                        resolution_proof_sha256=reopen_override.proof_sha256,
                        comment=comment,
                    )
                )
            else:
                reopen_proof = (
                    hashlib.sha256(
                        canonical_bytes(
                            {
                                "obligation_id": item.obligation_id,
                                "target": item.native_target,
                                "head_sha": head_sha,
                                "state": "reopen-pending",
                            }
                        )
                    ).hexdigest()
                    if item.native_target is not None
                    else None
                )
                next_obligations.append(
                    replace(
                        item,
                        path=comment.path,
                        line=comment.line,
                        side=comment.side,
                        symbol=comment.symbol or item.symbol,
                        blocking=item.blocking or comment.blocks_approval,
                        needs_human=item.needs_human or comment.needs_human,
                        lifecycle_state="active-unresolved",
                        target_proof_sha256=reopen_proof,
                        resolution_proof_sha256=None,
                        proposal_rank=(
                            proposal.proposal_rank
                            if proposal is not None
                            else item.proposal_rank
                        ),
                        comment=comment,
                    )
                )
        else:
            next_obligations.append(item)

    reviewed_set = set(coverage_state.reviewed_paths)

    for prior in unresolved_priors:
        prior_override = overrides_by_id.get(prior.obligation_id)
        prior_fix = fixes_by_id.get(prior.obligation_id)

        if prior_override is not None:
            new_target_state = prior.target_state
            if prior.native_target is not None:
                current_slot = slot_map.get(prior.native_target)
                if current_slot is None:
                    raise ReviewInputError("override obligation slot is missing")
                if current_slot.state == "uncertain":
                    raise ReviewInputError(
                        "uncertain native slot must reconcile before slot release"
                    )
                new_target_state = (
                    "released" if prior_override.slot_released else "contingent"
                )
                slot_map[prior.native_target] = PublishedSlot(
                    target=prior.native_target,
                    state=new_target_state,
                    proof_sha256=prior_override.proof_sha256,
                )
            next_obligations.append(
                replace(
                    prior,
                    lifecycle_state="authorized-override",
                    target_state=new_target_state,
                    target_proof_sha256=(
                        prior_override.proof_sha256
                        if prior.native_target is not None
                        else None
                    ),
                    resolution_proof_sha256=prior_override.proof_sha256,
                )
            )
            continue

        if prior.obligation_id in ambiguous_prior_ids:
            next_obligations.append(
                replace(
                    prior,
                    blocking=True,
                    needs_human=True,
                    lifecycle_state="ambiguous-identity",
                )
            )
            continue

        claimed_indices = claimed_by_prior.get(prior.obligation_id, [])
        if len(claimed_indices) == 1:
            c_idx = claimed_indices[0]
            comment = result.comments[c_idx]
            proposal = proposal_by_index.get(c_idx)
            # Never allow model output or #427 proposal to clear a prior obligation
            # or downgrade its blocking/human status.
            preserved_blocking = prior.blocking or comment.blocks_approval
            preserved_human = prior.needs_human or comment.needs_human
            downgraded = prior.blocking and not comment.blocks_approval
            next_obligations.append(
                replace(
                    prior,
                    path=comment.path,
                    line=comment.line,
                    side=comment.side,
                    symbol=comment.symbol or prior.symbol,
                    blocking=preserved_blocking,
                    needs_human=preserved_human,
                    lifecycle_state=(
                        "omitted-uncertain" if downgraded else "active-unresolved"
                    ),
                    proposal_rank=(
                        proposal.proposal_rank
                        if proposal is not None
                        else prior.proposal_rank
                    ),
                    comment=comment,
                )
            )
            continue

        # Prior obligation was omitted from current result.comments.
        if prior_fix is not None:
            if prior_fix.resolution_criterion != prior.resolution_criterion:
                raise ReviewInputError(
                    "confirmed fix criterion does not match original obligation"
                )
            path_verified = (
                bool(reviewed_set)
                and all(vp in reviewed_set for vp in prior_fix.verified_paths)
                if prior.path is None
                else (
                    prior.path in prior_fix.verified_paths
                    and prior.path in reviewed_set
                )
            )
            if review_complete and path_verified:
                new_target_state = prior.target_state
                if prior.native_target is not None:
                    current_slot = slot_map.get(prior.native_target)
                    if current_slot is None:
                        raise ReviewInputError("fixed obligation slot is missing")
                    if current_slot.state == "uncertain":
                        raise ReviewInputError(
                            "uncertain native slot must reconcile before fix release"
                        )
                    new_target_state = "released"
                    slot_map[prior.native_target] = PublishedSlot(
                        target=prior.native_target,
                        state="released",
                        proof_sha256=prior_fix.evidence_sha256,
                    )
                next_obligations.append(
                    replace(
                        prior,
                        lifecycle_state="verified-fixed",
                        target_state=new_target_state,
                        target_proof_sha256=(
                            prior_fix.evidence_sha256
                            if prior.native_target is not None
                            else None
                        ),
                        resolution_proof_sha256=prior_fix.evidence_sha256,
                    )
                )
                continue

        if full_rescan_qualified and rescan_proof is not None:
            new_target_state = prior.target_state
            if prior.native_target is not None:
                current_slot = slot_map.get(prior.native_target)
                if current_slot is not None:
                    if current_slot.state == "uncertain":
                        raise ReviewInputError(
                            "uncertain native slot must reconcile before rescan release"
                        )
                    new_target_state = "released"
                    slot_map[prior.native_target] = PublishedSlot(
                        target=prior.native_target,
                        state="released",
                        proof_sha256=rescan_proof,
                    )
            next_obligations.append(
                replace(
                    prior,
                    lifecycle_state="rescan-superseded",
                    target_state=new_target_state,
                    target_proof_sha256=(
                        rescan_proof if prior.native_target is not None else None
                    ),
                    resolution_proof_sha256=rescan_proof,
                )
            )
            continue

        # Ordinary incremental or fallback-full omission: never treat absence as a fix.
        path_was_reviewed = prior.path is None or prior.path in reviewed_set
        next_obligations.append(
            replace(
                prior,
                lifecycle_state=(
                    "omitted-uncertain"
                    if path_was_reviewed or coverage_mode == "fallback-full"
                    else "active-unresolved"
                ),
            )
        )

    # Admit genuinely new comments (and ambiguous current comments).
    for index, comment in enumerate(result.comments):
        if (
            index in matched_prior_for_comment
            and index not in ambiguous_comment_indices
        ):
            continue
        proposal = proposal_by_index.get(index)
        created = obligation_from_comment(
            comment,
            generation=round_number - 1,
            proposal_rank=(
                proposal.proposal_rank
                if proposal is not None
                else DEFAULT_PROPOSAL_RANK
            ),
        )
        if index in ambiguous_comment_indices:
            created = replace(
                created,
                blocking=True,
                needs_human=True,
                evidence=(
                    "human-judgment"
                    if created.evidence == "advisory"
                    else created.evidence
                ),
                impact=created.impact or "high",
                lifecycle_state="ambiguous-identity",
            )
        next_obligations.append(created)

    updated_slots = tuple(slot_map.values())
    inventory = AuthoritativeDecisionInventory(obligations=tuple(next_obligations))
    window = select_display_active_window(
        inventory,
        slots=updated_slots,
        max_active_findings=max_active_findings,
    )
    eligibility = evaluate_window_approval_eligibility(
        changed_file_coverage=coverage_state,
        authoritative_decision_inventory=inventory,
        display_active_findings=window,
        review_complete=review_complete,
    )
    return ActiveWindowRoundState(
        head_sha=head_sha,
        base_sha=base_sha,
        round_number=round_number,
        review_complete=review_complete,
        changed_file_coverage=coverage_state,
        authoritative_decision_inventory=inventory,
        display_active_findings=window,
        approval_eligibility=eligibility,
    )


def commit_window_publication(
    state: ActiveWindowRoundState,
    *,
    created_targets: Mapping[str, str] | None = None,
    uncertain_targets: Mapping[str, str] | None = None,
) -> ActiveWindowRoundState:
    """Record confirmed or uncertain native target creation/reopening receipts.

    Keeps planning (``evaluate_active_window_round``) distinct from physical
    native publication confirmation.
    """

    if not isinstance(state, ActiveWindowRoundState):
        raise ReviewInputError(
            "commit_window_publication requires an ActiveWindowRoundState"
        )
    if created_targets is not None and not isinstance(created_targets, Mapping):
        raise ReviewInputError("created_targets must be a mapping")
    if uncertain_targets is not None and not isinstance(uncertain_targets, Mapping):
        raise ReviewInputError("uncertain_targets must be a mapping")
    created_map = {} if created_targets is None else dict(created_targets)
    uncertain_map = {} if uncertain_targets is None else dict(uncertain_targets)
    overlap = set(created_map) & set(uncertain_map)
    if overlap:
        raise ReviewInputError(
            "obligation cannot be both confirmed and uncertain in one publication"
        )
    entries_by_id = {
        entry.obligation_id: entry for entry in state.display_active_findings.entries
    }
    for oid in sorted(set(created_map) | set(uncertain_map)):
        if oid not in entries_by_id:
            raise ReviewInputError(
                "publication target receipt references a non-displayed obligation"
            )

    slot_map: dict[str, PublishedSlot] = {
        slot.target: slot for slot in state.display_active_findings.slots
    }
    claimed_unresolved_by_target: dict[str, str] = {
        item.native_target: item.obligation_id
        for item in state.authoritative_decision_inventory.obligations
        if item.unresolved and item.native_target is not None
    }
    updated_obligations: list[AuthoritativeObligation] = []

    for item in state.authoritative_decision_inventory.obligations:
        entry = entries_by_id.get(item.obligation_id)
        if item.obligation_id in created_map:
            if entry is None:
                raise ReviewInputError(
                    "publication target receipt references a non-displayed obligation"
                )
            if entry.target_action != "create":
                raise ReviewInputError("created_targets applies only to create actions")
            target_id = _require_target_id(
                created_map[item.obligation_id], label="created target_id"
            )
            if target_id in slot_map:
                raise ReviewInputError("created target_id collides with existing slot")
            proof = hashlib.sha256(
                canonical_bytes(
                    {
                        "obligation_id": item.obligation_id,
                        "target": target_id,
                        "head_sha": state.head_sha,
                        "state": "active",
                    }
                )
            ).hexdigest()
            slot_map[target_id] = PublishedSlot(
                target=target_id, state="active", proof_sha256=proof
            )
            claimed_unresolved_by_target[target_id] = item.obligation_id
            updated_obligations.append(
                replace(
                    item,
                    native_target=target_id,
                    target_state="active",
                    target_proof_sha256=proof,
                )
            )
        elif item.obligation_id in uncertain_map:
            if entry is None:
                raise ReviewInputError(
                    "publication target receipt references a non-displayed obligation"
                )
            target_id = _require_target_id(
                uncertain_map[item.obligation_id], label="uncertain target_id"
            )
            if entry.target_action == "create" and target_id in slot_map:
                raise ReviewInputError(
                    "uncertain target_id collides with existing slot"
                )
            proof = hashlib.sha256(
                canonical_bytes(
                    {
                        "obligation_id": item.obligation_id,
                        "target": target_id,
                        "head_sha": state.head_sha,
                        "state": "uncertain",
                    }
                )
            ).hexdigest()
            slot_map[target_id] = PublishedSlot(
                target=target_id, state="uncertain", proof_sha256=proof
            )
            claimed_unresolved_by_target[target_id] = item.obligation_id
            updated_obligations.append(
                replace(
                    item,
                    native_target=target_id,
                    target_state="uncertain",
                    target_proof_sha256=proof,
                )
            )
        elif (
            entry is not None
            and entry.target_action == "reopen"
            and item.native_target is not None
        ):
            existing_slot = slot_map.get(item.native_target)
            other_owner = claimed_unresolved_by_target.get(item.native_target)
            if (
                existing_slot is None
                or existing_slot.state not in {"released", "contingent"}
                or (other_owner is not None and other_owner != item.obligation_id)
            ):
                raise ReviewInputError(
                    "reopen target collides with an active or uncertain slot"
                )
            proof = hashlib.sha256(
                canonical_bytes(
                    {
                        "obligation_id": item.obligation_id,
                        "target": item.native_target,
                        "head_sha": state.head_sha,
                        "state": "active",
                    }
                )
            ).hexdigest()
            slot_map[item.native_target] = PublishedSlot(
                target=item.native_target, state="active", proof_sha256=proof
            )
            claimed_unresolved_by_target[item.native_target] = item.obligation_id
            updated_obligations.append(
                replace(
                    item,
                    target_state="active",
                    target_proof_sha256=proof,
                )
            )
        else:
            updated_obligations.append(item)

    inventory = AuthoritativeDecisionInventory(obligations=tuple(updated_obligations))
    slots = tuple(slot_map.values())
    window = select_display_active_window(
        inventory,
        slots=slots,
        max_active_findings=state.display_active_findings.max_active_findings,
    )
    eligibility = evaluate_window_approval_eligibility(
        changed_file_coverage=state.changed_file_coverage,
        authoritative_decision_inventory=inventory,
        display_active_findings=window,
        review_complete=state.review_complete,
    )
    return replace(
        state,
        authoritative_decision_inventory=inventory,
        display_active_findings=window,
        approval_eligibility=eligibility,
    )


def reconcile_uncertain_slot(
    state: ActiveWindowRoundState,
    *,
    target: str,
    resolved_state: str,
    proof_sha256: str,
    confirmed_target: str | None = None,
) -> ActiveWindowRoundState:
    """Reconcile an uncertain native slot after exact owned readback.

    When ``resolved_state == "released"``, readback proved that the native
    target was never created on GitHub: ``PublishedSlot(target=target,
    state="released", proof_sha256=proof_sha256)`` retains the audit link in
    ``slots``, while ``native_target`` is cleared on the obligation so the next
    round emits ``target_action="create"`` rather than reopening a nonexistent
    thread.
    """

    if not isinstance(state, ActiveWindowRoundState):
        raise ReviewInputError(
            "reconcile_uncertain_slot requires an ActiveWindowRoundState"
        )
    _require_target_id(target, label="target")
    if resolved_state not in {"active", "released"}:
        raise ReviewInputError("resolved_state must be active or released")
    _require_sha256(proof_sha256, label="proof_sha256")
    slot_map = {s.target: s for s in state.display_active_findings.slots}
    existing = slot_map.get(target)
    if existing is None or existing.state != "uncertain":
        raise ReviewInputError("reconcile_uncertain_slot requires an uncertain target")
    destination = (
        target
        if confirmed_target is None
        else _require_target_id(confirmed_target, label="confirmed_target")
    )
    if destination != target:
        if resolved_state != "active" or destination in slot_map:
            raise ReviewInputError("confirmed target conflicts with retained slots")
        slot_map[target] = PublishedSlot(
            target=target, state="released", proof_sha256=proof_sha256
        )
    slot_map[destination] = PublishedSlot(
        target=destination, state=resolved_state, proof_sha256=proof_sha256
    )
    updated_obligations = tuple(
        replace(
            item,
            native_target=destination if resolved_state == "active" else None,
            target_state=resolved_state if resolved_state == "active" else None,
            target_proof_sha256=proof_sha256 if resolved_state == "active" else None,
        )
        if item.native_target == target
        else item
        for item in state.authoritative_decision_inventory.obligations
    )
    inventory = AuthoritativeDecisionInventory(obligations=updated_obligations)
    slots = tuple(slot_map.values())
    window = select_display_active_window(
        inventory,
        slots=slots,
        max_active_findings=state.display_active_findings.max_active_findings,
    )
    eligibility = evaluate_window_approval_eligibility(
        changed_file_coverage=state.changed_file_coverage,
        authoritative_decision_inventory=inventory,
        display_active_findings=window,
        review_complete=state.review_complete,
    )
    return replace(
        state,
        authoritative_decision_inventory=inventory,
        display_active_findings=window,
        approval_eligibility=eligibility,
    )


def baseline_from_active_window(
    state: ActiveWindowRoundState,
    *,
    baseline_template: ReviewBaseline,
) -> ReviewBaseline:
    """Build a compatible ``ReviewBaseline`` preserving all unresolved obligations."""

    if not isinstance(state, ActiveWindowRoundState):
        raise ReviewInputError("state must be an ActiveWindowRoundState")
    return state.to_baseline(
        baseline_template=baseline_template, generation=state.round_number - 1
    )


def project_display_review_result(
    result: ReviewResult,
    state: ActiveWindowRoundState,
) -> ReviewResult:
    """Project a ``ReviewResult`` onto ``DisplayActiveWindow`` with zero deferred leakage."""

    if not isinstance(result, ReviewResult):
        raise ReviewInputError("project_display_review_result requires a ReviewResult")
    if not isinstance(state, ActiveWindowRoundState):
        raise ReviewInputError(
            "project_display_review_result requires an ActiveWindowRoundState"
        )
    assert_no_deferred_leakage(state.to_public_status())
    obligations_by_id = {
        item.obligation_id: item
        for item in state.authoritative_decision_inventory.obligations
    }
    display_comments: list[ReviewComment] = []
    display_lifecycles: list[FindingLifecycleRecord] = []
    seen_fingerprints: set[str] = set()
    current_gen = max(0, state.round_number - 1)
    for entry in state.display_active_findings.entries:
        ob = obligations_by_id.get(entry.obligation_id)
        if ob is None or ob.comment is None:
            continue
        base_comment = ob.comment
        projected = replace(
            base_comment,
            path=ob.path or base_comment.path,
            line=ob.line if ob.line is not None else base_comment.line,
            side=ob.side,
            symbol=ob.symbol if ob.symbol is not None else base_comment.symbol,
            needs_human=ob.needs_human,
            effective_blocking=(
                True
                if (
                    ob.blocking
                    or ob.lifecycle_state in {"omitted-uncertain", "ambiguous-identity"}
                )
                else False
            ),
        )
        display_comments.append(projected)
        fp = finding_lifecycle_for_comment(
            projected, generation=ob.generation
        ).fingerprint
        if fp not in seen_fingerprints:
            seen_fingerprints.add(fp)
            lc_state = (
                "uncertain"
                if ob.lifecycle_state in {"omitted-uncertain", "ambiguous-identity"}
                else ("still-present" if ob.generation < current_gen else "new")
            )
            display_lifecycles.append(
                FindingLifecycleRecord(fingerprint=fp, state=lc_state)
            )
    comments_tuple = tuple(display_comments)
    lifecycles_tuple = tuple(display_lifecycles)
    if (
        comments_tuple == result.comments
        and lifecycles_tuple == result.finding_lifecycles
    ):
        return result
    return replace(
        result,
        comments=comments_tuple,
        finding_lifecycles=lifecycles_tuple,
        transaction=None,
    )


def apply_dispositions_to_window_state(
    state: ActiveWindowRoundState,
    *,
    honored_fingerprints: Collection[str],
    honored_instances: Collection[str],
    actor: str,
    head_sha: str,
    slot_released: bool,
    coverage_uncertainty_accepted: bool = False,
) -> ActiveWindowRoundState:
    """Apply honored maintainer command dispositions to a round's window state.

    Only obligations displayed in ``state.display_active_findings`` may be
    resolved by command overrides on ``head_sha``; non-displayed obligations
    stay unresolved in ``authoritative_decision_inventory`` and continue to
    gate ``approval_eligibility``.
    """

    if not isinstance(state, ActiveWindowRoundState):
        raise ReviewInputError(
            "apply_dispositions_to_window_state requires an ActiveWindowRoundState"
        )
    _require_commit_sha(head_sha, label="head_sha")
    if state.head_sha != head_sha:
        raise ReviewInputError(
            "window disposition head_sha does not match active window"
        )
    if not isinstance(actor, str) or not actor.strip():
        raise ReviewInputError("window disposition actor must be non-empty")
    _require_bool(slot_released, label="slot_released")
    _require_bool(coverage_uncertainty_accepted, label="coverage_uncertainty_accepted")

    displayed_ids = {
        entry.obligation_id for entry in state.display_active_findings.entries
    }
    honored_fp_set = frozenset(honored_fingerprints)
    honored_inst_set = frozenset(honored_instances)
    slot_map = {slot.target: slot for slot in state.display_active_findings.slots}
    updated_obligations: list[AuthoritativeObligation] = []

    for item in state.authoritative_decision_inventory.obligations:
        item_instance = (
            finding_instance_fingerprint(item.comment)
            if item.comment is not None
            else None
        )
        matched = item.obligation_id in displayed_ids and (
            (item_instance is not None and item_instance in honored_inst_set)
            or item.fingerprint in honored_fp_set
        )
        if item.unresolved and matched:
            if item.native_target is not None:
                existing_slot = slot_map.get(item.native_target)
                if existing_slot is None or existing_slot.state == "uncertain":
                    updated_obligations.append(item)
                    continue
                new_target_state = "released" if slot_released else "contingent"
                proof = hashlib.sha256(
                    canonical_bytes(
                        {
                            "kind": "command-override",
                            "obligation_id": item.obligation_id,
                            "actor": actor.strip(),
                            "head_sha": head_sha,
                            "slot_released": slot_released,
                        }
                    )
                ).hexdigest()
                slot_map[item.native_target] = PublishedSlot(
                    target=item.native_target,
                    state=new_target_state,
                    proof_sha256=proof,
                )
                updated_obligations.append(
                    replace(
                        item,
                        lifecycle_state="authorized-override",
                        target_state=new_target_state,
                        target_proof_sha256=proof,
                        resolution_proof_sha256=proof,
                    )
                )
            else:
                proof = hashlib.sha256(
                    canonical_bytes(
                        {
                            "kind": "command-override",
                            "obligation_id": item.obligation_id,
                            "actor": actor.strip(),
                            "head_sha": head_sha,
                            "slot_released": slot_released,
                        }
                    )
                ).hexdigest()
                updated_obligations.append(
                    replace(
                        item,
                        lifecycle_state="authorized-override",
                        resolution_proof_sha256=proof,
                    )
                )
        else:
            updated_obligations.append(item)

    coverage_state = state.changed_file_coverage
    if (
        coverage_uncertainty_accepted
        and not coverage_state.fully_reviewed
        and not coverage_state.coverage_uncertainty_accepted
        and coverage_state.approval_state == "partial"
    ):
        cov_proof = hashlib.sha256(
            canonical_bytes(
                {
                    "kind": "command-coverage-acceptance",
                    "actor": actor.strip(),
                    "head_sha": head_sha,
                }
            )
        ).hexdigest()
        coverage_state = replace(
            coverage_state,
            coverage_uncertainty_accepted=True,
            acceptance_proof_sha256=cov_proof,
        )

    inventory = AuthoritativeDecisionInventory(obligations=tuple(updated_obligations))
    slots = tuple(slot_map.values())
    retained_entries = tuple(
        entry
        for entry in state.display_active_findings.entries
        if any(
            ob.obligation_id == entry.obligation_id and ob.unresolved
            for ob in updated_obligations
        )
    )
    window = DisplayActiveWindow(
        max_active_findings=state.display_active_findings.max_active_findings,
        entries=retained_entries,
        slots=slots,
    )
    eligibility = evaluate_window_approval_eligibility(
        changed_file_coverage=coverage_state,
        authoritative_decision_inventory=inventory,
        display_active_findings=window,
        review_complete=state.review_complete,
    )
    return replace(
        state,
        changed_file_coverage=coverage_state,
        authoritative_decision_inventory=inventory,
        display_active_findings=window,
        approval_eligibility=eligibility,
    )


__all__ = [
    "AUTHORITATIVE_WINDOW_SCHEMA_VERSION",
    "ActiveWindowApprovalEligibility",
    "ActiveWindowEntry",
    "ActiveWindowRecoveryStep",
    "ActiveWindowRoundState",
    "AuthoritativeDecisionInventory",
    "AuthoritativeObligation",
    "AuthorizedCoverageAcceptance",
    "AuthorizedHumanOverride",
    "ChangedFileCoverageState",
    "ConfirmedFixEvidence",
    "DEFAULT_PROPOSAL_RANK",
    "DecisionProposal",
    "DisplayActiveWindow",
    "EVIDENCE_RANKS",
    "FORBIDDEN_PUBLIC_DEFERRED_KEYS",
    "IMPACT_RANKS",
    "OBLIGATION_LIFECYCLE_STATES",
    "OVERRIDE_DISPOSITION_ACTIONS",
    "PUBLIC_WINDOW_SCHEMA_VERSION",
    "RECOVERY_ROUTE_KINDS",
    "RESOLVED_LIFECYCLE_STATES",
    "UNRESOLVED_LIFECYCLE_STATES",
    "WITHHELD_REASON_CODES",
    "apply_dispositions_to_window_state",
    "assert_no_deferred_leakage",
    "baseline_from_active_window",
    "commit_window_publication",
    "evaluate_active_window_round",
    "evaluate_window_approval_eligibility",
    "extract_decision_proposals",
    "obligation_from_comment",
    "project_display_review_result",
    "reconcile_uncertain_slot",
    "select_display_active_window",
]


from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
