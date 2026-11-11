"""Qualification tests for repository and trust-boundary transitions (G15)."""

from __future__ import annotations

import hashlib
import unittest
from dataclasses import replace
from datetime import timedelta

from review_sensei_github import artifact_authority
from review_sensei_github.trust_transitions import (
    DEFAULT_REQUIRED_INSTALLATION_PERMISSIONS,
    TRANSITION_PHASES,
    TRUST_TRANSITION_KINDS,
    AuthenticatedTrustMigration,
    AuthenticatedTrustSnapshot,
    TrustTransitionDecision,
    compute_transfer_attestation_sha256,
    detect_trust_transitions,
    evaluate_trust_boundary,
    verify_and_migrate_trust_boundary,
)

from review_sensei.active_finding_window import (
    AuthoritativeDecisionInventory,
    AuthorizedHumanOverride,
    ConfirmedFixEvidence,
    evaluate_active_window_round,
    obligation_from_comment,
)
from review_sensei.coverage import CoverageManifest, FileCoverage
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewComment, ReviewResult
from review_sensei.session import SessionIdentity, SessionRecord

try:
    from session_fixture_clock import SESSION_FIXTURE_NOW
except ModuleNotFoundError:
    from tests.session_fixture_clock import SESSION_FIXTURE_NOW

FIXED_NOW = SESSION_FIXTURE_NOW
BASE_SHA = "a" * 40
HEAD_SHA = "1" * 40
HEAD_SHA_2 = "2" * 40


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _enrolled_snapshot() -> AuthenticatedTrustSnapshot:
    return AuthenticatedTrustSnapshot(
        repository_id=9001,
        owner_id=501,
        owner_login="acme",
        repository_slug="acme/widgets",
        is_fork=False,
        installation_id=3001,
        repository_selected=True,
        granted_permissions=dict(DEFAULT_REQUIRED_INSTALLATION_PERMISSIONS),
        actor_id=42,
        actor_login="alice-maintainer",
        actor_association="MEMBER",
        actor_permission="maintain",
        pull_request=88,
        head_sha=HEAD_SHA,
        permission_epoch=1,
    )


def _session_with_charges(snapshot: AuthenticatedTrustSnapshot) -> SessionRecord:
    return SessionRecord.create(
        snapshot.to_session_identity(),
        now=FIXED_NOW,
        ttl=timedelta(days=30),
        completed_initial_reviews=1,
        completed_verification_rounds=1,
        failed_attempts=1,
        generation=3,
        reservation_id=_sha("active-reservation"),
        reserved_slot="verification",
        last_committed_reservation_id=_sha("committed-initial"),
        failed_attempts_head_sha=snapshot.head_sha,
    )


def _blocking_inventory() -> AuthoritativeDecisionInventory:
    comment = ReviewComment(
        path="src/security.py",
        line=24,
        body="Missing signature verification on webhook payload.",
        blocking=True,
        effective_blocking=True,
        severity="critical",
        symbol="verify_webhook",
        defect_kind="authn",
    )
    return AuthoritativeDecisionInventory(
        obligations=(obligation_from_comment(comment),)
    )


class RepositoryTrustTransitionsG15Tests(unittest.TestCase):
    def test_artifact_authority_reexports_trust_transition_contracts(self) -> None:
        self.assertIs(
            artifact_authority.AuthenticatedTrustSnapshot,
            AuthenticatedTrustSnapshot,
        )
        self.assertIs(
            artifact_authority.AuthenticatedTrustMigration,
            AuthenticatedTrustMigration,
        )
        self.assertIs(
            artifact_authority.evaluate_trust_boundary,
            evaluate_trust_boundary,
        )
        self.assertIs(
            artifact_authority.verify_and_migrate_trust_boundary,
            verify_and_migrate_trust_boundary,
        )
        self.assertEqual(len(TRUST_TRANSITION_KINDS), 7)

    def test_unchanged_repository_id_never_proves_ownership_transfer_trust(
        self,
    ) -> None:
        enrolled = _enrolled_snapshot()
        # Same numeric repository_id=9001, but transferred from acme (501) -> newcorp (777)!
        transferred_same_repo_id = replace(
            enrolled,
            owner_id=777,
            owner_login="newcorp",
            repository_slug="newcorp/widgets",
            installation_id=4002,
        )
        self.assertEqual(enrolled.repository_id, transferred_same_repo_id.repository_id)

        transitions = detect_trust_transitions(enrolled, transferred_same_repo_id)
        self.assertIn("ownership_transfer", transitions)
        self.assertNotIn("repository_rename", transitions)
        self.assertIn("app_reinstall", transitions)

        # Synthetic native write gate: ZERO cross-owner writes permitted across all 3 phases!
        dispatched_writes: list[str] = []

        def guarded_publish(
            decision: TrustTransitionDecision, target_slug: str
        ) -> None:
            if decision.allowed:
                dispatched_writes.append(target_slug)

        for phase in (
            "before_admission",
            "before_native_effect",
            "after_unknown_effect",
        ):
            decision = evaluate_trust_boundary(
                enrolled,
                transferred_same_repo_id,
                phase=phase,
                has_prior_charges=True,
            )
            guarded_publish(decision, transferred_same_repo_id.repository_slug)
            self.assertFalse(decision.allowed)
            self.assertTrue(decision.cross_repository_write_blocked)
            self.assertFalse(decision.fresh_allowance_minted)
            self.assertTrue(decision.obligations_preserved)
            self.assertTrue(decision.charges_held)
            self.assertEqual(
                decision.reason_code, "trust-transition-refused:ownership_transfer"
            )
            self.assertIn("authenticated-trust-migration", decision.recovery_routes)
            if phase == "after_unknown_effect":
                self.assertTrue(decision.unknown_effect_held)
                self.assertIn("reconcile-unknown-effect", decision.recovery_routes)

        self.assertEqual(dispatched_writes, [])

    def test_all_seven_transitions_across_all_three_injection_phases(self) -> None:
        enrolled = _enrolled_snapshot()
        revoked_perms = dict(DEFAULT_REQUIRED_INSTALLATION_PERMISSIONS)
        revoked_perms["checks"] = "none"

        scenarios: tuple[tuple[str, AuthenticatedTrustSnapshot, str, str], ...] = (
            (
                "repository_rename",
                replace(enrolled, repository_slug="acme/widgets-next"),
                "review",
                "authenticated-trust-migration",
            ),
            (
                "ownership_transfer",
                replace(
                    enrolled,
                    owner_id=888,
                    owner_login="otherorg",
                    repository_slug="otherorg/widgets",
                ),
                "review",
                "authenticated-trust-migration",
            ),
            (
                "fork_status_change",
                replace(enrolled, is_fork=True),
                "review",
                "unsupported-fork-boundary",
            ),
            (
                "app_reinstall",
                replace(enrolled, installation_id=9999),
                "review",
                "authenticated-trust-migration",
            ),
            (
                "repository_selection_change",
                replace(enrolled, repository_selected=False),
                "review",
                "restore-repository-selection",
            ),
            (
                "permission_revoke_or_restore",
                replace(enrolled, granted_permissions=revoked_perms),
                "review",
                "restore-app-permissions",
            ),
            (
                "actor_privilege_downgrade",
                replace(
                    enrolled,
                    actor_association="COLLABORATOR",
                    actor_permission="write",
                ),
                "override",
                "authorized-maintainer-command",
            ),
        )

        self.assertEqual(
            {kind for kind, _, _, _ in scenarios}, set(TRUST_TRANSITION_KINDS)
        )

        for expected_kind, observed, operation, expected_route in scenarios:
            for phase in TRANSITION_PHASES:
                with self.subTest(transition=expected_kind, phase=phase):
                    has_prior = phase == "before_admission"
                    decision = evaluate_trust_boundary(
                        enrolled,
                        observed,
                        phase=phase,
                        operation=operation,
                        has_prior_charges=has_prior,
                    )
                    self.assertFalse(decision.allowed)
                    self.assertIn(expected_kind, decision.detected_transitions)
                    self.assertEqual(
                        decision.reason_code,
                        f"trust-transition-refused:{expected_kind}",
                    )
                    self.assertFalse(decision.fresh_allowance_minted)
                    self.assertTrue(decision.obligations_preserved)
                    self.assertEqual(
                        decision.charges_held,
                        has_prior or phase == "after_unknown_effect",
                    )
                    self.assertEqual(
                        decision.cross_repository_write_blocked,
                        expected_kind == "ownership_transfer",
                    )
                    self.assertIn(expected_route, decision.recovery_routes)
                    if phase == "after_unknown_effect":
                        self.assertTrue(decision.unknown_effect_held)
                        self.assertIn(
                            "reconcile-unknown-effect", decision.recovery_routes
                        )

    def test_verified_boundary_unknown_effect_and_cross_pr_guards(self) -> None:
        enrolled = _enrolled_snapshot()

        # Clean verified boundary across admission and native-effect gates:
        for phase in ("before_admission", "before_native_effect"):
            ok = evaluate_trust_boundary(
                enrolled,
                enrolled,
                phase=phase,
                operation="reply",
                has_prior_charges=True,
            )
            self.assertTrue(ok.allowed)
            self.assertEqual(ok.detected_transitions, ())
            self.assertEqual(ok.reason_code, "trust-boundary-verified")
            self.assertFalse(ok.cross_repository_write_blocked)
            self.assertTrue(ok.charges_held)
            self.assertEqual(ok.recovery_routes, ("none-needed",))

        # Unchanged snapshot after unknown effect still holds until reconciliation!
        held_unknown = evaluate_trust_boundary(
            enrolled,
            enrolled,
            phase="after_unknown_effect",
        )
        self.assertFalse(held_unknown.allowed)
        self.assertEqual(
            held_unknown.reason_code, "unknown-effect-reconciliation-required"
        )
        self.assertTrue(held_unknown.charges_held)
        self.assertTrue(held_unknown.unknown_effect_held)
        self.assertEqual(
            held_unknown.recovery_routes, ("reconcile-unknown-effect", "verify")
        )

        # Cross-PR mismatch on identical repository is blocked as cross-target risk!
        other_pr = replace(enrolled, pull_request=89)
        pr_mismatch = evaluate_trust_boundary(
            enrolled, other_pr, phase="before_admission"
        )
        self.assertFalse(pr_mismatch.allowed)
        self.assertTrue(pr_mismatch.cross_repository_write_blocked)
        self.assertEqual(
            pr_mismatch.reason_code,
            "trust-transition-refused:cross_pull_request_mismatch",
        )
        self.assertEqual(pr_mismatch.recovery_routes, ("operator-reconcile",))

    def test_end_to_end_rename_and_reinstall_migration_recovers_to_approval(
        self,
    ) -> None:
        enrolled = _enrolled_snapshot()
        session = _session_with_charges(enrolled)
        inventory = _blocking_inventory()
        obligation = inventory.obligations[0]

        # Legitimate repository rename + GitHub App reinstall + restored permission epoch:
        renamed_reinstalled = replace(
            enrolled,
            repository_slug="acme/widgets-v2",
            installation_id=3099,
            permission_epoch=2,
        )

        # Before migration, writes are refused and obligations/charges stay held:
        pre_migration = evaluate_trust_boundary(
            enrolled,
            renamed_reinstalled,
            phase="before_native_effect",
            has_prior_charges=True,
        )
        self.assertFalse(pre_migration.allowed)
        self.assertEqual(
            pre_migration.detected_transitions,
            ("repository_rename", "app_reinstall", "permission_revoke_or_restore"),
        )

        migration = AuthenticatedTrustMigration(
            old_snapshot_sha256=enrolled.digest(),
            new_snapshot_sha256=renamed_reinstalled.digest(),
            operator_id=42,
            operator_login="alice-maintainer",
            operator_association="MEMBER",
            operator_permission="maintain",
            reason="Verified repository rename to acme/widgets-v2 and reinstalled App.",
            operator_attestation_sha256=_sha("maintainer-rename-reinstall-attest"),
        )
        receipt = verify_and_migrate_trust_boundary(
            enrolled=enrolled,
            target=renamed_reinstalled,
            session_record=session,
            inventory=inventory,
            migration=migration,
        )

        # 100% of prior charges, reservations, and blocking obligations survive!
        self.assertFalse(receipt.fresh_allowance_minted)
        self.assertEqual(receipt.migrated_session.repository, "acme/widgets-v2")
        self.assertEqual(receipt.migrated_session.repository_id, 9001)
        self.assertEqual(
            receipt.migrated_session.completed_initial_reviews,
            session.completed_initial_reviews,
        )
        self.assertEqual(
            receipt.migrated_session.completed_verification_rounds,
            session.completed_verification_rounds,
        )
        self.assertEqual(
            receipt.migrated_session.failed_attempts,
            session.failed_attempts,
        )
        self.assertEqual(
            receipt.migrated_session.reservation_id,
            session.reservation_id,
        )
        self.assertEqual(receipt.migrated_session.generation, session.generation + 1)
        self.assertEqual(receipt.preserved_inventory.digest(), inventory.digest())

        # Rebound trust snapshot now passes admission cleanly:
        post_migration = evaluate_trust_boundary(
            receipt.migrated_snapshot,
            renamed_reinstalled,
            phase="before_admission",
            has_prior_charges=True,
        )
        self.assertTrue(post_migration.allowed)

        # Drive preserved blocking obligation to verified fix and clean PR approval!
        clean_result = ReviewResult(
            summary="Webhook signature verification verified on renamed repository.",
            comments=(),
            provider="fixture",
            review_status="complete",
            coverage=CoverageManifest(
                files=(FileCoverage("src/security.py", "reviewed"),),
                enumerated_paths=("src/security.py",),
                enumeration_complete=True,
            ),
        )
        fix = ConfirmedFixEvidence(
            obligation_id=obligation.obligation_id,
            resolution_criterion=obligation.resolution_criterion,
            head_sha=HEAD_SHA_2,
            evidence_sha256=_sha("webhook-hmac-fix-proof"),
            verified_paths=("src/security.py",),
        )
        outcome = evaluate_active_window_round(
            clean_result,
            head_sha=HEAD_SHA_2,
            base_sha=BASE_SHA,
            prior_inventory=receipt.preserved_inventory,
            max_active_findings=3,
            coverage_mode="incremental",
            confirmed_fixes=(fix,),
        )
        self.assertTrue(outcome.approval_eligibility.approved)
        self.assertEqual(outcome.approval_eligibility.outcome, "approved")

    def test_ownership_transfer_requires_dual_attestation_and_effect_reconciliation(
        self,
    ) -> None:
        enrolled = _enrolled_snapshot()
        session = _session_with_charges(enrolled)
        inventory = _blocking_inventory()
        obligation = inventory.obligations[0]

        transferred = replace(
            enrolled,
            owner_id=777,
            owner_login="newcorp",
            repository_slug="newcorp/widgets",
            installation_id=4002,
            actor_id=84,
            actor_login="newcorp-admin",
            actor_association="OWNER",
            actor_permission="admin",
        )
        transfer_attest = compute_transfer_attestation_sha256(
            repository_id=enrolled.repository_id,
            previous_owner_id=enrolled.owner_id,
            previous_owner_login=enrolled.owner_login,
            new_owner_id=transferred.owner_id,
            new_owner_login=transferred.owner_login,
            pull_request=enrolled.pull_request,
            head_sha=enrolled.head_sha,
            permission_epoch=enrolled.permission_epoch,
            previous_owner_witness="Verified prior-owner transfer approval.",
        )

        # 1. Refused without previous_owner_transfer_attestation_sha256 even though repository_id==9001!
        missing_prev_owner = AuthenticatedTrustMigration(
            old_snapshot_sha256=enrolled.digest(),
            new_snapshot_sha256=transferred.digest(),
            operator_id=84,
            operator_login="newcorp-admin",
            operator_association="OWNER",
            operator_permission="admin",
            reason="Attempt transfer migration relying only on repository_id.",
            operator_attestation_sha256=_sha("newcorp-only-attest"),
        )
        with self.assertRaisesRegex(
            ReviewInputError, "unchanged numeric repository_id alone never proves"
        ):
            verify_and_migrate_trust_boundary(
                enrolled=enrolled,
                target=transferred,
                session_record=session,
                inventory=inventory,
                migration=missing_prev_owner,
            )

        # 2. Refused after unknown effect when reconciled_effect_sha256 is absent!
        unreconciled_transfer = AuthenticatedTrustMigration(
            old_snapshot_sha256=enrolled.digest(),
            new_snapshot_sha256=transferred.digest(),
            operator_id=84,
            operator_login="newcorp-admin",
            operator_association="OWNER",
            operator_permission="admin",
            reason="Dual-attested transfer before reconciling unknown effect.",
            operator_attestation_sha256=_sha("newcorp-attest"),
            previous_owner_transfer_attestation_sha256=transfer_attest,
        )
        with self.assertRaisesRegex(
            ReviewInputError, "unknown native effect must be reconciled"
        ):
            verify_and_migrate_trust_boundary(
                enrolled=enrolled,
                target=transferred,
                session_record=session,
                inventory=inventory,
                migration=unreconciled_transfer,
                has_unknown_effect=True,
                expected_transfer_attestation_sha256=transfer_attest,
            )

        # 3. Succeeds with dual owner attestation + reconciled unknown-effect receipt,
        #    then recovers to approval via explicit authorized maintainer override!
        valid_transfer = AuthenticatedTrustMigration(
            old_snapshot_sha256=enrolled.digest(),
            new_snapshot_sha256=transferred.digest(),
            operator_id=84,
            operator_login="newcorp-admin",
            operator_association="OWNER",
            operator_permission="admin",
            reason="Dual-attested org transfer with reconciled readback.",
            operator_attestation_sha256=_sha("newcorp-attest"),
            previous_owner_transfer_attestation_sha256=transfer_attest,
            reconciled_effect_sha256=_sha("owned-readback-reconciled"),
        )
        receipt = verify_and_migrate_trust_boundary(
            enrolled=enrolled,
            target=transferred,
            session_record=session,
            inventory=inventory,
            migration=valid_transfer,
            has_unknown_effect=True,
            expected_transfer_attestation_sha256=transfer_attest,
        )
        self.assertIn("ownership_transfer", receipt.migrated_transitions)
        self.assertEqual(receipt.migrated_session.repository, "newcorp/widgets")
        self.assertFalse(receipt.fresh_allowance_minted)

        # Also verify actor_privilege_downgrade recovery: downgraded actor is refused,
        # while another maintainer/admin authority succeeds and overrides cleanly!
        downgraded_actor = replace(
            receipt.migrated_snapshot,
            actor_id=99,
            actor_login="junior-dev",
            actor_association="COLLABORATOR",
            actor_permission="read",
        )
        self.assertFalse(
            evaluate_trust_boundary(
                receipt.migrated_snapshot,
                downgraded_actor,
                phase="before_admission",
                operation="override",
            ).allowed
        )
        maintainer_actor = replace(
            receipt.migrated_snapshot,
            actor_id=85,
            actor_login="newcorp-sec-lead",
            actor_association="MEMBER",
            actor_permission="maintain",
        )
        self.assertTrue(
            evaluate_trust_boundary(
                receipt.migrated_snapshot,
                maintainer_actor,
                phase="before_admission",
                operation="override",
            ).allowed
        )

        override = AuthorizedHumanOverride(
            obligation_id=obligation.obligation_id,
            action="accept-risk",
            actor="newcorp-admin",
            association="OWNER",
            permission="admin",
            reason="Webhook endpoint behind mTLS gateway; tracked for follow-up.",
            head_sha=HEAD_SHA,
            proof_sha256=_sha("newcorp-admin-override-proof"),
        )
        clean_result = ReviewResult(
            summary="Post-transfer maintainer disposition round.",
            comments=(),
            provider="fixture",
            review_status="complete",
            coverage=CoverageManifest(
                files=(FileCoverage("src/security.py", "reviewed"),),
                enumerated_paths=("src/security.py",),
                enumeration_complete=True,
            ),
        )
        outcome = evaluate_active_window_round(
            clean_result,
            head_sha=HEAD_SHA,
            base_sha=BASE_SHA,
            prior_inventory=receipt.preserved_inventory,
            max_active_findings=3,
            coverage_mode="incremental",
            human_overrides=(override,),
        )
        self.assertTrue(outcome.approval_eligibility.approved)

    def test_migration_and_snapshot_validation_refuse_invalid_boundaries(self) -> None:
        enrolled = _enrolled_snapshot()
        session = _session_with_charges(enrolled)
        inventory = _blocking_inventory()
        renamed = replace(enrolled, repository_slug="acme/widgets-2")

        def make_migration(
            old_snap: AuthenticatedTrustSnapshot,
            new_snap: AuthenticatedTrustSnapshot,
        ) -> AuthenticatedTrustMigration:
            return AuthenticatedTrustMigration(
                old_snapshot_sha256=old_snap.digest(),
                new_snapshot_sha256=new_snap.digest(),
                operator_id=new_snap.actor_id,
                operator_login=new_snap.actor_login,
                operator_association=new_snap.actor_association,
                operator_permission=new_snap.actor_permission,
                reason="Boundary check.",
                operator_attestation_sha256=_sha("attest"),
            )

        # Cross-repository_id, cross-PR, and cross-head_sha migrations unconditionally fail:
        for bad_target in (
            replace(renamed, repository_id=9999),
            replace(renamed, pull_request=999),
            replace(renamed, head_sha=HEAD_SHA_2),
        ):
            with self.assertRaisesRegex(
                ReviewInputError,
                "cannot cross repository_id, pull_request, or head_sha",
            ):
                verify_and_migrate_trust_boundary(
                    enrolled=enrolled,
                    target=bad_target,
                    session_record=session,
                    inventory=inventory,
                    migration=make_migration(enrolled, bad_target),
                )

        # Fork target, unselected repo, and missing permissions fail migration:
        fork_target = replace(renamed, is_fork=True)
        with self.assertRaisesRegex(ReviewInputError, "fork repositories"):
            verify_and_migrate_trust_boundary(
                enrolled=enrolled,
                target=fork_target,
                session_record=session,
                inventory=inventory,
                migration=make_migration(enrolled, fork_target),
            )

        unselected_target = replace(renamed, repository_selected=False)
        with self.assertRaisesRegex(ReviewInputError, "not selected"):
            verify_and_migrate_trust_boundary(
                enrolled=enrolled,
                target=unselected_target,
                session_record=session,
                inventory=inventory,
                migration=make_migration(enrolled, unselected_target),
            )

        bad_perms = dict(DEFAULT_REQUIRED_INSTALLATION_PERMISSIONS)
        bad_perms["pull_requests"] = "read"
        low_perm_target = replace(renamed, granted_permissions=bad_perms)
        with self.assertRaisesRegex(ReviewInputError, "lacks required permissions"):
            verify_and_migrate_trust_boundary(
                enrolled=enrolled,
                target=low_perm_target,
                session_record=session,
                inventory=inventory,
                migration=make_migration(enrolled, low_perm_target),
            )

        # No transition -> refused:
        with self.assertRaisesRegex(
            ReviewInputError, "no trust transition requires migration"
        ):
            verify_and_migrate_trust_boundary(
                enrolled=enrolled,
                target=enrolled,
                session_record=session,
                inventory=inventory,
                migration=make_migration(enrolled, enrolled),
            )

        # Mismatched session_record identity -> refused:
        foreign_session = SessionRecord.create(
            SessionIdentity("other/repo", 88, repository_id=9001),
            now=FIXED_NOW,
        )
        with self.assertRaisesRegex(
            ReviewInputError, "session_record identity does not match"
        ):
            verify_and_migrate_trust_boundary(
                enrolled=enrolled,
                target=renamed,
                session_record=foreign_session,
                inventory=inventory,
                migration=make_migration(enrolled, renamed),
            )

        # Digest mismatches and invalid types in verify_and_migrate_trust_boundary:
        valid_mig = make_migration(enrolled, renamed)
        with self.assertRaisesRegex(ReviewInputError, "old_snapshot_sha256"):
            verify_and_migrate_trust_boundary(
                enrolled=renamed,
                target=renamed,
                session_record=session,
                inventory=inventory,
                migration=valid_mig,
            )
        with self.assertRaisesRegex(ReviewInputError, "new_snapshot_sha256"):
            verify_and_migrate_trust_boundary(
                enrolled=enrolled,
                target=enrolled,
                session_record=session,
                inventory=inventory,
                migration=valid_mig,
            )
        with self.assertRaises(ReviewInputError):
            verify_and_migrate_trust_boundary(
                enrolled="bad",  # type: ignore[arg-type]
                target=renamed,
                session_record=session,
                inventory=inventory,
                migration=valid_mig,
            )
        with self.assertRaises(ReviewInputError):
            verify_and_migrate_trust_boundary(
                enrolled=enrolled,
                target=renamed,
                session_record="bad",  # type: ignore[arg-type]
                inventory=inventory,
                migration=valid_mig,
            )
        with self.assertRaises(ReviewInputError):
            verify_and_migrate_trust_boundary(
                enrolled=enrolled,
                target=renamed,
                session_record=session,
                inventory="bad",  # type: ignore[arg-type]
                migration=valid_mig,
            )
        with self.assertRaises(ReviewInputError):
            verify_and_migrate_trust_boundary(
                enrolled=enrolled,
                target=renamed,
                session_record=session,
                inventory=inventory,
                migration="bad",  # type: ignore[arg-type]
            )

        # Snapshot, Decision, and Migration constructor guardrails:
        with self.assertRaises(ReviewInputError):
            replace(enrolled, repository_slug="otherowner/widgets")
        with self.assertRaises(ReviewInputError):
            replace(enrolled, is_fork=1)  # type: ignore[arg-type]
        with self.assertRaises(ReviewInputError):
            replace(enrolled, repository_id=0)
        with self.assertRaises(ReviewInputError):
            replace(enrolled, owner_login="bad login")
        with self.assertRaises(ReviewInputError):
            replace(enrolled, repository_slug="badslug")
        with self.assertRaises(ReviewInputError):
            replace(enrolled, granted_permissions="bad")  # type: ignore[arg-type]
        with self.assertRaises(ReviewInputError):
            replace(enrolled, granted_permissions={"": "read"})
        with self.assertRaises(ReviewInputError):
            replace(enrolled, granted_permissions={"checks": "superadmin"})
        with self.assertRaises(ReviewInputError):
            replace(enrolled, actor_login="bad actor")
        with self.assertRaises(ReviewInputError):
            replace(enrolled, actor_association="GUEST")
        with self.assertRaises(ReviewInputError):
            replace(enrolled, actor_permission="owner")
        with self.assertRaises(ReviewInputError):
            replace(enrolled, head_sha="not-a-sha")
        with self.assertRaises(ReviewInputError):
            enrolled.actor_meets_operation("merge")
        with self.assertRaises(ReviewInputError):
            detect_trust_transitions("bad", enrolled)  # type: ignore[arg-type]
        with self.assertRaises(ReviewInputError):
            detect_trust_transitions(enrolled, enrolled, operation="merge")
        with self.assertRaises(ReviewInputError):
            evaluate_trust_boundary(enrolled, enrolled, phase="post_merge")

        # Invariant guards on TrustTransitionDecision:
        with self.assertRaises(ReviewInputError):
            TrustTransitionDecision(
                phase="before_admission",
                allowed=False,
                detected_transitions=("repository_rename",),
                reason_code="x",
                cross_repository_write_blocked=True,
                fresh_allowance_minted=True,
                obligations_preserved=True,
                charges_held=False,
                unknown_effect_held=False,
                recovery_routes=("authenticated-trust-migration",),
            )
        with self.assertRaises(ReviewInputError):
            TrustTransitionDecision(
                phase="before_admission",
                allowed=False,
                detected_transitions=("repository_rename",),
                reason_code="x",
                cross_repository_write_blocked=True,
                fresh_allowance_minted=False,
                obligations_preserved=False,
                charges_held=False,
                unknown_effect_held=False,
                recovery_routes=("authenticated-trust-migration",),
            )
        with self.assertRaises(ReviewInputError):
            TrustTransitionDecision(
                phase="invalid",
                allowed=False,
                detected_transitions=(),
                reason_code="x",
                cross_repository_write_blocked=False,
                fresh_allowance_minted=False,
                obligations_preserved=True,
                charges_held=False,
                unknown_effect_held=False,
                recovery_routes=("operator-reconcile",),
            )
        with self.assertRaises(ReviewInputError):
            TrustTransitionDecision(
                phase="before_admission",
                allowed=False,
                detected_transitions=("unknown_kind",),
                reason_code="x",
                cross_repository_write_blocked=False,
                fresh_allowance_minted=False,
                obligations_preserved=True,
                charges_held=False,
                unknown_effect_held=False,
                recovery_routes=("operator-reconcile",),
            )

        # Non-maintainer cannot construct AuthenticatedTrustMigration:
        with self.assertRaisesRegex(
            ReviewInputError, "requires maintainer or admin authority"
        ):
            AuthenticatedTrustMigration(
                old_snapshot_sha256=enrolled.digest(),
                new_snapshot_sha256=renamed.digest(),
                operator_id=99,
                operator_login="contributor-bob",
                operator_association="CONTRIBUTOR",
                operator_permission="read",
                reason="Unauthorized migration attempt.",
                operator_attestation_sha256=_sha("bob"),
            )
        with self.assertRaises(ReviewInputError):
            AuthenticatedTrustMigration(
                old_snapshot_sha256=enrolled.digest(),
                new_snapshot_sha256=renamed.digest(),
                operator_id=42,
                operator_login="bad login",
                operator_association="MEMBER",
                operator_permission="maintain",
                reason="Invalid login.",
                operator_attestation_sha256=_sha("attest"),
            )

    def test_actor_handoff_unfork_epoch_operator_binding_and_audit_digests(
        self,
    ) -> None:
        enrolled = replace(
            _enrolled_snapshot(),
            actor_association="OWNER",
            actor_permission="admin",
            permission_epoch=2,
        )
        session = _session_with_charges(enrolled)
        inventory = _blocking_inventory()

        # 1) RS-366E18: Different actor (actor_id=77) who meets operation ("override")
        #    is NOT flagged as actor_privilege_downgrade even though admin -> maintain!
        peer_maintainer = replace(
            enrolled,
            actor_id=77,
            actor_login="peer-maintainer",
            actor_association="MEMBER",
            actor_permission="maintain",
        )
        self.assertEqual(
            detect_trust_transitions(enrolled, peer_maintainer, operation="override"),
            (),
        )
        peer_dec = evaluate_trust_boundary(
            enrolled,
            peer_maintainer,
            phase="before_native_effect",
            operation="override",
            has_prior_charges=False,
        )
        self.assertTrue(peer_dec.allowed)
        self.assertFalse(peer_dec.charges_held)

        # Same actor (actor_id=42) dropping admin -> maintain IS flagged as downgrade!
        same_actor_drop = replace(
            enrolled,
            actor_association="MEMBER",
            actor_permission="maintain",
        )
        self.assertIn(
            "actor_privilege_downgrade",
            detect_trust_transitions(enrolled, same_actor_drop, operation="override"),
        )

        # 2) RS-377BA7: Unfork transition (True -> False) is blocked from migration
        #    and routed to unsupported-fork-boundary!
        fork_enrolled = replace(enrolled, is_fork=True)
        unforked_target = replace(enrolled, is_fork=False)
        unfork_dec = evaluate_trust_boundary(
            fork_enrolled, unforked_target, phase="before_admission"
        )
        self.assertFalse(unfork_dec.allowed)
        self.assertIn("unsupported-fork-boundary", unfork_dec.recovery_routes)
        self.assertNotIn("authenticated-trust-migration", unfork_dec.recovery_routes)
        with self.assertRaisesRegex(ReviewInputError, "fork repositories"):
            verify_and_migrate_trust_boundary(
                enrolled=fork_enrolled,
                target=unforked_target,
                session_record=_session_with_charges(fork_enrolled),
                inventory=inventory,
                migration=AuthenticatedTrustMigration(
                    old_snapshot_sha256=fork_enrolled.digest(),
                    new_snapshot_sha256=unforked_target.digest(),
                    operator_id=unforked_target.actor_id,
                    operator_login=unforked_target.actor_login,
                    operator_association=unforked_target.actor_association,
                    operator_permission=unforked_target.actor_permission,
                    reason="Attempt unfork migration.",
                    operator_attestation_sha256=_sha("unfork"),
                ),
            )

        # 3) RS-CB5C9C: permission_epoch regression is blocked from migration!
        regressed_epoch = replace(enrolled, permission_epoch=1)
        epoch_dec = evaluate_trust_boundary(
            enrolled, regressed_epoch, phase="before_admission"
        )
        self.assertFalse(epoch_dec.allowed)
        self.assertIn("operator-reconcile", epoch_dec.recovery_routes)
        self.assertNotIn("authenticated-trust-migration", epoch_dec.recovery_routes)
        with self.assertRaisesRegex(
            ReviewInputError, "permission_epoch cannot regress"
        ):
            verify_and_migrate_trust_boundary(
                enrolled=enrolled,
                target=regressed_epoch,
                session_record=session,
                inventory=inventory,
                migration=AuthenticatedTrustMigration(
                    old_snapshot_sha256=enrolled.digest(),
                    new_snapshot_sha256=regressed_epoch.digest(),
                    operator_id=regressed_epoch.actor_id,
                    operator_login=regressed_epoch.actor_login,
                    operator_association=regressed_epoch.actor_association,
                    operator_permission=regressed_epoch.actor_permission,
                    reason="Epoch regression.",
                    operator_attestation_sha256=_sha("epoch-reg"),
                ),
            )

        # Changing granted_permissions without advancing permission_epoch fails closed:
        expanded_perms = dict(DEFAULT_REQUIRED_INSTALLATION_PERMISSIONS)
        expanded_perms["metadata"] = "read"
        stale_epoch_perms = replace(enrolled, granted_permissions=expanded_perms)
        with self.assertRaisesRegex(
            ReviewInputError, "strictly newer permission_epoch"
        ):
            verify_and_migrate_trust_boundary(
                enrolled=enrolled,
                target=stale_epoch_perms,
                session_record=session,
                inventory=inventory,
                migration=AuthenticatedTrustMigration(
                    old_snapshot_sha256=enrolled.digest(),
                    new_snapshot_sha256=stale_epoch_perms.digest(),
                    operator_id=stale_epoch_perms.actor_id,
                    operator_login=stale_epoch_perms.actor_login,
                    operator_association=stale_epoch_perms.actor_association,
                    operator_permission=stale_epoch_perms.actor_permission,
                    reason="Expanded perms without epoch bump.",
                    operator_attestation_sha256=_sha("perm-epoch"),
                ),
            )

        # 4) RS-A6D906: Spoofed operator identity not matching target snapshot fails!
        renamed_target = replace(enrolled, repository_slug="acme/widgets-renamed")
        with self.assertRaisesRegex(
            ReviewInputError, "operator identity and authority must match"
        ):
            verify_and_migrate_trust_boundary(
                enrolled=enrolled,
                target=renamed_target,
                session_record=session,
                inventory=inventory,
                migration=AuthenticatedTrustMigration(
                    old_snapshot_sha256=enrolled.digest(),
                    new_snapshot_sha256=renamed_target.digest(),
                    operator_id=9999,
                    operator_login="spoofed-admin",
                    operator_association="OWNER",
                    operator_permission="admin",
                    reason="Spoofed operator id/login.",
                    operator_attestation_sha256=_sha("spoof"),
                ),
            )

        # 5) RS-6D39F3 & RS-98D766: Transfer attestation mismatch fails closed, and
        #    audit_sha256 binds installation_id and operator authority!
        transferred_target = replace(
            enrolled,
            owner_id=900,
            owner_login="destorg",
            repository_slug="destorg/widgets",
            installation_id=7001,
        )
        expected_attest = compute_transfer_attestation_sha256(
            repository_id=enrolled.repository_id,
            previous_owner_id=enrolled.owner_id,
            previous_owner_login=enrolled.owner_login,
            new_owner_id=transferred_target.owner_id,
            new_owner_login=transferred_target.owner_login,
            pull_request=enrolled.pull_request,
            head_sha=enrolled.head_sha,
            permission_epoch=enrolled.permission_epoch,
            previous_owner_witness="Verified prior-owner transfer approval.",
        )
        with self.assertRaisesRegex(
            ReviewInputError, "does not match verified transfer attestation"
        ):
            verify_and_migrate_trust_boundary(
                enrolled=enrolled,
                target=transferred_target,
                session_record=session,
                inventory=inventory,
                migration=AuthenticatedTrustMigration(
                    old_snapshot_sha256=enrolled.digest(),
                    new_snapshot_sha256=transferred_target.digest(),
                    operator_id=transferred_target.actor_id,
                    operator_login=transferred_target.actor_login,
                    operator_association=transferred_target.actor_association,
                    operator_permission=transferred_target.actor_permission,
                    reason="Wrong transfer attestation.",
                    operator_attestation_sha256=_sha("op"),
                    previous_owner_transfer_attestation_sha256=_sha("wrong-attest"),
                ),
                expected_transfer_attestation_sha256=expected_attest,
            )

        rec_a = verify_and_migrate_trust_boundary(
            enrolled=enrolled,
            target=renamed_target,
            session_record=session,
            inventory=inventory,
            migration=AuthenticatedTrustMigration(
                old_snapshot_sha256=enrolled.digest(),
                new_snapshot_sha256=renamed_target.digest(),
                operator_id=renamed_target.actor_id,
                operator_login=renamed_target.actor_login,
                operator_association=renamed_target.actor_association,
                operator_permission=renamed_target.actor_permission,
                reason="Audit check A.",
                operator_attestation_sha256=_sha("op-a"),
            ),
        )
        renamed_other_install = replace(renamed_target, installation_id=8888)
        rec_b = verify_and_migrate_trust_boundary(
            enrolled=enrolled,
            target=renamed_other_install,
            session_record=session,
            inventory=inventory,
            migration=AuthenticatedTrustMigration(
                old_snapshot_sha256=enrolled.digest(),
                new_snapshot_sha256=renamed_other_install.digest(),
                operator_id=renamed_other_install.actor_id,
                operator_login=renamed_other_install.actor_login,
                operator_association=renamed_other_install.actor_association,
                operator_permission=renamed_other_install.actor_permission,
                reason="Audit check A.",
                operator_attestation_sha256=_sha("op-a"),
            ),
        )
        self.assertNotEqual(rec_a.audit_sha256, rec_b.audit_sha256)


if __name__ == "__main__":
    unittest.main()
