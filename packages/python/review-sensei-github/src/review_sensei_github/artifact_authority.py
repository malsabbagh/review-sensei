"""Artifact authority classification, mutation resilience, and restricted reenroll (G14).

Classifies every GitHub review artifact into one of three disjoint classes:
- ``authority``: ``session_ledger_marker``, ``retained_result_part``,
  ``retained_context_part``
- ``evidence``: ``owned_readback_receipt``, ``archive_chain_part``
- ``projection``: ``native_review_summary``, ``inline_review_comment``,
  ``check_run_text``, ``comment_reaction``

Enforces:
1. Human edits or deletions of ``projection`` artifacts never manufacture
   approval, authority, fix proof, or obligation erasure, and never trigger
   automatic spam re-creation.
2. Malformed, tampered, duplicated, stale, or foreign ``authority`` markers fail
   closed without auto-recreating comments.
3. Human edits or deletions after HTTP response loss preserve conservative
   uncertainty (``uncertain-after-response-loss``) with reservations and charges
   held, whereas unedited App-owned artifacts reconcile once via exact readback.
4. Archive-chain references to deleted or tampered comments refuse cleanly with
   specific missing-evidence codes.
5. Restricted operator ``reenroll`` recovers deleted witness-backed or expired
   markers under strict maintainer/admin and zero-active-liability gates without
   ever resetting healthy active obligations.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from review_sensei.active_finding_window import AuthoritativeDecisionInventory
from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.disposition import _require_reason, authorized_override
from review_sensei.errors import ReviewInputError
from review_sensei.session import SessionIdentity, SessionRecord

from .session_ledger import (
    SESSION_MARKER_PREFIX,
    GitHubIssueCommentSessionLedger,
    parse_session_comment,
)

_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_COMMIT_SHA_RE = re.compile(r"^[a-f0-9]{40,64}$")

ARTIFACT_CLASSES = frozenset({"authority", "evidence", "projection"})

AUTHORITY_ARTIFACT_KINDS = frozenset(
    {
        "session_ledger_marker",
        "retained_result_part",
        "retained_context_part",
    }
)
EVIDENCE_ARTIFACT_KINDS = frozenset(
    {
        "owned_readback_receipt",
        "archive_chain_part",
    }
)
PROJECTION_ARTIFACT_KINDS = frozenset(
    {
        "native_review_summary",
        "inline_review_comment",
        "check_run_text",
        "comment_reaction",
    }
)
ALL_ARTIFACT_KINDS = (
    AUTHORITY_ARTIFACT_KINDS | EVIDENCE_ARTIFACT_KINDS | PROJECTION_ARTIFACT_KINDS
)

ARTIFACT_CLASS_BY_KIND: Mapping[str, str] = {
    **{kind: "authority" for kind in AUTHORITY_ARTIFACT_KINDS},
    **{kind: "evidence" for kind in EVIDENCE_ARTIFACT_KINDS},
    **{kind: "projection" for kind in PROJECTION_ARTIFACT_KINDS},
}

ARTIFACT_RECOVERY_ROUTES = frozenset(
    {
        "none-needed",
        "verify",
        "override",
        "accept-risk",
        "rescan",
        "operator-reenroll",
        "operator-reconcile",
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


def classify_review_artifact(artifact_kind: str) -> str:
    """Return ``authority``, ``evidence``, or ``projection`` for ``artifact_kind``."""

    if (
        not isinstance(artifact_kind, str)
        or artifact_kind not in ARTIFACT_CLASS_BY_KIND
    ):
        raise ReviewInputError("unknown review artifact kind")
    return ARTIFACT_CLASS_BY_KIND[artifact_kind]


@dataclass(frozen=True)
class ObservedGitHubArtifact:
    """Observed state of one GitHub comment, review, check run, or reaction."""

    artifact_kind: str
    artifact_id: str
    present: bool
    author_login: str | None = None
    author_user_id: int | None = None
    author_is_authenticated_app: bool = True
    edited_by_human: bool = False
    expected_sha256: str | None = None
    observed_sha256: str | None = None
    raw_body: str | None = None
    duplicate_count: int = 1
    stale_generation: bool = False
    response_lost: bool = False

    def __post_init__(self) -> None:
        classify_review_artifact(self.artifact_kind)
        if not isinstance(self.artifact_id, str) or not self.artifact_id.strip():
            raise ReviewInputError("artifact_id must be a non-empty string")
        _require_bool(self.present, label="present")
        _require_bool(
            self.author_is_authenticated_app, label="author_is_authenticated_app"
        )
        _require_bool(self.edited_by_human, label="edited_by_human")
        _require_bool(self.stale_generation, label="stale_generation")
        _require_bool(self.response_lost, label="response_lost")
        if type(self.duplicate_count) is not int or self.duplicate_count < 0:
            raise ReviewInputError("duplicate_count must be >= 0")
        if not self.present and self.duplicate_count != 0:
            object.__setattr__(self, "duplicate_count", 0)
        if self.expected_sha256 is not None:
            _require_sha256(self.expected_sha256, label="expected_sha256")
        if self.observed_sha256 is not None:
            _require_sha256(self.observed_sha256, label="observed_sha256")
        if self.author_user_id is not None and (
            type(self.author_user_id) is not int or self.author_user_id < 1
        ):
            raise ReviewInputError("author_user_id must be a positive integer")

    @property
    def artifact_class(self) -> str:
        return classify_review_artifact(self.artifact_kind)


@dataclass(frozen=True)
class ArtifactEvaluationDecision:
    """Closed outcome of evaluating one or more observed review artifacts."""

    artifact_kind: str
    artifact_class: str
    verdict: str
    reason_code: str
    authority_intact: bool
    obligations_preserved: bool
    charges_held: bool
    approval_permitted: bool
    auto_recreate: bool
    recovery_routes: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.artifact_class not in ARTIFACT_CLASSES:
            raise ReviewInputError("invalid artifact_class")
        if self.verdict not in {
            "accepted",
            "reconciled",
            "ignored_projection_mutation",
            "ignored_foreign_artifact",
            "held_uncertain",
            "refused",
        }:
            raise ReviewInputError("invalid artifact evaluation verdict")
        for flag_name in (
            "authority_intact",
            "obligations_preserved",
            "charges_held",
            "approval_permitted",
            "auto_recreate",
        ):
            _require_bool(getattr(self, flag_name), label=flag_name)
        if self.auto_recreate:
            raise ReviewInputError(
                "automatic artifact recreation is forbidden without operator command"
            )
        if not self.obligations_preserved:
            raise ReviewInputError("artifact mutations must never erase obligations")
        if any(route not in ARTIFACT_RECOVERY_ROUTES for route in self.recovery_routes):
            raise ReviewInputError("unknown artifact recovery route")


def evaluate_observed_artifact(
    observed: ObservedGitHubArtifact,
    *,
    identity: SessionIdentity | None = None,
    inventory: AuthoritativeDecisionInventory | None = None,
    enrollment_witness_present: bool = False,
    has_active_reservation: bool = False,
) -> ArtifactEvaluationDecision:
    """Evaluate a single observed artifact under G14 mutation/deletion rules."""

    if not isinstance(observed, ObservedGitHubArtifact):
        raise ReviewInputError("observed must be an ObservedGitHubArtifact")
    _require_bool(enrollment_witness_present, label="enrollment_witness_present")
    _require_bool(has_active_reservation, label="has_active_reservation")

    artifact_class = observed.artifact_class
    blocking_count = 0 if inventory is None else len(inventory.blocking_obligations)
    base_approval_ok = blocking_count == 0 and not has_active_reservation

    # 1) Response-loss handling comes first across ALL artifact classes:
    #    human edit or deletion after response loss NEVER silently succeeds or
    #    re-creates; unedited exact App-owned readback reconciles once.
    if observed.response_lost:
        exact_digest_match = (
            observed.present
            and observed.author_is_authenticated_app
            and not observed.edited_by_human
            and observed.duplicate_count == 1
            and not observed.stale_generation
            and observed.expected_sha256 is not None
            and observed.observed_sha256 == observed.expected_sha256
        )
        if exact_digest_match:
            return ArtifactEvaluationDecision(
                artifact_kind=observed.artifact_kind,
                artifact_class=artifact_class,
                verdict="reconciled",
                reason_code="reconciled-via-owned-readback",
                authority_intact=True,
                obligations_preserved=True,
                charges_held=True,
                approval_permitted=base_approval_ok,
                auto_recreate=False,
                recovery_routes=(
                    ("none-needed",)
                    if base_approval_ok
                    else ("verify", "override", "accept-risk")
                ),
            )
        return ArtifactEvaluationDecision(
            artifact_kind=observed.artifact_kind,
            artifact_class=artifact_class,
            verdict="held_uncertain",
            reason_code="uncertain-after-response-loss",
            authority_intact=False,
            obligations_preserved=True,
            charges_held=True,
            approval_permitted=False,
            auto_recreate=False,
            recovery_routes=("verify", "operator-reconcile"),
        )

    # 2) Projection artifacts (reviews, inline comments, check text, reactions):
    #    Human edits, deletions, or foreign spoofed text NEVER alter authority,
    #    NEVER manufacture approval, and NEVER auto-recreate spam comments!
    if artifact_class == "projection":
        mutated = (
            not observed.present
            or observed.edited_by_human
            or not observed.author_is_authenticated_app
            or (
                observed.expected_sha256 is not None
                and observed.observed_sha256 != observed.expected_sha256
            )
        )
        routes = (
            ("none-needed",)
            if base_approval_ok
            else ("verify", "override", "accept-risk", "rescan")
        )
        return ArtifactEvaluationDecision(
            artifact_kind=observed.artifact_kind,
            artifact_class="projection",
            verdict="ignored_projection_mutation" if mutated else "accepted",
            reason_code=(
                "projection-non-authoritative" if mutated else "projection-intact"
            ),
            authority_intact=True,
            obligations_preserved=True,
            charges_held=has_active_reservation,
            approval_permitted=base_approval_ok,
            auto_recreate=False,
            recovery_routes=routes,
        )

    # 3) Evidence artifacts (owned_readback_receipt, archive_chain_part):
    if artifact_class == "evidence":
        if not observed.present:
            code = (
                "archive-part-missing"
                if observed.artifact_kind == "archive_chain_part"
                else "readback-evidence-missing"
            )
            return ArtifactEvaluationDecision(
                artifact_kind=observed.artifact_kind,
                artifact_class="evidence",
                verdict="refused",
                reason_code=code,
                authority_intact=False,
                obligations_preserved=True,
                charges_held=True,
                approval_permitted=False,
                auto_recreate=False,
                recovery_routes=("rescan", "override", "operator-reconcile"),
            )
        if (
            not observed.author_is_authenticated_app
            or observed.edited_by_human
            or observed.duplicate_count != 1
            or (
                observed.expected_sha256 is not None
                and observed.observed_sha256 != observed.expected_sha256
            )
        ):
            code = (
                "archive-part-corrupted"
                if observed.artifact_kind == "archive_chain_part"
                else "readback-evidence-corrupted"
            )
            return ArtifactEvaluationDecision(
                artifact_kind=observed.artifact_kind,
                artifact_class="evidence",
                verdict="refused",
                reason_code=code,
                authority_intact=False,
                obligations_preserved=True,
                charges_held=True,
                approval_permitted=False,
                auto_recreate=False,
                recovery_routes=("rescan", "override", "operator-reconcile"),
            )
        return ArtifactEvaluationDecision(
            artifact_kind=observed.artifact_kind,
            artifact_class="evidence",
            verdict="accepted",
            reason_code="evidence-verified",
            authority_intact=True,
            obligations_preserved=True,
            charges_held=has_active_reservation,
            approval_permitted=base_approval_ok,
            auto_recreate=False,
            recovery_routes=(
                ("none-needed",) if base_approval_ok else ("verify", "override")
            ),
        )

    # 4) Authority artifacts (session_ledger_marker, retained_result_part, retained_context_part):
    if not observed.present:
        if observed.artifact_kind == "session_ledger_marker":
            deleted_routes: tuple[str, ...] = (
                ("operator-reenroll",)
                if enrollment_witness_present
                and blocking_count == 0
                and not has_active_reservation
                else ("operator-reconcile", "rescan")
            )
            return ArtifactEvaluationDecision(
                artifact_kind=observed.artifact_kind,
                artifact_class="authority",
                verdict="refused",
                reason_code="authority-marker-deleted",
                authority_intact=False,
                obligations_preserved=True,
                charges_held=True,
                approval_permitted=False,
                auto_recreate=False,
                recovery_routes=deleted_routes,
            )
        return ArtifactEvaluationDecision(
            artifact_kind=observed.artifact_kind,
            artifact_class="authority",
            verdict="refused",
            reason_code="retained-part-missing",
            authority_intact=False,
            obligations_preserved=True,
            charges_held=True,
            approval_permitted=False,
            auto_recreate=False,
            recovery_routes=("rescan", "operator-reconcile"),
        )

    if not observed.author_is_authenticated_app:
        return ArtifactEvaluationDecision(
            artifact_kind=observed.artifact_kind,
            artifact_class="authority",
            verdict="refused",
            reason_code="authority-foreign-author",
            authority_intact=False,
            obligations_preserved=True,
            charges_held=True,
            approval_permitted=False,
            auto_recreate=False,
            recovery_routes=("operator-reconcile",),
        )

    if observed.duplicate_count > 1:
        return ArtifactEvaluationDecision(
            artifact_kind=observed.artifact_kind,
            artifact_class="authority",
            verdict="refused",
            reason_code="authority-duplicate-conflict",
            authority_intact=False,
            obligations_preserved=True,
            charges_held=True,
            approval_permitted=False,
            auto_recreate=False,
            recovery_routes=("operator-reconcile",),
        )

    if observed.stale_generation:
        return ArtifactEvaluationDecision(
            artifact_kind=observed.artifact_kind,
            artifact_class="authority",
            verdict="refused",
            reason_code="authority-stale-generation",
            authority_intact=False,
            obligations_preserved=True,
            charges_held=True,
            approval_permitted=False,
            auto_recreate=False,
            recovery_routes=("verify", "operator-reconcile"),
        )

    # Validate raw session marker syntax & cryptographic digest if supplied.
    if (
        observed.artifact_kind == "session_ledger_marker"
        and observed.raw_body is not None
    ):
        if identity is None:
            raise ReviewInputError(
                "session_ledger_marker raw_body verification requires SessionIdentity"
            )
        parsed = (
            parse_session_comment(observed.raw_body, identity=identity)
            if SESSION_MARKER_PREFIX in observed.raw_body
            else None
        )
        if parsed is None:
            return ArtifactEvaluationDecision(
                artifact_kind=observed.artifact_kind,
                artifact_class="authority",
                verdict="refused",
                reason_code="authority-marker-malformed",
                authority_intact=False,
                obligations_preserved=True,
                charges_held=True,
                approval_permitted=False,
                auto_recreate=False,
                recovery_routes=("operator-reconcile",),
            )

    if observed.edited_by_human or (
        observed.expected_sha256 is not None
        and observed.observed_sha256 != observed.expected_sha256
    ):
        code = (
            "authority-marker-tampered"
            if observed.artifact_kind == "session_ledger_marker"
            else "retained-part-corrupted"
        )
        return ArtifactEvaluationDecision(
            artifact_kind=observed.artifact_kind,
            artifact_class="authority",
            verdict="refused",
            reason_code=code,
            authority_intact=False,
            obligations_preserved=True,
            charges_held=True,
            approval_permitted=False,
            auto_recreate=False,
            recovery_routes=("rescan", "operator-reconcile"),
        )

    return ArtifactEvaluationDecision(
        artifact_kind=observed.artifact_kind,
        artifact_class="authority",
        verdict="accepted",
        reason_code="authority-verified",
        authority_intact=True,
        obligations_preserved=True,
        charges_held=has_active_reservation,
        approval_permitted=base_approval_ok,
        auto_recreate=False,
        recovery_routes=(
            ("none-needed",)
            if base_approval_ok
            else ("verify", "override", "accept-risk")
        ),
    )


@dataclass(frozen=True)
class ArchiveChainPartRef:
    """Expected digest and ownership binding for one archive-chain carrier comment."""

    comment_id: int
    expected_sha256: str

    def __post_init__(self) -> None:
        if type(self.comment_id) is not int or self.comment_id < 1:
            raise ReviewInputError("archive comment_id must be a positive integer")
        _require_sha256(self.expected_sha256, label="archive expected_sha256")


def verify_archive_chain_integrity(
    expected_chain: Sequence[ArchiveChainPartRef],
    observed_comments: Mapping[int, ObservedGitHubArtifact],
) -> ArtifactEvaluationDecision:
    """Verify every referenced archive-chain comment is present, owned, and intact."""

    if not expected_chain:
        raise ReviewInputError("expected_chain must be non-empty")
    for ref in expected_chain:
        if type(ref) is not ArchiveChainPartRef:
            raise ReviewInputError("expected_chain requires ArchiveChainPartRef items")
        obs = observed_comments.get(ref.comment_id)
        if obs is None or not obs.present:
            return ArtifactEvaluationDecision(
                artifact_kind="archive_chain_part",
                artifact_class="evidence",
                verdict="refused",
                reason_code="archive-part-missing",
                authority_intact=False,
                obligations_preserved=True,
                charges_held=True,
                approval_permitted=False,
                auto_recreate=False,
                recovery_routes=("rescan", "override", "operator-reconcile"),
            )
        checked = evaluate_observed_artifact(
            ObservedGitHubArtifact(
                artifact_kind="archive_chain_part",
                artifact_id=str(ref.comment_id),
                present=obs.present,
                author_login=obs.author_login,
                author_user_id=obs.author_user_id,
                author_is_authenticated_app=obs.author_is_authenticated_app,
                edited_by_human=obs.edited_by_human,
                expected_sha256=ref.expected_sha256,
                observed_sha256=obs.observed_sha256,
                duplicate_count=obs.duplicate_count,
                stale_generation=obs.stale_generation,
                response_lost=obs.response_lost,
            )
        )
        if checked.verdict != "accepted":
            return checked
    return ArtifactEvaluationDecision(
        artifact_kind="archive_chain_part",
        artifact_class="evidence",
        verdict="accepted",
        reason_code="archive-chain-verified",
        authority_intact=True,
        obligations_preserved=True,
        charges_held=False,
        approval_permitted=True,
        auto_recreate=False,
        recovery_routes=("none-needed",),
    )


@dataclass(frozen=True)
class OperatorReenrollRequest:
    """Authenticated maintainer/admin request for restricted marker re-enrollment."""

    identity: SessionIdentity
    head_sha: str
    operator_login: str
    association: str
    reason: str
    permission: str | None = None
    pr_open: bool = True
    pr_draft: bool = False
    enrollment_witness_present: bool = False
    active_blocking_obligations: int = 0
    has_active_reservation: bool = False
    has_uncertain_write: bool = False
    quarantine_lineage_present: bool = False
    assessment_queue_present: bool = False

    def __post_init__(self) -> None:
        if type(self.identity) is not SessionIdentity:
            raise ReviewInputError("identity must be a SessionIdentity")
        _require_commit_sha(self.head_sha, label="reenroll head_sha")
        if (
            not isinstance(self.operator_login, str)
            or not self.operator_login.strip()
            or len(self.operator_login.encode("utf-8")) > 256
        ):
            raise ReviewInputError("operator_login is invalid")
        _require_reason(self.reason)
        _require_bool(self.pr_open, label="pr_open")
        _require_bool(self.pr_draft, label="pr_draft")
        _require_bool(
            self.enrollment_witness_present, label="enrollment_witness_present"
        )
        if (
            type(self.active_blocking_obligations) is not int
            or self.active_blocking_obligations < 0
        ):
            raise ReviewInputError("active_blocking_obligations must be >= 0")
        _require_bool(self.has_active_reservation, label="has_active_reservation")
        _require_bool(self.has_uncertain_write, label="has_uncertain_write")
        _require_bool(
            self.quarantine_lineage_present, label="quarantine_lineage_present"
        )
        _require_bool(self.assessment_queue_present, label="assessment_queue_present")


@dataclass(frozen=True)
class OperatorReenrollEligibility:
    """Eligibility result for restricted operator ``reenroll``."""

    eligible: bool
    reason_code: str
    recovery_routes: tuple[str, ...]


def evaluate_operator_reenroll(
    request: OperatorReenrollRequest,
    *,
    ledger_status: str,
) -> OperatorReenrollEligibility:
    """Enforce strict G14 eligibility gates before any marker re-enrollment.

    Deletion of a marker is NEVER implicit permission for fresh budget or
    abandonment of active obligations, reservations, uncertain writes,
    quarantine lineage, or assessment queues.
    """

    if not isinstance(request, OperatorReenrollRequest):
        raise ReviewInputError("request must be an OperatorReenrollRequest")
    if ledger_status not in {
        "ok",
        "missing",
        "expired",
        "conflict",
        "integrity-failed",
    }:
        raise ReviewInputError("ledger_status is invalid")

    if not authorized_override(
        association=request.association, permission=request.permission
    ):
        return OperatorReenrollEligibility(
            eligible=False,
            reason_code="reenroll-unauthorized-actor",
            recovery_routes=("operator-reconcile",),
        )
    if not request.pr_open or request.pr_draft:
        return OperatorReenrollEligibility(
            eligible=False,
            reason_code="reenroll-pr-not-ready-open",
            recovery_routes=("none-needed",),
        )
    if request.has_uncertain_write:
        return OperatorReenrollEligibility(
            eligible=False,
            reason_code="reenroll-blocked-by-uncertain-write",
            recovery_routes=("verify", "operator-reconcile"),
        )
    if request.has_active_reservation:
        return OperatorReenrollEligibility(
            eligible=False,
            reason_code="reenroll-blocked-by-active-reservation",
            recovery_routes=("verify", "operator-reconcile"),
        )
    if request.active_blocking_obligations > 0:
        return OperatorReenrollEligibility(
            eligible=False,
            reason_code="reenroll-blocked-by-active-obligations",
            recovery_routes=("verify", "override", "accept-risk"),
        )
    if request.quarantine_lineage_present or request.assessment_queue_present:
        return OperatorReenrollEligibility(
            eligible=False,
            reason_code="reenroll-blocked-by-quarantine-or-queue",
            recovery_routes=("operator-reconcile",),
        )
    if ledger_status == "ok":
        return OperatorReenrollEligibility(
            eligible=False,
            reason_code="reenroll-refused-live-marker",
            recovery_routes=("verify",),
        )
    if ledger_status in {"conflict", "integrity-failed"}:
        return OperatorReenrollEligibility(
            eligible=False,
            reason_code=f"reenroll-refused-{ledger_status}",
            recovery_routes=("operator-reconcile",),
        )
    if ledger_status == "missing" and not request.enrollment_witness_present:
        return OperatorReenrollEligibility(
            eligible=False,
            reason_code="reenroll-missing-enrollment-witness",
            recovery_routes=("operator-reconcile",),
        )
    return OperatorReenrollEligibility(
        eligible=True,
        reason_code=f"reenroll-eligible-{ledger_status}",
        recovery_routes=("operator-reenroll",),
    )


@dataclass(frozen=True)
class OperatorReenrollReceipt:
    """Audited receipt returned after restricted operator ``reenroll`` succeeds."""

    identity: SessionIdentity
    head_sha: str
    operator_login: str
    previous_status: str
    restored_record_sha256: str
    audit_sha256: str


def execute_restricted_operator_reenroll(
    ledger: GitHubIssueCommentSessionLedger,
    request: OperatorReenrollRequest,
    *,
    now: datetime | None = None,
) -> tuple[SessionRecord, OperatorReenrollReceipt]:
    """Verify strict G14 eligibility and execute ``ledger.reenroll`` once."""

    if not isinstance(ledger, GitHubIssueCommentSessionLedger):
        raise ReviewInputError("ledger must be a GitHubIssueCommentSessionLedger")
    if not isinstance(request, OperatorReenrollRequest):
        raise ReviewInputError("request must be an OperatorReenrollRequest")

    loaded = ledger.load(request.identity, now=now)
    quarantine_present = request.quarantine_lineage_present or (
        loaded.record is not None and loaded.record.quarantine_lineage is not None
    )
    queue_present = request.assessment_queue_present or (
        loaded.record is not None and loaded.record.assessment_queue is not None
    )
    reservation_present = request.has_active_reservation or (
        loaded.record is not None and loaded.record.reservation_id is not None
    )
    enriched_request = OperatorReenrollRequest(
        identity=request.identity,
        head_sha=request.head_sha,
        operator_login=request.operator_login,
        association=request.association,
        permission=request.permission,
        reason=request.reason,
        pr_open=request.pr_open,
        pr_draft=request.pr_draft,
        enrollment_witness_present=request.enrollment_witness_present,
        active_blocking_obligations=request.active_blocking_obligations,
        has_active_reservation=reservation_present,
        has_uncertain_write=request.has_uncertain_write,
        quarantine_lineage_present=quarantine_present,
        assessment_queue_present=queue_present,
    )
    eligibility = evaluate_operator_reenroll(
        enriched_request, ledger_status=loaded.status
    )
    if not eligibility.eligible:
        raise ReviewInputError(
            f"restricted operator reenroll refused: {eligibility.reason_code}"
        )

    restored = ledger.reenroll(request.identity, now=now)
    audit_sha256 = hashlib.sha256(
        canonical_bytes(
            {
                "repository": request.identity.repository,
                "repository_id": request.identity.repository_id,
                "pull_request": request.identity.pull_request,
                "head_sha": request.head_sha,
                "operator_login": request.operator_login,
                "previous_status": loaded.status,
                "restored_record_sha256": restored.record_sha256,
                "reason": request.reason,
            }
        )
    ).hexdigest()
    receipt = OperatorReenrollReceipt(
        identity=request.identity,
        head_sha=request.head_sha,
        operator_login=request.operator_login,
        previous_status=loaded.status,
        restored_record_sha256=restored.record_sha256,
        audit_sha256=audit_sha256,
    )
    return restored, receipt


__all__ = [
    "ALL_ARTIFACT_KINDS",
    "ARTIFACT_CLASSES",
    "ARTIFACT_CLASS_BY_KIND",
    "ARTIFACT_RECOVERY_ROUTES",
    "AUTHORITY_ARTIFACT_KINDS",
    "ArchiveChainPartRef",
    "ArtifactEvaluationDecision",
    "EVIDENCE_ARTIFACT_KINDS",
    "ObservedGitHubArtifact",
    "OperatorReenrollEligibility",
    "OperatorReenrollReceipt",
    "OperatorReenrollRequest",
    "PROJECTION_ARTIFACT_KINDS",
    "classify_review_artifact",
    "evaluate_observed_artifact",
    "evaluate_operator_reenroll",
    "execute_restricted_operator_reenroll",
    "verify_archive_chain_integrity",
]
