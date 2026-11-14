"""Repository and trust-boundary transitions, refusal gates, and migration (G15).

Models seven distinct repository and privilege transitions separately:
1. ``repository_rename``: slug changed while ``repository_id`` and ``owner_id``
   remain identical.
2. ``ownership_transfer``: ``owner_id`` or ``owner_login`` changed even when
   numeric ``repository_id`` is unchanged (unchanged ``repository_id`` alone
   NEVER proves transfer trust).
3. ``fork_status_change``: repository fork flag changed or target is a fork.
4. ``app_reinstall``: GitHub App ``installation_id`` changed.
5. ``repository_selection_change``: repository removed from or re-added to the
   GitHub App's selected repositories.
6. ``permission_revoke_or_restore``: required installation permissions revoked,
   downgraded, or restored since enrollment.
7. ``actor_privilege_downgrade``: actor association or repository permission
   dropped below the operation's required authority.

Evaluates transitions across three injection points (``before_admission``,
``before_native_effect``, ``after_unknown_effect``) guaranteeing zero
cross-repository writes, zero implicit fresh allowances, preserved obligations
and charges, and an authenticated trust-migration recovery path for legitimate
transitions.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass

from review_sensei.active_finding_window import AuthoritativeDecisionInventory
from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.disposition import _require_reason, authorized_override
from review_sensei.errors import ReviewInputError
from review_sensei.session import SessionIdentity, SessionRecord

_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_COMMIT_SHA_RE = re.compile(r"^[a-f0-9]{40,64}$")
_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_LOGIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

TRUST_TRANSITION_KINDS = frozenset(
    {
        "repository_rename",
        "ownership_transfer",
        "fork_status_change",
        "app_reinstall",
        "repository_selection_change",
        "permission_revoke_or_restore",
        "actor_privilege_downgrade",
    }
)

TRANSITION_PHASES = frozenset(
    {
        "before_admission",
        "before_native_effect",
        "after_unknown_effect",
    }
)

PERMISSION_RANKS: Mapping[str, int] = {
    "none": 0,
    "read": 1,
    "triage": 2,
    "write": 3,
    "maintain": 4,
    "admin": 5,
}

DEFAULT_REQUIRED_INSTALLATION_PERMISSIONS: Mapping[str, str] = {
    "contents": "read",
    "issues": "write",
    "pull_requests": "write",
    "checks": "write",
}

OPERATION_MIN_ACTOR_PERMISSION: Mapping[str, str] = {
    "review": "read",
    "reply": "write",
    "command": "maintain",
    "override": "maintain",
}


def _require_bool(value: object, *, label: str) -> bool:
    if type(value) is not bool:
        raise ReviewInputError(f"{label} must be a boolean")
    return value


def _require_positive_int(value: object, *, label: str) -> int:
    if type(value) is not int or not 0 < value <= 9_007_199_254_740_991:
        raise ReviewInputError(f"{label} must be a positive integer")
    return value


def _require_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ReviewInputError(f"{label} must be a 64-character lowercase hex SHA-256")
    return value


def _require_commit_sha(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _COMMIT_SHA_RE.fullmatch(value) is None:
        raise ReviewInputError(f"{label} must be a commit SHA")
    return value


@dataclass(frozen=True)
class AuthenticatedTrustSnapshot:
    """Authenticated repository, installation, permission, and actor snapshot.

    ``permission_epoch`` is monotonically non-decreasing across enrollment and
    migration generations: epoch regressions fail closed, and any change to
    ``granted_permissions`` during migration must advance ``permission_epoch``.
    """

    repository_id: int
    owner_id: int
    owner_login: str
    repository_slug: str
    is_fork: bool
    installation_id: int
    repository_selected: bool
    granted_permissions: Mapping[str, str]
    actor_id: int
    actor_login: str
    actor_association: str
    actor_permission: str
    pull_request: int
    head_sha: str
    permission_epoch: int = 1

    def __post_init__(self) -> None:
        _require_positive_int(self.repository_id, label="repository_id")
        _require_positive_int(self.owner_id, label="owner_id")
        if (
            not isinstance(self.owner_login, str)
            or _LOGIN_RE.fullmatch(self.owner_login) is None
        ):
            raise ReviewInputError("owner_login is invalid")
        if (
            not isinstance(self.repository_slug, str)
            or _SLUG_RE.fullmatch(self.repository_slug) is None
        ):
            raise ReviewInputError("repository_slug is invalid")
        slug_owner, _sep, _repo = self.repository_slug.partition("/")
        if slug_owner.lower() != self.owner_login.lower():
            raise ReviewInputError(
                "repository_slug owner prefix does not match owner_login"
            )
        _require_bool(self.is_fork, label="is_fork")
        _require_positive_int(self.installation_id, label="installation_id")
        _require_bool(self.repository_selected, label="repository_selected")
        if not isinstance(self.granted_permissions, Mapping):
            raise ReviewInputError("granted_permissions must be a mapping")
        normalized_perms: dict[str, str] = {}
        for scope, level in self.granted_permissions.items():
            if not isinstance(scope, str) or not scope.strip():
                raise ReviewInputError("permission scope is invalid")
            if not isinstance(level, str) or level not in PERMISSION_RANKS:
                raise ReviewInputError("permission level is invalid")
            normalized_perms[scope] = level
        object.__setattr__(self, "granted_permissions", normalized_perms)
        _require_positive_int(self.actor_id, label="actor_id")
        if (
            not isinstance(self.actor_login, str)
            or _LOGIN_RE.fullmatch(self.actor_login) is None
        ):
            raise ReviewInputError("actor_login is invalid")
        if self.actor_association not in {
            "OWNER",
            "MEMBER",
            "COLLABORATOR",
            "CONTRIBUTOR",
            "FIRST_TIMER",
            "FIRST_TIME_CONTRIBUTOR",
            "NONE",
        }:
            raise ReviewInputError("actor_association is invalid")
        if self.actor_permission not in PERMISSION_RANKS:
            raise ReviewInputError("actor_permission is invalid")
        _require_positive_int(self.pull_request, label="pull_request")
        _require_commit_sha(self.head_sha, label="head_sha")
        _require_positive_int(self.permission_epoch, label="permission_epoch")

    def to_session_identity(self) -> SessionIdentity:
        """Return ledger-addressing key ``(repository, pull_request, repository_id)``.

        ``SessionIdentity`` addresses the PR session ledger only; admission,
        native-effect, and migration authorization MUST bind against the full
        ``AuthenticatedTrustSnapshot`` (including ``owner_id``,
        ``installation_id``, ``actor_id``, and ``permission_epoch``).
        """

        return SessionIdentity(
            repository=self.repository_slug,
            pull_request=self.pull_request,
            repository_id=self.repository_id,
        )

    def has_required_permissions(
        self,
        required: Mapping[str, str] = DEFAULT_REQUIRED_INSTALLATION_PERMISSIONS,
    ) -> bool:
        for scope, min_level in required.items():
            actual = self.granted_permissions.get(scope, "none")
            if PERMISSION_RANKS[actual] < PERMISSION_RANKS[min_level]:
                return False
        return True

    def actor_meets_operation(self, operation: str) -> bool:
        if operation not in OPERATION_MIN_ACTOR_PERMISSION:
            raise ReviewInputError("unknown trust operation")
        if operation in {"command", "override"}:
            return authorized_override(
                association=self.actor_association,
                permission=self.actor_permission,
            )
        min_perm = OPERATION_MIN_ACTOR_PERMISSION[operation]
        return PERMISSION_RANKS[self.actor_permission] >= PERMISSION_RANKS[min_perm]

    def digest(self) -> str:
        return hashlib.sha256(
            canonical_bytes(
                {
                    "repository_id": self.repository_id,
                    "owner_id": self.owner_id,
                    "owner_login": self.owner_login,
                    "repository_slug": self.repository_slug,
                    "is_fork": self.is_fork,
                    "installation_id": self.installation_id,
                    "repository_selected": self.repository_selected,
                    "granted_permissions": dict(
                        sorted(self.granted_permissions.items())
                    ),
                    "actor_id": self.actor_id,
                    "actor_login": self.actor_login,
                    "actor_association": self.actor_association,
                    "actor_permission": self.actor_permission,
                    "pull_request": self.pull_request,
                    "head_sha": self.head_sha,
                    "permission_epoch": self.permission_epoch,
                }
            )
        ).hexdigest()


def detect_trust_transitions(
    enrolled: AuthenticatedTrustSnapshot,
    observed: AuthenticatedTrustSnapshot,
    *,
    operation: str = "review",
    required_permissions: Mapping[str, str] = DEFAULT_REQUIRED_INSTALLATION_PERMISSIONS,
) -> tuple[str, ...]:
    """Detect all active trust transitions between ``enrolled`` and ``observed``.

    Explicitly separates ``repository_rename`` from ``ownership_transfer``:
    even when ``enrolled.repository_id == observed.repository_id``, any change
    in ``owner_id`` or ``owner_login`` classifies as ``ownership_transfer``!
    """

    if not isinstance(enrolled, AuthenticatedTrustSnapshot) or not isinstance(
        observed, AuthenticatedTrustSnapshot
    ):
        raise ReviewInputError("snapshots must be AuthenticatedTrustSnapshot values")
    if operation not in OPERATION_MIN_ACTOR_PERMISSION:
        raise ReviewInputError("unknown trust operation")

    detected: list[str] = []

    owner_changed = (
        enrolled.owner_id != observed.owner_id
        or enrolled.owner_login.lower() != observed.owner_login.lower()
    )
    if owner_changed or enrolled.repository_id != observed.repository_id:
        detected.append("ownership_transfer")
    elif enrolled.repository_slug != observed.repository_slug:
        detected.append("repository_rename")

    if enrolled.is_fork != observed.is_fork or observed.is_fork:
        detected.append("fork_status_change")

    if enrolled.installation_id != observed.installation_id:
        detected.append("app_reinstall")

    if (
        enrolled.repository_selected != observed.repository_selected
        or not observed.repository_selected
    ):
        detected.append("repository_selection_change")

    perms_changed = (
        dict(enrolled.granted_permissions) != dict(observed.granted_permissions)
        or enrolled.permission_epoch != observed.permission_epoch
        or not observed.has_required_permissions(required_permissions)
    )
    if perms_changed:
        detected.append("permission_revoke_or_restore")

    same_actor_downgraded = observed.actor_id == enrolled.actor_id and (
        PERMISSION_RANKS[observed.actor_permission]
        < PERMISSION_RANKS[enrolled.actor_permission]
        or (
            authorized_override(
                association=enrolled.actor_association,
                permission=enrolled.actor_permission,
            )
            and not authorized_override(
                association=observed.actor_association,
                permission=observed.actor_permission,
            )
        )
    )
    actor_downgraded = same_actor_downgraded or not observed.actor_meets_operation(
        operation
    )
    if actor_downgraded:
        detected.append("actor_privilege_downgrade")

    return tuple(detected)


@dataclass(frozen=True)
class TrustTransitionDecision:
    """Closed refusal/acceptance outcome for a repository/trust check."""

    phase: str
    allowed: bool
    detected_transitions: tuple[str, ...]
    reason_code: str
    cross_repository_write_blocked: bool
    fresh_allowance_minted: bool
    obligations_preserved: bool
    charges_held: bool
    unknown_effect_held: bool
    recovery_routes: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.phase not in TRANSITION_PHASES:
            raise ReviewInputError("phase is invalid")
        for flag in (
            "allowed",
            "cross_repository_write_blocked",
            "fresh_allowance_minted",
            "obligations_preserved",
            "charges_held",
            "unknown_effect_held",
        ):
            _require_bool(getattr(self, flag), label=flag)
        if self.fresh_allowance_minted:
            raise ReviewInputError(
                "trust transitions must never implicitly mint a fresh allowance"
            )
        if not self.obligations_preserved:
            raise ReviewInputError("trust transitions must preserve prior obligations")
        if any(t not in TRUST_TRANSITION_KINDS for t in self.detected_transitions):
            raise ReviewInputError("unknown trust transition kind")


def missing_trust_observation(
    *, phase: str, has_unknown_effect: bool = False
) -> TrustTransitionDecision:
    """Hold an enrolled boundary without claiming missing fields were observed."""
    if phase not in TRANSITION_PHASES:
        raise ReviewInputError("phase must be a valid TRANSITION_PHASES value")
    _require_bool(has_unknown_effect, label="has_unknown_effect")
    return TrustTransitionDecision(
        phase=phase,
        allowed=False,
        detected_transitions=(),
        reason_code="trust-observation-incomplete",
        cross_repository_write_blocked=True,
        fresh_allowance_minted=False,
        obligations_preserved=True,
        charges_held=True,
        unknown_effect_held=has_unknown_effect or phase == "after_unknown_effect",
        recovery_routes=("operator-reconcile",),
    )


def evaluate_trust_boundary(
    enrolled: AuthenticatedTrustSnapshot,
    observed: AuthenticatedTrustSnapshot,
    *,
    phase: str,
    operation: str = "review",
    has_prior_charges: bool = False,
    has_unknown_effect: bool = False,
    required_permissions: Mapping[str, str] = DEFAULT_REQUIRED_INSTALLATION_PERMISSIONS,
) -> TrustTransitionDecision:
    """Evaluate repository & privilege trust at a specific execution phase."""

    if phase not in TRANSITION_PHASES:
        raise ReviewInputError("phase must be a valid TRANSITION_PHASES value")
    _require_bool(has_prior_charges, label="has_prior_charges")
    _require_bool(has_unknown_effect, label="has_unknown_effect")
    if phase == "after_unknown_effect" and not has_unknown_effect:
        has_unknown_effect = True

    transitions = detect_trust_transitions(
        enrolled,
        observed,
        operation=operation,
        required_permissions=required_permissions,
    )
    cross_repo_risk = (
        enrolled.repository_id != observed.repository_id
        or enrolled.owner_id != observed.owner_id
        or enrolled.owner_login.lower() != observed.owner_login.lower()
        or enrolled.pull_request != observed.pull_request
    )

    if not transitions and not cross_repo_risk and not has_unknown_effect:
        return TrustTransitionDecision(
            phase=phase,
            allowed=True,
            detected_transitions=(),
            reason_code="trust-boundary-verified",
            cross_repository_write_blocked=False,
            fresh_allowance_minted=False,
            obligations_preserved=True,
            charges_held=has_prior_charges,
            unknown_effect_held=False,
            recovery_routes=("none-needed",),
        )

    if not transitions and has_unknown_effect and not cross_repo_risk:
        return TrustTransitionDecision(
            phase=phase,
            allowed=False,
            detected_transitions=(),
            reason_code="unknown-effect-reconciliation-required",
            cross_repository_write_blocked=False,
            fresh_allowance_minted=False,
            obligations_preserved=True,
            charges_held=True,
            unknown_effect_held=True,
            recovery_routes=("reconcile-unknown-effect", "verify"),
        )

    primary = transitions[0] if transitions else "cross_pull_request_mismatch"
    charges_held = has_prior_charges or has_unknown_effect
    fork_blocked = enrolled.is_fork or observed.is_fork
    epoch_regressed = observed.permission_epoch < enrolled.permission_epoch

    # Determine explicit bounded recovery routes per transition class!
    routes: list[str] = []
    if has_unknown_effect:
        routes.append("reconcile-unknown-effect")

    if "fork_status_change" in transitions and fork_blocked:
        routes.append("unsupported-fork-boundary")
    if (
        "repository_selection_change" in transitions
        and not observed.repository_selected
    ):
        routes.append("restore-repository-selection")
    if "permission_revoke_or_restore" in transitions and not (
        observed.has_required_permissions(required_permissions)
    ):
        routes.append("restore-app-permissions")
    if epoch_regressed:
        routes.append("operator-reconcile")
    if "actor_privilege_downgrade" in transitions:
        routes.append("authorized-maintainer-command")
    if (
        any(
            t in transitions
            for t in (
                "repository_rename",
                "ownership_transfer",
                "app_reinstall",
                "repository_selection_change",
                "permission_revoke_or_restore",
            )
        )
        and not fork_blocked
        and not epoch_regressed
    ):
        routes.append("authenticated-trust-migration")

    if not routes:
        routes.append("operator-reconcile")

    return TrustTransitionDecision(
        phase=phase,
        allowed=False,
        detected_transitions=transitions,
        reason_code=f"trust-transition-refused:{primary}",
        cross_repository_write_blocked=cross_repo_risk,
        fresh_allowance_minted=False,
        obligations_preserved=True,
        charges_held=charges_held,
        unknown_effect_held=has_unknown_effect,
        recovery_routes=tuple(routes),
    )


def compute_transfer_attestation_sha256(
    *,
    repository_id: int,
    previous_owner_id: int,
    previous_owner_login: str,
    new_owner_id: int,
    new_owner_login: str,
    pull_request: int,
    head_sha: str,
    permission_epoch: int,
    previous_owner_witness: str,
) -> str:
    """Compute canonical preimage digest for cross-owner transfer attestation."""

    _require_positive_int(repository_id, label="repository_id")
    _require_positive_int(previous_owner_id, label="previous_owner_id")
    _require_positive_int(new_owner_id, label="new_owner_id")
    if (
        not isinstance(previous_owner_login, str)
        or _LOGIN_RE.fullmatch(previous_owner_login) is None
        or not isinstance(new_owner_login, str)
        or _LOGIN_RE.fullmatch(new_owner_login) is None
    ):
        raise ReviewInputError("transfer attestation owner login is invalid")
    _require_positive_int(pull_request, label="pull_request")
    _require_commit_sha(head_sha, label="head_sha")
    _require_positive_int(permission_epoch, label="permission_epoch")
    _require_reason(previous_owner_witness)
    return hashlib.sha256(
        b"reviewsensei:ownership-transfer-attestation:v1:"
        + canonical_bytes(
            {
                "repository_id": repository_id,
                "previous_owner_id": previous_owner_id,
                "previous_owner_login": previous_owner_login.lower(),
                "new_owner_id": new_owner_id,
                "new_owner_login": new_owner_login.lower(),
                "pull_request": pull_request,
                "head_sha": head_sha,
                "permission_epoch": permission_epoch,
                "previous_owner_witness": previous_owner_witness,
            }
        )
    ).hexdigest()


@dataclass(frozen=True)
class AuthenticatedTrustMigration:
    """Explicit operator-authenticated migration proof across a trust transition."""

    old_snapshot_sha256: str
    new_snapshot_sha256: str
    operator_id: int
    operator_login: str
    operator_association: str
    operator_permission: str
    reason: str
    operator_attestation_sha256: str
    previous_owner_transfer_attestation_sha256: str | None = None
    reconciled_effect_sha256: str | None = None

    def __post_init__(self) -> None:
        _require_sha256(self.old_snapshot_sha256, label="old_snapshot_sha256")
        _require_sha256(self.new_snapshot_sha256, label="new_snapshot_sha256")
        _require_positive_int(self.operator_id, label="migration operator_id")
        if (
            not isinstance(self.operator_login, str)
            or _LOGIN_RE.fullmatch(self.operator_login) is None
        ):
            raise ReviewInputError("migration operator_login is invalid")
        if not authorized_override(
            association=self.operator_association,
            permission=self.operator_permission,
        ):
            raise ReviewInputError(
                "trust migration requires maintainer or admin authority"
            )
        _require_reason(self.reason)
        _require_sha256(
            self.operator_attestation_sha256, label="operator_attestation_sha256"
        )
        if self.previous_owner_transfer_attestation_sha256 is not None:
            _require_sha256(
                self.previous_owner_transfer_attestation_sha256,
                label="previous_owner_transfer_attestation_sha256",
            )
        if self.reconciled_effect_sha256 is not None:
            _require_sha256(
                self.reconciled_effect_sha256, label="reconciled_effect_sha256"
            )


@dataclass(frozen=True)
class TrustMigrationReceipt:
    """Audited receipt proving continuity of obligations and budget across migration."""

    migrated_snapshot: AuthenticatedTrustSnapshot
    migrated_session: SessionRecord
    preserved_inventory: AuthoritativeDecisionInventory
    migrated_transitions: tuple[str, ...]
    fresh_allowance_minted: bool
    audit_sha256: str


def verify_and_migrate_trust_boundary(
    *,
    enrolled: AuthenticatedTrustSnapshot,
    target: AuthenticatedTrustSnapshot,
    session_record: SessionRecord,
    inventory: AuthoritativeDecisionInventory,
    migration: AuthenticatedTrustMigration,
    has_unknown_effect: bool = False,
    required_permissions: Mapping[str, str] = DEFAULT_REQUIRED_INSTALLATION_PERMISSIONS,
    expected_transfer_attestation_sha256: str | None = None,
) -> TrustMigrationReceipt:
    """Authenticate and execute a bounded trust migration without resetting history.

    Preserves 100% of ``session_record`` counters/charges/reservations and
    ``inventory`` obligations while rebinding the trusted snapshot to ``target``
    so a legitimately renamed, reinstalled, or dual-attested transferred
    repository can resume review and recover cleanly to approval.
    """

    if not isinstance(enrolled, AuthenticatedTrustSnapshot) or not isinstance(
        target, AuthenticatedTrustSnapshot
    ):
        raise ReviewInputError("snapshots must be AuthenticatedTrustSnapshot values")
    if not isinstance(session_record, SessionRecord):
        raise ReviewInputError("session_record must be a SessionRecord")
    if not isinstance(inventory, AuthoritativeDecisionInventory):
        raise ReviewInputError("inventory must be an AuthoritativeDecisionInventory")
    if not isinstance(migration, AuthenticatedTrustMigration):
        raise ReviewInputError("migration must be an AuthenticatedTrustMigration")
    _require_bool(has_unknown_effect, label="has_unknown_effect")
    if expected_transfer_attestation_sha256 is not None:
        _require_sha256(
            expected_transfer_attestation_sha256,
            label="expected_transfer_attestation_sha256",
        )

    if migration.old_snapshot_sha256 != enrolled.digest():
        raise ReviewInputError("migration old_snapshot_sha256 does not match enrolled")
    if migration.new_snapshot_sha256 != target.digest():
        raise ReviewInputError("migration new_snapshot_sha256 does not match target")

    if (
        session_record.repository != enrolled.repository_slug
        or session_record.repository_id != enrolled.repository_id
        or session_record.pull_request != enrolled.pull_request
    ):
        raise ReviewInputError(
            "session_record identity does not match enrolled trust snapshot"
        )

    # Cross-repository or cross-PR substitution is unconditionally refused!
    if (
        enrolled.repository_id != target.repository_id
        or enrolled.pull_request != target.pull_request
        or enrolled.head_sha != target.head_sha
    ):
        raise ReviewInputError(
            "trust migration cannot cross repository_id, pull_request, or head_sha"
        )
    if enrolled.is_fork or target.is_fork:
        raise ReviewInputError(
            "fork repositories and fork-origin transitions cannot acquire hosted write authority via migration"
        )
    if target.permission_epoch < enrolled.permission_epoch:
        raise ReviewInputError(
            "target permission_epoch cannot regress below enrolled permission_epoch"
        )
    if not target.repository_selected:
        raise ReviewInputError(
            "target repository is not selected on the GitHub App installation"
        )
    if not target.has_required_permissions(required_permissions):
        raise ReviewInputError(
            "target installation lacks required permissions for migration"
        )
    if (
        dict(enrolled.granted_permissions) != dict(target.granted_permissions)
        and target.permission_epoch <= enrolled.permission_epoch
    ):
        raise ReviewInputError(
            "permission grant changes require a strictly newer permission_epoch"
        )
    if (
        migration.operator_id != target.actor_id
        or migration.operator_login.lower() != target.actor_login.lower()
        or migration.operator_association != target.actor_association
        or migration.operator_permission != target.actor_permission
        or not target.actor_meets_operation("override")
    ):
        raise ReviewInputError(
            "migration operator identity and authority must match authenticated target actor"
        )
    if has_unknown_effect and migration.reconciled_effect_sha256 is None:
        raise ReviewInputError(
            "unknown native effect must be reconciled before trust migration"
        )

    transitions = detect_trust_transitions(
        enrolled,
        target,
        operation="command",
        required_permissions=required_permissions,
    )
    if not transitions:
        raise ReviewInputError("no trust transition requires migration")

    if "ownership_transfer" in transitions:
        if migration.previous_owner_transfer_attestation_sha256 is None:
            raise ReviewInputError(
                "ownership transfer requires explicit previous_owner_transfer_attestation_sha256; "
                "unchanged numeric repository_id alone never proves transfer trust"
            )
        if (
            expected_transfer_attestation_sha256 is not None
            and migration.previous_owner_transfer_attestation_sha256
            != expected_transfer_attestation_sha256
        ):
            raise ReviewInputError(
                "previous_owner_transfer_attestation_sha256 does not match verified transfer attestation"
            )

    # Rebind SessionRecord repository slug while preserving ALL historical counters,
    # reservations, convergence history, failed_attempts, and inventory obligations!
    migrated_session = SessionRecord._construct(
        repository=target.repository_slug,
        pull_request=session_record.pull_request,
        repository_id=session_record.repository_id,
        completed_initial_reviews=session_record.completed_initial_reviews,
        completed_verification_rounds=session_record.completed_verification_rounds,
        failed_attempts=session_record.failed_attempts,
        generation=session_record.generation + 1,
        reservation_id=session_record.reservation_id,
        reserved_slot=session_record.reserved_slot,
        last_committed_reservation_id=session_record.last_committed_reservation_id,
        created_at=session_record.created_at,
        updated_at=session_record.updated_at,
        expires_at=session_record.expires_at,
        operator_paused=session_record.operator_paused,
        failed_attempts_head_sha=session_record.failed_attempts_head_sha,
        dispositions=session_record.dispositions,
        continuation_grants=session_record.continuation_grants,
        transaction=session_record.transaction,
        convergence_history=session_record.convergence_history,
        reservation_owner=session_record.reservation_owner,
        rescan_state=session_record.rescan_state,
        assessment_queue=session_record.assessment_queue,
        disposition_snapshot=session_record.disposition_snapshot,
        api_run_accounting=session_record.api_run_accounting,
        historical_assessments=session_record.historical_assessments,
        quarantine_lineage=session_record.quarantine_lineage,
        publication_staging=session_record.publication_staging,
    )

    audit_sha256 = hashlib.sha256(
        canonical_bytes(
            {
                "old_snapshot_sha256": migration.old_snapshot_sha256,
                "new_snapshot_sha256": migration.new_snapshot_sha256,
                "repository_id": target.repository_id,
                "owner_id": target.owner_id,
                "installation_id": target.installation_id,
                "operator_id": migration.operator_id,
                "operator_login": migration.operator_login,
                "operator_association": migration.operator_association,
                "operator_permission": migration.operator_permission,
                "operator_attestation_sha256": migration.operator_attestation_sha256,
                "previous_owner_transfer_attestation_sha256": (
                    migration.previous_owner_transfer_attestation_sha256
                ),
                "transitions": list(transitions),
                "inventory_digest": inventory.digest(),
                "completed_initial_reviews": migrated_session.completed_initial_reviews,
                "reconciled_effect_sha256": migration.reconciled_effect_sha256,
            }
        )
    ).hexdigest()

    return TrustMigrationReceipt(
        migrated_snapshot=target,
        migrated_session=migrated_session,
        preserved_inventory=inventory,
        migrated_transitions=transitions,
        fresh_allowance_minted=False,
        audit_sha256=audit_sha256,
    )


__all__ = [
    "AuthenticatedTrustMigration",
    "AuthenticatedTrustSnapshot",
    "DEFAULT_REQUIRED_INSTALLATION_PERMISSIONS",
    "OPERATION_MIN_ACTOR_PERMISSION",
    "PERMISSION_RANKS",
    "TRANSITION_PHASES",
    "TRUST_TRANSITION_KINDS",
    "TrustMigrationReceipt",
    "TrustTransitionDecision",
    "compute_transfer_attestation_sha256",
    "detect_trust_transitions",
    "evaluate_trust_boundary",
    "verify_and_migrate_trust_boundary",
]
